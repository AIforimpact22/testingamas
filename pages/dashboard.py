# pages/dashboard.py
"""
📊 Live Simulation Dashboard
────────────────────────────
Aggregates real‑time metrics from *POS*, *Shelf* and *Inventory*
simulator pages – all in one screen.
"""

from __future__ import annotations
from datetime import datetime
import streamlit as st

st.set_page_config(page_title="Simulation Dashboard", page_icon="📊",
                   layout="wide")

st.title("📊 Simulation Dashboard")

# ───────── helper ─────────
def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%F %T")

# ╭────────────────── POS STATUS ──────────────────╮
st.header("🛒 POS Simulator")
pos_running = st.session_state.get("pos_running", False)
colA, colB, colC = st.columns(3)
colA.metric("Status", "RUNNING ✅" if pos_running else "STOPPED ⏸️")
colB.metric("Sales processed",
            st.session_state.get("sales_count", 0))
colC.metric("Simulated clock",
            st.session_state.get("sim_clock",
                                 datetime.now()).strftime("%F %T"))

# ╭────────────────── SHELF STATUS ──────────────────╮
st.header("🗄️ Shelf Auto‑Refill")
s_running = st.session_state.get("shelf_run", False)
colD, colE, colF = st.columns(3)
colD.metric("Status", "RUNNING ✅" if s_running else "STOPPED ⏸️")
colE.metric("Cycles",
            st.session_state.get("shelf_cycles", 0))
colF.metric("Last cycle",
            fmt_ts(st.session_state.get("shelf_last")))

st.write("Latest shelf moves:")
log_s = st.session_state.get("shelf_log", [])
if log_s:
    st.dataframe(log_s)
else:
    st.caption("No moves logged yet.")

# ╭────────────────── INVENTORY STATUS ──────────────────╮
st.header("📦 Inventory Auto‑Refill")
i_running = st.session_state.get("inv_run", False)
colG, colH, colI = st.columns(3)
colG.metric("Status", "RUNNING ✅" if i_running else "STOPPED ⏸️")
colH.metric("Cycles",
            st.session_state.get("cycles", 0))
colI.metric("Last cycle",
            fmt_ts(st.session_state.get("last_inv_ts")))

st.write("Latest inventory rows added:")
log_i = st.session_state.get("last_log", [])
if log_i:
    st.dataframe(log_i)
else:
    st.caption("No inventory inserts logged yet.")
