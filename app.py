# app.py
import streamlit as st
from datetime import datetime

# ───────────────────── page config ─────────────────────
st.set_page_config(
    page_title="AMAS POS Test Suite",
    page_icon="🛒",
    layout="centered",
)

# ─────────────── sidebar: global switch ────────────────
st.sidebar.header("⚙️  Admin controls")

# Create the flag once; default = ON
if "sim_active" not in st.session_state:
    st.session_state["sim_active"] = True

st.sidebar.toggle(
    "Simulators running",
    key="sim_active",
    help=(
        "Turn OFF to pause Auto‑POS, Shelf‑refill, and "
        "Inventory‑refill loops.  "
        "Turn ON to resume."
    ),
)

# ───────────────────── main area ───────────────────────
st.title("🛒 AMAS POS – Test Console")
st.markdown(
    """
    Welcome to the **AMAS POS QA console**.

    * Use the sidebar pages to **simulate** random bulk sales that flow through
      the full cashier logic.
    * All simulated sales are tagged with **`[BULK TEST]`** in `sales.notes`
      so they can be filtered or purged later.
    * Database sequences are auto‑synced; duplicate‑key issues self‑heal.
    """
)

status = "ACTIVE ✅" if st.session_state["sim_active"] else "PAUSED ⏸️"
st.success(f"Simulators are **{status}**")

st.info(
    f"💡 Current time: **{datetime.now():%Y-%m-%d %H:%M:%S}**\n\n"
    "Pick a page on the left to begin testing."
)
