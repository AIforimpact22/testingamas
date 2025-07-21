from __future__ import annotations
"""
Warehouse Inventory Auto‑Refill
───────────────────────────────
Press **Start** to begin automatic top‑ups.  
The loop runs for the chosen interval until you press **Stop**.
"""

import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.inventory_handler import InventoryHandler

# ───────── page config ─────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ───────── interval controls ─────────
st.sidebar.header("Interval")
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours"))
VALUE = st.sidebar.number_input("Every …", min_value=1, step=1, value=10)
MULT  = {"Seconds": 1, "Minutes": 60, "Hours": 3600}[UNIT]
INTERVAL_SEC = VALUE * MULT

# ───────── start / stop buttons ─────────
RUNNING = st.session_state.get("inv_running", False)
b_start = st.button("▶ Start", disabled=RUNNING)
b_stop  = st.button("⏹ Stop",  disabled=not RUNNING)

if b_start:
    st.session_state.update(
        inv_running=True,
        last_inv_check=time.time() - INTERVAL_SEC,   # run immediately
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUNNING = True
elif b_stop:
    st.session_state["inv_running"] = False
    RUNNING = False

# ───────── handlers & cached helpers ─────────
inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    """Inventory snapshot incl. thresholds/averages."""
    return inv.stock_levels()

def run_cycle() -> list[dict]:
    snap = snapshot()
    below = snap[snap.totalqty < snap.threshold]

    actions: list[dict] = []
    for _, r in below.iterrows():
        need = int(r.average_required) - int(r.totalqty)
        status = inv.refill(itemid=int(r.itemid), qty_needed=need)
        actions.append(
            dict(
                item    = r.itemnameenglish,
                before  = int(r.totalqty),
                added   = need,
                result  = status,
            )
        )
    return actions

# ───────── main loop ─────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTERVAL_SEC:
        st.session_state["inv_last_result"] = run_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    st.metric("Cycles run", st.session_state["inv_cycle_count"])
    st.metric(
        "Last cycle at",
        datetime.fromtimestamp(st.session_state["last_inv_check"])
        .strftime("%F %T"),
    )
    st.metric("SKUs processed last", len(st.session_state["inv_last_result"]))

    # gentle yield to avoid tight spin‑loop
    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic warehouse top‑ups.")
