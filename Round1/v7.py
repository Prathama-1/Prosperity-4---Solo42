#9k


from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "fair_value":     10_000,
        "position_limit": 80,     # FIXED: was 20
        "take_width":     1,
        "make_edge":      2,
        "soft_limit":     40,     # FIXED: scaled proportionally (was 8)
    },
    Product.IPR: {
        "position_limit":  80,    # FIXED: was 20
        "trend_per_tick":  0.001,
        "entry_premium":   8,
        "exit_start_frac": 0.99,
        "adverse_volume":  15,
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

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

        return 12_000 + rate * ts

    def _trade_aco(self, state: TradingState) -> List[Order]:
        ap    = self.params[Product.ACO]
        od    = state.order_depths[Product.ACO]
        pos   = state.position.get(Product.ACO, 0)
        limit = self.LIMIT[Product.ACO]
        fv    = ap["fair_value"]
        soft  = ap["soft_limit"]
        orders: List[Order] = []
        bv = sv = 0

        # TAKE
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

        # MAKE
        bid_p = fv - ap["make_edge"]
        ask_p = fv + ap["make_edge"]

        if pos > soft:
            skew = (pos - soft) // 3 + 1
            bid_p -= skew; ask_p -= skew
        elif pos < -soft:
            skew = (-pos - soft) // 3 + 1
            bid_p += skew; ask_p += skew

        bid_p = round(min(bid_p, fv - 1))
        ask_p = round(max(ask_p, fv + 1))
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
        ts    = state.timestamp
        fv    = self._ipr_fv(state, obj)
        orders: List[Order] = []

        is_exit = ts >= pp["exit_start_frac"] * 999_900

        # BUY PHASE: accumulate to limit as fast as possible
        if not is_exit and pos < limit:
            ceiling = fv + pp["entry_premium"]
            for ask_p in sorted(od.sell_orders):
                if pos >= limit: break
                if ask_p > ceiling: break
                vol = -od.sell_orders[ask_p]
                if vol > pp["adverse_volume"]: continue
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, ask_p, qty))
                    pos += qty

        # SELL PHASE: liquidate everything
        elif is_exit and pos > 0:
            for bid_p in sorted(od.buy_orders, reverse=True):
                if pos <= 0: break
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