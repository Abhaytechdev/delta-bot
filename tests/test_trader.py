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
