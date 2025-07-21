# handler/inventory_handler.py
"""
InventoryHandler
────────────────
Refills warehouse stock until every SKU reaches
`threshold` / `averagerequired` (columns in **item**).

• One synthetic PO **per supplier per cycle** → very fast.
• Unit‑cost = 75 % of `sellingprice` (0 when price is 0 / NULL).
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100
FIX_EXPIRY        = date(2027, 7, 21)   # placeholder
FIX_WH_LOC        = "A2"                # warehouse slot


class InventoryHandler(DatabaseManager):
    # ─────────────────── stock snapshot ───────────────────
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
                   COALESCE(sellingprice,0)  AS sellingprice
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────── supplier look‑up ─────────
    def supplier_for(self, itemid: int) -> int | None:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return None if res.empty else int(res.iloc[0, 0])

    # ───────── PO helpers ─────────
    def _create_po(self, supplier_id: int) -> int:
        return int(
            self.execute_command_returning(
                """
                INSERT INTO purchaseorders
                      (supplierid,status,orderdate,expecteddelivery,
                       actualdelivery,createdby,suppliernote,totalcost)
                VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                        CURRENT_DATE,'AutoInventory','AUTO BULK',0)
                RETURNING poid
                """,
                (supplier_id,),
            )[0]
        )

    def _add_po_lines_and_costs(
        self, poid: int, items: list[tuple[int, int, float]]
    ) -> list[int]:
        """
        *items* → List[(itemid, qty, cpu)] – returns matching costids.
        """
        po_rows   = [(poid, it, q, q, cpu) for it, q, cpu in items]
        cost_ids  = []

        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO purchaseorderitems "
                    "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
                    "VALUES %s",
                    po_rows,
                )

                for itemid, qty, cpu in [(r[1], r[2], r[4]) for r in po_rows]:
                    cur.execute(
                        "INSERT INTO poitemcost "
                        "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
                        "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,'Auto Refill') "
                        "RETURNING costid",
                        (poid, itemid, cpu, qty),
                    )
                    cost_ids.append(int(cur.fetchone()[0]))

        return cost_ids

    # ───────── inventory bulk insert ─────────
    def _insert_inventory(self, rows: list[tuple[int,int,float,int,int]]):
        """
        rows → (itemid, qty, cpu, poid, costid)
        """
        tuples_ = [
            (item, qty, FIX_EXPIRY, FIX_WH_LOC, cpu, poid, costid)
            for item, qty, cpu, poid, costid in rows
        ]
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO inventory "
                "(itemid,quantity,expirationdate,storagelocation,"
                " cost_per_unit,poid,costid) VALUES %s",
                tuples_,
            )
        self.conn.commit()

    # ───────── public bulk restock ─────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need columns: itemid · need · sellingprice
        Returns list of action log dicts.
        """
        df_need = df_need.copy()
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)
        df_need.dropna(subset=["supplier"], inplace=True)

        log: list[dict] = []

        for sup_id, grp in df_need.groupby("supplier"):
            poid   = self._create_po(int(sup_id))
            items  = []
            for _, r in grp.iterrows():
                cpu  = round(float(r.sellingprice) * 0.75, 2) if r.sellingprice else 0.0
                items.append((int(r.itemid), int(r.need), cpu))

            if not items:
                continue

            cost_ids = self._add_po_lines_and_costs(poid, items)
            inv_rows = [
                (it, qty, cpu, poid, cid)
                for (it, qty, cpu), cid in zip(items, cost_ids)
            ]
            self._insert_inventory(inv_rows)

            for (it, qty, cpu), cid in zip(items, cost_ids):
                log.append(dict(itemid=it, added=qty,
                                cpu=cpu, poid=poid, costid=cid))

        return log
