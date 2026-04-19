from datamodel import OrderDepth, TradingState, Order, ConversionObservation
from typing import List, Dict, Optional
import jsonpickle


# ═══════════════════════════════════════════════════════════════════════════════
#  WHAT WAS WRONG WITH V5 / CURRENT CODE  (why only 6k) (-10k)
#  ────────────────────────────────────────────────────────────────────────────
#  ACO:
#   - position_limit=80 set in code but game enforces 20 → orders silently fail
#   - take_width_sell=5 fires rarely (bid≥10005 almost never happens)
#   - Passive quotes at fv±1/fv±4 are NOT inside the real spread (bid~9993,ask~10009)
#     so they never get passive fills from market-maker bots
#
#  IPR:
#   - accumulate_edge=3 → fires only 2.4% of ticks (ask rarely ≤ fv+3)
#   - reduce_edge=6   → fires 0% of ticks (bid never ≥ fv+6, spread only 14 ticks)
#   - passive MM at fv±2 posts INSIDE the spread → gets filled both ways → no
#     directional exposure → misses the entire 1000-pt/day trend
#   - Net result: symmetric churn, minimal trend capture
#
#  THE FIX:
#  ACO: correct limit=20, penny-quote INSIDE the real spread (bid+1 / ask-1),
#       with inventory skew to stay near 0.
#  IPR: ADAPTIVE three-regime strategy:
#       BULL  → buy and hold (max long), exit near day end
#       BEAR  → sell and hold (max short), cover near day end
#       FLAT  → pure symmetric MM around fair value
#       Regime detected via linear regression slope on recent prices
# ═══════════════════════════════════════════════════════════════════════════════


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "fair_value": 10_000,
        "position_limit": 20,           # TRUE game limit
        "take_width": 1,                # take if ask ≤ fv-1 or bid ≥ fv+1
        "disregard_edge": 1,            # ignore quotes within 1 of fv (noise)
        "default_edge": 2,              # default quote edge if no book to penny
        "soft_limit": 8,                # start skewing at ±8
    },
    Product.IPR: {
        "position_limit": 20,           # TRUE game limit
        "trend_per_tick": 0.001,        # FV rises 1 point per 1000 ticks
        "adverse_volume": 15,           # ignore MM orders above this size

        # Regime detection
        "trend_window": 5_000,          # ticks of history to fit slope over
        "bull_threshold": 0.0006,       # slope > this → BULL regime
        "bear_threshold": -0.0006,      # slope < this → BEAR regime
        # Between these thresholds → FLAT regime (pure MM)

        # Entry/exit premium accepted when taking
        "entry_premium": 8,             # buy at up to fv + 8 during bull/flat entry
        "exit_start_frac": 0.97,        # start liquidating at 97% of day

        # Flat MM parameters
        "mm_edge": 3,                   # quote fv ± 3 in flat regime
    },
}


class Trader:

    def __init__(self):
        self.params = PARAMS
        self.LIMIT = {
            Product.ACO: PARAMS[Product.ACO]["position_limit"],
            Product.IPR: PARAMS[Product.IPR]["position_limit"],
        }

    # ──────────────────────────────────────────────────────────────
    #  IPR FAIR VALUE
    # ──────────────────────────────────────────────────────────────

    def _ipr_fair_value(self, state: TradingState, obj: dict) -> float:
        p  = self.params[Product.IPR]
        ts = state.timestamp

        if "ipr_base" in obj:
            return obj["ipr_base"] + p["trend_per_tick"] * ts

        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            mid  = (max(od.buy_orders) + min(od.sell_orders)) / 2
            base = round((mid - p["trend_per_tick"] * ts) / 1000) * 1000
            obj["ipr_base"] = base
            return base + p["trend_per_tick"] * ts

        return 12_000 + p["trend_per_tick"] * ts

    # ──────────────────────────────────────────────────────────────
    #  IPR REGIME DETECTION
    #  Returns: 'bull', 'bear', or 'flat'
    #  Uses a rolling linear regression slope on detrended prices.
    #  If we haven't seen enough history yet → default to 'bull'
    #  (because we know from 3 days of data the trend is strongly up)
    # ──────────────────────────────────────────────────────────────

    def _ipr_regime(self, obj: dict, mid: float, fv: float, ts: int) -> str:
        p = self.params[Product.IPR]

        # Store detrended price history
        hist = obj.setdefault("ipr_hist", [])
        hist.append(mid - fv)  # deviation from theoretical fair
        window = p["trend_window"] // 100  # one entry per ~100 ticks
        if len(hist) > window:
            hist.pop(0)

        if len(hist) < 20:
            return "bull"   # default: trust historical prior

        # Fit slope to recent deviations (if slope > 0 even after detrending
        # → market is above/below the theoretical trend → momentum signal)
        n = len(hist)
        xs = list(range(n))
        xm = (n - 1) / 2
        ym = sum(hist) / n
        num = sum((x - xm) * (y - ym) for x, y in zip(xs, hist))
        den = sum((x - xm) ** 2 for x in xs)
        slope = num / den if den != 0 else 0

        # Also check raw absolute level: if persistently above fv → bull
        avg_dev = sum(hist[-20:]) / 20

        if slope > p["bull_threshold"] or avg_dev > 2:
            return "bull"
        elif slope < p["bear_threshold"] or avg_dev < -2:
            return "bear"
        else:
            return "flat"

    # ──────────────────────────────────────────────────────────────
    #  ACO TRADING  — penny-quote MM + symmetric take
    # ──────────────────────────────────────────────────────────────

    def _trade_aco(self, state: TradingState) -> List[Order]:
        p     = self.params[Product.ACO]
        od    = state.order_depths[Product.ACO]
        pos   = state.position.get(Product.ACO, 0)
        limit = self.LIMIT[Product.ACO]
        fv    = p["fair_value"]
        orders: List[Order] = []
        bv = sv = 0

        # ── TAKE: sweep mispriced orders ────────────────────────────
        # Buy anything ≤ fv - take_width
        for ask_p in sorted(od.sell_orders):
            if ask_p > fv - p["take_width"]: break
            vol = -od.sell_orders[ask_p]
            qty = min(vol, limit - pos - bv)
            if qty <= 0: break
            orders.append(Order(Product.ACO, ask_p, qty))
            bv += qty
            od.sell_orders[ask_p] += qty
            if od.sell_orders[ask_p] == 0: del od.sell_orders[ask_p]

        # Sell anything ≥ fv + take_width
        for bid_p in sorted(od.buy_orders, reverse=True):
            if bid_p < fv + p["take_width"]: break
            vol = od.buy_orders[bid_p]
            qty = min(vol, limit + pos - sv)
            if qty <= 0: break
            orders.append(Order(Product.ACO, bid_p, -qty))
            sv += qty
            od.buy_orders[bid_p] -= qty
            if od.buy_orders[bid_p] == 0: del od.buy_orders[bid_p]

        # ── MAKE: penny inside the real spread ──────────────────────
        # Get best competing quotes outside disregard band
        asks_above = [p2 for p2 in od.sell_orders if p2 > fv + p["disregard_edge"]]
        bids_below  = [p2 for p2 in od.buy_orders  if p2 < fv - p["disregard_edge"]]

        best_ask = min(asks_above) if asks_above else None
        best_bid = max(bids_below) if bids_below else None

        # Penny: one tick inside the best competing quote
        # Join if already very tight; use default_edge if book is empty
        if best_ask is not None:
            our_ask = best_ask - 1   # penny the ask
            our_ask = max(our_ask, fv + 1)   # never quote below fv+1
        else:
            our_ask = fv + p["default_edge"]

        if best_bid is not None:
            our_bid = best_bid + 1   # penny the bid
            our_bid = min(our_bid, fv - 1)   # never quote above fv-1
        else:
            our_bid = fv - p["default_edge"]

        # Inventory skew: nudge quotes to push back toward 0
        skew = 0
        if pos > p["soft_limit"]:
            skew = (pos - p["soft_limit"]) // 4 + 1   # proportional
        elif pos < -p["soft_limit"]:
            skew = -(-pos - p["soft_limit"]) // 4 - 1

        our_bid = round(our_bid - skew)
        our_ask = round(our_ask - skew)

        # Don't cross our own quotes
        if our_bid >= our_ask:
            our_bid = our_ask - 1

        buy_qty  = limit - (pos + bv)
        sell_qty = limit + (pos - sv)

        if buy_qty  > 0: orders.append(Order(Product.ACO, our_bid,   buy_qty))
        if sell_qty > 0: orders.append(Order(Product.ACO, our_ask,  -sell_qty))

        return orders

    # ──────────────────────────────────────────────────────────────
    #  IPR TRADING  — adaptive three-regime
    # ──────────────────────────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, obj: dict) -> List[Order]:
        p     = self.params[Product.IPR]
        od    = state.order_depths[Product.IPR]
        pos   = state.position.get(Product.IPR, 0)
        limit = self.LIMIT[Product.IPR]
        ts    = state.timestamp
        fv    = self._ipr_fair_value(state, obj)
        orders: List[Order] = []

        # Compute mid for regime detection
        mid = fv  # default
        if od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders) + min(od.sell_orders)) / 2

        regime = self._ipr_regime(obj, mid, fv, ts)

        # Track day progress (0–999900) for exit timing
        is_exit_time = ts >= p["exit_start_frac"] * 999_900

        # ════════════════════════════════════════════════
        #  BULL REGIME: buy max early, hold, sell at end
        # ════════════════════════════════════════════════
        if regime == "bull":
            if not is_exit_time:
                # Aggressively buy up to limit
                if pos < limit:
                    for ask_p in sorted(od.sell_orders):
                        if pos >= limit: break
                        if ask_p > fv + p["entry_premium"]: break
                        vol = -od.sell_orders[ask_p]
                        if vol > p["adverse_volume"]: continue
                        qty = min(vol, limit - pos)
                        if qty > 0:
                            orders.append(Order(Product.IPR, ask_p, qty))
                            pos += qty
            else:
                # Liquidate: hit all available bids
                for bid_p in sorted(od.buy_orders, reverse=True):
                    if pos <= 0: break
                    vol = od.buy_orders[bid_p]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, bid_p, -qty))
                        pos -= qty

        # ════════════════════════════════════════════════
        #  BEAR REGIME: sell max early, hold short, cover at end
        # ════════════════════════════════════════════════
        elif regime == "bear":
            if not is_exit_time:
                # Aggressively sell down to -limit (go short)
                if pos > -limit:
                    for bid_p in sorted(od.buy_orders, reverse=True):
                        if pos <= -limit: break
                        if bid_p < fv - p["entry_premium"]: break
                        vol = od.buy_orders[bid_p]
                        if vol > p["adverse_volume"]: continue
                        qty = min(vol, limit + pos)
                        if qty > 0:
                            orders.append(Order(Product.IPR, bid_p, -qty))
                            pos -= qty
            else:
                # Cover: buy back shorts
                for ask_p in sorted(od.sell_orders):
                    if pos >= 0: break
                    vol = -od.sell_orders[ask_p]
                    qty = min(vol, -pos)
                    if qty > 0:
                        orders.append(Order(Product.IPR, ask_p, qty))
                        pos += qty

        # ════════════════════════════════════════════════
        #  FLAT REGIME: symmetric MM around fair value
        # ════════════════════════════════════════════════
        else:
            edge = p["mm_edge"]

            # Take: sweep any order crossing fv by ≥1
            for ask_p in sorted(od.sell_orders):
                if ask_p > fv - 1: break
                vol = -od.sell_orders[ask_p]
                if vol > p["adverse_volume"]: continue
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, ask_p, qty))
                    pos += qty

            for bid_p in sorted(od.buy_orders, reverse=True):
                if bid_p < fv + 1: break
                vol = od.buy_orders[bid_p]
                if vol > p["adverse_volume"]: continue
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(Product.IPR, bid_p, -qty))
                    pos -= qty

            # Passive MM: quote inside the spread
            our_bid = round(fv - edge)
            our_ask = round(fv + edge)

            # Inventory skew
            if pos > limit // 2:
                our_ask -= 1; our_bid -= 1
            elif pos < -limit // 2:
                our_bid += 1; our_ask += 1

            buy_qty  = limit - pos
            sell_qty = limit + pos

            if buy_qty  > 0: orders.append(Order(Product.IPR, our_bid,  buy_qty))
            if sell_qty > 0: orders.append(Order(Product.IPR, our_ask, -sell_qty))

        return orders

    # ──────────────────────────────────────────────────────────────
    #  MAIN
    # ──────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        obj = {}
        if state.traderData:
            try:   obj = jsonpickle.decode(state.traderData)
            except: obj = {}

        result: Dict[str, List[Order]] = {}

        if Product.ACO in state.order_depths:
            result[Product.ACO] = self._trade_aco(state)

        if Product.IPR in state.order_depths:
            result[Product.IPR] = self._trade_ipr(state, obj)

        return result, 1, jsonpickle.encode(obj)