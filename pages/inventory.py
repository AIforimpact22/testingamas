"""
📦 Inventory Auto‑Refill – with live debug view and supplier batch progress
"""

from __future__ import annotations
import time
from datetime import datetime
import pandas as pd
import streamlit as st
from handler.inventory_handler import InventoryHandler

# ───────── Streamlit config ─────────
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="📦")
st.title("📦 Inventory Auto‑Refill")

# ───────── sidebar controls ─────────
unit  = st.sidebar.selectbox("Interval unit", ("Seconds", "Minutes", "Hours"))
value = st.sidebar.number_input("Every …", min_value=1, step=1, value=30)
INTERVAL = value * {"Seconds": 1, "Minutes": 60, "Hours": 3600}[unit]

DEBUG_MODE = st.sidebar.checkbox("🔍 Debug mode")

# ───────── session state ─────────
defaults = dict(
    inv_run=False, last_ts=0.0, cycles=0,
    last_log=[], all_logs=[], supplier_logs=[]
)
for k, v in defaults.items():
    st.session_state.setdefault(k, v)

inv = InventoryHandler()

# ───────── helper fns ─────────
def snapshot() -> pd.DataFrame:
    return inv.stock_levels()

def one_cycle() -> dict:
    snap  = snapshot()

    # show in debug
    if DEBUG_MODE:
        st.subheader("Snapshot")
        st.dataframe(snap, height=300, use_container_width=True)

    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return {"log": [], "by_supplier": {}}

    below["target"] = below[["average", "threshold"]].max(axis=1)
    below["need"]   = below["target"] - below["totalqty"]
    below = below[below.need > 0]

    if DEBUG_MODE:
        st.subheader("Below threshold")
        st.dataframe(below, height=300, use_container_width=True)

    # ----------- LIVE SUPPLIER PROGRESS -----------
    log: list = []
    supplier_logs = []
    total_suppliers = below["itemid"].apply(inv.supplier_for).nunique()
    suppliers_seen = 0

    df_need = below[["itemid", "need", "sellingprice"]]
    df_need["supplier"] = df_need["itemid"].apply(inv.supplier_for)

    for sup_id, grp in df_need.groupby("supplier"):
        suppliers_seen += 1
        st.info(f"Restocking for supplier {sup_id} ({suppliers_seen}/{total_suppliers})...")
        result = inv.restock_items_bulk(grp, debug=DEBUG_MODE)
        log.extend(result["log"])
        supplier_logs.append({
            "supplier_id": sup_id,
            "df": grp.copy(),
            "count": len(grp),
        })
        st.progress(suppliers_seen / total_suppliers, text=f"Suppliers processed: {suppliers_seen}/{total_suppliers}")
        if DEBUG_MODE:
            with st.expander(f"Supplier {sup_id} – {len(grp)} rows"):
                st.dataframe(grp, use_container_width=True)
        time.sleep(0.2)

    return {"log": log, "by_supplier": supplier_logs}

# ───────── start / stop ─────────
col_start, col_stop = st.columns(2)
if col_start.button("▶ Start", disabled=st.session_state.inv_run):
    st.session_state.update(inv_run=True, last_ts=0.0,
                            cycles=0, last_log=[], all_logs=[], supplier_logs=[])
if col_stop.button("⏹ Stop", disabled=not st.session_state.inv_run):
    st.session_state.inv_run = False

# ───────── main loop ─────────
if st.session_state.inv_run:
    now = time.time()
    remaining = max(0.0, INTERVAL - (now - st.session_state.last_ts))

    if remaining == 0:
        try:
            result = one_cycle()
            st.session_state.last_log = result["log"]
            st.session_state.all_logs.extend(result["log"])
            st.session_state.supplier_logs.extend(result.get("by_supplier", []))
            st.success(f"Cycle complete! {len(result['log'])} items restocked in this run.")
            time.sleep(2.0)
        except Exception as exc:
            st.error(f"⛔ {exc!s}")
            st.session_state.inv_run = False
            st.stop()

        st.session_state.last_ts = time.time()
        st.session_state.cycles += 1
        remaining = INTERVAL

    # ── metrics ──
    c1, c2, c3 = st.columns(3)
    c1.metric("Cycles",     st.session_state.cycles)
    c2.metric("Rows added", len(st.session_state.last_log))
    c3.metric(
        "Last run",
        datetime.fromtimestamp(st.session_state.last_ts).strftime("%F %T")
        if st.session_state.last_ts else "—",
    )

    st.progress(1.0 - remaining / INTERVAL,
                text=f"Next cycle in {int(remaining)} s")

    # Tabs for logs and per-supplier views
    tab1, tab2, tab3 = st.tabs(["Last Cycle Log", "All Cycles (History)", "Supplier Batches"])
    with tab1:
        st.subheader("Last cycle log")
        if st.session_state.last_log:
            st.dataframe(
                pd.DataFrame(st.session_state.last_log),
                use_container_width=True,
            )
        else:
            st.write("Nothing added last cycle.")

    with tab2:
        st.subheader("All logs (history)")
        if st.session_state.all_logs:
            st.dataframe(
                pd.DataFrame(st.session_state.all_logs),
                use_container_width=True,
            )
        else:
            st.write("No refill actions yet.")

    with tab3:
        st.subheader("Batches by Supplier")
        if st.session_state.supplier_logs:
            for entry in st.session_state.supplier_logs[-10:]:
                with st.expander(f"Supplier {entry['supplier_id']} – {entry['count']} items"):
                    st.dataframe(entry["df"], use_container_width=True)
        else:
            st.write("No supplier batches yet.")

    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
