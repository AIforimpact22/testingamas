import streamlit as st
import pandas as pd
from datetime import datetime

from db_handler import DatabaseManager  # Adjust this import to your path

# --- Initialize DB connection
db = DatabaseManager()

# --- Helpers: catalogue
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

def build_lookup(cat_df: pd.DataFrame):
    idx = {}
    for i, row in cat_df.iterrows():
        for c in ("barcode", "packetbarcode", "cartonbarcode"):
            if row[c]:
                idx[row[c]] = i
    return idx, cat_df.itemname.str.lower()

def resolve_scan(row, scanned: str):
    if scanned == row.packetbarcode:
        return row.packetsize or 1, "(packet)"
    if scanned == row.cartonbarcode:
        return row.cartonsize or 1, "(carton)"
    return 1, ""

def fetch_item(cat_df, idx, names, key: str, qty_in: int):
    key = key.strip()
    if key in idx:
        row = cat_df.loc[idx[key]]
        mult, lab = resolve_scan(row, key)
    else:
        m = cat_df[names.str.contains(key.lower())]
        if m.empty:
            return None
        row, mult, lab = m.iloc[0], 1, ""
    qty = qty_in * mult
    price = float(row.sellingprice or 0.0)
    return {
        "barcode":  key,
        "itemid":   int(row.itemid),
        "itemname": f"{row.itemname} {lab}",
        "quantity": qty,
        "price":    price,
        "total":    price * qty,
    }

def clear_test_bill():
    st.session_state.test_sales_table = pd.DataFrame(
        columns=["barcode", "itemid", "itemname", "quantity", "price", "total"]
    )

# --- Sale simulation logic
def simulate_sale(cart_items, disc_rate, subtotal, disc_amt, final_amt, note, payment_method):
    # Simulate sale as inserting into a `sale` table (or use your actual POS logic!)
    # We'll log test sales using a special note, and update inventory if needed.
    # You should adapt this to your actual sale logic.
    now = datetime.utcnow()
    try:
        # 1. Insert into `sale` table
        sale_sql = """
            INSERT INTO sale (timestamp, items, discount_rate, subtotal, discount_amount, total, note, payment_method)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING saleid
        """
        # You may need to adjust the table/columns as per your DB schema.
        items_json = str(cart_items)  # or json.dumps(cart_items) if your column is JSONB
        res = db.execute_command_returning(
            sale_sql,
            (now, items_json, disc_rate, subtotal, disc_amt, final_amt, "[SIM TEST] " + note, payment_method)
        )
        sale_id = res[0] if res else None

        # 2. Decrement inventory for each item (adapt this to your logic if different)
        shortage_msgs = []
        for itm in cart_items:
            # You should use transactions to ensure atomicity in production!
            update_sql = """
                UPDATE inventory
                SET quantity = quantity - %s
                WHERE itemid = %s AND quantity >= %s
            """
            db.execute_command(update_sql, (itm['quantity'], itm['itemid'], itm['quantity']))
            # Optionally, check for shortages or stockout (not covered here)

        return sale_id, shortage_msgs
    except Exception as e:
        st.error(f"Database error: {e}")
        return None, []

# --- Streamlit UI
def display_test_simulator():
    st.title("🧪 POS Sale Simulation Test")
    st.info("Simulate cashier sales by building a test bill, then run it as a real sale using your DB backend.")

    # Load catalogue and build lookup
    cat_df   = get_item_catalogue()
    bc_idx, name_series = build_lookup(cat_df)

    # Bill storage
    if "test_sales_table" not in st.session_state:
        clear_test_bill()

    # --- Build the test bill
    with st.form("test_add_item_form", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        txt = c1.text_input("Barcode or Item Name")
        qty = c2.number_input("Qty", 1, value=1)
        if st.form_submit_button("➕ Add Test Item"):
            itm = fetch_item(cat_df, bc_idx, name_series, txt, int(qty))
            if itm is None:
                st.warning("No matching item.")
            else:
                st.session_state.test_sales_table = pd.concat(
                    [st.session_state.test_sales_table, pd.DataFrame([itm])],
                    ignore_index=True
                )
                st.success(f"Added {itm['itemname']} ×{itm['quantity']}")

    st.markdown("### 🧾 Test Bill")
    df = st.session_state.test_sales_table
    if df.empty:
        st.info("Test bill is empty.")
    else:
        df["total"] = df["quantity"] * df["price"]
        st.dataframe(df[["itemname", "quantity", "price", "total"]],
                     hide_index=True, use_container_width=True)
        for idx, row in df.iterrows():
            cols = st.columns([7, 2, 1])
            cols[0].markdown(f"**{row.itemname}**")
            new_q = cols[1].number_input("",
                                         min_value=1,
                                         value=int(row.quantity),
                                         key=f"test_qty_{idx}",
                                         label_visibility="collapsed")
            if new_q != row.quantity:
                df.at[idx, "quantity"] = new_q
                st.session_state.test_sales_table = df
                st.rerun()
            if cols[2].button("🗑️ Remove", key=f"test_rm_{idx}"):
                st.session_state.test_sales_table = (
                    df.drop(idx).reset_index(drop=True)
                )
                st.rerun()

    # --- Totals & simulate
    subtotal  = float(df["total"].sum()) if not df.empty else 0.0
    disc_rate = st.number_input(
        "Discount (%)",
        min_value=0.0, max_value=100.0,
        step=0.5,
        value=st.session_state.get("test_discount_rate", 0.0),
        key="test_discount_rate",
    )
    disc_amt  = round(subtotal * disc_rate / 100, 2)
    final_amt = round(subtotal - disc_amt, 2)
    st.markdown(f"**Subtotal:** {subtotal:.2f}")
    st.markdown(f"**Discount ({disc_rate:.1f}%):** {disc_amt:.2f}")
    st.markdown(f"**Final Amount:** {final_amt:.2f}")

    note = st.text_input("Simulation Note (optional)", key="sim_note")

    c1, c2, c3 = st.columns(3)
    if c1.button("Simulate Sale (Cash)"):
        cart_items = [
            {"itemid": int(r.itemid), "quantity": int(r.quantity), "sellingprice": float(r.price)}
            for _, r in df.iterrows()
        ]
        sale_id, shortages = simulate_sale(cart_items, disc_rate, subtotal, disc_amt, final_amt, note, "Cash")
        if sale_id:
            st.success(f"✅ Simulated sale completed! ID {sale_id}")
    if c2.button("Simulate Sale (Card)"):
        cart_items = [
            {"itemid": int(r.itemid), "quantity": int(r.quantity), "sellingprice": float(r.price)}
            for _, r in df.iterrows()
        ]
        sale_id, shortages = simulate_sale(cart_items, disc_rate, subtotal, disc_amt, final_amt, note, "Card")
        if sale_id:
            st.success(f"✅ Simulated sale completed! ID {sale_id}")
    if c3.button("Clear Test Bill"):
        clear_test_bill()

if __name__ == "__main__":
    display_test_simulator()
