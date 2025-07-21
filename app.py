import streamlit as st
from datetime import datetime

# ───────────────────── page config ─────────────────────
st.set_page_config(
    page_title="AMAS POS Test Suite",
    page_icon="🛒",
    layout="centered",
)

# ───────────────────── main area ───────────────────────
st.title("🛒 AMAS POS – Test Console")

st.markdown(
    """
    Welcome to the **AMAS POS QA console**.

    * **POS** page simulates a live supermarket checkout – adjustable speed,
      load profiles, and 1‑10 cashiers.  
      It keeps running until you hit **Stop**.

    * **Shelf** and **Inventory** pages run passive auto‑refill loops.

    * All simulated sales are tagged in **`sales.notes`** so you can filter or
      purge them later.  Database sequences self‑heal automatically.
    """
)

st.info(
    f"💡 Current time: **{datetime.now():%Y-%m-%d %H:%M:%S}**\n\n"
    "Pick a page on the left to start or monitor simulations."
)
