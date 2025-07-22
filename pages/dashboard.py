# pages/dashboard.py
"""
📊  Simulation Dashboard
───────────────────────
Live overview of POS, Selling‑Area and Inventory simulators.
"""

from __future__ import annotations
from datetime import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Simulation Dashboard", page_icon="📊",
                   layout="wide")
st.title("📊 Live Simulation Dashboard")
st.caption("Shows the current status and most‑recent activity of all simulators."
           " Refreshes each Streamlit rerun (browser interaction).")

def ts_fmt(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%F %T")

# ╔═════════════════════ POS ═════════════════════╗
st.subheader("🛒 POS Simulator")
pos_on   = st.session_state.get("pos_running", False)
sales_n  = st.session_state.get("sales_count", 0)
sim_clk  = st.session_state.get("sim_clock", datetime.now())

c1, c2, c3 = st.columns(3)
c1.metric("Status", "RUNNING ✅" if pos_on else "STOPPED ⏸️")
c2.metric("Sales processed", sales_n)
c3.metric("Simulated time", f"{sim_clk:%F %T}")

st.divider()

# ╔══════════════ SELLING AREA (SHELF) ════════════╗
st.subheader("🗄️  Shelf Auto‑Refill")
s_on     = st.session_state.get("s_run", False)
s_cyc    = st.session_state.get("s_cycles", 0)
s_last   = st.session_state.get("s_last", None)
s_log    = st.session_state.get("s_log", [])

d1, d2, d3 = st.columns(3)
d1.metric("Status", "RUNNING ✅" if s_on else "STOPPED ⏸️")
d2.metric("Cycles run", s_cyc)
d3.metric("Last cycle", ts_fmt(s_last))

if s_log:
    st.write("Latest shelf moves")
    st.dataframe(pd.DataFrame(s_log))
else:
    st.caption("No shelf activity yet.")

st.divider()

# ╔════════════ INVENTORY (WAREHOUSE) ═════════════╗
st.subheader("📦 Inventory Auto‑Refill")
i_on    = st.session_state.get("i_run", False)
i_cyc   = st.session_state.get("i_cycles", 0)
i_last  = st.session_state.get("i_last", None)
i_log   = st.session_state.get("i_log", [])

e1, e2, e3 = st.columns(3)
e1.metric("Status", "RUNNING ✅" if i_on else "STOPPED ⏸️")
e2.metric("Cycles run", i_cyc)
e3.metric("Last cycle", ts_fmt(i_last))

if i_log:
    # summarise rows & suppliers
    df_i = pd.DataFrame(i_log)
    rows   = len(df_i)
    pos    = df_i["poid"].nunique() if "poid" in df_i.columns else "—"
    total  = int(df_i["added"].sum()) if "added" in df_i.columns else "—"

    st.write(f"**{rows}** inventory rows added "
             f"across **{pos}** PO(s) in last cycle "
             f"(total units added: **{total}**).")
    with st.expander("Show details"):
        st.dataframe(df_i)
else:
    st.caption("No inventory inserts yet.")
