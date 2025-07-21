# handler/inventory_handler.py  (replaces previous version)
from __future__ import annotations
from datetime import date
import pandas as pd
from psycopg2.extras import execute_values
from db_handler import DatabaseManager

DEFAULT_THRESHOLD = 50
DEFAULT_AVERAGE   = 100

class InventoryHandler(DatabaseManager):
    # ───────── snapshot (unchanged) ─────────
    def stock_levels(self) -> pd.DataFrame:
        inv  = self.fetch_data("SELECT itemid, SUM(quantity) AS totalqty "
                               "FROM inventory GROUP BY itemid")
        if inv.empty:
            inv = pd.DataFrame(columns=["itemid", "totalqty"])

        meta = self.fetch_data(
            f"""
            SELECT itemid,
                   itemnameenglish,
                   COALESCE(threshold,{DEFAULT_THRESHOLD})       AS threshold,
                   COALESCE(averagerequired,{DEFAULT_AVERAGE})  AS average,
                   COALESCE(sellingprice,0)*0.75                AS cpu     -- 25 % margin
            FROM   item
            """
        )
        df = meta.merge(inv, on="itemid", how="left")
        df["totalqty"] = df["totalqty"].fillna(0).astype(int)
        return df

    # ───────── helpers (unchanged) ─────────
    def supplier_for(self, itemid: int) -> int | None:
        res = self.fetch_data(
            "SELECT supplierid FROM itemsupplier WHERE itemid=%s LIMIT 1", (itemid,)
        )
        return None if res.empty else int(res.iloc[0, 0])

    def _bulk_insert_inventory(self, rows: list[tuple]):
        """
        rows -> tuples (itemid, qty, exp, loc, cpu, poid, costid)
        executed in ONE commit.
        """
        if not rows:
            return
        sql = ("INSERT INTO inventory "
               "(itemid,quantity,expirationdate,storagelocation,"
               " cost_per_unit,poid,costid) VALUES %s")
        self._ensure_live_conn()
        with self.conn:
            with self.conn.cursor() as cur:
                execute_values(cur, sql, rows)

    # ───────── batch restock (NEW fast path) ─────────
    def batch_restock(self, df_need: pd.DataFrame) -> list[dict]:
        """
        df_need rows: itemid, need, cpu
        Creates ONE PO per supplier and buckets items.
        Returns action‑log for UI.
        """
        if df_need.empty:
            return []

        # 1️⃣ group rows by supplier
        buckets: dict[int, list[tuple]] = {}
        for row in df_need.itertuples(index=False):
            sup = self.supplier_for(int(row.itemid))
            if sup is None:
                continue
            buckets.setdefault(sup, []).append(row)

        actions, inv_rows = [], []

        # 2️⃣ for each supplier create PO + rows
        for sup, items in buckets.items():
            poid = self.execute_command_returning(
                "INSERT INTO purchaseorders "
                "(supplierid,status,orderdate,expecteddelivery,actualdelivery,"
                " createdby,suppliernote,totalcost) "
                "VALUES (%s,'Completed',CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,"
                " 'AutoInventory','AUTO',0.0) RETURNING poid",
                (sup,),
            )[0]

            for r in items:
                # purchaseorderitems / poitemcost
                self.execute_command(
                    "INSERT INTO purchaseorderitems "
                    "(poid,itemid,orderedquantity,receivedquantity,estimatedprice) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (poid, int(r.itemid), int(r.need), int(r.need), float(r.cpu)),
                )
                costid = self.execute_command_returning(
                    "INSERT INTO poitemcost "
                    "(poid,itemid,cost_per_unit,quantity,cost_date,note) "
                    "VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP,'AUTO') RETURNING costid",
                    (poid, int(r.itemid), float(r.cpu), int(r.need)),
                )[0]

                inv_rows.append((
                    int(r.itemid), int(r.need), date(2027, 7, 21),  # fixed expiry
                    "A2", float(r.cpu), poid, costid
                ))
                actions.append(
                    dict(item=r.itemnameenglish,
                         added=int(r.need),
                         supplier=sup,
                         poid=poid)
                )

            # cheap total‑cost refresh
            self.execute_command(
                "UPDATE purchaseorders SET totalcost = (SELECT SUM(quantity*cost_per_unit)"
                " FROM poitemcost WHERE poid=%s) WHERE poid=%s", (poid, poid)
            )

        # 3️⃣ bulk insert inventory rows
        self._bulk_insert_inventory(inv_rows)
        return actions
