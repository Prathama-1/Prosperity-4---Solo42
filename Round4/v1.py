#700
"""
HYDROGEL_PACK — Round 4 Strategy (FIXED)
==========================================

ROOT CAUSE OF ZERO PNL:
  The original code posted at ask_price_1 and bid_price_1 — exactly matching the
  existing order book. In IMC Prosperity, you must BEAT the existing book price to 
  get filled. Orders at the same price as existing orders sit behind them in the queue.

WHAT THE DATA REVEALS:
  The prices CSV captures a MIXED state — it shows the book AFTER all bot orders 
  are posted but with the market-trade prices coinciding with that book's best bid/ask.
  The key facts:
    • ask_volume_1 and bid_volume_1 are ~12-14 units → those are OTHER bots' limit orders
    • Mark 38 hits the best ask/bid aggressively with ~4-6 units per trade
    • The volume doesn't cleanly decrease → other bots refresh their orders each tick
  
  Therefore:
    • The book at each tick is POPULATED by other bots (including Mark 14)
    • To get fills, WE must post BETTER prices than those bots
    • Post SELL at (ask_price_1 - 1) → we become the new best ask → M38 hits us
    • Post BUY  at (bid_price_1 + 1) → we become the new best bid → M38 hits us

EXPECTED PNL WITH THIS FIX:
  Sell at ask-1, buy at bid+1 → captures (ask-1) - (bid+1) = spread - 2 per round trip
  Backtest: ~21,014  (vs theoretical max of 25,110 if we were the exact book)
  Still ~84% of theoretical max

POSITION MANAGEMENT:
  Position never exceeds ±139 historically. The skew guard at ±150 is a safety net.
"""

from datamodel import (
    OrderDepth, TradingState, Order
)
from typing import List, Dict


class Trader:

    POSITION_LIMIT  = 200
    POST_QTY        = 200   # post full limit on each side
    SKEW_THRESHOLD  = 150   # if |pos| > this, reduce one side

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        result["HYDROGEL_PACK"] = self._trade_hydrogel(state)
        return result, 0, ""

    def _trade_hydrogel(self, state: TradingState) -> List[Order]:
        """
        Post at ask_price_1 - 1  (SELL) and  bid_price_1 + 1  (BUY).

        Why undercut by 1?
          - The existing book has ~12-14 units posted by other bots.
          - To get filled FIRST by Mark 38 (who is an aggressive price-taker),
            we must be the best price in the book.
          - Undercutting by 1 makes us the best ask → Mark 38 fills us.
          - Similarly, posting 1 above the best bid makes us the best bid.

        We lose 1 tick of edge per side (2 total per round-trip) vs the theoretical
        max, but we actually GET FILLED instead of sitting behind the queue.

        Net spread captured per round-trip: (ask - 1) - (bid + 1) = spread - 2 ≈ 13.7
        """
        symbol = "HYDROGEL_PACK"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth: OrderDepth = state.order_depths[symbol]
        pos: int = state.position.get(symbol, 0)
        limit = self.POSITION_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())

        # Sanity: spread must leave room after undercutting
        if best_ask - best_bid <= 2:
            return orders

        # Our posted prices: one inside the existing spread
        our_ask: int = best_ask - 1   # better than book → fills first
        our_bid: int = best_bid + 1   # better than book → fills first

        # Headroom
        sell_capacity = limit + pos   # how many more units we can short
        buy_capacity  = limit - pos   # how many more units we can buy

        # Position skew: if very long, back off buying; if very short, back off selling
        if pos > self.SKEW_THRESHOLD:
            sell_qty = min(self.POST_QTY, sell_capacity)
            buy_qty  = min(self.POST_QTY // 4, buy_capacity)
        elif pos < -self.SKEW_THRESHOLD:
            buy_qty  = min(self.POST_QTY, buy_capacity)
            sell_qty = min(self.POST_QTY // 4, sell_capacity)
        else:
            sell_qty = min(self.POST_QTY, sell_capacity)
            buy_qty  = min(self.POST_QTY, buy_capacity)

        if sell_qty > 0:
            orders.append(Order(symbol, our_ask, -sell_qty))

        if buy_qty > 0:
            orders.append(Order(symbol, our_bid, buy_qty))

        return orders


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pandas as pd

    DATA_DIR = "./"

    p1 = pd.read_csv(f"{DATA_DIR}prices_round_4_day_1.csv", sep=";")
    p2 = pd.read_csv(f"{DATA_DIR}prices_round_4_day_2.csv", sep=";")
    p3 = pd.read_csv(f"{DATA_DIR}prices_round_4_day_3.csv", sep=";")
    t1 = pd.read_csv(f"{DATA_DIR}trades_round_4_day_1.csv", sep=";")
    t2 = pd.read_csv(f"{DATA_DIR}trades_round_4_day_2.csv", sep=";")
    t3 = pd.read_csv(f"{DATA_DIR}trades_round_4_day_3.csv", sep=";")

    t1["day"] = 1; t2["day"] = 2; t3["day"] = 3
    prices = pd.concat([p1, p2, p3], ignore_index=True)
    trades = pd.concat([t1, t2, t3], ignore_index=True)

    hgel_p = (prices[prices["product"] == "HYDROGEL_PACK"]
              .sort_values(["day", "timestamp"]).reset_index(drop=True))
    hgel_t = trades[trades["symbol"] == "HYDROGEL_PACK"].copy()

    POS_LIMIT    = 200
    SKEW_THRESH  = 150
    POST_QTY     = 200

    position = 0
    cash     = 0.0
    fills_sell = 0
    fills_buy  = 0
    missed     = 0

    for _, row in hgel_p.iterrows():
        bid = row["bid_price_1"]
        ask = row["ask_price_1"]
        mid = row["mid_price"]
        day = row["day"]
        ts  = row["timestamp"]

        our_ask = ask - 1
        our_bid = bid + 1

        sell_capacity = POS_LIMIT + position
        buy_capacity  = POS_LIMIT - position

        if position > SKEW_THRESH:
            sell_qty = min(POST_QTY, sell_capacity)
            buy_qty  = min(POST_QTY // 4, buy_capacity)
        elif position < -SKEW_THRESH:
            buy_qty  = min(POST_QTY, buy_capacity)
            sell_qty = min(POST_QTY // 4, sell_capacity)
        else:
            sell_qty = min(POST_QTY, sell_capacity)
            buy_qty  = min(POST_QTY, buy_capacity)

        ts_trades = hgel_t[(hgel_t["day"] == day) & (hgel_t["timestamp"] == ts)]
        for _, tr in ts_trades.iterrows():
            qty    = tr["quantity"]
            buyer  = tr["buyer"]
            seller = tr["seller"]

            # Mark 38 buys aggressively → hits our SELL at ask-1
            if buyer == "Mark 38":
                fill = min(qty, sell_qty)
                if fill > 0:
                    cash     += fill * our_ask
                    position -= fill
                    fills_sell += fill
                else:
                    missed += qty

            # Mark 38 sells aggressively → hits our BUY at bid+1
            if seller == "Mark 38":
                fill = min(qty, buy_qty)
                if fill > 0:
                    cash     -= fill * our_bid
                    position += fill
                    fills_buy += fill
                else:
                    missed += qty

    final_mid = hgel_p["mid_price"].iloc[-1]
    final_pnl = cash + position * final_mid
    avg_sell = (cash + sum(
        hgel_t[(hgel_t["buyer"] == "Mark 38")].iloc[:fills_sell]["price"].values * 0
    )) if fills_sell > 0 else 0

    print("=" * 55)
    print("  HYDROGEL BACKTEST RESULTS (FIXED)")
    print("=" * 55)
    print(f"  Final PnL          : {final_pnl:>12,.0f}")
    print(f"  Theoretical max    :      ~25,110")
    print(f"  Capture %          : {final_pnl/25110*100:>11.1f}%")
    print(f"  Final position     : {position:>12}")
    print(f"  Units sold to M38  : {fills_sell:>12,}")
    print(f"  Units bought from M38: {fills_buy:>10,}")
    print(f"  Missed (pos limit) : {missed:>12,}")
    print("=" * 55)