# pages/auto_inventory_refill.py
"""
Auto‑Inventory Refill Simulator
───────────────────────────────
• Polls the back‑store inventory every N seconds
• When total quantity for an item falls below its **re‑order point**
  (here: `max(2 × shelfaverage, 3 × shelfthreshold)` or default 50),
  it creates a *synthetic* Purchase Order, books a matching receipt,
  and inserts the goods into the `inventory` table.
• Uses ReceiveHandler + ShelfHandler helpers – **no buttons**;
  the page just refreshes and logs what it did.
"""

from __future__ import annotations
import random
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from handler.shelf_handler     import ShelfHandler
from handler.receive_handler   import ReceiveHandler

shelf   = ShelfHandler()
recv    = ReceiveHandler()

# ⏱️  ——————————————————————— page config & auto‑refresh
st.set_page_config(page_title="Inventory Auto‑Refill", page_icon="🚚",
                   layout="wide")
st.title("🚚 Inventory Auto‑Refill Monitor")

if hasattr(st, "autorefresh"):          # Streamlit ≥ 1.33
    st.autorefresh(interval=5000, key="inv_autorefresh")   # every 5 s

# ───────────────────────── helpers ─────────────────────────
@st.cache_data(ttl=5, show_spinner=False)
def current_inventory() -> pd.DataFrame:
    """Item‑level totals in back‑store inventory."""
    return shelf.fetch_data(
        """
        SELECT  i.itemid,
                i.itemnameenglish AS itemname,
                COALESCE(SUM(inv.quantity), 0) AS qty
        FROM    item i
        LEFT JOIN inventory inv ON inv.itemid = i.itemid
        GROUP   BY i.itemid, i.itemnameenglish
        """
    )

@st.cache_data(ttl=600, show_spinner=False)
def item_targets() -> pd.DataFrame:
    """Bring shelfaverage / shelfthreshold for each item once / 10 min."""
    return shelf.get_all_items().set_index("itemid")        # threshold & average

def choose_supplier_id() -> int:
    """Pick the first supplier as the ‘auto’ supplier, else raise."""
    df = recv.get_suppliers()
    if df.empty:
        raise RuntimeError("No supplier records available – add at least one!")
    return int(df.iloc[0].supplierid)

def synthetic_cost(itemid: int) -> float:
    """Crude cost estimator: random 1.0–5.0 currency units."""
    random.seed(itemid)              # deterministic per SKU
    return round(random.uniform(1.00, 5.00), 2)

# ───────────────────────── refill engine ───────────────────
def auto_replenish() -> list[str]:
    """
    Check every item and top‑up inventory if below its reorder point.
    Returns log lines describing what was done this cycle.
    """
    inv_df  = current_inventory()
    meta_df = item_targets()
    supplier_id = choose_supplier_id()

    log: list[str] = []
    batch_inv: list[dict] = []
    poid = None       # create lazily when first needed

    for row in inv_df.itertuples(index=False):
        iid   = int(row.itemid)
        qty   = int(row.qty)

        # derive reorder point & target
        meta = meta_df.loc[iid] if iid in meta_df.index else None
        thresh = int(meta.shelfthreshold) if meta is not None and pd.notna(meta.shelfthreshold) else 10
        avg    = int(meta.shelfaverage)   if meta is not None and pd.notna(meta.shelfaverage)   else 20
        reorder_point = max(2 * avg, 3 * thresh, 50)   # fallback 50
        target_stock  = reorder_point + avg            # aim a bit higher

        if qty < reorder_point:
            topup_qty = target_stock - qty
            cost      = synthetic_cost(iid)

            # create synthetic PO header the first time we need it
            if poid is None:
                poid = recv.create_manual_po(supplier_id,
                                             note="[AUTO‑REFILL]")
                log.append(f"📝 Created synthetic PO #{poid}")

            recv.add_po_item(poid, iid, topup_qty, cost)
            costid = recv.insert_poitem_cost(
                poid, iid, cost, topup_qty, note="Auto‑inventory‑refill"
            )
            batch_inv.append({
                "item_id":          iid,
                "quantity":         topup_qty,
                "expiration_date":  date.today() + timedelta(days=365),
                "storage_location": "BACK‑STORE",
                "cost_per_unit":    cost,
                "poid":             poid,
                "costid":           costid,
            })
            log.append(
                f"📦 Re‑ordered {topup_qty:>5} x {row.itemname} "
                f"(inv {qty} < reorder {reorder_point})"
            )

    # commit inventory rows & refresh PO cost
    if batch_inv:
        recv.add_items_to_inventory(batch_inv)
        recv.refresh_po_total_cost(poid)
        log.append(f"✅ Received {len(batch_inv)} lines into inventory.")
    else:
        log.append("👍 All SKUs above reorder points – no action.")
    return log

# ───────────────────────── UI output ───────────────────────
try:
    messages = auto_replenish()
except Exception as e:
    st.error(f"Auto‑refill error: {e}")
    st.stop()

for msg in messages:
    if msg.startswith("📦") or msg.startswith("📝"):
        st.write(msg)
    elif msg.startswith("✅"):
        st.success(msg)
    else:
        st.info(msg)

st.divider()
st.subheader("Current inventory snapshot (top 50 by qty)")
snapshot = current_inventory().sort_values("qty", ascending=False).head(50)
st.dataframe(snapshot, use_container_width=True)
