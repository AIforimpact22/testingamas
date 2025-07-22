"""
🗄️ Shelf Auto‑Refill (bulk)
Press **Start** – every cycle moves inventory → shelf in one bulk
transaction until each SKU reaches `shelfaverage`
(or, if that is NULL, at least `shelfthreshold`).
"""

from __future__ import annotations
import time
from datetime import datetime

import streamlit as st
import pandas as pd

from handler.selling_area_handler import SellingAreaHandler

# ───────── page setup ─────────
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
st.session_state.setdefault("s_log",    [])      # last‑cycle rows

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

def snapshot() -> pd.DataFrame:
    """Always fresh – no Streamlit caching to avoid stale data."""
    return sa.shelf_kpis()

def cycle() -> list[dict]:
    df = snapshot()
    # thresholds may be NULL; treat NULL as 0
    df["threshold"] = df["shelfthreshold"].fillna(0).astype(int)
    df["average"]   = df["shelfaverage"].fillna(df["threshold"]).astype(int)

    # target = shelfaverage if defined else shelfthreshold
    df["target"] = df["average"].where(df["average"] > 0, df["threshold"])
    need_df = df[df.totalqty < df["target"]].copy()
    if need_df.empty:
        return []

    need_df["need"] = need_df["target"] - need_df["totalqty"]
    need_df = need_df[need_df.need > 0]

    return sa.restock_items_bulk(need_df[["itemid", "need"]])

# ───────── main loop ─────────
if RUN:
    now = time.time()
    if now - st.session_state["s_last"] >= INTERVAL:
        st.session_state["s_log"]    = cycle()
        st.session_state["s_last"]   = now
        st.session_state["s_cycles"] += 1

    st.metric("Cycles run",  st.session_state["s_cycles"])
    st.metric("Rows moved",  len(st.session_state["s_log"]))
    ts = datetime.fromtimestamp(st.session_state["s_last"]).strftime("%F %T")
    st.metric("Last cycle", ts)

    time.sleep(0.3)      # let the UI breathe ☺
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
