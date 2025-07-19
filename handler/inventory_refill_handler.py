# handler/inventory_refill_handler.py
"""
InventoryRefillHandler
======================
Automates warehouse‑level refills whenever an SKU’s on‑hand quantity drops
below its *inventorythreshold*.

Key public methods
------------------
• `check_and_restock_all(supplier_id: int, dry_run: bool = False) -> pd.DataFrame`
     Scan every item and restock those below threshold.  Returns a DataFrame
     listing all actions.  If `dry_run=True`, only reports what *would* happen.

• `restock_item(itemid: int, supplier_id: int, need: int, cost_per_unit: float = 0.0)`
     Low‑level helper that creates a synthetic PO and inserts a new inventory
     layer for *need* units.

Defaults
--------
If an item’s *inventorythreshold* or *inventoryaverage* is NULL, defaults are
50 and 100 respectively (change them to fit your business rules).
"""

from __future__ import annotations
from datetime import date

import pandas as pd

from handler.receive_handler import ReceiveHandler

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100


class InventoryRefillHandler(ReceiveHandler):
    # ───────────────────── base look‑ups ─────────────────────
    def _stock_levels(self) -> pd.DataFrame:
        """Return inventory totals merged with item thresholds/averages."""
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
                   COALESCE(inventorythreshold, {DEFAULT_THRESHOLD}) AS inventorythreshold,
                   COALESCE(inventoryaverage,   {DEFAULT_AVERAGE})   AS inventoryaverage
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
        """
        Create a synthetic *Completed* PO and insert an inventory layer for
        *need* units.  Returns the generated POID.
        """
        if need <= 0:
            return -1  # nothing to do

        # 1) synthetic PO header
        poid = self.create_manual_po(supplier_id, note=note)

        # 2) PO line + cost row
        self.add_po_item(poid, itemid, need, cost_per_unit)
        costid = self.insert_poitem_cost(
            poid, itemid, cost_per_unit, need, note=note
        )

        # 3) inventory insertion (today, dummy location 'AUTO')
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
        Scan every SKU; when totalqty < inventorythreshold, top it up to
        inventoryaverage.  Returns a DataFrame of actions taken.

        If *dry_run* is True, only returns the plan (no DB writes).
        """
        df = self._stock_levels()
        needs = df[df.totalqty < df.inventorythreshold].copy()

        actions = []
        for _, row in needs.iterrows():
            itemid      = int(row.itemid)
            need_units  = int(row.inventoryaverage) - int(row.totalqty)
            poid        = None

            if not dry_run:
                poid = self.restock_item(itemid, supplier_id, need_units)

            actions.append({
                "itemid"        : itemid,
                "itemname"      : row.itemnameenglish,
                "current_qty"   : row.totalqty,
                "threshold"     : row.inventorythreshold,
                "target_stock"  : row.inventoryaverage,
                "units_added"   : need_units,
                "poid"          : poid,
            })

        return pd.DataFrame(actions)
