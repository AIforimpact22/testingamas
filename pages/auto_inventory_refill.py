# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Monitor
─────────────────────────────
Polls inventory every 5 seconds; when a product’s total stock
falls below its `inventorythreshold`, it automatically creates
a synthetic PO and inserts inventory to reach `inventoryaverage`.
"""

from __future__ import annotations
import streamlit as st
import pandas as pd

from handler.inventory_refill_handler import InventoryRefillHandler

irh = InventoryRefillHandler()

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def stock_snapshot() -> pd.DataFrame:
    """Return inventory snapshot with thresholds / averages."""
    return irh._stock_levels()

def refill_needed(df_need: pd.DataFrame, supplier_id: int) -> pd.DataFrame:
    actions = []
    for _, row in df_need.iterrows():
        need = int(row.inventoryaverage) - int(row.totalqty)
        poid = irh.restock_item(int(row.itemid), supplier_id, need)
        actions.append({
            "item"     : row.itemnameenglish,
            "added"    : need,
            "poid"     : poid,
        })
    return pd.DataFrame(actions)

# ───────────────────────── UI / loop ───────────────────────
st.set_page_config(page_title="Auto‑Inventory Refill", page_icon="📦",
                   layout="wide")
st.title("📦 Auto‑Inventory Refill Monitor")

# Supplier picker for synthetic POs
suppliers = irh.get_suppliers()
if suppliers.empty:
    st.error("No suppliers found – add at least one supplier first.")
    st.stop()

supplier_map  = dict(zip(suppliers.suppliername, suppliers.supplierid))
supplier_name = st.selectbox("Supplier used for auto‑refills",
                             list(supplier_map.keys()), index=0)
supplier_id = supplier_map[supplier_name]

# Auto‑refresh every 5 s (Streamlit ≥ 1.33)
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=5000, key="inv_refill_refresh")

snapshot_df = stock_snapshot()
need_df     = snapshot_df[snapshot_df.totalqty < snapshot_df.inventorythreshold]

col1, col2 = st.columns(2)
col1.metric("Total SKUs", len(snapshot_df))
col2.metric("Below threshold", len(need_df))

if not need_df.empty:
    st.subheader("🛠 Triggering auto‑refill")
    st.dataframe(
        need_df[["itemnameenglish", "totalqty",
                 "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )

    actions_df = refill_needed(need_df, supplier_id)
    st.success(f"{len(actions_df)} item(s) restocked.")
    st.dataframe(actions_df, use_container_width=True)
else:
    st.info("All items are above their inventory thresholds.")

st.subheader("Current inventory snapshot")
st.dataframe(
    snapshot_df[["itemnameenglish", "totalqty",
                 "inventorythreshold", "inventoryaverage"]]
        .sort_values("itemnameenglish"),
    use_container_width=True,
)
