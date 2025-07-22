"""
🗄️  Selling‑Area Auto‑Refill  (bulk mode)
Press **Start** – every cycle moves inventory → shelf in bulk
until each SKU reaches `shelfaverage` (or at least `shelfthreshold`).
"""

from __future__ import annotations
import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.selling_area_handler import SellingAreaHandler

st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

# ─────────────────── interval controls ───────────────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes"))
value = st.sidebar.number_input("Every …", 1, step=1, value=15)
INTERVAL = value * (60 if unit == "Minutes" else 1)

# ─────────────────── seed session state ───────────────────
for k, v in {
    "shelf_run":   False,
    "shelf_last":  0.0,
    "shelf_cycles":0,
    "shelf_log":   [],
}.items():
    st.session_state.setdefault(k, v)

RUN = st.session_state["shelf_run"]
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=RUN):
    st.session_state.update(shelf_run=True,
                            shelf_last=0.0,
                            shelf_cycles=0,
                            shelf_log=[])
    RUN = True
if c2.button("⏹ Stop", disabled=not RUN):
    st.session_state["shelf_run"] = False
    RUN = False

sa = SellingAreaHandler()

@st.cache_data(ttl=300, show_spinner=False)
def kpi_snapshot() -> pd.DataFrame:
    return sa.shelf_kpis()

def one_cycle() -> list[dict]:
    kpi = kpi_snapshot()
    below = kpi[kpi.totalqty < kpi.shelfthreshold].copy()
    if below.empty:
        return []
    below["need"] = below["shelfaverage"].fillna(
        below["shelfthreshold"]) - below["totalqty"]
    below = below[below.need > 0]
    return sa.restock_items_bulk(below[["itemid", "need"]])

# ─────────────────── main loop ───────────────────
if RUN:
    now = time.time()
    if now - st.session_state["shelf_last"] >= INTERVAL:
        st.session_state["shelf_log"] = one_cycle()
        st.session_state["shelf_last"] = now
        st.session_state["shelf_cycles"] += 1

    st.metric("Cycles",        st.session_state["shelf_cycles"])
    st.metric("Rows moved",    len(st.session_state["shelf_log"]))
    st.metric("Last cycle",
              datetime.fromtimestamp(st.session_state["shelf_last"])
              .strftime("%F %T"))

    time.sleep(0.3)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
