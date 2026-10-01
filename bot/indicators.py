"""Technical indicators on pandas Series (Wilder smoothing for RSI/ATR)."""

import pandas as pd


def to_frame(candles: list[dict]) -> pd.DataFrame:
    cols = ["time", "open", "high", "low", "close"] + (["volume"] if candles and "volume" in candles[0] else [])
    df = pd.DataFrame(candles)[cols].astype(float)
    df["time"] = df["time"].astype(int)
    return df.reset_index(drop=True)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], p["ema_fast"])
    df["ema_mid"] = ema(df["close"], p["ema_mid"])
    df["ema_slow"] = ema(df["close"], p["ema_slow"])
    df["rsi"] = rsi(df["close"], p["rsi_len"])
    df["atr"] = atr(df, p["atr_len"])
    return df
