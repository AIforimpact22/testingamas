# handler/POS_handler.py
"""
POS_handler
───────────
Database helpers for the live POS simulator.

• process_sales_batch() bulk‑inserts N baskets in ONE transaction,
  subtracts shelf quantities, stamps `lastupdate`,
  and records shortages.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager


class POSHandler(DatabaseManager):
    # ───────────────────────── Single‑sale helper ──────────────────────
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
        res = self.execute_command_returning(
            """
            INSERT INTO sales (
                totalamount, discountrate, totaldiscount, finalamount,
                paymentmethod, cashier, notes, original_saleid
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING saleid
            """,
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

    # ─────────────────────── Bulk basket commit ────────────────────────
    def process_sales_batch(self, sales: List[Dict[str, Any]]) -> List[Dict]:
        if not sales:
            return []

        ts_now = datetime.now().strftime("%F %T")
        self._ensure_live_conn()
        debug_log: list[Dict] = []

        # 1) header rows with final totals
        hdr_rows = []
        for s in sales:
            gross = sum(int(it["quantity"]) * float(it["sellingprice"])
                        for it in s["cart_items"])
            disc  = round(gross * s["discount_rate"] / 100, 2)
            hdr_rows.append(
                (gross, s["discount_rate"], disc, gross - disc,
                 s["payment_method"], s["cashier"], s.get("notes", ""), None)
            )

        with self.conn.cursor() as cur:
            saleids = [r[0] for r in execute_values(
                cur,
                """
                INSERT INTO sales (
                    totalamount, discountrate, totaldiscount, finalamount,
                    paymentmethod, cashier, notes, original_saleid
                )
                VALUES %s
                RETURNING saleid
                """,
                hdr_rows,
                fetch=True,
            )]

            items_rows, shortage_rows = [], []

            for sid, s in zip(saleids, sales):
                local_items, local_short = [], []

                for it in s["cart_items"]:
                    iid, qty = int(it["itemid"]), int(it["quantity"])
                    price    = float(it["sellingprice"])
                    remain   = qty

                    # FIFO shelf depletion
                    cur.execute(
                        """
                        SELECT shelfid, quantity
                          FROM shelf
                         WHERE itemid=%s AND quantity>0
                     ORDER BY expirationdate
                        """, (iid,))
                    for shelfid, layer_qty in cur.fetchall():
                        if remain == 0:
                            break
                        take = min(remain, layer_qty)
                        if take == layer_qty:
                            cur.execute("DELETE FROM shelf WHERE shelfid=%s",
                                        (shelfid,))
                        else:
                            cur.execute(
                                """
                                UPDATE shelf
                                   SET quantity   = quantity - %s,
                                       lastupdate = CURRENT_TIMESTAMP
                                 WHERE shelfid    = %s
                                """, (take, shelfid))
                        remain -= take

                    tot_price = round(qty * price, 2)
                    items_rows.append((sid, iid, qty, price, tot_price))
                    local_items.append(
                        dict(itemid=iid, itemname=it.get("itemname"),
                             quantity=qty, unitprice=price,
                             totalprice=tot_price)
                    )

                    if remain:
                        shortage_rows.append((sid, iid, remain))
                        name = self.fetch_data(
                            "SELECT itemnameenglish FROM item WHERE itemid=%s",
                            (iid,)).iat[0, 0]
                        local_short.append({"itemname": name, "qty": remain})

                debug_log.append(
                    dict(saleid=sid, cashier=s["cashier"], timestamp=ts_now,
                         items=local_items, shortages=local_short)
                )

            execute_values(
                cur,
                "INSERT INTO salesitems "
                "(saleid,itemid,quantity,unitprice,totalprice) VALUES %s",
                items_rows,
            )
            if shortage_rows:
                execute_values(
                    cur,
                    "INSERT INTO shelf_shortage "
                    "(saleid,itemid,shortage_qty) VALUES %s",
                    shortage_rows,
                )
            self.conn.commit()

        return debug_log

    # ──────────── Reporting & Held‑bill helpers (unchanged) ────────────
    def get_sale_details(self, saleid: int):
        hdr = self.fetch_data("SELECT * FROM sales WHERE saleid=%s", (saleid,))
        det = self.fetch_data(
            """
            SELECT si.*, i.itemnameenglish AS itemname
              FROM salesitems si
              JOIN item i ON i.itemid = si.itemid
             WHERE si.saleid=%s
            """, (saleid,))
        return hdr, det

    def save_hold(self, *, cashier_id: str, label: str,
                  df_items: pd.DataFrame) -> int:
        payload = df_items[["itemid", "itemname",
                            "quantity", "price"]].to_dict("records")
        hid = self.execute_command_returning(
            """
            INSERT INTO pos_holds (hold_label, cashier_id, items)
            VALUES (%s,%s,%s::jsonb)
            RETURNING holdid
            """, (label, cashier_id, json.dumps(payload)))[0]
        return int(hid)

    def load_hold(self, hold_id: int) -> pd.DataFrame:
        js = self.fetch_data("SELECT items FROM pos_holds WHERE holdid=%s",
                             (hold_id,))
        if js.empty:
            raise ValueError("Hold not found")
        rows = json.loads(js.iat[0, 0])
        df   = pd.DataFrame(rows)
        if "itemname" not in df.columns:
            names = self.fetch_data(
                "SELECT itemid,itemnameenglish FROM item WHERE itemid IN %s",
                (tuple(df.itemid),)).set_index("itemid")["itemnameenglish"]
            df["itemname"] = df.itemid.map(names).fillna("Unknown")
        df["total"] = df.quantity * df.price
        return df[["itemid", "itemname", "quantity", "price", "total"]]

    def delete_hold(self, hold_id: int) -> None:
        self.execute_command("DELETE FROM pos_holds WHERE holdid=%s",
                             (hold_id,))
