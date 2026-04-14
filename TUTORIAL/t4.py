from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math



# ── EMERALDS ──────────────────────────────────────────────────────────────────
EMERALDS_FAIR       = 10_000
# ► DATA: mid_price mean = 10000.00, std = 0.72 across 20,000 rows.
#         The product is perfectly stable. Fair value never leaves 9996–10004.

EMERALDS_LIMIT      = 80
# ► DATA: Prosperity 4 tutorial sets position limit = 80 for both products.

EMERALDS_BOT_BID    = 9992
EMERALDS_BOT_ASK    = 10008
# ► DATA: bid_price_1 = 9992 in 98.4% of all rows.
#         ask_price_1 = 10008 in 98.4% of all rows.
#         The market-maker bot PERMANENTLY sits here. Spread = 16.

EMERALDS_OUR_BID    = 9993
EMERALDS_OUR_ASK    = 10007
# ► WHY:  We post 1 tick INSIDE the bot. Exchange uses price-time priority,
#         so any participant wanting to buy/sell will fill US first (10007 < 10008,
#         9993 > 9992). The bot becomes our backstop.
#         Edge per round-trip = 10007 - 9993 = 14 ticks.

EMERALDS_SKEW       = 0.3
# ► WHY:  If we accumulate a long position, we don't want to sit there
#         holding it forever. We shift BOTH our bid and ask downward by
#         0.3 × position, making our ask cheaper (easier to sell) and our
#         bid lower (less eager to buy more). This keeps us near flat.
#         Example: position = +40 → skew = -12 → we quote 9981 / 9995
#         instead of 9993 / 10007. We will sell sooner.


# ── TOMATOES ─────────────────────────────────────────────────────────────────
TOMATOES_LIMIT      = 80
# ► DATA: Same as EMERALDS — limit is 80.

TOMATOES_INSIDE     = 1
# ► WHY:  The market spread is 13–14 ticks wide. We post 1 tick inside
#         the best bid and ask, so our spread is 11–12. Anyone hitting the
#         market will fill us first. We earn ~5–6 ticks per side vs fair.
# ► DATA: Checked that bid1+1 never crosses ask1-1 in 20,000 rows. Safe.

TOMATOES_SKEW       = 0.2
# ► WHY:  Same inventory management logic as EMERALDS.
#         Day -1 drifted 50 points downward. Without skew, we kept buying
#         into the fall and hit +80 position → locked in losses.
#         Skew prevents this by making our ask cheaper as we go long.

# ► WHY NO MOVING AVERAGE FAIR VALUE FOR TOMATOES?
#   We tested rolling averages of window 5, 8, 12.
#   On Day -1 (50pt downtrend) the rolling avg ALWAYS lagged behind the
#   falling price → we kept buying "cheap" things that were still falling.
#   Result: position stuck at +80, large unrealised loss.
#   The current best bid/ask IS the market's best estimate of fair value
#   right now. We don't need to second-guess it — just make inside it.


# =============================================================================
#  HELPER: clamp a value between lo and hi
# =============================================================================

def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# =============================================================================
#  EMERALDS STRATEGY
# =============================================================================

def emeralds_orders(order_depth: OrderDepth, position: int) -> List[Order]:
    """
    Three-step logic every tick:

      STEP 1 — TAKE (free money)
        The bot occasionally tightens its spread to 8 (ask=10000 or bid=10000).
        ► DATA: happens ~163 times per day, vol ~8 each time.
        When ask < 10000, we can BUY below fair — instant profit.
        When bid > 10000, we can SELL above fair — instant profit.
        We grab as much as we can within our position limit.

      STEP 2 — CLEAR (risk management)
        After taking, if we're sitting on inventory at a position extreme,
        we try to exit at fair price if that level exists in the book.
        ► WHY: reduces overnight risk and frees up room for more trades.

      STEP 3 — MAKE (the main edge)
        Post bid=9993, ask=10007. These sit inside the bot's 9992/10008.
        We will get filled first when other participants arrive.
        Skew shifts both quotes by -0.3 × position to manage inventory.
    """
    orders: List[Order] = []
    buy_vol  = 0   # how much we've committed to buy this tick
    sell_vol = 0   # how much we've committed to sell this tick
    fair     = EMERALDS_FAIR
    limit    = EMERALDS_LIMIT

    # ── STEP 1: TAKE anything that crosses fair ───────────────────────────
    # Iterate through ALL ask levels (cheapest first)
    for ask_price in sorted(order_depth.sell_orders.keys()):
        if ask_price >= fair:
            break  # nothing cheaper than fair left — stop
        available = -order_depth.sell_orders[ask_price]   # sell_orders stored as negative
        can_buy   = limit - position - buy_vol             # how much room we have
        qty       = min(available, can_buy)
        if qty > 0:
            orders.append(Order("EMERALDS", ask_price, qty))
            buy_vol += qty

    # Iterate through ALL bid levels (highest first)
    for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
        if bid_price <= fair:
            break  # nothing above fair left — stop
        available = order_depth.buy_orders[bid_price]
        can_sell  = limit + position - sell_vol
        qty       = min(available, can_sell)
        if qty > 0:
            orders.append(Order("EMERALDS", bid_price, -qty))
            sell_vol += qty

    # ── STEP 2: CLEAR residual position at fair ───────────────────────────
    pos_after = position + buy_vol - sell_vol

    if pos_after > 0:
        # We are long — try to sell some at fair if a buyer exists there
        fair_as_int = int(fair)
        if fair_as_int in order_depth.buy_orders:
            available = order_depth.buy_orders[fair_as_int]
            can_sell  = limit + position - sell_vol
            qty       = min(available, pos_after, can_sell)
            if qty > 0:
                orders.append(Order("EMERALDS", fair_as_int, -qty))
                sell_vol += qty

    elif pos_after < 0:
        # We are short — try to buy some at fair if a seller exists there
        fair_as_int = int(fair)
        if fair_as_int in order_depth.sell_orders:
            available = -order_depth.sell_orders[fair_as_int]
            can_buy   = limit - position - buy_vol
            qty       = min(available, -pos_after, can_buy)
            if qty > 0:
                orders.append(Order("EMERALDS", fair_as_int, qty))
                buy_vol += qty

    # ── STEP 3: MAKE — post inside bot with inventory skew ────────────────
    # Skew example: position = +40 → skew_ticks = int(0.3 × 40) = 12
    #   → bid = 9993 - 12 = 9981  (we don't want to buy more)
    #   → ask = 10007 - 12 = 9995 (we really want to sell, so price it cheaper)
    skew_ticks = int(round(EMERALDS_SKEW * (position + buy_vol - sell_vol)))

    bid_price = EMERALDS_OUR_BID - skew_ticks
    ask_price = EMERALDS_OUR_ASK - skew_ticks

    # Hard safety: never bid at or above fair, never ask at or below fair
    bid_price = clamp(bid_price, 1,         int(fair) - 1)
    ask_price = clamp(ask_price, int(fair) + 1, 99999)

    buy_room  = limit - (position + buy_vol)
    sell_room = limit + (position - sell_vol)

    if buy_room > 0:
        orders.append(Order("EMERALDS", bid_price, buy_room))

    if sell_room > 0:
        orders.append(Order("EMERALDS", ask_price, -sell_room))

    return orders


# =============================================================================
#  TOMATOES STRATEGY
# =============================================================================

def tomatoes_orders(order_depth: OrderDepth, position: int):

    orders = []
    limit = 80

    if not order_depth.sell_orders or not order_depth.buy_orders:
        return orders

    best_ask = min(order_depth.sell_orders)
    best_bid = max(order_depth.buy_orders)

    ask_vol = -order_depth.sell_orders[best_ask]
    bid_vol = order_depth.buy_orders[best_bid]

    mid = (best_bid + best_ask) / 2

    # 🔥 VERY LIGHT TAKE (only obvious)
    if best_ask <= mid - 3:
        qty = min(ask_vol, limit - position)
        if qty > 0:
            orders.append(Order("TOMATOES", best_ask, qty))

    if best_bid >= mid + 3:
        qty = min(bid_vol, limit + position)
        if qty > 0:
            orders.append(Order("TOMATOES", best_bid, -qty))

    # 🔥 MAIN EDGE = AGGRESSIVE MAKING
    skew = int(0.1 * position)   # 🔥 REDUCED SKEW

    our_bid = best_bid + 1 - skew
    our_ask = best_ask - 1 - skew

    if our_bid >= our_ask:
        our_bid = int(mid) - 1
        our_ask = int(mid) + 1

    buy_room = limit - position
    sell_room = limit + position

    if buy_room > 0:
        orders.append(Order("TOMATOES", our_bid, buy_room))

    if sell_room > 0:
        orders.append(Order("TOMATOES", our_ask, -sell_room))

    return orders


# =============================================================================
#  TRADER CLASS  (entry point called by the Prosperity platform)
# =============================================================================

class Trader:
    """
    The platform calls trader.run(state) once per timestamp.
    state contains:
      - state.order_depths   : the live order book per product
      - state.position       : our current holdings per product
      - state.traderData     : string we can use to pass data across ticks
      - state.own_trades     : our trades this tick
      - state.market_trades  : all other trades this tick
    We return (result, conversions, traderData):
      - result       : dict of product → list of Orders
      - conversions  : int, for products that support conversion (none here)
      - traderData   : string to persist to next tick (we don't need it)
    """

    def run(self, state: TradingState):

        result: Dict[str, List[Order]] = {}

        # ── EMERALDS ──────────────────────────────────────────────────────
        if "EMERALDS" in state.order_depths:
            position = state.position.get("EMERALDS", 0)
            result["EMERALDS"] = emeralds_orders(
                state.order_depths["EMERALDS"],
                position
            )

        # ── TOMATOES ──────────────────────────────────────────────────────
        if "TOMATOES" in state.order_depths:
            position = state.position.get("TOMATOES", 0)
            result["TOMATOES"] = tomatoes_orders(
                state.order_depths["TOMATOES"],
                position
            )

        conversions = 0     # no conversion products in tutorial round
        trader_data = ""    # no rolling state needed — we use live book only

        return result, conversions, trader_data

