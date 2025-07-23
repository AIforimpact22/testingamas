from __future__ import annotations
"""
🏬 Inventory Bulk Restock – Live Progress & Logs
"""

import time
import pandas as pd
import streamlit as st
from datetime import datetime

from handler.inventory_handler import InventoryHandler  # update import as needed

st.set_page_config(page_title="Inventory Bulk Restock", page_icon="🏬")
st.title("🏬 Inventory Bulk Restock")

# --- Session log for all runs ---
st.session_state.setdefault("bulk_history", [])

handler = InventoryHandler()

# --- Demo/Upload: How will you get df_need? ---
st.subheader("Preview: Items Needing Restock")
df_need = None
# Option 1: Upload CSV (columns: itemid, need, sellingprice)
up = st.file_uploader("Upload .csv with 'itemid','need','sellingprice'", type="csv")
if up is not None:
    df_need = pd.read_csv(up)
elif "df_need_demo" not in st.session_state:
    # Option 2: Generate Demo (for testing)
    demo_data = [
        dict(itemid=1001, need=120, sellingprice=25.5),
        dict(itemid=1002, need=80, sellingprice=13.8),
        dict(itemid=1003, need=70, sellingprice=11.0),
    ]
    df_need = pd.DataFrame(demo_data)
    st.session_state["df_need_demo"] = df_need
else:
    df_need = st.session_state["df_need_demo"]

if df_need is not None:
    st.dataframe(df_need, use_container_width=True)
else:
    st.info("Please upload or generate a list of items to restock.")

# --- Bulk Restock Button ---
if df_need is not None:
    run_bulk = st.button("Bulk Restock Now")

    if run_bulk:
        st.session_state["run_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = []
        debug_per_supplier = {}
        supplier_ids = df_need["itemid"].apply(handler.supplier_for)
        suppliers = sorted(set(supplier_ids))
        supplier_map = dict(zip(df_need["itemid"], supplier_ids))
        df_need = df_need.copy()
        df_need["supplier"] = df_need["itemid"].map(supplier_map)
        total_sup = len(df_need["supplier"].unique())

        pbar = st.progress(0, text="Preparing...")
        status = st.empty()

        for j, (sup_id, grp) in enumerate(df_need.groupby("supplier"), 1):
            status.info(f"Restocking for supplier {sup_id} ({j} of {total_sup}) ...")
            # Run a single-supplier restock by slicing df_need for just this supplier
            result = handler.restock_items_bulk(grp, debug=True)
            for entry in result["log"]:
                entry["supplier"] = sup_id
                entry["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.extend(result["log"])
            debug_per_supplier[sup_id] = grp
            pbar.progress(j / total_sup, text=f"Completed {j}/{total_sup} suppliers")
            time.sleep(0.2)
        pbar.progress(1.0, text="Done.")

        st.session_state["bulk_history"].append(
            dict(
                timestamp=st.session_state["run_ts"],
                log=log,
                debug=debug_per_supplier
            )
        )

        st.success(f"Bulk restock complete: {len(log)} items processed for {total_sup} suppliers.")

        # Show results in tabs
        tab1, tab2, tab3 = st.tabs(["Action Log", "By Supplier", "Bulk Run History"])
        with tab1:
            st.subheader("Action Log (this run)")
            st.dataframe(pd.DataFrame(log), use_container_width=True)

        with tab2:
            st.subheader("Supplier-wise breakdown (this run)")
            for sup_id, df_s in debug_per_supplier.items():
                st.markdown(f"**Supplier {sup_id}**")
                st.dataframe(df_s, use_container_width=True)

        with tab3:
            st.subheader("Bulk Run History (all in this session)")
            for i, entry in enumerate(reversed(st.session_state["bulk_history"]), 1):
                with st.expander(f"Run at {entry['timestamp']}", expanded=(i == 1)):
                    st.write("Log:")
                    st.dataframe(pd.DataFrame(entry["log"]), use_container_width=True)
                    st.write("Supplier debug breakdown:")
                    for sup_id, df_s in entry["debug"].items():
                        st.markdown(f"**Supplier {sup_id}**")
                        st.dataframe(df_s, use_container_width=True)

    else:
        st.info("Press **Bulk Restock Now** to process all items in the list.")
