# handler/POS_handler.py
"""
POS_handler
───────────
Database helpers used by the live POS simulator.

New in this version
───────────────────
• process_sales_batch()  → bulk‑insert N sales in ONE shot
  (headers, salesitems, shortages, header‑totals update).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Dict, Any

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager


# --------------------------------------------------------------------------- #
#                               Main handler class                            #
# --------------------------------------------------------------------------- #
class POSHandler(DatabaseManager):
    # ─────────────────────────── sale header ────────────────────────────
    def create_sale_record(   # unchanged – still useful outside the batch API
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

    # ───────────────────── NEW: bulk sales processing ────────────────────
    def process_sales_batch(
        self,
        sales: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Bulk‑process *multiple* baskets in ONE DB transaction.

        Parameters
        ----------
        sales : list[dict]
            [
              {
                "cashier":        "CASH01",
                "cart_items":     [ {itemid, quantity, sellingprice, itemname}, … ],
                "discount_rate":  0.0,
                "payment_method": "Cash",
                "notes":          "...",
              },
              …
            ]

        Returns
        -------
        list[dict]
            Same schema the UI used before for its debug log:
              {"saleid", "cashier", "timestamp", "items", "shortages"}
        """
        if not sales:
            return []

        self._ensure_live_conn()
        log_out: list[dict] = []
        ts_now = datetime.now().strftime("%F %T")

        with self.conn.cursor() as cur:
            # -----------------------------------------------------------------
            # 1) Insert header placeholders → grab all saleids
            # -----------------------------------------------------------------
            header_rows = [
                (0.0, s["discount_rate"], 0.0, 0.0, s["payment_method"],
                 s["cashier"], s.get("notes", ""), None)
                for s in sales
            ]
            header_sql = """
                INSERT INTO sales (
                    totalamount, discountrate, totaldiscount, finalamount,
                    paymentmethod, cashier, notes, original_saleid
                )
                VALUES %s
                RETURNING saleid
            """
            saleids = [row[0] for row in
                       execute_values(cur, header_sql, header_rows, fetch=True)]

            # -----------------------------------------------------------------
            # 2) Build rows for salesitems, shortages, header‑totals
            # -----------------------------------------------------------------
            items_rows:     list[tuple] = []
            shortage_rows:  list[tuple] = []
            header_updates: list[tuple] = []   # total, disc, final, saleid

            for sid, sale in zip(saleids, sales):
                running_total = 0.0
                local_items:    list[dict] = []
                local_shortages: list[dict] = []

                for it in sale["cart_items"]:
                    iid     = int(it["itemid"])
                    req_qty = int(it["quantity"])
                    price   = float(it["sellingprice"])

                    # ---------- FIFO depletion WITHOUT per‑row commits ----------
                    remaining = req_qty
                    cur.execute(
                        """
                        SELECT shelfid, quantity
                          FROM shelf
                         WHERE itemid = %s AND quantity > 0
                      ORDER BY expirationdate
                        """,
                        (iid,),
                    )
                    for shelfid, qty in cur.fetchall():
                        if remaining == 0:
                            break
                        if remaining >= qty:
                            cur.execute("DELETE FROM shelf WHERE shelfid=%s",
                                        (shelfid,))
                            remaining -= qty
                        else:
                            cur.execute(
                                "UPDATE shelf SET quantity = quantity - %s "
                                "WHERE shelfid = %s",
                                (remaining, shelfid),
                            )
                            remaining = 0

                    # -------- record line‑item (full requested qty) -------------
                    tot_price = round(req_qty * price, 2)
                    items_rows.append(
                        (sid, iid, req_qty, price, tot_price)
                    )
                    local_items.append(
                        dict(itemid=iid,
                             itemname=it.get("itemname"),
                             quantity=req_qty,
                             unitprice=price,
                             totalprice=tot_price)
                    )
                    running_total += req_qty * price

                    # -------- shortage row (if any) ------------------------------
                    if remaining > 0:
                        shortage_rows.append((sid, iid, remaining))
                        name = self.fetch_data(
                            "SELECT itemnameenglish FROM item WHERE itemid=%s",
                            (iid,),
                        ).iat[0, 0]
                        local_shortages.append(
                            {"itemname": name, "qty": remaining}
                        )

                disc  = round(running_total * sale["discount_rate"] / 100, 2)
                final = running_total - disc
                header_updates.append((running_total, disc, final, sid))

                log_out.append(
                    {
                        "saleid":    sid,
                        "cashier":   sale["cashier"],
                        "timestamp": ts_now,
                        "items":     local_items,
                        "shortages": local_shortages,
                    }
                )

            # -----------------------------------------------------------------
            # 3) Bulk insert detail & shortage rows
            # -----------------------------------------------------------------
            execute_values(
                cur,
                """
                INSERT INTO salesitems
                      (saleid, itemid, quantity, unitprice, totalprice)
                VALUES %s
                """,
                items_rows,
            )

            if shortage_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO shelf_shortage (saleid, itemid, shortage_qty)
                    VALUES %s
                    """,
                    shortage_rows,
                )

            # -----------------------------------------------------------------
            # 4) Bulk‑update header totals
            # -----------------------------------------------------------------
            cur.executemany(
                """
                UPDATE sales
                   SET totalamount   = %s,
                       totaldiscount = %s,
                       finalamount   = %s
                 WHERE saleid = %s
                """,
                header_updates,
            )

            self.conn.commit()

        return log_out

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
