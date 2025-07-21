"""
SellingAreaHandler
==================
All DB helpers for the Selling Area (shelf) plus auto‑refill utilities.

Key point → the `shelf` table’s composite unique key is
(itemid, expirationdate, cost_per_unit, locid).  
Up‑serts therefore include a `locid` and the ON CONFLICT clause
matches that full key.
"""

from __future__ import annotations

import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

__all__ = ["SellingAreaHandler"]


class SellingAreaHandler(DatabaseManager):
    # ───────────────────── internal helper ─────────────────────
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
        """Insert / update exactly one shelf layer (assumes open cursor)."""
        cur.execute(
            """
            INSERT INTO shelf (itemid, expirationdate, quantity,
                               cost_per_unit, locid)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (itemid, expirationdate, cost_per_unit, locid)
            DO UPDATE SET quantity    = shelf.quantity + EXCLUDED.quantity,
                          lastupdated = CURRENT_TIMESTAMP;
            """,
            (itemid, expirationdate, quantity, cost_per_unit, locid),
        )
        # movement log
        cur.execute(
            """
            INSERT INTO shelfentries (itemid, expirationdate, quantity,
                                      createdby, locid)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (itemid, expirationdate, quantity, created_by, locid),
        )

    # ───────────────────── Inventory → Shelf ─────────────────────
    def transfer_from_inventory(
        self,
        *,
        itemid: int,
        expirationdate,
        quantity: int,
        cost_per_unit: float,
        created_by: str,
        locid: str = "AUTO",
    ) -> None:
        """
        Move a cost layer from back‑store inventory to the selling‑area shelf.
        All steps run in ONE database transaction.
        """
        self._ensure_live_conn()
        with self.conn:                                # BEGIN … COMMIT
            with self.conn.cursor() as cur:
                # Decrement inventory
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
                    raise ValueError(
                        "Not enough stock in that inventory layer."
                    )

                # Up‑sert into shelf + log
                self._upsert_shelf_layer(
                    cur=cur,
                    itemid=itemid,
                    expirationdate=expirationdate,
                    quantity=quantity,
                    cost_per_unit=cost_per_unit,
                    locid=locid,
                    created_by=created_by,
                )

    # ───────────────────── shelf queries ─────────────────────
    def get_shelf_items(self) -> pd.DataFrame:
        return self.fetch_data(
            """
            SELECT  s.shelfid,
                    s.itemid,
                    i.itemnameenglish AS itemname,
                    s.quantity,
                    s.expirationdate,
                    s.cost_per_unit,
                    s.locid,
                    s.lastupdated
            FROM    shelf s
            JOIN    item i ON s.itemid = i.itemid
            ORDER   BY i.itemnameenglish, s.expirationdate;
            """
        )

    # ───────────────────── inventory look‑ups ─────────────────────
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
            JOIN    item i ON inv.itemid = i.itemid
            WHERE   inv.quantity > 0
            ORDER   BY i.itemnameenglish, inv.expirationdate;
            """
        )

    # ───────────────────── get / update item master ─────────────────────
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

    def update_shelf_settings(
        self,
        itemid: int,
        new_threshold: int | None,
        new_average: int | None,
    ) -> None:
        self.execute_command(
            """
            UPDATE item
            SET    shelfthreshold = %s,
                   shelfaverage   = %s
            WHERE  itemid = %s;
            """,
            (new_threshold, new_average, itemid),
        )

    def get_shelf_quantity_by_item(self) -> pd.DataFrame:
        df = self.fetch_data(
            """
            SELECT  i.itemid,
                    i.itemnameenglish AS itemname,
                    COALESCE(SUM(s.quantity), 0) AS totalquantity,
                    i.shelfthreshold,
                    i.shelfaverage
            FROM    item i
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

    # ───────────────────── shortage resolver ─────────────────────
    def resolve_shortages(
        self, *, itemid: int, qty_need: int, user: str
    ) -> int:
        """
        Consume open shortages for *itemid* (oldest first) and return
        the quantity still NOT covered.
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
                # fully solved → delete
                self.execute_command(
                    "DELETE FROM shelf_shortage WHERE shortageid = %s;",
                    (r.shortageid,),
                )
            else:
                # partial solve → shrink
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                    SET    shortage_qty = shortage_qty - %s,
                           resolved_qty  = COALESCE(resolved_qty, 0) + %s,
                           resolved_by   = %s,
                           resolved_at   = CURRENT_TIMESTAMP
                    WHERE  shortageid = %s;
                    """,
                    (take, take, user, r.shortageid),
                )
            remaining -= take
        return remaining

    # ───────────────────── auto‑refill (POS helper) ─────────────────────
    def restock_item(self, itemid: int, *, user: str = "AUTOSIM") -> None:
        """
        Bring shelf stock for *itemid* up to its threshold/average,
        pulling from inventory if available.
        """
        kpi = self.get_shelf_quantity_by_item()
        row = kpi.loc[kpi.itemid == itemid]
        current   = int(row.totalquantity.iloc[0]) if not row.empty else 0
        threshold = int(row.shelfthreshold.iloc[0] or 0)
        target    = int(row.shelfaverage  .iloc[0] or threshold or 0)

        if current >= threshold:
            return

        need = max(target - current, threshold - current)
        need = self.resolve_shortages(itemid=itemid, qty_need=need, user=user)
        if need <= 0:
            return

        layers = self.fetch_data(
            """
            SELECT expirationdate, quantity, cost_per_unit
            FROM   inventory
            WHERE  itemid = %s AND quantity > 0
            ORDER  BY expirationdate, cost_per_unit;
            """,
            (itemid,),
        )
        for lyr in layers.itertuples():
            take = min(need, int(lyr.quantity))
            self.transfer_from_inventory(
                itemid=itemid,
                expirationdate=lyr.expirationdate,
                quantity=take,
                cost_per_unit=float(lyr.cost_per_unit),
                created_by=user,
                locid="AUTO",
            )
            need -= take
            if need <= 0:
                break
        # Any remaining 'need' simply means inventory is empty.
        return

    def post_sale_restock(self, cart: list[dict], *, user: str = "AUTOSIM") -> None:
        """Call once per successful sale to refill every sold SKU."""
        for entry in cart:
            self.restock_item(int(entry["itemid"]), user=user)
