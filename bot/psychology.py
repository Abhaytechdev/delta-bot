"""Crowd-behaviour footprints: stop hunts, trapped traders, FOMO, panic, participation, compression."""

import pandas as pd


def annotate(df: pd.DataFrame, k: dict) -> pd.DataFrame:
    """Needs structure (sl1/sh1), candles and indicators (atr, ema_mid) already applied."""
    out = df.copy()
    prev_low, prev_high = df["sl1"].shift(), df["sh1"].shift()
    # stop hunt below the last swing low, reclaimed by the close (and mirror)
    out["sweep_low"] = (df["low"] < prev_low) & (df["close"] > prev_low)
    out["sweep_high"] = (df["high"] > prev_high) & (df["close"] < prev_high)
    stretch = (df["close"] - df["ema_mid"]) / df["atr"]
    out["overext_up"] = stretch > k["overextended_atr"]
    out["overext_dn"] = stretch < -k["overextended_atr"]
    vol = df["volume"] if "volume" in df else pd.Series(0.0, index=df.index)
    out["volume_spike"] = vol > k["volume_spike"] * vol.rolling(20).mean().shift()
    rng = df["high"] - df["low"]
    big = rng > k["capitulation_atr"] * df["atr"]
    out["capitulation_low"] = big & out["volume_spike"] & (df["close_pos"] > 0.5)   # panic sold, then bought
    out["capitulation_high"] = big & out["volume_spike"] & (df["close_pos"] < 0.5)
    out["compression"] = (df["atr"] / df["atr"].rolling(100).median()).shift() < k["compression"]
    return out
