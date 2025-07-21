"""
InventoryHandler
────────────────
Refills warehouse stock whenever an SKU’s quantity drops below
`threshold`, topping it up to `averagerequired`.

* Supplier is resolved from itemsupplier.
* Synthetic POs are zero‑cost when sellingprice is 0 / NULL,
  otherwise cost_per_unit is 75 % of sellingprice.
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryHandler(DatabaseManager):
    # ───────────── snapshot ─────────────
    def stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity) AS totalqty "
            "FROM inventory GROUP BY itemid"
        )
        if inv.empty:
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                   COALESCE(averagerequired, {DEFAULT_AVERAGE})   AS averagerequired,
                   COALESCE(sellingprice ,0)                     AS sellingprice
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ─────────── supplier look‑up ───────────
    def supplier_for_item(self, itemid: int) -> int | None:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid=%s LIMIT 1",
            (itemid,),
        )
        return None if res.empty else int(res.iloc[0, 0])

    # ─────────── PO helpers ───────────
    def _create_po(self, supplier_id: int) -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,actualdelivery,
                   createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                    'AutoInventory','AUTO REFILL',0.0)
            RETURNING poid
            """,
            (supplier_id,),
        )[0]
        return int(poid)

    def _add_po_item(self, poid: int, itemid: int, qty: int, cpu: float):
        self.execute_command(
            "INSERT INTO purchaseorderitems "
            "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
            "VALUES (%s,%s,%s,%s,%s)",
            (poid, itemid, qty, qty, cpu),
        )

    def _insert_cost(self, poid: int, itemid: int,
                     cpu: float, qty: int) -> int:
        costid = self.execute_command_returning(
            "INSERT INTO poitemcost "
            "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
            "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,'AUTO REFILL') RETURNING costid",
            (poid, itemid, cpu, qty),
        )[0]
        return int(costid)

    def _refresh_po_cost(self, poid: int):
        self.execute_command(
            "UPDATE purchaseorders "
            "SET totalcost = COALESCE(("
            "SELECT SUM(quantity*cost_per_unit) FROM poitemcost WHERE poid=%s"
            "),0) WHERE poid=%s",
            (poid, poid),
        )

    # ─────────── inventory insert ───────────
    def _add_inventory_rows(self, rows: list[dict]):
        tuples = [
            (r["item_id"], r["quantity"], r["expiration_date"],
             r["storage_location"], r["cost_per_unit"], r["poid"], r["costid"])
            for r in rows
        ]
        sql = ("INSERT INTO inventory "
               "(itemid,quantity,expirationdate,storagelocation,"
               " cost_per_unit,poid,costid) VALUES %s")
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(cur, sql, tuples)
        self.conn.commit()

    # ─────────── public refill ───────────
    def refill(self, *, itemid: int, qty_needed: int) -> str:
        """
        Top‑up *itemid* by *qty_needed* units.

        • Calculates cost_per_unit = sellingprice × 0.75 (rounded 2 d.p.).
        • If sellingprice is 0 / NULL ⇒ cost_per_unit = 0.
        """
        if qty_needed <= 0:
            return "SKIP"

        supplier_id = self.supplier_for_item(itemid)
        if supplier_id is None:
            return "NO SUPPLIER"

        # fetch sellingprice once
        sp_df = self.fetch_data(
            "SELECT COALESCE(sellingprice,0) AS sp FROM item WHERE itemid=%s",
            (itemid,),
        )
        selling_price = float(sp_df.iloc[0, 0]) if not sp_df.empty else 0.0
        cpu = round(selling_price * 0.75, 2) if selling_price > 0 else 0.0

        poid   = self._create_po(supplier_id)
        self._add_po_item(poid, itemid, qty_needed, cpu)
        costid = self._insert_cost(poid, itemid, cpu, qty_needed)
        self._add_inventory_rows(
            [
                dict(
                    item_id          = itemid,
                    quantity         = qty_needed,
                    expiration_date  = date(2027, 7, 21),   # fixed for sim
                    storage_location = "SECTION A2",
                    cost_per_unit    = cpu,
                    poid             = poid,
                    costid           = costid,
                )
            ]
        )
        self._refresh_po_cost(poid)
        return f"OK PO#{poid}"
