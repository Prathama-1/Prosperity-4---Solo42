from datamodel import OrderDepth, UserId, TradingState, Order, ConversionObservation
from typing import List, Dict, Optional
import jsonpickle

# ═══════════════════════════════════════════════════════════════════════════════
#  WHAT CHANGED FROM V2 → V3
# ═══════════════════════════════════════════════════════════════════════════════
#
#  CHANGE A — POSITION LIMITS CORRECTED TO 80
#  ────────────────────────────────────────────
#  V2 used position_limit=20.  Actual game limits are 80 for both products.
#  This is a MASSIVE change — we can now hold 4× the inventory.
#  More inventory capacity means:
#    • We absorb more mispriced orders before hitting the wall
#    • Passive quotes remain active longer before the limit clips them
#    • Conversion arb can move up to 80 units per tick instead of 20
#  Expected PnL impact: roughly 4× improvement from market-making alone.
#
#  CHANGE B — TIERED INVENTORY SKEW SYSTEM
#  ─────────────────────────────────────────
#  V2 had a single binary skew: if |pos| > 15, shift quotes by 1.
#  With a limit of 80, that's useless — 1 tick of skew at position 79
#  is identical to position 16.  We need graduated pressure:
#
#    Zone 0  |pos| ≤ 20   → no skew      (normal aggressive quoting)
#    Zone 1  |pos| 21–40  → skew 1 tick  (mild nudge toward mean reversion)
#    Zone 2  |pos| 41–60  → skew 2 ticks (stronger push)
#    Zone 3  |pos| 61–80  → skew 3 ticks (defensive — must unwind fast)
#
#  This means:
#    • We stay fully aggressive for the first 25% of capacity (0–20)
#    • We gradually discourage further accumulation as we load up
#    • At 60+ we actively price to dump inventory before hitting hard limit
#    • We NEVER just stop quoting on one side — we always provide liquidity
#      but price it to naturally attract the reversion trade
#
#  CHANGE C — SOFT LIMITS RESCALED TO MATCH NEW POSITION LIMIT
#  ─────────────────────────────────────────────────────────────
#  With limit=80, the tiered zones replace the old single soft_position_limit.
#  The zone boundaries (20/40/60) are stored as "skew_tiers" in PARAMS.
#
#  CHANGE D — CONVERSION ARB SCALED TO NEW LIMIT
#  ───────────────────────────────────────────────
#  max_qty calculations now use 80 instead of 20, so we can arbitrage
#  up to 80 units per tick when the spread is open.
#
# ═══════════════════════════════════════════════════════════════════════════════


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "fair_value": 10_000,
        "take_width": 1,
        "disregard_edge": 1,
        "join_edge": 2,
        "default_edge": 2,
        "clear_width": 0,
        # Tiered skew boundaries — positions beyond each boundary add 1 more tick of skew
        "skew_tiers": [20, 40, 60],   # CHANGE B/C
        "position_limit": 80,         # CHANGE A: was 20
    },
    Product.IPR: {
        "base_day_minus2": 10_000,
        "trend_per_tick": 0.001,
        "take_width": 1,
        "disregard_edge": 1,
        "join_edge": 2,
        "default_edge": 2,
        "clear_width": 0,
        "prevent_adverse": True,
        "adverse_volume": 15,
        "skew_tiers": [20, 40, 60],   # CHANGE B/C
        "position_limit": 80,         # CHANGE A: was 20
    },
}


class Trader:
    """
    ═══════════════════════════════════════════════════════════════════
    STRATEGY OVERVIEW  (V3)
    ═══════════════════════════════════════════════════════════════════

    ASH-COATED OSMIUM (ACO)  — Pure Market Maker
    ─────────────────────────────────────────────
    Fair value rock-solid at 10 000.  Position limit: ±80.
    Pipeline each tick:
      1. TAKE (multi-level)  – sweep ALL mispriced levels in the book
      2. CLEAR               – unwind inventory at exact fair value
      3. MAKE                – post bid/ask with tiered skew:
                               0–20:  no skew (full aggression)
                               21–40: ±1 tick skew
                               41–60: ±2 tick skew
                               61–80: ±3 tick skew (defensive dump mode)

    INTARIAN PEPPER ROOT (IPR)  — Trending Market Maker
    ─────────────────────────────────────────────────────
    FV(t) = base + 0.001 * timestamp.  Position limit: ±80.
    Base locked in on first tick of each day via traderData.
    Same tiered skew system as ACO.
    Adverse-volume filter (>15) prevents trading against large MM bots.

    CONVERSION ARBITRAGE
    ─────────────────────
    Checks import and export arb every tick.
    Now capable of moving up to 80 units (was 20).
    Only converts when net profit > 0 after all fees.

    TIERED SKEW DETAIL
    ───────────────────
    _get_skew(position, tiers) counts how many tier boundaries |position|
    has crossed and returns that count as the skew magnitude.
    The sign of the skew is always toward zero (selling skew when long,
    buying skew when short).
    ═══════════════════════════════════════════════════════════════════
    """

    def __init__(self, params=None):
        self.params = params if params is not None else PARAMS
        self.LIMIT = {
            Product.ACO: self.params[Product.ACO]["position_limit"],
            Product.IPR: self.params[Product.IPR]["position_limit"],
        }

    # ──────────────────────────────────────────────────────────────
    #  CHANGE B: Tiered skew calculator
    # ──────────────────────────────────────────────────────────────

    def _get_skew(self, position: int, tiers: List[int]) -> int:
        """
        Returns the number of skew ticks to apply based on how many
        tier boundaries |position| has crossed.

        Example with tiers=[20, 40, 60]:
          |pos| = 10  → 0 ticks  (below tier 0)
          |pos| = 25  → 1 tick   (crossed tier 0 boundary)
          |pos| = 45  → 2 ticks  (crossed tiers 0 and 1)
          |pos| = 65  → 3 ticks  (crossed all three tiers)
        """
        abs_pos = abs(position)
        skew = sum(1 for tier in tiers if abs_pos > tier)
        return skew

    # ──────────────────────────────────────────────────────────────
    #  CHANGE 5 (from V2): Stable IPR fair value with day memory
    # ──────────────────────────────────────────────────────────────

    def _ipr_fair_value(self, state: TradingState, trader_obj: dict) -> float:
        p = self.params[Product.IPR]
        ts = state.timestamp

        if "ipr_base" in trader_obj:
            return trader_obj["ipr_base"] + p["trend_per_tick"] * ts

        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            observed_mid = (best_bid + best_ask) / 2
            inferred_base = observed_mid - p["trend_per_tick"] * ts
            snapped_base = round(inferred_base / 1000) * 1000
            trader_obj["ipr_base"] = snapped_base
            return snapped_base + p["trend_per_tick"] * ts

        return 12_000 + p["trend_per_tick"] * ts

    # ──────────────────────────────────────────────────────────────
    #  Multi-level taking (from V2, unchanged)
    # ──────────────────────────────────────────────────────────────

    def take_best_orders(
        self,
        product: str,
        fair_value: float,
        take_width: float,
        orders: List[Order],
        order_depth: OrderDepth,
        position: int,
        buy_vol: int,
        sell_vol: int,
        prevent_adverse: bool = False,
        adverse_volume: int = 0,
    ):
        limit = self.LIMIT[product]

        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > fair_value - take_width:
                break
            ask_vol = -order_depth.sell_orders[ask_price]
            if prevent_adverse and ask_vol > adverse_volume:
                continue
            qty = min(ask_vol, limit - position - buy_vol)
            if qty <= 0:
                break
            orders.append(Order(product, ask_price, qty))
            buy_vol += qty
            order_depth.sell_orders[ask_price] += qty
            if order_depth.sell_orders[ask_price] == 0:
                del order_depth.sell_orders[ask_price]

        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price < fair_value + take_width:
                break
            bid_vol = order_depth.buy_orders[bid_price]
            if prevent_adverse and bid_vol > adverse_volume:
                continue
            qty = min(bid_vol, limit + position - sell_vol)
            if qty <= 0:
                break
            orders.append(Order(product, bid_price, -qty))
            sell_vol += qty
            order_depth.buy_orders[bid_price] -= qty
            if order_depth.buy_orders[bid_price] == 0:
                del order_depth.buy_orders[bid_price]

        return buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  Clear position (unchanged from V2)
    # ──────────────────────────────────────────────────────────────

    def clear_position_order(
        self,
        product: str,
        fair_value: float,
        clear_width: int,
        orders: List[Order],
        order_depth: OrderDepth,
        position: int,
        buy_vol: int,
        sell_vol: int,
    ):
        limit = self.LIMIT[product]
        pos_after = position + buy_vol - sell_vol

        fair_bid = round(fair_value - clear_width)
        fair_ask = round(fair_value + clear_width)

        if pos_after > 0:
            clearable = sum(
                v for p, v in order_depth.buy_orders.items() if p >= fair_ask
            )
            clearable = min(clearable, pos_after)
            send = min(limit + position - sell_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_ask, -send))
                sell_vol += send

        elif pos_after < 0:
            clearable = sum(
                abs(v) for p, v in order_depth.sell_orders.items() if p <= fair_bid
            )
            clearable = min(clearable, abs(pos_after))
            send = min(limit - position - buy_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_bid, send))
                buy_vol += send

        return buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  CHANGE B: Make orders with tiered skew
    # ──────────────────────────────────────────────────────────────

    def make_orders(
        self,
        product: str,
        order_depth: OrderDepth,
        fair_value: float,
        position: int,
        buy_vol: int,
        sell_vol: int,
        disregard_edge: float,
        join_edge: float,
        default_edge: float,
        skew_tiers: List[int],
    ):
        """
        Quote logic is identical to V2, but inventory skew is now TIERED.

        The skew magnitude = number of tier boundaries crossed by |position|.
        Direction is always toward zero:
          - Long position  → lower ask (sell cheaper) AND lower bid (buy less)
          - Short position → raise bid (buy higher) AND raise ask (sell less)

        This means at extreme positions (61–80) we are quoting 3 ticks
        off-centre, which aggressively attracts the other side and prevents
        hitting the hard limit while still capturing some spread.
        """
        orders: List[Order] = []
        limit = self.LIMIT[product]

        asks_above = [p for p in order_depth.sell_orders if p > fair_value + disregard_edge]
        bids_below  = [p for p in order_depth.buy_orders  if p < fair_value - disregard_edge]

        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        # Base quote prices
        if best_ask_above is not None:
            ask = best_ask_above if best_ask_above - fair_value <= join_edge else best_ask_above - 1
        else:
            ask = round(fair_value + default_edge)

        if best_bid_below is not None:
            bid = best_bid_below if fair_value - best_bid_below <= join_edge else best_bid_below + 1
        else:
            bid = round(fair_value - default_edge)

        # CHANGE B: tiered skew — more ticks of pressure as position grows
        skew = self._get_skew(position, skew_tiers)
        if skew > 0:
            if position > 0:
                # Long: push ask down and bid down → encourage sells, discourage buys
                ask -= skew
                bid -= skew
            else:
                # Short: push bid up and ask up → encourage buys, discourage sells
                bid += skew
                ask += skew

        buy_qty = limit - (position + buy_vol)
        if buy_qty > 0:
            orders.append(Order(product, round(bid), buy_qty))

        sell_qty = limit + (position - sell_vol)
        if sell_qty > 0:
            orders.append(Order(product, round(ask), -sell_qty))

        return orders, buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  CHANGE D: Conversion arb scaled to limit=80
    # ──────────────────────────────────────────────────────────────

    def calculate_conversions(self, state: TradingState) -> int:
        product = Product.IPR

        if product not in state.order_depths:
            return 0

        obs: Optional[ConversionObservation] = None
        if hasattr(state, "observations") and state.observations:
            conv_obs = getattr(state.observations, "conversionObservations", {})
            obs = conv_obs.get(product)

        if obs is None:
            return 0

        od  = state.order_depths[product]
        pos = state.position.get(product, 0)
        limit = self.LIMIT[product]   # now 80

        transport = getattr(obs, "transportFees", 0)
        export_t  = getattr(obs, "exportTariff",  0)
        import_t  = getattr(obs, "importTariff",  0)
        auth_bid  = getattr(obs, "bidPrice",  0)
        auth_ask  = getattr(obs, "askPrice",  0)

        # IMPORT ARB: authority → us → market (we end up short, sell on market)
        if od.buy_orders:
            market_bid = max(od.buy_orders.keys())
            import_cost = auth_ask + transport + import_t
            if market_bid - import_cost > 0:
                max_qty = limit + pos          # how much short room we have
                qty = min(max_qty, od.buy_orders[market_bid])
                if qty > 0:
                    return int(qty)            # positive = import

        # EXPORT ARB: market → us → authority (we end up long, buy on market)
        if od.sell_orders:
            market_ask = min(od.sell_orders.keys())
            export_revenue = auth_bid - transport - export_t
            if export_revenue - market_ask > 0:
                max_qty = limit - pos          # how much long room we have
                qty = min(max_qty, -od.sell_orders[market_ask])
                if qty > 0:
                    return -int(qty)           # negative = export

        return 0

    # ──────────────────────────────────────────────────────────────
    #  Main entry point
    # ──────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        trader_obj = {}
        if state.traderData:
            try:
                trader_obj = jsonpickle.decode(state.traderData)
            except Exception:
                trader_obj = {}

        result: Dict[str, List[Order]] = {}

        # ── ASH-COATED OSMIUM ──────────────────────────────────────
        if Product.ACO in state.order_depths:
            ap  = self.params[Product.ACO]
            pos = state.position.get(Product.ACO, 0)
            od  = state.order_depths[Product.ACO]
            fv  = ap["fair_value"]

            orders: List[Order] = []
            bv = sv = 0

            bv, sv = self.take_best_orders(
                Product.ACO, fv, ap["take_width"],
                orders, od, pos, bv, sv,
            )
            bv, sv = self.clear_position_order(
                Product.ACO, fv, ap["clear_width"],
                orders, od, pos, bv, sv,
            )
            make, bv, sv = self.make_orders(
                Product.ACO, od, fv, pos, bv, sv,
                ap["disregard_edge"], ap["join_edge"], ap["default_edge"],
                ap["skew_tiers"],
            )
            orders += make
            result[Product.ACO] = orders

        # ── INTARIAN PEPPER ROOT ───────────────────────────────────
        if Product.IPR in state.order_depths:
            pp  = self.params[Product.IPR]
            pos = state.position.get(Product.IPR, 0)
            od  = state.order_depths[Product.IPR]
            fv  = self._ipr_fair_value(state, trader_obj)

            orders: List[Order] = []
            bv = sv = 0

            bv, sv = self.take_best_orders(
                Product.IPR, fv, pp["take_width"],
                orders, od, pos, bv, sv,
                pp["prevent_adverse"], pp["adverse_volume"],
            )
            bv, sv = self.clear_position_order(
                Product.IPR, fv, pp["clear_width"],
                orders, od, pos, bv, sv,
            )
            make, bv, sv = self.make_orders(
                Product.IPR, od, fv, pos, bv, sv,
                pp["disregard_edge"], pp["join_edge"], pp["default_edge"],
                pp["skew_tiers"],
            )
            orders += make
            result[Product.IPR] = orders

        conversions = self.calculate_conversions(state)
        trader_data = jsonpickle.encode(trader_obj)
        return result, conversions, trader_data