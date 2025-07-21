"""
SellingAreaHandler
==================
Database helpers for the “Selling Area” (shelves) – moving inventory
layers onto shelves, querying shelf KPIs, and resolving shortages.

Only generic primitives live here; the refill loop itself is implemented
in *pages/selling_area.py*.
"""

from __future__ import annotations

import pandas as pd
from db_handler import DatabaseManager

__all__ = ["SellingAreaHandler"]


class SellingAreaHandler(DatabaseManager):
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

    # ─────── add / update a single shelf layer (with log) ────────
    def _upsert_shelf_layer(
        self,
        *,
        cur,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        created_by: str,
    ) -> None:
        """Low‑level helper – assumes an open cursor/tx."""
        cur.execute(
            """
            INSERT INTO shelf (itemid, expirationdate, quantity, cost_per_unit)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (itemid, expirationdate, cost_per_unit)
            DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                          lastupdated = CURRENT_TIMESTAMP;
            """,
            (itemid, expirationdate, quantity, cost_per_unit),
        )
        cur.execute(
            """
            INSERT INTO shelfentries
                  (itemid, expirationdate, quantity, createdby)
            VALUES (%s,     %s,            %s,      %s);
            """,
            (itemid, expirationdate, quantity, created_by),
        )

    # ─────────── fast transfer: Inventory → Shelf (ONE commit) ────────────
    def transfer_from_inventory(
        self,
        *,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        created_by: str,
    ) -> None:
        """
        Move *quantity* of an exact inventory cost‑layer onto the shelf.
        All in a single transaction.
        """
        self._ensure_live_conn()
        with self.conn:                       # BEGIN/COMMIT once
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE inventory
                    SET    quantity = quantity - %s
                    WHERE  itemid         = %s
                      AND  expirationdate = %s
                      AND  cost_per_unit  = %s
                      AND  quantity      >= %s;
                    """,
                    (quantity, itemid, expirationdate, cost_per_unit, quantity),
                )
                if cur.rowcount == 0:
                    raise ValueError("Not enough stock in that inventory layer.")

                self._upsert_shelf_layer(
                    cur=cur,
                    itemid=itemid,
                    expirationdate=expirationdate,
                    quantity=quantity,
                    cost_per_unit=cost_per_unit,
                    created_by=created_by,
                )

    # ───────────────────── inventory helpers ─────────────────────
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

    # ───────────────────── shelf KPI helpers ─────────────────────
    def get_all_items(self) -> pd.DataFrame:
        """
        Returns item‑level shelf targets (`shelfthreshold`, `shelfaverage`).
        The two columns are Int64‑nullable.
        """
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

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        """
        One row per SKU with its current on‑shelf quantity
        plus its configured threshold/average.
        """
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

    # ───────────────────── shortage resolution ─────────────────────
    def resolve_shortages(
        self, *, itemid: int, qty_need: int, user: str
    ) -> int:
        """
        Consume open shortages for *itemid* (oldest first).
        Returns the quantity still **uncovered** (≥ 0).  Fully resolved rows
        are deleted; partially resolved rows are updated.
        """
        rows = self.fetch_data(
            """
            SELECT shortageid, shortage_qty
            FROM   shelf_shortage
            WHERE  itemid   = %s
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

            if take == r.shortage_qty:
                # full – delete row
                self.execute_command(
                    "DELETE FROM shelf_shortage WHERE shortageid = %s",
                    (r.shortageid,),
                )
            else:
                # shrink row
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                    SET    shortage_qty = shortage_qty - %s,
                           resolved_qty  = COALESCE(resolved_qty,0) + %s,
                           resolved_by   = %s,
                           resolved_at   = CURRENT_TIMESTAMP
                    WHERE  shortageid = %s;
                    """,
                    (take, take, user, r.shortageid),
                )

            remaining -= take

        return remaining
