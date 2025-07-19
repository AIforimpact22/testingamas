# handler/shelf_handler.py
"""
ShelfHandler
============
Centralised DB helper for every operation that touches the *Selling Area*
(`shelf`, `shelfentries`, `shelf_shortage`, …).

It is totally UI‑agnostic: no Streamlit imports, no caching — just
database work.  Import it anywhere via

    from handler.shelf_handler import ShelfHandler
"""

from __future__ import annotations

import pandas as pd
from db_handler import DatabaseManager

__all__ = ["ShelfHandler"]


class ShelfHandler(DatabaseManager):
    """Inventory → Shelf movement, queries, and shortage resolution."""

    # ───────────────────────── shelf queries ─────────────────────────
    def get_shelf_items(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  s.shelfid,
                    s.itemid,
                    i.itemnameenglish AS itemname,
                    s.quantity,
                    s.expirationdate,
                    s.cost_per_unit,
                    s.lastupdated
            FROM    shelf s
            JOIN    item  i ON s.itemid = i.itemid
            ORDER   BY i.itemnameenglish, s.expirationdate;
            """
        )

    # ───────────────────── add / update shelf (single call) ──────────────────
    def add_to_shelf(
        self,
        itemid: int,
        expirationdate,
        quantity: int,
        created_by: str,
        cost_per_unit: float,
        *,
        cur=None,
    ) -> None:
        """
        Upsert (item + expiry + cost) into shelf **and** write a shelfentries
        movement log.  If *cur* is provided, we reuse that open cursor so the
        caller can wrap multiple steps in one outer transaction.
        """
        own_cursor = cur is None
        if own_cursor:
            self._ensure_live_conn()
            cur = self.conn.cursor()

        cur.execute(
            """
            INSERT INTO shelf (itemid, expirationdate, quantity, cost_per_unit)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (itemid, expirationdate, cost_per_unit)
            DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                          lastupdated = CURRENT_TIMESTAMP;
            """,
            (int(itemid), expirationdate, int(quantity), float(cost_per_unit)),
        )

        cur.execute(
            """
            INSERT INTO shelfentries (itemid, expirationdate, quantity, createdby)
            VALUES (%s, %s, %s, %s);
            """,
            (int(itemid), expirationdate, int(quantity), created_by),
        )

        if own_cursor:
            self.conn.commit()
            cur.close()

    # ───────────────────── inventory look‑ups ───────────────────────
    def get_inventory_items(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  inv.itemid,
                    i.itemnameenglish AS itemname,
                    inv.quantity,
                    inv.expirationdate,
                    inv.storagelocation,
                    inv.cost_per_unit
            FROM    inventory inv
            JOIN    item       i ON inv.itemid = i.itemid
            WHERE   inv.quantity > 0
            ORDER   BY i.itemnameenglish, inv.expirationdate;
            """
        )

    # ───────────── fast transfer: Inventory → Shelf (one commit) ────────────
    def transfer_from_inventory(
        self,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        created_by: str,
    ) -> None:
        """
        Move a *single* cost layer from **Inventory** to **Shelf** atomically.
        Fails (and rolls back) if inventory doesn’t have enough of that layer.
        """
        self._ensure_live_conn()
        with self.conn:                       # one BEGIN/COMMIT block
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE inventory
                    SET    quantity = quantity - %s
                    WHERE  itemid         = %s
                      AND  expirationdate = %s
                      AND  cost_per_unit  = %s
                      AND  quantity >= %s;
                    """,
                    (int(quantity), int(itemid), expirationdate,
                     float(cost_per_unit), int(quantity)),
                )
                if cur.rowcount == 0:
                    raise ValueError("Not enough stock in that inventory layer.")

                # Upsert into shelf and log entry using the SAME cursor
                self.add_to_shelf(
                    itemid, expirationdate, quantity,
                    created_by, cost_per_unit, cur=cur
                )

    # ───────────────────── alerts & look‑ups ────────────────────────
    def get_low_shelf_stock(self, threshold: int = 10) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  s.itemid,
                    i.itemnameenglish AS itemname,
                    s.quantity,
                    s.expirationdate
            FROM    shelf s
            JOIN    item  i ON s.itemid = i.itemid
            WHERE   s.quantity <= %s
            ORDER   BY s.quantity ASC;
            """,
            (threshold,),
        )

    def get_inventory_by_barcode(self, barcode: str) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  inv.itemid,
                    i.itemnameenglish AS itemname,
                    inv.quantity,
                    inv.expirationdate,
                    inv.cost_per_unit
            FROM    inventory inv
            JOIN    item       i ON inv.itemid = i.itemid
            WHERE   i.barcode = %s
              AND   inv.quantity > 0
            ORDER   BY inv.expirationdate;
            """,
            (barcode,),
        )

    # -------------- item master helpers -----------------
    def get_all_items(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT  itemid,
                    itemnameenglish AS itemname,
                    shelfthreshold,
                    shelfaverage
            FROM    item
            ORDER   BY itemnameenglish;
            """
        )
        if not df.empty:
            df["shelfthreshold"] = df["shelfthreshold"].astype("Int64")
            df["shelfaverage"]   = df["shelfaverage"].astype("Int64")
        return df

    def update_shelf_settings(self, itemid: int,
                              new_threshold: int | None,
                              new_average: int | None) -> None:
        self.execute_command(
            """
            UPDATE item
            SET    shelfthreshold = %s,
                   shelfaverage   = %s
            WHERE  itemid = %s;
            """,
            (new_threshold, new_average, int(itemid)),
        )

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT  i.itemid,
                    i.itemnameenglish AS itemname,
                    COALESCE(SUM(s.quantity), 0) AS totalquantity,
                    i.shelfthreshold,
                    i.shelfaverage
            FROM    item  i
            LEFT JOIN shelf s ON i.itemid = s.itemid
            GROUP   BY i.itemid, i.itemnameenglish,
                      i.shelfthreshold, i.shelfaverage
            ORDER   BY i.itemnameenglish;
            """
        )
        if not df.empty:
            df["shelfthreshold"] = df["shelfthreshold"].astype("Int64")
            df["shelfaverage"]   = df["shelfaverage"].astype("Int64")
            df["totalquantity"]  = df["totalquantity"].astype(int)
        return df

    # ───────── shortage resolver (transfer‑side) ─────────
    def resolve_shortages(
        self, *, itemid: int, qty_need: int, user: str
    ) -> int:
        """
        Consume open shortages for *itemid* (oldest first) and return the
        quantity still **uncovered** (≥0).
        """
        rows = self.fetch_data(
            """
            SELECT shortageid, shortage_qty
            FROM   shelf_shortage
            WHERE  itemid = %s
              AND  resolved = FALSE
            ORDER  BY logged_at;
            """,
            (itemid,),
        )

        remaining = qty_need
        for r in rows.itertuples():
            if remaining == 0:
                break
            take = min(remaining, int(r.shortage_qty))

            # shrink or resolve the shortage
            self.execute_command(
                """
                UPDATE shelf_shortage
                SET    shortage_qty = shortage_qty - %s,
                       resolved_qty  = COALESCE(resolved_qty, 0) + %s,
                       resolved      = (shortage_qty - %s = 0),
                       resolved_at   = CASE WHEN shortage_qty - %s = 0
                                            THEN CURRENT_TIMESTAMP END,
                       resolved_by   = %s
                WHERE  shortageid = %s;
                """,
                (take, take, take, take, user, r.shortageid),
            )
            remaining -= take

        # tidy‑up zero rows
        self.execute_command(
            "DELETE FROM shelf_shortage WHERE shortage_qty = 0;"
        )
        return remaining
