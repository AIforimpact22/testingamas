    # ───────── shortage resolver (transfer‑side) ─────────
    def resolve_shortages(
        self, *, itemid: int, qty_need: int, user: str
    ) -> int:
        """
        Consume open shortages for *itemid* (oldest first).
        Returns any quantity still **uncovered** (≥ 0).

        Rows that reach 0 are **deleted** to satisfy the CHECK
        (shortage_qty > 0) and NOT‑NULL saleid constraint.
        """
        rows = self.fetch_data(
            """
            SELECT shortageid, shortage_qty
            FROM   shelf_shortage
            WHERE  itemid   = %s
              AND  resolved = FALSE
            ORDER  BY logged_at;
            """,
            (itemid,),
        )

        remaining = qty_need
        for r in rows.itertuples():
            if remaining == 0:
                break

            take = min(remaining, int(r.shortage_qty))

            if take == r.shortage_qty:
                # fully satisfied → delete the row
                self.execute_command(
                    "DELETE FROM shelf_shortage WHERE shortageid = %s;",
                    (r.shortageid,),
                )
            else:
                # partially satisfied
                self.execute_command(
                    """
                    UPDATE shelf_shortage
                    SET    shortage_qty = shortage_qty - %s,
                           resolved_qty  = COALESCE(resolved_qty, 0) + %s,
                           resolved_by   = %s,
                           resolved_at   = CURRENT_TIMESTAMP
                    WHERE  shortageid = %s;
                    """,
                    (take, take, user, r.shortageid),
                )

            remaining -= take

        return remaining

    # ───────── auto‑refill helpers (called by POS) ─────────
    def restock_item(
        self,
        itemid: int,
        *,
        saleid: int | None = None,   # ← NEW: carry POS saleid when available
        user:   str = "AUTOSIM",
    ) -> None:
        """Bring shelf stock for *itemid* up to its threshold/average."""
        kpis = self.get_shelf_quantity_by_item()
        row  = kpis.loc[kpis.itemid == itemid]

        current   = int(row.totalquantity.iloc[0]) if not row.empty else 0
        threshold = int(row.shelfthreshold.iloc[0] or 0)
        target    = int(row.shelfaverage  .iloc[0] or threshold or 0)

        if current >= threshold:
            return  # already healthy

        need = max(target - current, threshold - current)

        # 1️⃣  resolve open shortages first
        need = self.resolve_shortages(itemid=itemid, qty_need=need, user=user)
        if need <= 0:
            return

        # 2️⃣  move oldest inventory layers → shelf
        layers = self.fetch_data(
            """
            SELECT expirationdate, quantity, cost_per_unit
            FROM   inventory
            WHERE  itemid = %s AND quantity > 0
            ORDER  BY expirationdate, cost_per_unit;
            """,
            (itemid,),
        )
        for lyr in layers.itertuples():
            take = min(need, int(lyr.quantity))
            self.transfer_from_inventory(
                itemid, lyr.expirationdate, take,
                float(lyr.cost_per_unit), user
            )
            need -= take
            if need <= 0:
                break

        # 3️⃣  still short? → log shortage ticket (saleid = 0 if background)
        if need > 0:
            self.execute_command(
                """
                INSERT INTO shelf_shortage (saleid, itemid, shortage_qty, logged_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP);
                """,
                (saleid or 0, itemid, need),
            )

    def post_sale_restock(
        self,
        cart:   list[dict],
        *,
        saleid: int,
        user:   str = "AUTOSIM",
    ) -> None:
        """Call once per successful sale to refill every sold SKU."""
        for entry in cart:
            self.restock_item(int(entry["itemid"]), saleid=saleid, user=user)
