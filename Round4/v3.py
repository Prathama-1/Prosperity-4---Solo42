#-200k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict

class Trader:

    POSITION_LIMIT = 200

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        result["HYDROGEL_PACK"] = self._trade_hydrogel(state)
        return result, 0, ""

    def _trade_hydrogel(self, state: TradingState) -> List[Order]:
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
        mid: float = (best_bid + best_ask) / 2
        
        bid_volume = abs(depth.buy_orders[best_bid])
        ask_volume = abs(depth.sell_orders[best_ask])

        sell_capacity = limit + pos
        buy_capacity = limit - pos

        # ===== AGGRESSIVE TRADING - GUARANTEED TO FILL =====
        # Place orders AT the current best bid/ask
        
        # Sell aggressively at best_bid (hitting the bid)
        if sell_capacity > 0 and best_bid > 0:
            sell_qty = min(30, sell_capacity, bid_volume)
            if sell_qty > 0:
                orders.append(Order(symbol, best_bid, -sell_qty))
                print(f"AGGRESSIVE SELL: {sell_qty}@{best_bid}")
        
        # Buy aggressively at best_ask (hitting the ask)
        if buy_capacity > 0 and best_ask > 0:
            buy_qty = min(30, buy_capacity, ask_volume)
            if buy_qty > 0:
                orders.append(Order(symbol, best_ask, buy_qty))
                print(f"AGGRESSIVE BUY: {buy_qty}@{best_ask}")

        # ===== LIQUIDITY PROVIDING (passive) =====
        # Place limit orders just outside the spread to capture spread
        
        # Place sell order at best_ask + 1 (provide liquidity on ask side)
        if sell_capacity > 30:
            passive_sell_price = best_ask + 2
            if passive_sell_price not in depth.buy_orders:
                sell_qty = min(20, sell_capacity - 30)
                if sell_qty > 0:
                    orders.append(Order(symbol, passive_sell_price, -sell_qty))
                    print(f"PASSIVE SELL: {sell_qty}@{passive_sell_price}")
        
        # Place buy order at best_bid - 1 (provide liquidity on bid side)
        if buy_capacity > 30:
            passive_buy_price = best_bid - 2
            if passive_buy_price not in depth.sell_orders:
                buy_qty = min(20, buy_capacity - 30)
                if buy_qty > 0:
                    orders.append(Order(symbol, passive_buy_price, buy_qty))
                    print(f"PASSIVE BUY: {buy_qty}@{passive_buy_price}")

        return orders