"""Candle anatomy and patterns (vectorised). Meaning comes from context: see knowledge/core.md."""

import pandas as pd


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).where(h > l)
    body = (c - o).abs()
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    po, pc = o.shift(), c.shift()
    out = df.copy()
    out["close_pos"] = ((c - l) / rng).fillna(0.5)
    out["bull_engulf"] = (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > (pc - po).abs())
    out["bear_engulf"] = (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > (pc - po).abs())
    out["hammer"] = (lower >= 2 * body) & (upper <= 0.3 * rng) & (out["close_pos"] >= 0.66)
    out["shooting_star"] = (upper >= 2 * body) & (lower <= 0.3 * rng) & (out["close_pos"] <= 0.34)
    out["doji"] = body <= 0.1 * rng
    out["inside_bar"] = (h <= h.shift()) & (l >= l.shift())
    if "atr" in df:
        out["strong_bull"] = (c > o) & (body >= 0.7 * rng) & (rng >= df["atr"])
        out["strong_bear"] = (c < o) & (body >= 0.7 * rng) & (rng >= df["atr"])
    return out
