"""
SellingAreaHandler  (bulk mode, single‑txn)
───────────────────────────────────────────
• Pulls needed layers from *inventory* → *shelf* in one SQL transaction.
• Keeps `(itemid, expirationdate, cost_per_unit, locid)` unique in *shelf*.
"""

from __future__ import annotations
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_LOCID = "UNASSIGNED"   # fallback when item_slot has no row


class SellingAreaHandler(DatabaseManager):
    # ───────── KPI snapshot ─────────
    def shelf_kpis(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  i.itemid,
                    i.itemnameenglish,
                    COALESCE(SUM(s.quantity),0)::int AS totalqty,
                    i.shelfthreshold,
                    i.shelfaverage
            FROM    item i
            LEFT JOIN shelf s ON s.itemid = i.itemid
            GROUP   BY i.itemid, i.itemnameenglish,
                      i.shelfthreshold, i.shelfaverage
            """
        )

    # ───────── location helper ─────────
    def loc_for_item(self, itemid: int) -> str:
        res = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1", (itemid,)
        )
        return DEFAULT_LOCID if res.empty else res.iloc[0, 0]

    # ───────── bulk restock ─────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        Accepts dataframe with columns **itemid, need**.
        Returns list[dict] summarising moves.
        """
        if df_need.empty:
            return []

        # 1️⃣  Build inventory pulls
        pulls = []      # list of dicts: itemid, exp, cpu, take, loc
        for _, row in df_need.iterrows():
            item_id = int(row.itemid)
            need    = int(row.need)
            layers  = self.fetch_data(
                """
                SELECT expirationdate, quantity, cost_per_unit
                FROM   inventory
                WHERE  itemid=%s AND quantity>0
                ORDER  BY expirationdate, cost_per_unit
                """,
                (item_id,),
            )
            for lyr in layers.itertuples():
                take = min(need, int(lyr.quantity))
                pulls.append(
                    dict(
                        itemid=item_id,
                        exp=lyr.expirationdate,
                        cpu=float(lyr.cost_per_unit),
                        take=take,
                        loc=self.loc_for_item(item_id),
                    )
                )
                need -= take
                if need == 0:
                    break

        if not pulls:
            return []

        # 2️⃣  One BEGIN/COMMIT block
        with self.conn.cursor() as cur:
            # 2‑a  decrement inventory
            execute_values(
                cur,
                """
                UPDATE inventory AS inv
                   SET quantity = inv.quantity - v.take
                FROM (VALUES %s) AS v(itemid, exp, cpu, take)
                WHERE inv.itemid = v.itemid
                  AND inv.expirationdate = v.exp
                  AND inv.cost_per_unit  = v.cpu
                """,
                [(p["itemid"], p["exp"], p["cpu"], p["take"]) for p in pulls],
            )

            # 2‑b  upsert into shelf
            execute_values(
                cur,
                """
                INSERT INTO shelf
                      (itemid, expirationdate, quantity,
                       cost_per_unit, locid)
                VALUES %s
                ON CONFLICT (itemid, expirationdate, cost_per_unit, locid)
                DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                              lastupdated = CURRENT_TIMESTAMP
                """,
                [(p["itemid"], p["exp"], p["take"], p["cpu"], p["loc"])
                 for p in pulls],
            )

        self.conn.commit()

        # 3️⃣  compact log for UI
        return [
            dict(itemid=p["itemid"], added=p["take"],
                 exp=p["exp"], locid=p["loc"])
            for p in pulls
        ]
