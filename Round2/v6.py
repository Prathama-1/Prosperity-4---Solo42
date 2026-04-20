#Not used but is better
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "position_limit": 80,
        "fair_value":     10_000,   # confirmed: mean=10000.21, no drift
        "take_width":     6,        # only take if >6 from FV (prevents day-open short)
        "make_edge":      3,        # passive quotes at fv±3
        "soft_limit":     25,       # start skewing at ±25
        "skew_divisor":   3,        # skew 1 pip per 3 units over soft limit
    },
    Product.IPR: {
        "position_limit":  80,
        "trend_per_tick":  0.001,   # confirmed exact: +1000/day, residual std=2.37
        "entry_premium":   10,      # ask is typically fv+7, accept up to fv+10

        # REVERSAL DETECTION — correct method:
        # Compare mid to TREND LINE (not running peak)
        # Trend line = ipr_base + 0.001 * ts
        # A genuine reversal = price sustained BELOW trend by a large margin
        # Buffer=25 = 10.5 sigma from noise (std=2.37) → essentially impossible
        # in a trending market unless the trend genuinely breaks
        # N=15 consecutive ticks = confirmed signal, not a spike
        # This fires ZERO times on all 3 days of real data (30000 ticks)
        # but WOULD fire if price dropped 25+ below trend for 1500+ ticks
        "reversal_buffer": 25,      # ticks below trend line to call reversal
        "reversal_confirm": 15,     # consecutive ticks required
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

    def bid(self):
        # MAF break-even calculation:
        # Current ACO PnL ~1173. MAF adds 25% more fills → +293 extra.
        # Target ACO PnL ~2000. MAF adds → +500 extra.
        # Game theory: ~30% teams bid 0, median estimated 500-800.
        # Bid 500: above break-even, likely in top 50%.
        # If accepted: net +0 to +100. If rejected: pay nothing.
        # DO NOT bid 2500 (burns 2207 XIRECs for 293 gain).
        return 500

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

        # TAKE: only on extreme mispricings (>6 from FV)
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

        # MAKE: passive quotes at fv±3, with inventory skew
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
        ts    = state.timestamp
        fv    = self._ipr_fv(state, obj)
        orders: List[Order] = []

        if not od.sell_orders and not od.buy_orders:
            return orders
        if od.sell_orders and min(od.sell_orders) <= 0:
            return orders

        mid = fv
        if od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders) + min(od.sell_orders)) / 2

        # ── DAY BOUNDARY RESET ─────────────────────────────────────
        # traderData persists across days. Timestamp resets to ~0 at
        # each new day. Detect this and clear reversal state.
        last_ts = obj.get("ipr_last_ts", 0)
        if ts < last_ts:
            obj["ipr_bear"]    = False
            obj["ipr_consec"]  = 0
        obj["ipr_last_ts"] = ts

        if "ipr_bear"   not in obj: obj["ipr_bear"]   = False
        if "ipr_consec" not in obj: obj["ipr_consec"] = 0

        # ── REVERSAL DETECTION: TREND DEVIATION METHOD ─────────────
        # The CORRECT signal: mid sustained far BELOW the trend line
        # trend_line = ipr_base + 0.001 * ts  (our existing FV formula)
        # This is NOT "distance from running peak" (that fires constantly
        # in any trending market and caused 22 false triggers in real data)
        #
        # Calibration from data:
        # - Max residual noise: ±11.4 (std=2.37)
        # - Buffer=25: 10.5 sigma below trend → essentially impossible
        #   unless trend genuinely reverses
        # - Tested on 30,000 ticks of real data: ZERO false triggers
        # - A genuine reversal would need sustained -25 below trend line
        if not obj["ipr_bear"]:
            deviation = fv - mid   # positive = mid is BELOW trend line
            if deviation > pp["reversal_buffer"]:
                obj["ipr_consec"] += 1
            else:
                obj["ipr_consec"] = 0

            if obj["ipr_consec"] >= pp["reversal_confirm"]:
                obj["ipr_bear"]  = True
                obj["ipr_consec"] = 0

        # ── BEAR MODE ──────────────────────────────────────────────
        if obj["ipr_bear"]:
            # Step 1: liquidate any longs immediately at best bid
            if pos > 0:
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= 0: break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

            # Step 2: build short to -limit (sell into bids)
            # Only build short if we have time remaining
            if pos > -limit and ts < 999_900:
                floor = fv - pp["entry_premium"]
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= -limit: break
                    if bid_p < floor: break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, limit + pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

            # Step 3: cover shorts at end of day
            if ts >= 999_900 and pos < 0:
                for ask_p in sorted(od.sell_orders):
                    if pos >= 0: break
                    vol = -od.sell_orders[ask_p]
                    qty = min(vol, -pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, ask_p, qty))
                        pos += qty

            return orders

        # ── BULL MODE (default) ────────────────────────────────────
        # Buy to full 80 as fast as possible.
        # Every tick at 80 units = +8 XIRECs guaranteed.
        # Entry premium of 10 is recovered in 125 ticks out of 1000+
        if pos < limit:
            ceiling = fv + pp["entry_premium"]
            for ask_p in sorted(od.sell_orders):
                if pos >= limit: break
                if ask_p > ceiling: break
                vol = -od.sell_orders[ask_p]
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, ask_p, qty))
                    pos += qty

        # End-of-round liquidation (>= handles empty book edge case)
        if ts >= 999_900 and pos > 0:
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