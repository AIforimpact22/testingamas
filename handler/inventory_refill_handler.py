# handler/inventory_refill_handler.py
from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryRefillHandler(DatabaseManager):
    # ───────────────────────── snapshot ─────────────────────────
    def _stock_levels(self) -> pd.DataFrame:
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
                   COALESCE(shelfthreshold,{DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(shelfaverage,  {DEFAULT_AVERAGE})   AS inventoryaverage
            FROM   item;
            """
        )
        df = item_meta.merge(inv_totals, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────────── suppliers ────────────────────────
    def get_suppliers(self) -> pd.DataFrame:
        return self.fetch_data(
            "SELECT supplierid, suppliername FROM supplier ORDER BY suppliername"
        )

    # ─────────────── PO / cost / inventory helpers ──────────────
    def create_manual_po(self, supplier_id: int, note="AUTO REFILL") -> int:
        return int(
            self.execute_command_returning(
                """
                INSERT INTO purchaseorders
                      (supplierid, status, orderdate, expecteddelivery,
                       actualdelivery, createdby, suppliernote, totalcost)
                VALUES (%s, 'Completed', CURRENT_DATE, CURRENT_DATE,
                        CURRENT_DATE, 'AutoInventory', %s, 0.0)
                RETURNING poid;""",
                (supplier_id, note),
            )[0]
        )

    def add_po_item(self, poid: int, item_id: int, qty: int, cost: float):
        self.execute_command(
            """
            INSERT INTO purchaseorderitems
                  (poid, itemid, orderedquantity, receivedquantity, estimatedprice)
            VALUES (%s, %s, %s, %s, %s);""",
            (poid, item_id, qty, qty, cost),
        )

    def insert_poitem_cost(
        self, poid: int, item_id: int, cpu: float, qty: int, note: str
    ) -> int:
        return int(
            self.execute_command_returning(
                """
                INSERT INTO poitemcost (poid, itemid, cost_per_unit,
                                        quantity, cost_date, note)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
                RETURNING costid;""",
                (poid, item_id, cpu, qty, note),
            )[0]
        )

    def add_inventory_rows(self, rows: list[dict]) -> None:
        tuples = [
            (
                r["item_id"],
                r["quantity"],
                r["expiration_date"],
                r["storage_location"],
                r["cost_per_unit"],
                r["poid"],
                r["costid"],
            )
            for r in rows
        ]
        self._ensure_live_conn()
        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO inventory
                          (itemid, quantity, expirationdate,
                           storagelocation, cost_per_unit, poid, costid)
                    VALUES %s;""",
                    tuples,
                )

    def refresh_po_total_cost(self, poid: int):
        self.execute_command(
            """
            UPDATE purchaseorders
            SET    totalcost = COALESCE((
                     SELECT SUM(quantity * cost_per_unit)
                     FROM   poitemcost
                     WHERE  poid = %s),0)
            WHERE  poid = %s;""",
            (poid, poid),
        )

    # ───────────────────── restock one item ─────────────────────
    def restock_item(
        self,
        itemid: int,
        supplier_id: int,
        need: int,
        *,
        cpu: float = 0.0,
        note="AUTO REFILL",
    ) -> int:
        """Return new POID or −1 if nothing needed."""
        if need <= 0:
            return -1
        poid = self.create_manual_po(supplier_id, note)
        self.add_po_item(poid, itemid, need, cpu)
        costid = self.insert_poitem_cost(poid, itemid, cpu, need, note)
        self.add_inventory_rows(
            [
                dict(
                    item_id=itemid,
                    quantity=need,
                    expiration_date=date.today(),
                    storage_location="AUTO",
                    cost_per_unit=cpu,
                    poid=poid,
                    costid=costid,
                )
            ]
        )
        self.refresh_po_total_cost(poid)
        return poid
