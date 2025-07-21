"""
Inventory Auto‑Refill
Press **Start** – the worker checks every N seconds and tops‑up inventory
(creates one synthetic PO per supplier / cycle).  Runs until **Stop**.
"""

from __future__ import annotations
import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.inventory_handler import InventoryHandler

# ───────── page & sidebar ─────────
st.set_page_config(page_title="Inventory Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

st.sidebar.header("Interval")
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours"))
VAL   = st.sidebar.number_input("Every …", 1, step=1, value=10)
mult  = dict(Seconds=1, Minutes=60, Hours=3600)[UNIT]
INTERVAL_SEC = VAL * mult

run_col, stop_col = st.columns(2)
RUNNING = st.session_state.get("inv_running", False)
if run_col.button("▶ Start", disabled=RUNNING):
    st.session_state.update(
        inv_running=True,
        last_inv_check=time.time(),
        inv_cycle_count=0,
        inv_last_result=[],
    )
    RUNNING = True
if stop_col.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["inv_running"] = False
    RUNNING = False

# ───────── data helpers ─────────
inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def run_cycle() -> list[dict]:
    snap = snapshot()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return []

    below["need"] = below["average"] - below["totalqty"]
    acts = inv.batch_restock(below[["itemid", "need", "cpu", "itemnameenglish"]])
    # bust cache so next cycle sees fresh quantities
    snapshot.clear()
    return acts

# ───────── main loop ─────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_inv_check"] >= INTERVAL_SEC:
        st.session_state["inv_last_result"] = run_cycle()
        st.session_state["last_inv_check"]  = now
        st.session_state["inv_cycle_count"] += 1

    st.metric("Cycles", st.session_state["inv_cycle_count"])
    st.metric("Last run",
              datetime.fromtimestamp(st.session_state["last_inv_check"])
              .strftime("%F %T"))
    st.metric("Rows added",
              sum(a["added"] for a in st.session_state["inv_last_result"]))
    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory refill.")
