"""
SellingAreaHandler – bulk mode (no‑delete version)
───────────────────────────────────────────────────
• Moves stock from **inventory** → **shelf** in one atomic transaction.
• Keeps (itemid, expiry, cpu, locid) unique in *shelf*.
• Logs every movement in *shelfentries*.
• Never deletes rows – zero‑qty layers are kept for traceability.
"""

from __future__ import annotations
from collections import defaultdict
from typing import List, Dict

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

DEFAULT_LOCID = "UNASSIGNED"          # fallback when item_slot is missing
_CREATED_BY   = "AUTO‑SHELF"


class SellingAreaHandler(DatabaseManager):
    # ───────────────────── simple look‑ups ─────────────────────
    def shelf_kpis(self) -> pd.DataFrame:
        """Current shelf quantity and thresholds/averages for every SKU."""
        return self.fetch_data(
            """
            SELECT i.itemid,
                   COALESCE(SUM(s.quantity),0)::int AS totalqty,
                   i.shelfthreshold,
                   i.shelfaverage
              FROM item  i
         LEFT JOIN shelf s ON s.itemid = i.itemid
          GROUP BY i.itemid, i.shelfthreshold, i.shelfaverage
            """
        )

    def unresolved_shortages(self) -> pd.DataFrame:
        """Outstanding shortage qty per SKU (resolved = FALSE)."""
        return self.fetch_data(
            """
            SELECT itemid, SUM(shortage_qty)::int AS shortage
              FROM shelf_shortage
             WHERE resolved = FALSE
          GROUP BY itemid
            """
        )

    def loc_for_item(self, itemid: int) -> str:
        res = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1", (itemid,)
        )
        return DEFAULT_LOCID if res.empty else res.iloc[0, 0]

    # ───────────────────── shortage resolver ─────────────────────
    def resolve_shortages(self, *, itemid: int, qty_filled: int, user: str) -> None:
        """
        Mark up to `qty_filled` units of shortage as resolved for this item.
        Each row is processed FIFO on `logged_at`.
        """
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
        remaining = qty_filled
        for r in rows.itertuples():
            if remaining == 0:
                break
            take = min(remaining, int(r.shortage_qty))
            if take == r.shortage_qty:
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                       SET resolved     = TRUE,
                           resolved_qty = shortage_qty,
                           resolved_by  = %s,
                           resolved_at  = CURRENT_TIMESTAMP
                     WHERE shortageid   = %s
                    """,
                    (user, r.shortageid),
                )
            else:
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                       SET shortage_qty = shortage_qty - %s,
                           resolved_qty = COALESCE(resolved_qty,0)+%s,
                           resolved_by  = %s,
                           resolved_at  = CURRENT_TIMESTAMP
                     WHERE shortageid   = %s
                    """,
                    (take, take, user, r.shortageid),
                )
            remaining -= take

    # ───────────────────── bulk refill ─────────────────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> List[Dict]:
        """
        Parameters
        ----------
        df_need : DataFrame with columns  itemid • need  (positive ints)

        Returns
        -------
        list[dict]  UI log  {itemid, added, locid, exp}
        """
        if df_need.empty:
            return []

        # 1️⃣ build work‑list of inventory layers to pull from
        layers: list[dict] = []
        for _, row in df_need.iterrows():
            iid, need = int(row.itemid), int(row.need)
            if need <= 0:
                continue

            inv = self.fetch_data(
                """
                SELECT expirationdate, quantity, cost_per_unit
                  FROM inventory
                 WHERE itemid   = %s
                   AND quantity > 0
              ORDER BY expirationdate, cost_per_unit
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

        # 2️⃣ merge duplicate keys to avoid UPSERT collisions
        merged: dict[tuple, int] = defaultdict(int)
        for l in layers:
            key = (l["itemid"], l["exp"], l["cpu"], l["loc"])
            merged[key] += l["take"]

        compact_layers = [
            dict(itemid=k[0], exp=k[1], cpu=k[2], loc=k[3], take=v)
            for k, v in merged.items()
        ]

        # 3️⃣ atomic transaction: inventory −qty, shelf +qty, shelfentries log
        with self.conn:
            with self.conn.cursor() as cur:
                # 3a. decrement inventory (zero‑qty rows kept)
                execute_values(
                    cur,
                    """
                    UPDATE inventory AS inv
                       SET quantity = inv.quantity - v.take
                      FROM (VALUES %s) AS v(itemid,exp,cpu,take)
                     WHERE inv.itemid         = v.itemid
                       AND inv.expirationdate = v.exp
                       AND inv.cost_per_unit  = v.cpu
                    """,
                    [(l["itemid"], l["exp"], l["cpu"], l["take"])
                     for l in compact_layers],
                )

                # 3b. shelf UPSERT (+qty)
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
                     for l in compact_layers],
                )

                # 3c. movement log
                execute_values(
                    cur,
                    """
                    INSERT INTO shelfentries
                          (itemid, expirationdate, quantity, createdby, locid)
                    VALUES %s
                    """,
                    [(l["itemid"], l["exp"], l["take"], _CREATED_BY, l["loc"])
                     for l in compact_layers],
                )

        # 4️⃣ return compact UI log
        return [
            dict(itemid=l["itemid"], added=l["take"],
                 locid=l["loc"], exp=l["exp"])
            for l in compact_layers
        ]
