# handler/inventory_handler.py
"""
InventoryHandler – bulk‑refills warehouse stock
(single‑transaction per supplier · 2025‑07‑26)

Key upgrades vs 2025‑07‑24 version
──────────────────────────────────
1.  One COMMIT per supplier (≈ 3× fewer commits)  
2.  Warning‑free pandas reads  
3.  Uses psycopg2.TRANSACTION_STATUS_IDLE (fixes AttributeError)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Tuple

import warnings                              # NEW
import pandas as pd
from psycopg2 import extensions as _psx      # NEW
from psycopg2 import errors as pgerr
from psycopg2.extras import execute_values

from db_handler import DatabaseManager

# ───────── constants ─────────
DEFAULT_THRESHOLD   = 50
DEFAULT_AVERAGE     = 100
GENERIC_SUPPLIER_ID = 510
FIX_EXPIRY          = date(2027, 7, 21)
FIX_WH_LOC          = "A2"


class InventoryHandler(DatabaseManager):
    # ---------- lightweight wrappers (no nested ctx managers) -------------
    def fetch_data(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        """Warning‑free helper."""
        self._ensure_live_conn()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                message="pandas only supports SQLAlchemy connectable",
            )
            return pd.read_sql_query(sql, self.conn, params=params)

    def execute_command(self, sql: str, params: tuple = ()) -> None:
        """Commit only if we’re *not* inside an outer transaction."""
        self._ensure_live_conn()
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
        finally:
            cur.close()
            if (
                self.conn.get_transaction_status()
                == _psx.TRANSACTION_STATUS_IDLE
            ):
                self.conn.commit()

    def execute_command_returning(self, sql: str, params: tuple = ()) -> list:
        """Same idea as above, but returns cursor.fetchone()."""
        self._ensure_live_conn()
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            res = cur.fetchone()
        finally:
            cur.close()
            if (
                self.conn.get_transaction_status()
                == _psx.TRANSACTION_STATUS_IDLE
            ):
                self.conn.commit()
        return res

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

    # ---------- internal: restock one supplier in one TX -------------------
    def _restock_supplier(
        self,
        *,
        sup_id: int,
        items_df: pd.DataFrame,
        log_list: list,
        debug_dict: Dict[int, pd.DataFrame] | None,
    ) -> None:
        """
        Executes all inserts for one supplier inside **one** transaction.
        items_df columns: itemid | need | sellingprice
        Side‑effects:
            • Appends dicts to `log_list`
            • Optionally stores a debug copy in debug_dict[sup_id]
        """
        self._ensure_live_conn()

        for attempt in (1, 2):      # retry once if sequences were behind
            try:
                with self.conn:     # BEGIN … COMMIT
                    with self.conn.cursor() as cur:
                        # ---- keep all sequences ahead -------------------
                        for seq, tbl, pk in (
                            ("purchaseorders_poid_seq",      "purchaseorders",  "poid"),
                            ("purchaseorderitems_poitemid_seq", "purchaseorderitems", "poitemid"),
                            ("poitemcost_costid_seq",        "poitemcost",      "costid"),
                            ("inventory_batchid_seq",        "inventory",       "batchid"),
                        ):
                            self._sync_sequence(cur, seq, tbl, pk)

                        # ---- 1: PO header -------------------------------
                        cur.execute(
                            """
                            INSERT INTO purchaseorders
                                  (supplierid,status,orderdate,expecteddelivery,
                                   actualdelivery,createdby,suppliernote,totalcost)
                            VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,
                                    CURRENT_DATE,'AutoInventory','AUTO BULK',0)
                            RETURNING poid
                            """,
                            (sup_id,),
                        )
                        poid = int(cur.fetchone()[0])

                        # ---- 2: PO items & cost rows --------------------
                        items = []
                        for r in items_df.itertuples(index=False):
                            cpu = round(float(r.sellingprice) * 0.75, 2) if r.sellingprice else 0.0
                            items.append((int(r.itemid), int(r.need), cpu))

                        po_rows   = [(poid, it, q, q, cpu) for it, q, cpu in items]
                        cost_rows = [(poid, it, cpu, q, "Auto Refill") for it, q, cpu in items]

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

                        cost_ids = [
                            r[0]
                            for r in execute_values(
                                cur,
                                """
                                INSERT INTO poitemcost
                                      (poid,itemid,cost_per_unit,quantity,
                                       note,cost_date)
                                SELECT x.poid,x.itemid,x.cpu,x.qty,x.note,
                                       CURRENT_TIMESTAMP
                                FROM (VALUES %s) x(poid,itemid,cpu,qty,note)
                                RETURNING costid
                                """,
                                cost_rows,
                                fetch=True,
                            )
                        ]

                        # ---- 3: inventory rows -------------------------
                        inv_rows = [
                            (it, qty, FIX_EXPIRY, FIX_WH_LOC, cpu, poid, cid)
                            for (it, qty, cpu), cid in zip(items, cost_ids)
                        ]
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

                        # ---- 4: build Python‑side log ------------------
                        for (it, qty, cpu), cid in zip(items, cost_ids):
                            log_list.append(
                                dict(itemid=it, added=qty, cpu=cpu,
                                     poid=poid, costid=cid)
                            )

                        if debug_dict is not None:
                            debug_dict[sup_id] = items_df.copy()

                # success – exit retry loop
                break
            except pgerr.UniqueViolation:
                # sequence fell behind once more – sync & retry once
                if attempt == 1:
                    self.conn.rollback()
                    continue
                raise   # second failure ⇒ bubble up

    # ---------- public API -------------------------------------------------
    def restock_items_bulk(
        self,
        df_need: pd.DataFrame,
        *,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """
        Groups needed items by supplier and calls `_restock_supplier`
        (one transaction per supplier).
        """
        df_need       = df_need.copy()
        df_need["supplier"] = df_need["itemid"].apply(self.supplier_for)

        master_log: list = []
        debug_by_sup: Dict[int, pd.DataFrame] | None = {} if debug else None

        for sup_id, grp in df_need.groupby("supplier"):
            self._restock_supplier(
                sup_id=int(sup_id),
                items_df=grp[["itemid", "need", "sellingprice"]],
                log_list=master_log,
                debug_dict=debug_by_sup,
            )

        if debug:
            return {"log": master_log, "by_supplier": debug_by_sup}
        return {"log": master_log}
