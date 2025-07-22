"""
InventoryHandler – bulk‑refills warehouse stock.

One synthetic PO **per supplier** per cycle, then one
batch insert into purchaseorderitems, poitemcost, and
inventory. Unit‑cost = 75 % of `sellingprice` (0 if NULL).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Tuple

import pandas as pd
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

# ──────────────── constants ────────────────
DEFAULT_THRESHOLD   = 50
DEFAULT_AVERAGE     = 100
GENERIC_SUPPLIER_ID = 510            # “Not available”
FIX_EXPIRY          = date(2027, 7, 21)
FIX_WH_LOC          = "A2"


class InventoryHandler(DatabaseManager):
    """High‑level, bulk inventory refill API."""

    # ───────────────── snapshot ─────────────────
    def stock_levels(self) -> pd.DataFrame:
        """Current inventory totals merged with thresholds/averages."""
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity)::int AS totalqty "
            "FROM   inventory GROUP BY itemid"
        )
        if inv.empty:                                      # nice to have
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,       {DEFAULT_THRESHOLD}) AS threshold,
                   COALESCE(averagerequired, {DEFAULT_AVERAGE})   AS average,
                   COALESCE(sellingprice,0)                     AS sellingprice
            FROM   item
            """
        )

        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────────── supplier lookup ─────────────
    def supplier_for(self, itemid: int) -> int:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid=%s LIMIT 1",
            (itemid,),
        )
        return GENERIC_SUPPLIER_ID if res.empty else int(res.iloc[0, 0])

    # ───────────── private helpers ─────────────
    def _create_po(self, supplier_id: int) -> int:
        """Return freshly created PO id (status = Completed)."""
        return int(
            self.execute_command_returning(
                """
                INSERT INTO purchaseorders
                      (supplierid,status,orderdate,expecteddelivery,
                       actualdelivery,createdby,suppliernote,totalcost)
                VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                        CURRENT_DATE,'AutoInventory','AUTO BULK',0)
                RETURNING poid
                """,
                (supplier_id,),
            )[0]
        )

    def _add_lines_and_costs(
        self, poid: int, items: List[Tuple[int, int, float]]
    ) -> List[int]:
        """
        Bulk‑insert purchase‑order lines *and* matching cost rows.
        Returns the costids in the same order as *items*.
        """
        po_rows   = [(poid, it, q, q, cpu) for it, q, cpu in items]
        cost_rows = [
            (poid, it, cpu, q, "Auto Refill") for it, q, cpu in items
        ]

        with self.conn:                       # one transaction
            with self.conn.cursor() as cur:
                # purchaseorderitems
                execute_values(
                    cur,
                    """
                    INSERT INTO purchaseorderitems
                          (poid,itemid,orderedquantity,receivedquantity,estimatedprice)
                    VALUES %s
                    """,
                    po_rows,
                )

                # poitemcost – RETURNING to capture the generated IDs
                execute_values(
                    cur,
                    """
                    INSERT INTO poitemcost
                          (poid,itemid,cost_per_unit,quantity,note,cost_date)
                    SELECT x.poid, x.itemid, x.cpu, x.qty, x.note, CURRENT_TIMESTAMP
                    FROM (VALUES %s) AS x(poid,itemid,cpu,qty,note)
                    RETURNING costid
                    """,
                    cost_rows,
                )
                cost_ids = [row[0] for row in cur.fetchall()]

        return cost_ids

    def _insert_inventory(
        self,
        rows: List[Tuple[int, int, float, int, int]],
    ) -> None:
        """Bulk insert directly into `inventory`."""
        inv_rows = [
            (it, qty, FIX_EXPIRY, FIX_WH_LOC, cpu, poid, cid)
            for it, qty, cpu, poid, cid in rows
        ]
        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO inventory
                          (itemid,quantity,expirationdate,storagelocation,
                           cost_per_unit,poid,costid)
                    VALUES %s
                    """,
                    inv_rows,
                )

    # ───────────── public API ─────────────
    def restock_items_bulk(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need → itemid • need • sellingprice
        One PO per supplier, returning an action log:
        [{itemid, added, cpu, poid, costid}, …]
        """
        df_need = df_need.copy()
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)

        log: list[dict] = []

        for sup_id, grp in df_need.groupby("supplier"):
            poid  = self._create_po(int(sup_id))

            items = [
                (
                    int(r.itemid),
                    int(r.need),
                    round(float(r.sellingprice) * 0.75, 2) if r.sellingprice else 0.0,
                )
                for _, r in grp.iterrows()
            ]

            cost_ids = self._add_lines_and_costs(poid, items)

            inv_rows = [
                (it, qty, cpu, poid, cid)
                for (it, qty, cpu), cid in zip(items, cost_ids)
            ]
            self._insert_inventory(inv_rows)

            for (it, qty, cpu), cid in zip(items, cost_ids):
                log.append(
                    dict(
                        itemid=it,
                        added=qty,
                        cpu=cpu,
                        poid=poid,
                        costid=cid,
                    )
                )

        return log
