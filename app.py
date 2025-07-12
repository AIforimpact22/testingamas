import streamlit as st
import pandas as pd
import random, json
import psycopg2
from cashier.cashier_handler import CashierHandler   # ← adjust if path differs

cashier_handler = CashierHandler()                   # uses your real logic

# ───────────────────────── helpers ──────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def get_item_catalogue():
    return cashier_handler.fetch_data("""
        SELECT itemid, itemnameenglish AS itemname, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
    """)

def random_cart(cat_df, min_items=2, max_items=6, min_qty=1, max_qty=5):
    n_items = random.randint(min_items, min(max_items, len(cat_df)))
    picks   = cat_df.sample(n=n_items, replace=False)
    return [
        {
            "itemid":       int(r.itemid),
            "quantity":     random.randint(min_qty, max_qty),
            "sellingprice": float(r.sellingprice)
        }
        for _, r in picks.iterrows()
    ]

def sync_sequences():
    """
    Align every SERIAL/IDENTITY sequence with the current MAX(pk)+1.
    Safe to run repeatedly.
    """
    seq_targets = [
        ("sales",          "saleid"),
        ("salesitems",     "salesitemid"),
        ("shelf_shortage", "shortageid"),
    ]
    for tbl, pk in seq_targets:
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
def display_bulk_test():
    st.title("🧪 Bulk POS Sale Simulation — self‑healing")
    st.write(
        """
        Generates random sales and commits them through the same
        `process_sale_with_shortage()` logic your cashiers use.
        The script auto‑repairs out‑of‑sync sequences and retries once on any
        **duplicate‑key** error.
        """
    )

    cat_df = get_item_catalogue()
    if cat_df.empty:
        st.error("Catalogue is empty."); return

    # — parameters —
    num_sales = st.number_input("Number of test sales", 1, 500, 20)
    min_items = st.number_input("Min items / sale", 1, 20, 2)
    max_items = st.number_input("Max items / sale", min_items, 30, 6)
    min_qty   = st.number_input("Min qty / item", 1, 20, 1)
    max_qty   = st.number_input("Max qty / item", min_qty, 50, 5)
    pay_method= st.selectbox("Payment method", ["Cash", "Card"])
    note      = st.text_input("Extra note (optional)", "")

    if st.button(f"Run {num_sales} simulated sales"):
        results = []
        sync_sequences()                            # 1️⃣ initial alignment
        with st.spinner("Running bulk test…"):
            for i in range(int(num_sales)):
                cart = random_cart(
                    cat_df, min_items, max_items, min_qty, max_qty
                )
                attempt, saleid, shortages, msg = 0, None, [], ""
                while attempt < 2:                  # 2️⃣ retry loop (max 1 retry)
                    try:
                        saleid, shortages = cashier_handler.process_sale_with_shortage(
                            cart_items     = cart,
                            discount_rate  = 0.0,
                            payment_method = pay_method,
                            cashier        = "BULKTEST",
                            notes          = f"[BULK TEST] {note}".strip()
                        )
                        msg = f"✅ Sale #{saleid} OK" if saleid else "❌ Sale failed"
                        break                       # success → leave retry loop
                    except psycopg2.errors.UniqueViolation as e:
                        cashier_handler.conn.rollback()
                        sync_sequences()            # 3️⃣ fix sequences, retry once
                        msg = f"UniqueViolation: {e.diag.constraint_name}"
                        attempt += 1
                    except Exception as e:
                        cashier_handler.conn.rollback()
                        msg = f"DB error: {e}"
                        break                       # unrecoverable

                results.append({
                    "sale":       i + 1,
                    "sale_id":    saleid,
                    "result":     msg,
                    "shortages":  json.dumps(shortages) if shortages else ""
                })

        st.success(f"Finished. {sum(bool(r['sale_id']) for r in results)} "
                   f"of {len(results)} simulated sales succeeded.")
        st.dataframe(pd.DataFrame(results))

if __name__ == "__main__":
    display_bulk_test()
