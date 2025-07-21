"""
InventoryHandler
────────────────
Refills warehouse inventory whenever an SKU’s on‑hand quantity
falls below its `threshold`, topping it up to `average_required`.

• Supplier is auto‑resolved from `itemsupplier`.
• All synthetic POs are created with zero cost (simulation).
• If no inventory is present the merge still succeeds.
"""

from __future__ import annotations

from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager


DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryHandler(DatabaseManager):
    # ───────────────────────── inventory snapshot ─────────────────────────
    def stock_levels(self) -> pd.DataFrame:
        """
        Return a DataFrame with:
        itemid · itemnameenglish · threshold · average_required · totalqty
        Robust to an empty inventory table.
        """
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity) AS totalqty "
            "FROM inventory GROUP BY itemid"
        )
        if inv.empty:                                # ensure expected columns
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                   COALESCE(average_required,{DEFAULT_AVERAGE})   AS average_required
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────────────────── supplier helpers ──────────────────────────
    def supplier_for_item(self, itemid: int) -> int | None:
        df = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return None if df.empty else int(df.iloc[0, 0])

    # ───────────────────────── PO helpers ────────────────────────────────
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

    def _add_po_item(self, poid: int, itemid: int, qty: int, cpu: float = 0.0):
        self.execute_command(
            """
            INSERT INTO purchaseorderitems
                  (poid,itemid,orderedquantity,receivedquantity,estimatedprice)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (poid, itemid, qty, qty, cpu),
        )

    def _insert_cost(self, poid: int, itemid: int, cpu: float,
                     qty: int, note="AUTO REFILL") -> int:
        costid = self.execute_command_returning(
            """
            INSERT INTO poitemcost
                  (poid,itemid,cost_per_unit,quantity,cost_date,note)
            VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,%s)
            RETURNING costid
            """,
            (poid, itemid, cpu, qty, note),
        )[0]
        return int(costid)

    def _refresh_po_cost(self, poid: int):
        self.execute_command(
            """
            UPDATE purchaseorders
            SET totalcost = COALESCE((
                SELECT SUM(quantity*cost_per_unit)
                FROM   poitemcost
                WHERE  poid = %s
            ),0)
            WHERE poid = %s
            """,
            (poid, poid),
        )

    # ───────────────────────── inventory insert ──────────────────────────
    def _add_inventory_rows(self, rows: list[dict]):
        tuples = [
            (
                r["item_id"],          # 1
                r["quantity"],         # 2
                r["expiration_date"],  # 3
                r["storage_location"], # 4
                r["cost_per_unit"],    # 5
                r["poid"],             # 6
                r["costid"],           # 7
            )
            for r in rows
        ]
        sql = (
            "INSERT INTO inventory "
            "(itemid,quantity,expirationdate,storagelocation,"
            " cost_per_unit,poid,costid) VALUES %s"
        )
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(cur, sql, tuples)
        self.conn.commit()

    # ───────────────────────── public refill ─────────────────────────────
    def refill(self, *, itemid: int, qty_needed: int,
               cpu: float = 0.0) -> str:
        """
        Top‑up *itemid* by *qty_needed* units.
        Returns a status string for UI.
        """
        if qty_needed <= 0:
            return "SKIP"

        sup_id = self.supplier_for_item(itemid)
        if sup_id is None:
            return "NO SUPPLIER"

        poid = self._create_po(sup_id)
        self._add_po_item(poid, itemid, qty_needed, cpu)
        costid = self._insert_cost(poid, itemid, cpu, qty_needed)
        self._add_inventory_rows(
            [
                dict(
                    item_id=itemid,
                    quantity=qty_needed,
                    expiration_date=date(2027, 7, 21),   # fixed future date
                    storage_location="SECTION A2",
                    cost_per_unit=cpu,
                    poid=poid,
                    costid=costid,
                )
            ]
        )
        self._refresh_po_cost(poid)
        return f"OK PO#{poid}"
