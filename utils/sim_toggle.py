# utils/sim_toggle.py
import streamlit as st

def sidebar_switch() -> bool:
    """Add / reuse the global 'sim_active' toggle and return its value."""
    st.sidebar.header("⚙️ Admin controls")
    if "sim_active" not in st.session_state:
        st.session_state["sim_active"] = True
    st.sidebar.toggle(
        "Simulators running",
        key="sim_active",
        help="Turn OFF to pause all auto simulators; ON to resume.",
    )
    return st.session_state["sim_active"]
