"""Backtest the knowledge-driven swing strategy on real historical candles.

  python -m bot.backtest                          # all timeframes, 3 years
  python -m bot.backtest --tf 1h --trades 20      # one timeframe, list trades

Data: Binance public spot klines (BTCUSDT/ETHUSDT) as a proxy for Delta perps
(Delta production API is off-limits until "go live"). Cached in data/.
News is not simulated (no historical headlines).

Honesty rules:
- Signal on candle close, entry at the next candle's open.
- Swings are only known after confirmation (no lookahead).
- Same candle touches stop and target -> stop assumed first.
- Taker fee + GST on every fill, extra slippage on stop exits.
- Last `--oos` fraction of the period is out-of-sample: judge the strategy on that.
"""

import argparse
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from bot import brain, manage
from bot.config import ROOT, load_config
from bot.exchange import RESOLUTION_SECONDS
from bot.risk import ist_day, size_position

BINANCE = "https://data-api.binance.vision/api/v3/klines?symbol={s}&interval={tf}&limit=1000&startTime={t}"
SYMBOL_MAP = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT", "SOLUSD": "SOLUSDT", "XRPUSD": "XRPUSDT",
              "ADAUSD": "ADAUSDT", "DOGEUSD": "DOGEUSDT"}
CONTRACT_VALUE = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1, "XRPUSD": 1, "ADAUSD": 1, "DOGEUSD": 100}
FEE = 0.0005 * 1.18          # taker + GST, per fill
STOP_SLIPPAGE = 0.0002
FUNDING_ON = True            # charge perpetual funding while a position is open


FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&limit=1000&startTime={t}"


def load_funding(symbol: str, days: int) -> pd.Series:
    """Perpetual funding rates (Binance USDT perps as a proxy), indexed by funding time (s)."""
    path = ROOT / "data" / f"funding_{SYMBOL_MAP[symbol]}_{days}d.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        end = int(time.time() * 1000)
        t, rows = end - days * 86400 * 1000, []
        while t < end:
            data = json.load(urllib.request.urlopen(FUNDING.format(s=SYMBOL_MAP[symbol], t=t), timeout=30))
            if not data:
                break
            rows += [[r["fundingTime"] // 1000, float(r["fundingRate"])] for r in data]
            t = data[-1]["fundingTime"] + 1
        path.write_text(json.dumps(rows))
    rows = json.loads(path.read_text())
    return pd.Series([r[1] for r in rows], index=[r[0] for r in rows], dtype=float)


def load_candles(symbol: str, tf: str, days: int) -> pd.DataFrame:
    path = ROOT / "data" / f"binance_{SYMBOL_MAP[symbol]}_{tf}_{days}d_v2.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        end = int(time.time() * 1000)
        t, rows = end - days * 86400 * 1000, []
        while t < end:
            data = json.load(urllib.request.urlopen(BINANCE.format(s=SYMBOL_MAP[symbol], tf=tf, t=t), timeout=30))
            if not data:
                break
            rows += [[r[0] // 1000, *map(float, r[1:6])] for r in data]
            t = data[-1][0] + 1
        rows = [r for r in rows if r[0] + RESOLUTION_SECONDS[tf] <= end // 1000]
        path.write_text(json.dumps(rows))
    return pd.DataFrame(json.loads(path.read_text()), columns=["time", "open", "high", "low", "close", "volume"])


@dataclass
class Trade:
    symbol: str
    side: str
    size: int
    entry: float
    sl: float
    tp1: float
    runner: float
    risk_usd: float
    opened: int
    reason: str
    sl0: float = 0.0       # initial stop
    best: float = 0.0      # best price reached so far
    bars: int = 0          # candles held
    partials_done: set = field(default_factory=set)
    left: int = 0          # contracts still open
    tp1_done: bool = False
    pnl: float = 0.0       # realised, net of fees
    closed: int = 0
    exit_reason: str = ""

    @property
    def r(self) -> float:
        return self.pnl / self.risk_usd


def run(cfg: dict, k: dict, tf: str, days: int, balance: float,
        setup_tf: str | None = None) -> tuple[list[Trade], list[tuple[int, float]], dict]:
    """tf = entry timeframe; setup_tf (optional) = timeframe for trend/zones context."""
    pairs, risk = cfg["trading"]["pairs"], cfg["risk"]
    m = k["management"]
    tfs = RESOLUTION_SECONDS[tf]
    frames = {}
    for s in pairs:
        if setup_tf:
            frames[s] = brain.analyze(load_candles(s, tf, days), k, tfs,
                                      load_candles(s, setup_tf, days), RESOLUTION_SECONDS[setup_tf])
        else:
            frames[s] = brain.analyze(load_candles(s, tf, days), k, tfs)
    rows = {s: list(frames[s].itertuples(index=False)) for s in pairs}
    funding = {}  # symbol -> {candle time: summed funding rate paid by longs during that candle}
    if FUNDING_ON:
        for s in pairs:
            f = load_funding(s, days)
            bucket = (f.index // tfs) * tfs
            funding[s] = f.groupby(bucket).sum().to_dict()
    idx = {s: {t: i for i, t in enumerate(frames[s]["time"])} for s in pairs}
    times = sorted(set.intersection(*(set(idx[s]) for s in pairs)))

    trades: list[Trade] = []
    equity: list[tuple[int, float]] = []
    skipped: dict[str, int] = {}
    open_: dict[str, Trade] = {}
    pending: dict[str, brain.Setup] = {}
    day, day_start, day_halt = None, balance, False

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    def fill(t: Trade, qty: int, px: float, cv: float) -> None:
        nonlocal balance
        d = 1 if t.side == "buy" else -1
        p = (px - t.entry) * d * qty * cv - px * qty * cv * FEE
        t.pnl += p
        balance += p
        t.left -= qty

    for ts in times:
        d_ = ist_day(ts)
        if d_ != day:
            day, day_start, day_halt = d_, balance, False
        for s in pairs:
            i, cv = idx[s][ts], CONTRACT_VALUE[s]
            bar = rows[s][i]
            # 1) fill pending entry at the open
            st = pending.pop(s, None)
            if st is not None and s not in open_:
                long = st.side == "buy"
                entry = float(bar.open)
                dist = (entry - st.stop_loss) * (1 if long else -1)
                if dist <= 0:
                    skip("gapped through stop")
                else:
                    notional = sum(t.left * CONTRACT_VALUE[t.symbol] * t.entry for t in open_.values())
                    r_cfg = dict(risk, risk_per_trade_pct=risk["risk_per_trade_pct"] * st.risk_mult)
                    sz = size_position(balance, entry, st.stop_loss, cv, r_cfg, notional)
                    if sz.contracts < 1:
                        skip("size < 1 contract")
                    else:
                        d = 1 if long else -1
                        t = Trade(s, st.side, sz.contracts, entry, st.stop_loss, entry + d * dist * m["tp1_r"],
                                  entry + d * dist * manage.final_target_r(m), sz.risk_usd, int(ts), st.reason,
                                  sl0=st.stop_loss, best=entry, left=sz.contracts)
                        fee = entry * sz.contracts * cv * FEE
                        t.pnl -= fee
                        balance -= fee
                        open_[s] = t
            # 2) manage the open trade inside this candle (stop first = conservative)
            t = open_.get(s)
            if t:
                long = t.side == "buy"
                hit = (lambda px: bar.low <= px) if long else (lambda px: bar.high >= px)
                reach = (lambda px: bar.high >= px) if long else (lambda px: bar.low <= px)
                if hit(t.sl):
                    gapped = (bar.open < t.sl) if long else (bar.open > t.sl)
                    px = (bar.open if gapped else t.sl) * ((1 - STOP_SLIPPAGE) if long else (1 + STOP_SLIPPAGE))
                    fill(t, t.left, px, cv)
                    t.exit_reason = "breakeven/trail" if t.tp1_done else "stop_loss"
                else:
                    for n, (r_lvl, frac) in enumerate(m.get("partials") or []):
                        px = manage.level(t.side, t.entry, t.sl0, r_lvl)
                        if n not in t.partials_done and reach(px):
                            t.partials_done.add(n)
                            part = int(round(t.size * frac))
                            if 0 < part < t.left:
                                fill(t, part, px, cv)
                    if reach(t.runner):
                        fill(t, t.left, t.runner, cv)
                        t.exit_reason = "target"
                    elif ts + tfs - t.opened >= m["max_hold_days"] * 86400:
                        fill(t, t.left, float(bar.close), cv)
                        t.exit_reason = "max_hold"
                if t.left == 0:
                    t.closed = int(ts)
                    trades.append(t)
                    del open_[s]
                else:
                    fr = funding.get(s, {}).get(int(ts), 0.0)  # longs pay positive funding, shorts receive it
                    if fr:
                        cost = fr * t.left * cv * float(bar.close) * (1 if long else -1)
                        t.pnl -= cost
                        balance -= cost
                    t.best = max(t.best, bar.high) if long else min(t.best, bar.low)
                    t.bars += 1
                    if (m.get("stale_candles") and t.bars >= m["stale_candles"]
                            and manage.r_multiple(t.side, t.entry, t.sl0, t.best) < m["stale_r"]):
                        fill(t, t.left, float(bar.close), cv)
                        t.exit_reason = "stale"
                        t.closed = int(ts)
                        trades.append(t)
                        del open_[s]
                        continue
                    if any(m.get(x) is not None for x in
                           ("exit_opposite_setup", "exit_against_closes", "exit_ema_cross", "exit_giveback")):
                        opp = False
                        if m.get("exit_opposite_setup") is not None:
                            o = brain.setup_from_rows(rows[s], idx[s][ts], k)
                            opp = o is not None and o.side != t.side
                        why = manage.reversal_exit(t.side, t.entry, t.sl0, t.best, rows[s], idx[s][ts], opp, m)
                        if why:
                            fill(t, t.left, float(bar.close), cv)
                            t.exit_reason = why
                            t.closed = int(ts)
                            trades.append(t)
                            del open_[s]
                            continue
                    new_sl = manage.new_stop(t.side, t.entry, t.sl0, t.sl, t.best, bar, m, k,
                                             rows[s][max(0, idx[s][ts] - 7):idx[s][ts] + 1])  # next candle
                    if new_sl != t.sl:
                        t.sl = new_sl
                        t.tp1_done = (new_sl - t.entry) * (1 if long else -1) >= 0
        # 3) equity and daily loss limit
        unreal = 0.0
        for t in open_.values():
            c = float(rows[t.symbol][idx[t.symbol][ts]].close)
            unreal += (c - t.entry) * (1 if t.side == "buy" else -1) * t.left * CONTRACT_VALUE[t.symbol]
        eq = balance + unreal
        equity.append((int(ts), eq))
        if (day_start - eq) / day_start * 100 >= risk["daily_loss_limit_pct"]:
            day_halt = True
        # 4) new setups on this close, entered at the next open
        for s in pairs:
            if s in open_ or s in pending:
                continue
            st = brain.setup_from_rows(rows[s], idx[s][ts], k)
            if not st:
                continue
            if day_halt:
                skip("daily loss limit")
            elif len(open_) + len(pending) >= risk["max_open_positions"]:
                skip("max positions")
            else:
                pending[s] = st
    return trades, equity, skipped


def summarize(trades: list[Trade], equity: list[tuple[int, float]], start: int, end: int) -> dict:
    tr = [t for t in trades if start <= t.opened < end]
    eq = pd.Series([e for ts, e in equity if start <= ts < end])
    if not tr or eq.empty:
        return {"trades": 0}
    wins = [t for t in tr if t.pnl > 0]
    gp, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in tr if t.pnl <= 0)
    weeks = (end - start) / 604800
    streak = max(len(x) for x in "".join("L" if t.pnl <= 0 else "W" for t in tr).split("W"))
    return {
        "trades": len(tr), "per_week": len(tr) / weeks,
        "win_rate": len(wins) / len(tr) * 100,
        "avg_R": sum(t.r for t in tr) / len(tr),
        "avg_win_R": sum(t.r for t in wins) / len(wins) if wins else 0,
        "best_R": max(t.r for t in tr),
        "pf": gp / gl if gl else math.inf,
        "return_pct": (eq.iloc[-1] / eq.iloc[0] - 1) * 100,
        "max_dd_pct": (1 - eq / eq.cummax()).max() * 100,
        "streak": streak,
    }


def line(label: str, s: dict) -> str:
    if not s["trades"]:
        return f"  {label:14} no trades"
    return (f"  {label:14} trades={s['trades']:4} ({s['per_week']:.1f}/wk) win={s['win_rate']:5.1f}% "
            f"avgR={s['avg_R']:+.3f} avgWin={s['avg_win_R']:.2f}R best={s['best_R']:.1f}R PF={s['pf']:.2f} "
            f"return={s['return_pct']:+6.1f}% maxDD={s['max_dd_pct']:5.1f}% losing-streak={s['streak']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="+", default=["30m", "1h", "2h", "4h"], help="entry timeframe(s)")
    ap.add_argument("--pairs", nargs="+", help="override pairs, e.g. BTCUSD SOLUSD")
    ap.add_argument("--setup-tf", help="context timeframe for trend/zones, e.g. 4h")
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--oos", type=float, default=0.3, help="out-of-sample fraction at the end")
    ap.add_argument("--trades", type=int, default=0)
    args = ap.parse_args()

    cfg, k = load_config(), brain.load_knowledge()
    if args.pairs:
        cfg["trading"]["pairs"] = args.pairs
    for tf in args.tf:
        trades, equity, skipped = run(cfg, k, tf, args.days, args.balance, args.setup_tf)
        t0, t1 = equity[0][0], equity[-1][0] + 1
        cut = int(t0 + (t1 - t0) * (1 - args.oos))
        print(f"{args.setup_tf + ' setup / ' if args.setup_tf else ''}{tf} entry:")
        print(line("in-sample", summarize(trades, equity, t0, cut)))
        print(line("OUT-OF-SAMPLE", summarize(trades, equity, cut, t1)))
        exits: dict[str, int] = {}
        for t in trades:
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
        print(f"  exits={exits} skipped={skipped}")
        for t in trades[-args.trades:] if args.trades else []:
            when = datetime.fromtimestamp(t.opened, timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"    {when} {t.symbol} {t.side} R={t.r:+.2f} {t.exit_reason:15} [{t.reason}]")


if __name__ == "__main__":
    main()
