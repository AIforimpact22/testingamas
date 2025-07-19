# pages/auto_POS.py
"""
Bulk POS Sale Simulation
────────────────────────
Generates synthetic sales and auto‑refills the shelf immediately
after each successful sale.
"""

import json
import random

import pandas as pd
import psycopg2
import streamlit as st

from handler.cashier_handler import CashierHandler
from handler.shelf_handler   import ShelfHandler

cashier = CashierHandler()
shelf   = ShelfHandler()

# ───────────────────────── helpers ──────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def get_item_catalogue() -> pd.DataFrame:
    return cashier.fetch_data(
        """
        SELECT itemid,
               itemnameenglish AS itemname,
               sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL
          AND  sellingprice > 0;
        """
    )


def random_cart(
    cat_df: pd.DataFrame,
    min_items: int,
    max_items: int,
    min_qty: int,
    max_qty: int,
) -> list[dict]:
    n_items = random.randint(min_items, min(max_items, len(cat_df)))
    picks   = cat_df.sample(n=n_items, replace=False)
    return [
        {
            "itemid"      : int(r.itemid),
            "quantity"    : random.randint(min_qty, max_qty),
            "sellingprice": float(r.sellingprice),
        }
        for _, r in picks.iterrows()
    ]


def sync_sequences() -> None:
    targets = [
        ("sales",          "saleid"),
        ("salesitems",     "salesitemid"),
        ("shelf_shortage", "shortageid"),
    ]
    for tbl, pk in targets:
        cashier.execute_command(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{tbl}', '{pk}'),
                COALESCE((SELECT MAX({pk}) FROM {tbl}), 0) + 1,
                false
            );
            """
        )

# ───────────────────────── UI / main ──────────────────────────
def run_bulk_test() -> None:
    st.header("🧪 Bulk POS Sale Simulation")

    cat_df = get_item_catalogue()
    if cat_df.empty:
        st.error("Catalogue is empty – cannot run simulation.")
        return

    col1, col2 = st.columns(2)
    with col1:
        num_sales = st.number_input("Number of test sales", 1, 500, 20)
        min_items = st.number_input("Min items / sale", 1, 20, 2)
        max_items = st.number_input("Max items / sale", min_items, 30, 6)
    with col2:
        min_qty    = st.number_input("Min quantity / item", 1, 20, 1)
        max_qty    = st.number_input("Max quantity / item", min_qty, 50, 5)
        pay_method = st.selectbox("Payment method", ["Cash", "Card"])
    note = st.text_input("Extra note (optional)", key="bulk_note")

    if st.button(f"Run {num_sales} simulated sales"):
        results: list[dict] = []
        sync_sequences()  # align sequences once before the loop

        with st.spinner("Running bulk test…"):
            for i in range(int(num_sales)):
                cart = random_cart(cat_df, min_items, max_items, min_qty, max_qty)
                attempt, saleid, shortages = 0, None, []
                msg = ""

                while attempt < 2:  # retry at most once
                    try:
                        saleid, shortages = cashier.process_sale_with_shortage(
                            cart_items     = cart,
                            discount_rate  = 0.0,
                            payment_method = pay_method,
                            cashier        = "BULKTEST",
                            notes          = f"[BULK TEST] {note}".strip(),
                        )
                        msg = (
                            f"✅ Sale #{saleid} OK" if saleid else "❌ Sale failed"
                        )
                        break
                    except psycopg2.errors.UniqueViolation as e:
                        cashier.conn.rollback()
                        sync_sequences()
                        attempt += 1
                        msg = (
                            f"UniqueViolation on retry {attempt}: "
                            f"{e.diag.constraint_name}"
                        )
                    except Exception as e:
                        cashier.conn.rollback()
                        msg = f"DB error: {e}"
                        break

                # 💡 NEW: auto‑refill shelves after each successful sale
                if saleid:
                    shelf.post_sale_restock(cart, user="AUTOSIM")

                results.append(
                    {
                        "sale_no"  : i + 1,
                        "sale_id"  : saleid,
                        "result"   : msg,
                        "shortages": json.dumps(shortages) if shortages else "",
                    }
                )

        ok_count = sum(bool(r["sale_id"]) for r in results)
        st.success(f"Finished: **{ok_count} / {len(results)}** simulated sales succeeded.")
        st.dataframe(pd.DataFrame(results))


# ───────────────────────── run page ──────────────────────────
st.set_page_config(page_title="Bulk POS Simulator", page_icon="🧪")
run_bulk_test()
