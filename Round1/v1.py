from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict
import jsonpickle

class Product:
    ACO = "ASH_COATED_OSMIUM"
    IPR = "INTARIAN_PEPPER_ROOT"


PARAMS = {
    Product.ACO: {
        # Fair value is rock-solid at 10 000 (σ ≈ 5 across all 3 days)
        "fair_value": 10_000,
        # Take any order that crosses fair value by ≥1 tick
        "take_width": 1,
        # Quote inside the ~16-tick spread; default edge = 3 each side
        "disregard_edge": 1,   # ignore MM quotes within 1 tick of fair (noise)
        "join_edge": 2,        # join an existing quote if it's within 2 ticks of fair
        "default_edge": 3,     # otherwise quote fair ± 3
        "clear_width": 0,      # flat clear at fair when position needs unwinding
        "soft_position_limit": 8,   # start skewing quotes when |pos| > 8
        "position_limit": 20,
    },
    Product.IPR: {
        # Fair value is a LINEAR TREND: FV(t) = base + 0.001 * timestamp
        # base = 10 000 + 1000 * (day + 2)
        #   day -2 → base = 10 000
        #   day -1 → base = 11 000
        #   day  0 → base = 12 000
        # This means price rises by exactly 1 point per 1 000-ms tick.
        # Observed deviation from this trend: σ ≈ 2.2, mean abs dev ≈ 1.1
        "base_day_minus2": 10_000,
        "trend_per_tick": 0.001,    # price rises 1 unit per 1 000 ticks
        # Market maker quotes sitting ~7 ticks either side of fair.
        # We quote inside them to get selected.
        "take_width": 1,            # take if crossing fair by ≥1
        "disregard_edge": 1,
        "join_edge": 2,
        "default_edge": 3,
        "clear_width": 0,
        # Adverse-volume threshold: volumes >15 are large MM orders, not retail
        "prevent_adverse": True,
        "adverse_volume": 15,
        "soft_position_limit": 8,
        "position_limit": 20,
    },
}


class Trader:
    """
    ═══════════════════════════════════════════════════════════════════
    STRATEGY OVERVIEW
    ═══════════════════════════════════════════════════════════════════

    ASH-COATED OSMIUM (ACO)  — Pure Market Maker
    ─────────────────────────────────────────────
    • Fair value is perfectly stable at 10 000 (σ ≈ 5 across 30 000 rows).
    • Market spread is ~16 ticks wide. We sit inside it at ±3 from fair.
    • Three actions every tick:
        1. TAKE  – if any resting order crosses fair by ≥1, fill it immediately.
        2. CLEAR – if we're carrying inventory, try to unwind at fair price.
        3. MAKE  – post a bid/ask inside the book, penny or join the best
                   competing quote, skewing toward reducing position when
                   |pos| > soft_limit.

    INTARIAN PEPPER ROOT (IPR)  — Trending Market Maker
    ─────────────────────────────────────────────────────
    • Fair value is a DETERMINISTIC LINEAR TREND:
          FV(t) = (10 000 + 1000*(day+2)) + 0.001*timestamp
      This fits all 3 days with σ ≈ 2.2 ticks – essentially perfect.
    • Because we know the slope exactly, we can quote a MOVING spread
      around the true fair value and harvest edge on both sides.
    • Adverse-volume filter: ignore MM quotes with volume >15 (they are
      large market-making bots that know fair value; crossing them is
      dangerous). Only take smaller orders that give us a true edge.
    • Same take → clear → make pipeline as ACO, just with a dynamic FV.

    POSITION MANAGEMENT
    ────────────────────
    • Hard limit: ±20 for both products.
    • Soft limit: ±8. Once breached, make_orders skews quotes by 1 tick
      toward reducing inventory (classic inventory-skew market making).
    • clear_orders tries to unwind against resting orders at fair price
      before we post new passive quotes, keeping turnover high.

    ═══════════════════════════════════════════════════════════════════
    """

    def __init__(self, params=None):
        self.params = params if params is not None else PARAMS
        self.LIMIT = {
            Product.ACO: self.params[Product.ACO]["position_limit"],
            Product.IPR: self.params[Product.IPR]["position_limit"],
        }

    # ──────────────────────────────────────────────────────────────
    #  Fair-value helpers
    # ──────────────────────────────────────────────────────────────

    def _ipr_fair_value(self, state: TradingState) -> float:
        """
        FV = base + 0.001 * timestamp
        base depends on the current day, which we infer from
        traderData (persisted across ticks inside a day).
        """
        p = self.params[Product.IPR]
        # state.timestamp is the tick counter within the current day (0 … 999 900).
        # The day number is embedded in the state; the simplest way to get it
        # is from state.timestamp and the previous price trajectory.
        # Since the base is known perfectly from the data:
        #   day -2 → base 10 000  (game day index 0 in the engine)
        #   day -1 → base 11 000
        #   day  0 → base 12 000
        # We detect the day by checking which base fits the current mid-price.
        ts = state.timestamp

        # Try to infer base from the current order book mid-price
        od = state.order_depths.get(Product.IPR)
        observed_mid = None
        if od and od.buy_orders and od.sell_orders:
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            observed_mid = (best_bid + best_ask) / 2

        if observed_mid is not None:
            # The predicted mid for each possible base:
            # FV = base + 0.001 * ts  →  base = observed_mid - 0.001*ts
            inferred_base = observed_mid - p["trend_per_tick"] * ts
            # Round to nearest 1000 to snap to the known grid
            snapped_base = round(inferred_base / 1000) * 1000
            return snapped_base + p["trend_per_tick"] * ts

        # Fallback: use base 12 000 (latest known day)
        return 12_000 + p["trend_per_tick"] * ts

    # ──────────────────────────────────────────────────────────────
    #  Core order logic (shared between products)
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
        """
        Aggress resting orders that cross our fair value by at least take_width.
        Adverse-volume filter: skip if the resting size signals a well-informed MM.
        """
        limit = self.LIMIT[product]

        # Check best ask – buy if ask ≤ fair - take_width
        if order_depth.sell_orders:
            best_ask = min(order_depth.sell_orders)
            best_ask_vol = -order_depth.sell_orders[best_ask]  # positive quantity
            if not prevent_adverse or best_ask_vol <= adverse_volume:
                if best_ask <= fair_value - take_width:
                    qty = min(best_ask_vol, limit - position - buy_vol)
                    if qty > 0:
                        orders.append(Order(product, best_ask, qty))
                        buy_vol += qty
                        order_depth.sell_orders[best_ask] += qty
                        if order_depth.sell_orders[best_ask] == 0:
                            del order_depth.sell_orders[best_ask]

        # Check best bid – sell if bid ≥ fair + take_width
        if order_depth.buy_orders:
            best_bid = max(order_depth.buy_orders)
            best_bid_vol = order_depth.buy_orders[best_bid]
            if not prevent_adverse or best_bid_vol <= adverse_volume:
                if best_bid >= fair_value + take_width:
                    qty = min(best_bid_vol, limit + position - sell_vol)
                    if qty > 0:
                        orders.append(Order(product, best_bid, -qty))
                        sell_vol += qty
                        order_depth.buy_orders[best_bid] -= qty
                        if order_depth.buy_orders[best_bid] == 0:
                            del order_depth.buy_orders[best_bid]

        return buy_vol, sell_vol

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
        """
        If we're carrying inventory after taking, try to unwind it by joining
        resting orders at (or slightly beyond) fair value.
        This keeps our book balanced and reduces overnight risk.
        """
        limit = self.LIMIT[product]
        pos_after = position + buy_vol - sell_vol

        fair_bid = round(fair_value - clear_width)
        fair_ask = round(fair_value + clear_width)

        if pos_after > 0:
            # We're long – look for bids at ≥ fair_ask to sell into
            clearable = sum(
                v for p, v in order_depth.buy_orders.items() if p >= fair_ask
            )
            clearable = min(clearable, pos_after)
            send = min(limit + position - sell_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_ask, -send))
                sell_vol += send

        elif pos_after < 0:
            # We're short – look for asks at ≤ fair_bid to buy
            clearable = sum(
                abs(v) for p, v in order_depth.sell_orders.items() if p <= fair_bid
            )
            clearable = min(clearable, abs(pos_after))
            send = min(limit - position - buy_vol, clearable)
            if send > 0:
                orders.append(Order(product, fair_bid, send))
                buy_vol += send

        return buy_vol, sell_vol

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
        """
        Post passive bid/ask inside the current spread.

        Quote logic:
          - Ignore competing quotes within disregard_edge of fair (treat as noise).
          - If a competing quote is within join_edge → join (post same price).
          - Otherwise → penny (undercut/overcut by 1 tick).
          - If no competing quote → post at fair ± default_edge.

        Inventory skew:
          - |position| > soft_limit  → shift ask down by 1 (push sales) or
                                        shift bid up by 1 (push buys).
          This ensures we don't accumulate runaway inventory.
        """
        orders: List[Order] = []
        limit = self.LIMIT[product]

        # Best competing quotes outside the disregard band
        asks_above = [p for p in order_depth.sell_orders if p > fair_value + disregard_edge]
        bids_below  = [p for p in order_depth.buy_orders  if p < fair_value - disregard_edge]

        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        # Determine our ask
        if best_ask_above is not None:
            if best_ask_above - fair_value <= join_edge:
                ask = best_ask_above          # join
            else:
                ask = best_ask_above - 1      # penny
        else:
            ask = round(fair_value + default_edge)

        # Determine our bid
        if best_bid_below is not None:
            if fair_value - best_bid_below <= join_edge:
                bid = best_bid_below          # join
            else:
                bid = best_bid_below + 1      # penny
        else:
            bid = round(fair_value - default_edge)

        # Inventory skew: nudge quotes to encourage reversion
        if position > soft_position_limit:
            ask -= 1   # sell cheaper to offload longs
            bid -= 1   # be less eager to buy more
        elif position < -soft_position_limit:
            bid += 1   # buy higher to cover shorts
            ask += 1   # be less eager to sell more

        # Post bid (fill remaining buy capacity)
        buy_qty = limit - (position + buy_vol)
        if buy_qty > 0:
            orders.append(Order(product, round(bid), buy_qty))

        # Post ask (fill remaining sell capacity)
        sell_qty = limit + (position - sell_vol)
        if sell_qty > 0:
            orders.append(Order(product, round(ask), -sell_qty))

        return orders, buy_vol, sell_vol

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
            ap = self.params[Product.ACO]
            pos = state.position.get(Product.ACO, 0)
            od  = state.order_depths[Product.ACO]
            fv  = ap["fair_value"]

            orders: List[Order] = []
            bv = sv = 0

            # 1. Take mispriced orders
            bv, sv = self.take_best_orders(
                Product.ACO, fv, ap["take_width"],
                orders, od, pos, bv, sv
            )

            # 2. Unwind inventory at fair
            bv, sv = self.clear_position_order(
                Product.ACO, fv, ap["clear_width"],
                orders, od, pos, bv, sv
            )

            # 3. Passive market-making quotes
            make, bv, sv = self.make_orders(
                Product.ACO, od, fv, pos, bv, sv,
                ap["disregard_edge"], ap["join_edge"], ap["default_edge"],
                ap["soft_position_limit"],
            )
            orders += make
            result[Product.ACO] = orders

        # ── INTARIAN PEPPER ROOT ───────────────────────────────────
        if Product.IPR in state.order_depths:
            pp = self.params[Product.IPR]
            pos = state.position.get(Product.IPR, 0)
            od  = state.order_depths[Product.IPR]
            fv  = self._ipr_fair_value(state)

            orders: List[Order] = []
            bv = sv = 0

            # 1. Take – with adverse-volume filter
            bv, sv = self.take_best_orders(
                Product.IPR, fv, pp["take_width"],
                orders, od, pos, bv, sv,
                pp["prevent_adverse"], pp["adverse_volume"],
            )

            # 2. Unwind inventory at fair
            bv, sv = self.clear_position_order(
                Product.IPR, fv, pp["clear_width"],
                orders, od, pos, bv, sv
            )

            # 3. Passive market-making around moving fair value
            make, bv, sv = self.make_orders(
                Product.IPR, od, fv, pos, bv, sv,
                pp["disregard_edge"], pp["join_edge"], pp["default_edge"],
                pp["soft_position_limit"],
            )
            orders += make
            result[Product.IPR] = orders

        conversions = 1
        trader_data = jsonpickle.encode(trader_obj)
        return result, conversions, trader_data