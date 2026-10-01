import numpy as np
import pandas as pd
import pytest

from bot import brain, candles, structure


def frame(highs, lows, closes=None, opens=None):
    h, l = np.asarray(highs, float), np.asarray(lows, float)
    c = np.asarray(closes, float) if closes is not None else (h + l) / 2
    o = np.asarray(opens, float) if opens is not None else c
    return pd.DataFrame({"time": np.arange(len(h)) * 3600, "open": o, "high": h, "low": l, "close": c,
                         "volume": np.ones(len(h))})


def test_swing_is_only_known_after_confirmation():
    # triangle wave: peaks at 5, 15, 25..., troughs at 10, 20, 30...
    highs = [10 + (i % 10 if i % 10 <= 5 else 10 - i % 10) for i in range(40)]
    df = structure.annotate(frame(highs, [h - 1 for h in highs]), left=3, right=3, zone_memory=2)
    # needs 2 confirmed highs (5, 15) and 2 confirmed lows (10, 20); the last is known at 20 + 3
    assert df["sh1"].first_valid_index() == 23


def test_no_lookahead_in_analysis():
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 800))
    df = frame(c + 1, c - 1, c, np.r_[c[0], c[:-1]])
    k = brain.load_knowledge()
    full = brain.analyze(df, k, 3600)
    cut = brain.analyze(df.iloc[:600], k, 3600)
    cols = ["sh1", "sl1", "trend", "htf_trend", "near_support", "sweep_low", "bull_engulf"]
    pd.testing.assert_frame_equal(full[cols].iloc[:600].reset_index(drop=True), cut[cols].reset_index(drop=True))


def test_engulfing_and_hammer():
    df = candles.annotate(frame(highs=[11, 12, 10.2], lows=[9, 8.5, 7], closes=[9.5, 11.5, 10], opens=[10.5, 9.4, 9.8]))
    assert df["bull_engulf"].iloc[1]
    assert df["hammer"].iloc[2]


def test_setup_has_structural_stop_and_targets():
    k = brain.load_knowledge()
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0.05, 1, 3000))
    df = brain.analyze(frame(c + 1, c - 1, c, np.r_[c[0], c[:-1]]), k, 3600)
    setups = [s for i in range(250, len(df)) if (s := brain.setup_at(df, i, k))]
    assert setups, "expected at least one setup on 3000 random-walk candles"
    m = k["management"]
    for s in setups:
        d = 1 if s.side == "buy" else -1
        dist = (s.price - s.stop_loss) * d
        assert m["min_stop_atr"] * s.atr - 1e-9 <= dist <= m["max_stop_atr"] * s.atr + 1e-9
        assert s.tp1 == pytest.approx(s.price + d * dist * m["tp1_r"])
        assert s.runner_tp == pytest.approx(s.price + d * dist * m["runner_r"])
        assert s.score >= k["entry"]["min_score"] and s.reasons


def test_news_against_blocks_setup():
    k = brain.load_knowledge()
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0.05, 1, 3000))
    df = brain.analyze(frame(c + 1, c - 1, c, np.r_[c[0], c[:-1]]), k, 3600)
    for i in range(250, len(df)):
        s = brain.setup_at(df, i, k)
        if s:
            blocked = brain.setup_at(df, i, k, news=-0.9 if s.side == "buy" else 0.9)
            assert blocked is None or blocked.side != s.side
            return


def test_no_lookahead_multi_timeframe():
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 1, 3200))
    ltf = frame(c + 1, c - 1, c, np.r_[c[0], c[:-1]])  # 1h candles
    g = ltf.assign(grp=ltf["time"] // 14400).groupby("grp")
    htf = pd.DataFrame({"time": g["time"].first(), "open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum()}).reset_index(drop=True)
    k = brain.load_knowledge()
    cut = 2400
    full = brain.analyze(ltf, k, 3600, htf, 14400)
    part = brain.analyze(ltf.iloc[:cut], k, 3600, htf[htf["time"] < ltf["time"].iloc[cut - 1]], 14400)
    cols = ["ctx_trend", "ctx_htf_trend", "ctx_sl1", "ctx_zl1", "ctx_atr"]
    pd.testing.assert_frame_equal(full[cols].iloc[:cut].reset_index(drop=True), part[cols].reset_index(drop=True))


def test_fvg_and_previous_day_sweep():
    from bot import levels
    # bullish gap between candle 0 high (10) and candle 2 low (11), then a dip into it that holds
    df = frame(highs=[10, 12, 13, 12.5], lows=[9, 10.5, 11, 10.6], closes=[9.8, 11.8, 12.8, 11.5])
    df["atr"] = 1.0
    out = levels.annotate(df)
    assert not out["fvg_long"].iloc[2] and out["fvg_long"].iloc[3]
