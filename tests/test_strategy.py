import numpy as np
import pandas as pd
import pytest

from bot.config import load_config
from bot.indicators import add_indicators, atr, rsi
from bot.strategy import evaluate


@pytest.fixture
def cfg():
    return load_config()


def frame(closes):
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"time": range(len(c)), "open": c, "high": c + 1, "low": c - 1, "close": c})


def test_rsi_bounds_and_direction():
    up = rsi(pd.Series(np.arange(1, 100, dtype=float)))
    down = rsi(pd.Series(np.arange(100, 1, -1, dtype=float)))
    assert up.iloc[-1] > 99 and down.iloc[-1] < 1


def test_atr_constant_range():
    assert atr(frame([100.0] * 50)).iloc[-1] == pytest.approx(2.0)


def uptrend_with_pullback():
    # long rise, a pullback that pushes RSI low, then a strong recovery bar
    closes = list(np.linspace(100, 200, 260)) + list(np.linspace(200, 193, 6)) + [196.0]
    return closes


def test_long_signal_has_sl_and_tp(cfg):
    df = add_indicators(frame(uptrend_with_pullback()), cfg["strategy"])
    sig = None
    for n in range(len(df) - 10, len(df) + 1):
        sig = evaluate(df.iloc[:n], cfg) or sig
    assert sig and sig.side == "buy"
    stop_dist = sig.price - sig.stop_loss
    assert stop_dist == pytest.approx(cfg["risk"]["stop_atr_mult"] * sig.atr)
    assert sig.take_profit - sig.price == pytest.approx(stop_dist * cfg["risk"]["reward_risk"])


def test_strong_negative_news_blocks_long(cfg):
    df = add_indicators(frame(uptrend_with_pullback()), cfg["strategy"])
    for n in range(len(df) - 10, len(df) + 1):
        sig = evaluate(df.iloc[:n], cfg, news_score=-0.9)
        assert sig is None or sig.side == "sell"


def test_strong_positive_news_with_bullish_setup_goes_long(cfg):
    df = add_indicators(frame(list(np.linspace(100, 200, 260))), cfg["strategy"])
    assert evaluate(df, cfg, news_score=0.0) is None  # no pullback trigger
    sig = evaluate(df, cfg, news_score=0.8)
    assert sig and sig.side == "buy" and "news" in sig.reason


def test_too_little_data(cfg):
    df = add_indicators(frame([100.0] * 50), cfg["strategy"])
    assert evaluate(df, cfg) is None
