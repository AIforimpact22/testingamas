# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
──────────────────────
Refills warehouse inventory when total stock for an item falls below
its `shelfthreshold` (fallback = 50).  It tops the item back up to
`shelfaverage` (fallback = 100) by creating a synthetic **Completed**
purchase‑order and inserting a new inventory layer.

This handler depends only on `db_handler.DatabaseManager`; no other
handlers are required.
"""

from __future__ import annotations
from datetime import date

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50   # used when shelfthreshold is NULL / missing
DEFAULT_AVERAGE   = 100  # used when shelfaverage   is NULL / missing


class InventoryRefillHandler(DatabaseManager):
    # ───────────────────── inventory snapshot ─────────────────────
    def _stock_levels(self) -> pd.DataFrame:
        """
        Merge current inventory totals with per‑item thresholds & targets.
        Falls back to DEFAULT_THRESHOLD / DEFAULT_AVERAGE when necessary.
        """
        inv_totals = self.fetch_data(
            """
            SELECT itemid, SUM(quantity) AS totalqty
            FROM   inventory
            GROUP  BY itemid;
            """
        )

        item_meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(shelfthreshold, {DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(shelfaverage,   {DEFAULT_AVERAGE})   AS inventoryaverage
            FROM   item;
            """
        )

        df = item_meta.merge(inv_totals, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────── supplier helper ─────────────────────
    def get_suppliers(self) -> pd.DataFrame:
        return self.fetch_data(
            "SELECT supplierid, suppliername FROM supplier ORDER BY suppliername"
        )

    # ───────────────────── PO / inventory primitives ─────────────────────
    def create_manual_po(self, supplier_id: int, note: str = "") -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid, status, orderdate, expecteddelivery,
                   actualdelivery, createdby, suppliernote, totalcost)
            VALUES (%s, 'Completed', CURRENT_DATE, CURRENT_DATE,
                    CURRENT_DATE, 'AutoInventory', %s, 0.0)
            RETURNING poid;
            """,
            (supplier_id, note),
        )[0]
        return int(poid)

    def add_po_item(self, poid: int, item_id: int, qty: int, cost: float):
        self.execute_command(
            """
            INSERT INTO purchaseorderitems
                  (poid, itemid, orderedquantity, receivedquantity, estimatedprice)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (poid, item_id, qty, qty, cost),
        )

    def insert_poitem_cost(
        self, poid: int, item_id: int, cost_per_unit: float, qty: int, note: str
    ) -> int:
        costid = self.execute_command_returning(
            """
            INSERT INTO poitemcost (poid, itemid, cost_per_unit,
                                    quantity, cost_date, note)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
            RETURNING costid;
            """,
            (poid, item_id, cost_per_unit, qty, note),
        )[0]
        return int(costid)

    def add_items_to_inventory(self, rows: list[dict]) -> None:
        tuples = [
            (
                int(r["item_id"]),
                int(r["quantity"]),
                r["expiration_date"],
                r["storage_location"],
                float(r["cost_per_unit"]),
                r["poid"],
                r["costid"],
            )
            for r in rows
        ]

        sql = """
            INSERT INTO inventory
                  (itemid, quantity, expirationdate,
                   storagelocation, cost_per_unit, poid, costid)
            VALUES %s;
        """
        self._ensure_live_conn()
        with self.conn:  # one BEGIN/COMMIT block
            with self.conn.cursor() as cur:
                execute_values(cur, sql, tuples)

    def refresh_po_total_cost(self, poid: int):
        self.execute_command(
            """
            UPDATE purchaseorders
            SET    totalcost = COALESCE(
                     (SELECT SUM(quantity * cost_per_unit)
                      FROM   poitemcost
                      WHERE  poid = %s), 0)
            WHERE  poid = %s;
            """,
            (poid, poid),
        )

    # ─────────────────── single‑item restock ───────────────────
    def restock_item(
        self,
        itemid: int,
        supplier_id: int,
        need: int,
        *,
        cost_per_unit: float = 0.0,
        note: str = "Auto‑Inventory Refill",
    ) -> int:
        """
        Create synthetic PO and insert a new inventory layer for *need* units.
        Returns the generated POID (or −1 if need ≤ 0).
        """
        if need <= 0:
            return -1

        poid = self.create_manual_po(supplier_id, note=note)
        self.add_po_item(poid, itemid, need, cost_per_unit)
        costid = self.insert_poitem_cost(
            poid, itemid, cost_per_unit, need, note=note
        )

        self.add_items_to_inventory([{
            "item_id"         : itemid,
            "quantity"        : need,
            "expiration_date" : date.today(),
            "storage_location": "AUTO",
            "cost_per_unit"   : cost_per_unit,
            "poid"            : poid,
            "costid"          : costid,
        }])

        self.refresh_po_total_cost(poid)
        return poid
