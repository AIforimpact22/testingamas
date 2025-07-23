# handler/POS_handler.py
"""
POS_handler
───────────
Database helpers used by the live POS simulator.

• `process_sales_batch()` bulk‑inserts N baskets in a single transaction.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager


class POSHandler(DatabaseManager):
    # ───────────────────────── Header helper ──────────────────────────
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
        """Classic one‑sale insert (still available for ad‑hoc use)."""
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

    # ─────────────────────── Bulk sales commit ────────────────────────
    def process_sales_batch(self, sales: List[Dict[str, Any]]) -> List[Dict]:
        """
        Bulk‑process *multiple* baskets in one DB transaction.

        Parameters
        ----------
        sales : list of dict
            Each dict needs keys:
              cashier, cart_items, discount_rate, payment_method, notes

        Returns
        -------
        list[dict] – ready for Streamlit debug log
        """
        if not sales:
            return []

        self._ensure_live_conn()
        ts_now = datetime.now().strftime("%F %T")
        log_out: list[dict] = []

        with self.conn.cursor() as cur:
            # 1) Headers ------------------------------------------------------
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

            # 2) Details / shortages / header totals -------------------------
            items_rows, shortage_rows, header_updates = [], [], []

            for sid, sale in zip(saleids, sales):
                running_total = 0.0
                local_items, local_shortages = [], []

                for it in sale["cart_items"]:
                    iid, req_qty = int(it["itemid"]), int(it["quantity"])
                    price = float(it["sellingprice"])

                    # FIFO shelf depletion (no commits inside loop)
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

                    tot_price = round(req_qty * price, 2)
                    items_rows.append((sid, iid, req_qty, price, tot_price))
                    local_items.append(dict(itemid=iid,
                                            itemname=it.get("itemname"),
                                            quantity=req_qty,
                                            unitprice=price,
                                            totalprice=tot_price))
                    running_total += req_qty * price

                    if remaining:
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

                log_out.append({"saleid": sid,
                                "cashier": sale["cashier"],
                                "timestamp": ts_now,
                                "items": local_items,
                                "shortages": local_shortages})

            # 3) Bulk inserts / updates --------------------------------------
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

    # ────────────────────────── Reporting ──────────────────────────────
    def get_sale_details(self, saleid: int):
        sale_df = self.fetch_data("SELECT * FROM sales WHERE saleid=%s", (saleid,))
        items_df = self.fetch_data(
            """
            SELECT si.*, i.itemnameenglish AS itemname
              FROM salesitems si
              JOIN item i ON i.itemid = si.itemid
             WHERE si.saleid = %s
            """,
            (saleid,),
        )
        return sale_df, items_df

    # ---- Held‑bill helpers unchanged ----------------------------------
    # save_hold / load_hold / delete_hold …
