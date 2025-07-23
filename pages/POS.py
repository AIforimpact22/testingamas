from __future__ import annotations
"""
🛒 POS Simulation – stand‑alone (cashier multiplier fixed)
"""

import time
import random
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import psycopg2

from handler.POS_handler import POSHandler

# ───────────── page setup ─────────────
st.set_page_config(page_title="POS Simulation", page_icon="🛒")
st.title("🛒 POS Simulation")

st.sidebar.header("Parameters")
SPEED     = st.sidebar.number_input("Speed multiplier (×)", 1, 200, 1, 1)
PROFILE   = st.sidebar.selectbox("Load profile",
                                 ("Standard (steady)", "Real‑time market curve"))
CASHIERS  = st.sidebar.slider("Active cashiers", 1, 10, 3)

min_items = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty   = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty   = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

RUN = st.session_state.get("pos_run", False)
if st.button("▶ Start", disabled=RUN):
    now = datetime.now()
    st.session_state.update(pos_run=True,
                            sim_clock=now,
                            real_ts=time.time(),
                            next_sale_sim_ts=now,
                            sales_count=0)
    RUN = True
if st.button("⏹ Stop", disabled=not RUN):
    st.session_state.pos_run = False
    RUN = False

# ───────────── helpers ─────────────
pos = POSHandler()

@st.cache_data(ttl=600, show_spinner=False)
def catalogue() -> pd.DataFrame:
    return pos.fetch_data(
        "SELECT itemid, sellingprice FROM item "
        "WHERE sellingprice IS NOT NULL AND sellingprice > 0"
    )
CAT = catalogue()

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
    """Each cashier = one independent queue."""
    return base_interval(sim_dt) / (SPEED * CASHIERS)   # ← FIX

def process_one_sale(sim_dt: datetime):
    cart = random_cart()
    if not cart:
        return
    cid = f"CASH{random.randint(1, CASHIERS):02d}"
    for _ in range(2):   # at most one retry
        try:
            pos.process_sale_with_shortage(
                cart_items     = cart,
                discount_rate  = 0.0,
                payment_method = "Cash",
                cashier        = cid,
                notes          = f"[SIM {sim_dt:%F %T}]",
            )
            st.session_state.sales_count += 1
            break
        except psycopg2.errors.UniqueViolation:
            pos.conn.rollback()
            # resync sequences then retry once
            for tbl, pk in (("sales","saleid"), ("salesitems","salesitemid")):
                pos.execute_command(
                    f"""
                    SELECT setval(pg_get_serial_sequence('{tbl}','{pk}'),
                                  COALESCE((SELECT MAX({pk}) FROM {tbl}),0)+1,
                                  false)
                    """
                )
        except Exception:
            pos.conn.rollback()
            break

# ───────────── loop ─────────────
if RUN:
    now_real = time.time()
    elapsed  = now_real - st.session_state.real_ts
    st.session_state.real_ts = now_real
    st.session_state.sim_clock += timedelta(seconds=elapsed * SPEED)

    while st.session_state.next_sale_sim_ts <= st.session_state.sim_clock:
        process_one_sale(st.session_state.next_sale_sim_ts)
        gap = timedelta(seconds=next_interval(st.session_state.next_sale_sim_ts))
        st.session_state.next_sale_sim_ts += gap

    st.metric("Total sales", st.session_state.sales_count)
    st.metric("Sim time",
              f"{st.session_state.sim_clock:%F %T}")

    time.sleep(0.2)
    st.rerun()
else:
    st.info("Click **Start** to begin the simulation.")
