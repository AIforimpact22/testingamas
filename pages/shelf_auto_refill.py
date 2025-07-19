from __future__ import annotations
"""
Shelf Auto‑Refill Simulator
──────────────────────────
• Generates random POS sales (unique items, random qty)
• Executes each sale via CashierHandler.process_sale_with_shortage
• Immediately tops‑up shelf stock from back‑store inventory
  until it reaches `threshold` / `averagerequired` levels.
"""

import streamlit as st
import pandas as pd
import random
from datetime import datetime

from cashier_handler import CashierHandler
from shelf_handler   import ShelfHandler

# ────────────────── stop if simulators are paused ──────────────────
if not st.session_state.get("sim_active", True):
    st.warning("Simulators are paused (toggle in main sidebar).")
    st.stop()

# ───────────────────────── connections ────────────────────────────
cashier = CashierHandler()
shelf   = ShelfHandler()

# ───────────────────────── cached master data ─────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def get_item_meta() -> pd.DataFrame:
    return shelf.get_all_items().set_index("itemid")   # threshold / average

ITEM_META = get_item_meta()

# ───────────────────────── cart generator ─────────────────────────
def random_cart(cat_df: pd.DataFrame,
                min_items: int, max_items: int,
                min_qty  : int, max_qty  : int) -> list[dict]:
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

# ───────────────────────── inventory helpers ──────────────────────
def _inventory_layers(itemid: int) -> pd.DataFrame:
    return shelf.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
        FROM   inventory
        WHERE  itemid = %s AND quantity > 0
        ORDER  BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )

def _choose_layers(itemid: int, need_qty: int) -> list[dict]:
    layers, remain = [], need_qty
    for row in _inventory_layers(itemid).itertuples():
        take = min(remain, int(row.quantity))
        layers.append({
            "expirationdate": row.expirationdate,
            "qty"           : take,
            "cost"          : float(row.cost_per_unit),
        })
        remain -= take
        if remain == 0:
            break
    return layers

# ───────────────────────── refill logic ───────────────────────────
def restock_item(itemid: int, *, user="AUTOSIM") -> None:
    kpis = shelf.get_shelf_quantity_by_item()
    row  = kpis.loc[kpis.itemid == itemid]

    current   = int(row.totalquantity.iloc[0]) if not row.empty else 0
    threshold = int(ITEM_META.at[itemid, "shelfthreshold"] or 0)
    target    = int(ITEM_META.at[itemid, "shelfaverage"]   or threshold or 0)

    if current >= threshold:
        return                                    # nothing to do

    need = max(target - current, threshold - current)
    if need <= 0:
        return

    # 1) resolve open shortages
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)
    if need <= 0:
        return

    # 2) move layers from inventory → shelf
    for layer in _choose_layers(itemid, need):
        shelf.transfer_from_inventory(
            itemid        = itemid,
            expirationdate= layer["expirationdate"],
            quantity      = layer["qty"],
            cost_per_unit = layer["cost"],
            created_by    = user,
        )
        need -= layer["qty"]
        if need <= 0:
            break

    # 3) log shortage if still unmet
    if need > 0:
        shelf.execute_command(
            """
            INSERT INTO shelf_shortage (itemid, shortage_qty, logged_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            """,
            (itemid, need),
        )

def post_sale_restock(cart: list[dict], *, user="AUTOSIM") -> None:
    for entry in cart:
        restock_item(int(entry["itemid"]), user=user)

# ───────────────────────── simulation core ────────────────────────
def simulate_sales(num_sales: int,
                   min_items: int, max_items: int,
                   min_qty : int, max_qty : int,
                   pay_method: str, user_tag: str) -> pd.DataFrame:

    cat_df = cashier.fetch_data("""
        SELECT itemid, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
    """)
    if cat_df.empty:
        st.error("Catalogue empty – cannot simulate.")
        return pd.DataFrame()

    results = []
    for n in range(num_sales):
        cart = random_cart(cat_df, min_items, max_items, min_qty, max_qty)
        try:
            saleid, shortages = cashier.process_sale_with_shortage(
                cart_items     = cart,
                discount_rate  = 0.0,
                payment_method = pay_method,
                cashier        = user_tag,
                notes          = f"[AUTO SIM {datetime.utcnow():%Y-%m-%d}]",
            )
            status = "OK" if saleid else "FAIL"
        except Exception as e:
            cashier.conn.rollback()
            saleid, status, shortages = None, f"ERROR: {e}", []
        # refill after successful sale
        if saleid:
            post_sale_restock(cart, user=user_tag)

        results.append({
            "sale_no"  : n + 1,
            "sale_id"  : saleid,
            "items"    : len(cart),
            "status"   : status,
            "shortages": shortages,
        })
    return pd.DataFrame(results)

# ───────────────────────── Streamlit UI ───────────────────────────
st.set_page_config(page_title="Shelf Auto‑Refill Simulator", page_icon="🛒")

st.title("🛒 Shelf Auto‑Refill Simulator")

with st.sidebar:
    st.header("Simulation parameters")
    num_sales  = st.number_input("Sales to simulate", 1, 500, 50, 1)
    min_items  = st.number_input("Min items / sale", 1, 20, 2)
    max_items  = st.number_input("Max items / sale", min_items, 30, 6)
    min_qty    = st.number_input("Min qty / item", 1, 20, 1)
    max_qty    = st.number_input("Max qty / item", min_qty, 50, 5)
    pay_method = st.selectbox("Payment method", ["Cash", "Card"])
    user_tag   = st.text_input("Cashier tag", "AUTOSIM")

if st.button("Run simulation"):
    with st.spinner("Simulating…"):
        df = simulate_sales(
            num_sales, min_items, max_items,
            min_qty,   max_qty,
            pay_method, user_tag,
        )
    if not df.empty:
        st.success(f"Finished.  **{(df.status == 'OK').sum()} / {len(df)}** sales succeeded.")
        st.dataframe(df)
