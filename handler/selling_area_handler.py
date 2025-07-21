"""
SellingAreaHandler  (was ShelfHandler)
=====================================
All DB helpers for the Selling Area plus auto‑refill utilities.

Changes vs. previous revision
─────────────────────────────
• NEW  _lookup_locid() – pulls fixed shelf slot from item_slot.
• transfer_from_inventory() now accepts locid=None; it resolves
  the correct location automatically, raising ValueError if the
  item has no slot mapping.
• _upsert_shelf_layer() “ON CONFLICT (itemid,expirationdate,locid,cost_per_unit)”
  so the UNIQUE key matches the physical slot.
"""

from __future__ import annotations
import pandas as pd
from db_handler import DatabaseManager
from psycopg2.extras import execute_values

__all__ = ["SellingAreaHandler"]


class SellingAreaHandler(DatabaseManager):
    # ───────────────────────── internal helpers ─────────────────────────
    def _lookup_locid(self, itemid: int) -> str | None:
        """Return the fixed shelf slot for *itemid* (or None if missing)."""
        df = self.fetch_data(
            "SELECT locid FROM item_slot WHERE itemid = %s LIMIT 1",
            (itemid,),
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
        """Insert / update exactly ONE shelf layer (assumes open cursor)."""
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
        # log movement
        cur.execute(
            """
            INSERT INTO shelfentries
                  (itemid, expirationdate, quantity, createdby, locid)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (itemid, expirationdate, quantity, created_by, locid),
        )

    # ───────────────────── inventory → shelf transfer ─────────────────────
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
        """
        Atomically move *quantity* of one inventory layer to its shelf slot.

        If *locid* is None we look it up in item_slot.
        Raises ValueError when quantity unavailable or slot missing.
        """
        if locid is None:
            locid = self._lookup_locid(itemid)
            if locid is None:
                raise ValueError(f"Item {itemid} has no shelf slot mapping.")

        self._ensure_live_conn()
        with self.conn:                      # BEGIN … COMMIT once
            with self.conn.cursor() as cur:
                # 1️⃣  decrement that exact layer in inventory
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
                    raise ValueError("Insufficient inventory for that layer")

                # 2️⃣  upsert shelf layer + movement log
                self._upsert_shelf_layer(
                    cur=cur,
                    itemid=itemid,
                    expirationdate=expirationdate,
                    quantity=quantity,
                    cost_per_unit=cost_per_unit,
                    locid=locid,
                    created_by=created_by,
                )

    # ─────────────────────── public queries (unchanged) ──────────────────────
    def get_all_items(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT itemid,
                   itemnameenglish AS itemname,
                   shelfthreshold,
                   shelfaverage
              FROM item
          ORDER BY itemnameenglish
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
          GROUP BY i.itemid, i.itemnameenglish,
                   i.shelfthreshold, i.shelfaverage
          ORDER BY i.itemnameenglish
            """
        )
        if not df.empty:
            df["totalquantity"]  = df["totalquantity"].astype(int)
            df["shelfthreshold"] = df["shelfthreshold"].astype("Int64")
            df["shelfaverage"]   = df["shelfaverage"].astype("Int64")
        return df

    # (resolve_shortages & restock_item unchanged from last version)
    # … keep previous implementation …
