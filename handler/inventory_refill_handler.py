# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
──────────────────────
Automatically refills inventory for any SKU whose total on‑hand
quantity drops below `threshold`, topping it up to `averagerequired`.

Supplier is determined automatically from the itemsupplier table.
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50       # used when `threshold` is NULL
DEFAULT_AVERAGE   = 100      # used when `averagerequired` is NULL


class InventoryRefillHandler(DatabaseManager):
    # ───────────────────────── snapshot ─────────────────────────
    def _stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity) AS totalqty "
            "FROM inventory GROUP BY itemid;"
        )

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(averagerequired, {DEFAULT_AVERAGE})   AS inventoryaverage
            FROM   item;
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────── supplier lookup ─────────────────────
    def get_supplier_for_item(self, itemid: int) -> int | None:
        df = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return None if df.empty else int(df.iloc[0, 0])

    # ───────────────────── PO & cost helpers ───────────────────
    def create_manual_po(self, supplier_id: int, note="AUTO REFILL") -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,actualdelivery,
                   createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                    'AutoInventory',%s,0.0)
            RETURNING poid;
            """,
            (supplier_id, note),
        )[0]
        return int(poid)

    def add_po_item(self, poid, item_id, qty, cost):
        self.execute_command(
            "INSERT INTO purchaseorderitems "
            "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
            "VALUES (%s,%s,%s,%s,%s)",
            (poid, item_id, qty, qty, cost),
        )

    def insert_poitem_cost(self, poid, item_id, cpu, qty, note) -> int:
        costid = self.execute_command_returning(
            "INSERT INTO poitemcost "
            "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
            "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING costid",
            (poid, item_id, cpu, qty, note),
        )[0]
        return int(costid)

    def add_inventory_rows(self, rows: list[dict]):
        tuples = [
            (
                r["item_id"], r["quantity"], r["expiration_date"],
                r["storage_location"], r["cost_per_unit"], r["poid"], r["costid"]
            )
            for r in rows
        ]
        sql = ("INSERT INTO inventory "
               "(itemid,quantity,expirationdate,storagelocation,"
               " cost_per_unit,poid,costid) VALUES %s")
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(cur, sql, tuples)
        self.conn.commit()

    def refresh_po_total_cost(self, poid: int):
        self.execute_command(
            "UPDATE purchaseorders SET totalcost = COALESCE(("
            "SELECT SUM(quantity*cost_per_unit) FROM poitemcost WHERE poid=%s"
            "),0) WHERE poid=%s",
            (poid, poid),
        )

    # ───────────────────────── restock ─────────────────────────
    def restock_item(
        self,
        itemid: int,
        need: int,
        *,
        cpu: float = 0.0,
        note="AUTO REFILL",
    ) -> str:
        """
        Restock *need* units of *itemid*.
        • Supplier auto‑resolved via itemsupplier.
        • Returns status string for UI.
        """
        if need <= 0:
            return "NOT NEEDED"

        supplier_id = self.get_supplier_for_item(itemid)
        if supplier_id is None:
            return "NO SUPPLIER"

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
        return f"OK PO#{poid}"
