from __future__ import annotations
"""
🛒 Unified POS • Inventory • Shelf controller
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

# ─────────────── UI config ───────────────
st.set_page_config(page_title="Unified POS / Refill", page_icon="🛒")
st.title("🛒 POS + Inventory + Shelf automation")

# ─────────────── sidebar – POS params ───────────────
st.sidebar.header("POS parameters")
SPEED   = st.sidebar.number_input("Speed multiplier (×)", 1, 200, 1, 1)
PROFILE = st.sidebar.selectbox("Load profile",
                               ("Standard (steady)", "Real‑time market curve"))
CASHIERS = st.sidebar.slider("Active cashiers", 1, 10, 3)

min_items = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty   = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty   = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

# ─────────────── sidebar – refill intervals ───────────────
st.sidebar.header("Automation intervals")
def pick_interval(label: str, default_val: int, default_unit: str = "Minutes"):
    unit  = st.sidebar.selectbox(f"{label} unit",
                                 ("Seconds", "Minutes", "Hours", "Days"),
                                 key=f"{label}_unit",
                                 index={"Seconds":0,"Minutes":1,
                                        "Hours":2,"Days":3}[default_unit])
    val   = st.sidebar.number_input(f"{label} value",
                                    1, step=1, value=default_val,
                                    key=f"{label}_value")
    sec = val * {"Seconds":1,"Minutes":60,"Hours":3600,"Days":86_400}[unit]
    return sec

INV_SEC   = pick_interval("Inventory refill", 30)
SHELF_SEC = pick_interval("Shelf refill",     10)

# ─────────────── Start / Stop ───────────────
RUN = st.session_state.get("unified_run", False)
c1, c2 = st.columns(2)
if c1.button("▶ Start", disabled=RUN):
    now = datetime.now()
    st.session_state.update(
        unified_run=True,
        # POS
        sim_clock=now,
        real_ts=time.time(),
        next_sale_sim_ts=now,
        sales_count=0,
        # Inventory
        inv_last_ts=time.time() - INV_SEC,
        inv_cycles=0,
        last_inv_rows=0,
        # Shelf
        sh_last_ts=time.time() - SHELF_SEC,
        sh_cycles=0,
        last_sh_rows=0,
    )
    RUN = True
if c2.button("⏹ Stop", disabled=not RUN):
    st.session_state.unified_run = False
    RUN = False

# ─────────────── handlers & static data ───────────────
POS   = POSHandler()
INV   = InventoryHandler()
SHELF = SellingAreaHandler()

@st.cache_data(ttl=600, show_spinner=False)
def item_catalogue() -> pd.DataFrame:
    return POS.fetch_data(
        """
        SELECT itemid, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
        """
    )
CATALOG = item_catalogue()

def random_cart() -> list[dict]:
    n_available = len(CATALOG)
    if n_available == 0:
        return []
    n_items = random.randint(min(min_items, n_available),
                             min(max_items, n_available))
    picks = CATALOG.sample(n=n_items, replace=False)
    return [
        dict(itemid=int(r.itemid),
             quantity=random.randint(min_qty, max_qty),
             sellingprice=float(r.sellingprice))
        for _, r in picks.iterrows()
    ]

def base_interval(sim_dt: datetime) -> float:
    if PROFILE.startswith("Standard"):
        return 120.0
    h = sim_dt.hour
    if 6 <= h < 10:  return 180
    if 10 <= h < 14: return  90
    if 14 <= h < 18: return  60
    if 18 <= h < 22: return  40
    return 240

def next_interval(sim_dt: datetime) -> float:
    """Each cashier is an independent sale generator."""
    return base_interval(sim_dt) / (SPEED * CASHIERS)   # ← FIX

# ─────────────── Inventory helpers ───────────────
def inventory_cycle() -> int:
    snap = INV.stock_levels()
    below = snap[snap.totalqty < snap.threshold].copy()
    if below.empty:
        return 0
    below["need"] = below["average"] - below["totalqty"]
    log = INV.restock_items_bulk(below[["itemid", "need", "sellingprice"]])
    return len(log)

# ─────────────── Shelf helpers ───────────────
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

# ─────────────── POS helpers ───────────────
def process_one_sale(sim_dt: datetime):
    cart = random_cart()
    if not cart:
        return
    cashier_id = f"CASH{random.randint(1, CASHIERS):02d}"
    try:
        saleid, _ = POS.process_sale_with_shortage(
            cart_items     = cart,
            discount_rate  = 0.0,
            payment_method = "Cash",
            cashier        = cashier_id,
            notes          = f"[SIM {sim_dt:%F %T}]",
        )
        if saleid:
            st.session_state.sales_count += 1
    except Exception:
        POS.conn.rollback()

# ─────────────── MAIN LOOP ───────────────
if RUN:
    now_real = time.time()
    elapsed  = now_real - st.session_state.real_ts
    st.session_state.real_ts = now_real

    # advance simulated clock
    st.session_state.sim_clock += timedelta(seconds=elapsed * SPEED)

    # generate due sales
    while st.session_state.next_sale_sim_ts <= st.session_state.sim_clock:
        process_one_sale(st.session_state.next_sale_sim_ts)
        gap = timedelta(seconds=next_interval(st.session_state.next_sale_sim_ts))
        st.session_state.next_sale_sim_ts += gap

    # inventory refill
    if now_real - st.session_state.inv_last_ts >= INV_SEC:
        try:
            rows = inventory_cycle()
            st.session_state.last_inv_rows = rows
            st.session_state.inv_cycles   += 1
        except Exception:
            st.error("Inventory error:\n" +
                     "".join(traceback.format_exc(limit=1)))
        st.session_state.inv_last_ts = now_real

    # shelf refill
    if now_real - st.session_state.sh_last_ts >= SHELF_SEC:
        try:
            moved = shelf_cycle()
            st.session_state.last_sh_rows = moved
            st.session_state.sh_cycles   += 1
        except Exception:
            st.error("Shelf error:\n" +
                     "".join(traceback.format_exc(limit=1)))
        st.session_state.sh_last_ts = now_real

    # ─────────── metrics ───────────
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("POS")
        st.metric("Total sales", st.session_state.sales_count)
        st.metric("Sim time",
                  f"{st.session_state.sim_clock:%F %T}")
    with col2:
        st.subheader("Automation")
        st.metric("Inv rows last",   st.session_state.last_inv_rows)
        st.metric("Shelf qty moved", st.session_state.last_sh_rows)

    # progress bars
    st.progress((now_real - st.session_state.inv_last_ts) / INV_SEC,
                text="Inventory cycle progress")
    st.progress((now_real - st.session_state.sh_last_ts) / SHELF_SEC,
                text="Shelf cycle progress")

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Fill parameters, then press **Start** to launch all processes.")
