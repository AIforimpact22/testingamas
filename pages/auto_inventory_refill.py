# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Monitor
─────────────────────────────
• Polls warehouse inventory every 5 seconds.
• When an SKU’s on‑hand stock < `inventorythreshold`
  it calls InventoryRefillHandler to create a synthetic PO and
  insert stock up to `inventoryaverage`.
"""

from __future__ import annotations
import streamlit as st
import pandas as pd

from handler.inventory_refill_handler import InventoryRefillHandler

irh = InventoryRefillHandler()

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def stock_snapshot() -> pd.DataFrame:
    """Current inventory merged with item‑level thresholds / averages."""
    return irh._stock_levels()            # private helper is OK here


def refill_needed_items(df_need: pd.DataFrame, supplier_id: int) -> pd.DataFrame:
    """Restock each row in *df_need*.  Return a summary DataFrame."""
    actions = []
    for _, row in df_need.iterrows():
        need_units = int(row.inventoryaverage) - int(row.totalqty)
        poid = irh.restock_item(
            int(row.itemid), supplier_id, need_units
        )
        actions.append({
            "item"      : row.itemnameenglish,
            "added"     : need_units,
            "new_poid"  : poid,
        })
    return pd.DataFrame(actions)


# ───────────────────────── UI / loop ───────────────────────
st.set_page_config(page_title="Auto‑Inventory Refill", page_icon="📦",
                   layout="wide")
st.title("📦 Auto‑Inventory Refill Monitor")

# choose supplier for auto POs
suppliers = irh.get_suppliers()
if suppliers.empty:
    st.error("No suppliers found – add at least one supplier first.")
    st.stop()

supplier_map  = dict(zip(suppliers.suppliername, suppliers.supplierid))
supplier_name = st.selectbox("Supplier used for auto‑refills",
                             list(supplier_map.keys()), index=0)
supplier_id = supplier_map[supplier_name]

# auto‑refresh (Streamlit ≥ 1.33)
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=5000, key="inv_refill_refresh")

snapshot_df = stock_snapshot()
need_df     = snapshot_df[snapshot_df.totalqty < snapshot_df.inventorythreshold]

# high‑level metrics
col1, col2 = st.columns(2)
col1.metric("Total SKUs", len(snapshot_df))
col2.metric("Below threshold", len(need_df))

# perform refills if needed
if not need_df.empty:
    st.subheader("🛠 Triggering auto‑refill")
    st.dataframe(
        need_df[["itemnameenglish", "totalqty",
                 "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )

    actions_df = refill_needed_items(need_df, supplier_id)
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
