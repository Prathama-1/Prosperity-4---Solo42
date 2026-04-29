#45k
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:

    HYDROGEL_LIMIT = 200
    VELVET_LIMIT   = 200
    VEV_LIMIT      = 300

    VEV_DELTA = {
        "VEV_4000": 1.00,
        "VEV_4500": 1.00,
        "VEV_5000": 0.88,
        "VEV_5100": 0.79,
        "VEV_5200": 0.66,
        "VEV_5300": 0.50,
        "VEV_5400": 0.34,
        "VEV_5500": 0.20,
        "VEV_6000": 0.01,
        "VEV_6500": 0.00,
    }

    VEV_SELL_FLOOR = {
        "VEV_5000": 280,
        "VEV_5100": 190,
        "VEV_5200": 112,
        "VEV_5300":  52,
        "VEV_5400":  18,
        "VEV_5500":   5,
    }

    VEV_SELL_QTY = {
        "VEV_5000":  5,
        "VEV_5100":  8,
        "VEV_5200": 12,
        "VEV_5300": 15,
        "VEV_5400": 15,
        "VEV_5500": 10,
    }

    def __init__(self):
        self.vfe_prices: List[float] = []

    # ═══════════════════════════════════════════════════════════════════════
    # MAIN ENTRY POINT
    # ═══════════════════════════════════════════════════════════════════════
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}

        result["HYDROGEL_PACK"]       = self._trade_hydrogel(state)
        result["VELVETFRUIT_EXTRACT"] = self._trade_velvet(state)

        vev_orders, vfe_hedge_orders = self._trade_vev(state)
        for symbol, orders in vev_orders.items():
            result[symbol] = orders

        if vfe_hedge_orders:
            existing = result.get("VELVETFRUIT_EXTRACT", [])
            result["VELVETFRUIT_EXTRACT"] = self._merge_vfe_hedge(
                existing, vfe_hedge_orders, state
            )

        return result, 0, ""

    # ═══════════════════════════════════════════════════════════════════════
    # HYDROGEL
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_hydrogel(self, state: TradingState) -> List[Order]:
        symbol = "HYDROGEL_PACK"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.HYDROGEL_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())
        mid: float    = (best_bid + best_ask) / 2

        sell_capacity = limit + pos
        buy_capacity  = limit - pos

        our_ask: int = int(mid + 6)
        if sell_capacity > 0:
            sell_qty = min(100, sell_capacity)
            orders.append(Order(symbol, our_ask, -sell_qty))

        AGGRESSIVE_BUY_THRESHOLD = 8
        if best_ask < mid - AGGRESSIVE_BUY_THRESHOLD and buy_capacity > 0:
            ask_vol = abs(depth.sell_orders[best_ask])
            buy_qty = min(ask_vol, buy_capacity, 50)
            if buy_qty > 0:
                orders.append(Order(symbol, best_ask, buy_qty))

        return orders

    # ═══════════════════════════════════════════════════════════════════════
    # VELVETFRUIT — Fade Mark 55
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_velvet(self, state: TradingState) -> List[Order]:
        symbol = "VELVETFRUIT_EXTRACT"
        orders: List[Order] = []

        if symbol not in state.order_depths:
            return orders

        depth = state.order_depths[symbol]
        pos   = state.position.get(symbol, 0)
        limit = self.VELVET_LIMIT

        if not depth.buy_orders or not depth.sell_orders:
            return orders

        best_bid: int = max(depth.buy_orders.keys())
        best_ask: int = min(depth.sell_orders.keys())
        mid: float    = (best_bid + best_ask) / 2.0

        buy_capacity  = limit - pos
        sell_capacity = limit + pos

        SELL_THRESHOLD = 5262
        BUY_THRESHOLD  = 5256
        NEUTRAL_MID    = 5259

        dist = abs(mid - NEUTRAL_MID)

        if mid >= SELL_THRESHOLD and sell_capacity > 0:
            if dist >= 8:
                qty = min(40, sell_capacity)
            elif dist >= 5:
                qty = min(25, sell_capacity)
            else:
                qty = min(15, sell_capacity)

            orders.append(Order(symbol, best_ask, -qty))

            if dist >= 6 and sell_capacity > qty:
                inside = best_ask - 1
                if inside > best_bid:
                    orders.append(Order(symbol, inside, -min(15, sell_capacity - qty)))

        elif mid <= BUY_THRESHOLD and buy_capacity > 0:
            if dist >= 8:
                qty = min(40, buy_capacity)
            elif dist >= 5:
                qty = min(25, buy_capacity)
            else:
                qty = min(15, buy_capacity)

            orders.append(Order(symbol, best_bid, qty))

            if dist >= 6 and buy_capacity > qty:
                inside = best_bid + 1
                if inside < best_ask:
                    orders.append(Order(symbol, inside, min(15, buy_capacity - qty)))

        else:
            if pos > 10 and sell_capacity > 0:
                orders.append(Order(symbol, best_ask, -min(20, sell_capacity)))
            elif pos < -10 and buy_capacity > 0:
                orders.append(Order(symbol, best_bid, min(20, buy_capacity)))

        return orders

    # ═══════════════════════════════════════════════════════════════════════
    # VEV OPTIONS — Short vol strategy
    #
    # EDGE:
    #   Market bids on VEVs are above any reasonable intrinsic floor.
    #   We SELL options at the bid, collect premium, and delta-hedge
    #   with LONG VFE to stay delta-neutral.
    #   P&L source: theta decay (time value we collected erodes to zero).
    #
    # EXECUTION:
    #   SELL at best bid when bid >= our floor price.
    #   Floor prices set from actual log data — not BS model.
    #   Delta-hedge: we are SHORT calls → SHORT delta → hedge with LONG VFE.
    # ═══════════════════════════════════════════════════════════════════════
    def _trade_vev(
        self, state: TradingState
    ) -> tuple[Dict[str, List[Order]], List[Order]]:
        vev_orders: Dict[str, List[Order]] = {}
        vfe_hedge:  List[Order]            = []

        # ── 1. Net delta from existing short VEV positions ─────────────────
        net_vev_delta: float = 0.0
        for symbol, delta in self.VEV_DELTA.items():
            pos = state.position.get(symbol, 0)
            net_vev_delta += pos * delta

        # ── 2. For each strike, attempt to SELL at the bid ─────────────────
        tradeable = [
            "VEV_5300", "VEV_5400", "VEV_5500",   # OTM first: pure theta, low delta risk
            "VEV_5200",                             # near ATM: good premium
            "VEV_5100", "VEV_5000",                # ITM: large delta, smaller size
        ]

        for symbol in tradeable:
            if symbol not in state.order_depths:
                continue

            depth = state.order_depths[symbol]
            pos   = state.position.get(symbol, 0)
            limit = self.VEV_LIMIT

            # sell_capacity: how many more we can short before hitting -limit
            sell_capacity = limit + pos
            if sell_capacity <= 0:
                continue

            if not depth.buy_orders:
                continue

            best_bid: int = max(depth.buy_orders.keys())
            bid_vol:  int = abs(depth.buy_orders[best_bid])
            floor:    int = self.VEV_SELL_FLOOR[symbol]

            # Only sell if the market is paying above our minimum
            if best_bid < floor:
                continue

            target   = self.VEV_SELL_QTY[symbol]
            sell_qty = min(target, bid_vol, sell_capacity)

            if sell_qty <= 0:
                continue

            vev_orders[symbol] = [Order(symbol, best_bid, -sell_qty)]

            # Track delta added by this new short
            net_vev_delta += (-sell_qty) * self.VEV_DELTA[symbol]

        # ── 3. Delta hedge: short calls → short delta → buy VFE to offset ──
        # net_vev_delta is negative (short calls). We need to BUY VFE.
        required_long_vfe = round(-net_vev_delta)

        if required_long_vfe > 0 and "VELVETFRUIT_EXTRACT" in state.order_depths:
            vfe_depth = state.order_depths["VELVETFRUIT_EXTRACT"]

            if vfe_depth.sell_orders:
                vfe_best_ask: int = min(vfe_depth.sell_orders.keys())
                vfe_hedge.append(
                    Order("VELVETFRUIT_EXTRACT", vfe_best_ask, required_long_vfe)
                )

        return vev_orders, vfe_hedge

    # ═══════════════════════════════════════════════════════════════════════
    # VFE HEDGE MERGE
    # ═══════════════════════════════════════════════════════════════════════
    def _merge_vfe_hedge(
        self,
        velvet_orders: List[Order],
        hedge_orders:  List[Order],
        state:         TradingState,
    ) -> List[Order]:
        symbol = "VELVETFRUIT_EXTRACT"
        pos    = state.position.get(symbol, 0)
        limit  = self.VELVET_LIMIT

        committed_qty: int = sum(o.quantity for o in velvet_orders)
        hedge_qty:     int = sum(o.quantity for o in hedge_orders)

        projected_pos: int = pos + committed_qty + hedge_qty

        if -limit <= projected_pos <= limit:
            return velvet_orders + hedge_orders

        # Clip hedge to whatever room remains in the direction of the hedge
        if hedge_qty > 0:
            # Hedge wants to go LONG — room to buy
            room    = limit - pos - committed_qty
            clipped = min(room, hedge_qty)
        else:
            # Hedge wants to go SHORT (unusual in sell-vol strategy, handle defensively)
            room    = limit + pos + committed_qty
            clipped = max(-room, hedge_qty)

        if clipped == 0 or not hedge_orders:
            return velvet_orders

        ref_order     = hedge_orders[0]
        clipped_order = Order(symbol, ref_order.price, clipped)

        return velvet_orders + [clipped_order]