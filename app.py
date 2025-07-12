import streamlit as st
from datetime import datetime

st.set_page_config(
    page_title="AMAS POS Test Suite",
    page_icon="🛒",
    layout="centered",
)

st.title("🛒 AMAS POS – Test Console")
st.markdown(
    """
    Welcome to the **AMAS POS QA console**.

    * Use the sidebar to open **“simulate”** and generate random bulk sales
      that run through the exact cashier logic.
    * All simulated sales are tagged with **`[BULK TEST]`** in the
      `sales.notes` column so you can filter or purge them later.
    * Sequences are auto-synced, and any duplicate-key issues
      self-heal during the run.
    """
)

st.info(
    f"💡 Current time: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
    "Pick a page on the left to begin testing."
)
