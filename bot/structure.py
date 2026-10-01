"""Market structure: confirmed swings, trend (HH/HL vs LH/LL), BOS, support/resistance.

A swing at candle j is only *known* at candle j + right (after the right side closes),
so every column here uses information available at that candle's close: no lookahead.
"""

import numpy as np
import pandas as pd


def annotate(df: pd.DataFrame, left: int, right: int, zone_memory: int) -> pd.DataFrame:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n = len(df)
    highs: list[float] = []  # confirmed swing high prices, oldest first
    lows: list[float] = []
    cols = {k: np.full(n, np.nan) for k in (
        "sh1", "sh2", "sl1", "sl2", "trend", "leg_low", "leg_high", "near_support", "near_resistance")}
    leg_low = leg_high = np.nan  # the low that started the last up-leg / high that started the last down-leg
    last_kind = None
    for i in range(n):
        j = i - right
        if j >= left:
            win_h, win_l = h[j - left:i + 1], l[j - left:i + 1]
            if h[j] == win_h.max() and h[j] > h[j - left:j].max():
                highs.append(h[j])
                if last_kind == "low":
                    leg_low = lows[-1]
                last_kind = "high"
            if l[j] == win_l.min() and l[j] < l[j - left:j].min():
                lows.append(l[j])
                if last_kind == "high":
                    leg_high = highs[-1]
                last_kind = "low"
        if len(highs) >= 2 and len(lows) >= 2:
            cols["sh1"][i], cols["sh2"][i] = highs[-1], highs[-2]
            cols["sl1"][i], cols["sl2"][i] = lows[-1], lows[-2]
            up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
            down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
            cols["trend"][i] = 1 if up else -1 if down else 0
            cols["leg_low"][i], cols["leg_high"][i] = leg_low, leg_high
            cols["near_support"][i] = min(abs(l[i] - z) for z in lows[-zone_memory:])
            cols["near_resistance"][i] = min(abs(h[i] - z) for z in highs[-zone_memory:])
    out = df.copy()
    for k, v in cols.items():
        out[k] = v
    out["trend"] = out["trend"].fillna(0)
    prev_sh, prev_sl = out["sh1"].shift(), out["sl1"].shift()
    out["bos_up"] = (out["close"] > prev_sh) & (out["close"].shift() <= prev_sh)
    out["bos_dn"] = (out["close"] < prev_sl) & (out["close"].shift() >= prev_sl)
    return out


def htf_trend(df: pd.DataFrame, tf_seconds: int, factor: int, left: int, right: int) -> pd.Series:
    """Structure trend on candles `factor` x larger, aligned to each candle's close (completed HTF candles only)."""
    span = tf_seconds * factor
    g = df.assign(grp=df["time"] // span * span).groupby("grp")
    htf = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), n=("close", "size")).reset_index()
    htf = htf[htf["n"] == factor].rename(columns={"grp": "time"})
    htf = annotate(htf, left, right, 1)
    htf["avail"] = htf["time"] + span
    ltf = pd.DataFrame({"close_time": df["time"] + tf_seconds, "idx": df.index})
    m = pd.merge_asof(ltf.sort_values("close_time"), htf[["avail", "trend"]].sort_values("avail"),
                      left_on="close_time", right_on="avail", direction="backward")
    return m.set_index("idx")["trend"].reindex(df.index).fillna(0)
