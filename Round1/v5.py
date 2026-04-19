# 6k

from datamodel import OrderDepth, TradingState, Order, ConversionObservation
from typing import List, Dict, Optional
import jsonpickle


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "fair_value": 10_000,

        # Asymmetric edge (proven strong)
        "bid_edge": 1,
        "ask_edge": 4,

        "take_width_buy": 1,
        "take_width_sell": 5,

        "position_limit": 80,

        # Let it stay long
        "long_skew_tiers": [60, 72],
        "short_skew_tiers": [0, 20, 40],
    },

    Product.IPR: {
        "trend_per_tick": 0.001,

        # smarter thresholds
        "accumulate_edge": 3,     # buy if price <= fair + 3
        "reduce_edge": 6,         # sell if price >= fair + 6

        "max_hold": 60,           # don’t always go max 80
        "min_hold": 10,           # always keep some exposure

        "adverse_volume": 15,
        "position_limit": 80,
    }
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {
            Product.ACO: 80,
            Product.IPR: 80
        }

    # ─────────────────────────────────────────────
    # IPR FAIR VALUE (stable)
    # ─────────────────────────────────────────────
    def _ipr_fair_value(self, state: TradingState, trader_obj: dict) -> float:
        p = self.params[Product.IPR]
        ts = state.timestamp

        if "ipr_base" in trader_obj:
            return trader_obj["ipr_base"] + p["trend_per_tick"] * ts

        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders) + min(od.sell_orders)) / 2
            base = mid - p["trend_per_tick"] * ts
            base = round(base / 1000) * 1000
            trader_obj["ipr_base"] = base
            return base + p["trend_per_tick"] * ts

        return 12_000 + p["trend_per_tick"] * ts

    # ─────────────────────────────────────────────
    # ACO: STRONG BIASED MARKET MAKER (your 4k edge)
    # ─────────────────────────────────────────────
    def _trade_aco(self, state: TradingState) -> List[Order]:
        p = self.params[Product.ACO]
        od = state.order_depths[Product.ACO]
        pos = state.position.get(Product.ACO, 0)
        limit = p["position_limit"]
        fv = p["fair_value"]

        orders: List[Order] = []
        bv = sv = 0

        # --- TAKE (biased) ---
        for ask in sorted(od.sell_orders):
            if ask > fv - p["take_width_buy"]:
                break
            vol = -od.sell_orders[ask]
            qty = min(vol, limit - pos - bv)
            if qty > 0:
                orders.append(Order(Product.ACO, ask, qty))
                bv += qty

        for bid in sorted(od.buy_orders, reverse=True):
            if bid < fv + p["take_width_sell"]:
                break
            vol = od.buy_orders[bid]
            qty = min(vol, limit + pos - sv)
            if qty > 0:
                orders.append(Order(Product.ACO, bid, -qty))
                sv += qty

        # --- MAKE (asymmetric) ---
        bid_price = round(fv - p["bid_edge"])
        ask_price = round(fv + p["ask_edge"])

        # skew
        if pos > 0:
            skew = sum(1 for t in p["long_skew_tiers"] if pos > t)
            bid_price -= skew
            ask_price -= skew
        elif pos < 0:
            skew = sum(1 for t in p["short_skew_tiers"] if abs(pos) > t)
            bid_price += skew
            ask_price += skew

        buy_qty = limit - (pos + bv)
        sell_qty = limit + (pos - sv)

        if buy_qty > 0:
            orders.append(Order(Product.ACO, bid_price, buy_qty))
        if sell_qty > 0:
            orders.append(Order(Product.ACO, ask_price, -sell_qty))

        return orders

    # ─────────────────────────────────────────────
    # IPR: ADAPTIVE TREND + MM HYBRID
    # ─────────────────────────────────────────────
    def _trade_ipr(self, state: TradingState, trader_obj: dict) -> List[Order]:
        p = self.params[Product.IPR]
        od = state.order_depths[Product.IPR]
        pos = state.position.get(Product.IPR, 0)
        limit = p["position_limit"]
        fv = self._ipr_fair_value(state, trader_obj)

        orders: List[Order] = []

        # --- ACCUMULATE (but controlled) ---
        for ask in sorted(od.sell_orders):
            if ask > fv + p["accumulate_edge"]:
                break
            vol = -od.sell_orders[ask]

            if vol > p["adverse_volume"]:
                continue

            # don't overbuy
            if pos >= p["max_hold"]:
                break

            qty = min(vol, p["max_hold"] - pos)
            if qty > 0:
                orders.append(Order(Product.IPR, ask, qty))
                pos += qty

        # --- REDUCE (take profit) ---
        for bid in sorted(od.buy_orders, reverse=True):
            if bid < fv + p["reduce_edge"]:
                break

            if pos <= p["min_hold"]:
                break

            vol = od.buy_orders[bid]
            qty = min(vol, pos - p["min_hold"])
            if qty > 0:
                orders.append(Order(Product.IPR, bid, -qty))
                pos -= qty

        # --- PASSIVE MM around trend ---
        bid_price = round(fv - 2)
        ask_price = round(fv + 2)

        buy_qty = limit - pos
        sell_qty = pos

        if buy_qty > 0:
            orders.append(Order(Product.IPR, bid_price, buy_qty))
        if sell_qty > 0:
            orders.append(Order(Product.IPR, ask_price, -sell_qty))

        return orders

    # ─────────────────────────────────────────────
    # CONVERSION (optional, safe)
    # ─────────────────────────────────────────────
    def _conversion(self, state: TradingState) -> int:
        product = Product.IPR
        if product not in state.order_depths:
            return 0

        obs = None
        if hasattr(state, "observations") and state.observations:
            obs = getattr(state.observations, "conversionObservations", {}).get(product)

        if not obs:
            return 0

        od = state.order_depths[product]
        pos = state.position.get(product, 0)
        limit = self.LIMIT[product]

        auth_bid = getattr(obs, "bidPrice", 0)
        auth_ask = getattr(obs, "askPrice", 0)

        # simple safe arb
        if od.buy_orders:
            best_bid = max(od.buy_orders)
            if best_bid > auth_ask:
                return min(limit + pos, od.buy_orders[best_bid])

        if od.sell_orders:
            best_ask = min(od.sell_orders)
            if best_ask < auth_bid:
                return -min(limit - pos, -od.sell_orders[best_ask])

        return 0

    # ─────────────────────────────────────────────
    # MAIN
    # ─────────────────────────────────────────────
    def run(self, state: TradingState):
        trader_obj = {}
        if state.traderData:
            try:
                trader_obj = jsonpickle.decode(state.traderData)
            except:
                trader_obj = {}

        result = {}

        if Product.ACO in state.order_depths:
            result[Product.ACO] = self._trade_aco(state)

        if Product.IPR in state.order_depths:
            result[Product.IPR] = self._trade_ipr(state, trader_obj)

        conversions = self._conversion(state)

        return result, conversions, jsonpickle.encode(trader_obj)