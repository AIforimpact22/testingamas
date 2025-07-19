# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Monitor
─────────────────────────────
Runs every 15 seconds.  When warehouse stock for an item is below its
`shelfthreshold` it creates a synthetic PO (supplier fetched from
itemsupplier) and tops the item up to `shelfaverage`.
"""

from __future__ import annotations
import time
import streamlit as st
import pandas as pd

from handler.inventory_refill_handler import InventoryRefillHandler

irh = InventoryRefillHandler()

# ───────── helpers ─────────
@st.cache_data(ttl=5, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return irh._stock_levels()

def restock(df_need: pd.DataFrame) -> pd.DataFrame:
    acts = []
    for _, row in df_need.iterrows():
        need = int(row.inventoryaverage) - int(row.totalqty)
        poid = irh.restock_item(int(row.itemid), need)
        acts.append(
            {
                "item": row.itemnameenglish,
                "added": need,
                "poid": poid if poid is not None else "No supplier mapping",
            }
        )
    return pd.DataFrame(acts)

# ───────── throttling to avoid rapid loops ─────────
NOW = time.time()
if "last_refill_ts" not in st.session_state:
    st.session_state["last_refill_ts"] = 0.0
ALLOW_REFILL = NOW - st.session_state["last_refill_ts"] > 10  # seconds

# ───────── UI ─────────
st.set_page_config("Auto‑Inventory Refill", "📦")
st.title("📦 Auto‑Inventory Refill Monitor")

df = snapshot()
below = df[df.totalqty < df.inventorythreshold]

col1, col2 = st.columns(2)
col1.metric("Total SKUs", len(df))
col2.metric("Below threshold", len(below))

if not below.empty:
    st.dataframe(
        below[["itemnameenglish", "totalqty",
               "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )
    if ALLOW_REFILL:
        with st.spinner("Restocking..."):
            acts = restock(below)
        st.session_state["last_refill_ts"] = NOW
        st.success(f"{len(acts)} item(s) processed.")
        st.dataframe(acts, use_container_width=True)
        df = snapshot()  # immediate refreshed view
        below = df[df.totalqty < df.inventorythreshold]
else:
    st.info("All items meet threshold.")

st.subheader("Current inventory snapshot")
st.dataframe(
    df[["itemnameenglish", "totalqty",
        "inventorythreshold", "inventoryaverage"]]
      .sort_values("itemnameenglish"),
    use_container_width=True,
)

# Auto‑refresh every 15 s
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=15000, key="inv_refresh")
