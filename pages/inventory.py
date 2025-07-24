"""
📦 Inventory Auto‑Refill – live debug view + supplier‑batch progress
(2025‑07‑24 verbose edition)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List

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

DEBUG_MODE = st.sidebar.checkbox("🔍 Debug mode (show extra frames)")

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
    """Full stock snapshot (warehouse totals vs meta)."""
    return inv.stock_levels()

def compute_below(df: pd.DataFrame) -> pd.DataFrame:
    """Return rows that need replenishment + need qty."""
    below = df[df.totalqty < df.threshold].copy()
    if below.empty:
        return below
    below["target"] = below[["average", "threshold"]].max(axis=1)
    below["need"]   = below["target"] - below["totalqty"]
    return below[below.need > 0]

def one_cycle() -> Dict[str, List]:
    """
    • Detect items below threshold
    • Refill per supplier in batches
    • Returns {'log': [...], 'by_supplier': [...] }
    """
    snap = snapshot()
    if DEBUG_MODE:
        st.subheader("Snapshot (warehouse totals)")
        st.dataframe(snap, height=300, use_container_width=True)

    below = compute_below(snap)
    if below.empty:
        st.toast("Warehouse already above thresholds – nothing to do.", icon="✅")
        return {"log": [], "by_supplier": []}

    st.subheader(f"Items below threshold ({len(below)})")
    st.dataframe(below[["itemid","itemnameenglish","totalqty",
                        "threshold","average","need"]],
                 height=300, use_container_width=True)

    # -- live supplier batches ----------------------------------------
    log: list  = []
    batches: list = []
    df_need = below[["itemid", "need", "sellingprice"]]
    df_need["supplier"] = df_need["itemid"].apply(inv.supplier_for)

    total_suppliers = df_need.supplier.nunique()
    prog = st.progress(0.0, text="Waiting…")

    for i, (sup_id, grp) in enumerate(df_need.groupby("supplier"), start=1):
        with st.spinner(f"Restocking supplier {sup_id} "
                        f"({i}/{total_suppliers})…"):
            result = inv.restock_items_bulk(grp, debug=DEBUG_MODE)
            log.extend(result["log"])
            batches.append({
                "supplier_id": sup_id,
                "df": grp.copy(),
                "count": len(grp),
            })
            prog.progress(i/total_suppliers,
                          text=f"Suppliers processed: {i}/{total_suppliers}")
            st.toast(f"Supplier {sup_id} done ({len(grp)} rows)", icon="📦")

            if DEBUG_MODE and result.get("by_supplier"):
                with st.expander(f"DEBUG ↘ supplier {sup_id} handler info"):
                    st.dataframe(result["by_supplier"][sup_id],
                                 use_container_width=True)

            time.sleep(0.2)

    prog.empty()
    return {"log": log, "by_supplier": batches}

# ───────── start / stop buttons ─────────
col_start, col_stop = st.columns(2)
if col_start.button("▶ Start", disabled=st.session_state.inv_run):
    st.session_state.update(inv_run=True, last_ts=0.0,
                            cycles=0, last_log=[], all_logs=[], supplier_logs=[])
if col_stop.button("⏹ Stop", disabled=not st.session_state.inv_run):
    st.session_state.inv_run = False

# ───────── MAIN LOOP ─────────
if st.session_state.inv_run:
    now = time.time()
    remaining = max(0.0, INTERVAL - (now - st.session_state.last_ts))

    if remaining == 0:
        try:
            result = one_cycle()
            st.session_state.last_log = result["log"]
            st.session_state.all_logs.extend(result["log"])
            st.session_state.supplier_logs.extend(result["by_supplier"])
            st.success(f"Cycle complete – {len(result['log'])} inventory rows added.")
            time.sleep(1.0)
        except Exception as exc:
            st.exception(exc)
            st.session_state.inv_run = False
            st.stop()

        st.session_state.last_ts = time.time()
        st.session_state.cycles += 1
        remaining = INTERVAL

    # ── metrics & timers ──
    c1, c2, c3 = st.columns(3)
    c1.metric("Cycles",     st.session_state.cycles)
    c2.metric("Rows added", len(st.session_state.last_log))
    ts = st.session_state.last_ts
    c3.metric("Last run", datetime.fromtimestamp(ts).strftime("%F %T") if ts else "—")

    st.progress(1.0 - remaining / INTERVAL,
                text=f"Next cycle in {int(remaining)} s")

    # ── history/debug tabs ──
    tabs = st.tabs(["Last Cycle Log", "All Cycles", "Supplier Batches"])
    with tabs[0]:
        st.subheader("Last cycle log")
        st.dataframe(pd.DataFrame(st.session_state.last_log)
                     if st.session_state.last_log else
                     pd.DataFrame({"info":["Nothing added last cycle."]}),
                     use_container_width=True)

    with tabs[1]:
        st.subheader("All refill actions")
        st.dataframe(pd.DataFrame(st.session_state.all_logs)
                     if st.session_state.all_logs else
                     pd.DataFrame({"info":["No refill actions yet."]}),
                     use_container_width=True)

    with tabs[2]:
        st.subheader("Batches by supplier (last 10)")
        if st.session_state.supplier_logs:
            for entry in st.session_state.supplier_logs[-10:]:
                with st.expander(f"Supplier {entry['supplier_id']} "
                                 f"– {entry['count']} items"):
                    st.dataframe(entry["df"], use_container_width=True)
        else:
            st.write("No supplier batches yet.")

    time.sleep(0.1)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic inventory top‑ups.")
