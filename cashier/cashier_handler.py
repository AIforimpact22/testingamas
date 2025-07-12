# cashier/cashier_handler.py
import json
import pandas as pd
from datetime import date
from psycopg2.extras import execute_values
from db_handler import DatabaseManager


class CashierHandler(DatabaseManager):
    """
    DB helpers for POS:
      • create sale header & line items
      • keep shelf stock in sync
      • log shortages when shelf stock is insufficient
    """

    # ───────────────────── sale header ─────────────────────
    def create_sale_record(
        self,
        total_amount,
        discount_rate,
        total_discount,
        final_amount,
        payment_method,
        cashier,
        notes="",
        original_saleid=None,
    ) -> int | None:
        sql = """
        INSERT INTO sales (
            totalamount, discountrate, totaldiscount, finalamount,
            paymentmethod, cashier, notes, original_saleid
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING saleid;
        """
        res = self.execute_command_returning(
            sql,
            (
                float(total_amount),
                float(discount_rate),
                float(total_discount),
                float(final_amount),
                payment_method,
                cashier,
                notes,
                original_saleid,
            ),
        )
        return int(res[0]) if res else None

    # ───────────────────── line items (batch) ─────────────────────
    def add_sale_items(self, saleid, items_list):
        rows = [
            (
                int(saleid),
                int(it["itemid"]),
                int(it["quantity"]),
                float(it["unitprice"]),
                float(it["totalprice"]),
            )
            for it in items_list
        ]
        sql = """
            INSERT INTO salesitems
                  (saleid, itemid, quantity, unitprice, totalprice)
            VALUES %s
        """
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            execute_values(cur, sql, rows)
        self.conn.commit()

    # ───────────────────── shelf stock helpers ─────────────────────
    def _deduct_from_shelf(self, itemid: int, qty_needed: int) -> int:
        """
        Remove qty from oldest shelf layers.
        If a layer is emptied, DELETE the row.
        Returns the remaining (shortage) qty.
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
                # consume the whole layer → delete row
                self.execute_command(
                    "DELETE FROM shelf WHERE shelfid = %s",
                    (lyr.shelfid,),
                )
                remaining -= lyr.quantity
            else:
                # partial take → update residual qty
                self.execute_command(
                    "UPDATE shelf SET quantity = quantity - %s "
                    "WHERE shelfid = %s",
                    (remaining, lyr.shelfid),
                )
                remaining = 0
    
        return remaining  # >0 means shortage
    
    # legacy method (still used by Return flow)
    def reduce_shelf_stock(self, itemid, quantity_sold):
        """
        Subtract qty from oldest shelf layer.
        If a layer reaches 0, delete it.
        Negative rows are no longer created.
        """
        itemid_py = int(itemid)
        qty_py    = int(quantity_sold)
    
        layer = self.fetch_data(
            """
            SELECT shelfid, quantity
            FROM   shelf
            WHERE  itemid = %s
            ORDER  BY expirationdate
            LIMIT 1
            """,
            (itemid_py,),
        )
    
        if layer.empty:
            return  # nothing on shelf – caller should now rely on shortage table
    
        shelfid = int(layer.iloc[0].shelfid)
        qty_left = int(layer.iloc[0].quantity) - qty_py
    
        if qty_left <= 0:
            self.execute_command("DELETE FROM shelf WHERE shelfid = %s", (shelfid,))
        else:
            self.execute_command(
                "UPDATE shelf SET quantity = %s, lastupdated = CURRENT_TIMESTAMP "
                "WHERE shelfid = %s",
                (qty_left, shelfid),
            )
    

    # ───────────────────── SHORTAGE-AWARE POS commit ─────────────────
    def process_sale_with_shortage(
        self,
        *,
        cart_items,
        discount_rate,
        payment_method,
        cashier,
        notes="",
    ):
        """
        Create sale, deduct shelf quantities, log shortages.
        Returns (saleid, shortages_list) where shortages_list contains
        dicts: {"itemname": str, "qty": int}
        """
        # --- header placeholder (totals filled later) ----------------
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
            return None

        shortages: list[dict] = []
        sale_lines  = []
        running_total = 0

        for item in cart_items:
            iid = int(item["itemid"])
            qty = int(item["quantity"])
            price = float(item["sellingprice"])
            running_total += qty * price

            # 1. try to pull from shelf
            short = self._deduct_from_shelf(iid, qty)

            if short > 0:
                # shortage table entry
                self.execute_command(
                    """
                    INSERT INTO shelf_shortage (saleid, itemid, shortage_qty)
                    VALUES (%s, %s, %s)
                    """,
                    (saleid, iid, short),
                )
                # fetch name for toast
                name = self.fetch_data(
                    "SELECT itemnameenglish FROM item WHERE itemid = %s",
                    (iid,),
                ).iloc[0, 0]
                shortages.append({"itemname": name, "qty": short})

            # 2. salesitems row (record full sold qty)
            sale_lines.append(
                {
                    "itemid": iid,
                    "quantity": qty,
                    "unitprice": price,
                    "totalprice": round(qty * price, 2),
                }
            )

        # batch insert lines
        self.add_sale_items(saleid, sale_lines)

        # update totals in header
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

    # ───────────────────── reporting helper ─────────────────────
    def get_sale_details(self, saleid):
        sale_df  = self.fetch_data("SELECT * FROM sales WHERE saleid = %s", (saleid,))
        items_df = self.fetch_data(
            """
            SELECT si.*, i.itemnameenglish AS itemname
              FROM salesitems si
              JOIN item i ON i.itemid = si.itemid
             WHERE si.saleid = %s;
            """,
            (saleid,),
        )
        return sale_df, items_df

    # ---- Held bill helpers ---------------------------------------------
    def save_hold(self, *, cashier_id: str, label: str,
                  df_items: pd.DataFrame) -> int:
        """Store current bill in pos_holds (items as JSONB array)."""
        # only the columns we need to rebuild a bill
        payload = df_items[["itemid", "itemname",
                            "quantity", "price"]].to_dict(orient="records")
        hold_id = self.execute_command_returning(
            """
            INSERT INTO pos_holds (hold_label, cashier_id, items)
            VALUES (%s, %s, %s::jsonb)         -- cast text → jsonb
            RETURNING holdid
            """,
            (label, cashier_id, json.dumps(payload)),
        )[0]
        return int(hold_id)

    def load_hold(self, hold_id: int) -> pd.DataFrame:
        """
        Return the held bill as a DataFrame shaped like sales_table.
        Fills itemname if missing.
        """
        js = self.fetch_data("SELECT items FROM pos_holds WHERE holdid=%s",
                             (hold_id,))
        if js.empty:
            raise ValueError("Hold not found")

        data = js.iloc[0, 0]
        rows = json.loads(data) if isinstance(data, str) else data
        df   = pd.DataFrame(rows)

        # 🔹 back-fill itemname when absent
        if "itemname" not in df.columns:
            ids = df["itemid"].tolist()
            if len(ids) == 1:
                q   = "SELECT itemid, itemnameenglish AS name FROM item WHERE itemid = %s"
                par = (ids[0],)
            else:
                q   = "SELECT itemid, itemnameenglish AS name FROM item WHERE itemid IN %s"
                par = (tuple(ids),)
            names = self.fetch_data(q, par).set_index("itemid")["name"].to_dict()
            df["itemname"] = df["itemid"].map(names).fillna("Unknown")

        df["total"] = df["quantity"] * df["price"]
        return df[["itemid", "itemname", "quantity", "price", "total"]]
    def delete_hold(self, hold_id: int):
        self.execute_command("DELETE FROM pos_holds WHERE holdid=%s", (hold_id,))
