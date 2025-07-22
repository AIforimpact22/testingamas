"""
🗄️ Shelf Auto‑Refill
────────────────────────────────────────────
Press **Start** – every cycle moves inventory → shelf
for SKUs below `shelfthreshold`.  Refill quantity is:

    (shelfaverage − current shelf qty)  +  open shortages

Shortages are then resolved in FIFO order.
"""

from __future__ import annotations
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from handler.selling_area_handler import SellingAreaHandler

# ───────── page config ─────────
st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

# ───────── interval controls ─────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes"))
value = st.sidebar.number_input("Every …", 1, step=1, value=15)
INTERVAL = value * (60 if unit == "Minutes" else 1)

# ───────── session state ─────────
st.session_state.setdefault("s_run",    False)
st.session_state.setdefault("s_last",   0.0)
st.session_state.setdefault("s_cycles", 0)
st.session_state.setdefault("s_log",    [])      # rows moved last cycle

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
_USER = "AUTO‑SHELF"

# ───────── helpers ─────────
def cycle() -> list[dict]:
    """One refill pass – returns compact movement log for the UI."""
    # live KPIs
    kpi = sa.shelf_kpis()
    kpi["threshold"] = kpi["shelfthreshold"].fillna(0).astype(int)
    kpi["average"]   = kpi["shelfaverage"].fillna(kpi["threshold"]).astype(int)

    # unresolved shortages
    sh = sa.unresolved_shortages()
    sh = sh if not sh.empty else pd.DataFrame(columns=["itemid", "shortage"])
    sh["shortage"] = sh["shortage"].astype(int)

    df = kpi.merge(sh, on="itemid", how="left")
    df["shortage"] = df["shortage"].fillna(0).astype(int)

    # SKUs below threshold
    below = df[df.totalqty < df.threshold].copy()
    if below.empty:
        return []

    # compute refill need  (= avg gap + shortages)
    below["need"] = (below["average"] - below["totalqty"]).clip(lower=0)
    below["need"] += below["shortage"]
    below = below[below.need > 0]

    # move stock in bulk
    log = sa.restock_items_bulk(below[["itemid", "need"]])

    # ---- resolve shortages in proportion to what was actually added ----
    added_by_item = {row["itemid"]: row["added"] for row in log}
    for _, r in below.iterrows():
        iid   = int(r.itemid)
        short = int(r.shortage)
        if short <= 0 or iid not in added_by_item:
            continue
        # how many of the "added" units should clear shortages?
        resolved_qty = min(short, added_by_item[iid])
        if resolved_qty > 0:
            sa.resolve_shortages(itemid=iid,
                                 qty_filled=resolved_qty,
                                 user=_USER)

    return log

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
    st.metric("Last cycle",  ts)

    time.sleep(0.3)             # let the UI breathe ☺
    st.rerun()

else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
