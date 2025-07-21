from __future__ import annotations
"""
POS Simulation (continuous)
───────────────────────────
Live supermarket checkout with adjustable speed/load and 1‑10 cashiers.
Runs until you hit **Stop**.  CASH‑only; does **not** refill shelves.
"""

import time
from datetime import datetime, timedelta
import random
import json

import streamlit as st
import pandas as pd
import psycopg2

from handler.POS_handler import POSHandler     # renamed handler

# ───────────────────── page & sidebar ─────────────────────
st.set_page_config(page_title="POS Simulation", page_icon="🛒")

st.sidebar.header("🛠 Live‑POS Controls")

SPEED      = st.sidebar.number_input("Speed multiplier (×)", 1, 100, 1, 1)
LOAD_MODE  = st.sidebar.selectbox(
    "Load profile",
    ("Standard (steady)", "Real‑time market curve"),
)
CASHIERS   = st.sidebar.slider("Active cashiers", 1, 10, 3)

# Item‑mix parameters
min_items  = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items  = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty    = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty    = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

# ───── Start / Stop buttons ─────
RUNNING = st.session_state.get("pos_running", False)
col_run, col_stop = st.columns(2)
if col_run.button("▶ Start" if not RUNNING else "⏸ Resume", disabled=RUNNING):
    st.session_state.update(
        pos_running=True,
        sim_clock=datetime.now(),
        real_ts=time.time(),
        next_sale_sim_ts=datetime.now(),
        sales_log=[],
    )
    RUNNING = True

if col_stop.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["pos_running"] = False
    RUNNING = False

# ─────────── helpers ───────────
cashier = POSHandler()

@st.cache_data(ttl=600, show_spinner=False)
def item_catalogue() -> pd.DataFrame:
    return cashier.fetch_data(
        """
        SELECT itemid, itemnameenglish AS itemname, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
        """
    )

CATALOGUE = item_catalogue()

def random_cart() -> list[dict]:
    n_items = random.randint(min_items, min(max_items, len(CATALOGUE)))
    picks   = CATALOGUE.sample(n=n_items, replace=False)
    return [
        dict(itemid=int(r.itemid),
             quantity=random.randint(min_qty, max_qty),
             sellingprice=float(r.sellingprice))
        for _, r in picks.iterrows()
    ]

# Sale‑interval helpers
def interval_standard() -> float:                 # seconds
    return 120.0

def interval_real_time(sim_dt: datetime) -> float:
    h = sim_dt.hour
    if 6 <= h < 10:
        base = 180
    elif 10 <= h < 14:
        base = 90
    elif 14 <= h < 18:
        base = 60
    elif 18 <= h < 22:
        base = 40
    else:
        base = 240
    return base

def next_interval(sim_dt: datetime) -> float:
    base = interval_standard() if LOAD_MODE.startswith("Standard") \
           else interval_real_time(sim_dt)
    return base / SPEED

def sync_sequences() -> None:
    for tbl, pk in (("sales", "saleid"), ("salesitems", "salesitemid")):
        cashier.execute_command(
            f"""
            SELECT setval(pg_get_serial_sequence('{tbl}','{pk}'),
                          COALESCE((SELECT MAX({pk}) FROM {tbl}),0)+1,
                          false)
            """
        )

def process_one_sale(sim_dt: datetime):
    cart = random_cart()
    cashier_id = f"CASH{random.randint(1, CASHIERS):02d}"
    try:
        saleid, _ = cashier.process_sale_with_shortage(
            cart_items     = cart,
            discount_rate  = 0.0,
            payment_method = "Cash",
            cashier        = cashier_id,
            notes          = f"[SIM {sim_dt:%F %T}]",
        )
        status = f"✅ #{saleid}"
    except psycopg2.errors.UniqueViolation:
        cashier.conn.rollback()
        sync_sequences()
        status, saleid = "Retry fail", None
    except Exception as e:
        cashier.conn.rollback()
        status, saleid = f"Error: {e}", None

    st.session_state["sales_log"].append(
        dict(ts=sim_dt, saleid=saleid, cashier=cashier_id,
             items=len(cart), status=status)
    )

# ─────────── simulation loop ───────────
if RUNNING:
    # advance simulated time based on real elapsed seconds
    now_real  = time.time()
    elapsed   = now_real - st.session_state["real_ts"]
    st.session_state["sim_clock"] += timedelta(seconds=elapsed * SPEED)
    st.session_state["real_ts"]    = now_real

    # generate any sales that fell within the advanced window
    while st.session_state["next_sale_sim_ts"] <= st.session_state["sim_clock"]:
        process_one_sale(st.session_state["next_sale_sim_ts"])
        gap = timedelta(seconds=next_interval(st.session_state["next_sale_sim_ts"]))
        st.session_state["next_sale_sim_ts"] += gap

# ─────────── live feed ───────────
st.header("Live feed")
df = pd.DataFrame(st.session_state.get("sales_log", []))
if df.empty:
    st.info("No sales yet – click **Start**.")
else:
    st.dataframe(
        df[["ts", "saleid", "cashier", "items", "status"]]
          .sort_values("ts", ascending=False)
          .reset_index(drop=True),
        use_container_width=True, height=400
    )

# Auto‑refresh the page every 1 s while running
if RUNNING and hasattr(st, "autorefresh"):
    st.autorefresh(interval=1000, key="pos_loop_refresh")
