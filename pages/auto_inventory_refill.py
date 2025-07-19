# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Monitor
─────────────────────────────
• Polls current inventory every 5 seconds.
• When an item’s total quantity drops below its `inventorythreshold`
  (column in `item` table; fallback = 50), it creates a synthetic PO and
  inserts stock to bring the count back up to `inventoryaverage`
  (fallback = inventorythreshold × 2).
"""

from __future__ import annotations
from datetime import date
import streamlit as st
import pandas as pd

from handler.receive_handler import ReceiveHandler

rh = ReceiveHandler()

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def get_stock_levels() -> pd.DataFrame:
    """
    Return current inventory totals merged with item‑level thresholds.
    Expects optional columns `inventorythreshold` & `inventoryaverage`
    in the `item` table. Falls back to 50 / 100 if NULL / missing.
    """
    inv = rh.fetch_data(
        """
        SELECT itemid, SUM(quantity) AS totalqty
        FROM   inventory
        GROUP  BY itemid;
        """
    )

    meta = rh.fetch_data(
        """
        SELECT itemid,
               itemnameenglish,
               COALESCE(inventorythreshold, 50) AS inventorythreshold,
               COALESCE(inventoryaverage, 100)  AS inventoryaverage
        FROM   item;
        """
    )

    df = meta.merge(inv, on="itemid", how="left")
    df["totalqty"] = df["totalqty"].fillna(0).astype(int)
    return df


def restock_item(row: pd.Series, supplier_id: int) -> None:
    """
    Bring `row.itemid` up to `inventoryaverage`. Uses ReceiveHandler
    helpers to create a synthetic PO and insert inventory.
    """
    need  = int(row.inventoryaverage) - int(row.totalqty)
    if need <= 0:
        return

    # 1⃣ Create synthetic PO header (status='Completed')
    poid = rh.create_manual_po(supplier_id, note="AUTO INVENTORY REFILL")

    # 2⃣ Add PO line & cost row (zero cost for simulation)
    rh.add_po_item(poid, int(row.itemid), need, 0.0)
    costid = rh.insert_poitem_cost(
        poid, int(row.itemid), 0.0, need, note="Auto‑refill"
    )

    # 3⃣ Insert inventory layer (today’s date, dummy location 'AUTO')
    rh.add_items_to_inventory([{
        "item_id"         : int(row.itemid),
        "quantity"        : need,
        "expiration_date" : date.today(),     # fresh stock
        "storage_location": "AUTO",
        "cost_per_unit"   : 0.0,
        "poid"            : poid,
        "costid"          : costid,
    }])

    rh.refresh_po_total_cost(poid)  # remains 0.0

# ───────────────────────── UI & loop ───────────────────────
st.set_page_config(page_title="Auto‑Inventory Refill", page_icon="📦",
                   layout="wide")
st.title("📦 Auto‑Inventory Refill Monitor")

# pick a supplier for synthetic POs
suppliers = rh.get_suppliers()
if suppliers.empty:
    st.error("No suppliers found – add at least one supplier first.")
    st.stop()

supplier_map = dict(zip(suppliers.suppliername, suppliers.supplierid))
default_supplier = suppliers.suppliername.iloc[0]

supplier_name = st.selectbox("Supplier used for auto‑refills",
                             list(supplier_map.keys()),
                             index=list(supplier_map.keys()).index(
                                 default_supplier))
supplier_id = supplier_map[supplier_name]

# autorefresh (Streamlit ≥ 1.33)
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=5000, key="inv_refill_refresh")

# fetch current data
stock_df = get_stock_levels()

# decide which items need refill
need_df = stock_df[stock_df.totalqty < stock_df.inventorythreshold]

st.metric("Items below threshold", len(need_df))
st.metric("Total SKUs", len(stock_df))

if not need_df.empty:
    st.subheader("Triggering auto‑refill for:")
    st.dataframe(
        need_df[["itemnameenglish", "totalqty",
                 "inventorythreshold", "inventoryaverage"]],
        use_container_width=True,
    )

    # perform restock (one DB commit per item)
    for _, r in need_df.iterrows():
        restock_item(r, supplier_id)

    st.success(f"{len(need_df)} item(s) restocked (synthetic PO(s) created).")
else:
    st.info("All items are above their inventory thresholds.")

st.subheader("Current inventory snapshot")
st.dataframe(
    stock_df[["itemnameenglish", "totalqty",
              "inventorythreshold", "inventoryaverage"]]
        .sort_values("itemnameenglish"),
    use_container_width=True,
)
