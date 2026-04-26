#4k
"""
IMC Prosperity Round 3 — trader.py  (v6 — VOLATILITY SMILE STRATEGY)
======================================================================
COMPLETE REWRITE based on what top-10 global teams actually used.

WHY v5 ONLY MAKES 1-3K:
  - Fixed SIGMA=0.26 is wrong. IV changes every tick. Using stale sigma
    makes you systematically buy overpriced options and sell cheap ones.
  - Market-making on HYDRO/VEV with tight spreads earns ~3k max.
    The 100k+ teams are making their money on OPTIONS, not market-making.

THE REAL STRATEGY (used by 2nd place globally, 1st USA, top 1% teams):
  1. VOLATILITY SMILE: Fit a quadratic to implied IV vs log-moneyness
     across all option strikes every tick. This gives you a "fair IV" for
     each strike based on where the smile actually is RIGHT NOW.
  2. IV Z-SCORE: Track a rolling mean and std of IV. When IV deviates
     significantly (z > threshold), trade: buy when IV is low (options
     cheap), sell when IV is high (options expensive).
  3. ROLLING IV per strike: Don't use a quadratic alone — blend it with
     a per-strike rolling IV estimate for stability (avoids overfitting
     to the smile shape on any single tick).
  4. VELVETFRUIT mean-reversion: VEV has known mean-reversion properties.
     Use z-score of VEV price (not just EMA) to take directional bets.

ARCHITECTURE:
  - Option trading: IV smile fit + rolling IV z-score → directional vol trades
  - HYDROGEL: tight market-making with clamped skew (fixed from v5)
  - VELVETFRUIT: mean-reversion z-score directional trading
  - All expiry safety logic preserved from v3
"""

from datamodel import (
    OrderDepth, TradingState, Order,
    ConversionObservation, Listing, Observation,
    ProsperityEncoder, Symbol, Trade, UserId
)
from typing import Dict, List, Optional, Tuple
import json
import math


# ─────────────────────────────────────────────────────────────────────────────
#  BLACK-SCHOLES HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_call_price(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0:
        return max(0.0, S - K)
    if sigma <= 0 or S <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * norm_cdf(d2)

def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    if sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def implied_vol(market_price: float, S: float, K: float, T: float,
                lo: float = 0.01, hi: float = 5.0, tol: float = 1e-6) -> Optional[float]:
    """Binary search for implied volatility. Returns None if not solvable."""
    if T <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, S - K)
    if market_price <= intrinsic:
        return None
    if market_price >= S:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        price = bs_call_price(S, K, T, mid)
        if abs(price - market_price) < tol:
            return mid
        if price < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0

def log_moneyness(S: float, K: float, T: float) -> float:
    """m_t = log(K/S) / sqrt(T) — time-scaled log moneyness."""
    if T <= 0 or S <= 0 or K <= 0:
        return 0.0
    return math.log(K / S) / math.sqrt(T)


# ─────────────────────────────────────────────────────────────────────────────
#  SIMPLE STATS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fit_quadratic(xs: List[float], ys: List[float]) -> Optional[Tuple[float, float, float]]:
    """Fit y = a*x^2 + b*x + c via normal equations. Returns (a, b, c) or None."""
    n = len(xs)
    if n < 3:
        return None
    # Build sums for normal equations
    s0 = n
    s1 = sum(x for x in xs)
    s2 = sum(x**2 for x in xs)
    s3 = sum(x**3 for x in xs)
    s4 = sum(x**4 for x in xs)
    t0 = sum(y for y in ys)
    t1 = sum(xs[i]*ys[i] for i in range(n))
    t2 = sum(xs[i]**2*ys[i] for i in range(n))
    # Solve 3x3 system [s4 s3 s2; s3 s2 s1; s2 s1 s0] * [a; b; c] = [t2; t1; t0]
    A = [[s4, s3, s2], [s3, s2, s1], [s2, s1, s0]]
    B = [t2, t1, t0]
    # Gaussian elimination
    for col in range(3):
        pivot = A[col][col]
        if abs(pivot) < 1e-12:
            return None
        for row in range(col+1, 3):
            factor = A[row][col] / pivot
            for j in range(col, 3):
                A[row][j] -= factor * A[col][j]
            B[row] -= factor * B[col]
    if abs(A[2][2]) < 1e-12:
        return None
    c = B[2] / A[2][2]
    b = (B[1] - A[1][2]*c) / A[1][1] if abs(A[1][1]) > 1e-12 else 0.0
    a = (B[0] - A[0][1]*b - A[0][2]*c) / A[0][0] if abs(A[0][0]) > 1e-12 else 0.0
    return (a, b, c)


def rolling_mean_std(vals: List[float]) -> Tuple[float, float]:
    if not vals:
        return 0.0, 1.0
    n = len(vals)
    mean = sum(vals) / n
    if n < 2:
        return mean, 1.0
    var = sum((v - mean)**2 for v in vals) / (n - 1)
    return mean, max(math.sqrt(var), 1e-8)


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

# ATM-only options: the smile is well-defined here, IVs are stable
# Deep ITM/OTM have thin books and unreliable IVs
SMILE_FIT_VOUCHERS: List[str] = [
    "VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"
]

POS_LIMIT_HYDRO:  int = 200
POS_LIMIT_VEV:    int = 200
POS_LIMIT_OPTION: int = 300

HYDRO_QUOTE_OFFSET:    int   = 2
VEV_QUOTE_OFFSET:      int   = 1
HYDRO_ORDER_SIZE:      int   = 10
VEV_ORDER_SIZE:        int   = 8
OPTION_ORDER_SIZE:     int   = 15

TIMESTAMPS_PER_DAY:    int   = 10_000
TTE_START_DAYS:        float = 4.0
UNWIND_THRESHOLD_DAYS: float = 1.0
EMERGENCY_DUMP_DAYS:   float = 0.5
OPTION_POS_CAP_NEAR_EXPIRY: int = 200
DEEP_OTM_MAX_SHORT:    int   = 100

# Volatility smile strategy parameters
IV_HISTORY_LENGTH:     int   = 200    # rolling window for IV z-score
IV_Z_ENTRY:            float = 1.5    # enter when |z| > this
IV_Z_EXIT:             float = 0.5    # exit when |z| < this
IV_BLEND_SMILE:        float = 0.5    # weight on smile fit vs per-strike rolling IV

# VEV mean-reversion parameters
VEV_HISTORY_LENGTH:    int   = 100
VEV_Z_ENTRY:           float = 1.5    # enter directional VEV position when |z| > this
VEV_Z_EXIT:            float = 0.5

# Default sigma fallback if smile fitting fails
SIGMA_FALLBACK: float = 0.26


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def best_bid(od: OrderDepth) -> Optional[int]:
    return max(od.buy_orders.keys()) if od.buy_orders else None

def best_ask(od: OrderDepth) -> Optional[int]:
    return min(od.sell_orders.keys()) if od.sell_orders else None

def mid_price(od: OrderDepth) -> Optional[float]:
    b, a = best_bid(od), best_ask(od)
    if b is not None and a is not None:
        return (b + a) / 2.0
    return b if b is not None else a

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def vwap_mid(od: OrderDepth, levels: int = 3) -> Optional[float]:
    bvol = bpv = 0.0
    for price, vol in sorted(od.buy_orders.items(), reverse=True)[:levels]:
        bvol += vol; bpv += price * vol
    avol = apv = 0.0
    for price, vol in sorted(od.sell_orders.items())[:levels]:
        q = -vol; avol += q; apv += price * q
    bw = bpv / bvol if bvol > 0 else None
    aw = apv / avol if avol > 0 else None
    if bw is not None and aw is not None:
        return (bw + aw) / 2.0
    return bw if bw is not None else aw


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER
# ─────────────────────────────────────────────────────────────────────────────

class Trader:

    def __init__(self):
        # Fair values
        self.hydro_fv:    Optional[float] = None
        self.vev_fv:      Optional[float] = None
        self.timestamp:   int             = 0
        self.initialized: bool            = False

        # Per-strike rolling IV history (list of recent IVs)
        # Stored as dict: strike_int -> list of floats (max IV_HISTORY_LENGTH)
        self.iv_history:  Dict[int, List[float]] = {}

        # VEV price history for mean reversion z-score
        self.vev_history: List[float] = []

        # Smile fit params (a, b, c) for current tick — recomputed each tick
        self._smile_params: Optional[Tuple[float,float,float]] = None

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_state(self, state: TradingState):
        if not state.traderData or state.traderData == "":
            return
        try:
            d = json.loads(state.traderData)
            self.hydro_fv    = d.get("hydro_fv",    None)
            self.vev_fv      = d.get("vev_fv",      None)
            self.timestamp   = d.get("timestamp",   0)
            self.initialized = d.get("initialized", False)
            # Restore iv_history (keys are strings in JSON → convert back to int)
            raw_iv = d.get("iv_history", {})
            self.iv_history = {int(k): v for k, v in raw_iv.items()}
            self.vev_history = d.get("vev_history", [])
        except Exception:
            pass

    def _save_state(self) -> str:
        return json.dumps({
            "hydro_fv":    self.hydro_fv,
            "vev_fv":      self.vev_fv,
            "timestamp":   self.timestamp,
            "initialized": self.initialized,
            "iv_history":  {str(k): v[-IV_HISTORY_LENGTH:] for k, v in self.iv_history.items()},
            "vev_history": self.vev_history[-VEV_HISTORY_LENGTH:],
        })

    def _init_fair_values(self, od: Dict[str, OrderDepth]):
        if self.initialized:
            return
        if "HYDROGEL_PACK" in od:
            m = vwap_mid(od["HYDROGEL_PACK"])
            if m is not None:
                self.hydro_fv = m
        if "VELVETFRUIT_EXTRACT" in od:
            m = vwap_mid(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = m
        if self.hydro_fv is not None and self.vev_fv is not None:
            self.initialized = True

    def _compute_tte(self) -> Tuple[float, float]:
        elapsed = self.timestamp / TIMESTAMPS_PER_DAY
        tte_days = max(TTE_START_DAYS - elapsed, 0.0)
        return tte_days, tte_days / 365.0

    # ── Volatility Smile ─────────────────────────────────────────────────────

    def _update_iv_history(self, S: float, tte_years: float,
                            od: Dict[str, OrderDepth]):
        """Compute mid-market IV for each ATM voucher and update rolling history."""
        for voucher in SMILE_FIT_VOUCHERS:
            if voucher not in od:
                continue
            K = STRIKES[voucher]
            m = mid_price(od[voucher])
            if m is None or m <= 0:
                continue
            iv = implied_vol(m, S, K, tte_years)
            if iv is None or iv <= 0 or iv > 3.0:
                continue
            if K not in self.iv_history:
                self.iv_history[K] = []
            self.iv_history[K].append(iv)
            if len(self.iv_history[K]) > IV_HISTORY_LENGTH:
                self.iv_history[K] = self.iv_history[K][-IV_HISTORY_LENGTH:]

    def _fit_smile(self, S: float, tte_years: float) -> Optional[Tuple[float,float,float]]:
        """Fit quadratic IV smile: IV = a*m^2 + b*m + c where m = log-moneyness."""
        if tte_years <= 0:
            return None
        xs, ys = [], []
        for K, ivs in self.iv_history.items():
            if not ivs:
                continue
            mean_iv = sum(ivs) / len(ivs)
            m = log_moneyness(S, K, tte_years)
            xs.append(m)
            ys.append(mean_iv)
        if len(xs) < 3:
            return None
        return fit_quadratic(xs, ys)

    def _fair_iv_for_strike(self, S: float, K: int, tte_years: float) -> float:
        """
        Blended fair IV for a strike:
        - 50% from smile quadratic fit (global shape)
        - 50% from per-strike rolling mean (local stability)
        Falls back to SIGMA_FALLBACK if insufficient data.
        """
        fallback = SIGMA_FALLBACK

        # Per-strike rolling mean
        strike_mean = None
        if K in self.iv_history and len(self.iv_history[K]) >= 5:
            vals = self.iv_history[K]
            strike_mean = sum(vals) / len(vals)

        # Smile quadratic
        smile_iv = None
        if self._smile_params is not None and tte_years > 0:
            a, b, c = self._smile_params
            m = log_moneyness(S, K, tte_years)
            smile_iv = a * m * m + b * m + c
            if smile_iv <= 0 or smile_iv > 3.0:
                smile_iv = None

        if smile_iv is not None and strike_mean is not None:
            return IV_BLEND_SMILE * smile_iv + (1 - IV_BLEND_SMILE) * strike_mean
        elif smile_iv is not None:
            return smile_iv
        elif strike_mean is not None:
            return strike_mean
        return fallback

    def _iv_zscore(self, K: int, current_iv: float) -> Optional[float]:
        """Z-score of current IV relative to its rolling distribution."""
        if K not in self.iv_history or len(self.iv_history[K]) < 20:
            return None
        mean, std = rolling_mean_std(self.iv_history[K])
        return (current_iv - mean) / std

    # ── VEV mean reversion ───────────────────────────────────────────────────

    def _vev_zscore(self) -> Optional[float]:
        if len(self.vev_history) < 20:
            return None
        mean, std = rolling_mean_std(self.vev_history)
        current = self.vev_history[-1]
        return (current - mean) / std

    # ── Main run ─────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        self._load_state(state)
        self.timestamp = state.timestamp
        od = state.order_depths

        self._init_fair_values(od)
        if not self.initialized:
            return {}, 0, self._save_state()

        orders: Dict[str, List[Order]] = {}
        tte_days, tte_years = self._compute_tte()

        # ── Update EMA fair values ──
        if "HYDROGEL_PACK" in od:
            m = vwap_mid(od["HYDROGEL_PACK"])
            if m is not None:
                self.hydro_fv = 0.30 * m + 0.70 * self.hydro_fv

        if "VELVETFRUIT_EXTRACT" in od:
            m = vwap_mid(od["VELVETFRUIT_EXTRACT"])
            if m is not None:
                self.vev_fv = 0.30 * m + 0.70 * self.vev_fv
                self.vev_history.append(m)
                if len(self.vev_history) > VEV_HISTORY_LENGTH:
                    self.vev_history = self.vev_history[-VEV_HISTORY_LENGTH:]

        # ── Update IV history and fit smile ──
        if self.vev_fv is not None and tte_years > 0:
            self._update_iv_history(self.vev_fv, tte_years, od)
            self._smile_params = self._fit_smile(self.vev_fv, tte_years)

        # ── 1. HYDROGEL_PACK market-making ──
        if "HYDROGEL_PACK" in od:
            pos = state.position.get("HYDROGEL_PACK", 0)
            orders["HYDROGEL_PACK"] = self._market_make_hydrogel(od["HYDROGEL_PACK"], pos)

        # ── 2. VELVETFRUIT_EXTRACT mean-reversion ──
        if "VELVETFRUIT_EXTRACT" in od:
            pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
            orders["VELVETFRUIT_EXTRACT"] = self._trade_vev_meanrev(od["VELVETFRUIT_EXTRACT"], pos)

        # ── 3. VEV Options — IV smile strategy ──
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

        return orders, 0, self._save_state()

    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 1: HYDROGEL_PACK — tight market-making, clamped skew
    # ─────────────────────────────────────────────────────────────────────────

    def _market_make_hydrogel(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        fv = self.hydro_fv

        raw_skew = pos / 20.0
        skew = int(clamp(raw_skew, -(HYDRO_QUOTE_OFFSET-1), (HYDRO_QUOTE_OFFSET-1)))

        bid_p = round(fv) - HYDRO_QUOTE_OFFSET - skew
        ask_p = round(fv) + HYDRO_QUOTE_OFFSET - skew

        if bid_p >= round(fv) or ask_p <= round(fv):
            bid_p = round(fv) - 1
            ask_p = round(fv) + 1

        buy_cap  = POS_LIMIT_HYDRO - pos
        sell_cap = POS_LIMIT_HYDRO + pos

        aggr = HYDRO_QUOTE_OFFSET + 1

        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - aggr and buy_cap > 0:
                qty = min(-ask_vol, buy_cap, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", ask, qty))
                    buy_cap -= qty

        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + aggr and sell_cap > 0:
                qty = min(bid_vol, sell_cap, HYDRO_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("HYDROGEL_PACK", bid, -qty))
                    sell_cap -= qty

        if ask_p > bid_p:
            if buy_cap > 0:
                orders.append(Order("HYDROGEL_PACK", bid_p, min(HYDRO_ORDER_SIZE, buy_cap)))
            if sell_cap > 0:
                orders.append(Order("HYDROGEL_PACK", ask_p, -min(HYDRO_ORDER_SIZE, sell_cap)))

        return orders

    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 2: VELVETFRUIT — mean-reversion z-score directional
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_vev_meanrev(self, od: OrderDepth, pos: int) -> List[Order]:
        """
        Mean-reversion on VEV price. When price is significantly above its
        rolling mean (z > threshold), sell. When significantly below, buy.
        Also market-make around the EMA fair value with tight spread.
        """
        orders: List[Order] = []
        fv = self.vev_fv

        # ── Market making (tight) ──
        raw_skew = pos / 20.0
        skew = int(clamp(raw_skew, -(VEV_QUOTE_OFFSET-1), (VEV_QUOTE_OFFSET-1)))
        bid_p = round(fv) - VEV_QUOTE_OFFSET - skew
        ask_p = round(fv) + VEV_QUOTE_OFFSET - skew
        if bid_p >= round(fv) or ask_p <= round(fv):
            bid_p = round(fv) - 1
            ask_p = round(fv) + 1

        buy_cap  = POS_LIMIT_VEV - pos
        sell_cap = POS_LIMIT_VEV + pos
        aggr = VEV_QUOTE_OFFSET + 1

        for ask, ask_vol in sorted(od.sell_orders.items()):
            if ask < fv - aggr and buy_cap > 0:
                qty = min(-ask_vol, buy_cap, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", ask, qty))
                    buy_cap -= qty

        for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fv + aggr and sell_cap > 0:
                qty = min(bid_vol, sell_cap, VEV_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", bid, -qty))
                    sell_cap -= qty

        # ── Mean-reversion directional overlay ──
        z = self._vev_zscore()
        if z is not None:
            if z > VEV_Z_ENTRY:
                # Price is high → sell aggressively
                b = best_bid(od)
                if b is not None and sell_cap > 0:
                    qty = min(sell_cap, VEV_ORDER_SIZE * 3)
                    orders.append(Order("VELVETFRUIT_EXTRACT", b, -qty))
                    sell_cap -= qty
            elif z < -VEV_Z_ENTRY:
                # Price is low → buy aggressively
                a = best_ask(od)
                if a is not None and buy_cap > 0:
                    qty = min(buy_cap, VEV_ORDER_SIZE * 3)
                    orders.append(Order("VELVETFRUIT_EXTRACT", a, qty))
                    buy_cap -= qty

        if ask_p > bid_p:
            if buy_cap > 0:
                orders.append(Order("VELVETFRUIT_EXTRACT", bid_p, min(VEV_ORDER_SIZE, buy_cap)))
            if sell_cap > 0:
                orders.append(Order("VELVETFRUIT_EXTRACT", ask_p, -min(VEV_ORDER_SIZE, sell_cap)))

        return orders

    # ─────────────────────────────────────────────────────────────────────────
    #  STRATEGY 3: VEV OPTIONS — IV smile z-score strategy
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_option(
        self, product: str, od: OrderDepth,
        K: int, S: float, tte_days: float, tte_years: float, pos: int
    ) -> List[Order]:

        orders: List[Order] = []

        # ── EMERGENCY DUMP MODE (TTE < 0.5 days) ──
        if tte_days < EMERGENCY_DUMP_DAYS:
            intrinsic = max(0.0, S - K)
            if pos > 0:
                b = best_bid(od)
                dump_price = b if b is not None else max(1, int(intrinsic) - 1)
                if dump_price is not None and dump_price > 0:
                    orders.append(Order(product, dump_price, -pos))
            elif pos < 0:
                a = best_ask(od)
                cover_price = a if a is not None else int(intrinsic) + 2
                orders.append(Order(product, cover_price, -pos))
            return orders

        # ── UNWIND MODE (TTE < 1 day) ──
        if tte_days < UNWIND_THRESHOLD_DAYS:
            intrinsic = max(0.0, S - K)
            if pos > 0:
                b = best_bid(od)
                if b is not None and b >= intrinsic - 1:
                    orders.append(Order(product, b, -min(pos, OPTION_ORDER_SIZE * 3)))
                else:
                    orders.append(Order(product, max(1, int(intrinsic)),
                                        -min(pos, OPTION_ORDER_SIZE * 2)))
            elif pos < 0:
                a = best_ask(od)
                if a is not None:
                    orders.append(Order(product, a, min(-pos, OPTION_ORDER_SIZE * 3)))
            return orders

        # ── NORMAL TRADING ──
        buy_cap  = (OPTION_POS_CAP_NEAR_EXPIRY if tte_days < 2.0 else POS_LIMIT_OPTION) - pos
        sell_cap = POS_LIMIT_OPTION + pos

        # ── (A) DEEP-ITM: VEV_4000 and VEV_4500 — intrinsic value arb ──
        if K <= 4500:
            fair = S - K
            if fair <= 0:
                return []
            for ask, ask_vol in sorted(od.sell_orders.items()):
                if ask < fair - 2.0 and buy_cap > 0:
                    qty = min(-ask_vol, buy_cap, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, ask, qty))
                        buy_cap -= qty
            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                if bid > fair + 2.0 and sell_cap > 0:
                    qty = min(bid_vol, sell_cap, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_cap -= qty
            return orders

        # ── (B) DEEP-OTM: VEV_6000 and VEV_6500 ──
        if K >= 6000:
            fair_iv  = self._fair_iv_for_strike(S, K, tte_years)
            bs_fair  = bs_call_price(S, K, tte_years, fair_iv)
            for bid, bid_vol in sorted(od.buy_orders.items(), reverse=True):
                thresh = (bs_fair + 2.0 if tte_days >= 2.0 else 1.0)
                if bid >= thresh and sell_cap > 0 and pos > -DEEP_OTM_MAX_SHORT:
                    qty = min(bid_vol, sell_cap, OPTION_ORDER_SIZE, DEEP_OTM_MAX_SHORT + pos)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        sell_cap -= qty
            return orders

        # ── (C) NEAR-ATM: VEV_5000 to VEV_5500 — IV smile z-score ──
        # This is the main alpha source. We compare the CURRENT market IV
        # to the fair IV from the smile fit. If market IV is too high → sell
        # (vol is rich), if too low → buy (vol is cheap).

        b = best_bid(od)
        a = best_ask(od)
        if b is None and a is None:
            return []

        # Compute current market IV from the mid
        m = (b + a) / 2.0 if (b is not None and a is not None) else (b or a)
        if m is None or m <= 0:
            return []

        current_iv = implied_vol(m, S, K, tte_years)
        fair_iv    = self._fair_iv_for_strike(S, K, tte_years)

        if current_iv is None:
            # Fall back to price-based trading with fair IV
            fair_price = bs_call_price(S, K, tte_years, fair_iv)
            if a is not None and a < fair_price - 2.0 and buy_cap > 0:
                qty = min(-od.sell_orders.get(a,0), buy_cap, OPTION_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order(product, a, qty))
            if b is not None and b > fair_price + 2.0 and sell_cap > 0:
                intrinsic = max(0.0, S - K)
                if b > intrinsic + 2.0:
                    qty = min(od.buy_orders.get(b,0), sell_cap, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, b, -qty))
            return orders

        # IV deviation from fair
        iv_diff = current_iv - fair_iv

        # Also get z-score of current IV vs its rolling history
        z = self._iv_zscore(K, current_iv)

        # ── Trading signal: buy when IV is LOW (options cheap), sell when HIGH ──
        # Signal is stronger if BOTH iv_diff and z-score agree

        # IV too HIGH (options overpriced) → SELL
        rich_signal = (iv_diff > 0.01) and (z is None or z > IV_Z_ENTRY)
        # IV too LOW (options cheap) → BUY
        cheap_signal = (iv_diff < -0.01) and (z is None or z < -IV_Z_ENTRY)

        # Compute fair price using current smile fair IV
        fair_price = bs_call_price(S, K, tte_years, fair_iv)

        if cheap_signal:
            # Buy aggressively from the ask — IV is low, options are cheap
            if a is not None and buy_cap > 0:
                qty = min(-od.sell_orders.get(a, 0), buy_cap, OPTION_ORDER_SIZE)
                if qty > 0:
                    orders.append(Order(product, a, qty))
                    buy_cap -= qty

        elif rich_signal:
            # Sell aggressively at the bid — IV is high, options are expensive
            if b is not None and sell_cap > 0:
                intrinsic = max(0.0, S - K)
                if b > intrinsic + 1.0:
                    qty = min(od.buy_orders.get(b, 0), sell_cap, OPTION_ORDER_SIZE)
                    if qty > 0:
                        orders.append(Order(product, b, -qty))
                        sell_cap -= qty

        # ── Passive quotes based on fair price from smile ──
        if tte_days > 1.5:
            # Apply inventory skew to passive quotes
            opt_skew = clamp(pos / 30.0, -1.0, 1.0)
            passive_bid = math.floor(fair_price - 2.0 - opt_skew)
            passive_ask = math.ceil(fair_price + 2.0 - opt_skew)

            if passive_bid > 0 and passive_bid < passive_ask:
                if buy_cap > 0:
                    orders.append(Order(product, passive_bid,
                                        min(OPTION_ORDER_SIZE, buy_cap)))
                if sell_cap > 0:
                    orders.append(Order(product, passive_ask,
                                        -min(OPTION_ORDER_SIZE, sell_cap)))

        return orders