from __future__ import annotations
from functools import lru_cache
from typing import Any

import pandas as pd

from db_handler import DatabaseManager


class SellingAreaHandler(DatabaseManager):
    # ───────────── small helpers ─────────────
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

    # ───────────── public API ─────────────
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

    # ───────────── data access wrappers ─────────────
    def fetch_data(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        with self.conn:
            return pd.read_sql_query(sql, self.conn, params=params)

    def execute_command(self, sql: str, params: tuple = ()) -> None:
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)

    # ───────────── shortage resolver ─────────────
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
                    "DELETE FROM shelf_shortage WHERE shortageid = %s",
                    (r.shortageid,),
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

    # ───────────── KPI helpers (used by POS.py) ─────────────
    def get_all_items(self) -> pd.DataFrame:
        """
        Returns one row per catalog item with shelf KPI settings.
        Columns expected by POS.shelf_cycle():
            itemid | itemname | shelfthreshold | shelfaverage
        """
        return self.fetch_data(
            """
            SELECT itemid,
                   itemnameenglish AS itemname,
                   COALESCE(shelfthreshold, 0)::int AS shelfthreshold,
                   COALESCE(shelfaverage,
                            shelfthreshold,
                            0)::int          AS shelfaverage
              FROM item
            """
        )

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        """
        Aggregates current shelf stock.
        Returns columns: itemid | totalquantity
        """
        return self.fetch_data(
            """
            SELECT itemid,
                   COALESCE(SUM(quantity), 0)::int AS totalquantity
              FROM shelf
             GROUP BY itemid
            """
        )

    # ───────────── optional helper (kept from original) ─────────────
    def get_items_below_shelfthreshold(self) -> pd.DataFrame:
        """
        Returns only items whose current shelf quantity is below threshold.
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
