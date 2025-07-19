"""
Auto‑refill Sales Simulator for AMAS
------------------------------------
• Generates synthetic POS sales (same cart logic you already use)
• Executes each sale via CashierHandler.process_sale_with_shortage
• Immediately restocks any shelf item that drops below its threshold
  using ShelfHandler.transfer_from_inventory, oldest‑expiry first
• Resolves open shortages before placing new stock on shelf
"""

from __future__ import annotations
import random
from datetime import datetime
import pandas as pd
from cashier.cashier_handler import CashierHandler
from selling_area.shelf_handler import ShelfHandler

# ───────────────────────── connections ────────────────────────────
cashier = CashierHandler()          # re‑use your existing db wrappers
shelf   = ShelfHandler()

# ───────────────────────── cached master data ─────────────────────
ITEM_META = (
    shelf.get_all_items()           # itemid | itemname | shelfthreshold | shelfaverage
         .set_index("itemid")
)

# ───────────────────────── cart generator (same idea as Bulk POS) ─
def random_cart(cat_df: pd.DataFrame,
                min_items: int = 2,
                max_items: int = 6,
                min_qty: int   = 1,
                max_qty: int   = 5) -> list[dict]:
    """Return a random, *unique‑item* cart."""
    n_items = random.randint(min_items, min(max_items, len(cat_df)))
    picks   = cat_df.sample(n=n_items, replace=False)
    return [
        {
            "itemid"      : int(r.itemid),
            "quantity"    : random.randint(min_qty, max_qty),
            "sellingprice": float(r.sellingprice)
        }
        for _, r in picks.iterrows()
    ]

# ───────────────────────── inventory helpers ──────────────────────
def _inventory_layers(itemid: int) -> pd.DataFrame:
    """Return FIFO‑ordered cost layers for an item that still have stock."""
    return shelf.fetch_data(
        """
        SELECT expirationdate, quantity, cost_per_unit
        FROM   inventory
        WHERE  itemid = %s AND quantity > 0
        ORDER  BY expirationdate, cost_per_unit
        """,
        (itemid,),
    )

def _choose_layers(itemid: int, need_qty: int) -> list[dict]:
    """Pick cost layers (oldest expiry first) that cover `need_qty`."""
    plan   : list[dict] = []
    remain : int        = need_qty
    for row in _inventory_layers(itemid).itertuples():
        take = min(remain, int(row.quantity))
        plan.append({
            "expirationdate": row.expirationdate,
            "qty"          : take,
            "cost"         : float(row.cost_per_unit),
        })
        remain -= take
        if remain == 0:
            break
    return plan

# ───────────────────────── refill logic ───────────────────────────
def restock_item(itemid: int, *, user="AUTOSIM") -> None:
    """
    Bring shelf stock for `itemid` up to its shelfaverage
    (or at least shelfthreshold) if it is below threshold.
    """
    # current shelf qty + policy
    shelf_kpis = shelf.get_shelf_quantity_by_item()
    row = shelf_kpis.loc[shelf_kpis.itemid == itemid]

    current     = int(row.totalquantity.iloc[0]) if not row.empty else 0
    threshold   = int(ITEM_META.at[itemid, "shelfthreshold"] or 0)
    target      = int(ITEM_META.at[itemid, "shelfaverage"]   or threshold or 0)

    if current >= threshold:
        return  # nothing to do

    need = max(target - current, 0)
    if need == 0:
        need = threshold - current      # fallback

    # 1) resolve open shortages first
    need = shelf.resolve_shortages(itemid=itemid, qty_need=need, user=user)
    if need <= 0:
        return                          # shortages ate everything

    # 2) pull layers from inventory -> shelf
    for layer in _choose_layers(itemid, need):
        shelf.transfer_from_inventory(
            itemid        = itemid,
            expirationdate= layer["expirationdate"],
            quantity      = layer["qty"],
            cost_per_unit = layer["cost"],
            created_by    = user,
        )
        need -= layer["qty"]
        if need <= 0:
            break

    if need > 0:
        # Not enough back‑store stock – leave a shortage ticket
        shelf.execute_command(
            """
            INSERT INTO shelf_shortage (itemid, shortage_qty, logged_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            """,
            (itemid, need),
        )

def post_sale_restock(cart: list[dict], *, user="AUTOSIM") -> None:
    """Loop through items sold and restock if shelf dipped below threshold."""
    for entry in cart:
        restock_item(int(entry["itemid"]), user=user)

# ───────────────────────── main simulation loop ────────────────────
def simulate_sales(num_sales: int = 50,
                   min_items: int = 2,
                   max_items: int = 6,
                   min_qty  : int = 1,
                   max_qty  : int = 5,
                   pay_method: str = "Cash",
                   user_tag: str = "SIM") -> pd.DataFrame:
    """
    Generates `num_sales` synthetic transactions and auto‑refills shelves.
    Returns a DataFrame of sale‑level outcomes for inspection.
    """
    cat_df  = cashier.fetch_data("""
        SELECT itemid, sellingprice
        FROM   item
        WHERE  sellingprice IS NOT NULL AND sellingprice > 0
    """)
    if cat_df.empty:
        raise RuntimeError("Catalogue empty: cannot simulate sales.")

    results = []
    for n in range(num_sales):
        cart      = random_cart(cat_df, min_items, max_items, min_qty, max_qty)
        try:
            saleid, shortages = cashier.process_sale_with_shortage(
                cart_items     = cart,
                discount_rate  = 0.0,
                payment_method = pay_method,
                cashier        = user_tag,
                notes          = f"[AUTO SIM {datetime.utcnow():%Y-%m-%d}]",
            )
            status = "OK" if saleid else "FAIL"
        except Exception as e:
            cashier.conn.rollback()
            saleid  = None
            status  = f"DB_ERROR: {e}"
            shortages = []

        # After sale: refill each sold item if shelf low
        if saleid:
            post_sale_restock(cart, user=user_tag)

        results.append({
            "sale_no"  : n + 1,
            "sale_id"  : saleid,
            "items"    : len(cart),
            "status"   : status,
            "shortages": shortages,
        })

    return pd.DataFrame(results)

# ───────────────────────── CLI entry ‑ simple demo ────────────────
if __name__ == "__main__":
    import argparse, sys
    argp = argparse.ArgumentParser(description="Run POS + auto‑refill simulator.")
    argp.add_argument("--sales", type=int, default=100,
                      help="Number of synthetic sales to generate.")
    a = argp.parse_args(sys.argv[1:])

    df_out = simulate_sales(num_sales=a.sales)
    print(df_out)
    print("\nSucceeded:",
          (df_out.status == "OK").sum(), "/", len(df_out),
          "sales.")

