"""
InventoryHandler
────────────────
Refills warehouse inventory automatically whenever an SKU’s stock
falls below its `threshold`, topping it up to `averagerequired`.

• Supplier is resolved via itemsupplier.
• Each refill uses a synthetic PO (status = Completed, cost = 0).
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryHandler(DatabaseManager):
    # ───────────────────── current snapshot ─────────────────────
    def stock_levels(self) -> pd.DataFrame:
        """Return inventory qty merged with per‑item thresholds/averages."""
        inv = self.fetch_data(
            "SELECT itemid, COALESCE(SUM(quantity),0) AS totalqty "
            "FROM inventory GROUP BY itemid"
        )

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                   COALESCE(averagerequired, {DEFAULT_AVERAGE})   AS average_required
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────── supplier lookup ─────────────────────
    def supplier_for_item(self, itemid: int) -> int | None:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return None if res.empty else int(res.iloc[0, 0])

    # ───────────────────── PO helpers ──────────────────────────
    def _create_po(self, supplier_id: int, note="AUTO REFILL") -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,actualdelivery,
                   createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                    'AutoInventory',%s,0.0)
            RETURNING poid
            """,
            (supplier_id, note),
        )[0]
        return int(poid)

    def _add_po_line(self, poid: int, itemid: int, qty: int, cpu: float):
        self.execute_command(
            "INSERT INTO purchaseorderitems "
            "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
            "VALUES (%s,%s,%s,%s,%s)",
            (poid, itemid, qty, qty, cpu),
        )

    def _add_cost_row(self, poid: int, itemid: int, cpu: float, qty: int, note: str) -> int:
        costid = self.execute_command_returning(
            "INSERT INTO poitemcost "
            "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
            "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING costid",
            (poid, itemid, cpu, qty, note),
        )[0]
        return int(costid)

    def _insert_inventory_layer(
        self,
        itemid: int,
        qty: int,
        poid: int,
        costid: int,
        *,
        cpu: float = 0.0,
        expiry=date(2027, 7, 21),
        location="A2",
    ):
        self.add_inventory_rows(
            [
                dict(
                    item_id=itemid,
                    quantity=qty,
                    expiration_date=expiry,
                    storage_location=location,
                    cost_per_unit=cpu,
                    poid=poid,
                    costid=costid,
                )
            ]
        )

    # low‑level batch insert
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

    # ───────────────────── public refill API ─────────────────────
    def restock_item(
        self,
        itemid: int,
        need: int,
        *,
        cpu: float = 0.0,
        note="AUTO REFILL",
    ) -> int | None:
        """
        Top‑up *need* units of *itemid*; return generated POID or None.
        If supplier missing → ValueError is raised.
        """
        if need <= 0:
            return None

        supplier_id = self.supplier_for_item(itemid)
        if supplier_id is None:
            raise ValueError("Supplier not defined for item")

        poid   = self._create_po(supplier_id, note)
        costid = self._add_cost_row(poid, itemid, cpu, need, note)
        self._add_po_line(poid, itemid, need, cpu)
        self._insert_inventory_layer(itemid, need, poid, costid, cpu=cpu)
        return poid
