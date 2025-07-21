# handler/inventory_handler.py
"""
InventoryHandler
────────────────
Bulk‑refill warehouse inventory until each SKU reaches
`threshold / averagerequired` (columns in **item**).

• One synthetic PO per supplier per cycle  ⇒  FAST.
• Unit‑cost = 75 % of `sellingprice`  (0 if price is 0 / NULL).
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100
FIX_EXPIRY_DATE   = date(2027, 7, 21)
FIX_STORAGE_LOC   = "A2"


class InventoryHandler(DatabaseManager):
    # ───────────────── snapshot ─────────────────
    def stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity)::int AS totalqty "
            "FROM inventory GROUP BY itemid"
        )
        if inv.empty:
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                   COALESCE(averagerequired, {DEFAULT_AVERAGE})   AS average,
                   COALESCE(sellingprice, 0)                      AS sellingprice
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ─────────── supplier helpers ───────────
    def supplier_for(self, itemid: int) -> int | None:
        rows = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return None if rows.empty else int(rows.iloc[0, 0])

    # ─────────── PO helpers ───────────
    def create_supplier_po(self, supplier_id: int) -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,actualdelivery,
                   createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                    'AutoInventory','AUTO BULK',0)
            RETURNING poid
            """,
            (supplier_id,),
        )[0]
        return int(poid)

    def add_po_lines_and_costs(
        self, poid: int, rows: list[tuple[int, int, float]]
    ) -> list[int]:
        """
        rows -> [(itemid, qty, cpu), …]   · returns the new costids (same order)
        """
        cost_ids: list[int] = []

        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO purchaseorderitems "
                    "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
                    "VALUES %s",
                    [(poid, it, q, q, cpu) for it, q, cpu in rows],
                )

                for itemid, qty, cpu in rows:
                    cur.execute(
                        "INSERT INTO poitemcost "
                        "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
                        "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,'Auto Refill') "
                        "RETURNING costid",
                        (poid, itemid, cpu, qty),
                    )
                    cost_ids.append(int(cur.fetchone()[0]))
        return cost_ids

    # ─────────── bulk inventory insert ───────────
    def bulk_inventory_rows(
        self, tuples_: list[tuple[int, int, float, int, int]]
    ) -> None:
        """
        tuples_ -> (itemid, qty, cpu, poid, costid)
        """
        values = [
            (
                itemid,
                qty,
                FIX_EXPIRY_DATE,
                FIX_STORAGE_LOC,
                cpu,
                poid,
                costid,
            )
            for itemid, qty, cpu, poid, costid in tuples_
        ]

        with self.conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO inventory "
                "(itemid,quantity,expirationdate,storagelocation,"
                " cost_per_unit,poid,costid) VALUES %s",
                values,
            )
        self.conn.commit()

    # ─────────── public bulk restock ───────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need columns → itemid · need · sellingprice
        """
        df_need = df_need.copy()

        # *** CRUCIAL: cast to int() before passing to SQL ***
        df_need["supplier"] = df_need["itemid"].apply(
            lambda x: self.supplier_for(int(x))
        )
        df_need.dropna(subset=["supplier"], inplace=True)

        action_log: list[dict] = []

        for sup_id, group in df_need.groupby("supplier"):
            sup_id = int(sup_id)
            poid   = self.create_supplier_po(sup_id)

            po_rows  = []  # for PO + cost
            inv_rows = []  # for inventory insert

            for _, r in group.iterrows():
                need = int(r.need)
                if need <= 0:
                    continue

                cpu = round(float(r.sellingprice) * 0.75, 2) if r.sellingprice else 0
                po_rows.append((int(r.itemid), need, cpu))

            if not po_rows:
                continue

            cost_ids = self.add_po_lines_and_costs(poid, po_rows)

            for (itemid, qty, cpu), costid in zip(po_rows, cost_ids):
                inv_rows.append((itemid, qty, cpu, poid, costid))
                action_log.append(
                    dict(itemid=itemid, added=qty, cpu=cpu, poid=poid)
                )

            self.bulk_inventory_rows(inv_rows)

        return action_log
