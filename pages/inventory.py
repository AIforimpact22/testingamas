# pages/inventory.py
"""
📦 Inventory Auto‑Refill
────────────────────────
Press **Start** and the background loop will keep warehouse stock
≥ `threshold`, topping up to `averagerequired` at the chosen interval.

• Unit‑cost = 75 % of current `sellingprice` (0 if price = 0/NULL).
• One synthetic PO **per supplier per cycle** (fast).
"""

from __future__ import annotations
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from handler.inventory_handler import InventoryHandler

# ────────── page config ──────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ────────── sidebar: interval controls ──────────
unit = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", min_value=1, step=1, value=30)

INTERVAL_SEC = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

# ────────── Start / Stop buttons ──────────
RUNNING = st.session_state.get("inv_running", False)
if st.button("▶ Start", disabled=RUNNING):
    st.session_state.update(
        inv_running=True,
        last_inv_check=time.time() - INTERVAL_SEC,  # fire instantly
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUNNING = True

if st.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["inv_running"] = False
    RUNNING = False

# ────────── helpers ──────────
inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    """Inventory snapshot incl. thresholds & averages."""
    return inv.stock_levels()

def run_cycle() -> list[dict]:
    snap = snapshot()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return []

    below["need"] = below["average"] - below["totalqty"]
    log = inv.restock_items_bulk(below[["itemid", "need", "sellingprice"]])
    return log

# ────────── main loop ──────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTERVAL_SEC:
        st.session_state["inv_last_result"] = run_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    # live metrics
    st.metric("Cycles run", st.session_state["inv_cycle_count"])
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["last_inv_check"]).strftime("%F %T"),
    )
    st.metric("Rows added last cycle", len(st.session_state["inv_last_result"]))

    time.sleep(0.3)   # gentle yield to avoid tight loop
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
