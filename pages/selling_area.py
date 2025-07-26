from __future__ import annotations
"""
🗄️ Shelf Auto‑Refill – batchid‑safe single‑transaction version
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

UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VAL   = st.sidebar.number_input("Interval", 1, step=1, value=10)
SECONDS = VAL * {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}[UNIT]

DEBUG = st.sidebar.checkbox("🔍 Debug mode")

# ─────────── session defaults ───────────
st.session_state.setdefault("running", False)
st.session_state.setdefault("last_ts", 0.0)
st.session_state.setdefault("cycles", 0)
st.session_state.setdefault("last_log", [])
st.session_state.setdefault("history_log", [])
st.session_state.setdefault("refilled_log", [])
st.session_state.setdefault("last_refilled_count", 0)

# ─────────── start / stop ───────────
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=st.session_state.running):
    st.session_state.update(
        running=True,
        last_ts=time.time() - SECONDS,
        cycles=0,
        last_log=[],
        history_log=[],
        refilled_log=[],
        last_refilled_count=0,
    )
if c2.button("⏹ Stop", disabled=not st.session_state.running):
    st.session_state.running = False

# ─────────── handler ───────────
handler = SellingAreaHandler()
USER            = "AUTO‑SHELF"
DUMMY_SALEID    = 0

# ─────────── per‑item refill ───────────
def refill_item(*, itemid: int, current_qty: int, meta) -> str:
    threshold = meta.shelfthreshold
    average   = meta.shelfaverage
    if current_qty >= threshold:
        return "OK"

    need = max(average - current_qty, threshold - current_qty)
    need = handler.resolve_shortages(itemid=itemid, qty_need=need, user=USER)
    if need <= 0:
        return "Shortage cleared"

    layers_df = handler.fetch_data(
        """
        SELECT batchid, expirationdate, quantity, cost_per_unit
          FROM inventory
         WHERE itemid = %s AND quantity > 0
      ORDER BY expirationdate, cost_per_unit, batchid
        """,
        (itemid,),
    )

    plan: list[tuple] = []           # (batchid, expirationdate, take, cpu)
    for lyr in layers_df.itertuples():
        if need == 0:
            break
        take = min(need, int(lyr.quantity))
        if take:
            plan.append(
                (int(lyr.batchid), lyr.expirationdate, take,
                 float(lyr.cost_per_unit))
            )
            need -= take

    if plan:
        handler.move_layers_to_shelf(
            itemid=itemid,
            layers=plan,
            created_by=USER,
        )

    if need > 0:
        handler.execute_command(
            """
            INSERT INTO shelf_shortage
                  (saleid, itemid, shortage_qty, logged_at)
            VALUES (%s,%s,%s,CURRENT_TIMESTAMP)
            """,
            (DUMMY_SALEID, itemid, need),
        )
        return f"Partial (short {need})"

    return "Refilled"

# ─────────── one full cycle ───────────
def run_cycle() -> list[dict]:
    below = handler.get_items_below_shelfthreshold()
    if below.empty:
        st.info("Nothing to refill this cycle.")
        st.session_state.last_refilled_count = 0
        return []

    log, refilled = [], []
    n = len(below)
    item_progress = st.empty()
    step_bar = st.progress(0.0, text="Processing items…")

    for i, row in enumerate(below.itertuples(index=False), 1):
        item_progress.info(f"Processing: **{row.itemname}** ({i}/{n})")
        try:
            action = refill_item(
                itemid=row.itemid,
                current_qty=row.totalquantity,
                meta=row,
            )
        except Exception as exc:
            action = f"Error: {exc}"
            if DEBUG:
                st.error(f"Error processing {row.itemname}: {exc}")

        log_entry = {
            "item":   row.itemname,
            "action": action,
            "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        log.append(log_entry)
        if action in ("Refilled", "Shortage cleared") or action.startswith("Partial"):
            refilled.append(log_entry)

        step_bar.progress(i / n, text=f"Processed {i}/{n}")
        if DEBUG:
            time.sleep(0.1)

    item_progress.success("Cycle complete!")
    step_bar.progress(1.0, text="Done.")
    if refilled:
        st.session_state.refilled_log.extend(refilled)
    st.session_state.last_refilled_count = len(refilled)
    return log

# ─────────── main loop ───────────
if st.session_state.running:
    now = time.time()
    rem = SECONDS - (now - st.session_state.last_ts)
    notify = st.empty()

    if rem <= 0:
        try:
            log = run_cycle()
            st.session_state.last_log = log
            st.session_state.history_log.extend(log)
            if st.session_state.last_refilled_count:
                notify.success(f"Cycle complete – {st.session_state.last_refilled_count} item(s) updated.")
            else:
                notify.info("Cycle complete – no items needed refilling.")
            time.sleep(2)
        except Exception as exc:
            st.error("⛔ " + "".join(traceback.format_exception_only(type(exc), exc)))
            st.session_state.running = False
            st.stop()

        st.session_state.last_ts = time.time()
        st.session_state.cycles += 1
        rem = SECONDS

    # metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("Cycles",    st.session_state.cycles)
    c2.metric("Processed", len(st.session_state.last_log))
    c3.metric("Last run",
              datetime.fromtimestamp(st.session_state.last_ts).strftime("%F %T"))

    st.progress(1 - rem / SECONDS, text=f"Next cycle in {int(rem)} s")

    # logs
    tab1, tab2, tab3 = st.tabs(
        ["Current Cycle", "All History", "Refilled This Session"]
    )
    with tab1:
        st.dataframe(pd.DataFrame(st.session_state.last_log)
                     if st.session_state.last_log else
                     pd.DataFrame({"info": ["Nothing this cycle."]}),
                     use_container_width=True)

    with tab2:
        st.dataframe(pd.DataFrame(st.session_state.history_log)
                     if st.session_state.history_log else
                     pd.DataFrame({"info": ["No actions yet."]}),
                     use_container_width=True)

    with tab3:
        st.dataframe(pd.DataFrame(st.session_state.refilled_log)
                     if st.session_state.refilled_log else
                     pd.DataFrame({"info": ["No refills yet."]}),
                     use_container_width=True)

    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
