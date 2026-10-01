from decimal import Decimal

from bot.exchange import Product


def test_round_price():
    p = Product("ETHUSD", 1, 0.01, Decimal("0.05"))
    assert p.round_price(2652.87, up=True) == "2652.9"
    assert p.round_price(2652.87, up=False) == "2652.85"
    assert p.round_price(2652.87) == "2652.85"
    btc = Product("BTCUSD", 2, 0.001, Decimal("0.1"))
    assert btc.round_price(84000.04, up=True) == "84000.1"
    assert btc.round_price(84000, up=True) == "84000"
