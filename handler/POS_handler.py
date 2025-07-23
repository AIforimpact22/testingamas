# handler/POS_handler.py
"""
POS_handler
───────────
Database helpers used by the live POS simulator.

Responsibilities
• Create `sales` header rows and `salesitems` detail rows.
• Deduct quantities from the *shelf* and log shortages when shelf stock
  cannot cover the requested quantity.
• **Always record the customer's full basket** in `salesitems` and the
  header totals, even if some units are back‑ordered (shortage).
  Those shortages are later cleared by the Shelf‑refill loop.

⚠️  Shelf auto‑refill lives elsewhere; this handler only writes shortages.
"""

from __future__ import annotations

import json
from typing import List, Dict, Any

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager


# --------------------------------------------------------------------------- #
#                               Main handler class                            #
# --------------------------------------------------------------------------- #
class POSHandler(DatabaseManager):
    # ─────────────────────────── sale header ────────────────────────────
    def create_sale_record(
        self,
        *,
        total_amount: float,
        discount_rate: float,
        total_discount: float,
        final_amount: float,
        payment_method: str,
        cashier: str,
        notes: str = "",
        original_saleid: int | None = None,
    ) -> int | None:
        sql = """
        INSERT INTO sales (
            totalamount, discountrate, totaldiscount, finalamount,
            paymentmethod, cashier, notes, original_saleid
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING saleid
        """
        res = self.execute_command_returning(
            sql,
            (
                total_amount,
                discount_rate,
                total_discount,
                final_amount,
                payment_method,
                cashier,
                notes,
                original_saleid,
            ),
        )
        return int(res[0]) if res else None

    # ───────────────────── line‑items (batch insert) ────────────────────
    def add_sale_items(self, saleid: int, items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        rows = [
            (
                saleid,
                int(it["itemid"]),
                int(it["quantity"]),
                float(it["unitprice"]),
                float(it["totalprice"]),
            )
            for it in items
            if it["quantity"] > 0          # skip zero‑qty edge cases
        ]
        if not rows:
            return

        sql = """
            INSERT INTO salesitems
                  (saleid, itemid, quantity, unitprice, totalprice)
            VALUES %s
        """
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(cur, sql, rows)
        self.conn.commit()

    # ───────────────────── shelf stock helpers ──────────────────────────
    def _deduct_from_shelf(self, itemid: int, qty_needed: int) -> int:
        """
        Consume quantity from the oldest shelf layers (FIFO).
        Returns remaining qty that could **not** be fulfilled (shortage).
        """
        remaining = qty_needed
        layers = self.fetch_data(
            """
            SELECT shelfid, quantity
            FROM   shelf
            WHERE  itemid = %s AND quantity > 0
            ORDER  BY expirationdate
            """,
            (itemid,),
        )

        for lyr in layers.itertuples():
            if remaining == 0:
                break

            if remaining >= lyr.quantity:
                # take whole layer and delete
                self.execute_command("DELETE FROM shelf WHERE shelfid = %s",
                                     (lyr.shelfid,))
                remaining -= lyr.quantity
            else:
                # partial take
                self.execute_command(
                    "UPDATE shelf SET quantity = quantity - %s "
                    "WHERE shelfid = %s",
                    (remaining, lyr.shelfid),
                )
                remaining = 0

        return remaining  # >0 means shortage

    # ─────────────── main POS commit (shortage‑aware) ───────────────────
    def process_sale_with_shortage(
        self,
        *,
        cart_items: List[Dict[str, Any]],
        discount_rate: float,
        payment_method: str,
        cashier: str,
        notes: str = "",
    ):
        """
        • Inserts a `sales` header (placeholder totals first).
        • Deducts what is available from shelf; logs any shortage rows.
        • ALWAYS records the requested quantity in `salesitems`.
        • Updates header totals so they match the till receipt even when
          shortages occur.

        Returns
        -------
        (saleid, shortages_list)
            shortages_list = [{"itemname": str, "qty": int}, …]
        """
        saleid = self.create_sale_record(
            total_amount   = 0,
            discount_rate  = discount_rate,
            total_discount = 0,
            final_amount   = 0,
            payment_method = payment_method,
            cashier        = cashier,
            notes          = notes,
        )
        if saleid is None:
            return None, []

        shortages: list[dict] = []
        lines: List[Dict[str, Any]] = []
        running_total = 0.0

        for it in cart_items:
            iid      = int(it["itemid"])
            req_qty  = int(it["quantity"])
            price    = float(it["sellingprice"])

            # try to fulfil from shelf
            shortage_qty = self._deduct_from_shelf(iid, req_qty)

            # log shortage row if needed
            if shortage_qty > 0:
                self.execute_command(
                    """
                    INSERT INTO shelf_shortage (saleid, itemid, shortage_qty)
                    VALUES (%s,%s,%s)
                    """,
                    (saleid, iid, shortage_qty),
                )
                name = self.fetch_data(
                    "SELECT itemnameenglish FROM item WHERE itemid=%s", (iid,)
                ).iat[0, 0]
                shortages.append({"itemname": name, "qty": shortage_qty})

            # always record the full requested quantity in the receipt
            lines.append(
                dict(
                    itemid     = iid,
                    quantity   = req_qty,
                    unitprice  = price,
                    totalprice = round(req_qty * price, 2),
                )
            )
            running_total += req_qty * price

        # batch insert all line‑items
        self.add_sale_items(saleid, lines)

        # update header totals
        total_disc = round(running_total * discount_rate / 100, 2)
        final_amt  = running_total - total_disc
        self.execute_command(
            """
            UPDATE sales
               SET totalamount   = %s,
                   totaldiscount = %s,
                   finalamount   = %s
             WHERE saleid = %s
            """,
            (running_total, total_disc, final_amt, saleid),
        )

        return saleid, shortages

    # ───────────────────── simple reporting helpers ────────────────────
    def get_sale_details(self, saleid: int):
        sale_df  = self.fetch_data("SELECT * FROM sales WHERE saleid=%s",
                                   (saleid,))
        items_df = self.fetch_data(
            """
            SELECT si.*, i.itemnameenglish AS itemname
            FROM   salesitems si
            JOIN   item        i ON i.itemid = si.itemid
            WHERE  si.saleid = %s
            """,
            (saleid,),
        )
        return sale_df, items_df

    # ---- Held‑bill helpers (unchanged) --------------------------------
    def save_hold(self, *, cashier_id: str, label: str,
                  df_items: pd.DataFrame) -> int:
        payload = df_items[["itemid", "itemname",
                            "quantity", "price"]].to_dict("records")
        hold_id = self.execute_command_returning(
            """
            INSERT INTO pos_holds (hold_label, cashier_id, items)
            VALUES (%s, %s, %s::jsonb)
            RETURNING holdid
            """,
            (label, cashier_id, json.dumps(payload)),
        )[0]
        return int(hold_id)

    def load_hold(self, hold_id: int) -> pd.DataFrame:
        js = self.fetch_data("SELECT items FROM pos_holds WHERE holdid=%s",
                             (hold_id,))
        if js.empty:
            raise ValueError("Hold not found")
        data = js.iat[0, 0]
        rows = json.loads(data) if isinstance(data, str) else data
        df   = pd.DataFrame(rows)

        if "itemname" not in df.columns:
            ids = df["itemid"].tolist()
            q   = "SELECT itemid,itemnameenglish FROM item WHERE itemid IN %s"
            names = self.fetch_data(q, (tuple(ids),)).set_index("itemid")\
                                                    ["itemnameenglish"].to_dict()
            df["itemname"] = df["itemid"].map(names).fillna("Unknown")

        df["total"] = df["quantity"] * df["price"]
        return df[["itemid", "itemname", "quantity", "price", "total"]]

    def delete_hold(self, hold_id: int) -> None:
        self.execute_command("DELETE FROM pos_holds WHERE holdid=%s",
                             (hold_id,))
