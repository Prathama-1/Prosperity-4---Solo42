#6.5k
from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict
import numpy as np
import math
 
# ─── CONFIG ────────────────────────────────────────────────────
POSITION_LIMIT = 10
 
# Products we take directional positions in (based on 3-day trend analysis)
LONG_PRODUCTS = {
    "MICROCHIP_SQUARE",          # +24% trend
    "GALAXY_SOUNDS_BLACK_HOLES", # +35% trend
    "OXYGEN_SHAKE_GARLIC",       # +39% trend
    "SNACKPACK_STRAWBERRY",      # mild +9% trend
    "UV_VISOR_YELLOW",           # mild +7% trend
    "UV_VISOR_RED",              # mild +7% trend
    "UV_VISOR_MAGENTA",          # mild +5% trend
}
 
SHORT_PRODUCTS = {
    "MICROCHIP_OVAL",            # -28% trend
    "UV_VISOR_AMBER",            # -29% trend
    "PEBBLES_XS",                # -40% trend
    "MICROCHIP_RECTANGLE",       # -12% trend
    "MICROCHIP_TRIANGLE",        # -11% trend
}
 
# Market-make these (tight spread, mean-reverting)
MARKET_MAKE_PRODUCTS = {
    "SNACKPACK_RASPBERRY",
    "SNACKPACK_CHOCOLATE",
    "SNACKPACK_VANILLA",
    "SNACKPACK_PISTACHIO",
    "ROBOT_IRONING",
    "ROBOT_VACUUMING",
    "ROBOT_DISHES",
    "ROBOT_LAUNDRY",
    "ROBOT_MOPPING",
}
 
MM_EDGE = 1          # ticks away from best bid/ask we post our quotes
MM_MAX_POS = 8       # softer limit for market makers to avoid inventory blowup
 
 
class Trader:
 
    def __init__(self):
        self.price_history: Dict[str, List[float]] = {}
 
    # ── helpers ────────────────────────────────────────────────
 
    def mid(self, od: OrderDepth) -> float:
        bids = od.buy_orders
        asks = od.sell_orders
        if bids and asks:
            return (max(bids) + min(asks)) / 2
        if bids:
            return max(bids)
        if asks:
            return min(asks)
        return None
 
    def best_bid(self, od): return max(od.buy_orders) if od.buy_orders else None
    def best_ask(self, od): return min(od.sell_orders) if od.sell_orders else None
 
    # ── directional (momentum) strategy ────────────────────────
 
    def momentum_orders(self, product: str, od: OrderDepth, pos: int) -> List[Order]:
        orders = []
        mid = self.mid(od)
        if mid is None:
            return orders
 
        target = POSITION_LIMIT if product in LONG_PRODUCTS else -POSITION_LIMIT
        delta  = target - pos
 
        if delta == 0:
            return orders
 
        if delta > 0:
            # Need to buy — hit best ask aggressively
            ask = self.best_ask(od)
            if ask is None:
                return orders
            qty = min(delta, abs(od.sell_orders.get(ask, delta)))
            qty = min(qty, POSITION_LIMIT - pos)
            if qty > 0:
                orders.append(Order(product, ask, qty))
        else:
            # Need to sell — hit best bid aggressively
            bid = self.best_bid(od)
            if bid is None:
                return orders
            qty = min(-delta, abs(od.buy_orders.get(bid, -delta)))
            qty = min(qty, POSITION_LIMIT + pos)
            if qty > 0:
                orders.append(Order(product, bid, -qty))
 
        return orders
 
    # ── market-making strategy ──────────────────────────────────
 
    def mm_orders(self, product: str, od: OrderDepth, pos: int) -> List[Order]:
        orders = []
        bid = self.best_bid(od)
        ask = self.best_ask(od)
        if bid is None or ask is None:
            return orders
 
        mid = (bid + ask) / 2
        spread = ask - bid
 
        # Only post if spread wide enough to profit
        if spread < 4:
            return orders
 
        # Skew quotes based on inventory
        skew = pos / POSITION_LIMIT  # [-1, 1]
        our_bid = round(mid - MM_EDGE - skew * MM_EDGE)
        our_ask = round(mid + MM_EDGE - skew * MM_EDGE)
 
        # Post buy if not over long
        buy_capacity  = MM_MAX_POS - pos
        sell_capacity = MM_MAX_POS + pos
 
        if buy_capacity > 0 and our_bid < ask:
            orders.append(Order(product, our_bid,  min(3, buy_capacity)))
        if sell_capacity > 0 and our_ask > bid:
            orders.append(Order(product, our_ask, -min(3, sell_capacity)))
 
        return orders
 
    # ── main run ────────────────────────────────────────────────
 
    def run(self, state: TradingState):
        result = {}
        conversions = 0
 
        for product, od in state.order_depths.items():
            pos = state.position.get(product, 0)
            orders: List[Order] = []
 
            if product in LONG_PRODUCTS or product in SHORT_PRODUCTS:
                orders = self.momentum_orders(product, od, pos)
            elif product in MARKET_MAKE_PRODUCTS:
                orders = self.mm_orders(product, od, pos)
            # else: do nothing (random walk / no edge)
 
            if orders:
                result[product] = orders
 
        trader_data = ""
        return result, conversions, trader_data