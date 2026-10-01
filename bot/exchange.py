"""Thin wrapper over the official Delta REST client (testnet only)."""

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from delta_rest_client import DeltaRestClient, OrderType

from bot.config import Secrets

log = logging.getLogger(__name__)

RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass(frozen=True)
class Product:
    symbol: str
    id: int
    contract_value: float  # underlying units per contract
    tick_size: Decimal

    def round_price(self, price: float, up: bool | None = None) -> str:
        """Round to tick size. up=True ceil, False floor, None nearest."""
        q = Decimal(str(price)) / self.tick_size
        mode = ROUND_UP if up else ROUND_DOWN
        q = q.quantize(Decimal(1), rounding=mode) if up is not None else q.quantize(Decimal(1))
        return format((q * self.tick_size).normalize(), "f")


@dataclass
class Position:
    symbol: str
    product_id: int
    size: int  # signed contracts: + long, - short
    entry_price: float


class Exchange:
    def __init__(self, cfg: dict, secrets: Secrets):
        url = cfg["exchange"]["rest_url"]
        assert "testnet" in url, "testnet only"
        self.client = DeltaRestClient(url, secrets.delta_api_key, secrets.delta_api_secret)
        self._products: dict[str, Product] = {}

    # ---------- market data ----------
    def product(self, symbol: str) -> Product:
        if symbol not in self._products:
            p = self.client.get_product(symbol)
            self._products[symbol] = Product(symbol, p["id"], float(p["contract_value"]), Decimal(p["tick_size"]))
        return self._products[symbol]

    def candles(self, symbol: str, resolution: str, count: int) -> list[dict]:
        """Closed candles only, oldest first. Each: time, open, high, low, close, volume."""
        step = RESOLUTION_SECONDS[resolution]
        now = int(time.time())
        end = now - now % step  # start of the current (still forming) candle
        out: dict[int, dict] = {}
        start = end - count * step
        while start < end:
            chunk_end = min(start + 2000 * step, end)
            for c in self.client.get_candles(symbol, resolution, start, chunk_end - 1):
                if c["time"] < end:
                    out[c["time"]] = c
            start = chunk_end
        return [out[t] for t in sorted(out)]

    def mark_price(self, symbol: str) -> float:
        return float(self.client.get_ticker(symbol)["mark_price"])

    def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        q = self.client.get_ticker(symbol)["quotes"]
        return float(q["best_bid"]), float(q["best_ask"])

    # ---------- account ----------
    def balance_usd(self) -> tuple[float, float]:
        """(wallet balance, available balance) in USD."""
        for w in self.client.get_all_wallet_balances():
            if w["asset_symbol"] == "USD":
                return float(w["balance"]), float(w["available_balance"])
        return 0.0, 0.0

    def positions(self) -> list[Position]:
        res = self.client.request("GET", "/v2/positions/margined", auth=True).json()["result"]
        return [
            Position(p["product_symbol"], p["product_id"], int(p["size"]), float(p["entry_price"]))
            for p in res
            if int(p["size"]) != 0
        ]

    def unrealized_pnl(self) -> float:
        res = self.client.request("GET", "/v2/positions/margined", auth=True).json()["result"]
        return sum(float(p.get("unrealized_pnl") or 0) for p in res)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        q = {"product_ids": self.product(symbol).id} if symbol else None
        return self.client.get_live_orders(q)

    def set_leverage(self, symbol: str, leverage: float) -> None:
        self.client.set_leverage(self.product(symbol).id, str(leverage))

    # ---------- orders ----------
    def place_entry(
        self, symbol: str, side: str, size: int, stop_loss: float, take_profit: float,
        limit_price: float | None = None, client_order_id: str | None = None,
    ) -> dict:
        """Entry order with stop-loss and take-profit brackets attached. Refuses without both."""
        if not stop_loss or not take_profit:
            raise ValueError("every order needs a stop-loss and a take-profit")
        if size < 1:
            raise ValueError("size must be >= 1 contract")
        p = self.product(symbol)
        long = side == "buy"
        # Round protective prices toward safety: stop tighter, target nearer.
        order = {
            "product_id": p.id,
            "size": int(size),
            "side": side,
            "order_type": OrderType.LIMIT.value if limit_price else OrderType.MARKET.value,
            "bracket_stop_loss_price": p.round_price(stop_loss, up=long),
            "bracket_take_profit_price": p.round_price(take_profit, up=not long),
            "bracket_stop_trigger_method": "mark_price",
        }
        if limit_price:
            order["limit_price"] = p.round_price(limit_price, up=not long)
            order["post_only"] = "true"
        if client_order_id:
            order["client_order_id"] = client_order_id
        return self.client.create_order(order)

    def place_position_bracket(self, symbol: str, stop_loss: float, take_profit: float, long: bool) -> dict:
        """Attach SL/TP to an existing position (covers the whole position)."""
        p = self.product(symbol)
        return self.client.place_bracket_order({
            "product_id": p.id,
            "stop_loss_order": {"order_type": "market_order", "stop_price": p.round_price(stop_loss, up=long)},
            "take_profit_order": {"order_type": "market_order", "stop_price": p.round_price(take_profit, up=not long)},
            "bracket_stop_trigger_method": "mark_price",
        })

    def order(self, order_id: int) -> dict:
        return self.client.get_order_by_id(order_id)

    def cancel_order(self, symbol: str, order_id: int) -> None:
        self.client.cancel_order(self.product(symbol).id, order_id)

    def cancel_all(self) -> None:
        self.client.cancel_all_orders({"cancel_limit_orders": "true", "cancel_stop_orders": "true"})

    def close_position(self, pos: Position) -> dict:
        side = "sell" if pos.size > 0 else "buy"
        return self.client.place_order(pos.product_id, abs(pos.size), side, order_type=OrderType.MARKET, reduce_only="true")
