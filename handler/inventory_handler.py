"""
InventoryHandler – bulk‑refills warehouse stock
(recursion‑safe ∙ full‑seq‑sync · 2025‑07‑24)

• Syncs every relevant sequence to MAX(pk) before inserts:
      purchaseorders_poid_seq
      purchaseorderitems_poitemid_seq
      poitemcost_costid_seq
      inventory_batchid_seq
• Never nests a second connection context manager ⇒ no
  "connection cannot be re‑entered recursively" errors.
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
    # ---------- lightweight wrappers (NO with self.conn) -------------------
    def execute_command(self, sql: str, params: tuple = ()) -> None:
        self._ensure_live_conn()
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            self.conn.commit()
        finally:
            cur.close()

    def fetch_data(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        self._ensure_live_conn()
        return pd.read_sql_query(sql, self.conn, params=params)

    # ---------- generic seq‑sync helper ------------------------------------
    def _sync_sequence(self, cur, seq: str, table: str, pk: str) -> None:
        cur.execute(f"SELECT COALESCE(MAX({pk}),0) FROM {table}")
        max_val = cur.fetchone()[0]
        cur.execute("SELECT setval(%s, %s, true)", (seq, max_val))

    # ---------- snapshot ---------------------------------------------------
    def stock_levels(self) -> pd.DataFrame:
        inv = self.fetch_data(
            "SELECT itemid, SUM(quantity)::int AS totalqty "
            "FROM inventory GROUP BY itemid"
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
            FROM item
            """
        )
        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ---------- misc helper -------------------------------------------------
    def supplier_for(self, itemid: int) -> int:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid=%s LIMIT 1",
            (itemid,),
        )
        return GENERIC_SUPPLIER_ID if res.empty else int(res.iloc[0, 0])

    # ---------- purchase‑order header -------------------------------------
    def _create_po(self, supplier_id: int) -> int:
        self._ensure_live_conn()
        for attempt in (1, 2):
            cur = self.conn.cursor()
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
                self.conn.rollback()
                if attempt == 1:
                    self._sync_sequence(
                        cur, "purchaseorders_poid_seq",
                        "purchaseorders", "poid"
                    )
                    self.conn.commit()
                else:
                    raise
            finally:
                cur.close()

    # ---------- PO lines & cost rows --------------------------------------
    def _add_lines_and_costs(
        self, poid: int, items: List[Tuple[int, int, float]]
    ) -> List[int]:
        po_rows   = [(poid, it, q, q, cpu) for it, q, cpu in items]
        cost_rows = [(poid, it, cpu, q, "Auto Refill") for it, q, cpu in items]

        for attempt in (1, 2):
            cur = self.conn.cursor()
            try:
                # keep sequences ahead
                self._sync_sequence(
                    cur, "purchaseorderitems_poitemid_seq",
                    "purchaseorderitems", "poitemid"
                )
                self._sync_sequence(
                    cur, "poitemcost_costid_seq",
                    "poitemcost", "costid"
                )

                execute_values(
                    cur,
                    """
                    INSERT INTO purchaseorderitems
                          (poid,itemid,orderedquantity,receivedquantity,
                           estimatedprice)
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
                self.conn.commit()
                return cost_ids
            except pgerr.UniqueViolation:
                self.conn.rollback()
                if attempt == 1:
                    self._sync_sequence(
                        cur, "poitemcost_costid_seq",
                        "poitemcost", "costid"
                    )
                    self.conn.commit()
                else:
                    raise
            finally:
                cur.close()

    # ---------- insert inventory layers -----------------------------------
    def _insert_inventory(
        self,
        rows: List[Tuple[int, int, float, int, int]],
    ) -> None:
        inv_rows = [
            (it, qty, FIX_EXPIRY, FIX_WH_LOC, cpu, poid, cid)
            for it, qty, cpu, poid, cid in rows
        ]
        for attempt in (1, 2):
            cur = self.conn.cursor()
            try:
                if attempt == 1:
                    self._sync_sequence(
                        cur, "inventory_batchid_seq",
                        "inventory", "batchid"
                    )

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
                self.conn.commit()
                return
            except pgerr.UniqueViolation:
                self.conn.rollback()
                if attempt == 1:
                    self._sync_sequence(
                        cur, "inventory_batchid_seq",
                        "inventory", "batchid"
                    )
                    self.conn.commit()
                else:
                    raise
            finally:
                cur.close()

    # ---------- public API -------------------------------------------------
    def restock_items_bulk(
        self,
        df_need: pd.DataFrame,
        *,
        debug: bool = False,
    ) -> Dict[str, Any]:
        df_need = df_need.copy()
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)

        log: List[dict] = []
        dbg: Dict[int, pd.DataFrame] = {}

        for sup_id, grp in df_need.groupby("supplier"):
            poid = self._create_po(int(sup_id))
            items = [
                (
                    int(r.itemid),
                    int(r.need),
                    round(float(r.sellingprice) * 0.75, 2)
                    if r.sellingprice else 0.0,
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
                    dict(itemid=it, added=qty, cpu=cpu,
                         poid=poid, costid=cid)
                )

            if debug:
                dbg[int(sup_id)] = grp.copy()

        return {"log": log, "by_supplier": dbg} if debug else {"log": log}
