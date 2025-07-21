# app.py
import streamlit as st
from datetime import datetime
from utils.sim_toggle_persist import sidebar_switch   # persistent switch

# ───────────────────── page config ─────────────────────
st.set_page_config(
    page_title="AMAS POS Test Suite",
    page_icon="🛒",
    layout="centered",
)

# ─────────────── sidebar: global switch ────────────────
sim_active = sidebar_switch()        # adds toggle & returns current state

# ───────────────────── main area ───────────────────────
st.title("🛒 AMAS POS – Test Console")

st.markdown(
    """
    Welcome to the **AMAS POS QA console**.

    * **“POS”** page simulates a *live supermarket* checkout – adjustable
      speed, load profiles, and number of cashiers.  
      It keeps running until you hit **Stop**.

    * **Shelf** and **Inventory** pages run their own passive auto‑refill
      loops.  
      They respect the same sidebar **Simulators running** switch.

    * All simulated sales are tagged in **`sales.notes`** so you can filter or purge
      them later.

    * Database sequences stay in sync automatically.
    """
)

status_txt = "ACTIVE ✅" if sim_active else "PAUSED ⏸️"
st.success(f"Simulators are **{status_txt}**")

st.info(
    f"💡 Current time: **{datetime.now():%Y-%m-%d %H:%M:%S}**\n\n"
    "Pick a page on the left to start or monitor simulations."
)
