"""Trade-flow scalping research (backtest only). Needs data/flow/ from research/flow_download.py.

Ideas, written down before testing (5-minute bars built from 1-minute trade-flow bars):
  F1 absorption    heavy aggressive selling over 15 min but price did not fall -> hidden buyers absorbing -> long
                   (mirror: heavy aggressive buying, price did not rise -> short)
  F2 flow_break    strong one-sided aggressive flow AND price breaks the 30-min high/low -> follow it
  F3 whales        big-trade (>= $100k BTC/ETH, $30k ADA) imbalance over 15 min is extreme -> follow it
  F4 divergence    price makes a new 1h high but cumulative delta (buy-sell) is lower than at the previous high
                   -> buyers are exhausted -> short (mirror for lows)
  F5 oi_flush      open interest drops sharply while price drops (longs liquidated) -> long; mirror for squeezes
Entry at the next 5m open. Stop = max(1.2 ATR, 0.3%). Target 1.5R. Hold <= 12 bars (1h).
dev = first half of the days, unseen = second half.
"""
import glob, os, sys
import numpy as np, pandas as pd
sys.argv = [sys.argv[0], "5m", "365"]
from research import scalp as S
from research import scalp2 as S2
S.HOLD = 12
S2.S.HOLD = 12
FLOW = os.path.join(os.path.dirname(__file__), "..", "data", "flow")
SYMS = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT", "ADAUSD": "ADAUSDT"}


def load(sym):
    fs = sorted(glob.glob(os.path.join(FLOW, f"{sym}_20*.csv")))
    m = pd.concat([pd.read_csv(f) for f in fs]).sort_values("minute").drop_duplicates("minute")
    m["bar"] = m["minute"] // 300 * 300
    g = m.groupby("bar")
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                      "buy": g["buy_qty"].sum(), "sell": g["sell_qty"].sum(), "n": g["n_trades"].sum(),
                      "bb": g["big_buy_usd"].sum(), "bs": g["big_sell_usd"].sum()})
    d = d[g["minute"].count() == 5].reset_index().rename(columns={"bar": "time"})
    mt = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(FLOW, f"metrics_{sym}_20*.csv")))])
    mt["time"] = (pd.to_datetime(mt["create_time"], utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().astype("int64")
    mt = mt.sort_values("time").drop_duplicates("time").set_index("time")
    d["oi"] = mt["sum_open_interest_value"].reindex(d["time"].values, method="ffill").values
    c, h, l = d.close, d.high, d.low
    pc = c.shift()
    d["atr"] = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).ewm(alpha=1 / 14, adjust=False).mean()
    return d


def ideas(d):
    c, o, h, l, atr = (d[k].values for k in ("close", "open", "high", "low", "atr"))
    tot = d.buy + d.sell
    delta = d.buy - d.sell
    out = {}
    stop_d = np.maximum(1.2 * atr, 0.003 * c)
    nan = np.full(len(d), np.nan)

    def pack(Lm, Sm):
        side = np.where(Lm, 1, np.where(Sm, -1, 0))
        stop = np.where(side == 1, c - stop_d, np.where(side == -1, c + stop_d, np.nan))
        return side, stop, nan

    imb15 = delta.rolling(3).sum() / tot.rolling(3).sum()          # -1..+1 over 15 min
    ret15 = (d.close / d.close.shift(3) - 1)
    q = lambda s, p: s.rolling(2000, min_periods=500).quantile(p).shift(1)
    sell_heavy = imb15 < q(imb15, 0.05); buy_heavy = imb15 > q(imb15, 0.95)
    out["F1_absorption"] = pack((sell_heavy & (ret15 >= 0)).values, (buy_heavy & (ret15 <= 0)).values)

    hi30, lo30 = d.high.shift().rolling(6).max(), d.low.shift().rolling(6).min()
    imb5 = delta / tot
    out["F2_flow_break"] = pack(((d.close > hi30) & (imb5 > q(imb5, 0.9))).values, ((d.close < lo30) & (imb5 < q(imb5, 0.1))).values)

    wh = ((d.bb - d.bs).rolling(3).sum())
    out["F3_whales"] = pack((wh > q(wh, 0.97)).values, (wh < q(wh, 0.03)).values)

    cd = delta.cumsum()
    newhi = d.high >= d.high.shift().rolling(12).max()
    newlo = d.low <= d.low.shift().rolling(12).min()
    prev_cd_hi = cd.where(newhi).ffill().shift()
    prev_cd_lo = cd.where(newlo).ffill().shift()
    out["F4_divergence"] = pack((newlo & (cd > prev_cd_lo)).values, (newhi & (cd < prev_cd_hi)).values)

    oi_chg = d.oi.pct_change(3)
    pr = d.close.pct_change(3)
    flush_l = (oi_chg < q(oi_chg, 0.03)) & (pr < 0)
    squeeze = (oi_chg < q(oi_chg, 0.03)) & (pr > 0)
    out["F5_oi_flush"] = pack(flush_l.values, squeeze.values)
    return out


if __name__ == "__main__":
    data = {s: load(v) for s, v in SYMS.items()}
    sigs = {s: ideas(data[s]) for s in SYMS}
    t = data["BTCUSD"].time.values
    mid = (t[0] + t[-1]) // 2
    print(f"{len(data['BTCUSD'])} five-minute bars per coin, {(t[-1] - t[0]) / 86400:.0f} days; dev = first half, unseen = second half")
    for name in sigs["BTCUSD"]:
        for fees in ("zero", "maker", "taker"):
            res = []
            for s in SYMS:
                side, stop, tgt = sigs[s][name]
                res += S2.sim(data[s], side, stop, tgt, fees, 0.0015)
            res.sort()
            print(f"{name:16s} {fees:5s} dev: {S.stats(res, 0, mid)} | unseen: {S.stats(res, mid, 1e12)}", flush=True)
