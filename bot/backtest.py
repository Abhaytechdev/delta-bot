"""Backtest the technical strategy on real historical candles.

  python -m bot.backtest                       # 730 days, config timeframe and settings
  python -m bot.backtest --tf 15m --rr 3       # try variants without touching config.yaml

Data: Binance public spot klines (BTCUSDT/ETHUSDT) as a proxy for Delta perps
(Delta production API is off-limits until "go live"). Cached in data/.
News is not simulated (no historical headlines): this tests the technical core.

Fills: signal on candle close -> enter next candle open. SL/TP checked on each
candle's high/low; if both are touched in the same candle, assume the stop hit first.
Fees: Delta taker 0.05% + 18% GST on entry and exit; extra slippage on stop exits.
"""

import argparse
import copy
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from bot.config import ROOT, load_config
from bot.exchange import RESOLUTION_SECONDS
from bot.indicators import add_indicators
from bot.risk import ist_day, size_position
from bot.strategy import evaluate

BINANCE = "https://data-api.binance.vision/api/v3/klines?symbol={s}&interval={tf}&limit=1000&startTime={t}"
SYMBOL_MAP = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}
CONTRACT_VALUE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
FEE = 0.0005 * 1.18          # taker + GST, per side
STOP_SLIPPAGE = 0.0002       # extra adverse fill on stop-loss exits
START_BALANCE = 1000.0


def load_candles(symbol: str, tf: str, days: int) -> pd.DataFrame:
    path = ROOT / "data" / f"binance_{SYMBOL_MAP[symbol]}_{tf}_{days}d.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        end = int(time.time() * 1000)
        t, rows = end - days * 86400 * 1000, []
        while t < end:
            data = json.load(urllib.request.urlopen(BINANCE.format(s=SYMBOL_MAP[symbol], tf=tf, t=t), timeout=30))
            if not data:
                break
            rows += [[r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in data]
            t = data[-1][0] + 1
        rows = [r for r in rows if r[0] + RESOLUTION_SECONDS[tf] <= end // 1000]  # closed candles only
        path.write_text(json.dumps(rows))
    return pd.DataFrame(json.loads(path.read_text()), columns=["time", "open", "high", "low", "close"])


@dataclass
class Trade:
    symbol: str
    side: str
    size: int
    entry: float
    sl: float
    tp: float
    risk_usd: float
    opened: int
    exit: float = 0.0
    closed: int = 0
    reason: str = ""
    pnl: float = 0.0
    r: float = 0.0


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity: list[tuple[int, float]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


def run(cfg: dict, days: int = 730) -> Result:
    tf = cfg["trading"]["timeframe"]
    pairs = cfg["trading"]["pairs"]
    risk = cfg["risk"]
    p = cfg["strategy"]
    max_hold = risk["max_hold_hours"] * 3600
    frames = {s: add_indicators(load_candles(s, tf, days), p).set_index("time") for s in pairs}
    times = sorted(set.intersection(*(set(f.index) for f in frames.values())))
    window = p["ema_slow"] + 2

    res = Result()
    balance = START_BALANCE
    open_: dict[str, Trade] = {}
    pending: dict[str, object] = {}
    day, day_start, day_halt = None, balance, False
    pos = {s: frames[s].index.get_indexer(times) for s in pairs}

    def skip(why):
        res.skipped[why] = res.skipped.get(why, 0) + 1

    for i, ts in enumerate(times):
        d = ist_day(ts)
        if d != day:
            day, day_start, day_halt = d, balance, False
        for s in pairs:
            bar = frames[s].iloc[pos[s][i]]
            # 1) fill pending entry at this candle's open
            sig = pending.pop(s, None)
            if sig is not None and s not in open_:
                long = sig.side == "buy"
                entry = bar.open
                if (long and not sig.stop_loss < entry < sig.take_profit) or \
                   (not long and not sig.take_profit < entry < sig.stop_loss):
                    skip("gapped past SL/TP")
                else:
                    notional = sum(t.size * CONTRACT_VALUE[t.symbol] * t.entry for t in open_.values())
                    sz = size_position(balance, entry, sig.stop_loss, CONTRACT_VALUE[s], risk, notional)
                    if sz.contracts < 1:
                        skip("size < 1 contract")
                    else:
                        balance -= entry * sz.contracts * CONTRACT_VALUE[s] * FEE
                        open_[s] = Trade(s, sig.side, sz.contracts, entry, sig.stop_loss, sig.take_profit,
                                         sz.risk_usd, int(ts))
            # 2) manage open trade on this candle
            t = open_.get(s)
            if t:
                long = t.side == "buy"
                hit_sl = bar.low <= t.sl if long else bar.high >= t.sl
                hit_tp = bar.high >= t.tp if long else bar.low <= t.tp
                exit_px = reason = None
                if hit_sl:
                    gap = bar.open if (long and bar.open < t.sl) or (not long and bar.open > t.sl) else t.sl
                    exit_px, reason = gap * (1 - STOP_SLIPPAGE if long else 1 + STOP_SLIPPAGE), "stop_loss"
                elif hit_tp:
                    exit_px, reason = t.tp, "take_profit"
                elif ts + RESOLUTION_SECONDS[tf] - t.opened >= max_hold:
                    exit_px, reason = bar.close, "max_hold"
                if exit_px is not None:
                    cv = CONTRACT_VALUE[s]
                    gross = (exit_px - t.entry) * (1 if long else -1) * t.size * cv
                    fees = (t.entry + exit_px) * t.size * cv * FEE
                    balance += gross - exit_px * t.size * cv * FEE
                    t.exit, t.closed, t.reason, t.pnl = exit_px, int(ts), reason, gross - fees
                    t.r = t.pnl / t.risk_usd
                    res.trades.append(t)
                    del open_[s]
        # 3) daily loss limit on realised + unrealised equity at the close
        unreal = sum((frames[t.symbol].iloc[pos[t.symbol][i]].close - t.entry) * (1 if t.side == "buy" else -1)
                     * t.size * CONTRACT_VALUE[t.symbol] for t in open_.values())
        equity = balance + unreal
        res.equity.append((int(ts), equity))
        if (day_start - equity) / day_start * 100 >= risk["daily_loss_limit_pct"]:
            day_halt = True
        # 4) signals on this candle's close, entered next candle
        if i + 1 < len(times):
            for s in pairs:
                j = pos[s][i]
                if j < window or s in open_:
                    continue
                sig = evaluate(frames[s].iloc[j - window + 1:j + 1], cfg)
                if not sig:
                    continue
                if day_halt:
                    skip("daily loss limit")
                elif len(open_) + len(pending) >= risk["max_open_positions"]:
                    skip("max positions")
                else:
                    pending[s] = sig
    return res


def stats(res: Result, days: int) -> dict:
    tr = res.trades
    eq = pd.Series([e for _, e in res.equity])
    dd = (1 - eq / eq.cummax()).max() if len(eq) else 0
    wins = [t for t in tr if t.pnl > 0]
    gp, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in tr if t.pnl <= 0)
    final = eq.iloc[-1] if len(eq) else START_BALANCE
    years = days / 365
    return {
        "trades": len(tr),
        "per_week": len(tr) / (days / 7),
        "win_rate": len(wins) / len(tr) * 100 if tr else 0,
        "avg_R": sum(t.r for t in tr) / len(tr) if tr else 0,
        "profit_factor": gp / gl if gl else math.inf,
        "return_pct": (final / START_BALANCE - 1) * 100,
        "cagr_pct": ((final / START_BALANCE) ** (1 / years) - 1) * 100 if final > 0 else -100,
        "max_dd_pct": dd * 100,
        "exits": {r: sum(t.reason == r for t in tr) for r in ("take_profit", "stop_loss", "max_hold")},
        "worst_streak": max((len(s) for s in "".join("L" if t.pnl <= 0 else "W" for t in tr).split("W")), default=0),
    }


def fmt(name: str, s: dict) -> str:
    return (f"{name:28} trades={s['trades']:4} ({s['per_week']:.1f}/wk)  win={s['win_rate']:5.1f}%  "
            f"avgR={s['avg_R']:+.3f}  PF={s['profit_factor']:.2f}  return={s['return_pct']:+7.1f}%  "
            f"maxDD={s['max_dd_pct']:5.1f}%  streak={s['worst_streak']}  exits={s['exits']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--tf", help="override timeframe, e.g. 15m")
    ap.add_argument("--rr", type=float, help="override reward:risk")
    ap.add_argument("--atr", type=float, help="override stop ATR multiple")
    ap.add_argument("--split", action="store_true", help="also report each half of the period")
    ap.add_argument("--trades", type=int, default=0, help="print the last N trades")
    args = ap.parse_args()

    cfg = copy.deepcopy(load_config())
    if args.tf:
        cfg["trading"]["timeframe"] = args.tf
    if args.rr:
        cfg["risk"]["reward_risk"] = args.rr
    if args.atr:
        cfg["risk"]["stop_atr_mult"] = args.atr
    res = run(cfg, args.days)
    name = f"{cfg['trading']['timeframe']} {cfg['risk']['stop_atr_mult']}xATR 1:{cfg['risk']['reward_risk']:g}"
    print(fmt(name, stats(res, args.days)))
    for s in cfg["trading"]["pairs"]:
        sub = Result([t for t in res.trades if t.symbol == s], res.equity)
        st = stats(sub, args.days)
        print(f"   {s}: trades={st['trades']} win={st['win_rate']:.1f}% avgR={st['avg_R']:+.3f} "
              f"pnl={sum(t.pnl for t in sub.trades):+.2f} USD")
    if res.skipped:
        print("   skipped signals:", res.skipped)
    if args.split and res.equity:
        mid = res.equity[len(res.equity) // 2][0]
        for label, cond in (("first half", lambda t: t.opened < mid), ("second half", lambda t: t.opened >= mid)):
            tr = [t for t in res.trades if cond(t)]
            if tr:
                print(f"   {label}: trades={len(tr)} win={sum(t.pnl > 0 for t in tr) / len(tr) * 100:.1f}% "
                      f"avgR={sum(t.r for t in tr) / len(tr):+.3f} pnl={sum(t.pnl for t in tr):+.2f} USD")
    for t in res.trades[-args.trades:] if args.trades else []:
        when = datetime.fromtimestamp(t.opened, timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"   {when} {t.symbol} {t.side} x{t.size} {t.entry:.2f}->{t.exit:.2f} {t.reason} R={t.r:+.2f}")


if __name__ == "__main__":
    main()
