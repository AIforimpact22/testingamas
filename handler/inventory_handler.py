"""
InventoryHandler – bulk‑refills warehouse stock
(batch‑safe edition, 2025‑07‑24)

• Guarantees the purchase‑orders sequence is ahead of MAX(poid)
  so inserts never collide with existing keys.
• Public API and data‑shape unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Tuple

import pandas as pd
from psycopg2.extras import execute_values
from psycopg2 import errors as pgerr

from db_handler import DatabaseManager

# ───────── constants ─────────
DEFAULT_THRESHOLD   = 50
DEFAULT_AVERAGE     = 100
GENERIC_SUPPLIER_ID = 510
FIX_EXPIRY          = date(2027, 7, 21)
FIX_WH_LOC          = "A2"


class InventoryHandler(DatabaseManager):
    # ───────────────────────── snapshot helpers ──────────────────────
    def stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity)::int AS totalqty "
            "FROM   inventory GROUP BY itemid"
        )
        if inv.empty:
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

    # ───────────────────────── misc helpers ──────────────────────────
    def supplier_for(self, itemid: int) -> int:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid=%s LIMIT 1",
            (itemid,),
        )
        return GENERIC_SUPPLIER_ID if res.empty else int(res.iloc[0, 0])

    # ---- NEW: keep sequence ahead of existing POIDs -----------------
    def _sync_po_sequence(self, cur) -> None:
        cur.execute("SELECT MAX(poid) FROM purchaseorders")
        max_poid = cur.fetchone()[0] or 0
        # Set sequence *to* max (nextval will return max+1)
        cur.execute(
            "SELECT setval('purchaseorders_poid_seq', %s, true)",
            (max_poid,),
        )

    # -----------------------------------------------------------------
    def _create_po(self, supplier_id: int) -> int:
        """
        Inserts a Completed purchase‑order and returns its poid.
        Guarantees sequence sync even if someone inserted manual IDs.
        """
        self._ensure_live_conn()
        with self.conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO purchaseorders
                          (supplierid,status,orderdate,expecteddelivery,
                           actualdelivery,createdby,suppliernote,totalcost)
                    VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                            CURRENT_DATE,'AutoInventory','AUTO BULK',0)
                    RETURNING poid
                    """,
                    (supplier_id,),
                )
                poid = cur.fetchone()[0]
                self.conn.commit()
                return int(poid)
            except pgerr.UniqueViolation:
                # Sequence lagged behind – fix and retry once
                self.conn.rollback()
                self._sync_po_sequence(cur)
                self.conn.commit()           # commit the setval
                with self.conn.cursor() as cur2:
                    cur2.execute(
                        """
                        INSERT INTO purchaseorders
                              (supplierid,status,orderdate,expecteddelivery,
                               actualdelivery,createdby,suppliernote,totalcost)
                        VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                                CURRENT_DATE,'AutoInventory','AUTO BULK',0)
                        RETURNING poid
                        """,
                        (supplier_id,),
                    )
                    poid = cur2.fetchone()[0]
                    self.conn.commit()
                    return int(poid)

    # -----------------------------------------------------------------
    def _add_lines_and_costs(
        self, poid: int, items: List[Tuple[int, int, float]]
    ) -> List[int]:
        po_rows   = [(poid, it, q, q, cpu) for it, q, cpu in items]
        cost_rows = [(poid, it, cpu, q, "Auto Refill") for it, q, cpu in items]

        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO purchaseorderitems
                          (poid,itemid,orderedquantity,receivedquantity,estimatedprice)
                    VALUES %s
                    """,
                    po_rows,
                )
                execute_values(
                    cur,
                    """
                    INSERT INTO poitemcost
                          (poid,itemid,cost_per_unit,quantity,note,cost_date)
                    SELECT x.poid,x.itemid,x.cpu,x.qty,x.note,CURRENT_TIMESTAMP
                    FROM (VALUES %s) x(poid,itemid,cpu,qty,note)
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

    # ───────────────────────── public API ────────────────────────────
    def restock_items_bulk(
        self,
        df_need: pd.DataFrame,
        *,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """
        If *debug* → returns dict with keys:
          log         – normal action log
          by_supplier – {supplier_id: DataFrame_of_items}
        Else returns {'log': …}
        """
        df_need = df_need.copy()
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)

        log: List[dict] = []
        dbg: Dict[int, pd.DataFrame] = {}

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
                    dict(itemid=it, added=qty, cpu=cpu, poid=poid, costid=cid)
                )

            if debug:
                dbg[int(sup_id)] = grp.copy()

        return {"log": log, "by_supplier": dbg} if debug else {"log": log}
