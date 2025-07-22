# pages/dashboard.py
"""
📊  Simulation Dashboard
────────────────────────────────────────────────────────
Consolidated view of the three background simulators:

• **POS Simulation**      – live/total sales
• **Shelf Auto‑Refill**   – layers moved from inventory → shelf
• **Inventory Auto‑Refill** – units received & synthetic POs created
"""

from __future__ import annotations
from datetime import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Simulation Dashboard", page_icon="📊")
st.title("📊 Simulation Dashboard")

# ───────────────────────── helpers ─────────────────────────
def status(flag: bool) -> str:
    return "🟢 RUNNING" if flag else "🔴 STOPPED"

def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%F %T")

# ───────────────────────── POS block ─────────────────────────
pos_run  = st.session_state.get("pos_running", False)
pos_cnt  = st.session_state.get("sales_count", 0)

with st.container():
    st.subheader("🛒 POS Simulation")
    st.write(f"**Status:** {status(pos_run)}")
    cols = st.columns(2)
    cols[0].metric("Total sales (this session)", pos_cnt)
    cols[1].metric("Active cashiers",
                   st.session_state.get("CASHIERS", "—"))

# ───────────────────────── Shelf block ───────────────────────
s_run   = st.session_state.get("shelf_run", False)
s_rows  = len(st.session_state.get("shelf_log", []))
s_cyc   = st.session_state.get("shelf_cycles", 0)
s_ts    = st.session_state.get("shelf_last", None)

with st.container():
    st.subheader("🗄️ Shelf Auto‑Refill")
    st.write(f"**Status:** {status(s_run)}")
    cols = st.columns(3)
    cols[0].metric("Cycles run", s_cyc)
    cols[1].metric("Rows moved last cycle", s_rows)
    cols[2].metric("Last cycle", fmt_ts(s_ts))

# ───────────────────────── Inventory block ───────────────────
i_run   = st.session_state.get("inv_run", False)
i_rows  = len(st.session_state.get("inv_last_log", []))
i_cyc   = st.session_state.get("inv_cycles", 0)
i_ts    = st.session_state.get("inv_last_ts", None)

# count unique POIDs in last cycle’s log
last_log = st.session_state.get("inv_last_log", [])
poids = {row.get("poid") for row in last_log if row.get("poid")}

with st.container():
    st.subheader("📦 Inventory Auto‑Refill")
    st.write(f"**Status:** {status(i_run)}")
    cols = st.columns(3)
    cols[0].metric("Cycles run", i_cyc)
    cols[1].metric("Units added last cycle", i_rows)
    cols[2].metric("New POs last cycle", len(poids))
    st.caption(f"Last cycle @ {fmt_ts(i_ts)}")

# ───────────────────────── Detail toggles ────────────────────
with st.expander("🔍 Details – last shelf cycle"):
    if s_rows:
        st.dataframe(pd.DataFrame(st.session_state["shelf_log"]),
                      use_container_width=True)
    else:
        st.write("No shelf movements this cycle.")

with st.expander("🔍 Details – last inventory cycle"):
    if i_rows:
        st.dataframe(pd.DataFrame(last_log),
                      use_container_width=True)
    else:
        st.write("No inventory receipts this cycle.")
