import streamlit as st
import pandas as pd
import random
from datetime import datetime
from db_handler import DatabaseManager  # Adjust this path to your handler

db = DatabaseManager()

@st.cache_data(ttl=600)
def get_item_catalogue():
    return db.fetch_data("""
        SELECT itemid, itemnameenglish AS itemname, sellingprice,
               COALESCE(barcode,'') AS barcode,
               COALESCE(packetbarcode,'') AS packetbarcode,
               COALESCE(cartonbarcode,'') AS cartonbarcode,
               packetsize, cartonsize
        FROM item
    """)

def random_cart(cat_df, min_items=2, max_items=6, min_qty=1, max_qty=5):
    n_items = random.randint(min_items, max_items)
    picks = cat_df.sample(n=min(n_items, len(cat_df)), replace=False)
    cart = []
    for _, row in picks.iterrows():
        qty = random.randint(min_qty, max_qty)
        cart.append({
            "itemid": int(row.itemid),
            "itemname": row.itemname,
            "quantity": qty,
            "sellingprice": float(row.sellingprice or 0.0)
        })
    return cart

def simulate_bulk_sale(cart_items, note, payment_method):
    now = datetime.utcnow()
    subtotal = sum(i["quantity"] * i["sellingprice"] for i in cart_items)
    disc_rate = 0.0  # No discount for bulk test, change if you want randomness
    disc_amt = 0.0
    final_amt = subtotal - disc_amt
    try:
        # Insert into sale table (adjust as needed)
        sale_sql = """
            INSERT INTO sale (timestamp, items, discount_rate, subtotal, discount_amount, total, note, payment_method)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING saleid
        """
        items_json = str(cart_items)  # or json.dumps(cart_items)
        res = db.execute_command_returning(
            sale_sql,
            (now, items_json, disc_rate, subtotal, disc_amt, final_amt, "[BULK TEST] " + note, payment_method)
        )
        sale_id = res[0] if res else None

        # Decrement inventory for each item (same logic as in a real sale)
        shortage_msgs = []
        for itm in cart_items:
            update_sql = """
                UPDATE inventory
                SET quantity = quantity - %s
                WHERE itemid = %s AND quantity >= %s
            """
            db.execute_command(update_sql, (itm['quantity'], itm['itemid'], itm['quantity']))
            # Optional: check for out-of-stock, log shortages if needed

        return sale_id, shortage_msgs
    except Exception as e:
        return None, [f"DB error: {e}"]

def display_bulk_test():
    st.title("🧪 Bulk POS Sale Simulation Test")
    st.info("Generate and process random test sales in bulk. Every sale updates the database like a real cashier transaction.")

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
                sale_id, shortages = simulate_bulk_sale(cart, note, pay_method)
                if sale_id:
                    msg = f"✅ Sale #{sale_id} OK"
                else:
                    msg = f"❌ Sale failed: {shortages}"
                results.append({"sale": i + 1, "sale_id": sale_id, "result": msg, "shortages": shortages})

        st.success(f"Bulk test complete! {len(results)} sales processed.")
        st.dataframe(pd.DataFrame(results))

        # Optionally: show details of each sale, or log for audit

if __name__ == "__main__":
    display_bulk_test()
