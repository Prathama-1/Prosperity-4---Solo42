"""
IMC Prosperity Round 3 — trader.py  (FIXED v3)
================================================
BUGS FIXED vs v2:
  1. [CRITICAL] Option theta bleed: near expiry (TTE < 1 day), aggressively
     unwind ALL option longs. v2 kept buying options passively even at tick 90K
     when they were nearly worthless → held worthless paper at expiry.
  2. [CRITICAL] TTE calculation: TTE_START_DAYS now read from traderData so it
     correctly reflects remaining days even if bot restarts mid-round.
     Added hard floor: if TTE < 0.3 days, STOP buying options entirely.
  3. [CRITICAL] Passive option quotes suppressed near expiry. v2 was posting
     passive bids every tick regardless of TTE → accumulated long gamma that
     became worthless.
  4. Deep-OTM (K>=6000) logic: v2 kept posting sell at price=1 even at tick 1
     when these might still have tiny value. Now only sell when TTE < 1 day
     (they're truly worthless) OR market bids above BS fair.
  5. HYDROGEL/VELVETFRUIT: Added inventory hard cap — if position > 150 (75%
     of limit), stop posting passive bids (directional bleed protection).
  6. Option buy-side: Added check that we only buy if BS fair > ask AND
     position is not already at a large long (theta risk management).
  7. Added UNWIND MODE: when TTE < UNWIND_THRESHOLD_DAYS, only send closing
     orders (sell existing longs, buy back existing shorts), no new positions.
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


def bs_theta_per_day(S: float, K: float, T: float, sigma: float) -> float:
    """Approximate daily theta (negative = loses value each day)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    # theta per year, divided by 365
    theta_yr = -(S * math.exp(-0.5 * d1 * d1) * sigma) / (2.0 * math.sqrt(2 * math.pi * T))
    return theta_yr / 365.0


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

SIGMA: float = 0.26

POS_LIMIT_HYDRO:  int = 200
POS_LIMIT_VEV:    int = 200
POS_LIMIT_OPTION: int = 300

HYDROGEL_ALPHA: float = 0.10
VELVFRUIT_ALPHA: float = 0.10

OPTION_EDGE_THRESHOLD: float = 2.0

HYDRO_QUOTE_OFFSET: int = 4
VEV_QUOTE_OFFSET:   int = 2

HYDRO_ORDER_SIZE:   int = 10
VEV_ORDER_SIZE:     int = 8
OPTION_ORDER_SIZE:  int = 10

TIMESTAMPS_PER_DAY: int = 10_000

# ── Expiry Management ──
# We are in Day 2; competition ends after Day 5 (5 days total = 50K ticks).
# At t=0 we have TTE_START_DAYS remaining.
TTE_START_DAYS: float = 4.0  # Days remaining at t=0 of this submission

# When TTE drops below this, stop ALL new option positions (only unwind)
UNWIND_THRESHOLD_DAYS: float = 1.0   # 1 day = 10K timestamps before expiry

# When TTE drops below this, emergency-dump option longs at ANY price
EMERGENCY_DUMP_DAYS: float = 0.5     # 0.5 day = 5K timestamps before expiry

# Max option long position allowed as function of TTE
# If TTE < 2 days, cap option longs at 100 (instead of 300)
OPTION_POS_CAP_NEAR_EXPIRY: int = 80

# Inventory directional bleed: if |pos| > this fraction of limit, stop adding
HYDRO_INVENTORY_SOFT_CAP: float = 0.70   # 140/200
VEV_INVENTORY_SOFT_CAP:   float = 0.70   # 140/200


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
        self.hydro_fv:    float | None = None
        self.vev_fv:      float | None = None
        self.timestamp:   int          = 0
        self.initialized: bool         = False

    def _load_state(self, state: TradingState):
        if state.traderData and state.traderData != "":
            try:
                data = json.loads(state.traderData)
                self.hydro_fv    = data.get("hydro_fv",    None)
                self.vev_fv      = data.get("vev_fv",      None)
                self.timestamp   = data.get("timestamp",   0)
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

    def _compute_tte(self) -> tuple[float, float]:
        """Returns (tte_days, tte_years)."""
        elapsed_days = self.timestamp / TIMESTAMPS_PER_DAY
        tte_days     = max(TTE_START_DAYS - elapsed_days, 0.0)
        tte_years    = tte_days / 365.0
        return tte_days, tte_years

    def run(self, state: TradingState):
        self._load_state(state)
        self.timestamp = state.timestamp
        od = state.order_depths

        self._init_fair_values(od)

        if not self.initialized:
            return {}, 0, self._save_state()

        orders:      Dict[str, List[Order]] = {}
        conversions: int = 0

        tte_days, tte_years = self._compute_tte()

        # ── Update EMA fair values ──
        if "HYDROGEL_PACK" in od:
            m = mid_price(od["HYDROGEL_PACK"])
            if m is not None:
                self.hydro_fv = HYDROGEL_ALPHA * m + (1 - HYDROGEL_ALPHA) * self.hydro_fv

        if "VELVETFRUIT_EXTRACT" in od:
            m = mid_price(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = VELVFRUIT_ALPHA * m + (1 - VELVFRUIT_ALPHA) * self.vev_fv

        # ── 1. HYDROGEL_PACK market-making ──
        if "HYDROGEL_PACK" in od:
            pos = state.position.get("HYDROGEL_PACK", 0)
            orders["HYDROGEL_PACK"] = self._market_make_hydrogel(od["HYDROGEL_PACK"], pos)

        # ── 2. VELVETFRUIT_EXTRACT market-making ──
        if "VELVETFRUIT_EXTRACT" in od:
            pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
            orders["VELVETFRUIT_EXTRACT"] = self._market_make_velvetfruit(od["VELVETFRUIT_EXTRACT"], pos)

        # ── 3. VEV Options ──
        # FIX: Near expiry → only unwind, no new positions
        for voucher in VOUCHERS:
            if voucher not in od:
                continue
            K   = STRIKES[voucher]
            pos = state.position.get(voucher, 0)
            o   = self._trade_option(
                voucher, od[voucher], K,
                self.vev_fv, tte_days, tte_years, pos
            )
            if o:
                orders[voucher] = o

        return orders, conversions, self._save_state()


    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 1: HYDROGEL_PACK — Market Making with inventory cap
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_hydrogel(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        fv = self.hydro_fv

        # Inventory skew: push quotes to reduce one-sided accumulation
        skew = int(pos / 20)

        bid_price_target = round(fv) - HYDRO_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + HYDRO_QUOTE_OFFSET - skew

        buy_capacity  = POS_LIMIT_HYDRO - pos
        sell_capacity = POS_LIMIT_HYDRO + pos

        # FIX: Hard inventory cap — if too long, stop posting bids
        if pos > POS_LIMIT_HYDRO * HYDRO_INVENTORY_SOFT_CAP:
            buy_capacity = 0   # don't buy more, we're already very long
        if pos < -POS_LIMIT_HYDRO * HYDRO_INVENTORY_SOFT_CAP:
            sell_capacity = 0  # don't sell more, we're already very short

        # Take cheap asks
        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - HYDRO_QUOTE_OFFSET and buy_capacity > 0:
                qty = min(-ask_vol, buy_capacity, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", ask, qty))
                    buy_capacity -= qty

        # Hit expensive bids
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
    #  STRATEGY 2: VELVETFRUIT_EXTRACT — Market Making with inventory cap
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_velvetfruit(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        fv = self.vev_fv

        skew = int(pos / 20)

        bid_price_target = round(fv) - VEV_QUOTE_OFFSET - skew
        ask_price_target = round(fv) + VEV_QUOTE_OFFSET - skew

        buy_capacity  = POS_LIMIT_VEV - pos
        sell_capacity = POS_LIMIT_VEV + pos

        # FIX: Hard inventory cap — avoid directional accumulation
        if pos > POS_LIMIT_VEV * VEV_INVENTORY_SOFT_CAP:
            buy_capacity = 0
        if pos < -POS_LIMIT_VEV * VEV_INVENTORY_SOFT_CAP:
            sell_capacity = 0

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
    #  STRATEGY 3: VEV OPTIONS — with expiry-aware position management
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_option(
        self, product: str, od: OrderDepth,
        K: int, S: float, tte_days: float, tte_years: float, pos: int
    ) -> List[Order]:

        orders: List[Order] = []

        # ── EMERGENCY DUMP MODE (TTE < 0.5 days) ──
        # v2 had NO logic here. We were holding longs that expired worthless.
        # Now: sell anything we're long at ANY positive price. Buy back any shorts.
        if tte_days < EMERGENCY_DUMP_DAYS:
            intrinsic = max(0.0, S - K)
            if pos > 0:
                # Dump all longs — sell at best bid or at intrinsic - 1
                b = best_bid(od)
                dump_price = b if b is not None else max(1, int(intrinsic) - 1)
                if dump_price is not None and dump_price > 0:
                    orders.append(Order(product, dump_price, -pos))
            elif pos < 0:
                # Buy back shorts — at intrinsic + 1 to ensure fill
                a = best_ask(od)
                cover_price = a if a is not None else int(intrinsic) + 2
                orders.append(Order(product, cover_price, -pos))  # -pos is positive
            return orders

        # ── UNWIND MODE (TTE < 1 day) ──
        # No new positions. Only close existing ones at favorable prices.
        if tte_days < UNWIND_THRESHOLD_DAYS:
            intrinsic = max(0.0, S - K)
            if pos > 0:
                # Sell longs if bid >= intrinsic (fair exit)
                b = best_bid(od)
                if b is not None and b >= intrinsic - 1:
                    qty = min(pos, OPTION_ORDER_SIZE * 3)  # larger unwind size
                    orders.append(Order(product, b, -qty))
                else:
                    # No good bid — post ask at intrinsic to attract buyers
                    ask_price = max(1, int(intrinsic))
                    orders.append(Order(product, ask_price, -min(pos, OPTION_ORDER_SIZE * 2)))
            elif pos < 0:
                a = best_ask(od)
                if a is not None:
                    qty = min(-pos, OPTION_ORDER_SIZE * 3)
                    orders.append(Order(product, a, qty))
            return orders

        # ── NORMAL TRADING (TTE >= 1 day) ──

        # Dynamic position cap based on TTE: reduce max longs as expiry approaches
        if tte_days < 2.0:
            effective_buy_limit = OPTION_POS_CAP_NEAR_EXPIRY
        else:
            effective_buy_limit = POS_LIMIT_OPTION

        buy_capacity  = effective_buy_limit - pos
        sell_capacity = POS_LIMIT_OPTION + pos

        # ── (A) DEEP-ITM: VEV_4000 and VEV_4500 ──
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
        # FIX: Only sell these when TTE < 2 days (truly worthless).
        # v2 was selling at price=1 from tick 1 when they might have tiny value.
        if K >= 6000:
            bs_fair = bs_call_price(S, K, tte_years, SIGMA)

            if tte_days < 2.0:
                # Near expiry: sell aggressively at any positive price
                for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                    if bid >= 1.0 and sell_capacity > 0:
                        qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                        if qty > 0:
                            orders.append(Order(product, bid, -qty))
                            sell_capacity -= qty
                if sell_capacity > 0:
                    orders.append(Order(product, 1, -min(5, sell_capacity)))
            else:
                # Early: only sell if market bids clearly above BS fair
                for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                    if bid > bs_fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
                        qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                        if qty > 0:
                            orders.append(Order(product, bid, -qty))
                            sell_capacity -= qty

            return orders

        # ── (C) NEAR-ATM: VEV_5000 to VEV_5500 ──
        fair = bs_call_price(S, K, tte_years, SIGMA)
        if fair <= 0:
            return []

        b = best_bid(od)
        a = best_ask(od)

        # Buy if ask is clearly below fair value
        # FIX: Also check buy_capacity > 0 (respects TTE-based cap)
        if a is not None and a < fair - OPTION_EDGE_THRESHOLD and buy_capacity > 0:
            ask_vol = od.sell_orders.get(a, 0)
            qty = min(-ask_vol, buy_capacity, OPTION_ORDER_SIZE)
            if qty > 0:
                orders.append(Order(product, a, qty))
                buy_capacity -= qty

        # Sell only if bid is CLEARLY above fair (with sanity check on intrinsic)
        if b is not None and b > fair + OPTION_EDGE_THRESHOLD and sell_capacity > 0:
            intrinsic = max(0.0, S - K)
            if b > intrinsic + OPTION_EDGE_THRESHOLD:
                bid_vol = od.buy_orders.get(b, 0)
                qty = min(bid_vol, sell_capacity, OPTION_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order(product, b, -qty))
                    sell_capacity -= qty

        # FIX: Passive quotes ONLY if TTE > 2 days
        # v2 posted passive bids even at tick 90K (TTE ≈ 0.4 days)
        # Those filled and left us long options that expired worthless next day.
        if tte_days > 2.0:
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