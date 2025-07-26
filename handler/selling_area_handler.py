# handler/selling_area_handler.py
from __future__ import annotations

from functools import lru_cache
from typing import Sequence

import pandas as pd
import warnings
from psycopg2 import extensions as _psx

from db_handler import DatabaseManager


class SellingAreaHandler(DatabaseManager):
    # ────────────────────── small helpers ──────────────────────
    @lru_cache(maxsize=10_000)
    def _lookup_locid(self, itemid: int) -> str | None:
        df = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1", (itemid,)
        )
        return None if df.empty else df.iloc[0, 0]

    # ────────────────── PUBLIC helpers (used by POS.py) ──────────────────
    def get_all_items(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT itemid,
                   itemnameenglish AS itemname,
                   COALESCE(shelfthreshold, 0)::int          AS shelfthreshold,
                   COALESCE(shelfaverage, shelfthreshold, 0) AS shelfaverage
              FROM item
            """
        )

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT itemid,
                   COALESCE(SUM(quantity), 0)::int AS totalquantity
              FROM shelf
          GROUP BY itemid
            """
        )

    # ─────────────────── internal low‑level helpers ────────────────────
    def _decrement_inventory_layer(
        self,
        *,
        cur,
        batchid: int,
        quantity: int,
    ) -> None:
        """
        Subtract `quantity` from ONE specific inventory row,
        identified by its primary key `batchid`.
        """
        cur.execute(
            """
            UPDATE inventory
               SET quantity = quantity - %s
             WHERE batchid  = %s
               AND quantity >= %s
            """,
            (quantity, batchid, quantity),
        )
        if cur.rowcount == 0:
            raise ValueError("Insufficient inventory layer")

        # Auto‑cleanup – delete empty rows so the table stays lean
        cur.execute(
            "DELETE FROM inventory WHERE batchid = %s AND quantity = 0",
            (batchid,),
        )

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

    # ───────────────────── bulk mover (single item) ─────────────────────
    def move_layers_to_shelf(
        self,
        *,
        itemid: int,
        layers: Sequence[tuple],  # (batchid, expirationdate, quantity, cost_per_unit)
        created_by: str,
        locid: str | None = None,
    ) -> None:
        """
        Atomically move one‑or‑more FIFO layers from warehouse
        inventory to shelf for **one** item.

        Example `layers`:
            [
              (123, date(2025,10,31),  6,  4.10),
              (124, date(2025,11,15), 12,  4.25),
            ]
        """
        if not layers:
            return

        if locid is None:
            locid = self._lookup_locid(itemid)
            if locid is None:
                raise ValueError(f"No slot mapping for item {itemid}")

        self._ensure_live_conn()
        with self.conn:           # one outer transaction
            with self.conn.cursor() as cur:
                for bid, exp, qty, cpu in layers:
                    self._decrement_inventory_layer(
                        cur=cur,
                        batchid=bid,
                        quantity=qty,
                    )
                    self._upsert_shelf_layer(
                        cur=cur,
                        itemid=itemid,
                        expirationdate=exp,
                        quantity=qty,
                        cost_per_unit=cpu,
                        locid=locid,
                        created_by=created_by,
                    )

    # ────────────────── generic DB wrappers (warning‑free) ─────────────────
    def fetch_data(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        self._ensure_live_conn()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                message="pandas only supports SQLAlchemy connectable",
            )
            return pd.read_sql_query(sql, self.conn, params=params)

    def execute_command(self, sql: str, params: tuple = ()) -> None:
        self._ensure_live_conn()
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
        finally:
            cur.close()
            if (
                self.conn.get_transaction_status()
                == _psx.TRANSACTION_STATUS_IDLE
            ):
                self.conn.commit()

    # ───────────────── shortage reconciliation ──────────────────
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

    # ───────────────── convenience query ─────────────────────────
    def get_items_below_shelfthreshold(self) -> pd.DataFrame:
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
