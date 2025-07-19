# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
======================
Automates warehouse‑level refills whenever an SKU’s inventory total
drops under its threshold.

Defaults:
• inventorythreshold (if NULL / missing) → 50
• inventoryaverage   (if NULL / missing) → 100
"""

from __future__ import annotations
from datetime import date

import pandas as pd

# ⬇️  CORRECT import path – ReceiveHandler lives in project root
from receive_handler import ReceiveHandler

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryRefillHandler(ReceiveHandler):
    # ───────────────────── stock snapshot ─────────────────────
    def _stock_levels(self) -> pd.DataFrame:
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

    # ───────────────────── single‑item restock ─────────────────────
    def restock_item(
        self,
        itemid: int,
        supplier_id: int,
        need: int,
        *,
        cost_per_unit: float = 0.0,
        note: str = "Auto‑Inventory Refill",
    ) -> int:
        """Create synthetic PO, insert inventory; return new POID."""
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

    # ───────────────────── bulk checker ─────────────────────
    def check_and_restock_all(
        self,
        supplier_id: int,
        *,
        dry_run: bool = False,
    ) -> pd.DataFrame:
        """
        Restock every SKU below threshold up to average.
        Returns a DataFrame summarising actions.
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
                "itemid"       : int(row.itemid),
                "itemname"     : row.itemnameenglish,
                "current_qty"  : int(row.totalqty),
                "threshold"    : int(row.inventorythreshold),
                "target_stock" : int(row.inventoryaverage),
                "units_added"  : need_units,
                "poid"         : poid,
            })

        return pd.DataFrame(actions)
