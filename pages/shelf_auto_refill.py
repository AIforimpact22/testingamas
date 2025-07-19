# pages/shelf_auto_refill.py
"""
Shelf Refill Monitor (auto‑refresh)
-----------------------------------
Displays live shelf KPIs and open shortages.
Refill work itself happens inside handler.shelf_handler.post_sale_restock().
"""

from __future__ import annotations

import streamlit as st
import pandas as pd

from handler.shelf_handler import ShelfHandler

shelf = ShelfHandler()

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def fetch_kpis() -> pd.DataFrame:
    return shelf.get_shelf_quantity_by_item()

@st.cache_data(ttl=5, show_spinner=False)
def fetch_open_shortages() -> pd.DataFrame:
    return shelf.fetch_data(
        """
        SELECT i.itemnameenglish AS itemname,
               ss.shortage_qty,
               ss.logged_at
        FROM   shelf_shortage ss
        JOIN   item i ON i.itemid = ss.itemid
        WHERE  ss.resolved = FALSE
        ORDER  BY ss.logged_at;
        """
    )

# ───────────────────────── UI ──────────────────────────────
st.set_page_config(page_title="Shelf Refill Monitor", page_icon="📈", layout="wide")
st.title("📈 Shelf Refill Monitor")

# auto‑refresh every 5 s (Streamlit ≥ 1.33)
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=5000, key="refill_monitor")

kpis = fetch_kpis()
if kpis.empty:
    st.info("Shelf table empty.")
    st.stop()

kpis["below_threshold"] = kpis["totalquantity"] < kpis["shelfthreshold"].fillna(0)

low_df       = kpis[kpis["below_threshold"]]
shortages_df = fetch_open_shortages()

col1, col2 = st.columns(2)
col1.metric("Total SKUs on shelf", len(kpis))
col1.metric("Below threshold", len(low_df))
col2.metric("Open shortage tickets", len(shortages_df))

st.subheader("Shelf stock vs. thresholds")
st.dataframe(
    kpis[["itemname", "totalquantity", "shelfthreshold", "shelfaverage"]]
        .sort_values("itemname"),
    use_container_width=True
)

if not low_df.empty:
    st.subheader("⚠️ Items needing attention")
    st.dataframe(
        low_df[["itemname", "totalquantity", "shelfthreshold", "shelfaverage"]],
        use_container_width=True
    )

if not shortages_df.empty:
    st.subheader("🚨 Open shortages")
    st.dataframe(shortages_df, use_container_width=True)
