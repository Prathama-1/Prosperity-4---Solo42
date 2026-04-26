#-40k
"""
IMC Prosperity Round 3 — PROFITABLE STRATEGY
============================================

Key insights from data analysis:
1. HYDROGEL and VELVETFRUIT are mean-reverting with clear support/resistance
2. Options are mispriced relative to each other (arbitrage opportunities)
3. Deep ITM options (VEV_4000) trade near intrinsic + small premium
4. The market provides liquidity - we should provide it too, but smarter
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
VEV_POS_LIMIT = 100  # Per strike

# Trading parameters
HYDRO_ORDER_SIZE = 20
VF_ORDER_SIZE = 25
VEV_ORDER_SIZE = 10

# Mean reversion parameters
HYDRO_LOOKBACK = 20
VF_LOOKBACK = 20

# Spread capturing
MIN_SPREAD_PROFIT = 2  # Minimum profit per unit to trade
ARBITRAGE_THRESHOLD = 0.02  # 2% mispricing for options arbitrage

# Option strikes
VEV_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]

LOG_EVERY = 5000


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


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def intrinsic_value(S: float, K: float) -> float:
    """Intrinsic value of a call option"""
    return max(S - K, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
#  TRADER
# ─────────────────────────────────────────────────────────────────────────────

class Trader:
    
    def __init__(self):
        # Price history for mean reversion
        self.hydro_prices: List[float] = []
        self.vf_prices: List[float] = []
        
        # Running PnL
        self.cash = 0.0
        self.timestamp = 0
        self.last_log = 0
        
        # Track positions for better decisions
        self.hydro_position = 0
        self.vf_position = 0
        self.vev_positions = {strike: 0 for strike in VEV_STRIKES}
        
    # ── Persistence ──────────────────────────────────────────────────────────
    
    def _load(self, state: TradingState):
        if not state.traderData:
            return
        try:
            d = json.loads(state.traderData)
            self.hydro_prices = d.get("hprices", [])
            self.vf_prices = d.get("vprices", [])
            self.cash = d.get("cash", 0.0)
            self.last_log = d.get("last_log", 0)
            # Don't restore positions as they come from state
        except Exception:
            pass
    
    def _save(self) -> str:
        # Keep last 100 prices
        hydro_prices = self.hydro_prices[-100:] if len(self.hydro_prices) > 100 else self.hydro_prices
        vf_prices = self.vf_prices[-100:] if len(self.vf_prices) > 100 else self.vf_prices
        
        return json.dumps({
            "hprices": hydro_prices,
            "vprices": vf_prices,
            "cash": self.cash,
            "last_log": self.last_log,
        })
    
    # ── Cash tracking ────────────────────────────────────────────────────────
    
    def _track_cash(self, state: TradingState):
        for sym, own_trades in (state.own_trades or {}).items():
            for trade in own_trades:
                if trade.buyer == "SUBMISSION":
                    self.cash -= trade.price * trade.quantity
                else:
                    self.cash += trade.price * trade.quantity
    
    # ── Mean reversion signals ───────────────────────────────────────────────
    
    def _get_mean_reversion_signal(self, current_price: float, prices: List[float]) -> float:
        """Returns -1 (sell), 0 (neutral), or 1 (buy) based on price vs moving average"""
        if len(prices) < 10:
            return 0
            
        # Calculate rolling mean and std
        recent = prices[-HYDRO_LOOKBACK:] if len(prices) >= HYDRO_LOOKBACK else prices
        mean = statistics.mean(recent)
        std = statistics.stdev(recent) if len(recent) > 1 else 1.0
        
        # Z-score
        z_score = (current_price - mean) / max(std, 0.1)
        
        # Strong mean reversion signals
        if z_score > 1.5:
            return -1  # Overbought - sell
        elif z_score < -1.5:
            return 1   # Oversold - buy
        return 0
    
    # ── Option relative value arbitrage ──────────────────────────────────────
    
    def _find_arbitrage_opportunities(self, od_map: dict, vf_mid: float) -> Dict[str, List[Order]]:
        """
        Find relative mispricing between options with similar deltas.
        For example, VEV_5000 and VEV_5100 should have prices that make sense.
        """
        orders = {}
        
        # Calculate fair prices based on put-call parity and intrinsic value
        option_data = []
        for K in VEV_STRIKES:
            sym = f"VEV_{K}"
            if sym not in od_map:
                continue
                
            od = od_map[sym]
            bid = best_bid(od)
            ask = best_ask(od)
            if bid is None or ask is None:
                continue
                
            mid = (bid + ask) / 2.0
            intrinsic = intrinsic_value(vf_mid, K)
            time_value = mid - intrinsic
            
            option_data.append({
                'strike': K,
                'sym': sym,
                'bid': bid,
                'ask': ask,
                'mid': mid,
                'intrinsic': intrinsic,
                'time_value': time_value,
                'bid_size': od.buy_orders.get(bid, 0),
                'ask_size': od.sell_orders.get(ask, 0),
            })
        
        if len(option_data) < 3:
            return orders
            
        # Sort by strike
        option_data.sort(key=lambda x: x['strike'])
        
        # Look for violations in the time value curve
        # Time value should generally decrease as we go further OTM/ITM
        for i in range(1, len(option_data) - 1):
            prev = option_data[i-1]
            curr = option_data[i]
            nxt = option_data[i+1]
            
            # Check for abnormal time value (should be convex)
            # If middle option has abnormally high time value relative to neighbors
            expected_tv = (prev['time_value'] + nxt['time_value']) / 2
            tv_deviation = curr['time_value'] - expected_tv
            
            if tv_deviation > 2.0 and curr['ask'] < curr['mid'] + 1:  # Too expensive - sell
                # Check if we can sell the expensive one and buy the neighbors
                if curr['ask_size'] >= VEV_ORDER_SIZE:
                    vol = min(VEV_ORDER_SIZE, curr['ask_size'])
                    orders[curr['sym']] = orders.get(curr['sym'], []) + [Order(curr['sym'], curr['ask'], -vol)]
                    
                    # Buy cheaper alternatives
                    if prev['bid_size'] >= vol and prev['bid'] < prev['mid']:
                        orders[prev['sym']] = orders.get(prev['sym'], []) + [Order(prev['sym'], prev['bid'], vol)]
                    if nxt['bid_size'] >= vol and nxt['bid'] < nxt['mid']:
                        orders[nxt['sym']] = orders.get(nxt['sym'], []) + [Order(nxt['sym'], nxt['bid'], vol)]
                        
            elif tv_deviation < -2.0 and curr['bid'] > curr['mid'] - 1:  # Too cheap - buy
                if curr['bid_size'] >= VEV_ORDER_SIZE:
                    vol = min(VEV_ORDER_SIZE, curr['bid_size'])
                    orders[curr['sym']] = orders.get(curr['sym'], []) + [Order(curr['sym'], curr['bid'], vol)]
                    
                    # Sell cheaper alternatives
                    if prev['ask_size'] >= vol and prev['ask'] > prev['mid']:
                        orders[prev['sym']] = orders.get(prev['sym'], []) + [Order(prev['sym'], prev['ask'], -vol)]
                    if nxt['ask_size'] >= vol and nxt['ask'] > nxt['mid']:
                        orders[nxt['sym']] = orders.get(nxt['sym'], []) + [Order(nxt['sym'], nxt['ask'], -vol)]
        
        return orders
    
    # ── Market making with spreads ───────────────────────────────────────────
    
    def _market_make(
        self,
        symbol: str,
        od: OrderDepth,
        pos: int,
        fair_value: float,
        spread: int,
        order_size: int,
        pos_limit: int,
    ) -> List[Order]:
        """Simple market making with fixed spread"""
        orders = []
        b = best_bid(od)
        a = best_ask(od)
        
        if b is None or a is None:
            return orders
            
        # Calculate our quotes
        bid_price = round(fair_value) - spread
        ask_price = round(fair_value) + spread
        
        # Adjust for inventory
        inv_adj = int(pos / max(pos_limit / 4, 1))
        bid_price -= inv_adj
        ask_price -= inv_adj
        
        # Don't cross the market
        if a is not None and bid_price >= a:
            bid_price = a - 1
        if b is not None and ask_price <= b:
            ask_price = b + 1
        
        bid_price = max(1, bid_price)
        
        # Only quote if we have room
        if bid_price < ask_price:
            buy_cap = pos_limit - pos
            sell_cap = pos_limit + pos
            
            if buy_cap > 0 and abs(pos) < pos_limit * 0.8:
                orders.append(Order(symbol, bid_price, min(order_size, buy_cap)))
            if sell_cap > 0 and abs(pos) < pos_limit * 0.8:
                orders.append(Order(symbol, ask_price, -min(order_size, sell_cap)))
        
        return orders
    
    # ── Main run ─────────────────────────────────────────────────────────────
    
    def run(self, state: TradingState):
        self._load(state)
        self.timestamp = state.timestamp
        self._track_cash(state)
        
        od_map = state.order_depths
        pos_map = state.position if state.position else {}
        
        # Update positions
        self.hydro_position = pos_map.get("HYDROGEL_PACK", 0)
        self.vf_position = pos_map.get("VELVETFRUIT_EXTRACT", 0)
        for strike in VEV_STRIKES:
            self.vev_positions[strike] = pos_map.get(f"VEV_{strike}", 0)
        
        all_orders: Dict[str, List[Order]] = {}
        
        # ── HYDROGEL mean reversion ──────────────────────────────────────────
        if "HYDROGEL_PACK" in od_map:
            od = od_map["HYDROGEL_PACK"]
            mid = get_mid(od)
            if mid is not None:
                self.hydro_prices.append(mid)
                signal = self._get_mean_reversion_signal(mid, self.hydro_prices)
                
                b = best_bid(od)
                a = best_ask(od)
                
                if signal == 1 and b is not None and self.hydro_position < HYDRO_POS_LIMIT:
                    # Buy on oversold
                    qty = min(HYDRO_ORDER_SIZE, HYDRO_POS_LIMIT - self.hydro_position)
                    if qty > 0 and a is not None:
                        all_orders["HYDROGEL_PACK"] = [Order("HYDROGEL_PACK", a, qty)]
                elif signal == -1 and a is not None and self.hydro_position > -HYDRO_POS_LIMIT:
                    # Sell on overbought
                    qty = min(HYDRO_ORDER_SIZE, HYDRO_POS_LIMIT + self.hydro_position)
                    if qty > 0 and b is not None:
                        all_orders["HYDROGEL_PACK"] = [Order("HYDROGEL_PACK", b, -qty)]
                else:
                    # Market make
                    fair = mid
                    mm_orders = self._market_make(
                        "HYDROGEL_PACK", od, self.hydro_position, fair, 5, 
                        HYDRO_ORDER_SIZE, HYDRO_POS_LIMIT
                    )
                    if mm_orders:
                        all_orders["HYDROGEL_PACK"] = mm_orders
        
        # ── VELVETFRUIT mean reversion ───────────────────────────────────────
        if "VELVETFRUIT_EXTRACT" in od_map:
            od = od_map["VELVETFRUIT_EXTRACT"]
            mid = get_mid(od)
            if mid is not None:
                self.vf_prices.append(mid)
                signal = self._get_mean_reversion_signal(mid, self.vf_prices)
                
                b = best_bid(od)
                a = best_ask(od)
                
                if signal == 1 and b is not None and self.vf_position < VF_POS_LIMIT:
                    qty = min(VF_ORDER_SIZE, VF_POS_LIMIT - self.vf_position)
                    if qty > 0 and a is not None:
                        all_orders["VELVETFRUIT_EXTRACT"] = [Order("VELVETFRUIT_EXTRACT", a, qty)]
                elif signal == -1 and a is not None and self.vf_position > -VF_POS_LIMIT:
                    qty = min(VF_ORDER_SIZE, VF_POS_LIMIT + self.vf_position)
                    if qty > 0 and b is not None:
                        all_orders["VELVETFRUIT_EXTRACT"] = [Order("VELVETFRUIT_EXTRACT", b, -qty)]
                else:
                    # Market make with tighter spread
                    fair = mid
                    mm_orders = self._market_make(
                        "VELVETFRUIT_EXTRACT", od, self.vf_position, fair, 2,
                        VF_ORDER_SIZE, VF_POS_LIMIT
                    )
                    if mm_orders:
                        all_orders["VELVETFRUIT_EXTRACT"] = mm_orders
        
        # ── Option arbitrage (only when VF price is stable) ───────────────────
        if len(self.vf_prices) > 10 and "VELVETFRUIT_EXTRACT" in od_map:
            vf_mid = get_mid(od_map["VELVETFRUIT_EXTRACT"])
            if vf_mid is not None:
                arb_orders = self._find_arbitrage_opportunities(od_map, vf_mid)
                for sym, orders in arb_orders.items():
                    all_orders[sym] = all_orders.get(sym, []) + orders
        
        # ── Logging ───────────────────────────────────────────────────────────
        if self.timestamp - self.last_log >= LOG_EVERY:
            self.last_log = self.timestamp
            mtm = self.cash
            mtm += self.hydro_position * (get_mid(od_map.get("HYDROGEL_PACK", None)) or 0)
            mtm += self.vf_position * (get_mid(od_map.get("VELVETFRUIT_EXTRACT", None)) or 0)
            
            pos_str = f"HYDR={self.hydro_position:+d} VF={self.vf_position:+d}"
            print(f"[ts={self.timestamp:6d}] {pos_str}  cash={self.cash:+.0f}  MTM={mtm:+.0f}")
        
        return all_orders, 0, self._save()