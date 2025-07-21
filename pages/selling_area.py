from __future__ import annotations
"""
Selling‑Area Auto‑Refill
────────────────────────
Press **Start** → the checker runs forever at the chosen interval
until you press **Stop**.
"""

import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.selling_area_handler import SellingAreaHandler

# ───────── page config ─────────
st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

# ───────── interval picker ─────────
st.sidebar.header("Interval")
UNIT  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VALUE = st.sidebar.number_input("Every …", min_value=1, step=1, value=10)
SEC_PER_UNIT = {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86400}
INTERVAL_SEC = VALUE * SEC_PER_UNIT[UNIT]

# ───────── start / stop ─────────
RUNNING = st.session_state.get("shelf_running", False)
btn_start, btn_stop = st.columns(2)
if btn_start.button("▶ Start", disabled=RUNNING):
    st.session_state.update(
        shelf_running=True,
        last_check_ts=0.0,   # force immediate first run
        cycle_count=0,
        last_result=[],
    )
    RUNNING = True
if btn_stop.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["shelf_running"] = False
    RUNNING = False

# ───────── helpers ─────────
shelf = SellingAreaHandler()
DUMMY_SALEID = 0  # for system shortages


@st.cache_data(ttl=300, show_spinner=False)
def item_meta() -> pd.DataFrame:
    return shelf.get_all_items().set_index("itemid")


def restock_item(itemid: int, *, user="AUTO‑SHELF") -> str:
    meta = item_meta()
    kpi = shelf.get_shelf_quantity_by_item()
    rowk = kpi.loc[kpi.itemid == itemid]
    current = int(rowk.totalquantity.iloc[0]) if not rowk.empty else 0

    threshold = int(meta.at[itemid, "shelfthreshold"] or 0)
    average = int(meta.at[itemid, "shelfaverage"] or threshold or 0)
    if current >= threshold:
        return "OK"

    need = max(average - current, threshold - current)
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)
    if need <= 0:
        return "Shortage cleared"

    layers = shelf.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
        FROM   inventory
        WHERE  itemid = %s AND quantity > 0
        ORDER  BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )
    for lyr in layers.itertuples():
        take = min(need, int(lyr.quantity))
        shelf.transfer_from_inventory(
            itemid=itemid,
            expirationdate=lyr.expirationdate,
            quantity=take,
            cost_per_unit=float(lyr.cost_per_unit),
            created_by=user,
            locid=None,  # let the handler decide via item_slot
        )
        need -= take
        if need == 0:
            return "Refilled"

    # still short → system shortage
    shelf.execute_command(
        """
        INSERT INTO shelf_shortage
              (saleid, itemid, shortage_qty, logged_at)
        VALUES (%s,     %s,     %s,           CURRENT_TIMESTAMP)
        """,
        (DUMMY_SALEID, itemid, need),
    )
    return f"Partial (short {need})"


def run_cycle() -> list[dict]:
    kpi = shelf.get_shelf_quantity_by_item()
    meta = item_meta().reset_index()

    df = kpi.merge(
        meta[["itemid", "shelfthreshold"]],
        on="itemid", how="left",
    )
    df["shelfthreshold"] = df["shelfthreshold"].fillna(0)

    below = df[df.totalquantity < df.shelfthreshold]
    actions = []
    for _, r in below.iterrows():
        actions.append(
            dict(item=r.itemname, action=restock_item(int(r.itemid)))
        )
    return actions


# ───────── main loop ─────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_check_ts"] >= INTERVAL_SEC:
        st.session_state["last_result"] = run_cycle()
        st.session_state["last_check_ts"] = now
        st.session_state["cycle_count"] += 1

    st.metric("Cycles run", st.session_state["cycle_count"])
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["last_check_ts"])
        .strftime("%F %T"),
    )
    st.metric("SKUs processed last cycle", len(st.session_state["last_result"]))

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
