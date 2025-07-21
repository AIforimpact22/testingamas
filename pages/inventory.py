from __future__ import annotations
"""
Warehouse Inventory Auto‑Refill
──────────────────────────────
Press **Start** → the job runs forever at the chosen interval until you press **Stop**.
An item is topped‑up to `average_required` whenever its total stock falls
below `threshold` (both columns live on table **item**).

• Supplier is read from **itemsupplier**.  
• Each refill creates one synthetic PO (status = Completed, cost = 0).  
• Inventory layer is inserted with expiry 2027‑07‑21, location **A2**.
"""

import time
from datetime import datetime
import streamlit as st
import pandas as pd

from handler.inventory_handler import InventoryHandler

# ─────────── page config ───────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Warehouse Inventory Auto‑Refill")

# ─────────── sidebar – interval picker ───────────
st.sidebar.header("Check interval")
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VALUE = st.sidebar.number_input("Every …", min_value=1, step=1, value=10)

MULTIPLIER = {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}[UNIT]
INTERVAL_SEC = VALUE * MULTIPLIER

# ─────────── Start / Stop controls ───────────
RUNNING = st.session_state.get("inv_running", False)

col_start, col_stop = st.columns(2)
if col_start.button("▶ Start", disabled=RUNNING, use_container_width=True):
    st.session_state.update(
        inv_running=True,
        last_inv_check=time.time() - INTERVAL_SEC,   # force immediate run
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUNNING = True

if col_stop.button("⏹ Stop", disabled=not RUNNING, use_container_width=True):
    st.session_state["inv_running"] = False
    RUNNING = False

# ─────────── DB helper ───────────
inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def get_snapshot() -> pd.DataFrame:
    """Cached inventory snapshot (qty + thresholds)."""
    return inv.stock_levels()

def refill_cycle() -> list[dict]:
    """
    One pass: for every SKU below threshold create stock up to average_required.
    Returns simple action log for UI.
    """
    snap = get_snapshot()
    below = snap[snap.totalqty < snap.threshold]

    actions: list[dict] = []
    for _, row in below.iterrows():
        need = int(row.average_required) - int(row.totalqty)
        try:
            poid = inv.restock_item(int(row.itemid), need)
            status = f"PO #{poid}" if poid else "OK"
        except ValueError as e:
            status = f"ERR: {e}"
        actions.append(
            {
                "item": row.itemnameenglish,
                "stock_before": int(row.totalqty),
                "added": need,
                "result": status,
            }
        )
    return actions

# ─────────── main loop ───────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTERVAL_SEC:
        st.session_state["inv_last_result"] = refill_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    # miniature dashboard
    st.metric("Cycles run", st.session_state["inv_cycle_count"])
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["last_inv_check"])
        .strftime("%F %T"),
    )
    st.metric("SKUs processed", len(st.session_state["inv_last_result"]))

    if st.session_state["inv_last_result"]:
        st.dataframe(
            pd.DataFrame(st.session_state["inv_last_result"]),
            use_container_width=True,
        )

    # yield control & re‑run
    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory refills.")
