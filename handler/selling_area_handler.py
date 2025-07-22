"""
SellingAreaHandler – fast bulk moves (final fix)
"""

from __future__ import annotations
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_LOCID = "UNASSIGNED"


class SellingAreaHandler(DatabaseManager):

    # ───────── KPI snapshot ─────────
    def shelf_kpis(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT i.itemid,
                   i.itemnameenglish,
                   COALESCE(SUM(s.quantity),0)::int   AS totalqty,
                   i.shelfthreshold,
                   i.shelfaverage
            FROM   item i
            LEFT  JOIN shelf s USING (itemid)
            GROUP BY i.itemid, i.itemnameenglish,
                     i.shelfthreshold, i.shelfaverage
            """
        )

    # ───────── helper maps ─────────
    def slot_map(self) -> dict[int, str]:
        rows = self.fetch_data("SELECT itemid, locid FROM item_slot")
        return dict(zip(rows.itemid, rows.locid))

    # ───────── bulk refill ─────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need → itemid • need   (already filtered for need>0)
        """
        if df_need.empty:
            return []

        want    = dict(df_need.set_index("itemid")["need"])
        itemids = tuple(want.keys())

        inv = self.fetch_data(
            """
            SELECT itemid, expirationdate, quantity, cost_per_unit
            FROM   inventory
            WHERE  itemid IN %s AND quantity > 0
            ORDER  BY itemid, expirationdate, cost_per_unit
            """,
            (itemids,),
        )
        if inv.empty:
            return []

        loc_map = self.slot_map()

        layers: list[tuple] = []          # (it, exp, take, cpu, loc)
        for row in inv.itertuples():
            need = want.get(row.itemid, 0)
            if need <= 0:
                continue
            take = min(need, int(row.quantity))
            want[row.itemid] -= take
            layers.append(
                (row.itemid,
                 row.expirationdate,
                 take,
                 float(row.cost_per_unit),
                 loc_map.get(row.itemid, DEFAULT_LOCID))
            )
        if not layers:
            return []

        # 🚀 one TX, no nesting
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                UPDATE inventory AS inv
                SET    quantity = inv.quantity - v.take
                FROM  (VALUES %s) AS v(itemid,exp,cpu,take)
                WHERE inv.itemid=v.itemid
                  AND inv.expirationdate=v.exp
                  AND inv.cost_per_unit=v.cpu
                """,
                [(l[0], l[1], l[3], l[2]) for l in layers],
            )
            execute_values(
                cur,
                """
                INSERT INTO shelf
                      (itemid,expirationdate,quantity,
                       cost_per_unit,locid)
                VALUES %s
                ON CONFLICT (itemid,expirationdate,cost_per_unit,locid)
                DO UPDATE SET quantity   = shelf.quantity + EXCLUDED.quantity,
                              lastupdated= CURRENT_TIMESTAMP
                """,
                layers,
            )
        self.conn.commit()

        return [
            dict(itemid=l[0], added=l[2], locid=l[4], exp=l[1])
            for l in layers
        ]
