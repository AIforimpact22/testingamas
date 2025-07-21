from __future__ import annotations
"""
Auto‑Inventory Refill (passive)
───────────────────────────────
Every 10 seconds it tops‑up warehouse inventory to an item's
`threshold / average_required` (columns in `item`).

Shortages logged without a sale use saleid = 0.
"""

import streamlit as st
import pandas as pd
from handler.inventory_refill_handler import InventoryRefillHandler
from utils.sim_toggle_persist import sidebar_switch

# ─────── global switch & guard ───────
if not sidebar_switch():
    st.warning("Simulators are paused (use sidebar switch to resume).")
    st.stop()

DUMMY_SALEID = 0           # for system‑generated shortages

irh = InventoryRefillHandler()

@st.cache_data(ttl=10, show_spinner=False)
def stock_levels() -> pd.DataFrame:
    return irh._stock_levels()          # helper returns qty + thresholds

def restock_item(itemid: int, need: int) -> int:
    """Create synthetic PO & add inventory; return new POID."""
    poid = irh.restock_item(itemid, need)          # already zeros cost
    if need > 0:                                   # if still unmet → shortage
        irh.execute_command(
            """
            INSERT INTO shelf_shortage
                  (saleid, itemid, shortage_qty, logged_at)
            VALUES (%s,     %s,     %s,           CURRENT_TIMESTAMP)
            """,
            (DUMMY_SALEID, itemid, need),
        )
    return poid

def auto_cycle() -> pd.DataFrame:
    df = stock_levels()
    below = df[df.totalqty < df.inventorythreshold]
    acts = []
    for _, r in below.iterrows():
        need = int(r.inventoryaverage) - int(r.totalqty)
        poid = restock_item(int(r.itemid), need)
        acts.append(
            dict(
                item    = r.itemnameenglish,
                before  = int(r.totalqty),
                added   = need,
                new_poid= poid,
            )
        )
    return pd.DataFrame(acts)

# ─────────── Streamlit page ───────────
st.set_page_config("Inventory Auto‑Refill", "📦")
st.title("📦 Inventory Auto‑Refill Monitor (passive)")

log_df = auto_cycle()
if log_df.empty:
    st.success("Warehouse inventory is above all thresholds.")
else:
    st.success(f"{len(log_df)} SKU(s) restocked this cycle.")
    st.dataframe(log_df, use_container_width=True)

if hasattr(st, "autorefresh"):
    st.autorefresh(interval=10000, key="inv_refill_refresh")
