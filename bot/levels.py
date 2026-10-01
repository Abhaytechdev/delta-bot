"""Extra liquidity levels traders watch (knowledge/core.md section 3b):
previous-day high/low sweeps, fair value gap (FVG) retests, round-number reactions."""

import math

import numpy as np
import pandas as pd


def annotate(df: pd.DataFrame, round_atr: float = 0.25) -> pd.DataFrame:
    out = df.copy()
    h, l, c, t = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), df["time"].to_numpy()

    # previous UTC day's high / low (known once that day has closed)
    day = t // 86400
    daily = df.assign(day=day).groupby("day").agg(dh=("high", "max"), dl=("low", "min"))
    prev = daily.shift(1).reindex(day)
    pdh, pdl = prev["dh"].to_numpy(), prev["dl"].to_numpy()
    out["sweep_pdl"] = (l < pdl) & (c > pdl)
    out["sweep_pdh"] = (h > pdh) & (c < pdh)

    # FVG: 3-candle imbalance. Bullish gap when low[i] > high[i-2]; retest = later dip into it that holds.
    n = len(df)
    fvg_long, fvg_short = np.zeros(n, bool), np.zeros(n, bool)
    bull = bear = None  # (bottom, top) of the latest active gap
    for i in range(n):
        if bull and l[i] <= bull[1] and c[i] >= bull[0]:
            fvg_long[i] = True
        if bull and c[i] < bull[0]:
            bull = None  # gap failed
        if bear and h[i] >= bear[0] and c[i] <= bear[1]:
            fvg_short[i] = True
        if bear and c[i] > bear[1]:
            bear = None
        if i >= 2 and l[i] > h[i - 2]:
            bull = (h[i - 2], l[i])
        if i >= 2 and h[i] < l[i - 2]:
            bear = (h[i], l[i - 2])
    out["fvg_long"], out["fvg_short"] = fvg_long, fvg_short

    # round numbers: BTC 1000s, ETH 100s (step = 10^floor(log10(price)) / 10)
    step = np.array([10 ** math.floor(math.log10(x)) / 10 if x > 0 else 1 for x in c])
    atr = df["atr"].to_numpy()
    below = np.floor(c / step) * step  # nearest round level under the close
    above = below + step
    out["round_bounce"] = (l - below <= round_atr * atr) & (c > below)
    out["round_reject"] = (above - h <= round_atr * atr) & (c < above)
    return out
