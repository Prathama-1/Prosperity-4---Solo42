#7.8k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "position_limit": 80,
        "fair_value":     10_000,   # confirmed: no drift across all 3 days
        "take_width":     1,        # take if ask <= 9999 or bid >= 10001
        "make_edge":      3,        # quote fv±3, inside the typical ±8 spread
        "soft_limit":     25,       # start skewing earlier for tighter inventory
        "skew_divisor":   3,
    },
    Product.IPR: {
        "position_limit":  80,
        "trend_per_tick":  0.001,   # confirmed exact value from data
        "entry_premium":   10,      # ask is typically fv+7; allow up to fv+10
        # NO adverse_volume filter — data shows large asks are trend, not informed
        # NO early exit — every tick at max position = +8 XIRECs, never exit early
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

    # ── BID FOR MARKET ACCESS FEE ────────────────────────────
    # Extra 25% volume = significantly more ACO market-making flow.
    # Data shows ACO avg bid/ask vol = 14 units/side per tick.
    # 25% extra = ~3.5 more units. Over 30,000 ticks at edge=3 each side:
    # very roughly 3.5 * 6 * 30000 * fill_rate ≈ well worth 2500.
    # Bid 2500 to safely be in top 50% without overpaying.
    def bid(self):
        return 2500

    # ── IPR FAIR VALUE ───────────────────────────────────────
    # Exact: base + 0.001 * timestamp per day
    # Day bases confirmed from data: -1→11000, 0→12000, 1→13000
    def _ipr_fv(self, state: TradingState, obj: dict) -> float:
        ts = state.timestamp
        rate = self.params[Product.IPR]["trend_per_tick"]

        if "ipr_base" in obj:
            return obj["ipr_base"] + rate * ts

        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders) + min(od.sell_orders)) / 2
            # Snap base to nearest 1000 (day boundaries are clean multiples)
            base = round((mid - rate * ts) / 1000) * 1000
            obj["ipr_base"] = base
            return base + rate * ts

        return 11_000 + rate * ts  # fallback: assume day -1 start

    # ── ACO TRADING ──────────────────────────────────────────
    def _trade_aco(self, state: TradingState) -> List[Order]:
        ap    = self.params[Product.ACO]
        od    = state.order_depths[Product.ACO]
        pos   = state.position.get(Product.ACO, 0)
        limit = self.LIMIT[Product.ACO]
        fv    = ap["fair_value"]
        soft  = ap["soft_limit"]
        orders: List[Order] = []
        bv = sv = 0

        # Safety: skip if book is empty (zero-price ticks from data)
        if not od.sell_orders and not od.buy_orders:
            return orders

        # TAKE: lift underpriced asks / hit overpriced bids
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

        # MAKE: post quotes inside the typical ±8 market spread
        edge  = ap["make_edge"]
        bid_p = fv - edge
        ask_p = fv + edge

        # Inventory skew: if long, push quotes down to sell back faster
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

    # ── IPR TRADING: pure buy-and-hold ───────────────────────
    # Core insight from data: trend is +0.001/tick with residual std=2.37.
    # Holding 80 units earns +8 XIRECs/tick every single tick.
    # The ONLY job is to reach position=80 as fast as possible
    # and HOLD until the very last tick.
    # Never sell early. Never filter. Just buy.
    def _trade_ipr(self, state: TradingState, obj: dict) -> List[Order]:
        pp    = self.params[Product.IPR]
        od    = state.order_depths[Product.IPR]
        pos   = state.position.get(Product.IPR, 0)
        limit = self.LIMIT[Product.IPR]
        fv    = self._ipr_fv(state, obj)
        orders: List[Order] = []

        # Skip zero-price/empty ticks (47 observed in data, 0.16%)
        if not od.sell_orders and not od.buy_orders:
            return orders
        if od.sell_orders and min(od.sell_orders) <= 0:
            return orders

        # BUY PHASE: fill position as fast as possible
        # entry_premium=10 allows buying up to fv+10
        # Data: ask is typically fv+7, so we capture nearly all available volume
        # NO adverse_volume filter — data proves large asks are not informed flow
        if pos < limit:
            ceiling = fv + pp["entry_premium"]
            # Sweep all levels of the ask side
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

        # HOLD PHASE: never sell before the final tick
        # Data confirms: 80 units * 0.001/tick = +8 XIRECs every tick
        # Previous 0.97 exit fractional was leaving ~(0.03 * 10000 * 8) = 2400 XIRECs/day

        # END-OF-ROUND ONLY: liquidate at very last tick
        # timestamp 999900 = last tick. Sell everything at best available bid.
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

    # ─────────────────────────────────────────────────────────
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