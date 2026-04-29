#17k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Tuple
import json

# ══════════════════════════════════════════════════════════════════════
#  IMC PROSPERITY 4 — ROUND 5
#
#  HONEST ASSESSMENT OF WHAT WE KNOW vs DON'T KNOW:
#
#  KNOW (from 3 days of data, stable structure):
#    - Day always runs ts 0 → 999900 (100 ticks per 100-unit step)
#    - 6 products trended UP every single day (3/3): we long them
#    - 6 products trended DOWN every single day (3/3): we short them
#    - The trend is embedded by IMC in the "story" of the round
#
#  DON'T KNOW:
#    - Whether the trend magnitude continues, accelerates, or reverses
#    - Which direction is better: enter at open or wait
#
#  STRATEGY:
#    - Use a ROLLING VWAP as adaptive fair-value estimate
#    - Enter LONG when price > VWAP (momentum confirmed, adaptive to any price level)
#    - Enter SHORT when price < VWAP
#    - Exit at ts >= 999500 by hitting best bid/ask
#    - If VWAP signal never fires: still enter at ts=500000 (halfway) as fallback
#      for the strongly trending products — better to catch half the move than none
#
#  WHY THIS GENERALISES:
#    - VWAP threshold adapts to whatever price level the product is at on day 5+
#    - If trend continues: we profit. If it reverses: we still enter only after
#      momentum is confirmed in the NEW direction (for mixed products)
#    - Hardcoded list = the 3/3 consistent ones only. If they start losing,
#      we lose smaller (we enter AFTER confirming intraday direction).
# ══════════════════════════════════════════════════════════════════════

POSITION_LIMIT = 10
VWAP_WINDOW    = 200          # rolling window (ticks) for VWAP calculation
FALLBACK_TS    = 500_000      # enter here if VWAP never triggers (halfway point)
EXIT_TS        = 999_500      # start liquidating at this timestamp

# 3/3 consistent long products across all observed days
LONG_PRODUCTS = {
    "GALAXY_SOUNDS_BLACK_HOLES",
    "OXYGEN_SHAKE_GARLIC",
    "PANEL_2X4",
    "UV_VISOR_RED",
    "SNACKPACK_STRAWBERRY",
    "SLEEP_POD_LAMB_WOOL",
}

# 3/3 consistent short products across all observed days
SHORT_PRODUCTS = {
    "MICROCHIP_OVAL",
    "PEBBLES_XS",
    "UV_VISOR_AMBER",
    "SNACKPACK_CHOCOLATE",
    "SNACKPACK_PISTACHIO",
    "PEBBLES_S",
}

ALL_ACTIVE = LONG_PRODUCTS | SHORT_PRODUCTS


class Trader:

    def __init__(self):
        # Per-product rolling price buffer for VWAP
        self.price_buf:   Dict[str, list] = {}
        # Whether we've taken our position today
        self.entered:     Dict[str, bool] = {}
        # day-open mid price (for fallback comparison)
        self.open_mid:    Dict[str, float] = {}

    # ── State serialisation ────────────────────────────────────────────

    def _load(self, s: str):
        if not s:
            return
        try:
            d = json.loads(s)
            self.price_buf = d.get("buf", {})
            self.entered   = d.get("entered", {})
            self.open_mid  = d.get("open_mid", {})
        except Exception:
            pass

    def _save(self) -> str:
        # Only keep last VWAP_WINDOW prices to cap state size
        trimmed_buf = {k: v[-VWAP_WINDOW:] for k, v in self.price_buf.items()}
        return json.dumps({
            "buf":      trimmed_buf,
            "entered":  self.entered,
            "open_mid": self.open_mid,
        })

    # ── Order book helpers ─────────────────────────────────────────────

    @staticmethod
    def _mid(od: OrderDepth):
        b = max(od.buy_orders)  if od.buy_orders  else None
        a = min(od.sell_orders) if od.sell_orders else None
        if b and a:
            return (b + a) / 2
        return b or a

    # ── Main loop ──────────────────────────────────────────────────────

    def run(self, state: TradingState):
        self._load(state.traderData)

        result:      Dict[str, List[Order]] = {}
        ts = state.timestamp

        for product in ALL_ACTIVE:
            od = state.order_depths.get(product)
            if od is None:
                continue

            pos = state.position.get(product, 0)
            mid = self._mid(od)
            if mid is None:
                continue

            orders: List[Order] = []

            # ── Reset state at start of each day ──────────────────
            if ts == 0:
                self.price_buf[product] = []
                self.entered[product]   = False
                self.open_mid[product]  = mid

            # Update rolling price buffer
            buf = self.price_buf.setdefault(product, [])
            buf.append(mid)
            if len(buf) > VWAP_WINDOW:
                buf.pop(0)

            vwap = sum(buf) / len(buf)
            open_mid = self.open_mid.get(product, mid)
            is_long  = product in LONG_PRODUCTS

            # ── EXIT PHASE ─────────────────────────────────────────
            if ts >= EXIT_TS:
                if is_long and pos > 0:
                    if od.buy_orders:
                        orders.append(Order(product, max(od.buy_orders), -pos))
                elif not is_long and pos < 0:
                    if od.sell_orders:
                        orders.append(Order(product, min(od.sell_orders), -pos))

            # ── ENTRY PHASE ────────────────────────────────────────
            elif not self.entered.get(product, False):

                # VWAP signal: price crossed above VWAP for longs,
                #              price crossed below VWAP for shorts
                vwap_long_signal  = is_long     and mid > vwap and mid > open_mid
                vwap_short_signal = (not is_long) and mid < vwap and mid < open_mid

                # Fallback: halfway through the day, just enter regardless
                # (ensures we never miss the whole move on a slow-starting day)
                fallback = (ts >= FALLBACK_TS)

                if vwap_long_signal or (is_long and fallback):
                    if od.sell_orders:
                        best_ask  = min(od.sell_orders)
                        available = abs(od.sell_orders[best_ask])
                        qty       = min(POSITION_LIMIT - pos, available)
                        if qty > 0:
                            orders.append(Order(product, best_ask, qty))
                            if pos + qty >= POSITION_LIMIT:
                                self.entered[product] = True

                elif vwap_short_signal or (not is_long and fallback):
                    if od.buy_orders:
                        best_bid  = max(od.buy_orders)
                        available = abs(od.buy_orders[best_bid])
                        qty       = min(POSITION_LIMIT + pos, available)
                        if qty > 0:
                            orders.append(Order(product, best_bid, -qty))
                            if abs(pos) + qty >= POSITION_LIMIT:
                                self.entered[product] = True

            if orders:
                result[product] = orders

        return result, 0, self._save()