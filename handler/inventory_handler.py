"""
InventoryHandler
================
Utility‑class that keeps warehouse stock above each SKU’s
**threshold / average_required** levels.

• Supplier is taken from `itemsupplier` (first match).
• One synthetic PO (status = Completed, cost = 0) is created per refill.
• A single inventory layer is inserted with fixed expiry/location.
"""

from __future__ import annotations

from datetime import date
import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

# ───────────── constants ─────────────
FIXED_EXPIRY   = date(2027, 7, 21)
FIXED_LOCATION = "A2"
BOT_USER       = "AUTO‑INV"

FALLBACK_THRESHOLD = 50
FALLBACK_AVERAGE   = 100


class InventoryHandler(DatabaseManager):
    # ───────────────── snapshot helpers ─────────────────
    def stock_levels(self) -> pd.DataFrame:
        """
        Return a DataFrame with:
        itemid • itemnameenglish • threshold • average_required • totalqty
        """
        inv = self.fetch_data(
            "SELECT itemid, COALESCE(SUM(quantity),0) AS totalqty "
            "FROM inventory GROUP BY itemid"
        )

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {FALLBACK_THRESHOLD}) AS threshold,
                   COALESCE(average_required,{FALLBACK_AVERAGE})   AS average_required
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────── supplier resolution ─────────────
    def _supplier_for_item(self, itemid: int) -> int | None:
        row = self.fetch_data(
            "SELECT supplierid FROM itemsupplier "
            "WHERE itemid=%s LIMIT 1",
            (itemid,),
        )
        return None if row.empty else int(row.iloc[0, 0])

    # ───────────── PO / cost utilities ─────────────
    def _create_po(self, supplier_id: int) -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,actualdelivery,
                   createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                    %s,'AUTO‑INVENTORY REFILL',0.0)
            RETURNING poid
            """,
            (supplier_id, BOT_USER),
        )[0]
        return int(poid)

    def _add_po_item(self, poid: int, itemid: int, qty: int):
        self.execute_command(
            """
            INSERT INTO purchaseorderitems
                  (poid,itemid,orderedquantity,receivedquantity,estimatedprice)
            VALUES (%s,%s,%s,%s,0.0)
            """,
            (poid, itemid, qty, qty),
        )

    def _add_cost_row(self, poid: int, itemid: int, qty: int) -> int:
        costid = self.execute_command_returning(
            """
            INSERT INTO poitemcost
                  (poid,itemid,cost_per_unit,quantity,cost_date,note)
            VALUES (%s,%s,0.0,%s,CURRENT_TIMESTAMP,'Auto‑inventory refill')
            RETURNING costid
            """,
            (poid, itemid, qty),
        )[0]
        return int(costid)

    def _insert_inventory(self, *, itemid: int, qty: int,
                          poid: int, costid: int) -> None:
        self.execute_command(
            """
            INSERT INTO inventory
                  (itemid,quantity,expirationdate,
                   storagelocation,cost_per_unit,poid,costid)
            VALUES (%s,%s,%s,%s,0.0,%s,%s)
            """,
            (
                itemid,
                qty,
                FIXED_EXPIRY,
                FIXED_LOCATION,
                poid,
                costid,
            ),
        )

    # ───────────── public refill API ─────────────
    def restock_item(self, itemid: int, need_qty: int) -> int:
        """
        Top‑up *itemid* by *need_qty* units.
        Returns the newly created POID (0 if nothing done).
        """
        if need_qty <= 0:
            return 0

        supplier_id = self._supplier_for_item(itemid)
        if supplier_id is None:
            raise ValueError(f"No supplier linked to itemid {itemid}")

        poid   = self._create_po(supplier_id)
        self._add_po_item(poid, itemid, need_qty)
        costid = self._add_cost_row(poid, itemid, need_qty)
        self._insert_inventory(itemid=itemid, qty=need_qty,
                               poid=poid, costid=costid)
        return poid
