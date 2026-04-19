#8.5k

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "position_limit": 80,
        "fair_value":     10_000,
        "take_width":     6,       # KEY FIX: was 1, now 6
                                   # Market opens ~7 above FV on day 1
                                   # Only take if TRULY mispriced (>6 away)
        "make_edge":      3,
        "soft_limit":     25,
        "skew_divisor":   3,
    },
    Product.IPR: {
        "position_limit":  80,
        "trend_per_tick":  0.001,
        "entry_premium":   10,     # Ask is typically FV+7, allow up to +10
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

    def bid(self):
        return 2500  # MAF bid — ignored in testing, real on final submission

    def _ipr_fv(self, state: TradingState, obj: dict) -> float:
        ts   = state.timestamp
        rate = self.params[Product.IPR]["trend_per_tick"]

        if "ipr_base" in obj:
            return obj["ipr_base"] + rate * ts

        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            mid  = (max(od.buy_orders) + min(od.sell_orders)) / 2
            base = round((mid - rate * ts) / 1000) * 1000
            obj["ipr_base"] = base
            return base + rate * ts

        return 13_000 + rate * ts

    def _trade_aco(self, state: TradingState) -> List[Order]:
        ap    = self.params[Product.ACO]
        od    = state.order_depths[Product.ACO]
        pos   = state.position.get(Product.ACO, 0)
        limit = self.LIMIT[Product.ACO]
        fv    = ap["fair_value"]
        soft  = ap["soft_limit"]
        orders: List[Order] = []
        bv = sv = 0

        if not od.sell_orders and not od.buy_orders:
            return orders

        # TAKE: only trade if significantly mispriced (take_width=6)
        # This prevents immediately shorting when market opens 7 above FV
        if od.sell_orders:
            best_ask = min(od.sell_orders)
            if best_ask <= fv - ap["take_width"]:
                qty = min(-od.sell_orders[best_ask], limit - pos - bv)
                if qty > 0:
                    orders.append(Order(Product.ACO, best_ask, qty))
                    bv += qty

        if od.buy_orders:
            best_bid = max(od.buy_orders)
            if best_bid >= fv + ap["take_width"]:
                qty = min(od.buy_orders[best_bid], limit + pos - sv)
                if qty > 0:
                    orders.append(Order(Product.ACO, best_bid, -qty))
                    sv += qty

        # MAKE: post quotes inside the ±8 typical spread
        edge  = ap["make_edge"]
        bid_p = fv - edge
        ask_p = fv + edge

        if pos > soft:
            skew = (pos - soft) // ap["skew_divisor"] + 1
            bid_p -= skew
            ask_p -= skew
        elif pos < -soft:
            skew = (-pos - soft) // ap["skew_divisor"] + 1
            bid_p += skew
            ask_p += skew

        bid_p = int(round(min(bid_p, fv - 1)))
        ask_p = int(round(max(ask_p, fv + 1)))
        if bid_p >= ask_p:
            bid_p = ask_p - 1

        buy_qty  = limit - (pos + bv)
        sell_qty = limit + (pos - sv)

        if buy_qty  > 0: orders.append(Order(Product.ACO, bid_p,  buy_qty))
        if sell_qty > 0: orders.append(Order(Product.ACO, ask_p, -sell_qty))

        return orders

    def _trade_ipr(self, state: TradingState, obj: dict) -> List[Order]:
        pp    = self.params[Product.IPR]
        od    = state.order_depths[Product.IPR]
        pos   = state.position.get(Product.IPR, 0)
        limit = self.LIMIT[Product.IPR]
        fv    = self._ipr_fv(state, obj)
        orders: List[Order] = []

        # Skip empty/zero-price ticks
        if not od.sell_orders and not od.buy_orders:
            return orders
        if od.sell_orders and min(od.sell_orders) <= 0:
            return orders

        # BUY: accumulate to limit, no adverse filter (all asks are trend flow)
        if pos < limit:
            ceiling = fv + pp["entry_premium"]
            for ask_p in sorted(od.sell_orders):
                if pos >= limit:
                    break
                if ask_p > ceiling:
                    break
                vol = -od.sell_orders[ask_p]
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, ask_p, qty))
                    pos += qty

        # HOLD: never sell before final tick
        # Every tick at 80 units = +8 XIRECs guaranteed

        # End-of-round liquidation only at ts=999900
        if state.timestamp >= 999_900 and pos > 0:
            if od.buy_orders:
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= 0:
                        break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

        return orders

    def run(self, state: TradingState):
        obj = {}
        if state.traderData:
            try:
                obj = jsonpickle.decode(state.traderData)
            except Exception:
                obj = {}

        result: Dict[str, List[Order]] = {}

        if Product.ACO in state.order_depths:
            result[Product.ACO] = self._trade_aco(state)

        if Product.IPR in state.order_depths:
            result[Product.IPR] = self._trade_ipr(state, obj)

        return result, 1, jsonpickle.encode(obj)