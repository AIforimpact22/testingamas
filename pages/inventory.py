from __future__ import annotations
"""
Warehouse Inventory Auto‑Refill
Press **Start** to launch the loop; it keeps running at the selected
interval until **Stop** is pressed.
"""

import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.inventory_handler import InventoryHandler

# ─── UI setup ───
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

st.sidebar.header("Interval")
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VALUE = st.sidebar.number_input("Every …", min_value=1, step=1, value=10)

UNIT_TO_SEC = {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}
INTERVAL_SEC = VALUE * UNIT_TO_SEC[UNIT]

# ─── Start/Stop state ───
RUNNING = st.session_state.get("inv_running", False)

start_btn = st.button("▶ Start", disabled=RUNNING)
stop_btn  = st.button("⏹ Stop",  disabled=not RUNNING)

if start_btn:
    st.session_state.update(
        inv_running=True,
        last_inv_check=0.0,           # force immediate first run
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUNNING = True

if stop_btn:
    st.session_state["inv_running"] = False
    RUNNING = False

# ─── DB helper ───
inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def run_cycle() -> list[dict]:
    snap = snapshot()
    below = snap[snap.totalqty < snap.threshold]

    actions: list[dict] = []
    for _, row in below.iterrows():
        need = int(row.average_required) - int(row.totalqty)
        try:
            poid   = inv.restock_item(int(row.itemid), need)
            result = f"PO #{poid}" if poid else "OK"
        except ValueError as e:
            result = f"ERR: {e}"
        actions.append(
            dict(
                item         = row.itemnameenglish,
                stock_before = int(row.totalqty),
                added        = need,
                result       = result,
            )
        )
    return actions

# ─── main loop ───
if RUNNING:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTERVAL_SEC:
        st.session_state["inv_last_result"] = run_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    st.metric("Cycles run", st.session_state["inv_cycle_count"])
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["last_inv_check"]).strftime("%F %T"),
    )
    st.metric("SKUs processed", len(st.session_state["inv_last_result"]))

    if st.session_state["inv_last_result"]:
        st.dataframe(
            pd.DataFrame(st.session_state["inv_last_result"]),
            use_container_width=True,
        )

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory refills.")
