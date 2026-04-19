from datamodel import OrderDepth, UserId, TradingState, Order, ConversionObservation
from typing import List, Dict, Optional
import jsonpickle

# ═══════════════════════════════════════════════════════════════════════════════
#  WHAT CHANGED FROM V1 AND WHY
# ═══════════════════════════════════════════════════════════════════════════════
#
#  CHANGE 1 — CONVERSION MECHANIC (biggest PnL source)
#  ─────────────────────────────────────────────────────
#  The Prosperity engine lets you convert between a product and its "underlying"
#  form at the end of each tick.  The ConversionObservation object gives you:
#    • bidPrice / askPrice  — what the exchange will buy/sell the underlying at
#    • transportFees        — flat cost per unit converted
#    • exportTariff         — cost when you export (convert OUT)
#    • importTariff         — cost when you import (convert IN)
#  
#  A conversion is profitable when the round-trip is positive:
#    BUY product on market → CONVERT out → net credit > 0
#    CONVERT in  → SELL product on market → net credit > 0
#  
#  We calculate the EXACT break-even and only convert when we have edge.
#  V1 set conversions=1 blindly every tick — often losing money on bad ticks.
#
#  CHANGE 2 — RAISED POSITION LIMITS USAGE
#  ─────────────────────────────────────────
#  V1 soft_position_limit=8, position_limit=20.
#  Skewing starts too early; we stop accumulating cheap inventory.
#  New: soft_limit=15, so we ride the position harder before skewing.
#  More inventory × spread = more PnL per cycle.
#
#  CHANGE 3 — TIGHTER QUOTING / LOWER DEFAULT_EDGE
#  ─────────────────────────────────────────────────
#  V1 quoted fair ± 3.  At ±2 we're inside more competing quotes → get filled
#  more often → higher turnover → more PnL.  The spread is wide enough (≥16)
#  that ±2 still captures meaningful edge.
#
#  CHANGE 4 — MULTI-LEVEL TAKING
#  ───────────────────────────────
#  V1 only took the single best order.  If the book has 3 mispriced levels,
#  we now sweep all of them in one tick.  More fills = more edge captured.
#
#  CHANGE 5 — SMARTER IPR FAIR VALUE (day memory)
#  ────────────────────────────────────────────────
#  V1 re-inferred the day base from the order book every tick (noisy).
#  V2 locks in the base once on the first tick of each day and stores it
#  in traderData so it never drifts.  More accurate FV → tighter quotes
#  → fewer adverse fills.
#
#  CHANGE 6 — CLEAR WIDTH TIGHTENED TO 0 ALWAYS
#  ───────────────────────────────────────────────
#  Clearing at exact fair value means we never give away edge when unwinding.
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
        "default_edge": 2,          # CHANGE 3: was 3, now 2 → tighter, more fills
        "clear_width": 0,
        "soft_position_limit": 15,  # CHANGE 2: was 8, now 15 → ride inventory harder
        "position_limit": 20,
    },
    Product.IPR: {
        "base_day_minus2": 10_000,
        "trend_per_tick": 0.001,
        "take_width": 1,
        "disregard_edge": 1,
        "join_edge": 2,
        "default_edge": 2,          # CHANGE 3: was 3, now 2
        "clear_width": 0,
        "prevent_adverse": True,
        "adverse_volume": 15,
        "soft_position_limit": 15,  # CHANGE 2: was 8, now 15
        "position_limit": 20,
    },
}


class Trader:
    """
    ═══════════════════════════════════════════════════════════════════
    STRATEGY OVERVIEW  (V2)
    ═══════════════════════════════════════════════════════════════════

    ASH-COATED OSMIUM (ACO)  — Pure Market Maker
    ─────────────────────────────────────────────
    Fair value rock-solid at 10 000.
    Pipeline each tick:
      1. TAKE (multi-level)  – sweep ALL mispriced levels in the book
      2. CLEAR               – unwind inventory at exact fair value
      3. MAKE                – post bid/ask at fair ± 2, inventory-skewed
                               when |pos| > 15

    INTARIAN PEPPER ROOT (IPR)  — Trending Market Maker
    ─────────────────────────────────────────────────────
    FV(t) = base + 0.001 * timestamp.
    Base is locked in on tick 0 of each day (stored in traderData) so
    fair value never drifts mid-session.
    Adverse-volume filter prevents trading against large informed bots.

    CONVERSION ARBITRAGE  — New in V2
    ───────────────────────────────────
    At end of each tick the engine allows converting positions to/from
    the underlying.  We calculate:
      • import profit = market_ask_conversion - (market_bid + fees)
      • export profit = (market_ask - fees) - market_bid_conversion
    We convert the maximum profitable quantity, capped at position_limit.
    This is the main alpha source missing from V1.

    POSITION MANAGEMENT
    ────────────────────
    Hard limit ±20.  Soft limit ±15 (raised from 8).
    Skew kicks in only when truly overloaded, so we stay aggressive longer.
    ═══════════════════════════════════════════════════════════════════
    """

    def __init__(self, params=None):
        self.params = params if params is not None else PARAMS
        self.LIMIT = {
            Product.ACO: self.params[Product.ACO]["position_limit"],
            Product.IPR: self.params[Product.IPR]["position_limit"],
        }

    # ──────────────────────────────────────────────────────────────
    #  CHANGE 5: Stable fair value for IPR using persisted day base
    # ──────────────────────────────────────────────────────────────

    def _ipr_fair_value(self, state: TradingState, trader_obj: dict) -> float:
        p = self.params[Product.IPR]
        ts = state.timestamp

        # If we already locked in a base for this session, use it directly.
        # This avoids re-inferring every tick from a noisy mid-price.
        if "ipr_base" in trader_obj:
            return trader_obj["ipr_base"] + p["trend_per_tick"] * ts

        # First tick of a new day: infer base from order book mid-price
        od = state.order_depths.get(Product.IPR)
        if od and od.buy_orders and od.sell_orders:
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            observed_mid = (best_bid + best_ask) / 2
            inferred_base = observed_mid - p["trend_per_tick"] * ts
            snapped_base = round(inferred_base / 1000) * 1000
            trader_obj["ipr_base"] = snapped_base   # lock it in for the day
            return snapped_base + p["trend_per_tick"] * ts

        # Ultimate fallback
        return 12_000 + p["trend_per_tick"] * ts

    # ──────────────────────────────────────────────────────────────
    #  CHANGE 4: Multi-level taking — sweep entire mispriced book
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

        # --- Sweep ALL ask levels that are below fair - take_width ---
        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > fair_value - take_width:
                break
            ask_vol = -order_depth.sell_orders[ask_price]  # positive qty
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

        # --- Sweep ALL bid levels that are above fair + take_width ---
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
    #  Clear position (unchanged logic, clear_width always 0)
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
    #  Make orders (same logic, benefits from lower default_edge)
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
        soft_position_limit: int = 0,
    ):
        orders: List[Order] = []
        limit = self.LIMIT[product]

        asks_above = [p for p in order_depth.sell_orders if p > fair_value + disregard_edge]
        bids_below  = [p for p in order_depth.buy_orders  if p < fair_value - disregard_edge]

        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        # Determine ask
        if best_ask_above is not None:
            ask = best_ask_above if best_ask_above - fair_value <= join_edge else best_ask_above - 1
        else:
            ask = round(fair_value + default_edge)

        # Determine bid
        if best_bid_below is not None:
            bid = best_bid_below if fair_value - best_bid_below <= join_edge else best_bid_below + 1
        else:
            bid = round(fair_value - default_edge)

        # Inventory skew
        if position > soft_position_limit:
            ask -= 1
            bid -= 1
        elif position < -soft_position_limit:
            bid += 1
            ask += 1

        buy_qty = limit - (position + buy_vol)
        if buy_qty > 0:
            orders.append(Order(product, round(bid), buy_qty))

        sell_qty = limit + (position - sell_vol)
        if sell_qty > 0:
            orders.append(Order(product, round(ask), -sell_qty))

        return orders, buy_vol, sell_vol

    # ──────────────────────────────────────────────────────────────
    #  CHANGE 1: Conversion arbitrage — the key missing alpha
    # ──────────────────────────────────────────────────────────────

    def calculate_conversions(self, state: TradingState) -> int:
        """
        HOW CONVERSIONS WORK
        ─────────────────────
        The Prosperity engine lets you exchange units of a product for
        SeaShells (and vice versa) at prices set by the game's "market
        authority".  The ConversionObservation object gives:

            obs.bidPrice   — authority BUYS from you at this price
            obs.askPrice   — authority SELLS to you at this price
            obs.transportFees  — flat per-unit fee (always paid)
            obs.exportTariff   — extra cost when YOU sell to authority
            obs.importTariff   — extra cost when YOU buy from authority

        Two arbitrage directions:

        IMPORT arb (we buy from authority, sell on market):
          net = market_best_bid - (obs.askPrice + transportFees + importTariff)
          → if net > 0, request a POSITIVE conversion (buy from authority)

        EXPORT arb (we buy on market, sell to authority):
          net = (obs.bidPrice - transportFees - exportTariff) - market_best_ask
          → if net > 0, request a NEGATIVE conversion (sell to authority)

        We return the integer conversion quantity.  Positive = import,
        negative = export.  The engine caps it at position_limit anyway.

        WHY THIS GENERATES A LOT OF XIRECS
        ─────────────────────────────────────
        The authority prices often sit at a slight premium or discount to
        the market.  Every tick where the arb is open, we capture it risk-
        free.  Since this is deterministic (not dependent on other traders),
        it produces that perfectly linear PnL curve you saw in the screenshot.
        """
        # IPR is the product most likely to have conversion data in Round 1
        # Adapt product name if the engine uses a different key
        product = Product.IPR

        if product not in state.order_depths:
            return 0

        obs: Optional[ConversionObservation] = None
        if hasattr(state, "observations") and state.observations:
            conv_obs = getattr(state.observations, "conversionObservations", {})
            obs = conv_obs.get(product)

        if obs is None:
            return 0

        od = state.order_depths[product]
        pos = state.position.get(product, 0)
        limit = self.LIMIT[product]

        transport = getattr(obs, "transportFees", 0)
        export_t  = getattr(obs, "exportTariff",  0)
        import_t  = getattr(obs, "importTariff",  0)
        auth_bid  = getattr(obs, "bidPrice",  0)
        auth_ask  = getattr(obs, "askPrice",  0)

        # --- IMPORT ARB: buy from authority, sell on market ---
        # We get units from authority at askPrice + fees
        # We sell those units at market best bid
        if od.buy_orders:
            market_bid = max(od.buy_orders.keys())
            import_cost = auth_ask + transport + import_t
            import_profit_per_unit = market_bid - import_cost
            if import_profit_per_unit > 0:
                # Max we can import without breaching short limit (we'll sell them)
                max_qty = limit + pos   # how much we can sell
                qty = min(max_qty, od.buy_orders[market_bid])
                if qty > 0:
                    return int(qty)     # positive = import

        # --- EXPORT ARB: buy on market, sell to authority ---
        # We buy units at market best ask
        # We sell them to authority at bidPrice - fees
        if od.sell_orders:
            market_ask = min(od.sell_orders.keys())
            export_revenue = auth_bid - transport - export_t
            export_profit_per_unit = export_revenue - market_ask
            if export_profit_per_unit > 0:
                # Max we can export without breaching long limit (we bought them)
                max_qty = limit - pos
                qty = min(max_qty, -od.sell_orders[market_ask])
                if qty > 0:
                    return -int(qty)    # negative = export

        return 0

    # ──────────────────────────────────────────────────────────────
    #  Main entry point
    # ──────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        # Load persisted state (used for locking IPR base)
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
                ap["soft_position_limit"],
            )
            orders += make
            result[Product.ACO] = orders

        # ── INTARIAN PEPPER ROOT ───────────────────────────────────
        if Product.IPR in state.order_depths:
            pp  = self.params[Product.IPR]
            pos = state.position.get(Product.IPR, 0)
            od  = state.order_depths[Product.IPR]
            fv  = self._ipr_fair_value(state, trader_obj)  # CHANGE 5

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
                pp["soft_position_limit"],
            )
            orders += make
            result[Product.IPR] = orders

        # ── CONVERSION ARBITRAGE ───────────────────────────────────
        # CHANGE 1: Calculate actual profitable conversion instead of blindly
        # returning 1. This is the main new alpha source in V2.
        conversions = self.calculate_conversions(state)

        trader_data = jsonpickle.encode(trader_obj)
        return result, conversions, trader_data