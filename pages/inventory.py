# pages/inventory.py
"""
📦 Inventory Auto‑Refill
────────────────────────
Press **Start** – every cycle bulk‑refills inventory up to
`averagerequired`.  Live progress bar + action log appended.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from handler.inventory_handler import InventoryHandler

# ──────────────── Streamlit setup ────────────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ──────────────── sidebar interval ────────────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", min_value=1, step=1, value=30)
INTERVAL = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

# ──────────────── session state ────────────────
defaults = dict(
    inv_run   = False,
    last_ts   = 0.0,
    cycles    = 0,
    last_log  = [],            # current‑cycle log (overwritten)
    all_logs  = [],            # cumulative
)
for k, v in defaults.items():
    st.session_state.setdefault(k, v)

RUN = st.session_state.inv_run
inv = InventoryHandler()

# ──────────────── small helpers ────────────────
def take_snapshot() -> pd.DataFrame:
    """Always hit the DB – no caching to avoid stale data."""
    return inv.stock_levels()

def one_cycle() -> list[dict]:
    snap  = take_snapshot()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return []
    below["need"] = below["average"] - below["totalqty"]
    return inv.restock_items_bulk(
        below[["itemid", "need", "sellingprice"]]
    )

# ──────────────── start / stop buttons ────────────────
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=RUN):
    st.session_state.update(inv_run=True, last_ts=0.0, cycles=0,
                            last_log=[], all_logs=[])
    RUN = True
if c2.button("⏹ Stop", disabled=not RUN):
    st.session_state.inv_run = False
    RUN = False

# ──────────────── main loop ────────────────
placeholder_metrics = st.empty()
placeholder_progress = st.empty()
placeholder_log = st.expander("▼ Last cycle log", expanded=False)

if RUN:
    now = time.time()
    remaining = max(0.0, INTERVAL - (now - st.session_state.last_ts))

    if remaining == 0:
        # do the work
        try:
            cycle_log = one_cycle()
        except Exception as exc:
            st.error(f"⚠️ Database error: {exc!s}")
            st.session_state.inv_run = False
            st.stop()

        st.session_state.update(
            last_ts=time.time(),
            cycles=st.session_state.cycles + 1,
            last_log=cycle_log,
            all_logs=st.session_state.all_logs + cycle_log,
        )
        remaining = INTERVAL     # reset the countdown

    # ────────── live widgets ──────────
    placeholder_metrics.metric("Cycles", st.session_state.cycles)
    placeholder_metrics.metric("Rows added", len(st.session_state.last_log))
    placeholder_metrics.metric(
        "Last run",
        datetime.fromtimestamp(
            st.session_state.last_ts
        ).strftime("%F %T") if st.session_state.last_ts else "—",
    )

    placeholder_progress.progress(
        1.0 - remaining / INTERVAL,
        text=f"Next cycle in {int(remaining)} s",
    )

    if st.session_state.last_log:
        df_log = pd.DataFrame(st.session_state.last_log)
        placeholder_log.dataframe(df_log, use_container_width=True)

    # gentle yield so the UI can refresh smoothly
    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
