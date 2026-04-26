"""
IMC Prosperity Round 3 - PROFITABLE SCALING STRATEGY
=====================================================

KEY INSIGHTS:
1. VF is mean-reverting (range 5255-5275)
2. Options are consistently cheap (ask < fair by 3-5)
3. Buy options when cheap, SELL when VF moves up or options become fairly priced
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math
import statistics


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

HYDRO_POS_LIMIT = 200
VF_POS_LIMIT = 200
VEV_POS_LIMIT = 300

# VF mean reversion
VF_MEAN = 5265.0
VF_RANGE = 10.0

# Option trading
MAX_OPTION_CONTRACTS = 50

# Time to expiry (5 days)
TTE_DAYS = 5.0
TTE_YEARS = TTE_DAYS / 365.0
VOL_CONST = 0.0337
SIGMA = VOL_CONST / math.sqrt(TTE_YEARS)

VEV_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
STRIKE_MAP = {f"VEV_{k}": k for k in VEV_STRIKES}

LOG_EVERY = 5000
PROFIT_TARGET = 3.0  # Sell when profit >= this amount per share


# ─────────────────────────────────────────────────────────────────────────────
#  BLACK-SCHOLES
# ─────────────────────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def option_fair_price(S: float, K: int) -> float:
    return bs_call(S, K, TTE_YEARS, SIGMA)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def best_bid(od: OrderDepth) -> Optional[int]:
    return max(od.buy_orders.keys()) if od.buy_orders else None


def best_ask(od: OrderDepth) -> Optional[int]:
    return min(od.sell_orders.keys()) if od.sell_orders else None


def get_mid(od: OrderDepth) -> Optional[float]:
    b, a = best_bid(od), best_ask(od)
    if b is not None and a is not None:
        return (b + a) / 2.0
    return None


def get_spread(od: OrderDepth) -> Optional[int]:
    b, a = best_bid(od), best_ask(od)
    if b is not None and a is not None:
        return a - b
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER
# ─────────────────────────────────────────────────────────────────────────────

class Trader:

    def __init__(self):
        self.vf_prices: List[float] = []
        self.vf_mean: float = VF_MEAN
        self.vf_trend: float = 0.0
        self.cash: float = 0.0
        self.timestamp: int = 0
        self.last_log: int = 0
        self.entry_prices: Dict[str, List[float]] = {}  # Track buy prices

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self, state: TradingState):
        if not state.traderData:
            return
        try:
            d = json.loads(state.traderData)
            self.vf_prices = d.get("vf_prices", [])
            self.vf_mean = d.get("vf_mean", VF_MEAN)
            self.cash = d.get("cash", 0.0)
            self.last_log = d.get("last_log", 0)
            self.entry_prices = d.get("entry_prices", {})
        except Exception:
            pass

    def _save(self) -> str:
        if len(self.vf_prices) > 10:
            self.vf_prices = self.vf_prices[-10:]
        return json.dumps({
            "vf_prices": self.vf_prices,
            "vf_mean": self.vf_mean,
            "cash": self.cash,
            "last_log": self.last_log,
            "entry_prices": self.entry_prices,
        })

    def _track_cash(self, state: TradingState):
        for sym, own_trades in (state.own_trades or {}).items():
            for trade in own_trades:
                if trade.buyer == "SUBMISSION":
                    self.cash -= trade.price * trade.quantity
                    # Track entry price
                    if sym.startswith("VEV_"):
                        avg_price = self.entry_prices.get(sym, 0)
                        total_qty = self.entry_prices.get(f"{sym}_qty", 0)
                        new_avg = (avg_price * total_qty + trade.price * trade.quantity) / (total_qty + trade.quantity)
                        self.entry_prices[sym] = new_avg
                        self.entry_prices[f"{sym}_qty"] = total_qty + trade.quantity
                else:
                    self.cash += trade.price * trade.quantity
                    # Clear entry price when selling
                    if sym.startswith("VEV_"):
                        qty_sold = trade.quantity
                        current_qty = self.entry_prices.get(f"{sym}_qty", 0)
                        new_qty = current_qty - qty_sold
                        if new_qty <= 0:
                            self.entry_prices.pop(sym, None)
                            self.entry_prices.pop(f"{sym}_qty", None)
                        else:
                            self.entry_prices[f"{sym}_qty"] = new_qty

    # ── VF analysis (mean reversion) ─────────────────────────────────────────

    def _update_vf(self, mid: float):
        self.vf_prices.append(mid)
        
        if len(self.vf_prices) >= 5:
            self.vf_mean = statistics.mean(self.vf_prices[-5:])
        
        if len(self.vf_prices) >= 3:
            recent = self.vf_prices[-3:]
            self.vf_trend = (recent[-1] - recent[0]) / len(recent)

    def _vf_signal(self) -> str:
        if len(self.vf_prices) < 3:
            return "NEUTRAL"
        
        current = self.vf_prices[-1]
        
        if current < self.vf_mean - 4:
            return "OVERSOLD"
        elif current > self.vf_mean + 4:
            return "OVERBOUGHT"
        
        return "NEUTRAL"

    # ── Market making ────────────────────────────────────────────────────────

    def _market_make(
        self,
        symbol: str,
        od: OrderDepth,
        pos: int,
        fair: float,
        offset: int,
        order_size: int,
        pos_limit: int,
    ) -> List[Order]:
        orders = []
        b = best_bid(od)
        a = best_ask(od)
        
        if fair is None or b is None or a is None:
            return orders
        
        if a - b > 8:
            return orders
        
        bid_price = round(fair) - offset
        ask_price = round(fair) + offset
        
        inv_ratio = pos / pos_limit if pos_limit > 0 else 0
        inv_adj = int(inv_ratio * 2)
        bid_price -= inv_adj
        ask_price -= inv_adj
        
        if bid_price >= a:
            bid_price = a - 1
        if ask_price <= b:
            ask_price = b + 1
        
        if bid_price >= ask_price:
            return orders
        
        bid_price = max(1, bid_price)
        
        bid_room = pos_limit - pos
        ask_room = pos_limit + pos
        
        if bid_room >= order_size:
            orders.append(Order(symbol, bid_price, order_size))
        if ask_room >= order_size:
            orders.append(Order(symbol, ask_price, -order_size))
        
        return orders

    # ── Option trading - BUY CHEAP, SELL WHEN PROFITABLE ────────────────────

    def _trade_options(self, od_map: dict, pos_map: dict, vf_mid: float) -> Dict[str, List[Order]]:
        result = {}
        signal = self._vf_signal()
        
        for sym, K in STRIKE_MAP.items():
            if sym not in od_map:
                continue
            
            od = od_map[sym]
            pos = pos_map.get(sym, 0)
            
            fair = option_fair_price(vf_mid, K)
            intrinsic = max(0.0, vf_mid - K)
            b = best_bid(od)
            a = best_ask(od)
            
            # ── SELL EXISTING POSITIONS WHEN PROFITABLE ─────────────────────
            if pos > 0 and b is not None:
                # Calculate profit if sold at bid
                avg_entry = self.entry_prices.get(sym, b)
                profit = b - avg_entry
                
                # Sell conditions:
                # 1. Profit target reached
                # 2. OR VF is overbought (expected to fall)
                # 3. OR option is fairly priced or overpriced
                if profit >= PROFIT_TARGET or (signal == "OVERBOUGHT" and K <= 5400) or b >= fair - 1.0:
                    vol = od.buy_orders.get(b, 0)
                    qty = min(pos, vol, 30)
                    if qty > 0:
                        result[sym] = [Order(sym, b, -qty)]
                        print(f"[ts={self.timestamp}] SELL {qty} of {sym} at {b} (entry={avg_entry:.1f}, profit={profit:.1f}, signal={signal})")
                        continue  # Skip buying if we just sold
            
            # ── BUY CHEAP OPTIONS ───────────────────────────────────────────
            if a is None:
                continue
            
            max_qty = min(MAX_OPTION_CONTRACTS, VEV_POS_LIMIT - pos)
            
            # Buy when ASK < FAIR - 3 (cheap)
            if a < fair - 3.0 and max_qty > 0:
                qty = min(max_qty, 25)  # Base size
                
                # Stronger signal when VF is OVERSOLD
                if signal == "OVERSOLD" and K <= 5400:
                    qty = min(max_qty, 40)
                
                # Deep ITM options are safer
                if K <= 5000 and intrinsic > 0:
                    qty = min(max_qty, 40)
                
                # Check available volume
                vol = od.sell_orders.get(a, 0)
                qty = min(qty, abs(vol))
                
                if qty > 0:
                    result[sym] = [Order(sym, a, qty)]
                    print(f"[ts={self.timestamp}] BUY {qty} of {sym} at {a} (fair={fair:.1f}, diff={fair - a:.1f}, signal={signal}, intrinsic={intrinsic:.1f})")
            
            # ── ALSO SELL ITM OPTIONS AT BID IF PRICE IS GOOD ───────────────
            elif pos > 0 and b is not None and b >= intrinsic + 2.0:
                # Sell ITM options at a good price even if not at profit target
                vol = od.buy_orders.get(b, 0)
                qty = min(pos, vol, 20)
                if qty > 0:
                    result[sym] = [Order(sym, b, -qty)]
                    print(f"[ts={self.timestamp}] SELL {qty} of {sym} at {b} (ITM exit)")
        
        return result

    # ── Main run ─────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        self._load(state)
        self.timestamp = state.timestamp
        self._track_cash(state)
        
        od_map = state.order_depths
        pos_map = state.position if state.position else {}
        
        all_orders: Dict[str, List[Order]] = {}
        
        # ── Update VF price ──────────────────────────────────────────────────
        vf_mid = None
        if "VELVETFRUIT_EXTRACT" in od_map:
            od = od_map["VELVETFRUIT_EXTRACT"]
            vf_mid = get_mid(od)
            if vf_mid is not None:
                self._update_vf(vf_mid)
        
        # ── Market making for HYDROGEL ───────────────────────────────────────
        if "HYDROGEL_PACK" in od_map:
            od = od_map["HYDROGEL_PACK"]
            hydro_mid = get_mid(od)
            if hydro_mid is not None:
                pos = pos_map.get("HYDROGEL_PACK", 0)
                spread = get_spread(od)
                
                if spread is not None and spread <= 6:
                    hydro_orders = self._market_make(
                        "HYDROGEL_PACK", od, pos, hydro_mid,
                        offset=3, order_size=10, pos_limit=HYDRO_POS_LIMIT
                    )
                    if hydro_orders:
                        all_orders["HYDROGEL_PACK"] = hydro_orders
        
        # ── Options (main profit driver) ─────────────────────────────────────
        if vf_mid is not None:
            opt_orders = self._trade_options(od_map, pos_map, vf_mid)
            for sym, orders in opt_orders.items():
                all_orders[sym] = orders
        
        # ── Simple VF mean reversion trading ─────────────────────────────────
        if vf_mid is not None and "VELVETFRUIT_EXTRACT" not in all_orders:
            pos = pos_map.get("VELVETFRUIT_EXTRACT", 0)
            signal = self._vf_signal()
            
            if signal == "OVERSOLD" and pos < 100:
                b = best_bid(od_map["VELVETFRUIT_EXTRACT"])
                if b is not None:
                    qty = min(20, 100 - pos)
                    if qty > 0:
                        all_orders["VELVETFRUIT_EXTRACT"] = [Order("VELVETFRUIT_EXTRACT", b, qty)]
                        print(f"[ts={self.timestamp}] BUY VF at {b} (oversold)")
            
            elif signal == "OVERBOUGHT" and pos > -100:
                a = best_ask(od_map["VELVETFRUIT_EXTRACT"])
                if a is not None:
                    qty = min(20, 100 + pos)
                    if qty > 0:
                        all_orders["VELVETFRUIT_EXTRACT"] = [Order("VELVETFRUIT_EXTRACT", a, -qty)]
                        print(f"[ts={self.timestamp}] SELL VF at {a} (overbought)")
        
        # ── Logging ─────────────────────────────────────────────────────────
        if self.timestamp - self.last_log >= LOG_EVERY:
            self.last_log = self.timestamp
            mtm = self.cash
            for sym in ["HYDROGEL_PACK", "VELVETFRUIT_EXTRACT"]:
                if sym in od_map:
                    mid = get_mid(od_map[sym])
                    if mid:
                        mtm += pos_map.get(sym, 0) * mid
            
            # Add options MTM
            for sym in STRIKE_MAP.keys():
                if sym in od_map and sym in pos_map and pos_map[sym] != 0:
                    mid = get_mid(od_map[sym])
                    if mid:
                        mtm += pos_map[sym] * mid
            
            opt_pos = sum(1 for s in pos_map if s.startswith("VEV_") and pos_map[s] != 0)
            pos_str = f"HYDR={pos_map.get('HYDROGEL_PACK',0):+d} VF={pos_map.get('VELVETFRUIT_EXTRACT',0):+d}"
            print(f"[ts={self.timestamp:6d}] {pos_str} opts={opt_pos} cash={self.cash:+.0f} MTM={mtm:+.0f}")
        
        return all_orders, 0, self._save()