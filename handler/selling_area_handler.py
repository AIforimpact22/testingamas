# handler/selling_area_handler.py
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable, List, Tuple
from collections import defaultdict

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

# fallback location when an item is missing in item_slot
DEFAULT_LOCID = "UNASSIGNED"


class SellingAreaHandler(DatabaseManager):
    # ─────────────────────────── SMALL HELPERS ────────────────────────────
    @lru_cache(maxsize=10_000)  # slot mapping cache
    def _lookup_locid(self, itemid: int) -> str | None:
        df = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1", (itemid,)
        )
        return None if df.empty else df.iloc[0, 0]

    def _upsert_shelf_layer(
        self,
        *,
        cur,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        locid: str,
        created_by: str,
    ) -> None:
        cur.execute(
            """
            INSERT INTO shelf
                  (itemid, expirationdate, quantity, cost_per_unit, locid)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (itemid, expirationdate, locid, cost_per_unit)
            DO UPDATE
               SET quantity    = shelf.quantity + EXCLUDED.quantity,
                   lastupdated = CURRENT_TIMESTAMP
            """,
            (itemid, expirationdate, quantity, cost_per_unit, locid),
        )
        cur.execute(
            """
            INSERT INTO shelfentries
                  (itemid, expirationdate, quantity, createdby, locid)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (itemid, expirationdate, quantity, created_by, locid),
        )

    # ────────────────────────── LEGACY METHODS ────────────────────────────
    # (Kept so existing single‐item flows continue to work)

    def transfer_from_inventory(
        self,
        *,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        created_by: str,
        locid: str | None = None,
    ) -> None:
        if locid is None:
            locid = self._lookup_locid(itemid)
            if locid is None:
                raise ValueError(f"No slot mapping for item {itemid}")

        self._ensure_live_conn()
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE inventory
                       SET quantity = quantity - %s
                     WHERE itemid = %s
                       AND expirationdate = %s
                       AND cost_per_unit  = %s
                       AND quantity      >= %s
                    """,
                    (quantity, itemid, expirationdate, cost_per_unit, quantity),
                )
                if cur.rowcount == 0:
                    raise ValueError("Insufficient inventory layer")

                self._upsert_shelf_layer(
                    cur=cur,
                    itemid=itemid,
                    expirationdate=expirationdate,
                    quantity=quantity,
                    cost_per_unit=cost_per_unit,
                    locid=locid,
                    created_by=created_by,
                )

    def fetch_data(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        with self.conn:
            return pd.read_sql_query(sql, self.conn, params=params)

    def execute_command(self, sql: str, params: tuple = ()) -> None:
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)

    def resolve_shortages(self, *, itemid: int, qty_need: int, user: str) -> int:
        rows = self.fetch_data(
            """
            SELECT shortageid, shortage_qty
              FROM shelf_shortage
             WHERE itemid   = %s
               AND resolved = FALSE
          ORDER BY logged_at
            """,
            (itemid,),
        )
        remaining = qty_need
        for r in rows.itertuples():
            if remaining == 0:
                break
            take = min(remaining, int(r.shortage_qty))
            if take == r.shortage_qty:
                self.execute_command(
                    "DELETE FROM shelf_shortage WHERE shortageid = %s", (r.shortageid,)
                )
            else:
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                       SET shortage_qty = shortage_qty - %s,
                           resolved_qty  = COALESCE(resolved_qty,0)+%s,
                           resolved_by   = %s,
                           resolved_at   = CURRENT_TIMESTAMP
                     WHERE shortageid = %s
                    """,
                    (take, take, user, r.shortageid),
                )
            remaining -= take
        return remaining

    def get_items_below_shelfthreshold(self) -> pd.DataFrame:
        """
        Returns only items whose current shelf quantity is below their threshold.
        """
        df = self.fetch_data(
            """
            SELECT i.itemid,
                   i.itemnameenglish AS itemname,
                   COALESCE(i.shelfthreshold,0) AS shelfthreshold,
                   COALESCE(i.shelfaverage, i.shelfthreshold, 0) AS shelfaverage,
                   COALESCE(SUM(s.quantity),0)::int AS totalquantity
              FROM item i
         LEFT JOIN shelf s ON s.itemid = i.itemid
          GROUP BY i.itemid, i.itemnameenglish, i.shelfthreshold, i.shelfaverage
            HAVING COALESCE(SUM(s.quantity),0) < COALESCE(i.shelfthreshold,0)
            ORDER BY i.itemnameenglish
            """
        )
        if not df.empty:
            df[["shelfthreshold", "shelfaverage", "totalquantity"]] = df[
                ["shelfthreshold", "shelfaverage", "totalquantity"]
            ].astype(int)
        return df

    # ────────────────────── BULK‑MODE HELPERS ───────────────────────
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
    ) -> List[Tuple]:
        """
        Decide how much to pull from each layer and aggregate duplicates.
        Returns UNIQUE tuples:
        (itemid, exp, cpu, take_qty, locid)
        """
        need_left   = dict(zip(need_df.itemid, need_df.need))
        loc_by_item = dict(zip(need_df.itemid, need_df.locid))

        # first pass may contain duplicates
        raw: List[Tuple] = []
        for lyr in layers_df.itertuples(index=False):
            needed = need_left.get(lyr.itemid, 0)
            if needed <= 0:
                continue
            take = min(needed, lyr.quantity)
            raw.append(
                (lyr.itemid, lyr.expirationdate, lyr.cost_per_unit,
                 take, loc_by_item[lyr.itemid])
            )
            need_left[lyr.itemid] -= take

        # aggregate duplicates to avoid ON‑CONFLICT collisions inside VALUES
        agg: defaultdict[Tuple[int, Any, float, str], int] = defaultdict(int)
        for itemid, exp, cpu, qty, loc in raw:
            agg[(itemid, exp, cpu, loc)] += qty

        # back to list[tuple] in final order
        picks = [(k[0], k[1], k[2], q, k[3]) for k, q in agg.items()]
        return picks

    # ───────────────────── ONE‑SHOT REFILL (BULK) ─────────────────────
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

        self._ensure_live_conn()          # keep-alive
        cur = self.conn.cursor()
        try:
            # 1️⃣  decrement inventory
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

            # 2️⃣  upsert into shelf
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

            # 3️⃣  audit into shelfentries
            execute_values(
                cur,
                """
                INSERT INTO shelfentries
                      (itemid, expirationdate, quantity, createdby, locid)
                VALUES %s
                """,
                [(p[0], p[1], p[3], user, p[4]) for p in picks],
            )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

        return len(picks)
