# pages/inventory.py
"""
📦 Inventory Auto‑Refill
────────────────────────
Press **Start** and the loop keeps warehouse stock ≥ `threshold`,
topping to `averagerequired` at the chosen interval.

• Unit‑cost = 75 % of selling price.
• One PO per supplier per cycle (bulk, fast).
"""

from __future__ import annotations
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from handler.inventory_handler import InventoryHandler   # ← updated path

# ────────── page config ──────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# interval controls
unit   = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value  = st.sidebar.number_input("Every …", 1, step=1, value=30)
INTSEC = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

# start / stop
RUN = st.session_state.get("inv_running", False)
if st.button("▶ Start", disabled=RUN):
    st.session_state.update(
        inv_running=True,
        last_inv_check=time.time() - INTSEC,
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUN = True
if st.button("⏹ Stop", disabled=not RUN):
    st.session_state["inv_running"] = False
    RUN = False

inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def run_cycle() -> list[dict]:
    snap  = snapshot()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return []

    below["need"] = below["average"] - below["totalqty"]
    return inv.restock_items_bulk(below[["itemid", "need", "sellingprice"]])

# loop
if RUN:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTSEC:
        st.session_state["inv_last_result"] = run_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    st.metric("Cycles", st.session_state["inv_cycle_count"])
    st.metric(
        "Last cycle", datetime.fromtimestamp(st.session_state["last_inv_check"]).strftime("%F %T")
    )
    st.metric("Rows added", len(st.session_state["inv_last_result"]))

    time.sleep(0.3)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
