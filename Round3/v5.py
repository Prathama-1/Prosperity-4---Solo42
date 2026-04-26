"""
IMC Prosperity Round 3 — trader.py (HYDROGEL ONLY)
====================================================
Strategy: Pure market-making on HYDROGEL_PACK only.

WHAT THE LOG REVEALED:
  - HYDROGEL spread = 16 ticks (bid ~10003, ask ~10019) 91.6% of the time
  - Market is very low volume: only ~23 trades per 10k-tick day
  - We are essentially the ONLY active trader hitting the book
  - Every fill we get is us TAKING from market makers (aggressive orders)
  - Our passive quotes do get hit too when price moves into us
  - HYDROGEL alone earns ~+177 per day realistically; theoretical max ~+500/day

  THE -7k CHART LOSS was entirely from OPTIONS (theta bleed from long calls).
  HYDROGEL itself was never the problem.

STRATEGY:
  1. Track fair value = EMA of (best_bid + best_ask) / 2 each tick
  2. Post PASSIVE limit orders INSIDE the 16-tick spread at ±5 from fair
     → We earn 5 ticks per side when filled (vs 8 if at mid but never filled)
  3. Take AGGRESSIVELY if ask < fair - 3 (someone selling cheap) or
     bid > fair + 3 (someone buying expensive)
  4. Inventory skew: if long, push quotes down (sell more eagerly);
     if short, push quotes up (buy more eagerly)
  5. Hard position limits respected: ±200

NOTE: +-7 = 1243
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional
import json


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

POS_LIMIT:    int   = 200    # HYDROGEL position limit
PASSIVE_OFF:  int   = 7    # Post passive quotes ±5 from fair (inside 16-tick spread)
AGGR_THRESH:  float = 7.5    # Take aggressively if price crosses fair by > 3 ticks
ORDER_SIZE:   int   = 15     # Max units per order
EMA_FAST:     float = 0.80   # Fast EMA weight for fair value
LOG_EVERY:    int   = 5000   # Print P&L summary every N ticks


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def best_bid(od: OrderDepth) -> Optional[int]:
    return max(od.buy_orders.keys()) if od.buy_orders else None

def best_ask(od: OrderDepth) -> Optional[int]:
    return min(od.sell_orders.keys()) if od.sell_orders else None

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER
# ─────────────────────────────────────────────────────────────────────────────

class Trader:

    def __init__(self):
        self.fair_value: Optional[float] = None
        self.timestamp:  int = 0
        self.cash:       float = 0.0
        self.last_log:   int = -1

    def _load(self, state: TradingState):
        if not state.traderData:
            return
        try:
            d = json.loads(state.traderData)
            self.fair_value = d.get("fv")
            self.cash       = d.get("cash", 0.0)
            self.last_log   = d.get("last_log", -1)
        except Exception:
            pass

    def _save(self) -> str:
        return json.dumps({
            "fv":       self.fair_value,
            "cash":     self.cash,
            "last_log": self.last_log,
        })

    def _log(self, pos: int, mid: float):
        ts = self.timestamp
        if self.last_log >= 0 and (ts - self.last_log) < LOG_EVERY:
            return
        self.last_log = ts
        mtm_pnl = self.cash + pos * mid
        print(f"[HYDRO ts={ts:6d}] pos={pos:+4d}  fair={self.fair_value:.1f}  "
              f"mid={mid:.1f}  cash={self.cash:+.0f}  MTM_pnl={mtm_pnl:+.0f}")

    def run(self, state: TradingState):
        self._load(state)
        self.timestamp = state.timestamp
        od_map = state.order_depths

        orders: Dict[str, List[Order]] = {}

        if "HYDROGEL_PACK" not in od_map:
            return orders, 0, self._save()

        od  = od_map["HYDROGEL_PACK"]
        pos = state.position.get("HYDROGEL_PACK", 0)
        b   = best_bid(od)
        a   = best_ask(od)

        if b is None and a is None:
            return orders, 0, self._save()

        # ── Instantaneous mid ──────────────────────────────────────────────
        if b is not None and a is not None:
            mid = (b + a) / 2.0
        elif b is not None:
            mid = float(b)
        else:
            mid = float(a)

        # ── EMA fair value ─────────────────────────────────────────────────
        if self.fair_value is None:
            self.fair_value = mid
        else:
            self.fair_value = EMA_FAST * mid + (1.0 - EMA_FAST) * self.fair_value

        fv = self.fair_value

        # ── Track cash from own fills ──────────────────────────────────────
        for trade in (state.own_trades or {}).get("HYDROGEL_PACK", []):
            if trade.buyer == "SUBMISSION":
                self.cash -= trade.price * trade.quantity
            else:
                self.cash += trade.price * trade.quantity

        # ── Log ────────────────────────────────────────────────────────────
        self._log(pos, mid)

        # ── Build orders ───────────────────────────────────────────────────
        hydro_orders: List[Order] = []
        buy_cap  = POS_LIMIT - pos
        sell_cap = POS_LIMIT + pos

        # 1. AGGRESSIVE TAKES
        if a is not None and a < fv - AGGR_THRESH and buy_cap > 0:
            qty = min(abs(od.sell_orders.get(a, 0)), buy_cap, ORDER_SIZE * 2)
            if qty > 0:
                hydro_orders.append(Order("HYDROGEL_PACK", a, qty))
                buy_cap -= qty
                print(f"[HYDRO ts={self.timestamp}] AGGR BUY  {qty}@{a}  fv={fv:.1f}  edge={fv-a:.1f}")

        if b is not None and b > fv + AGGR_THRESH and sell_cap > 0:
            qty = min(od.buy_orders.get(b, 0), sell_cap, ORDER_SIZE * 2)
            if qty > 0:
                hydro_orders.append(Order("HYDROGEL_PACK", b, -qty))
                sell_cap -= qty
                print(f"[HYDRO ts={self.timestamp}] AGGR SELL {qty}@{b}  fv={fv:.1f}  edge={b-fv:.1f}")

        # 2. PASSIVE QUOTES inside the 16-tick spread
        inv_skew = int(clamp(pos / 40.0, -3.0, 3.0))
        p_bid = round(fv) - PASSIVE_OFF - inv_skew
        p_ask = round(fv) + PASSIVE_OFF - inv_skew

        # Never cross
        if p_bid >= p_ask:
            p_bid = round(fv) - 1
            p_ask = round(fv) + 1

        # Don't accidentally cross the existing book (let aggressive block handle that)
        if a is not None:
            p_bid = min(p_bid, a - 1)
        if b is not None:
            p_ask = max(p_ask, b + 1)

        if p_bid < p_ask:
            if buy_cap > 0:
                hydro_orders.append(Order("HYDROGEL_PACK", p_bid,
                                          min(ORDER_SIZE, buy_cap)))
            if sell_cap > 0:
                hydro_orders.append(Order("HYDROGEL_PACK", p_ask,
                                          -min(ORDER_SIZE, sell_cap)))

        if hydro_orders:
            orders["HYDROGEL_PACK"] = hydro_orders

        return orders, 0, self._save()