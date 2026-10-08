"""Scalping research, round 2: entries built on crowd-psychology ideas, with targets that fit the idea.

Each idea is written down BEFORE testing (why it should work), tested once on a development half and
once on an unseen half, with three cost models: zero fees (does the raw idea have any edge?), maker, taker.
An idea is only worth a demo test if the raw edge is positive in BOTH halves AND stays positive after taker or
realistic maker costs.

  1 liquidation_fade   a huge, high-volume candle is forced selling/buying (liquidations); price tends to
                       snap back part of it. Target = half of that candle's body retraced.
  2 climax_exhaustion  extreme RSI + volume spike + rejection wick = last panic sellers/buyers are done.
                       Target = the 20-EMA (mean).
  3 failed_breakout    breakout traders get trapped when price closes back inside the range within 2 candles.
                       Target = middle of the range.
  4 stretch_fade       price stretched > 2.5 ATR from its 20-EMA and an engulfing candle appears. Target = EMA.
  5 session_drive      the first strong 15m candle of the London / New York open shows where the big money
                       leans; follow it with the 1h trend. Fixed 1.5R target.
"""
import sys
import numpy as np, pandas as pd
sys.argv = [sys.argv[0]] + sys.argv[1:]
from research import scalp as S

TF = S.TF


def ideas(d):
    c, o, h, l, atr = (d[k].values for k in ("close", "open", "high", "low", "atr"))
    rng = h - l
    patr = d.atr.shift().values
    vol3 = (d.volume > 3 * d.vsma).values
    nan = np.full(len(d), np.nan)
    out = {}

    def pack(Lm, Sm, stop_l, stop_s, tgt_l, tgt_s):
        side = np.where(Lm, 1, np.where(Sm, -1, 0))
        stop = np.where(side == 1, stop_l, np.where(side == -1, stop_s, np.nan))
        tgt = np.where(side == 1, tgt_l, np.where(side == -1, tgt_s, np.nan))
        return side, stop, tgt

    big = rng > 2.5 * patr
    Lm = big & vol3 & (c < o); Sm = big & vol3 & (c > o)
    out["1_liquidation_fade"] = pack(Lm, Sm, l - 0.1 * atr, h + 0.1 * atr, c + 0.5 * (o - c), c - 0.5 * (c - o))

    Lm = (d.rsi < 15).values & vol3 & (d.lw > 0.4).values; Sm = (d.rsi > 85).values & vol3 & (d.uw > 0.4).values
    out["2_climax_exhaustion"] = pack(Lm, Sm, l - 0.1 * atr, h + 0.1 * atr, d.ema20.values, d.ema20.values)

    hh, ll = d.hh, d.ll
    brk_up = (d.close > hh)
    brk_dn = (d.close < ll)
    # breakout one or two candles ago, now closed back inside the old range
    trapped_up = (brk_up.shift(1) | brk_up.shift(2)) & (d.close < hh) & (d.close < d.open)
    trapped_dn = (brk_dn.shift(1) | brk_dn.shift(2)) & (d.close > ll) & (d.close > d.open)
    mid = ((hh + ll) / 2).values
    sh = pd.concat([d.high, d.high.shift(1), d.high.shift(2)], axis=1).max(axis=1).values
    sl = pd.concat([d.low, d.low.shift(1), d.low.shift(2)], axis=1).min(axis=1).values
    out["3_failed_breakout"] = pack(trapped_dn.values, trapped_up.values, sl - 0.1 * atr, sh + 0.1 * atr, mid, mid)

    dist = (d.close - d.ema20) / d.atr
    Lm = ((dist < -2.5) & d.engulf_up).values; Sm = ((dist > 2.5) & d.engulf_dn).values
    out["4_stretch_fade"] = pack(Lm, Sm, np.minimum(l, d.low.shift().values) - 0.1 * atr,
                                 np.maximum(h, d.high.shift().values) + 0.1 * atr, d.ema20.values, d.ema20.values)

    hour = ((d.time.values // 60) % (24 * 60))
    openers = np.isin(hour, [7 * 60, 13 * 60 + 30]) if TF == "15m" else np.isin(hour, [7 * 60, 13 * 60 + 30])
    strong = rng > 1.2 * patr
    Lm = openers & strong & (c > o) & (d.pos.values > 0.7) & (d.htf.values == 1)
    Sm = openers & strong & (c < o) & (d.pos.values < 0.3) & (d.htf.values == -1)
    out["5_session_drive"] = pack(Lm, Sm, l - 0.1 * atr, h + 0.1 * atr, nan, nan)
    return out


def sim(d, side, stop, tgt, fees, min_stop):
    """fees: 'zero' | 'maker' | 'taker'. Target is a price (NaN -> 1.5R)."""
    o, h, l, c, t = d.open.values, d.high.values, d.low.values, d.close.values, d.time.values
    n, res, busy = len(d), [], -1
    for i in np.flatnonzero(side != 0):
        if i <= busy or i + 1 >= n or not np.isfinite(stop[i]):
            continue
        sd, e = side[i], o[i + 1]
        dist = (e - stop[i]) * sd
        if dist <= 0 or dist / e < min_stop:
            continue
        tp = tgt[i] if np.isfinite(tgt[i]) else e + sd * 1.5 * dist
        if (tp - e) * sd <= 0.3 * dist:   # not worth taking: reward under 0.3R
            continue
        exit_px, why, j = None, "time", i + 1
        for j in range(i + 1, min(i + 1 + S.HOLD, n)):
            if (l[j] <= stop[i]) if sd == 1 else (h[j] >= stop[i]):
                exit_px, why = stop[i] - sd * S.STOP_SLIPPAGE * e, "sl"; break
            if (h[j] >= tp) if sd == 1 else (l[j] <= tp):
                exit_px, why = tp, "tp"; break
        if exit_px is None:
            exit_px = c[j]
        if fees == "zero":
            fe = fx = 0.0
        elif fees == "maker":
            fe, fx = S.MAKER, (S.MAKER if why == "tp" else S.FEE)
        else:
            fe = fx = S.FEE
        res.append((t[i + 1], ((exit_px - e) * sd - fe * e - fx * exit_px) / dist, why))
        busy = j
    return res


if __name__ == "__main__":
    data = {s: S.prep(s) for s in S.SYMS}
    sigs = {s: ideas(data[s]) for s in S.SYMS}
    t_all = data["BTCUSD"].time.values
    mid_t = (t_all[0] + t_all[-1]) // 2
    print(f"{TF} candles, {S.DAYS} days, hold <= {S.HOLD} candles. dev = first half, unseen = second half")
    for name in sigs["BTCUSD"]:
        for ms in (0.0025, 0.005):
            for fees in ("zero", "maker", "taker"):
                res = []
                for s in S.SYMS:
                    side, stop, tgt = sigs[s][name]
                    res += sim(data[s], side, stop, tgt, fees, ms)
                res.sort()
                print(f"{name:20s} minstop={ms * 100:.2f}% {fees:5s} dev: {S.stats(res, 0, mid_t)} | unseen: {S.stats(res, mid_t, 1e12)}", flush=True)
