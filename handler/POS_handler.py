# handler/POS_handler.py
"""
POS_handler
───────────
Database helpers for the live POS simulator.

• `process_sales_batch()` bulk‑inserts N baskets in ONE transaction,
  writing fully‑populated header rows first (no follow‑up UPDATE).
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
        Bulk‑process *multiple* baskets in ONE DB transaction.

        Each element in `sales` must include:
            cashier, cart_items, discount_rate, payment_method, notes

        Returns
        -------
        list[dict] – entries for Streamlit debug log
        """
        if not sales:
            return []

        self._ensure_live_conn()
        ts_now = datetime.now().strftime("%F %T")
        log_out: list[Dict] = []

        # ---- 1) build header rows with FINAL totals ------------------------
        header_rows: list[tuple] = []
        for s in sales:
            gross = sum(
                int(it["quantity"]) * float(it["sellingprice"])
                for it in s["cart_items"]
            )
            disc  = round(gross * s["discount_rate"] / 100, 2)
            final = gross - disc
            header_rows.append(
                (
                    gross,
                    s["discount_rate"],
                    disc,
                    final,
                    s["payment_method"],
                    s["cashier"],
                    s.get("notes", ""),
                    None,  # original_saleid
                )
            )

        with self.conn.cursor() as cur:
            # ---- 2) insert headers, capture IDs ---------------------------
            saleids = [
                row[0]
                for row in execute_values(
                    cur,
                    """
                    INSERT INTO sales (
                        totalamount, discountrate, totaldiscount, finalamount,
                        paymentmethod, cashier, notes, original_saleid
                    )
                    VALUES %s
                    RETURNING saleid
                    """,
                    header_rows,
                    fetch=True,
                )
            ]

            items_rows:    list[tuple] = []
            shortage_rows: list[tuple] = []

            # ---- 3) process each basket ----------------------------------
            for sid, sale in zip(saleids, sales):
                local_items:     list[Dict] = []
                local_shortages: list[Dict] = []

                for it in sale["cart_items"]:
                    iid  = int(it["itemid"])
                    qty  = int(it["quantity"])
                    price = float(it["sellingprice"])
                    remaining = qty

                    # FIFO depletion from shelf
                    cur.execute(
                        """
                        SELECT shelfid, quantity
                          FROM shelf
                         WHERE itemid = %s AND quantity > 0
                      ORDER BY expirationdate
                        """,
                        (iid,),
                    )
                    for shelfid, q in cur.fetchall():
                        if remaining == 0:
                            break
                        take = min(remaining, q)
                        if take == q:
                            cur.execute("DELETE FROM shelf WHERE shelfid=%s",
                                        (shelfid,))
                        else:
                            cur.execute(
                                "UPDATE shelf SET quantity = quantity - %s "
                                "WHERE shelfid = %s",
                                (take, shelfid),
                            )
                        remaining -= take

                    tot_price = round(qty * price, 2)
                    items_rows.append((sid, iid, qty, price, tot_price))
                    local_items.append(
                        dict(itemid=iid,
                             itemname=it.get("itemname"),
                             quantity=qty,
                             unitprice=price,
                             totalprice=tot_price)
                    )

                    if remaining > 0:
                        shortage_rows.append((sid, iid, remaining))
                        name = self.fetch_data(
                            "SELECT itemnameenglish FROM item WHERE itemid=%s",
                            (iid,),
                        ).iat[0, 0]
                        local_shortages.append({"itemname": name,
                                                "qty": remaining})

                log_out.append(
                    dict(
                        saleid=sid,
                        cashier=sale["cashier"],
                        timestamp=ts_now,
                        items=local_items,
                        shortages=local_shortages,
                    )
                )

            # ---- 4) bulk‑insert items & shortages -------------------------
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

            self.conn.commit()

        return log_out

    # ────────────────────────── Reporting ──────────────────────────────
    def get_sale_details(self, saleid: int):
        sale_df = self.fetch_data(
            "SELECT * FROM sales WHERE saleid=%s", (saleid,)
        )
        items_df = self.fetch_data(
            """
            SELECT si.*, i.itemnameenglish AS itemname
              FROM salesitems si
              JOIN item        i ON i.itemid = si.itemid
             WHERE si.saleid = %s
            """,
            (saleid,),
        )
        return sale_df, items_df

    # ───────────────────── Held‑bill helpers ───────────────────────────
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
        js = self.fetch_data(
            "SELECT items FROM pos_holds WHERE holdid=%s", (hold_id,)
        )
        if js.empty:
            raise ValueError("Hold not found")
        data = js.iat[0, 0]
        rows = json.loads(data) if isinstance(data, str) else data
        df   = pd.DataFrame(rows)

        if "itemname" not in df.columns:
            ids   = df["itemid"].tolist()
            names = self.fetch_data(
                "SELECT itemid,itemnameenglish FROM item WHERE itemid IN %s",
                (tuple(ids),)
            ).set_index("itemid")["itemnameenglish"].to_dict()
            df["itemname"] = df["itemid"].map(names).fillna("Unknown")

        df["total"] = df["quantity"] * df["price"]
        return df[["itemid", "itemname", "quantity", "price", "total"]]

    def delete_hold(self, hold_id: int) -> None:
        self.execute_command(
            "DELETE FROM pos_holds WHERE holdid=%s", (hold_id,)
        )
