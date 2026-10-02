"""Live mark prices over the Delta WebSocket (background thread, auto-reconnect)."""

import json
import logging
import threading
import time

import websocket

from bot.config import check_endpoint

log = logging.getLogger(__name__)


class PriceFeed:
    def __init__(self, ws_url: str, symbols: list[str], environment: str = "testnet"):
        check_endpoint(environment, ws_url)
        self.url = ws_url
        self.symbols = symbols
        self.prices: dict[str, tuple[float, float]] = {}  # symbol -> (mark, received_at)
        self._ws: websocket.WebSocketApp | None = None

    def start(self) -> None:
        self._ws = websocket.WebSocketApp(
            self.url, on_open=self._on_open, on_message=self._on_message,
            on_error=lambda ws, e: log.warning("websocket error: %s", e),
        )
        threading.Thread(
            target=self._ws.run_forever, kwargs={"ping_interval": 30, "ping_timeout": 10, "reconnect": 5},
            daemon=True, name="price-feed",
        ).start()

    def stop(self) -> None:
        if self._ws:
            self._ws.close()

    def _on_open(self, ws) -> None:
        ws.send(json.dumps({"type": "subscribe", "payload": {
            "channels": [{"name": "v2/ticker", "symbols": self.symbols}]}}))
        log.info("websocket connected, subscribed to %s", self.symbols)

    def _on_message(self, ws, raw: str) -> None:
        m = json.loads(raw)
        if m.get("type") == "v2/ticker" and m.get("mark_price"):
            self.prices[m["symbol"]] = (float(m["mark_price"]), time.time())

    def price(self, symbol: str, max_age_s: float = 30) -> float | None:
        """Fresh mark price, or None if the feed is stale/disconnected."""
        p = self.prices.get(symbol)
        return p[0] if p and time.time() - p[1] <= max_age_s else None
