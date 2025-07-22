"""
SellingAreaHandler  (fast bulk mode)
────────────────────────────────────
• Pulls needed layers from *inventory* → *shelf* **in bulk**  
  (one SQL transaction per cycle — no per‑row commits).  
• 1 row per (itemid, expiry, cpu, locid) is kept unique in *shelf*.
"""

from __future__ import annotations
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

# fallback location when an item is missing in item_slot
DEFAULT_LOCID = "UNASSIGNED"


class SellingAreaHandler(DatabaseManager):
    # ───────────────────────── current KPI ─────────────────────────
    def shelf_kpis(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT i.itemid,
                   i.itemnameenglish,
                   COALESCE(SUM(s.quantity),0)::int AS totalqty,
                   i.shelfthreshold,
                   i.shelfaverage
            FROM   item i
            LEFT JOIN shelf s ON s.itemid = i.itemid
            GROUP  BY i.itemid, i.itemnameenglish,
                      i.shelfthreshold, i.shelfaverage
            """
        )

    # ─────────────────── helpers (mappings) ────────────────────
    def loc_for_item(self, itemid: int) -> str:
        res = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1",
            (itemid,),
        )
        return DEFAULT_LOCID if res.empty else res.iloc[0, 0]

    # ─────────────────── BULK refill API ────────────────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need cols → itemid • need  
        Returns list[dict] for UI logging.
        """
        if df_need.empty:
            return []

        # 1️⃣  Build list of inventory layers to pull
        inv_layers = []
        for _, r in df_need.iterrows():
            it, need = int(r.itemid), int(r.need)
            layers = self.fetch_data(
                """
                SELECT expirationdate, quantity, cost_per_unit
                FROM   inventory
                WHERE  itemid=%s AND quantity>0
                ORDER  BY expirationdate, cost_per_unit
                """,
                (it,),
            )
            for lyr in layers.itertuples():
                take = min(need, int(lyr.quantity))
                inv_layers.append(
                    dict(itemid=it,
                         exp=lyr.expirationdate,
                         take=take,
                         cpu=float(lyr.cost_per_unit),
                         loc=self.loc_for_item(it))
                )
                need -= take
                if need == 0:
                    break

        if not inv_layers:
            return []

        # 2️⃣  Single transaction: UPDATE inventory −qty & UPSERT to shelf
        with self.conn:
            with self.conn.cursor() as cur:
                # batch inventory decrements via VALUES‑list UPDATE
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
                    [(l["itemid"], l["exp"], l["cpu"], l["take"])
                     for l in inv_layers],
                )

                # batch UPSERT into shelf
                execute_values(
                    cur,
                    """
                    INSERT INTO shelf
                          (itemid,expirationdate,quantity,
                           cost_per_unit,locid)
                    VALUES %s
                    ON CONFLICT (itemid,expirationdate,cost_per_unit,locid)
                    DO UPDATE SET quantity = shelf.quantity + EXCLUDED.quantity,
                                  lastupdated = CURRENT_TIMESTAMP
                    """,
                    [(l["itemid"], l["exp"], l["take"],
                      l["cpu"], l["loc"])
                     for l in inv_layers],
                )

        # 3️⃣  return compact log
        return [
            dict(itemid=l["itemid"], added=l["take"],
                 locid=l["loc"], exp=l["exp"])
            for l in inv_layers
        ]
