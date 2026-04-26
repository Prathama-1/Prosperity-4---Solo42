#-30k
"""
IMC Prosperity Round 3 — trader.py
====================================
Products:
  - HYDROGEL_PACK          (delta-1, position limit ±200)
  - VELVETFRUIT_EXTRACT    (delta-1, position limit ±200)  [underlying for options]
  - VEV_4000 … VEV_6500   (call options,  position limit ±300 each)

Strategy overview:
  1. HYDROGEL_PACK       → Pure market-making around a rolling fair value
  2. VELVETFRUIT_EXTRACT → Pure market-making around a rolling fair value
  3. VEV options         → Black-Scholes fair value pricing; trade when market
                           price deviates from fair value by more than our edge
                           threshold.  Deep-ITM and deep-OTM get special handling.

All strategies are explained inline below.
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
#  BLACK-SCHOLES HELPERS  (no external libraries — pure math)
# ─────────────────────────────────────────────────────────────────────────────

def _erf(x: float) -> float:
    """
    Approximation of the error function used inside norm_cdf.
    Accurate to ~1.5e-7 (Abramowitz & Stegun formula 7.1.26).
    We need this because scipy is not available in the IMC sandbox.
    """
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t)
                  + 1.421413741) * t - 0.284496736) * t
                + 0.254829592) * t * math.exp(-x * x)
    return sign * y


def norm_cdf(x: float) -> float:
    """Standard normal CDF N(x)."""
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal PDF n(x)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call_price(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes price for a European CALL option (r = 0).

    WHY r=0?  The IMC exchange uses no risk-free rate — there is no interest
    earned on cash, so discounting is irrelevant.

    S     = current spot price of VELVETFRUIT_EXTRACT
    K     = strike price of the voucher (e.g. 5000 for VEV_5000)
    T     = time to expiry in YEARS  (e.g. 5 days → 5/365)
    sigma = annualised volatility (we use 0.2334 from historical data)

    Returns the theoretical fair value of the call option.
    """
    if T <= 0:
        # Expired: worth only intrinsic value (or zero)
        return max(0.0, S - K)
    if sigma <= 0:
        return max(0.0, S - K)

    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes DELTA = dPrice/dS for a call option.

    Delta tells us: if spot moves by 1, how much does the option price move?
    - Deep ITM  → delta ≈ 1.0  (moves dollar-for-dollar with spot)
    - ATM       → delta ≈ 0.5
    - Deep OTM  → delta ≈ 0.0

    We use delta to calculate how much VELVETFRUIT_EXTRACT we should hold
    to hedge the options exposure in our portfolio.
    """
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS  (calibrated from historical data)
# ─────────────────────────────────────────────────────────────────────────────

# All voucher products, their strikes, and position limits
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

# Implied volatility calibrated from all near-ATM options (5000–5500) across
# all 3 historical days.  Every strike gave 22–24%; we use the average 23.34%.
#
# WHY a fixed IV?  The IV was extremely stable (std ≈ 0.007) across time and
# strikes. This means the market consistently prices options using this vol.
# Using a fixed IV rather than re-estimating every tick avoids noise.
SIGMA: float = 0.2334

# Position limits
POS_LIMIT_HYDRO:  int = 200
POS_LIMIT_VEV:    int = 200
POS_LIMIT_OPTION: int = 300

# HYDROGEL fair value — the price oscillates around ~9991 with mean-reversion.
# We use a rolling EMA (exponential moving average) instead of a hard-coded
# constant, because the mean drifts slightly between days (9991, 9992, 9989).
HYDROGEL_ALPHA: float = 0.02   # EMA decay factor (small = slow, sticky estimate)

# VELVETFRUIT fair value — same approach.
VELVFRUIT_ALPHA: float = 0.05  # slightly faster because it is the options underlying

# Options trading: how far must market price deviate from BS fair value
# before we act?  This is our "edge threshold" — it must exceed the spread
# we give up crossing.
#
# From data: VEV option spreads are 1–7 depending on the strike.
# We require at least 1.5 units of edge before trading so we don't get
# picked off by noise.
OPTION_EDGE_THRESHOLD: float = 1.5   # minimum profit per unit before trading

# Market-making spread for HYDROGEL (observed spread ~16; we quote inside at ±8)
HYDRO_QUOTE_OFFSET: int = 4   # we quote mid ± 4 (tighter than market = gets filled)

# Market-making spread for VELVETFRUIT (observed spread ~5; we quote inside at ±2)
VEV_QUOTE_OFFSET:   int = 2

# Max size per order to avoid moving the market against ourselves
HYDRO_ORDER_SIZE:   int = 10
VEV_ORDER_SIZE:     int = 8
OPTION_ORDER_SIZE:  int = 10   # conservative size per voucher order

# Round 3 live TTE: at the START of the simulation it is 5 days.
# Each timestamp is 100 units; there are 10000 timestamps per day.
# So 1 day = 10000 timestamps → total = 5 days = 50000 timestamps.
TIMESTAMPS_PER_DAY: int = 10000
TTE_START_DAYS:     int = 5     # TTE at beginning of round 3


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY: extract best bid/ask from order depth
# ─────────────────────────────────────────────────────────────────────────────

def best_bid(order_depth: OrderDepth):
    """Highest price someone is willing to BUY at (we can sell here)."""
    if order_depth.buy_orders:
        return max(order_depth.buy_orders.keys())
    return None


def best_ask(order_depth: OrderDepth):
    """Lowest price someone is willing to SELL at (we can buy here)."""
    if order_depth.sell_orders:
        return min(order_depth.sell_orders.keys())
    return None


def mid_price(order_depth: OrderDepth):
    """Simple average of best bid and ask. Returns None if one side missing."""
    b = best_bid(order_depth)
    a = best_ask(order_depth)
    if b is not None and a is not None:
        return (b + a) / 2.0
    return b if b is not None else a


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class Trader:

    def __init__(self):
        # EMA-based fair value estimates (updated tick by tick)
        self.hydro_fv: float  = 9991.0   # HYDROGEL fair value estimate
        self.vev_fv:   float  = 5250.0   # VELVETFRUIT fair value estimate

        # Track how many timestamps have elapsed (for TTE calculation)
        self.timestamp: int = 0


    # ── PERSIST STATE ACROSS TICKS ────────────────────────────────────────────
    # IMC calls a fresh Trader() each tick, so we must save/load state via
    # traderData (a JSON string that persists between ticks).

    def _load_state(self, state: TradingState):
        if state.traderData and state.traderData != "":
            try:
                data = json.loads(state.traderData)
                self.hydro_fv  = data.get("hydro_fv",  9991.0)
                self.vev_fv    = data.get("vev_fv",    5250.0)
                self.timestamp = data.get("timestamp", 0)
            except Exception:
                pass

    def _save_state(self) -> str:
        return json.dumps({
            "hydro_fv":  self.hydro_fv,
            "vev_fv":    self.vev_fv,
            "timestamp": self.timestamp,
        })


    # ── MAIN ENTRY POINT ──────────────────────────────────────────────────────

    def run(self, state: TradingState):
        self._load_state(state)

        # Advance timestamp counter
        self.timestamp = state.timestamp

        orders:      Dict[str, List[Order]] = {}
        conversions: int = 0

        # ── Update fair value estimates (EMA) ──
        od = state.order_depths

        if "HYDROGEL_PACK" in od:
            m = mid_price(od["HYDROGEL_PACK"])
            if m is not None:
                # EMA update: new_fv = alpha * observed_mid + (1-alpha) * old_fv
                # WHY EMA?  HYDROGEL oscillates around its fair value. The raw
                # mid is noisy.  The EMA smooths noise while tracking slow drifts.
                self.hydro_fv = (HYDROGEL_ALPHA * m
                                 + (1 - HYDROGEL_ALPHA) * self.hydro_fv)

        if "VELVETFRUIT_EXTRACT" in od:
            m = mid_price(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = (VELVFRUIT_ALPHA * m
                               + (1 - VELVFRUIT_ALPHA) * self.vev_fv)

        # ── Compute current TTE ──
        # At timestamp=0 we have TTE_START_DAYS=5 days.
        # One full day = TIMESTAMPS_PER_DAY = 10000 timestamps.
        # So at timestamp t, elapsed_days = t / 10000.
        # TTE_years = (5 - elapsed_days) / 365
        elapsed_days = self.timestamp / TIMESTAMPS_PER_DAY
        tte_days     = max(TTE_START_DAYS - elapsed_days, 0.0)
        tte_years    = tte_days / 365.0

        # ── 1. HYDROGEL_PACK market-making ──
        if "HYDROGEL_PACK" in od:
            pos = state.position.get("HYDROGEL_PACK", 0)
            orders["HYDROGEL_PACK"] = self._market_make_hydrogel(
                od["HYDROGEL_PACK"], pos
            )

        # ── 2. VELVETFRUIT_EXTRACT market-making ──
        if "VELVETFRUIT_EXTRACT" in od:
            pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
            orders["VELVETFRUIT_EXTRACT"] = self._market_make_velvetfruit(
                od["VELVETFRUIT_EXTRACT"], pos
            )

        # ── 3. VEV Options ──
        for voucher in VOUCHERS:
            if voucher not in od:
                continue
            K   = STRIKES[voucher]
            pos = state.position.get(voucher, 0)
            o   = self._trade_option(
                voucher, od[voucher], K, self.vev_fv, tte_years, pos
            )
            if o:
                orders[voucher] = o

        traderData = self._save_state()
        return orders, conversions, traderData


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 1: HYDROGEL_PACK — Market Making
    # ─────────────────────────────────────────────────────────────────────────
    #
    # WHAT WE OBSERVED:
    #   - Mean ~9991, oscillates in range 9891–10079, std ~32
    #   - Spread in the orderbook is consistently ~16 (bid-ask gap of 16)
    #   - Autocorrelation 0.996 → very slow-moving, predictable
    #   - No directional trend
    #
    # STRATEGY:
    #   We post a BID slightly below our EMA fair value, and an ASK slightly
    #   above it.  Whenever the market moves to our quotes, we get filled and
    #   earn the spread.  We quote INSIDE the existing market spread (market
    #   has ±8 from mid → we quote ±4 from mid) so we jump to the front of
    #   the queue.
    #
    # INVENTORY MANAGEMENT:
    #   If we are long (pos > 0), the market could fall before we sell. To
    #   reduce risk we skew our quotes: push the bid lower (less eager to buy
    #   more) and the ask lower (more eager to sell). Symmetric for short.
    #   Skew amount = 1 unit per 20 units of position.
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_hydrogel(
        self, od: OrderDepth, pos: int
    ) -> List[Order]:

        orders: List[Order] = []
        fv = self.hydro_fv

        # Inventory skew: if long, lower quotes to sell faster
        skew = int(pos / 20)   # e.g. pos=100 → skew=5

        bid_price_target = round(fv) - HYDRO_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + HYDRO_QUOTE_OFFSET - skew

        # How much room do we have before hitting position limits?
        buy_capacity  = POS_LIMIT_HYDRO - pos    # can still buy this many
        sell_capacity = POS_LIMIT_HYDRO + pos    # can still sell this many

        # First: aggressively take any cheap asks (below our fair value)
        # If someone is selling below what we think it's worth → buy it
        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - HYDRO_QUOTE_OFFSET and buy_capacity > 0:
                qty = min(-ask_vol, buy_capacity, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", ask, qty))
                    buy_capacity -= qty

        # Then: aggressively sell any expensive bids (above our fair value)
        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + HYDRO_QUOTE_OFFSET and sell_capacity > 0:
                qty = min(bid_vol, sell_capacity, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", bid, -qty))
                    sell_capacity -= qty

        # Finally: post passive quotes to earn spread from future trades
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
    #
    # WHAT WE OBSERVED:
    #   - Mean ~5250, range 5198–5300, std ~15
    #   - Spread in orderbook: consistently ~5
    #   - IMPORTANT: This is also the underlying for all VEV options.
    #     Our fair value estimate (vev_fv) feeds directly into option pricing.
    #
    # STRATEGY:
    #   Same market-making logic as HYDROGEL but tuned to the tighter spread.
    #   We quote ±2 from mid (market quotes ±2.5, we beat it by 0.5).
    #   Same inventory skew applied.
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_velvetfruit(
        self, od: OrderDepth, pos: int
    ) -> List[Order]:

        orders: List[Order] = []
        fv = self.vev_fv

        skew = int(pos / 20)

        bid_price_target = round(fv) - VEV_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + VEV_QUOTE_OFFSET - skew

        buy_capacity  = POS_LIMIT_VEV - pos
        sell_capacity = POS_LIMIT_VEV + pos

        # Take cheap asks
        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - VEV_QUOTE_OFFSET and buy_capacity > 0:
                qty = min(-ask_vol, buy_capacity, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", ask, qty))
                    buy_capacity -= qty

        # Hit expensive bids
        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + VEV_QUOTE_OFFSET and sell_capacity > 0:
                qty = min(bid_vol, sell_capacity, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", bid, -qty))
                    sell_capacity -= qty

        # Post passive quotes
        if buy_capacity > 0:
            orders.append(Order("VELVETFRUIT_EXTRACT", bid_price_target,
                                min(VEV_ORDER_SIZE, buy_capacity)))
        if sell_capacity > 0:
            orders.append(Order("VELVETFRUIT_EXTRACT", ask_price_target,
                                -min(VEV_ORDER_SIZE, sell_capacity)))

        return orders


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 3: VEV OPTIONS — Black-Scholes Fair Value Trading
    # ─────────────────────────────────────────────────────────────────────────
    #
    # WHAT WE OBSERVED:
    #   - All near-ATM options (VEV_5000–5500) have consistent IV ≈ 23.34%
    #   - Time decay is clearly visible: options lose ~2 units/day (ATM range)
    #   - Deep-ITM (VEV_4000, VEV_4500): price = S − K to within 0.02 units.
    #     Time value is essentially 0. No option pricing needed.
    #   - Deep-OTM (VEV_6000, VEV_6500): price stuck at 0.5 (exchange minimum).
    #     These will expire worthless. Just sell them if you can get > 0.5.
    #
    # STRATEGY for each category:
    #
    # (A) DEEP-ITM (K ≤ 4500):
    #   Fair value = S − K  (almost no time value).
    #   Treat like a synthetic VELVETFRUIT_EXTRACT position.
    #   Buy if market price < (S − K) − edge_threshold
    #   Sell if market price > (S − K) + edge_threshold
    #
    # (B) NEAR-ATM (5000 ≤ K ≤ 5500):
    #   Fair value = bs_call_price(S, K, TTE_years, sigma=0.2334)
    #   Buy if market_ask < fair_value − edge_threshold  [cheap → buy]
    #   Sell if market_bid > fair_value + edge_threshold [expensive → sell]
    #   Also post passive quotes around fair value.
    #
    # (C) DEEP-OTM (K ≥ 6000):
    #   Fair value ≈ 0.  Only action: SELL if someone bids > 0.5.
    #   Never buy these — they expire worthless.
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_option(
        self,
        product: str,
        od: OrderDepth,
        K: int,
        S: float,      # spot price of VELVETFRUIT_EXTRACT
        T: float,      # time to expiry in years
        pos: int,
    ) -> List[Order]:

        orders: List[Order] = []
        buy_capacity  = POS_LIMIT_OPTION - pos
        sell_capacity = POS_LIMIT_OPTION + pos

        # ── (A) DEEP-ITM: VEV_4000 and VEV_4500 ──────────────────────────────
        # Fair value = intrinsic = S − K.
        # Time value is measured to be ~0.01 (negligible).
        # These options move dollar-for-dollar with VELVETFRUIT_EXTRACT.
        # We treat them like a very cheap way to hold VELVETFRUIT_EXTRACT exposure.
        if K <= 4500:
            fair = S - K
            if fair <= 0:
                return []   # shouldn't happen but be safe

            # Buy if the market is selling below intrinsic
            for ask, ask_vol in sorted(od.sell_orders.items()):
                if ask < fair - OPTION_EDGE_THRESHOLD and buy_capacity > 0:
                    qty = min(-ask_vol, buy_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, ask, qty))
                        buy_capacity -= qty

            # Sell if market is buying above intrinsic
            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                if bid > fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
                    qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_capacity -= qty

            return orders

        # ── (B) DEEP-OTM: VEV_6000 and VEV_6500 ─────────────────────────────
        # These are essentially worthless with 5 days left and spot at 5250.
        # VEV_6000 needs spot to reach 6000 (+14%) in 5 days — extremely unlikely.
        # Market prices them at 0.5 (the exchange minimum tick).
        # We NEVER buy these.  If we can sell at 1.0 or above, we do it.
        if K >= 6000:
            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                if bid >= 1.0 and sell_capacity > 0:
                    qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_capacity -= qty
            # Post a small passive sell at 1.0 to pick up any buyers
            if sell_capacity > 0:
                orders.append(Order(product, 1, -min(5, sell_capacity)))
            return orders

        # ── (C) NEAR-ATM: VEV_5000 to VEV_5500 ──────────────────────────────
        # Compute Black-Scholes fair value.
        #
        # WHY BLACK-SCHOLES?
        #   BS is the standard formula that tells you what a call option is worth
        #   given: S (spot), K (strike), T (time to expiry), sigma (volatility).
        #   We calibrated sigma = 23.34% from historical data.  The market
        #   consistently prices options using this vol, so BS gives us the
        #   "correct" reference price.  Any deviation from BS fair value is an
        #   arbitrage opportunity.
        #
        # EDGE THRESHOLD:
        #   We only trade if the mispricing exceeds OPTION_EDGE_THRESHOLD (1.5).
        #   This ensures we're not just trading noise or paying hidden costs.

        fair = bs_call_price(S, K, T, SIGMA)
        if fair <= 0:
            return []

        b = best_bid(od)
        a = best_ask(od)

        # If someone is SELLING below our fair value → BUY (it's cheap)
        if a is not None and a < fair - OPTION_EDGE_THRESHOLD and buy_capacity > 0:
            ask_vol = od.sell_orders.get(a, 0)
            qty = min(-ask_vol, buy_capacity, OPTION_ORDER_SIZE)
            if qty > 0:
                orders.append(Order(product, a, qty))
                buy_capacity -= qty

        # If someone is BUYING above our fair value → SELL (it's expensive)
        if b is not None and b > fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
            bid_vol = od.buy_orders.get(b, 0)
            qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
            if qty > 0:
                orders.append(Order(product, b, -qty))
                sell_capacity -= qty

        # Post PASSIVE QUOTES around fair value
        # WHY PASSIVE QUOTES?
        #   Besides taking mispricings, we also want to earn spread by being
        #   the resting order that other traders hit.  We quote at:
        #     bid = fair - edge_threshold  (we earn edge when someone sells to us)
        #     ask = fair + edge_threshold  (we earn edge when someone buys from us)
        #   This is conservative — we only fill if there's clear edge.

        passive_bid = math.floor(fair - OPTION_EDGE_THRESHOLD)
        passive_ask = math.ceil(fair + OPTION_EDGE_THRESHOLD)

        # Don't post if our passive quotes would overlap (market too tight)
        if passive_bid < passive_ask:
            if buy_capacity > 0:
                orders.append(Order(product, passive_bid,
                                    min(OPTION_ORDER_SIZE, buy_capacity)))
            if sell_capacity > 0:
                orders.append(Order(product, passive_ask,
                                    -min(OPTION_ORDER_SIZE, sell_capacity)))

        return orders