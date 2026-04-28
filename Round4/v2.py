#1.3k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:

    POSITION_LIMIT = 200

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        result["HYDROGEL_PACK"] = self._trade_hydrogel(state)
        return result, 0, ""

    def _trade_hydrogel(self, state: TradingState) -> List[Order]:
        symbol = "HYDROGEL_PACK"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth: OrderDepth = state.order_depths[symbol]
        pos: int = state.position.get(symbol, 0)
        limit = self.POSITION_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())
        mid: float = (best_bid + best_ask) / 2

        sell_capacity = limit + pos  # how short we can go (pos=-200 → 0)
        buy_capacity  = limit - pos  # only used for aggressive buys

        # ── SELL SIDE (passive) ───────────────────────────────────────────
        # Post above Mark 14's buy zone so he ignores us.
        # Mark 38 pays mid+15 to mid+25 when buying aggressively.
        # mid+12 sits above Mark 14's ceiling (~mid+8) but below Mark 38's ceiling.
        # This means only Mark 38 hits our ask — not Mark 14.

        our_ask: int = int(mid + 6)

        if sell_capacity > 0:
            sell_qty = min(100, sell_capacity)
            orders.append(Order(symbol, our_ask, -sell_qty))

        # ── BUY SIDE (aggressive only, no passive bids) ───────────────────
        # Never post passive bids — Mark 14 exploits them.
        # Only buy aggressively when Mark 38 is selling well below mid.
        # From logs: Mark 38 sells at ~10006-10010, mid is ~10015-10020 at those times
        # So if best_ask is more than 8 below mid, that's Mark 38 selling cheap → take it.

        AGGRESSIVE_BUY_THRESHOLD = 8  # best_ask is this far below mid

        if best_ask < mid - AGGRESSIVE_BUY_THRESHOLD and buy_capacity > 0:
            # Hit the ask directly — market order style
            ask_vol = abs(depth.sell_orders[best_ask])
            buy_qty = min(ask_vol, buy_capacity, 50)
            if buy_qty > 0:
                orders.append(Order(symbol, best_ask, buy_qty))

        return orders