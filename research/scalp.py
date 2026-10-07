"""Scalping research (backtest only, nothing here touches the bots).

Costs: Delta taker fee + GST on both fills, stop slippage, entry at next candle open, stop-first on a
same-candle touch, one trade per symbol at a time. Results are in R (1R = the stop distance) after costs.
Data: Binance spot candles as a proxy. First half of the period = development, second half = unseen check.
"""
import sys, itertools
import numpy as np, pandas as pd
from bot.backtest import load_candles, FEE, STOP_SLIPPAGE

TF = sys.argv[1] if len(sys.argv) > 1 else "5m"
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 730
SYMS = ["BTCUSD", "ETHUSD", "ADAUSD"]
HOLD = {"1m": 60, "5m": 24, "15m": 16}[TF]          # max candles held (2h / 4h)
MIN_STOP_PCT = {"1m": 0.0015}.get(TF, 0.0025)                      # skip setups whose stop is under 0.25%: fees would eat them


def load_retry(sym):
    import time
    for k in range(40):  # the network here drops now and then; the download resumes from scratch per symbol
        try:
            return load_candles(sym, TF, DAYS)
        except Exception as e:
            print("download retry", sym, type(e).__name__, flush=True); time.sleep(10)
    raise SystemExit("download failed")


def prep(sym):
    d = load_retry(sym).copy()
    c, h, l, o, v = d.close, d.high, d.low, d.open, d.volume
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    dl = c.diff()
    rs = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean() / (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + rs)
    m, s = c.rolling(20).mean(), c.rolling(20).std()
    d["bbu"], d["bbl"] = m + 2 * s, m - 2 * s
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["vsma"] = v.rolling(20).mean()
    d["hh"], d["ll"] = h.shift().rolling(20).max(), l.shift().rolling(20).min()
    # higher-timeframe trend from 1h candles, only using hours that are already closed
    t = pd.to_datetime(d.time, unit="s")
    h1 = d.set_index(t).close.resample("1h").last().dropna()
    tr1 = np.sign(h1.ewm(span=50, adjust=False).mean() - h1.ewm(span=200, adjust=False).mean()).shift(1)
    d["htf"] = tr1.reindex(t, method="ffill").values
    rng = (h - l).replace(0, np.nan)
    d["body_up"], d["body_dn"] = c > o, c < o
    d["lw"], d["uw"] = (np.minimum(o, c) - l) / rng, (h - np.maximum(o, c)) / rng
    d["pos"] = (c - l) / rng
    d["engulf_up"] = (c > o) & (c.shift() < o.shift()) & (c >= o.shift()) & (o <= c.shift())
    d["engulf_dn"] = (c < o) & (c.shift() > o.shift()) & (c <= o.shift()) & (o >= c.shift())
    return d


def signals(d):
    """name -> (side array +1/-1/0, stop price array)."""
    c, h, l, atr = d.close.values, d.high.values, d.low.values, d.atr.values
    out = {}
    n = len(d)
    def mk(long_m, short_m, stop_long, stop_short):
        side = np.where(long_m, 1, np.where(short_m, -1, 0))
        stop = np.where(side == 1, stop_long, np.where(side == -1, stop_short, np.nan))
        return side, stop
    L = (d.rsi < 20) & (d.close < d.bbl); S = (d.rsi > 80) & (d.close > d.bbu)
    out["rsi_extreme_reversal"] = mk(L, S, c - atr, c + atr)
    vol = d.volume > 1.5 * d.vsma
    L = (d.close > d.hh) & vol & (d.pos > 0.75); S = (d.close < d.ll) & vol & (d.pos < 0.25)
    out["range_breakout_volume"] = mk(L, S, c - atr, c + atr)
    L = (d.low < d.ll) & (d.close > d.ll) & (d.lw > 0.5); S = (d.high > d.hh) & (d.close < d.hh) & (d.uw > 0.5)
    sl, ss = np.minimum(l, c) - 0.1 * atr, np.maximum(h, c) + 0.1 * atr
    out["sweep_reclaim"] = mk(L, S, np.minimum(l - 0.1 * atr, c - 0.5 * atr), np.maximum(h + 0.1 * atr, c + 0.5 * atr))
    out["sweep_reclaim_with_1h_trend"] = mk(L & (d.htf == 1), S & (d.htf == -1),
                                            np.minimum(l - 0.1 * atr, c - 0.5 * atr), np.maximum(h + 0.1 * atr, c + 0.5 * atr))
    L2 = (L | (d.low.rolling(3).min() < d.ll)) & d.engulf_up & (d.htf == 1)
    S2 = (S | (d.high.rolling(3).max() > d.hh)) & d.engulf_dn & (d.htf == -1)
    out["sweep+engulf+1h_trend(psych)"] = mk(L2, S2, np.minimum(l, d.low.shift()) - 0.1 * atr, np.maximum(h, d.high.shift()) + 0.1 * atr)
    pull_l = (d.htf == 1) & (d.low <= d.ema20) & (d.close > d.ema20) & d.body_up & (d.close > d.open)
    pull_s = (d.htf == -1) & (d.high >= d.ema20) & (d.close < d.ema20) & d.body_dn
    out["trend_pullback_ema20"] = mk(pull_l, pull_s, np.minimum(l, d.low.shift()) - 0.1 * atr, np.maximum(h, d.high.shift()) + 0.1 * atr)
    return out


MAKER = 0.0002 * 1.18  # Delta maker fee + GST (checked against real fills: 0.0236%)


def simulate(d, side, stop, tp_r, maker=False):
    o, h, l, c, t = d.open.values, d.high.values, d.low.values, d.close.values, d.time.values
    n, res, i = len(d), [], 0
    idx = np.flatnonzero(side != 0)
    busy = -1
    for i in idx:
        if i <= busy or i + 1 >= n or not np.isfinite(stop[i]):
            continue
        sd = side[i]
        e = o[i + 1]
        dist = (e - stop[i]) * sd
        if dist <= 0 or dist / e < MIN_STOP_PCT:
            continue
        tp = e + sd * tp_r * dist
        exit_px, why, j = None, "time", i + 1
        for j in range(i + 1, min(i + 1 + HOLD, n)):
            hit_sl = l[j] <= stop[i] if sd == 1 else h[j] >= stop[i]
            hit_tp = h[j] >= tp if sd == 1 else l[j] <= tp
            if hit_sl:
                exit_px, why = stop[i] - sd * STOP_SLIPPAGE * e, "sl"; break
            if hit_tp:
                exit_px, why = tp, "tp"; break
        if exit_px is None:
            exit_px = c[j]
        gross = (exit_px - e) * sd / dist
        fe = MAKER if maker else FEE
        fx = MAKER if (maker and why == "tp") else FEE
        cost = (fe * e + fx * exit_px) / dist
        res.append((t[i + 1], gross - cost, why))
        busy = j
    return res


def stats(res, t0, t1):
    r = [x[1] for x in res if t0 <= x[0] < t1]
    if len(r) < 15:
        return f"n={len(r):4d} (too few)"
    r = np.array(r); w = r[r > 0].sum(); ls = -r[r < 0].sum()
    return f"n={len(r):5d} win={np.mean(r > 0) * 100:4.1f}% avgR={r.mean():+.3f} PF={(w / ls if ls else 9):.2f}"


if __name__ == "__main__":
    data = {s: prep(s) for s in SYMS}
    allsig = {s: signals(data[s]) for s in SYMS}
    t_all = data["BTCUSD"].time.values
    mid = (t_all[0] + t_all[-1]) // 2
    print(f"{TF} candles, {DAYS} days; fee {FEE * 100:.3f}%/fill; hold <= {HOLD} candles; first half = dev, second half = unseen")
    for name in allsig["BTCUSD"]:
        for tp_r in (1.0, 1.5, 2.0):
            for mk_ in (False, True):
                res = []
                for s in SYMS:
                    side, stop = allsig[s][name]
                    res += simulate(data[s], side, stop, tp_r, mk_)
                res.sort()
                print(f"{name:30s} TP={tp_r}R {'maker-in/out' if mk_ else 'taker       '} dev: {stats(res, 0, mid)} | unseen: {stats(res, mid, 1e12)}", flush=True)
