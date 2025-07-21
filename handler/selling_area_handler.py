"""
SellingAreaHandler  –  full version
===================================
•  Keeps resolve_shortages() and restock_item() from earlier drafts.
•  Upsert key on shelf = (itemid, expirationdate, locid, cost_per_unit).
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

__all__ = ["SellingAreaHandler"]


class SellingAreaHandler(DatabaseManager):
    # ───────────────────── private helpers ─────────────────────
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
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (itemid, expirationdate, locid, cost_per_unit)
            DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                          lastupdated = CURRENT_TIMESTAMP;
            """,
            (itemid, expirationdate, quantity, cost_per_unit, locid),
        )
        cur.execute(
            """
            INSERT INTO shelfentries
                  (itemid, expirationdate, quantity, createdby, locid)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (itemid, expirationdate, quantity, created_by, locid),
        )

    # ───────────────────── inventory → shelf ─────────────────────
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

    # ───────────────────── public look‑ups (unchanged) ─────────────────────
    def get_all_items(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT itemid,
                   itemnameenglish AS itemname,
                   shelfthreshold,
                   shelfaverage
              FROM item
          ORDER BY itemnameenglish;
            """
        )
        if not df.empty:
            df["shelfthreshold"] = df["shelfthreshold"].astype("Int64")
            df["shelfaverage"]   = df["shelfaverage"].astype("Int64")
        return df

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT i.itemid,
                   i.itemnameenglish AS itemname,
                   COALESCE(SUM(s.quantity),0) AS totalquantity,
                   i.shelfthreshold,
                   i.shelfaverage
              FROM item i
         LEFT JOIN shelf s ON s.itemid = i.itemid
          GROUP BY i.itemid,i.itemnameenglish,
                   i.shelfthreshold,i.shelfaverage
          ORDER BY i.itemnameenglish;
            """
        )
        if not df.empty:
            df["totalquantity"]  = df["totalquantity"].astype(int)
            df["shelfthreshold"] = df["shelfthreshold"].astype("Int64")
            df["shelfaverage"]   = df["shelfaverage"].astype("Int64")
        return df

    # ───────── shortage resolver (unchanged) ─────────
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
