# pages/inventory.py
"""
📦 Inventory Auto‑Refill
────────────────────────
Press **Start**; every cycle bulk‑refills inventory up to
`averagerequired` at the chosen interval.

• One PO per supplier per cycle.
• cost_per_unit = 75 % of selling price (0 if price is 0/NULL).
"""

from __future__ import annotations
import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.inventory_handler import InventoryHandler

st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ────────────────── sidebar interval ──────────────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", 1, step=1, value=30)
INTERVAL = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

# ────────────────── session‑state seeds ──────────────────
# (run once per browser session; avoids KeyError on reload)
for k, v in {
    "inv_run":   False,
    "last_ts":   0.0,
    "cycles":    0,
    "last_log":  [],
}.items():
    st.session_state.setdefault(k, v)

# ────────────────── start / stop ──────────────────
RUN = st.session_state["inv_run"]
c1, c2 = st.columns(2)

if c1.button("▶ Start", disabled=RUN):
    st.session_state.update(inv_run=True,
                            last_ts=0.0,
                            cycles=0,
                            last_log=[])
    RUN = True
if c2.button("⏹ Stop", disabled=not RUN):
    st.session_state["inv_run"] = False
    RUN = False

inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def one_cycle() -> list[dict]:
    snap  = snapshot()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return []

    below["need"] = below["average"] - below["totalqty"]
    return inv.restock_items_bulk(below[["itemid", "need", "sellingprice"]])

# ────────────────── main loop ──────────────────
if RUN:
    now = time.time()
    if now - st.session_state["last_ts"] >= INTERVAL:
        st.session_state["last_log"] = one_cycle()
        st.session_state["last_ts"]  = now
        st.session_state["cycles"]  += 1

    st.metric("Cycles",        st.session_state["cycles"])
    st.metric("Rows added",    len(st.session_state["last_log"]))
    st.metric("Last run",
              datetime.fromtimestamp(st.session_state["last_ts"]).strftime("%F %T"))

    # gentle yield so the UI can refresh smoothly
    time.sleep(0.3)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
