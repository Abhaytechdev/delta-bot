"""Market regime: is the market trending or ranging, and is the bigger picture bull or bear?
(knowledge/core.md section 8). Every value uses only closed candles."""

import numpy as np
import pandas as pd


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    a = 1 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=a, adjust=False).mean()


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    """1 = price moved in a straight line, 0 = all back-and-forth noise."""
    return (close - close.shift(n)).abs() / close.diff().abs().rolling(n).sum()


def choppiness(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """~100 = choppy range, low (< 38) = strong trend."""
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    rng = df["high"].rolling(n).max() - df["low"].rolling(n).min()
    return 100 * np.log10(tr.rolling(n).sum() / rng) / np.log10(n)


def _daily(df: pd.DataFrame, tf_seconds: int) -> pd.DataFrame:
    """Daily OHLC built from completed days only."""
    g = df.assign(day=df["time"] // 86400).groupby("day")
    d = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
              last=("time", "max"))
    return d[d["last"] + tf_seconds >= (d.index + 1) * 86400]


def _map_daily(df: pd.DataFrame, tf_seconds: int, daily_values: pd.Series) -> pd.Series:
    usable = (df["time"] + tf_seconds) // 86400 - 1  # last fully closed day at this candle's close
    return pd.Series(daily_values.reindex(usable.values).to_numpy(), index=df.index)


def daily_bias(df: pd.DataFrame, tf_seconds: int) -> pd.Series:
    """+1 bull (daily EMA50 > EMA200 and close above EMA200), -1 bear, 0 mixed."""
    closes = _daily(df, tf_seconds)["close"]
    e50, e200 = closes.ewm(span=50, adjust=False).mean(), closes.ewm(span=200, adjust=False).mean()
    bias = pd.Series(np.where((e50 > e200) & (closes > e200), 1, np.where((e50 < e200) & (closes < e200), -1, 0)),
                     index=closes.index)
    bias[np.arange(len(bias)) < 200] = 0  # EMA200 needs 200 days of history
    return _map_daily(df, tf_seconds, bias).fillna(0)


def daily_adx(df: pd.DataFrame, tf_seconds: int) -> pd.Series:
    return _map_daily(df, tf_seconds, adx(_daily(df, tf_seconds)))


def annotate(df: pd.DataFrame, tf_seconds: int) -> pd.DataFrame:
    out = df.copy()
    out["adx"] = adx(df)
    out["er"] = efficiency_ratio(df["close"])
    out["chop"] = choppiness(df)
    out["daily_bias"] = daily_bias(df, tf_seconds)
    out["daily_adx"] = daily_adx(df, tf_seconds)
    return out
