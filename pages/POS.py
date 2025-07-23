from __future__ import annotations
"""
🛒 POS  +  Inventory  +  Shelf automation – parallel cashiers
"""

import time
import random
import traceback
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from handler.POS_handler import POSHandler
from handler.inventory_handler import InventoryHandler
from handler.selling_area_handler import SellingAreaHandler

# ───────────── UI CONFIG ─────────────
st.set_page_config(page_title="Unified POS / Refill", page_icon="🛒")
st.title("🛒 POS + Inventory + Shelf automation")

# ───────────── SIDEBAR – POS PARAMS ─────────────
st.sidebar.header("POS parameters")
SPEED     = st.sidebar.number_input("Speed multiplier (×)", 1, 200, 1, 1)
PROFILE   = st.sidebar.selectbox(
    "Load profile", ("Standard (steady)", "Real‑time market curve")
)
CASHIERS  = st.sidebar.slider("Active cashiers", 1, 10, 3)

min_items = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty   = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty   = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

# ───────────── SIDEBAR – REFILL INTERVALS ─────────────
st.sidebar.header("Automation intervals")
def _interval(label: str, default_val: int, default_unit="Minutes") -> int:
    unit  = st.sidebar.selectbox(f"{label} unit",
                                 ("Seconds", "Minutes", "Hours", "Days"),
                                 key=f"{label}_unit",
                                 index={"Seconds":0,"Minutes":1,"Hours":2,"Days":3}[default_unit])
    val   = st.sidebar.number_input(f"{label} value", 1, step=1,
                                    value=default_val, key=f"{label}_val")
    return val * {"Seconds":1, "Minutes":60, "Hours":3600, "Days":86_400}[unit]

INV_SEC   = _interval("Inventory refill", 30)
SHELF_SEC = _interval("Shelf refill",     10)

# ───────────── START / STOP ─────────────
RUN = st.session_state.get("unified_run", False)
b1, b2 = st.columns(2)
if b1.button("▶ Start", disabled=RUN):
    now = datetime.now()
    st.session_state.clear()         # clean slate
    st.session_state.update(
        unified_run=True,
        # global real‑time anchor
        real_ts=time.time(),
        # POS clocks
        sim_clock=now,
        next_sale_times=[now] * CASHIERS,   # one timer per cashier
        sales_count=0,
        # Inventory timers
        inv_last_ts=time.time() - INV_SEC,
        inv_cycles=0,  last_inv_rows=0,
        # Shelf timers
        sh_last_ts=time.time() - SHELF_SEC,
        sh_cycles=0,   last_sh_rows=0,
    )
    RUN = True
if b2.button("⏹ Stop", disabled=not RUN):
    st.session_state.unified_run = False
    RUN = False

# ───────────── HANDLERS & STATIC DATA ─────────────
POS   = POSHandler()
INV   = InventoryHandler()
SHELF = SellingAreaHandler()

@st.cache_data(ttl=600, show_spinner=False)
def catalogue() -> pd.DataFrame:
    return POS.fetch_data(
        "SELECT itemid, sellingprice FROM item "
        "WHERE sellingprice IS NOT NULL AND sellingprice > 0"
    )
CAT = catalogue()

# ───────────── SMALL HELPERS ─────────────
def random_cart() -> list[dict]:
    n_avail = len(CAT)
    if n_avail == 0:
        return []
    n_items = random.randint(min(min_items, n_avail),
                             min(max_items, n_avail))
    picks = CAT.sample(n=n_items, replace=False)
    return [
        dict(itemid=int(r.itemid),
             quantity=random.randint(min_qty, max_qty),
             sellingprice=float(r.sellingprice))
        for _, r in picks.iterrows()
    ]

def base_interval(sim_dt: datetime) -> float:           # seconds
    if PROFILE.startswith("Standard"):
        return 120.0
    h = sim_dt.hour
    if 6 <= h < 10:  return 180
    if 10 <= h < 14: return  90
    if 14 <= h < 18: return  60
    if 18 <= h < 22: return  40
    return 240

def next_gap(sim_dt: datetime) -> float:
    """Gap *per cashier* (global rate multiplies by CASHIERS)."""
    return base_interval(sim_dt) / SPEED

def process_sale(cashier_idx: int, sim_dt: datetime):
    cart = random_cart()
    if not cart:
        return
    cid = f"CASH{cashier_idx+1:02d}"
    try:
        saleid, _ = POS.process_sale_with_shortage(
            cart_items=cart,
            discount_rate=0.0,
            payment_method="Cash",
            cashier=cid,
            notes=f"[SIM {sim_dt:%F %T}]",
        )
        if saleid:
            st.session_state.sales_count += 1
    except Exception:
        POS.conn.rollback()

# ───────────── INVENTORY & SHELF CYCLES (unchanged) ─────────────
def inventory_cycle() -> int:
    snap = INV.stock_levels()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return 0
    below["need"] = below["average"] - below["totalqty"]
    return len(INV.restock_items_bulk(below[["itemid", "need", "sellingprice"]]))

def shelf_cycle() -> int:
    meta = SHELF.get_all_items().set_index("itemid")
    kpi  = SHELF.get_shelf_quantity_by_item().set_index("itemid")
    df   = meta.join(kpi, how="left").fillna({"totalquantity": 0})
    df["totalquantity"] = df.totalquantity.astype(int)
    below = df[df.totalquantity < df.shelfthreshold]
    moved = 0
    for itemid, row in below.iterrows():
        thresh, avg, current = row.shelfthreshold, row.shelfaverage, row.totalquantity
        need = max(avg - current, thresh - current)
        need = SHELF.resolve_shortages(itemid=itemid, qty_need=need,
                                       user="AUTO‑UNIFIED")
        if need <= 0:
            continue
        layers = SHELF.fetch_data(
            """
            SELECT expirationdate, quantity, cost_per_unit
              FROM inventory
             WHERE itemid=%s AND quantity>0
          ORDER BY expirationdate, cost_per_unit
            """,
            (itemid,),
        )
        for lyr in layers.itertuples():
            take = min(need, int(lyr.quantity))
            SHELF.transfer_from_inventory(
                itemid=itemid,
                expirationdate=lyr.expirationdate,
                quantity=take,
                cost_per_unit=float(lyr.cost_per_unit),
                created_by="AUTO‑UNIFIED",
            )
            need  -= take
            moved += take
            if need == 0:
                break
        if need > 0:
            SHELF.execute_command(
                """
                INSERT INTO shelf_shortage
                      (saleid, itemid, shortage_qty, logged_at)
                VALUES (0,%s,%s,CURRENT_TIMESTAMP)
                """,
                (itemid, need),
            )
    return moved

# ───────────── MAIN LOOP ─────────────
if RUN:
    now_real = time.time()
    elapsed  = now_real - st.session_state.real_ts
    st.session_state.real_ts = now_real
    st.session_state.sim_clock += timedelta(seconds=elapsed * SPEED)

    # ----- PER‑CASHIER SCHEDULING -----
    for idx, nxt in enumerate(st.session_state.next_sale_times):
        while nxt <= st.session_state.sim_clock:
            process_sale(idx, nxt)
            nxt += timedelta(seconds=next_gap(nxt))
        st.session_state.next_sale_times[idx] = nxt  # update back

    # ----- INVENTORY REFILL -----
    if now_real - st.session_state.inv_last_ts >= INV_SEC:
        try:
            st.session_state.last_inv_rows = inventory_cycle()
            st.session_state.inv_cycles   += 1
        except Exception:
            st.error("Inventory error:\n" +
                     "".join(traceback.format_exc(limit=1)))
        st.session_state.inv_last_ts = now_real

    # ----- SHELF REFILL -----
    if now_real - st.session_state.sh_last_ts >= SHELF_SEC:
        try:
            st.session_state.last_sh_rows = shelf_cycle()
            st.session_state.sh_cycles   += 1
        except Exception:
            st.error("Shelf error:\n" +
                     "".join(traceback.format_exc(limit=1)))
        st.session_state.sh_last_ts = now_real

    # ─────────── METRICS ───────────
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("POS")
        st.metric("Total sales", st.session_state.sales_count)
        st.metric("Sim time", f"{st.session_state.sim_clock:%F %T}")
    with col2:
        st.subheader("Automation")
        st.metric("Inv rows last",   st.session_state.last_inv_rows)
        st.metric("Shelf qty moved", st.session_state.last_sh_rows)

    st.progress((now_real - st.session_state.inv_last_ts) / INV_SEC,
                text="Inventory cycle progress")
    st.progress((now_real - st.session_state.sh_last_ts) / SHELF_SEC,
                text="Shelf cycle progress")

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Set parameters and press **Start** to launch all processes.")
