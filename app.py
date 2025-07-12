import streamlit as st
import pandas as pd
import random
from cashier.cashier_handler import CashierHandler
import psycopg2

cashier_handler = CashierHandler()

@st.cache_data(ttl=600)
def get_item_catalogue():
    return cashier_handler.fetch_data("""
        SELECT itemid, itemnameenglish AS itemname, sellingprice
        FROM item
        WHERE sellingprice IS NOT NULL AND sellingprice > 0
    """)

def random_cart(cat_df, min_items=2, max_items=6, min_qty=1, max_qty=5):
    n_items = random.randint(min_items, min(max_items, len(cat_df)))
    picks = cat_df.sample(n=n_items, replace=False)
    cart = []
    for _, row in picks.iterrows():
        qty = random.randint(min_qty, max_qty)
        cart.append({
            "itemid": int(row.itemid),
            "quantity": qty,
            "sellingprice": float(row.sellingprice)
        })
    return cart

def display_bulk_test():
    st.title("🧪 Bulk POS Sale Simulation Test (Handles Errors, No Table Edits)")
    st.info("Simulate many sales using the POS logic. Any DB errors (including PK/unique issues) are reported and skipped.")

    cat_df = get_item_catalogue()
    total_items = len(cat_df)
    if total_items < 2:
        st.warning("Not enough items in the catalogue for bulk testing.")
        return

    num_sales = st.number_input("How many test sales (transactions)?", 1, 100, 10)
    min_items = st.number_input("Min items per sale", 1, 10, 2)
    max_items = st.number_input("Max items per sale", min_items, 20, 5)
    min_qty   = st.number_input("Min quantity per item", 1, 20, 1)
    max_qty   = st.number_input("Max quantity per item", min_qty, 50, 3)
    pay_method = st.selectbox("Payment Method", ["Cash", "Card"])
    note = st.text_input("Bulk Test Note (optional)", "")

    if st.button(f"Run Bulk Test ({num_sales} sales)"):
        results = []
        with st.spinner("Running bulk test..."):
            for i in range(int(num_sales)):
                cart = random_cart(cat_df, min_items, max_items, min_qty, max_qty)
                try:
                    saleid, shortages = cashier_handler.process_sale_with_shortage(
                        cart_items=cart,
                        discount_rate=0.0,
                        payment_method=pay_method,
                        cashier="BULKTEST",
                        notes=f"[BULK TEST] {note}".strip()
                    )
                    if saleid:
                        msg = f"✅ Sale #{saleid} OK"
                    else:
                        msg = f"❌ Sale failed"
                except psycopg2.errors.UniqueViolation as e:
                    msg = f"❌ UniqueViolation (skipped): {e}"
                    saleid = None
                    shortages = []
                except Exception as e:
                    msg = f"❌ DB error: {e}"
                    saleid = None
                    shortages = []
                results.append({
                    "sale": i + 1,
                    "sale_id": saleid,
                    "result": msg,
                    "shortages": shortages
                })

        st.success(f"Bulk test complete! {len(results)} sales processed.")
        df = pd.DataFrame(results)
        st.dataframe(df)

if __name__ == "__main__":
    display_bulk_test()
