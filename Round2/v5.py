from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY SUMMARY
#  ─────────────────────────────────────────────────────────────────
#  ACO — Symmetric Market Maker
#  • take_width=6: only aggress if order is >6 ticks mispriced
#  • Passive quotes at FV±3, inventory skew beyond ±25
#
#  IPR — Buy-and-Hold Trend Rider + Reversal Stop-Loss
#  • Accumulate to +80 fast (entry_premium=10), hold, sell at end
#  • Reversal: 10-tick drop held for 3 consecutive ticks → flip short
#  • Bear mode mirrors bull: short -80, cover at end of day
#
#  MAF BID = 100 (top 50% accepted, worth ~250 XIRECs to us)
#
#  BUG FIXES vs previous version:
#  1. ipr_bear and ipr_peak now reset at start of each new day
#     (traderData persists across days — stale bear mode from day 1
#      would keep us short on a bullish day 2)
#  2. End liquidation extended to ts>=999900 (not just ==999900)
#     in case the exact tick has an empty book
#  3. CONFIRM_TICKS moved into PARAMS for consistency
# ═══════════════════════════════════════════════════════════════════


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "position_limit": 80,
        "fair_value":     10_000,
        "take_width":     6,
        "make_edge":      3,
        "soft_limit":     25,
        "skew_divisor":   3,
    },
    Product.IPR: {
        "position_limit":     80,
        "trend_per_tick":     0.001,
        "entry_premium":      10,
        "reversal_threshold": 10,   # ticks below peak to start counter
        "confirm_ticks":       3,   # consecutive ticks needed to confirm
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

    def bid(self):
        return 100

    # ──────────────────────────────────────────────────────────────
    #  IPR fair value
    # ──────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────
    #  ACO — Symmetric Market Maker (unchanged)
    # ──────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────
    #  IPR — Buy-and-Hold + Reversal Stop-Loss
    # ──────────────────────────────────────────────────────────────

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

        # ── BUG FIX 1: Reset reversal state at start of each new day ──
        # traderData persists across days. Without this reset, a reversal
        # triggered on day 1 would keep us in bear mode for day 2+,
        # even if the trend resumes upward.
        # Day boundary: timestamp resets to ~100 at start of each new day.
        last_ts = obj.get("ipr_last_ts", 0)
        if ts < last_ts:
            # Timestamp went backwards → new day started
            obj["ipr_bear"]         = False
            obj["ipr_peak"]         = mid
            obj["ipr_consec_below"] = 0
        obj["ipr_last_ts"] = ts

        # Initialise state keys if missing
        if "ipr_bear"         not in obj: obj["ipr_bear"]         = False
        if "ipr_peak"         not in obj: obj["ipr_peak"]         = mid
        if "ipr_consec_below" not in obj: obj["ipr_consec_below"] = 0

        # ── REVERSAL DETECTION ───────────────────────────────────────
        if not obj["ipr_bear"]:
            if mid > obj["ipr_peak"]:
                obj["ipr_peak"]         = mid
                obj["ipr_consec_below"] = 0

            drop = obj["ipr_peak"] - mid
            if drop > pp["reversal_threshold"] and pos > 0:
                obj["ipr_consec_below"] += 1
            else:
                obj["ipr_consec_below"] = 0

            if obj["ipr_consec_below"] >= pp["confirm_ticks"]:
                obj["ipr_bear"]   = True
                obj["ipr_trough"] = mid

        # ── BEAR MODE ────────────────────────────────────────────────
        if obj["ipr_bear"]:
            if "ipr_trough" not in obj: obj["ipr_trough"] = mid
            if mid < obj["ipr_trough"]:  obj["ipr_trough"] = mid

            # Step 1: liquidate longs immediately
            if pos > 0:
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= 0: break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

            # Step 2: build short to -limit
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
            # BUG FIX 2: >= 999900 not == 999900
            # If the exact tick has an empty book, == would never fire
            if ts >= 999_900 and pos < 0:
                for ask_p in sorted(od.sell_orders):
                    if pos >= 0: break
                    vol = -od.sell_orders[ask_p]
                    qty = min(vol, -pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, ask_p, qty))
                        pos += qty

            return orders

        # ── BULL MODE (default) ───────────────────────────────────────
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

        # BUG FIX 2: >= 999900 not == 999900
        if ts >= 999_900 and pos > 0:
            for bid_p in sorted(od.buy_orders, reverse=True):
                if pos <= 0: break
                vol = od.buy_orders[bid_p]
                qty = min(vol, pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, bid_p, -qty))
                    pos -= qty

        return orders

    # ──────────────────────────────────────────────────────────────
    #  Main
    # ──────────────────────────────────────────────────────────────

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