# ────────────────────────────────────────────────────────────────
# pages/🛒 Shelf Auto Refill.py
# Streamlit page name will appear as  🛒 Shelf Auto Refill
# ────────────────────────────────────────────────────────────────
import random
from datetime import datetime

import pandas as pd
import streamlit as st

# <<< ADJUST THESE TWO LINES IF YOUR FILE LOCATIONS DIFFER >>>
from cashier_handler import CashierHandler     # root‑level file
from shelf_handler   import ShelfHandler       # root‑level file
# <<< ------------------------------------------------------- >>>

# ────────── initialise singletons ──────────
cashier = CashierHandler()
shelf   = ShelfHandler()

# ────────── page config ──────────
st.set_page_config(page_title="Shelf Auto Refill", page_icon="🛒")

st.title("🛒 Shelf Auto Refill – POS + Instant Top‑Up Simulator")

# ────────── UI inputs ──────────
with st.sidebar:
    st.header("Simulation Parameters")
    num_sales = st.number_input("Number of synthetic sales", 1, 500, 50)
    min_items = st.number_input("Min items per sale", 1, 20, 2)
    max_items = st.number_input("Max items per sale", min_items, 30, 6)
    min_qty   = st.number_input("Min quantity per item", 1, 20, 1)
    max_qty   = st.number_input("Max quantity per item", min_qty, 50, 5)
    pay_method= st.selectbox("Payment method", ["Cash", "Card", "Mobile"])
    run_btn   = st.button(f"Run simulation")

# ────────── helper functions ──────────
def random_cart(cat_df: pd.DataFrame) -> list[dict]:
    n_items = random.randint(min_items, min(max_items, len(cat_df)))
    picks   = cat_df.sample(n=n_items, replace=False)
    return [
        {
            "itemid":       int(r.itemid),
            "quantity":     random.randint(min_qty, max_qty),
            "sellingprice": float(r.sellingprice),
        }
        for _, r in picks.iterrows()
    ]

def choose_inventory_layers(itemid: int, need_qty: int) -> list[dict]:
    layers = shelf.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
        FROM   inventory
        WHERE  itemid=%s AND quantity>0
        ORDER  BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )
    plan, remain = [], need_qty
    for row in layers.itertuples():
        take = min(remain, int(row.quantity))
        plan.append(
            {"exp": row.expirationdate, "qty": take, "cost": float(row.cost_per_unit)}
        )
        remain -= take
        if remain == 0:
            break
    return plan

def restock_item(itemid: int, user: str = "AUTOSIM"):
    kpis = shelf.get_shelf_quantity_by_item()
    row  = kpis[kpis.itemid == itemid]
    cur  = int(row.totalquantity.iloc[0]) if not row.empty else 0
    thr  = int(row.shelfthreshold.iloc[0] or 0)
    tgt  = int(row.shelfaverage.iloc[0]   or thr)

    if cur >= thr:
        return  # already OK

    need = max(tgt - cur, 0) or (thr - cur)
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)

    for layer in choose_inventory_layers(itemid, need):
        shelf.transfer_from_inventory(
            itemid        = itemid,
            expirationdate= layer["exp"],
            quantity      = layer["qty"],
            cost_per_unit = layer["cost"],
            created_by    = user,
        )
        need -= layer["qty"]
        if need <= 0:
            break

    if need > 0:  # back‑store empty, leave shortage ticket
        shelf.execute_command(
            "INSERT INTO shelf_shortage (itemid, shortage_qty, logged_at) VALUES (%s,%s,CURRENT_TIMESTAMP)",
            (itemid, need),
        )

# ────────── main simulation ──────────
def run_sim():
    cat_df = cashier.fetch_data(
        "SELECT itemid, sellingprice FROM item WHERE sellingprice IS NOT NULL AND sellingprice>0"
    )
    if cat_df.empty:
        st.error("Catalogue is empty.")
        return

    results = []
    with st.spinner("Running…"):
        for n in range(int(num_sales)):
            cart = random_cart(cat_df)
            try:
                saleid, shortages = cashier.process_sale_with_shortage(
                    cart_items     = cart,
                    discount_rate  = 0.0,
                    payment_method = pay_method,
                    cashier        = "AUTOSIM",
                    notes          = f"[AUTO SIM {datetime.utcnow():%Y-%m-%d}]",
                )
                status = "✅" if saleid else "❌"
            except Exception as e:
                cashier.conn.rollback()
                saleid, status, shortages = None, f"DB err: {e}", []
            if saleid:
                for item in cart:
                    restock_item(int(item["itemid"]))
            results.append(
                {
                    "Sale #": n + 1,
                    "Sale ID": saleid,
                    "Items": len(cart),
                    "Status": status,
                    "Shortages": shortages,
                }
            )

    df = pd.DataFrame(results)
    ok = (df["Status"] == "✅").sum()
    st.success(f"Finished – {ok}/{len(df)} sales succeeded.")
    st.dataframe(df)

# ────────── run if clicked ──────────
if run_btn:
    run_sim()

