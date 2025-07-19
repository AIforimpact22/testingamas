# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
======================
Automates warehouse‑level refills when inventory dips below thresholds.
"""

from __future__ import annotations
from datetime import date
import importlib
import pandas as pd

# ── Robust import: supports either `receive_handler.py`
#    at repo root *or* inside handler/ package.
try:
    ReceiveHandler = importlib.import_module(
        "handler.receive_handler"
    ).ReceiveHandler
except ModuleNotFoundError:
    ReceiveHandler = importlib.import_module("receive_handler").ReceiveHandler

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryRefillHandler(ReceiveHandler):
    # ───────────────────── stock snapshot ─────────────────────
    def _stock_levels(self) -> pd.DataFrame:
        """
        Return current inventory totals merged with thresholds/averages.
        Missing values default to 50 / 100.
        """
        inv = self.fetch_data(
            """
            SELECT itemid, SUM(quantity) AS totalqty
            FROM   inventory
            GROUP  BY itemid;
            """
        )

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(inventorythreshold,{DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(inventoryaverage,  {DEFAULT_AVERAGE})   AS inventoryaverage
            FROM   item;
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ─────────────────── single‑item restock ───────────────────
    def restock_item(
        self,
        itemid: int,
        supplier_id: int,
        need: int,
        *,
        cost_per_unit: float = 0.0,
        note: str = "Auto‑Inventory Refill",
    ) -> int:
        """Create synthetic PO, insert inventory layer, return new POID."""
        if need <= 0:
            return -1

        poid = self.create_manual_po(supplier_id, note=note)
        self.add_po_item(poid, itemid, need, cost_per_unit)
        costid = self.insert_poitem_cost(
            poid, itemid, cost_per_unit, need, note=note
        )

        self.add_items_to_inventory([{
            "item_id"         : itemid,
            "quantity"        : need,
            "expiration_date" : date.today(),
            "storage_location": "AUTO",
            "cost_per_unit"   : cost_per_unit,
            "poid"            : poid,
            "costid"          : costid,
        }])

        self.refresh_po_total_cost(poid)
        return poid

    # ─────────────────── bulk check & refill ───────────────────
    def check_and_restock_all(
        self,
        supplier_id: int,
        *,
        dry_run: bool = False,
    ) -> pd.DataFrame:
        """
        Refill every SKU below threshold.  Returns summary DataFrame.
        If *dry_run* is True, no DB writes are made.
        """
        df    = self._stock_levels()
        needs = df[df.totalqty < df.inventorythreshold].copy()

        actions = []
        for _, row in needs.iterrows():
            need_units = int(row.inventoryaverage) - int(row.totalqty)
            poid = None
            if not dry_run:
                poid = self.restock_item(
                    int(row.itemid), supplier_id, need_units
                )
            actions.append({
                "itemid"      : int(row.itemid),
                "itemname"    : row.itemnameenglish,
                "current_qty" : int(row.totalqty),
                "threshold"   : int(row.inventorythreshold),
                "target"      : int(row.inventoryaverage),
                "added"       : need_units,
                "poid"        : poid,
            })
        return pd.DataFrame(actions)
