"""
SellingAreaHandler – bulk mode
──────────────────────────────
• Moves stock from **inventory** → **shelf** in one transaction.
• Keeps (itemid, expiry, cpu, locid) unique in *shelf*.
• Logs every movement in *shelfentries*.
"""

from __future__ import annotations
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_LOCID = "UNASSIGNED"          # fallback when item_slot is missing


class SellingAreaHandler(DatabaseManager):
    # ───────────────────── KPI snapshot ─────────────────────
    def shelf_kpis(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT i.itemid,
                   i.itemnameenglish,
                   COALESCE(SUM(s.quantity),0)::int AS totalqty,
                   i.shelfthreshold,
                   i.shelfaverage
            FROM   item i
            LEFT  JOIN shelf s ON s.itemid = i.itemid
            GROUP  BY i.itemid, i.itemnameenglish,
                      i.shelfthreshold, i.shelfaverage
            """
        )

    # ───────────────────── loc‑lookup ─────────────────────
    def loc_for_item(self, itemid: int) -> str:
        res = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return DEFAULT_LOCID if res.empty else res.iloc[0, 0]

    # ───────────────────── bulk refill ─────────────────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need columns →  itemid • need
        Returns UI‑log list[dict].
        """
        if df_need.empty:
            return []

        # 1️⃣ build work‑list of inv layers to pull
        layers: list[dict] = []
        for _, row in df_need.iterrows():
            iid, need = int(row.itemid), int(row.need)
            if need <= 0:
                continue
            inv = self.fetch_data(
                """
                SELECT expirationdate, quantity, cost_per_unit
                FROM   inventory
                WHERE  itemid=%s AND quantity>0
                ORDER  BY expirationdate, cost_per_unit
                """,
                (iid,),
            )
            for lyr in inv.itertuples():
                take = min(need, int(lyr.quantity))
                layers.append(
                    dict(itemid=iid,
                         exp=lyr.expirationdate,
                         take=take,
                         cpu=float(lyr.cost_per_unit),
                         loc=self.loc_for_item(iid))
                )
                need -= take
                if need == 0:
                    break

        if not layers:
            return []

        # 2️⃣ one atomic tx: update inventory, upsert shelf, insert shelfentries
        with self.conn:
            with self.conn.cursor() as cur:
                # inventory −qty
                execute_values(
                    cur,
                    """
                    UPDATE inventory AS inv
                    SET    quantity = inv.quantity - v.take
                    FROM  (VALUES %s) AS v(itemid,exp,cpu,take)
                    WHERE inv.itemid         = v.itemid
                      AND inv.expirationdate = v.exp
                      AND inv.cost_per_unit  = v.cpu
                    """,
                    [(l["itemid"], l["exp"], l["cpu"], l["take"]) for l in layers],
                )

                # shelf +qty (upsert)
                execute_values(
                    cur,
                    """
                    INSERT INTO shelf
                          (itemid, expirationdate, quantity,
                           cost_per_unit, locid)
                    VALUES %s
                    ON CONFLICT (itemid,expirationdate,cost_per_unit,locid)
                    DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                                  lastupdated = CURRENT_TIMESTAMP
                    """,
                    [(l["itemid"], l["exp"], l["take"], l["cpu"], l["loc"])
                     for l in layers],
                )

                # shelfentries log
                execute_values(
                    cur,
                    """
                    INSERT INTO shelfentries
                          (itemid, expirationdate, quantity, createdby)
                    VALUES %s
                    """,
                    [(l["itemid"], l["exp"], l["take"], "AUTO‑SHELF")
                     for l in layers],
                )

        # 3️⃣ compact return log
        return [
            dict(itemid=l["itemid"], added=l["take"],
                 locid=l["loc"], exp=l["exp"])
            for l in layers
        ]
