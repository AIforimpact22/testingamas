from __future__ import annotations
import time
from datetime import datetime
import streamlit as st
import pandas as pd
from handler.inventory_handler import InventoryHandler

st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# interval controls
st.sidebar.header("Interval")
unit  = st.sidebar.selectbox("Unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", 1, step=1, value=10)
mult  = {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]
INTERVAL_SEC = value * mult

# start/stop buttons
RUN = st.session_state.get("inv_run", False)
if st.button("▶ Start", disabled=RUN):
    st.session_state.update(inv_run=True, last_chk=0.0,
                            cycles=0, last_actions=[])
    RUN = True
if st.button("⏹ Stop", disabled=not RUN):
    st.session_state["inv_run"] = False
    RUN = False

inv = InventoryHandler()

@st.cache_data(ttl=300, show_spinner=False)
def snap() -> pd.DataFrame:
    return inv.stock_levels()

def cycle() -> list[dict]:
    df = snap()
    below = df[df.totalqty < df.threshold]
    acts  : list[dict] = []
    for _, r in below.iterrows():
        need = int(r.averagerequired) - int(r.totalqty)
        res  = inv.refill(itemid=int(r.itemid), qty_needed=need)
        acts.append(dict(item=r.itemnameenglish, added=need, status=res))
    return acts

if RUN:
    if time.time() - st.session_state["last_chk"] >= INTERVAL_SEC:
        st.session_state["last_actions"] = cycle()
        st.session_state["last_chk"]    = time.time()
        st.session_state["cycles"]     += 1

    st.metric("Cycles run", st.session_state["cycles"])
    st.metric(
        "Last cycle",
        datetime.fromtimestamp(st.session_state["last_chk"]).strftime("%F %T"),
    )
    st.metric("SKUs processed", len(st.session_state["last_actions"]))
    time.sleep(0.2)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic warehouse top‑ups.")
