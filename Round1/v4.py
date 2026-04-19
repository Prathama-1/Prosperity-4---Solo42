from datamodel import OrderDepth, UserId, TradingState, Order, ConversionObservation
from typing import List, Dict, Optional
import jsonpickle

# ═══════════════════════════════════════════════════════════════════════════════
#  WHAT CHANGED FROM V3 → V4
# ═══════════════════════════════════════════════════════════════════════════════
#
#  KEY INSIGHT FROM COMPETITOR ANALYSIS
#  ──────────────────────────────────────
#  A top competitor is scoring ~7.3k purely from ACO by maintaining a
#  persistent long position of 50–60 units.  This signals one or both of:
#
#    (a) ACO's TRUE fair value is slightly below the quoted 10,000
#        (e.g. 9,997–9,999).  Resting bids near fair get filled constantly
#        while asks rarely do, causing natural long accumulation.
#
#    (b) Structural order flow asymmetry: large participants are net sellers
#        into the book, so the buy side fills more than the sell side.
#
#  In both cases the optimal strategy is NOT neutral market making but
#  DIRECTIONALLY BIASED market making — intentionally hold a long book.
#
#  CHANGE A — ACO ASYMMETRIC QUOTING
#  ────────────────────────────────────
#  New parameters:
#    "bid_edge": 1   → quote bid at fair - 1  (aggressive buy, gets filled often)
#    "ask_edge": 4   → quote ask at fair + 4  (patient sell, only fills on spikes)
#    "target_long":  50  → desired steady-state long position
#
#  The asymmetric spread means:
#    • We accumulate longs quickly (tight bid)
#    • We sell slowly at a premium (wide ask)
#    • Net position naturally drifts to +50/+60 and stays there
#    • Every sell that DOES happen is at +4 ticks above fair = great edge
#
#  CHANGE B — ACO TIERED SKEW RETUNED FOR LONG BIAS
#  ──────────────────────────────────────────────────
#  Old tiers were symmetric around 0.  New tiers are asymmetric:
#    Long side:  only skew defensively at 70+ (we WANT to be long up to 60)
#    Short side: skew aggressively at any short position (we never want short)
#
#  This means:
#    • 0  to +60 long  → no sell-side skew (hold the position)
#    • +60 to +80 long → mild skew to avoid hitting hard limit
#    • Any short       → immediately aggressive skew to get back to flat/long
#
#  CHANGE C — ACO TAKE LOGIC UPDATED FOR BIAS
#  ────────────────────────────────────────────
#  We still sweep mispriced asks (buy cheap) aggressively.
#  But we are now MORE conservative about hitting bids — we only sell
#  if the bid is ≥ fair + 5 (was fair + 1).  This prevents us from
#  accidentally selling out of our desired long position for tiny edge.
#
#  IPR and conversions are unchanged from V3.
#
# ═══════════════════════════════════════════════════════════════════════════════


class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        "fair_value": 10_000,

        # CHANGE C: asymmetric take widths
        "take_width_buy":  1,   # buy if ask ≤ fair - 1  (aggressive)
        "take_width_sell": 5,   # sell only if bid ≥ fair + 5  (patient)

        "disregard_edge": 1,
        "join_edge": 2,

        # CHANGE A: asymmetric quote edges
        "bid_edge": 1,          # tight bid → accumulate longs easily
        "ask_edge": 4,          # wide ask  → only sell at premium

        "clear_width": 0,

        # CHANGE B: asymmetric skew tiers
        # Long side: tolerate up to 60, only defend at 70+
        # Short side: always skew back toward long immediately
        "long_skew_tiers":  [60, 72],   # skew 1 at 60+, skew 2 at 72+
        "short_skew_tiers": [0, 20, 40],# skew 1 at any short, 2 at -20, 3 at -40

        "position_limit": 80,
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
        "skew_tiers": [20, 40, 60],
        "position_limit": 80,
    },
}


class Trader:
    """
    ═══════════════════════════════════════════════════════════════════
    STRATEGY OVERVIEW  (V4)
    ═══════════════════════════════════════════════════════════════════

    ASH-COATED OSMIUM (ACO)  — Directionally Biased Market Maker
    ──────────────────────────────────────────────────────────────
    Target: hold ~50–60 long.  Quote bid at fair-1, ask at fair+4.
    Take aggressively on the buy side (ask ≤ fair-1).
    Take conservatively on the sell side (bid ≥ fair+5 only).
    Skew tiers only kick in above +60 long or any short position.

    This mimics the competitor's observed behaviour and should
    capture the structural long bias in ACO's order flow.

    INTARIAN PEPPER ROOT (IPR)  — Trending Market Maker (unchanged)
    ──────────────────────────────────────────────────────────────────
    FV(t) = base + 0.001 * timestamp, base locked in on day 1.
    Symmetric quoting at fair ± 2 with tiered skew at [20, 40, 60].

    CONVERSION ARBITRAGE (unchanged from V3)
    ─────────────────────────────────────────
    Import/export arb on IPR up to ±80 units per tick.
    ═══════════════════════════════════════════════════════════════════
    """

    def __init__(self, params=None):
        self.params = params if params is not None else PARAMS
        self.LIMIT = {
            Product.ACO: self.params[Product.ACO]["position_limit"],
            Product.IPR: self.params[Product.IPR]["position_limit"],
        }

    # ──────────────────────────────────────────────────────────────
    #  Tiered skew helper (generic)
    # ──────────────────────────────────────────────────────────────

    def _get_skew(self, position: int, tiers: List[int]) -> int:
        abs_pos = abs(position)
        return sum(1 for tier in tiers if abs_pos > tier)

    # ──────────────────────────────────────────────────────────────
    #  IPR fair value (unchanged from V3)
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
    #  CHANGE C: Asymmetric taking for ACO
    # ──────────────────────────────────────────────────────────────

    def take_orders_aco(
        self,
        orders: List[Order],
        order_depth: OrderDepth,
        fair_value: float,
        position: int,
        buy_vol: int,
        sell_vol: int,
    ):
        """
        Buy aggressively (take_width_buy=1) but only sell if bid is
        very high (take_width_sell=5).  This lets us accumulate longs
        without churning out of them for 1-tick edge.
        """
        ap = self.params[Product.ACO]
        limit = self.LIMIT[Product.ACO]
        tw_buy  = ap["take_width_buy"]
        tw_sell = ap["take_width_sell"]

        # Sweep cheap asks (buy side — aggressive)
        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > fair_value - tw_buy:
                break
            ask_vol = -order_depth.sell_orders[ask_price]
            qty = min(ask_vol, limit - position - buy_vol)
            if qty <= 0:
                break
            orders.append(Order(Product.ACO, ask_price, qty))
            buy_vol += qty
            order_depth.sell_orders[ask_price] += qty
            if order_depth.sell_orders[ask_price] == 0:
                del order_depth.sell_orders[ask_price]

        # Only hit very high bids (sell side — patient)
        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price < fair_value + tw_sell:
                break
            bid_vol = order_depth.buy_orders[bid_price]
            qty = min(bid_vol, limit + position - sell_vol)
            if qty <= 0:
                break
            orders.append(Order(Product.ACO, bid_price, -qty))
            sell_vol += qty
            order_depth.buy_orders[bid_price] -= qty
            if order_depth.buy_orders[bid_price] == 0:
                del order_depth.buy_orders[bid_price]

        return buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  Standard symmetric taking for IPR (unchanged from V3)
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
    #  Clear position (unchanged)
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
            clearable = sum(v for p, v in order_depth.buy_orders.items() if p >= fair_ask)
            clearable = min(clearable, pos_after)
            send = min(limit + position - sell_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_ask, -send))
                sell_vol += send

        elif pos_after < 0:
            clearable = sum(abs(v) for p, v in order_depth.sell_orders.items() if p <= fair_bid)
            clearable = min(clearable, abs(pos_after))
            send = min(limit - position - buy_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_bid, send))
                buy_vol += send

        return buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  CHANGE A+B: ACO make orders — asymmetric edges + biased skew
    # ──────────────────────────────────────────────────────────────

    def make_orders_aco(
        self,
        order_depth: OrderDepth,
        fair_value: float,
        position: int,
        buy_vol: int,
        sell_vol: int,
    ):
        """
        Asymmetric quoting:
          Bid at fair - bid_edge (1)  → tight, gets filled often → build long
          Ask at fair + ask_edge (4)  → wide, only fills on spikes → sell premium

        Asymmetric skew:
          Long 0–60:   no adjustment  (hold the position — this is desired)
          Long 61–72:  ask -1, bid -1 (mild pressure to stop accumulating)
          Long 72+:    ask -2, bid -2 (stronger — avoid hard limit)
          Short at all: immediately skew back toward long
        """
        ap = self.params[Product.ACO]
        limit = self.LIMIT[Product.ACO]
        orders: List[Order] = []

        disregard = ap["disregard_edge"]
        join      = ap["join_edge"]

        asks_above = [p for p in order_depth.sell_orders if p > fair_value + disregard]
        bids_below  = [p for p in order_depth.buy_orders  if p < fair_value - disregard]

        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        # Base ask: wide (patient sell)
        if best_ask_above is not None:
            ask = best_ask_above if best_ask_above - fair_value <= join else best_ask_above - 1
            ask = max(ask, round(fair_value + ap["ask_edge"]))  # never quote ask below ask_edge
        else:
            ask = round(fair_value + ap["ask_edge"])

        # Base bid: tight (eager buy)
        if best_bid_below is not None:
            bid = best_bid_below if fair_value - best_bid_below <= join else best_bid_below + 1
            bid = min(bid, round(fair_value - ap["bid_edge"]))  # never quote bid above bid_edge
        else:
            bid = round(fair_value - ap["bid_edge"])

        # Asymmetric skew
        if position > 0:
            # Long — only skew defensively beyond 60
            skew = self._get_skew(position, ap["long_skew_tiers"])
            if skew > 0:
                ask -= skew
                bid -= skew
        elif position < 0:
            # Short — skew aggressively back toward long immediately
            skew = self._get_skew(position, ap["short_skew_tiers"])
            if skew > 0:
                bid += skew
                ask += skew

        buy_qty = limit - (position + buy_vol)
        if buy_qty > 0:
            orders.append(Order(Product.ACO, round(bid), buy_qty))

        sell_qty = limit + (position - sell_vol)
        if sell_qty > 0:
            orders.append(Order(Product.ACO, round(ask), -sell_qty))

        return orders, buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  Symmetric make orders for IPR (unchanged from V3)
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
        orders: List[Order] = []
        limit = self.LIMIT[product]

        asks_above = [p for p in order_depth.sell_orders if p > fair_value + disregard_edge]
        bids_below  = [p for p in order_depth.buy_orders  if p < fair_value - disregard_edge]

        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        ask = (best_ask_above if best_ask_above - fair_value <= join_edge else best_ask_above - 1) \
              if best_ask_above is not None else round(fair_value + default_edge)

        bid = (best_bid_below if fair_value - best_bid_below <= join_edge else best_bid_below + 1) \
              if best_bid_below is not None else round(fair_value - default_edge)

        skew = self._get_skew(position, skew_tiers)
        if skew > 0:
            if position > 0:
                ask -= skew
                bid -= skew
            else:
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
    #  Conversion arb (unchanged from V3)
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
        limit = self.LIMIT[product]

        transport = getattr(obs, "transportFees", 0)
        export_t  = getattr(obs, "exportTariff",  0)
        import_t  = getattr(obs, "importTariff",  0)
        auth_bid  = getattr(obs, "bidPrice",  0)
        auth_ask  = getattr(obs, "askPrice",  0)

        if od.buy_orders:
            market_bid = max(od.buy_orders.keys())
            import_cost = auth_ask + transport + import_t
            if market_bid - import_cost > 0:
                qty = min(limit + pos, od.buy_orders[market_bid])
                if qty > 0:
                    return int(qty)

        if od.sell_orders:
            market_ask = min(od.sell_orders.keys())
            export_revenue = auth_bid - transport - export_t
            if export_revenue - market_ask > 0:
                qty = min(limit - pos, -od.sell_orders[market_ask])
                if qty > 0:
                    return -int(qty)

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

        # ── ASH-COATED OSMIUM — biased MM ─────────────────────────
        if Product.ACO in state.order_depths:
            pos = state.position.get(Product.ACO, 0)
            od  = state.order_depths[Product.ACO]
            fv  = self.params[Product.ACO]["fair_value"]

            orders: List[Order] = []
            bv = sv = 0

            # Asymmetric take: aggressive buys, patient sells
            bv, sv = self.take_orders_aco(orders, od, fv, pos, bv, sv)

            # Clear only if we somehow went short (shouldn't happen often)
            bv, sv = self.clear_position_order(
                Product.ACO, fv, self.params[Product.ACO]["clear_width"],
                orders, od, pos, bv, sv,
            )

            # Asymmetric passive quotes
            make, bv, sv = self.make_orders_aco(od, fv, pos, bv, sv)
            orders += make
            result[Product.ACO] = orders

        # ── INTARIAN PEPPER ROOT — trending MM ────────────────────
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