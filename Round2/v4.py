#8.35k version with MAF (Market Access Fee) + more passive quote levels
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY SUMMARY
#  ─────────────────────────────────────────────────────────────────
#  ACO — Symmetric Market Maker
#  • take_width=6: only aggress if order is >6 ticks mispriced
#    (prevents shorting on the normal day-open where market sits
#     ~7 ticks above FV before settling)
#  • Passive quotes at FV±3, inventory skew beyond ±25
#  • HOW MAF HELPS ACO: 25% more passive quote opportunities =
#    more fills per tick = ~25% more ACO PnL (~+250 XIRECs)
#
#  IPR — Buy-and-Hold Trend Rider + Reversal Stop-Loss
#  • Accumulate to +80 as fast as possible (entry_premium=10)
#  • Hold until ts=999900 then liquidate — captures full daily trend
#  • HOW MAF HELPS IPR: more ask levels in book = faster accumulation
#    to 80 units (hits limit in 1-2 ticks instead of 3-4)
#    Tiny improvement since we hit limit fast anyway
#
#  REVERSAL PROTECTION (new vs 8.5k):
#  • Track rolling price peak since entry
#  • If price drops >15 ticks below peak for 5 CONSECUTIVE ticks
#    → confirmed reversal (not a spike), flip to short
#  • Bear mode: mirror image of bull (short -80, cover at end)
#  • Single-tick spikes reset the counter → never false-triggered
#
#  MAF BID = 100
#  • Need top 50% to get accepted (blind auction)
#  • MAF worth ~250 XIRECs to us
#  • Bid 100: if accepted → net +150 gain, if rejected → pay nothing
#  • 2500 (original) was overbidding by 10x
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
        "position_limit":   80,
        "trend_per_tick":   0.001,
        "entry_premium":    10,
        "reversal_threshold": 15,  # ticks below peak to start counting
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {p: PARAMS[p]["position_limit"] for p in PARAMS}

    def bid(self):
        # MAF (Market Access Fee) — 25% more quotes in order book
        # Top 50% of all bids get accepted (blind auction)
        # MAF value to us ≈ 250 XIRECs (extra ACO fills + faster IPR entry)
        # Bid 100: safe buffer above likely median, net +150 if accepted
        return 100

    # ──────────────────────────────────────────────────────────────
    #  IPR fair value — locked on first tick, never re-inferred
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
    #  ACO — Symmetric Market Maker
    #  Unchanged from 8.5k version. Extra MAF quotes give more
    #  passive fill opportunities on both sides of the book.
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

        # TAKE: only aggress if truly mispriced (>6 ticks from FV)
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

        # MAKE: passive quotes at FV±3, skew when inventory builds
        # MAF adds extra quote levels in the book → our passive orders
        # get matched against more counterparties each tick
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
    #  IPR — Buy-and-Hold with Reversal Stop-Loss
    #  MAF adds extra ask levels → we hit our 80-unit limit faster
    #  (minor benefit since we accumulate quickly regardless)
    # ──────────────────────────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, obj: dict) -> List[Order]:
        pp    = self.params[Product.IPR]
        od    = state.order_depths[Product.IPR]
        pos   = state.position.get(Product.IPR, 0)
        limit = self.LIMIT[Product.IPR]
        fv    = self._ipr_fv(state, obj)
        orders: List[Order] = []

        if not od.sell_orders and not od.buy_orders:
            return orders
        if od.sell_orders and min(od.sell_orders) <= 0:
            return orders

        mid = fv
        if od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders) + min(od.sell_orders)) / 2

        # ── REVERSAL DETECTION ──────────────────────────────────────
        # Track rolling peak. Price must stay >threshold below peak for
        # CONFIRM_TICKS consecutive ticks before we act.
        # Single-tick spikes reset the counter → no false triggers.

        if "ipr_bear"         not in obj: obj["ipr_bear"]         = False
        if "ipr_peak"         not in obj: obj["ipr_peak"]         = mid
        if "ipr_consec_below" not in obj: obj["ipr_consec_below"] = 0

        CONFIRM_TICKS = 5  # ~500ms of sustained drop = genuine reversal

        if not obj["ipr_bear"]:
            if mid > obj["ipr_peak"]:
                obj["ipr_peak"]         = mid
                obj["ipr_consec_below"] = 0      # new peak, reset counter

            drop = obj["ipr_peak"] - mid
            if drop > pp["reversal_threshold"] and pos > 0:
                obj["ipr_consec_below"] += 1     # sustained below threshold
            else:
                obj["ipr_consec_below"] = 0      # recovered — was just a spike

            if obj["ipr_consec_below"] >= CONFIRM_TICKS:
                obj["ipr_bear"]   = True
                obj["ipr_trough"] = mid

        # ── BEAR MODE ───────────────────────────────────────────────
        # Mirror image of bull: go short -80, hold, cover at end of day
        if obj["ipr_bear"]:
            if "ipr_trough" not in obj: obj["ipr_trough"] = mid
            if mid < obj["ipr_trough"]:  obj["ipr_trough"] = mid

            # Step 1: liquidate any remaining longs immediately
            if pos > 0:
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= 0: break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

            # Step 2: build short position to -limit
            if pos > -limit and state.timestamp < 999_900:
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
            if state.timestamp >= 999_900 and pos < 0:
                for ask_p in sorted(od.sell_orders):
                    if pos >= 0: break
                    vol = -od.sell_orders[ask_p]
                    qty = min(vol, -pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, ask_p, qty))
                        pos += qty

            return orders

        # ── BULL MODE (default) ──────────────────────────────────────
        # Accumulate to +80 as fast as possible, hold, sell at ts=999900
        # MAF gives extra ask levels → fill 80 units in fewer ticks
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

        if state.timestamp >= 999_900 and pos > 0:
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