from __future__ import annotations
"""
POS Simulation – continuous
Press **Start** to begin; it will run until you press **Stop**.
"""

import time
from datetime import datetime, timedelta
import random

import streamlit as st
import pandas as pd
import psycopg2

from handler.POS_handler import POSHandler

# ───────────────────── page setup ─────────────────────
st.set_page_config(page_title="POS Simulation", page_icon="🛒")

st.title("🛒 POS Simulation")
st.caption("Press **Start** to begin. The simulator keeps running until you "
           "click **Stop**.")

# ───────────── sidebar controls ─────────────
st.sidebar.header("Parameters")
SPEED      = st.sidebar.number_input("Speed multiplier (×)", 1, 200, 1, 1)
LOAD_MODE  = st.sidebar.selectbox(
    "Load profile",
    ("Standard (steady)", "Real‑time market curve"),
)
CASHIERS   = st.sidebar.slider("Active cashiers", 1, 10, 3)

min_items  = st.sidebar.number_input("Min items / sale", 1, 20, 2)
max_items  = st.sidebar.number_input("Max items / sale", min_items, 30, 6)
min_qty    = st.sidebar.number_input("Min qty / item", 1, 20, 1)
max_qty    = st.sidebar.number_input("Max qty / item", min_qty, 50, 5)

# ───────────── Start / Stop ─────────────
RUNNING = st.session_state.get("pos_running", False)
if st.button("▶ Start", disabled=RUNNING):
    st.session_state.update(
        pos_running=True,
        sim_clock=datetime.now(),
        real_ts=time.time(),
        next_sale_sim_ts=datetime.now(),
        sales_count=0,
    )
    RUNNING = True

if st.button("⏹ Stop", disabled=not RUNNING):
    st.session_state["pos_running"] = False
    RUNNING = False

# ───────────── helpers ─────────────
cashier = POSHandler()
cashier.execute_command("SELECT 1")            # ensure connection

def _sync_sequences_once() -> None:
    """Bring Postgres sequences in line with current max(pk)."""
    for tbl, pk in (("sales", "saleid"), ("salesitems", "salesitemid")):
        cashier.execute_command(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{tbl}','{pk}'),
                COALESCE((SELECT MAX({pk}) FROM {tbl}), 0) + 1,
                false
            )
            """
        )

_sync_sequences_once()

@st.cache_data(ttl=600, show_spinner=False)
def item_catalogue() -> pd.DataFrame:
    return cashier.fetch_data(
        """
        SELECT itemid, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
        """
    )

CATALOGUE = item_catalogue()

def random_cart() -> list[dict]:
    """Return a random basket respecting sidebar constraints
    and current catalogue size."""
    available = len(CATALOGUE)
    if available == 0:
        return []

    low  = min(min_items, available)
    high = min(max_items, available)
    n_items = random.randint(low, high)

    picks   = CATALOGUE.sample(n=n_items, replace=False)
    return [
        dict(itemid=int(r.itemid),
             quantity=random.randint(min_qty, max_qty),
             sellingprice=float(r.sellingprice))
        for _, r in picks.iterrows()
    ]

def base_interval(sim_dt: datetime) -> float:            # seconds
    if LOAD_MODE.startswith("Standard"):
        return 120.0
    h = sim_dt.hour
    if 6 <= h < 10:
        return 180
    if 10 <= h < 14:
        return  90
    if 14 <= h < 18:
        return  60
    if 18 <= h < 22:
        return  40
    return 240

def next_interval(sim_dt: datetime) -> float:
    return base_interval(sim_dt) / SPEED

def sync_sequences() -> None:
    """Re‑sync sequences after a UniqueViolation, then continue."""
    for tbl, pk in (("sales", "saleid"), ("salesitems", "salesitemid")):
        cashier.execute_command(
            f"""
            SELECT setval(pg_get_serial_sequence('{tbl}','{pk}'),
                          COALESCE((SELECT MAX({pk}) FROM {tbl}),0)+1,
                          false)
            """
        )

def process_one_sale(sim_dt: datetime):
    cart        = random_cart()
    if not cart:                     # catalogue might be empty
        return
    cashier_id  = f"CASH{random.randint(1, CASHIERS):02d}"

    success = False
    for attempt in range(2):         # at most one retry
        try:
            saleid, _ = cashier.process_sale_with_shortage(
                cart_items     = cart,
                discount_rate  = 0.0,
                payment_method = "Cash",
                cashier        = cashier_id,
                notes          = f"[SIM {sim_dt:%F %T}]",
            )
            success = saleid is not None
            break
        except psycopg2.errors.UniqueViolation:
            cashier.conn.rollback()
            sync_sequences()         # repair sequences, then retry once
        except Exception:
            cashier.conn.rollback()
            break

    if success:
        st.session_state["sales_count"] += 1

# ───────────── simulation loop ─────────────
if RUNNING:
    # advance simulated clock
    now_real  = time.time()
    elapsed   = now_real - st.session_state["real_ts"]
    st.session_state["sim_clock"] += timedelta(seconds=elapsed * SPEED)
    st.session_state["real_ts"]    = now_real

    # generate due sales
    while st.session_state["next_sale_sim_ts"] <= st.session_state["sim_clock"]:
        process_one_sale(st.session_state["next_sale_sim_ts"])
        gap = timedelta(seconds=next_interval(st.session_state["next_sale_sim_ts"]))
        st.session_state["next_sale_sim_ts"] += gap

    # status
    st.metric("Total sales", st.session_state["sales_count"])
    st.metric("Simulated time", f"{st.session_state['sim_clock']:%F %T}")

    # yield control briefly, then rerun
    time.sleep(0.2)
    st.rerun()

else:
    st.info("Click **Start** to begin the simulation.")
