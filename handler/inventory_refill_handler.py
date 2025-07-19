# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
======================
Automatically refills warehouse inventory when stock drops below each
item’s `inventorythreshold`.

Because `receive_handler.py` has been removed, this class now inherits
directly from `db_handler.DatabaseManager` and re‑implements the minimal
purchase‑order / inventory SQL helpers it needs.
"""

from __future__ import annotations
from datetime import date
import pandas as pd

from db_handler import DatabaseManager
from psycopg2.extras import execute_values

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryRefillHandler(DatabaseManager):
    # ───────────────────── stock snapshot ─────────────────────
    def _stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            """
            SELECT itemid, SUM(quantity) AS totalqty
            FROM   inventory
            GROUP  BY itemid;
            """
        )

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(inventorythreshold,{DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(inventoryaverage,  {DEFAULT_AVERAGE})   AS inventoryaverage
            FROM   item;
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────── supplier helper ─────────────────────
    def get_suppliers(self) -> pd.DataFrame:
        return self.fetch_data(
            "SELECT supplierid, suppliername FROM supplier ORDER BY suppliername"
        )

    # ───────────────────── PO / inventory helpers ─────────────────────
    def create_manual_po(self, supplier_id: int, note: str = "") -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid, status, orderdate, expecteddelivery,
                   actualdelivery, createdby, suppliernote, totalcost)
            VALUES (%s, 'Completed', CURRENT_DATE, CURRENT_DATE,
                    CURRENT_DATE, 'AutoInventory', %s, 0.0)
            RETURNING poid
            """,
            (supplier_id, note),
        )[0]
        return int(poid)

    def add_po_item(self, poid: int, item_id: int, qty: int, cost: float):
        self.execute_command(
            """
            INSERT INTO purchaseorderitems
                  (poid, itemid, orderedquantity, receivedquantity, estimatedprice)
            VALUES (%s, %s, %s, %s, %s)
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
            RETURNING costid
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
            VALUES %s
        """
        self._ensure_live_conn()
        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(cur, sql, tuples)

    def refresh_po_total_cost(self, poid: int):
        self.execute_command(
            """
            UPDATE purchaseorders
            SET    totalcost = COALESCE((
                    SELECT SUM(quantity * cost_per_unit)
                    FROM   poitemcost
                    WHERE  poid = %s
                  ),0)
            WHERE  poid = %s
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
        """Create synthetic PO, insert inventory layer; return POID."""
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

    # ─────────────────── bulk check & refill ───────────────────
    def check_and_restock_all(
        self,
        supplier_id: int,
        *,
        dry_run: bool = False,
    ) -> pd.DataFrame:
        df    = self._stock_levels()
        needs = df[df.totalqty < df.inventorythreshold].copy()

        actions = []
        for _, row in needs.iterrows():
            need_units = int(row.inventoryaverage) - int(row.totalqty)
            poid = None
            if not dry_run:
                poid = self.restock_item(
                    int(row.itemid), supplier_id, need_units
                )
            actions.append({
                "itemid"      : int(row.itemid),
                "itemname"    : row.itemnameenglish,
                "current_qty" : int(row.totalqty),
                "threshold"   : int(row.inventorythreshold),
                "target"      : int(row.inventoryaverage),
                "added"       : need_units,
                "poid"        : poid,
            })
        return pd.DataFrame(actions)
