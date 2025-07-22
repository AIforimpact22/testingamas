"""
🗄️ Shelf Auto‑Refill (bulk)
Press **Start** – every cycle moves inventory → shelf in one bulk
transaction until each SKU reaches `shelfaverage`
(or at least `shelfthreshold`).
"""

from __future__ import annotations
import time
from datetime import datetime
import streamlit as st
import pandas as pd

from handler.selling_area_handler import SellingAreaHandler

st.set_page_config("Shelf Auto‑Refill", "🗄️")
st.title("🗄️ Shelf Auto‑Refill (bulk)")

# ───────── interval controls ─────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes"))
value = st.sidebar.number_input("Every …", 1, step=1, value=15)
INTERVAL = value * (60 if unit == "Minutes" else 1)

# ───────── session state ─────────
st.session_state.setdefault("s_run",    False)
st.session_state.setdefault("s_last",   0.0)
st.session_state.setdefault("s_cycles", 0)
st.session_state.setdefault("s_log",    [])

RUN = st.session_state["s_run"]

c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=RUN):
    st.session_state.update(s_run=True,
                            s_last=0.0,
                            s_cycles=0,
                            s_log=[])
    RUN = True
if c2.button("⏹ Stop", disabled=not RUN):
    st.session_state["s_run"] = False
    RUN = False

sa = SellingAreaHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snapshot() -> pd.DataFrame:
    return sa.shelf_kpis()

def cycle() -> list[dict]:
    df = snapshot()
    df["threshold"] = df["shelfthreshold"].fillna(0)
    df["average"]   = df["shelfaverage"].fillna(df["threshold"])

    below = df[df.totalqty < df.threshold].copy()
    if below.empty:           # ← fixed (no parentheses)
        return []

    below["need"] = below["average"] - below["totalqty"]
    below = below[below.need > 0]
    return sa.restock_items_bulk(below[["itemid", "need"]])

# ───────── main loop ─────────
if RUN:
    now = time.time()
    if now - st.session_state["s_last"] >= INTERVAL:
        st.session_state["s_log"]    = cycle()
        st.session_state["s_last"]   = now
        st.session_state["s_cycles"] += 1

    st.metric("Cycles",      st.session_state["s_cycles"])
    st.metric("Rows moved",  len(st.session_state["s_log"]))
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["s_last"]).strftime("%F %T"),
    )

    time.sleep(0.3)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
