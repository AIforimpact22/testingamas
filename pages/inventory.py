# pages/inventory.py
"""
📦 Inventory Auto‑Refill – with live debug view
"""

from __future__ import annotations
import time
from datetime import datetime
import pandas as pd
import streamlit as st
from handler.inventory_handler import InventoryHandler

# ───────── Streamlit config ─────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ───────── sidebar controls ─────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", min_value=1, step=1, value=30)
INTERVAL = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

DEBUG_MODE = st.sidebar.checkbox("🔍 Debug mode")

# ───────── session state ─────────
defaults = dict(inv_run=False, last_ts=0.0, cycles=0,
                last_log=[], all_logs=[])
for k, v in defaults.items():
    st.session_state.setdefault(k, v)

inv = InventoryHandler()

# ───────── helper fns ─────────
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def one_cycle() -> dict:
    snap  = snapshot()

    # show in debug
    if DEBUG_MODE:
        st.subheader("Snapshot")
        st.dataframe(snap, height=300, use_container_width=True)

    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return {"log": [], "by_supplier": {}}

    # safe target when average is 0 / NULL
    below["target"] = below[["average", "threshold"]].max(axis=1)
    below["need"]   = below["target"] - below["totalqty"]
    below = below[below.need > 0]

    if DEBUG_MODE:
        st.subheader("Below threshold")
        st.dataframe(below, height=300, use_container_width=True)

    return inv.restock_items_bulk(
        below[["itemid", "need", "sellingprice"]], debug=DEBUG_MODE
    )

# ───────── start / stop ─────────
col_start, col_stop = st.columns(2)
if col_start.button("▶ Start", disabled=st.session_state.inv_run):
    st.session_state.update(inv_run=True, last_ts=0.0,
                            cycles=0, last_log=[], all_logs=[])
if col_stop.button("⏹ Stop", disabled=not st.session_state.inv_run):
    st.session_state.inv_run = False

# ───────── main loop ─────────
if st.session_state.inv_run:
    now = time.time()
    remaining = max(0.0, INTERVAL - (now - st.session_state.last_ts))

    if remaining == 0:
        try:
            result = one_cycle()
        except Exception as exc:            # surface any SQL / lock errors
            st.error(f"⛔ {exc!s}")
            st.session_state.inv_run = False
            st.stop()

        st.session_state.last_ts = time.time()
        st.session_state.cycles += 1
        st.session_state.last_log = result["log"]
        st.session_state.all_logs.extend(result["log"])
        remaining = INTERVAL

        if DEBUG_MODE and result["by_supplier"]:
            st.subheader("Refill groups (per supplier)")
            for sup, df_sup in result["by_supplier"].items():
                with st.expander(f"Supplier {sup} – {len(df_sup)} rows"):
                    st.dataframe(df_sup, use_container_width=True)

    # ── metrics ──
    c1, c2, c3 = st.columns(3)
    c1.metric("Cycles",     st.session_state.cycles)
    c2.metric("Rows added", len(st.session_state.last_log))
    c3.metric(
        "Last run",
        datetime.fromtimestamp(st.session_state.last_ts).strftime("%F %T")
        if st.session_state.last_ts else "—",
    )

    # ── progress bar ──
    st.progress(1.0 - remaining / INTERVAL,
                text=f"Next cycle in {int(remaining)} s")

    # ── last‑cycle log ──
    with st.expander("Last cycle log", expanded=False):
        if st.session_state.last_log:
            st.dataframe(
                pd.DataFrame(st.session_state.last_log),
                use_container_width=True,
            )
        else:
            st.write("Nothing added last cycle.")

    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
