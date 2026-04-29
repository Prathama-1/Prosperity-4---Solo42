#13.2k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict

# ══════════════════════════════════════════════════════════════════
#  IMC PROSPERITY 4 — ROUND 5  |  BUY & HOLD STRATEGY
#
#  Logic per product:
#    Phase 1  (ts < 999500) : accumulate to +10 by hitting ask
#    Phase 2  (ts >= 999500): sell everything by hitting bid
#
#  Exit window = last 5 ticks (ts 999500-999900).
#  Historical bid_volume at those ticks is always 7-23, comfortably
#  absorbs our full position of 10 in one shot.
#
#  No stop-loss. These products trended up on every day we have data.
# ══════════════════════════════════════════════════════════════════

POSITION_LIMIT = 10

# Day-win rate = fraction of observed days where daily close > open
LONG_PRODUCTS = {
    "GALAXY_SOUNDS_BLACK_HOLES",  # 3/3 days up | net +35% over 3 days
    "OXYGEN_SHAKE_GARLIC",        # 3/3 days up | net +39% over 3 days
    "SNACKPACK_STRAWBERRY",       # 3/3 days up | net +9%  over 3 days
    "UV_VISOR_RED",               # 3/3 days up | net +26% over 3 days
    "SLEEP_POD_POLYESTER",        # 2/3 days up | net +28% over 3 days
    "SLEEP_POD_SUEDE",            # 2/3 days up | net +18% over 3 days
    "PEBBLES_XL",                 # 2/3 days up | net +61% over 3 days
    "UV_VISOR_MAGENTA",           # 2/3 days up | net +15% over 3 days
    "TRANSLATOR_VOID_BLUE",       # 2/3 days up | net +15% over 3 days
    "PANEL_2X4",                  # 3/3 days up | net +23% over 3 days
}

# We start selling at the second-to-last tick group (ts >= 999500).
# This gives us 5 ticks (999500, 999600, 999700, 999800, 999900) to exit.
# Observed bid_volume in that window: always >= 7, usually 10-23.
# Our max position is 10, so we always clear in the first exit tick.
EXIT_START_TS = 999500


class Trader:

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        for product, od in state.order_depths.items():

            if product not in LONG_PRODUCTS:
                continue

            pos = state.position.get(product, 0)
            ts  = state.timestamp
            orders: List[Order] = []

            # ── PHASE 2: EXIT (last 5 ticks) ─────────────────────
            if ts >= EXIT_START_TS:
                if pos > 0:
                    # Hit the best bid to guarantee a fill
                    if od.buy_orders:
                        best_bid = max(od.buy_orders)
                        orders.append(Order(product, best_bid, -pos))

            # ── PHASE 1: ACCUMULATE (everything before exit window)
            else:
                buy_needed = POSITION_LIMIT - pos
                if buy_needed > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders)
                    # Don't exceed what's available at that price level
                    available = abs(od.sell_orders[best_ask])
                    qty = min(buy_needed, available)
                    if qty > 0:
                        orders.append(Order(product, best_ask, qty))

            if orders:
                result[product] = orders

        return result, 0, ""