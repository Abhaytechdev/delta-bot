from types import SimpleNamespace

import pytest

from bot.trader import Trader


def fill(side, size, price, fee, ts):
    return {"side": side, "size": str(size), "price": str(price), "commission": str(fee), "created_at": ts}


def make_trader(fills):
    t = Trader.__new__(Trader)
    t.ex = SimpleNamespace(
        product=lambda s: SimpleNamespace(id=1),
        client=SimpleNamespace(fills=lambda q, page_size: {"result": fills}),
    )
    return t


def test_realized_ignores_older_trade_fills():
    opened = 1790830000.0  # 2026-10-01T04:46:40Z
    fills = [
        fill("buy", 1, 2700, 0.01, "2026-10-01T04:40:00Z"),   # previous trade, before open
        fill("sell", 1, 2690, 0.01, "2026-10-01T04:41:00Z"),
        fill("buy", 3, 2704.6, 0.04, "2026-10-01T04:46:41Z"),  # this trade
        fill("sell", 2, 2710, 0.02, "2026-10-01T05:00:00Z"),
        fill("sell", 1, 2712, 0.01, "2026-10-01T05:00:01Z"),
    ]
    trade = {"symbol": "ETHUSD", "side": "buy", "size": 3, "contract_value": 0.01,
             "entry_price": 2704.6, "opened_at": opened}
    pnl, fees, exit_price = make_trader(fills).realized(trade)
    assert exit_price == 2712  # last exit fill
    assert fees == pytest.approx(0.07)
    assert pnl == pytest.approx(((2710 - 2704.6) * 2 + (2712 - 2704.6)) * 0.01 - 0.07)


def test_exit_classification():
    from bot.manage import classify_exit
    # long, entry 100, stop 95, target 150
    assert classify_exit("buy", 94.9, 95, 150, False) == "stop_loss"
    assert classify_exit("buy", 90.0, 95, 150, False) == "stop_loss"          # slippage below the stop
    assert classify_exit("buy", 100.1, 100.15, 150, True) == "breakeven/trail"
    assert classify_exit("buy", 150.5, 95, 150, True) == "runner_target"
    assert classify_exit("buy", 102.4, 95, 150, False) == "manual_or_other"  # closed by hand in profit
    assert classify_exit("sell", 90.0, 105, 50, False) == "manual_or_other"


def test_get_requests_retry_but_orders_do_not(monkeypatch):
    import requests
    from delta_rest_client import DeltaRestClient
    from bot import exchange
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(self, method, path, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ReadTimeout("slow")
        return "ok"

    monkeypatch.setattr(DeltaRestClient, "request", flaky)
    c = exchange.RetryingClient("https://x.testnet.example")
    assert c.request("GET", "/v2/positions") == "ok" and calls["n"] == 3
    calls["n"] = 0
    with pytest.raises(requests.exceptions.ReadTimeout):
        c.request("POST", "/v2/orders")  # an order may already exist server-side: never retried
    assert calls["n"] == 1
