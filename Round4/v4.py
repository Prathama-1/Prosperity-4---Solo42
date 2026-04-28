#2.5k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:

    HYDROGEL_LIMIT = 200
    VELVET_LIMIT   = 200

    def __init__(self):
        self.vfe_prices: List[float] = []   # rolling trade price history

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        result["HYDROGEL_PACK"]       = self._trade_hydrogel(state)
        result["VELVETFRUIT_EXTRACT"] = self._trade_velvet(state)
        return result, 0, ""

    # ═══════════════════════════════════════════════════════════════════════
    # HYDROGEL — original 1.3k logic, untouched
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_hydrogel(self, state: TradingState) -> List[Order]:
        symbol = "HYDROGEL_PACK"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.HYDROGEL_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())
        mid: float    = (best_bid + best_ask) / 2

        sell_capacity = limit + pos
        buy_capacity  = limit - pos

        our_ask: int = int(mid + 6)
        if sell_capacity > 0:
            sell_qty = min(100, sell_capacity)
            orders.append(Order(symbol, our_ask, -sell_qty))

        AGGRESSIVE_BUY_THRESHOLD = 8
        if best_ask < mid - AGGRESSIVE_BUY_THRESHOLD and buy_capacity > 0:
            ask_vol = abs(depth.sell_orders[best_ask])
            buy_qty = min(ask_vol, buy_capacity, 50)
            if buy_qty > 0:
                orders.append(Order(symbol, best_ask, buy_qty))

        return orders

    # ═══════════════════════════════════════════════════════════════════════
    # VELVETFRUIT — Fade Mark 55 using absolute price levels
    #
    # FROM THIS LOG:
    #   Mark 55 BUY  zone: mean 5264.9 (range 5248–5296)
    #   Mark 55 SELL zone: mean 5256.2 (range 5244–5283)
    #   She loses 8.7 ticks per cycle — we capture it
    #
    # RULE:
    #   mid >= 5262 → Mark 55 is buying (paying too much) → WE SELL into her
    #   mid <= 5256 → Mark 55 is selling (selling too cheap) → WE BUY from her
    #   5256 < mid < 5262 → neutral zone → only unwind existing position
    #
    # WHY NOT ROLLING MEAN:
    #   Previous versions bought when Mark 55 sold, but price kept falling
    #   −5.6 ticks after every SUBMISSION buy. The rolling mean lags too much.
    #   Absolute price levels react immediately.
    #
    # EXECUTION:
    #   Always post PASSIVE orders (bid/ask) — never cross the spread
    #   Mark 55 hits our quotes since she's an aggressive taker
    #   Size scales with distance from neutral (5259 midpoint)
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_velvet(self, state: TradingState) -> List[Order]:
        symbol = "VELVETFRUIT_EXTRACT"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.VELVET_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())
        mid: float    = (best_bid + best_ask) / 2.0

        buy_capacity  = limit - pos
        sell_capacity = limit + pos

        # ── Price level thresholds (from log analysis) ────────────────────
        # Mark 55 buy zone  → we sell: mid >= 5262
        # Mark 55 sell zone → we buy:  mid <= 5256
        # Neutral:                      5256 < mid < 5262
        # These update each round as you get more data — hardcode for now
        SELL_THRESHOLD = 5262   # above this, Mark 55 buying → sell to her
        BUY_THRESHOLD  = 5256   # below this, Mark 55 selling → buy from her
        NEUTRAL_MID    = 5259   # midpoint between the two zones

        # Distance from neutral — used to scale order size
        dist = abs(mid - NEUTRAL_MID)   # 0 at neutral, grows to ~8 at extremes

        # ── SELL ZONE: mid >= SELL_THRESHOLD ─────────────────────────────
        # Mark 55 is buying aggressively above fair
        # Post passive ask — she will hit it
        if mid >= SELL_THRESHOLD and sell_capacity > 0:
            # Scale: bigger size the higher price goes
            if dist >= 8:
                qty = min(40, sell_capacity)
            elif dist >= 5:
                qty = min(25, sell_capacity)
            else:
                qty = min(15, sell_capacity)

            # Post at best_ask — Mark 55 hits aggressive asks
            orders.append(Order(symbol, best_ask, -qty))

            # Also post one tick inside if very extended (guarantees fill)
            if dist >= 6 and sell_capacity > qty:
                inside = best_ask - 1
                if inside > best_bid:
                    orders.append(Order(symbol, inside, -min(15, sell_capacity - qty)))

        # ── BUY ZONE: mid <= BUY_THRESHOLD ───────────────────────────────
        # Mark 55 is selling cheap below fair
        # Post passive bid — she will hit it
        elif mid <= BUY_THRESHOLD and buy_capacity > 0:
            if dist >= 8:
                qty = min(40, buy_capacity)
            elif dist >= 5:
                qty = min(25, buy_capacity)
            else:
                qty = min(15, buy_capacity)

            # Post at best_bid — Mark 55 hits aggressive bids
            orders.append(Order(symbol, best_bid, qty))

            # Also post one tick inside if very extended
            if dist >= 6 and buy_capacity > qty:
                inside = best_bid + 1
                if inside < best_ask:
                    orders.append(Order(symbol, inside, min(15, buy_capacity - qty)))

        # ── NEUTRAL ZONE: unwind inventory toward zero ────────────────────
        # We're between thresholds — no directional edge
        # Gently close out any open position at best available price
        else:
            if pos > 10 and sell_capacity > 0:
                # Long → post ask at best_ask to exit
                orders.append(Order(symbol, best_ask, -min(20, sell_capacity)))
            elif pos < -10 and buy_capacity > 0:
                # Short → post bid at best_bid to cover
                orders.append(Order(symbol, best_bid, min(20, buy_capacity)))

        return orders