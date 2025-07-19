# pages/auto_inventory_refill.py
from __future__ import annotations
import time
import streamlit as st
import pandas as pd

from handler.inventory_refill_handler import InventoryRefillHandler

irh = InventoryRefillHandler()

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return irh._stock_levels()

def restock(df_need: pd.DataFrame, supplier_id: int) -> pd.DataFrame:
    acts = []
    for _, row in df_need.iterrows():
        need = int(row.inventoryaverage) - int(row.totalqty)
        poid = irh.restock_item(int(row.itemid), supplier_id, need)
        acts.append({"item": row.itemnameenglish, "added": need, "poid": poid})
    return pd.DataFrame(acts)

# ───────────────────────── UI / loop ───────────────────────
st.set_page_config(page_title="Auto‑Inventory Refill", page_icon="📦")
st.title("📦 Auto‑Inventory Refill Monitor")

suppliers = irh.get_suppliers()
if suppliers.empty:
    st.error("No suppliers found – add at least one.")
    st.stop()

supplier_map = dict(zip(suppliers.suppliername, suppliers.supplierid))
supplier_name = st.selectbox("Supplier for auto‑refills",
                             list(supplier_map.keys()), index=0)
supplier_id = supplier_map[supplier_name]

# Throttle – run refills at most once every 10 s
NOW = time.time()
if "last_refill_ts" not in st.session_state:
    st.session_state["last_refill_ts"] = 0.0
ALLOW_REFILL = NOW - st.session_state["last_refill_ts"] > 10

df = snapshot()
below = df[df.totalqty < df.inventorythreshold]

col1, col2 = st.columns(2)
col1.metric("Total SKUs", len(df))
col2.metric("Below threshold", len(below))

if below.empty:
    st.info("All items meet threshold.")
else:
    st.dataframe(
        below[["itemnameenglish", "totalqty",
               "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )

    if ALLOW_REFILL:
        with st.spinner("Restocking..."):
            acts = restock(below, supplier_id)
        st.session_state["last_refill_ts"] = NOW
        st.success(f"{len(acts)} item(s) restocked.")
        st.dataframe(acts, use_container_width=True)
        # show updated snapshot without waiting for next autorefresh
        df = snapshot()
        below = df[df.totalqty < df.inventorythreshold]

# fresh snapshot display
st.subheader("Current inventory snapshot")
st.dataframe(
    df[["itemnameenglish", "totalqty",
        "inventorythreshold", "inventoryaverage"]]
      .sort_values("itemnameenglish"),
    use_container_width=True,
)

# gentle auto‑refresh every 15 s
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=15000, key="inv_refresh")
