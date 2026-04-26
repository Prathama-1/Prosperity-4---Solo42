#The graph went to 20k then came back to 1k
"""
IMC Prosperity Round 3 — trader.py  (FIXED v2)
================================================
BUGS FIXED vs v1:
  1. hydro_fv initialized from FIRST MARKET MID, not hardcoded 9991
     (old 9991 was Day 1 mean; Day 2 price was 10011 → caused -20k loss)
  2. SIGMA corrected to 0.26 (market IV backed out from actual option prices)
     (old 0.2334 made all options look ~15% overpriced → code was selling
      options that were actually fairly priced → -7k loss)
  3. vev_fv initialized from first market mid, not hardcoded 5250
     (old 5250 was ~17 below actual price → options mispriced for first 100 ticks)
  4. EMA alpha for hydrogel bumped from 0.02 → 0.10
     (old 0.02 took 350 ticks to converge; 0.10 converges in ~30 ticks)
  5. Options: NEVER sell unless our BS-derived fair value is CLEARLY below market.
     Added a hard floor: don't sell if BS fair > market price (means it's cheap)
"""

from datamodel import (
    OrderDepth, TradingState, Order,
    ConversionObservation, Listing, Observation,
    ProsperityEncoder, Symbol, Trade, UserId
)
from typing import Dict, List, Any
import json
import math


# ─────────────────────────────────────────────────────────────────────────────
#  BLACK-SCHOLES HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(S: float, K: float, T: float, sigma: float) -> float:
    """European call, r=0."""
    if T <= 0:
        return max(0.0, S - K)
    if sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VOUCHERS: List[str] = [
    "VEV_4000", "VEV_4500", "VEV_5000", "VEV_5100",
    "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500",
    "VEV_6000", "VEV_6500",
]
STRIKES: Dict[str, int] = {
    "VEV_4000": 4000, "VEV_4500": 4500,
    "VEV_5000": 5000, "VEV_5100": 5100,
    "VEV_5200": 5200, "VEV_5300": 5300,
    "VEV_5400": 5400, "VEV_5500": 5500,
    "VEV_6000": 6000, "VEV_6500": 6500,
}

# FIX #2: Corrected sigma from 0.2334 → 0.26
# Backed out from actual market option prices at t=0 (Day 2):
#   VEV_5300 market mid = 53.0  → implied IV = 27.6%
#   VEV_5400 market mid = 17.0  → implied IV = 24.9%
#   VEV_5500 market mid = 6.5   → implied IV = 26.8%
#   Average across strikes ≈ 26%
# Old code used 23.34% from Day 1 data. Day 2 market repriced to ~26%.
# Using 23.34% made all options look 10-20% overpriced → we sold them.
# At 26% they are fairly priced → we should NOT be selling.
SIGMA: float = 0.26

POS_LIMIT_HYDRO:  int = 200
POS_LIMIT_VEV:    int = 200
POS_LIMIT_OPTION: int = 300

# FIX #4: Increased alpha from 0.02 → 0.10
# With alpha=0.02 and initial fv=9991 vs actual price=10011:
#   Convergence time ≈ 1/alpha ticks = 50 ticks = 5000 timestamps
#   For those 5000 timestamps the code was panic-selling HP as "overpriced"
# With alpha=0.10: converges in ~10 ticks = 1000 timestamps
HYDROGEL_ALPHA: float = 0.10
VELVFRUIT_ALPHA: float = 0.10  # also bumped from 0.05 for faster convergence

OPTION_EDGE_THRESHOLD: float = 2.0   # slightly higher to reduce noise trades

HYDRO_QUOTE_OFFSET: int = 4
VEV_QUOTE_OFFSET:   int = 2

HYDRO_ORDER_SIZE:   int = 10
VEV_ORDER_SIZE:     int = 8
OPTION_ORDER_SIZE:  int = 10

TIMESTAMPS_PER_DAY: int = 10000
TTE_START_DAYS:     int = 4     # We are in Day 2 now; 4 days remain, not 5


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def best_bid(od: OrderDepth):
    if od.buy_orders:
        return max(od.buy_orders.keys())
    return None

def best_ask(od: OrderDepth):
    if od.sell_orders:
        return min(od.sell_orders.keys())
    return None

def mid_price(od: OrderDepth):
    b = best_bid(od)
    a = best_ask(od)
    if b is not None and a is not None:
        return (b + a) / 2.0
    return b if b is not None else a


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER
# ─────────────────────────────────────────────────────────────────────────────

class Trader:

    def __init__(self):
        # FIX #1 & #3: Do NOT hardcode stale prices.
        # Use None to signal "not yet initialized from market".
        # On the very first tick we set fv = actual market mid.
        self.hydro_fv:       float | None = None   # was hardcoded 9991 ← WRONG
        self.vev_fv:         float | None = None   # was hardcoded 5250 ← WRONG
        self.timestamp:      int          = 0
        self.initialized:    bool         = False

    def _load_state(self, state: TradingState):
        if state.traderData and state.traderData != "":
            try:
                data = json.loads(state.traderData)
                self.hydro_fv   = data.get("hydro_fv",   None)
                self.vev_fv     = data.get("vev_fv",     None)
                self.timestamp  = data.get("timestamp",  0)
                self.initialized = data.get("initialized", False)
            except Exception:
                pass

    def _save_state(self) -> str:
        return json.dumps({
            "hydro_fv":    self.hydro_fv,
            "vev_fv":      self.vev_fv,
            "timestamp":   self.timestamp,
            "initialized": self.initialized,
        })

    def _init_fair_values(self, od: Dict):
        """
        On the very first tick, bootstrap fair values from actual market midpoints.
        This avoids the 'stale hardcoded price' bug that caused us to think
        HYDROGEL was overpriced by ~20 units for the first 350 ticks.
        """
        if self.initialized:
            return

        if "HYDROGEL_PACK" in od:
            m = mid_price(od["HYDROGEL_PACK"])
            if m is not None:
                self.hydro_fv = m

        if "VELVETFRUIT_EXTRACT" in od:
            m = mid_price(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = m

        if self.hydro_fv is not None and self.vev_fv is not None:
            self.initialized = True

    def run(self, state: TradingState):
        self._load_state(state)
        self.timestamp = state.timestamp
        od = state.order_depths

        # FIX #1/#3: Initialize from market on first tick
        self._init_fair_values(od)

        # If still not initialized somehow, skip this tick safely
        if not self.initialized:
            return {}, 0, self._save_state()

        orders:      Dict[str, List[Order]] = {}
        conversions: int = 0

        # ── Update EMA fair values ──
        if "HYDROGEL_PACK" in od:
            m = mid_price(od["HYDROGEL_PACK"])
            if m is not None:
                self.hydro_fv = HYDROGEL_ALPHA * m + (1 - HYDROGEL_ALPHA) * self.hydro_fv

        if "VELVETFRUIT_EXTRACT" in od:
            m = mid_price(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = VELVFRUIT_ALPHA * m + (1 - VELVFRUIT_ALPHA) * self.vev_fv

        # ── Compute TTE ──
        # FIX: TTE_START_DAYS=4 (not 5) because we're in Day 2
        elapsed_days = self.timestamp / TIMESTAMPS_PER_DAY
        tte_days     = max(TTE_START_DAYS - elapsed_days, 0.0)
        tte_years    = tte_days / 365.0

        # ── 1. HYDROGEL_PACK market-making ──
        if "HYDROGEL_PACK" in od:
            pos = state.position.get("HYDROGEL_PACK", 0)
            orders["HYDROGEL_PACK"] = self._market_make_hydrogel(od["HYDROGEL_PACK"], pos)

        # ── 2. VELVETFRUIT_EXTRACT market-making ──
        if "VELVETFRUIT_EXTRACT" in od:
            pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
            orders["VELVETFRUIT_EXTRACT"] = self._market_make_velvetfruit(od["VELVETFRUIT_EXTRACT"], pos)

        # ── 3. VEV Options ──
        for voucher in VOUCHERS:
            if voucher not in od:
                continue
            K   = STRIKES[voucher]
            pos = state.position.get(voucher, 0)
            o   = self._trade_option(voucher, od[voucher], K, self.vev_fv, tte_years, pos)
            if o:
                orders[voucher] = o

        return orders, conversions, self._save_state()


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 1: HYDROGEL_PACK — Market Making
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_hydrogel(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        fv = self.hydro_fv

        # Inventory skew: long → push quotes down (eager to sell); short → push up
        skew = int(pos / 20)

        bid_price_target = round(fv) - HYDRO_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + HYDRO_QUOTE_OFFSET - skew

        buy_capacity  = POS_LIMIT_HYDRO - pos
        sell_capacity = POS_LIMIT_HYDRO + pos

        # Take any ask that is genuinely cheap vs fair value
        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - HYDRO_QUOTE_OFFSET and buy_capacity > 0:
                qty = min(-ask_vol, buy_capacity, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", ask, qty))
                    buy_capacity -= qty

        # Hit any bid that is genuinely expensive vs fair value
        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + HYDRO_QUOTE_OFFSET and sell_capacity > 0:
                qty = min(bid_vol, sell_capacity, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", bid, -qty))
                    sell_capacity -= qty

        # Post passive quotes
        if buy_capacity > 0:
            orders.append(Order("HYDROGEL_PACK", bid_price_target,
                                min(HYDRO_ORDER_SIZE, buy_capacity)))
        if sell_capacity > 0:
            orders.append(Order("HYDROGEL_PACK", ask_price_target,
                                -min(HYDRO_ORDER_SIZE, sell_capacity)))

        return orders


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 2: VELVETFRUIT_EXTRACT — Market Making
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_velvetfruit(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        fv = self.vev_fv

        skew = int(pos / 20)

        bid_price_target = round(fv) - VEV_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + VEV_QUOTE_OFFSET - skew

        buy_capacity  = POS_LIMIT_VEV - pos
        sell_capacity = POS_LIMIT_VEV + pos

        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - VEV_QUOTE_OFFSET and buy_capacity > 0:
                qty = min(-ask_vol, buy_capacity, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", ask, qty))
                    buy_capacity -= qty

        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + VEV_QUOTE_OFFSET and sell_capacity > 0:
                qty = min(bid_vol, sell_capacity, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", bid, -qty))
                    sell_capacity -= qty

        if buy_capacity > 0:
            orders.append(Order("VELVETFRUIT_EXTRACT", bid_price_target,
                                min(VEV_ORDER_SIZE, buy_capacity)))
        if sell_capacity > 0:
            orders.append(Order("VELVETFRUIT_EXTRACT", ask_price_target,
                                -min(VEV_ORDER_SIZE, sell_capacity)))

        return orders


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 3: VEV OPTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_option(
        self, product: str, od: OrderDepth,
        K: int, S: float, T: float, pos: int
    ) -> List[Order]:

        orders: List[Order] = []
        buy_capacity  = POS_LIMIT_OPTION - pos
        sell_capacity = POS_LIMIT_OPTION + pos

        # ── (A) DEEP-ITM: VEV_4000 and VEV_4500 ──
        # Fair value = S − K (pure intrinsic, no time value)
        if K <= 4500:
            fair = S - K
            if fair <= 0:
                return []

            for ask, ask_vol in sorted(od.sell_orders.items()):
                if ask < fair - OPTION_EDGE_THRESHOLD and buy_capacity > 0:
                    qty = min(-ask_vol, buy_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, ask, qty))
                        buy_capacity -= qty

            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                if bid > fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
                    qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_capacity -= qty

            return orders

        # ── (B) DEEP-OTM: VEV_6000 and VEV_6500 ──
        # Worthless in 4 days. Only sell if someone bids ≥ 1.
        if K >= 6000:
            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                if bid >= 1.0 and sell_capacity > 0:
                    qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_capacity -= qty
            if sell_capacity > 0:
                orders.append(Order(product, 1, -min(5, sell_capacity)))
            return orders

        # ── (C) NEAR-ATM: VEV_5000 to VEV_5500 ──
        # FIX #2: Use corrected SIGMA=0.26 (was 0.2334)
        # Old sigma made every option look 10-30% overpriced → sold everything.
        # Corrected sigma matches what the market actually uses.
        fair = bs_call_price(S, K, T, SIGMA)
        if fair <= 0:
            return []

        b = best_bid(od)
        a = best_ask(od)

        # Buy if ask is below our fair value by more than edge threshold
        if a is not None and a < fair - OPTION_EDGE_THRESHOLD and buy_capacity > 0:
            ask_vol = od.sell_orders.get(a, 0)
            qty = min(-ask_vol, buy_capacity, OPTION_ORDER_SIZE)
            if qty > 0:
                orders.append(Order(product, a, qty))
                buy_capacity -= qty

        # FIX #5: Safety guard — only sell if market bid is CLEARLY above our fair value.
        # Old code was selling whenever bid > fair + 1.5, but with wrong sigma=0.2334,
        # "fair" was systematically too low, so the condition triggered constantly.
        # Now with correct sigma, this guard is extra safety.
        if b is not None and b > fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
            # Additional sanity check: never sell if intrinsic value alone justifies market price
            intrinsic = max(0.0, S - K)
            if b > intrinsic + OPTION_EDGE_THRESHOLD:   # only sell if genuinely expensive
                bid_vol = od.buy_orders.get(b, 0)
                qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order(product, b, -qty))
                    sell_capacity -= qty

        # Passive quotes around fair value
        passive_bid = math.floor(fair - OPTION_EDGE_THRESHOLD)
        passive_ask = math.ceil(fair + OPTION_EDGE_THRESHOLD)

        if passive_bid < passive_ask:
            if buy_capacity > 0:
                orders.append(Order(product, passive_bid,
                                    min(OPTION_ORDER_SIZE, buy_capacity)))
            if sell_capacity > 0:
                orders.append(Order(product, passive_ask,
                                    -min(OPTION_ORDER_SIZE, sell_capacity)))

        return orders