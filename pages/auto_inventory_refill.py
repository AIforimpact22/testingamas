from __future__ import annotations
"""
Auto‑Inventory Refill Monitor
─────────────────────────────
• Checks inventory every 15 s.
• If total stock < `threshold`, it creates a synthetic PO (supplier is taken
  automatically from the itemsupplier table) and tops the item up to
  `averagerequired`.
"""

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
    actions = []
    for _, row in df_need.iterrows():
        need = int(row.inventoryaverage) - int(row.totalqty)
        status = irh.restock_item(int(row.itemid), need)
        actions.append(
            {"item": row.itemnameenglish, "added": need, "status": status}
        )
    return pd.DataFrame(actions)

# ───────── throttling (10 s) ─────────
now = time.time()
if "last_refill" not in st.session_state:
    st.session_state["last_refill"] = 0.0
allow_refill = now - st.session_state["last_refill"] > 10  # seconds

# ───────── simulation master switch ─────────
if not st.session_state.get("sim_active", True):
    st.warning("Simulators are paused (toggle in main sidebar).")
    st.stop()

# ───────── UI ─────────
st.set_page_config("Auto‑Inventory Refill", "📦")
st.title("📦 Auto‑Inventory Refill Monitor")

df = snapshot()
below = df[df.totalqty < df.inventorythreshold]

c1, c2 = st.columns(2)
c1.metric("Total SKUs", len(df))
c2.metric("Below threshold", len(below))

if not below.empty:
    st.dataframe(
        below[["itemnameenglish", "totalqty",
               "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )

    if allow_refill:
        with st.spinner("Restocking…"):
            acts = restock(below)
        st.session_state["last_refill"] = now
        st.success("Refill cycle complete.")
        st.dataframe(acts, use_container_width=True)
        df = snapshot()           # refreshed view
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

# auto‑refresh every 15 s
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=15000, key="inv_refresh")
