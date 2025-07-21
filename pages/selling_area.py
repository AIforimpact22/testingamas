from __future__ import annotations
"""
Selling‑Area Auto‑Refill
Press **Start** to begin; the checker runs forever at the chosen interval
until you press **Stop**.
"""

import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.shelf_handler import ShelfHandler

# ───────────── page config ─────────────
st.set_page_config(page_title="Shelf Auto‑Refill", page_icon="🗄️")
st.title("🗄️ Shelf Auto‑Refill")

# ───────────── sidebar controls ─────────────
st.sidebar.header("Interval")
UNIT = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours", "Days"))
VALUE = st.sidebar.number_input("Every …", min_value=1, step=1, value=10)

mult = dict(Seconds=1, Minutes=60, Hours=3600, Days=86400)[UNIT]
INTERVAL_SEC = VALUE * mult

# ───────────── Start / Stop buttons ─────────────
RUNNING = st.session_state.get("shelf_running", False)
col_run, col_stop = st.columns(2)
if col_run.button("▶ Start", disabled=RUNNING):
    st.session_state.update(
        shelf_running=True,
        last_check_ts=time.time(),     # so first cycle runs immediately
        cycle_count=0,
        last_result=[]
    )
    RUNNING = True

if col_stop.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["shelf_running"] = False
    RUNNING = False

# ───────────── data helpers ─────────────
shelf = ShelfHandler()
DUMMY_SALEID = 0

@st.cache_data(ttl=300, show_spinner=False)
def item_meta() -> pd.DataFrame:
    return shelf.get_all_items().set_index("itemid")

def restock_item(itemid: int, *, user="AUTO‑SHELF") -> str:
    meta = item_meta()
    kpi  = shelf.get_shelf_quantity_by_item()
    rowk = kpi.loc[kpi.itemid == itemid]
    current = int(rowk.totalquantity.iloc[0]) if not rowk.empty else 0

    threshold = int(meta.at[itemid, "shelfthreshold"] or 0)
    average   = int(meta.at[itemid, "shelfaverage"]   or threshold or 0)

    if current >= threshold:
        return "OK"

    need = max(average - current, threshold - current)
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)
    if need <= 0:
        return "Shortage cleared"

    layers = shelf.fetch_data(
        "SELECT expirationdate, quantity, cost_per_unit "
        "FROM inventory WHERE itemid=%s AND quantity>0 "
        "ORDER BY expirationdate, cost_per_unit",
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
        )
        need -= take
        if need == 0:
            return "Refilled"

    # not enough inventory – log shortage (saleid = 0)
    shelf.execute_command(
        """
        INSERT INTO shelf_shortage
              (saleid, itemid, shortage_qty, logged_at)
        VALUES (%s,     %s,     %s,           CURRENT_TIMESTAMP)
        """,
        (DUMMY_SALEID, itemid, need),
    )
    return f"Partial (short {need})"

def run_cycle() -> list[dict]:
    kpi  = shelf.get_shelf_quantity_by_item()
    meta = item_meta().reset_index()
    df = kpi.merge(meta[["itemid", "shelfthreshold", "shelfaverage"]],
                   on="itemid", how="left")
    df["threshold"] = df["shelfthreshold"].fillna(0)
    actions = []
    below = df[df.totalquantity < df.threshold]
    for _, r in below.iterrows():
        status = restock_item(int(r.itemid))
        actions.append({"item": r.itemname, "action": status})
    return actions

# ───────────── main loop ─────────────
if RUNNING:
    now = time.time()
    if now - st.session_state["last_check_ts"] >= INTERVAL_SEC:
        result = run_cycle()
        st.session_state["last_result"]   = result
        st.session_state["last_check_ts"] = now
        st.session_state["cycle_count"]  += 1

    # status block
    st.metric("Cycles run", st.session_state["cycle_count"])
    st.metric("Last cycle", datetime.fromtimestamp(
        st.session_state["last_check_ts"]).strftime("%F %T"))
    touched = len(st.session_state["last_result"])
    st.metric("SKUs processed last cycle", touched)

    time.sleep(0.2)      # keep the loop alive but not busy‑wait
    st.rerun()
else:
    st.info("Click **Start** to begin automatic shelf top‑ups.")
