# pages/auto_POS.py
"""
Bulk POS Sale Simulation
────────────────────────
Generates synthetic sales and pushes them through CashierHandler.process_sale_with_shortage.
"""

import streamlit as st
import pandas as pd
import random, json
import psycopg2

# NEW IMPORT PATH  → handlers now live under handler/
from handler.cashier_handler import CashierHandler

cashier_handler = CashierHandler()

# ───────────────────────── helpers ──────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def get_item_catalogue() -> pd.DataFrame:
    """Fetch the active item catalogue (id • name • price)."""
    return cashier_handler.fetch_data(
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
    min_items: int = 2,
    max_items: int = 6,
    min_qty: int   = 1,
    max_qty: int   = 5,
) -> list[dict]:
    """Build a random cart with *unique* items."""
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
    """
    Align SERIAL / IDENTITY sequences with current MAX(pk)+1.
    Call before the bulk run and after any duplicate‑key error.
    """
    targets = [
        ("sales",          "saleid"),
        ("salesitems",     "salesitemid"),
        ("shelf_shortage", "shortageid"),
    ]
    for tbl, pk in targets:
        cashier_handler.execute_command(
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

    # ― user parameters ―
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
                        saleid, shortages = cashier_handler.process_sale_with_shortage(
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
                        cashier_handler.conn.rollback()
                        sync_sequences()  # realign sequences then retry
                        attempt += 1
                        msg = (
                            f"UniqueViolation on retry {attempt}: "
                            f"{e.diag.constraint_name}"
                        )
                    except Exception as e:
                        cashier_handler.conn.rollback()
                        msg = f"DB error: {e}"
                        break

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


# ───────────────────────── run page (Streamlit) ────────────────────
st.set_page_config(page_title="Bulk POS Simulator", page_icon="🧪")
run_bulk_test()
