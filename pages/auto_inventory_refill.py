from __future__ import annotations
"""
Shelf Auto‑Refill (passive)
───────────────────────────
Checks shelf stock every 10 s and moves inventory to shelf up to each
item’s threshold / average levels.  No UI controls; execution is stopped
when the “Simulators running” toggle (in *app.py*) is OFF.
"""

import streamlit as st
import pandas as pd
import time
from handler.shelf_handler import ShelfHandler

# ───────── stop if simulators are paused ─────────
if not st.session_state.get("sim_active", True):
    st.warning("Simulators are paused (toggle in main sidebar).")
    st.stop()

shelf = ShelfHandler()

@st.cache_data(ttl=10, show_spinner=False)
def item_meta() -> pd.DataFrame:
    return shelf.get_all_items().set_index("itemid")     # shelfthreshold / average

# ───────────────────────── helpers ─────────────────────────
def restock_item(itemid: int, *, user: str = "AUTO‑SHELF") -> str:
    meta = item_meta()
    kpi  = shelf.get_shelf_quantity_by_item()
    rowk = kpi.loc[kpi.itemid == itemid]
    if rowk.empty:
        current = 0
    else:
        current = int(rowk.totalquantity.iloc[0])

    threshold = int(meta.at[itemid, "shelfthreshold"] or 0)
    average   = int(meta.at[itemid, "shelfaverage"]   or threshold or 0)

    if current >= threshold:
        return "OK"

    need = max(average - current, threshold - current)
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

    # not enough inventory
    shelf.execute_command(
        "INSERT INTO shelf_shortage (itemid, shortage_qty, logged_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (itemid, need),
    )
    return f"Partial (short {need})"

def auto_restock_cycle() -> pd.DataFrame:
    kpi  = shelf.get_shelf_quantity_by_item()
    meta = item_meta().reset_index()

    # merge with explicit suffixes to avoid _x/_y surprises
    df = kpi.merge(
        meta[["itemid", "shelfthreshold", "shelfaverage"]],
        on="itemid",
        how="left",
        suffixes=("_kpi", "_meta"),
    )

    # unified columns
    df["threshold"] = df["shelfthreshold_meta"].fillna(df["shelfthreshold_kpi"])
    df["average"]   = df["shelfaverage_meta"].fillna(df["shelfaverage_kpi"])

    below = df[df.totalquantity < df.threshold]
    actions = []
    for _, r in below.iterrows():
        status = restock_item(int(r.itemid))
        actions.append(
            dict(
                item        = r.itemname,
                qty_before  = int(r.totalquantity),
                threshold   = int(r.threshold),
                average     = int(r.average),
                action      = status,
            )
        )
    return pd.DataFrame(actions)

# ───────────────────────── Streamlit page ─────────────────────────
st.set_page_config("Shelf Auto‑Refill", "🗄️")
st.title("🗄️ Shelf Auto‑Refill Monitor (passive)")

log_df = auto_restock_cycle()
if log_df.empty:
    st.success("All shelf SKUs are at or above their thresholds.")
else:
    st.success(f"{len(log_df)} SKU(s) processed this cycle.")
    st.dataframe(log_df, use_container_width=True)

# Auto‑refresh every 10 s
if hasattr(st, "autorefresh"):
    st.autorefresh(interval=10000, key="shelf_refill_refresh")
