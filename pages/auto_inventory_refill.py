import streamlit as st
if not st.session_state.get("sim_active", True):
    st.stop()          # abort the page run immediately


# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Monitor (supplier auto‑lookup)
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
    out = []
    for _, row in df_need.iterrows():
        need = int(row.inventoryaverage) - int(row.totalqty)
        status = irh.restock_item(int(row.itemid), need)
        out.append(
            {"item": row.itemnameenglish, "added": need, "status": status}
        )
    return pd.DataFrame(out)

# ───────── throttling ─────────
NOW = time.time()
if "last_refill" not in st.session_state:
    st.session_state["last_refill"] = 0.0
ALLOW = NOW - st.session_state["last_refill"] > 10  # seconds

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

    if ALLOW:
        with st.spinner("Restocking…"):
            acts = restock(below)
        st.session_state["last_refill"] = NOW
        st.success("Refill cycle complete.")
        st.dataframe(acts, use_container_width=True)
        df = snapshot()
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
