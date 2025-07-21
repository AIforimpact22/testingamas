from __future__ import annotations
"""
Shelf Auto‑Refill (passive)
───────────────────────────
Runs every 10 s in the background:

1. Scans shelf stock.
2. If any SKU’s shelf quantity < `shelfthreshold`,
   it moves product layers from inventory to shelf until it reaches
   `shelfaverage` (or at least `shelfthreshold`).
3. Outstanding shortages are logged when inventory is insufficient.

No user interaction.  The master switch is the
“Simulators running” toggle in *app.py*.
"""

import streamlit as st
import pandas as pd
import time
from handler.shelf_handler   import ShelfHandler

# ───────── stop if simulators are paused ─────────
if not st.session_state.get("sim_active", True):
    st.warning("Simulators are paused (toggle in main sidebar).")
    st.stop()

# ─────────────────── helpers ────────────────────
shelf = ShelfHandler()

@st.cache_data(ttl=5, show_spinner=False)
def item_meta() -> pd.DataFrame:
    return shelf.get_all_items().set_index("itemid")       # threshold / average

def restock_item(itemid: int, *, user: str = "AUTO‑SHELF") -> str:
    meta = item_meta()
    row  = shelf.get_shelf_quantity_by_item()
    row  = row.loc[row.itemid == itemid]

    current   = int(row.totalquantity.iloc[0]) if not row.empty else 0
    threshold = int(meta.at[itemid, "shelfthreshold"] or 0)
    target    = int(meta.at[itemid, "shelfaverage"]   or threshold or 0)

    if current >= threshold:
        return "OK"

    need = max(target - current, threshold - current)
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)
    if need <= 0:
        return "Shortage cleared"

    layers = shelf.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
        FROM   inventory
        WHERE  itemid = %s AND quantity > 0
        ORDER  BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )

    for lyr in layers.itertuples():
        take = min(need, int(lyr.quantity))
        shelf.transfer_from_inventory(
            itemid=itemid,
            expirationdate=lyr.expirationdate,
            quantity=take,
            cost_per_unit=float(lyr.cost_per_unit),
            created_by=user,
        )
        need -= take
        if need == 0:
            return "Refilled"

    # inventory could not fully cover need
    shelf.execute_command(
        """
        INSERT INTO shelf_shortage (itemid, shortage_qty, logged_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        """,
        (itemid, need),
    )
    return f"Partial (short {need})"

def auto_restock_cycle() -> pd.DataFrame:
    kpi = shelf.get_shelf_quantity_by_item()
    meta = item_meta().reset_index()
    df  = kpi.merge(meta[["itemid", "shelfthreshold", "shelfaverage"]],
                    on="itemid", how="left")

    below = df[df.totalquantity < df.shelfthreshold]
    actions = []
    for _, r in below.iterrows():
        status = restock_item(int(r.itemid))
        actions.append({
            "item": r.itemname,
            "qty_before": r.totalquantity,
            "threshold": int(r.shelfthreshold),
            "average": int(r.shelfaverage),
            "action": status,
        })
    return pd.DataFrame(actions)

# ─────────────────── UI / loop ───────────────────
st.set_page_config("Shelf Auto‑Refill", "🗄️")
st.title("🗄️ Shelf Auto‑Refill Monitor (passive)")

run_actions = auto_restock_cycle()
if run_actions.empty:
    st.success("All shelf SKUs are at or above their thresholds.")
else:
    st.success(f"{len(run_actions)} SKU(s) processed this cycle.")
    st.dataframe(run_actions, use_container_width=True)

# Auto‑refresh every 10 s
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=10000, key="shelf_refill_refresh")
