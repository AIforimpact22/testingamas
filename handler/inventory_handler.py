# handler/inventory_handler.py
"""
InventoryHandler
────────────────
Bulk‑refill the back‑store inventory up to each SKU’s
`threshold / averagerequired` (columns in `item`).

* One synthetic PO **per supplier per cycle**.
* Unit‑cost = 75 % of `sellingprice` (0 if selling price is 0/NULL).
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100
FIX_EXPIRY_DATE   = date(2027, 7, 21)   # ← until real values are supplied
FIX_STORAGE_LOC   = "A2"                # ← hard‑coded warehouse slot


class InventoryHandler(DatabaseManager):
    # ───────────────────── current stock + meta ─────────────────────
    def stock_levels(self) -> pd.DataFrame:
        """Return inventory totals + threshold / average from *item*."""
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity)::int AS totalqty "
            "FROM   inventory GROUP BY itemid"
        )
        if inv.empty:
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT  i.itemid,
                    i.itemnameenglish,
                    COALESCE(i.threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                    COALESCE(i.averagerequired, {DEFAULT_AVERAGE})   AS average,
                    COALESCE(s.sellingprice,0)  AS sellingprice
            FROM    item i
            LEFT JOIN (
                SELECT itemid, MIN(sellingprice) AS sellingprice
                FROM   item GROUP BY itemid
            ) s USING (itemid)
            """
        )
        # merge → every item row present even if no inventory yet
        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ─────────── supplier helpers ───────────
    def supplier_for(self, itemid: int) -> int | None:
        rows = self.fetch_data(
            "SELECT supplierid FROM itemsupplier "
            "WHERE itemid=%s LIMIT 1", (itemid,)
        )
        return None if rows.empty else int(rows.iloc[0, 0])

    # ─────────── PO helpers ───────────
    def create_supplier_po(self, supplier_id: int) -> int:
        poid = self.execute_command_returning(
            """
            INSERT INTO purchaseorders
                  (supplierid,status,orderdate,expecteddelivery,
                   actualdelivery,createdby,suppliernote,totalcost)
            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                    CURRENT_DATE,'AutoInventory','AUTO BULK',0)
            RETURNING poid;
            """,
            (supplier_id,)
        )[0]
        return int(poid)

    def add_po_lines_and_costs(
        self, poid: int, rows: list[tuple[int,int,float]]
    ) -> list[int]:
        """
        rows → [(itemid, qty, cpu), …]
        returns list[costid]
        """
        po_item_rows  = []
        po_cost_rows  = []
        cost_ids: list[int] = []

        for itemid, qty, cpu in rows:
            po_item_rows.append((poid, itemid, qty, qty, cpu))
            po_cost_rows.append((poid, itemid, cpu, qty, "AUTO REFILL"))

        with self.conn:                                # single tx
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO purchaseorderitems "
                    "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
                    "VALUES %s",
                    po_item_rows
                )
                cur.execute("SELECT currval('purchaseorderitems_poiid_seq');")
                # we do cost rows one‑by‑one so we get their IDs
                for poid_, itemid, cpu, qty, note in po_cost_rows:
                    cur.execute(
                        "INSERT INTO poitemcost "
                        "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
                        "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING costid",
                        (poid_, itemid, cpu, qty, note)
                    )
                    cost_ids.append(int(cur.fetchone()[0]))
        return cost_ids

    # ─────────── inventory bulk insert ───────────
    def bulk_inventory_rows(
        self,
        item_rows: list[tuple[int,int,float,int,int]]
    ) -> None:
        """
        item_rows → (itemid, qty, cpu, poid, costid)
        """
        tuples = [
            (itemid, qty, FIX_EXPIRY_DATE, FIX_STORAGE_LOC, cpu, poid, costid)
            for itemid, qty, cpu, poid, costid in item_rows
        ]
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO inventory "
                "(itemid,quantity,expirationdate,storagelocation,"
                " cost_per_unit,poid,costid) VALUES %s",
                tuples
            )
        self.conn.commit()

    # ─────────── public restock API ───────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        Accepts a **need dataframe** (itemid, need, sellingprice).
        Creates one PO per supplier and inserts inventory layers in bulk.
        Returns action log rows.
        """
        # 1️⃣ group by supplier
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)
        df_need.dropna(subset=["supplier"], inplace=True)

        log: list[dict] = []
        for supplier_id, group in df_need.groupby("supplier"):
            poid = self.create_supplier_po(int(supplier_id))

            # prepare PO‑line tuples & inventory rows
            po_rows      = []
            inv_rows     = []
            for _, row in group.iterrows():
                cpu  = round(float(row.sellingprice) * 0.75, 2)
                po_rows.append((int(row.itemid), int(row.need), cpu))
            cost_ids = self.add_po_lines_and_costs(
                poid, [(r[0], r[1], r[2]) for r in po_rows]
            )
            # pair po_rows with returned cost_ids
            for (itemid, qty, cpu), costid in zip(po_rows, cost_ids):
                inv_rows.append((itemid, qty, cpu, poid, costid))
                log.append(
                    dict(itemid=itemid, added=qty, cpu=cpu, poid=poid)
                )

            self.bulk_inventory_rows(inv_rows)
        return log
