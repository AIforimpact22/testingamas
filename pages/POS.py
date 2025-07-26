from __future__ import annotations
"""
🛒 POS + Inventory + Shelf automation – batchid‑safe edition (2025‑07‑26)
"""

import random
import time
import traceback
from datetime import datetime, timedelta
from typing import List

import pandas as pd
import streamlit as st

from handler.POS_handler import POSHandler
from handler.inventory_handler import InventoryHandler
from handler.selling_area_handler import SellingAreaHandler

# ───── UI CONFIG ─────
st.set_page_config(page_title="Unified POS / Refill", page_icon="🛒")
st.title("🛒 POS + Inventory + Shelf automation")

# POS parameters ----------------------------------------------------------
st.sidebar.header("POS parameters")
SPEED   = st.sidebar.number_input("Speed multiplier (×)", 1, 200, 1, 1)
PROFILE = st.sidebar.selectbox(
    "Load profile", ("Standard (steady)", "Real‑time market curve")
)
CASHIERS = st.sidebar.slider("Active cashiers", 1, 10, 3)

min_items = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty   = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty   = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

# automation intervals ----------------------------------------------------
st.sidebar.header("Automation intervals")
def _interval(label: str, default_val: int, default_unit="Minutes") -> int:
    unit = st.sidebar.selectbox(
        f"{label} unit",
        ("Seconds", "Minutes", "Hours", "Days"),
        key=f"{label}_unit",
        index={"Seconds": 0, "Minutes": 1, "Hours": 2, "Days": 3}[default_unit],
    )
    val = st.sidebar.number_input(
        f"{label} value", 1, step=1, value=default_val, key=f"{label}_val"
    )
    return val * {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86_400}[unit]

INV_SEC   = _interval("Inventory refill", 30)
SHELF_SEC = _interval("Shelf refill",    10)

# session state -----------------------------------------------------------
defaults = dict(
    unified_run=False,
    real_ts=time.time(),
    sim_clock=datetime.now(),
    next_sale_times=[],
    sales_count=0,
    pos_log=[],
    shortage_log=[],
    inv_last_ts=time.time() - INV_SEC,
    inv_cycles=0,
    last_inv_rows=0,
    inv_all_logs=[],
    sh_last_ts=time.time() - SHELF_SEC,
    sh_cycles=0,
    last_sh_rows=0,
    sh_all_logs=[],
)
for k, v in defaults.items():
    st.session_state.setdefault(k, v)

RUN = st.session_state["unified_run"]

# start / stop buttons ----------------------------------------------------
b1, b2 = st.columns(2)
if b1.button("▶ Start", disabled=RUN):
    now = datetime.now()
    st.session_state.clear()
    st.session_state.update(
        unified_run=True,
        real_ts=time.time(),
        sim_clock=now,
        next_sale_times=[now] * CASHIERS,
        sales_count=0,
        pos_log=[],
        shortage_log=[],
        inv_last_ts=time.time() - INV_SEC,
        inv_cycles=0,
        last_inv_rows=0,
        inv_all_logs=[],
        sh_last_ts=time.time() - SHELF_SEC,
        sh_cycles=0,
        last_sh_rows=0,
        sh_all_logs=[],
    )
    RUN = True
if b2.button("⏹ Stop", disabled=not RUN):
    st.session_state.unified_run = False
    RUN = False

# handlers & static catalogue --------------------------------------------
POS   = POSHandler()
INV   = InventoryHandler()
SHELF = SellingAreaHandler()

@st.cache_data(ttl=600, show_spinner=False)
def catalogue() -> pd.DataFrame:
    return POS.fetch_data(
        """
        SELECT itemid, sellingprice, itemnameenglish
          FROM item
         WHERE sellingprice IS NOT NULL AND sellingprice > 0
        """
    )

CAT = catalogue()

# helper fns --------------------------------------------------------------
def random_cart() -> list[dict]:
    n_avail = len(CAT)
    if n_avail == 0:
        return []
    n_items = random.randint(min(min_items, n_avail), min(max_items, n_avail))
    picks = CAT.sample(n=n_items, replace=False)
    return [
        dict(
            itemid=int(r.itemid),
            quantity=random.randint(min_qty, max_qty),
            sellingprice=float(r.sellingprice),
            itemname=str(r.itemnameenglish),
        )
        for _, r in picks.iterrows()
    ]

def base_interval(sim_dt: datetime) -> float:
    if PROFILE.startswith("Standard"):
        return 120.0
    h = sim_dt.hour
    if 6 <= h < 10:  return 180
    if 10 <= h < 14: return 90
    if 14 <= h < 18: return 60
    if 18 <= h < 22: return 40
    return 240

def next_gap(sim_dt: datetime) -> float:
    return base_interval(sim_dt) / SPEED

# inventory cycle ---------------------------------------------------------
def inventory_cycle() -> int:
    snap  = INV.stock_levels()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return 0
    below["need"] = below["average"] - below["totalqty"]
    logs = INV.restock_items_bulk(below[["itemid", "need", "sellingprice"]])["log"]
    st.session_state.inv_all_logs.extend(logs)
    return len(logs)

# shelf cycle -------------------------------------------------------------
def shelf_cycle() -> int:
    below = SHELF.get_items_below_shelfthreshold()
    if below.empty():
        return 0

    USER, DUMMY_SALEID = "AUTO‑UNIFIED", 0
    moved, log_entries = 0, []

    for row in below.itertuples(index=False):
        need = max(row.shelfaverage - row.totalquantity,
                   row.shelfthreshold - row.totalquantity)
        need = SHELF.resolve_shortages(itemid=row.itemid, qty_need=need, user=USER)
        if need <= 0:
            continue

        layers = SHELF.fetch_data(
            """
            SELECT batchid, expirationdate, quantity, cost_per_unit
              FROM inventory
             WHERE itemid = %s AND quantity > 0
          ORDER BY expirationdate, cost_per_unit, batchid
            """,
            (row.itemid,),
        )

        plan = []
        for lyr in layers.itertuples():
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
            SHELF.move_layers_to_shelf(
                itemid=row.itemid,
                layers=plan,
                created_by=USER,
            )
            moved += 1
            log_entries.append(
                dict(
                    itemid=row.itemid,
                    itemname=row.itemname,
                    layers=len(plan),
                    timestamp=datetime.now().strftime("%F %T"),
                )
            )

        if need > 0:
            SHELF.execute_command(
                """
                INSERT INTO shelf_shortage
                      (saleid, itemid, shortage_qty, logged_at)
                VALUES (%s,%s,%s,CURRENT_TIMESTAMP)
                """,
                (DUMMY_SALEID, row.itemid, need),
            )

    st.session_state.sh_all_logs.extend(log_entries)
    return moved      # number of different items refilled

# main loop ---------------------------------------------------------------
if RUN:
    now_real = time.time()
    elapsed  = now_real - st.session_state.real_ts
    st.session_state.real_ts = now_real
    st.session_state.sim_clock += timedelta(seconds=elapsed * SPEED)

    # sales due ------------------------------------------------------------
    pending_sales: List[dict] = []
    for idx, nxt in enumerate(st.session_state.next_sale_times):
        while nxt <= st.session_state.sim_clock:
            cart = random_cart()
            if cart:
                pending_sales.append(
                    dict(
                        cashier=f"CASH{idx+1:02d}",
                        cart_items=cart,
                        discount_rate=0.0,
                        payment_method="Cash",
                        notes=f"[SIM {nxt:%F %T}]",
                    )
                )
            nxt += timedelta(seconds=next_gap(nxt))
        st.session_state.next_sale_times[idx] = nxt

    # process sales --------------------------------------------------------
    if pending_sales:
        try:
            batch_log = POS.process_sales_batch(pending_sales)
            for entry in batch_log:
                st.session_state.sales_count += 1
                st.session_state.pos_log.append(entry)
                for s in entry["shortages"]:
                    st.session_state.shortage_log.append(
                        {**s, "saleid": entry["saleid"], "timestamp": entry["timestamp"]}
                    )
        except Exception:
            POS.conn.rollback()
            st.error("POS batch error:\n" + "".join(traceback.format_exc(limit=1)))

    # inventory / shelf refills -------------------------------------------
    if now_real - st.session_state.inv_last_ts >= INV_SEC:
        try:
            st.session_state.last_inv_rows = inventory_cycle()
            st.session_state.inv_cycles += 1
        except Exception:
            st.error("Inventory error:\n" + "".join(traceback.format_exc(limit=1)))
        st.session_state.inv_last_ts = now_real

    if now_real - st.session_state.sh_last_ts >= SHELF_SEC:
        try:
            st.session_state.last_sh_rows = shelf_cycle()
            st.session_state.sh_cycles += 1
        except Exception:
            st.error("Shelf error:\n" + "".join(traceback.format_exc(limit=1)))
        st.session_state.sh_last_ts = now_real

    # UI -------------------------------------------------------------------
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("POS")
        st.metric("Total sales",  st.session_state.sales_count)
        st.metric("Sim time",     f"{st.session_state.sim_clock:%F %T}")
    with col2:
        st.subheader("Automation")
        st.metric("Inv rows last",   st.session_state.last_inv_rows)
        st.metric("Shelf items refilled", st.session_state.last_sh_rows)

    st.progress(
        (now_real - st.session_state.inv_last_ts) / INV_SEC,
        text="Inventory cycle progress",
    )
    st.progress(
        (now_real - st.session_state.sh_last_ts) / SHELF_SEC,
        text="Shelf cycle progress",
    )

    tab1, tab2, tab3, tab4 = st.tabs(
        ["POS Activity", "Shortages", "Inventory Refill Log", "Shelf Refill Log"]
    )

    with tab1:
        st.subheader("Recent Sales (last 10)")
        if st.session_state.pos_log:
            for entry in reversed(st.session_state.pos_log[-10:]):
                with st.expander(
                    f"Sale {entry['saleid']} at {entry['timestamp']} "
                    f"(Cashier: {entry['cashier']})"
                ):
                    st.dataframe(pd.DataFrame(entry["items"]))
                    if entry["shortages"]:
                        st.write("Shortages:")
                        st.dataframe(pd.DataFrame(entry["shortages"]))
        else:
            st.write("No sales yet.")

    with tab2:
        st.subheader("All Shortages this session")
        st.dataframe(pd.DataFrame(st.session_state.shortage_log)
                     if st.session_state.shortage_log else
                     pd.DataFrame({"info": ["No shortages so far."]}),
                     use_container_width=True)

    with tab3:
        st.subheader("Inventory Auto‑Refill")
        st.dataframe(pd.DataFrame(st.session_state.inv_all_logs)
                     if st.session_state.inv_all_logs else
                     pd.DataFrame({"info": ["No inventory auto‑refills."]}),
                     use_container_width=True)

    with tab4:
        st.subheader("Shelf Auto‑Refill")
        st.dataframe(pd.DataFrame(st.session_state.sh_all_logs)
                     if st.session_state.sh_all_logs else
                     pd.DataFrame({"info": ["No shelf auto‑refills."]}),
                     use_container_width=True)

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Set parameters and press **Start** to launch all processes.")
