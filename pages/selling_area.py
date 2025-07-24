from __future__ import annotations
"""
🗄️ Shelf Auto‑Refill – bulk, one‑shot version
"""
import time
from datetime import datetime

import pandas as pd
import streamlit as st                # ← the missing import!

from handler.selling_area_handler import SellingAreaHandler

# ─────────── UI basics ───────────
st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VAL   = st.sidebar.number_input("Interval", 1, step=1, value=10)
SECONDS = VAL * {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}[UNIT]

# ─────────── session defaults ───────────
for key, default in {
    "running": False,
    "last_ts": 0.0,
    "cycles": 0,
    "last_refilled_count": 0,
    "history_log": [],
    "refilled_log": [],
}.items():
    st.session_state.setdefault(key, default)

# ─────────── start / stop buttons ───────────
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=st.session_state.running):
    st.session_state.update(running=True,
                            last_ts=time.time() - SECONDS,
                            cycles=0,
                            history_log=[],
                            refilled_log=[],
                            last_refilled_count=0)
if c2.button("⏹ Stop", disabled=not st.session_state.running):
    st.session_state.running = False

# ─────────── handler & constants ───────────
handler = SellingAreaHandler()
USER = "AUTO‑SHELF"

# ─────────── bulk‑refill cycle ───────────
def run_cycle() -> None:
    moved = handler.bulk_refill(user=USER)
    st.session_state.last_refilled_count = moved
    log_entry = {
        "time": datetime.now().strftime("%Y‑%m‑%d %H:%M:%S"),
        "rows_moved": moved,
    }
    st.session_state.history_log.append(log_entry)
    if moved:
        st.session_state.refilled_log.append(log_entry)

# ─────────── main loop ───────────
if st.session_state.running:
    now = time.time()
    remaining = SECONDS - (now - st.session_state.last_ts)
    if remaining <= 0:
        run_cycle()
        st.session_state.cycles += 1
        st.session_state.last_ts = time.time()
        remaining = SECONDS

    # metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("Cycles",       st.session_state.cycles)
    m2.metric("Rows moved",   st.session_state.last_refilled_count)
    m3.metric("Last run",
              datetime.fromtimestamp(st.session_state.last_ts)
                      .strftime("%F %T"))

    st.progress(1 - remaining / SECONDS,
                text=f"Next cycle in {int(remaining)} s")

    # history tabs
    tab1, tab2 = st.tabs(["This Session", "Refilled Only"])
    with tab1:
        st.subheader("All actions this session")
        if st.session_state.history_log:
            st.dataframe(pd.DataFrame(st.session_state.history_log),
                         use_container_width=True)
        else:
            st.write("— nothing yet —")

    with tab2:
        st.subheader("Rows actually refilled / updated")
        if st.session_state.refilled_log:
            st.dataframe(pd.DataFrame(st.session_state.refilled_log),
                         use_container_width=True)
        else:
            st.write("— none this session —")

    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
