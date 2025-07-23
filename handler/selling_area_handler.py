# handler/selling_area_handler.py  – full import block
from __future__ import annotations
from functools import lru_cache
from typing import Any
import pandas as pd

from psycopg2.extras import execute_values      # bulk helpers
from db_handler import DatabaseManager          # ←← this line was missing!



# ─────────────────────────────── NEW  IMPORT ───────────────────────────────
from psycopg2.extras import execute_values               # ⬅️ add to imports

class SellingAreaHandler(DatabaseManager):
    # … keep everything you already have …

    # ──────────────────── BULK‑MODE HELPERS  ────────────────────
    def items_needing_refill(self) -> pd.DataFrame:
        """
        itemid • need • locid for every SKU below threshold/average.
        """
        return self.fetch_data(
            """
            WITH kpi AS (
                SELECT i.itemid,
                       COALESCE(SUM(s.quantity),0)::int AS on_shelf,
                       COALESCE(i.shelfthreshold,0)     AS shelfthreshold,
                       COALESCE(i.shelfaverage,
                                i.shelfthreshold,0)     AS shelfaverage
                FROM   item i
                LEFT   JOIN shelf s USING (itemid)
                GROUP  BY i.itemid, i.shelfthreshold, i.shelfaverage
            )
            SELECT k.itemid,
                   GREATEST(k.shelfaverage   - k.on_shelf,
                            k.shelfthreshold - k.on_shelf) AS need,
                   COALESCE(sl.locid,'UNASSIGNED')         AS locid
            FROM   kpi k
            LEFT   JOIN item_slot sl USING (itemid)
            WHERE  GREATEST(k.shelfaverage   - k.on_shelf,
                            k.shelfthreshold - k.on_shelf) > 0
            """
        )

    def layers_for_items(self, item_ids: list[int]) -> pd.DataFrame:
        """
        All positive‑stock layers for the given items, sorted FIFO.
        """
        return self.fetch_data(
            """
            SELECT itemid, expirationdate, cost_per_unit, quantity
              FROM inventory
             WHERE itemid = ANY(%s) AND quantity > 0
          ORDER BY itemid, expirationdate, cost_per_unit
            """,
            (item_ids,),
        )

    def _plan_picks(
        self, need_df: pd.DataFrame, layers_df: pd.DataFrame
    ) -> list[tuple]:
        """
        Decide how much to pull from each layer.
        Returns [(itemid, exp, cpu, take_qty, locid), …]
        ready for execute_values().
        """
        need_left   = dict(zip(need_df.itemid, need_df.need))
        loc_by_item = dict(zip(need_df.itemid, need_df.locid))
        picks: list[tuple] = []

        for lyr in layers_df.itertuples(index=False):
            needed = need_left.get(lyr.itemid, 0)
            if needed <= 0:
                continue
            take = min(needed, lyr.quantity)
            picks.append(
                (lyr.itemid, lyr.expirationdate, lyr.cost_per_unit,
                 take, loc_by_item[lyr.itemid])
            )
            need_left[lyr.itemid] -= take

        return picks

    # ───────────────────── ONE‑SHOT REFILL  ─────────────────────
    def bulk_refill(self, *, user: str = "AUTO‑SHELF") -> int:
        """
        Refills *all* items below threshold in one transaction.
        Returns number of inventory rows moved to shelf.
        """
        need_df = self.items_needing_refill()
        if need_df.empty:
            return 0

        layers_df = self.layers_for_items(need_df.itemid.tolist())
        picks     = self._plan_picks(need_df, layers_df)
        if not picks:
            return 0

        with self.conn:
            with self.conn.cursor() as cur:
                # 1️⃣ decrement inventory in bulk
                execute_values(
                    cur,
                    """
                    UPDATE inventory AS inv
                       SET quantity = inv.quantity - v.take_qty
                      FROM (VALUES %s)
                           AS v(itemid,exp,cpu,take_qty,locid)
                     WHERE inv.itemid         = v.itemid
                       AND inv.expirationdate = v.exp
                       AND inv.cost_per_unit  = v.cpu
                    """,
                    picks,
                )

                # 2️⃣ upsert to shelf
                execute_values(
                    cur,
                    """
                    INSERT INTO shelf
                          (itemid, expirationdate, cost_per_unit,
                           quantity, locid)
                    VALUES %s
                    ON CONFLICT (itemid, expirationdate,
                                 cost_per_unit, locid)
                    DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                                  lastupdated = CURRENT_TIMESTAMP
                    """,
                    [(p[0], p[1], p[2], p[3], p[4]) for p in picks],
                )

                # 3️⃣ audit into shelfentries
                execute_values(
                    cur,
                    """
                    INSERT INTO shelfentries
                          (itemid, expirationdate, quantity, createdby, locid)
                    VALUES %s
                    """,
                    [(p[0], p[1], p[3], user, p[4]) for p in picks],
                )

        return len(picks)
