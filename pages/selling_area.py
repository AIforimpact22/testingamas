from __future__ import annotations
"""
🗄️ Shelf Auto‑Refill – Optimized, UI/Streamlit only (no handler code here)
"""

import time
import traceback
from datetime import datetime

import pandas as pd
import streamlit as st

from handler.selling_area_handler import SellingAreaHandler

# ─────────── UI basics ───────────
st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

# interval
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VAL   = st.sidebar.number_input("Interval", 1, step=1, value=10)
SECONDS = VAL * {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}[UNIT]

DEBUG = st.sidebar.checkbox("🔍 Debug mode")

# session defaults
st.session_state.setdefault("running", False)
st.session_state.setdefault("last_ts", 0.0)
st.session_state.setdefault("cycles", 0)
st.session_state.setdefault("last_log", [])
st.session_state.setdefault("history_log", [])   # for full log history

# start/stop
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=st.session_state.running):
    st.session_state.update(running=True,
                            last_ts=time.time() - SECONDS,
                            cycles=0,
                            last_log=[],
                            history_log=[])
if c2.button("⏹ Stop", disabled=not st.session_state.running):
    st.session_state.running = False

# instantiate handler once
handler = SellingAreaHandler()
USER = "AUTO‑SHELF"
DUMMY_SALEID = 0     # unchanged

# ─────────── refill logic, optimized ───────────
def refill_item(
    *,
    itemid: int,
    current_qty: int,
    meta,
) -> str:
    threshold = getattr(meta, "shelfthreshold", meta["shelfthreshold"])
    average   = getattr(meta, "shelfaverage", meta["shelfaverage"])
    if current_qty >= threshold:
        return "OK"

    need = max(average - current_qty, threshold - current_qty)

    # resolve open shortages
    need = handler.resolve_shortages(itemid=itemid, qty_need=need, user=USER)
    if need <= 0:
        return "Shortage cleared"

    layers = handler.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
          FROM inventory
         WHERE itemid = %s AND quantity > 0
      ORDER BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )
    for lyr in layers.itertuples():
        take = min(need, int(lyr.quantity))
        handler.transfer_from_inventory(
            itemid=itemid,
            expirationdate=lyr.expirationdate,
            quantity=take,
            cost_per_unit=float(lyr.cost_per_unit),
            created_by=USER,
        )
        need -= take
        if need == 0:
            return "Refilled"

    # not enough inventory – log shortage
    handler.execute_command(
        """
        INSERT INTO shelf_shortage
              (saleid, itemid, shortage_qty, logged_at)
        VALUES (%s,%s,%s,CURRENT_TIMESTAMP)
        """,
        (DUMMY_SALEID, itemid, need),
    )
    return f"Partial (short {need})"

# ─────────── main refill cycle ───────────
def run_cycle() -> list[dict]:
    below = handler.get_items_below_shelfthreshold()
    if below.empty:
        st.info("Nothing to refill this cycle.")
        return []

    log: list[dict] = []
    n = len(below)
    item_progress = st.empty()
    step_bar = st.progress(0, text="Processing items...")

    for i, row in enumerate(below.itertuples(index=False), 1):
        item_progress.info(f"Processing: **{row.itemname}** ({i} of {n})")
        try:
            action = refill_item(
                itemid=row.itemid,
                current_qty=row.totalquantity,
                meta=row,
            )
        except Exception as e:
            action = f"Error: {e}"
            if DEBUG:
                st.error(f"Error processing {row.itemname}: {e}")
        log.append({"item": row.itemname, "action": action})
        step_bar.progress(i / n, text=f"Processed {i}/{n}")
        if DEBUG:
            time.sleep(0.15)
    item_progress.success("Cycle complete!")
    step_bar.progress(1.0, text="Done.")

    return log

# ─────────── main loop ───────────
if st.session_state.running:
    now = time.time()
    rem = SECONDS - (now - st.session_state.last_ts)
    if rem <= 0:
        try:
            log = run_cycle()
            st.session_state.last_log = log
            if log:
                st.session_state.history_log.extend(log)
        except Exception as exc:
            st.error("⛔ " + "".join(traceback.format_exception_only(type(exc), exc)))
            st.session_state.running = False
            st.stop()

        st.session_state.last_ts = time.time()
        st.session_state.cycles += 1
        rem = SECONDS

    # metrics
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Cycles",     st.session_state.cycles)
    cc2.metric("Processed",  len(st.session_state.last_log))
    cc3.metric(
        "Last run",
        datetime.fromtimestamp(st.session_state.last_ts).strftime("%F %T"),
    )

    st.progress(1 - rem / SECONDS, text=f"Next cycle in {int(rem)} s")

    with st.expander("Last cycle log", expanded=False):
        if st.session_state.last_log:
            st.dataframe(pd.DataFrame(st.session_state.last_log),
                         use_container_width=True)
        else:
            st.write("— nothing this time —")

    with st.expander("All actions this session (history)", expanded=False):
        if st.session_state.history_log:
            st.dataframe(pd.DataFrame(st.session_state.history_log),
                         use_container_width=True)
        else:
            st.write("— no actions yet —")

    time.sleep(0.15)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
