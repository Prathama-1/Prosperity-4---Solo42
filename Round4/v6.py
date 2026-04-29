from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math


class Trader:

    HYDROGEL_LIMIT = 200
    VELVET_LIMIT   = 200
    VEV_LIMIT      = 300

    # Approx delta map (kept — structural, not overfit)
    VEV_DELTA = {
        "VEV_4000": 1.00,
        "VEV_4500": 1.00,
        "VEV_5000": 0.88,
        "VEV_5100": 0.79,
        "VEV_5200": 0.66,
        "VEV_5300": 0.50,
        "VEV_5400": 0.34,
        "VEV_5500": 0.20,
        "VEV_6000": 0.01,
        "VEV_6500": 0.00,
    }

    def __init__(self):
        self.vfe_prices: List[float] = []

    # ═══════════════════════════════════════════════════════════════════════
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        result["HYDROGEL_PACK"]       = self._trade_hydrogel(state)
        result["VELVETFRUIT_EXTRACT"] = self._trade_velvet(state)

        vev_orders, hedge_orders = self._trade_vev(state)

        for sym, orders in vev_orders.items():
            result[sym] = orders

        if hedge_orders:
            existing = result.get("VELVETFRUIT_EXTRACT", [])
            result["VELVETFRUIT_EXTRACT"] = self._merge_vfe_hedge(
                existing, hedge_orders, state
            )

        return result, 0, ""

    # ═══════════════════════════════════════════════════════════════════════
    # HYDROGEL (slightly adaptive)
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_hydrogel(self, state: TradingState):
        symbol = "HYDROGEL_PACK"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.HYDROGEL_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid = max(depth.buy_orders)
        best_ask = min(depth.sell_orders)
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        sell_capacity = limit + pos
        buy_capacity  = limit - pos

        # adaptive quoting
        edge = max(3, spread / 2)

        if sell_capacity > 0:
            orders.append(Order(symbol, int(mid + edge), -min(100, sell_capacity)))

        # opportunistic buy
        if best_ask < mid - edge and buy_capacity > 0:
            vol = abs(depth.sell_orders[best_ask])
            orders.append(Order(symbol, best_ask, min(vol, buy_capacity, 50)))

        return orders

    # ═══════════════════════════════════════════════════════════════════════
    # VFE (dynamic fair value)
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_velvet(self, state: TradingState):
        symbol = "VELVETFRUIT_EXTRACT"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.VELVET_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid = max(depth.buy_orders)
        best_ask = min(depth.sell_orders)
        mid = (best_bid + best_ask) / 2

        # ── dynamic fair value (rolling mean) ──
        self.vfe_prices.append(mid)
        window = 20
        fair = sum(self.vfe_prices[-window:]) / min(len(self.vfe_prices), window)

        # volatility estimate
        if len(self.vfe_prices) > 10:
            mean = fair
            var = sum((p - mean) ** 2 for p in self.vfe_prices[-window:]) / window
            vol = math.sqrt(var)
        else:
            vol = 2

        buy_capacity  = limit - pos
        sell_capacity = limit + pos

        threshold = max(2, vol)

        # mean reversion logic
        if mid > fair + threshold and sell_capacity > 0:
            size = int(min(sell_capacity, 10 + 5 * (mid - fair) / threshold))
            orders.append(Order(symbol, best_ask, -size))

        elif mid < fair - threshold and buy_capacity > 0:
            size = int(min(buy_capacity, 10 + 5 * (fair - mid) / threshold))
            orders.append(Order(symbol, best_bid, size))

        # inventory control
        if pos > 50:
            orders.append(Order(symbol, best_ask, -min(20, sell_capacity)))
        elif pos < -50:
            orders.append(Order(symbol, best_bid, min(20, buy_capacity)))

        return orders

    # ═══════════════════════════════════════════════════════════════════════
    # VEV (de-overfit short vol)
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_vev(self, state: TradingState):

        vev_orders: Dict[str, List[Order]] = {}
        hedge_orders: List[Order] = []

        # get spot price
        if "VELVETFRUIT_EXTRACT" not in state.order_depths:
            return vev_orders, hedge_orders

        vfe_depth = state.order_depths["VELVETFRUIT_EXTRACT"]
        if not vfe_depth.buy_orders or not vfe_depth.sell_orders:
            return vev_orders, hedge_orders

        spot = (max(vfe_depth.buy_orders) + min(vfe_depth.sell_orders)) / 2

        # net delta
        net_delta = 0.0
        for sym, delta in self.VEV_DELTA.items():
            pos = state.position.get(sym, 0)
            net_delta += pos * delta

        for symbol, depth in state.order_depths.items():

            if not symbol.startswith("VEV_"):
                continue

            if symbol not in self.VEV_DELTA:
                continue

            pos   = state.position.get(symbol, 0)
            limit = self.VEV_LIMIT
            sell_capacity = limit + pos

            if sell_capacity <= 0 or not depth.buy_orders:
                continue

            best_bid = max(depth.buy_orders)
            bid_vol  = abs(depth.buy_orders[best_bid])

            strike = int(symbol.split("_")[1])

            # intrinsic value
            intrinsic = max(0, spot - strike)

            # adaptive buffer (key change)
            buffer = max(5, 0.02 * spot)

            if best_bid < intrinsic + buffer:
                continue

            # adaptive sizing
            mispricing = best_bid - intrinsic
            size = int(min(
                sell_capacity,
                bid_vol,
                5 + mispricing / 10
            ))

            if size <= 0:
                continue

            vev_orders[symbol] = [Order(symbol, best_bid, -size)]

            net_delta += (-size) * self.VEV_DELTA[symbol]

        # ── delta hedge ──
        hedge_qty = int(round(-net_delta))

        if hedge_qty > 0 and vfe_depth.sell_orders:
            best_ask = min(vfe_depth.sell_orders)
            hedge_orders.append(Order("VELVETFRUIT_EXTRACT", best_ask, hedge_qty))

        return vev_orders, hedge_orders

    # ═══════════════════════════════════════════════════════════════════════
    def _merge_vfe_hedge(self, velvet_orders, hedge_orders, state):

        symbol = "VELVETFRUIT_EXTRACT"
        pos    = state.position.get(symbol, 0)
        limit  = self.VELVET_LIMIT

        committed = sum(o.quantity for o in velvet_orders)
        hedge     = sum(o.quantity for o in hedge_orders)

        projected = pos + committed + hedge

        if -limit <= projected <= limit:
            return velvet_orders + hedge_orders

        # clip hedge
        if hedge > 0:
            room = limit - pos - committed
            hedge = min(room, hedge)
        else:
            room = limit + pos + committed
            hedge = max(-room, hedge)

        if hedge == 0:
            return velvet_orders

        ref = hedge_orders[0]
        return velvet_orders + [Order(symbol, ref.price, hedge)]