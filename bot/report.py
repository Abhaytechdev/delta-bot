"""Daily report (cron 23:55 IST): python -m bot.report [--date YYYY-MM-DD]

Summarises the day's closed trades (P&L, win rate, drawdown, best/worst trade),
adds Claude's analysis when ANTHROPIC_API_KEY is set, and saves it to reports/.
Suggestions only: nothing here changes the strategy or config.
"""

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot import llm
from bot.config import ROOT, load_config, load_secrets
from bot.db import DB

SYSTEM = (
    "You review a crypto swing-trading bot's day on Delta Exchange testnet. The bot trades BTCUSD/ETHUSD "
    "perpetuals on 4h candles using a knowledge base of market structure, candle context and trading "
    "psychology. Expected shape: many small losses, few large wins (TP1 at 2R books 30% and moves the stop "
    "to breakeven, the rest trails behind swings). Give a short, concrete analysis in Hinglish: what went "
    "right/wrong, whether losses look like normal variance or a pattern, and at most 3 suggestions. "
    "Suggestions only; never claim to change anything. One day is a tiny sample; say so when relevant."
)


def build(cfg: dict, day: str) -> tuple[str, str]:
    tz = ZoneInfo(cfg["report"]["timezone"])
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    s, e = start.timestamp(), (start + timedelta(days=1)).timestamp()
    db = DB(cfg["storage"]["db_path"])
    trades = db.trades_between(s, e)
    eq = [r["balance"] + r["unrealized"] for r in db.equity_between(s, e)]

    lines = [f"# Daily report {day} ({cfg['exchange']['environment']})", ""]
    pnl = sum(t["pnl"] or 0 for t in trades)
    wins = [t for t in trades if (t["pnl"] or 0) > 0]
    dd = 0.0
    peak = None
    for v in eq:
        peak = v if peak is None else max(peak, v)
        dd = max(dd, (peak - v) / peak * 100 if peak else 0)
    lines += [
        f"- Closed trades: {len(trades)}",
        f"- Net P&L: {pnl:+.2f} USD" + (f" ({pnl / eq[0] * 100:+.2f}% of start equity)" if eq else ""),
        f"- Win rate: {len(wins) / len(trades) * 100:.0f}%" if trades else "- Win rate: n/a",
        f"- Intraday max drawdown: {dd:.2f}%",
        f"- Equity: {eq[0]:.2f} -> {eq[-1]:.2f} USD" if eq else "- Equity: no snapshots",
    ]
    if trades:
        r = lambda t: (t["pnl"] or 0) / t["risk_usd"] if t["risk_usd"] else 0
        best, worst = max(trades, key=r), min(trades, key=r)
        lines += [f"- Best trade: {best['symbol']} {best['side']} {r(best):+.2f}R ({best['exit_reason']})",
                  f"- Worst trade: {worst['symbol']} {worst['side']} {r(worst):+.2f}R ({worst['exit_reason']})",
                  "", "## Trades", ""]
        for t in trades:
            lines.append(f"- {t['symbol']} {t['side']} x{t['size']} {t['entry_price']} -> {t['exit_price']} "
                         f"{t['exit_reason']} {r(t):+.2f}R ({t['pnl']:+.2f} USD) [{t['reason']}]")
    opened = db.conn.execute("SELECT COUNT(*) FROM trades WHERE opened_at>=? AND opened_at<?", (s, e)).fetchone()[0]
    events = db.conn.execute("SELECT level, msg FROM events WHERE ts>=? AND ts<? AND level!='INFO'", (s, e)).fetchall()
    lines += ["", f"Trades opened today: {opened}. Warnings/errors: {len(events)}"]
    lines += [f"- {ev['level']}: {ev['msg']}" for ev in events[:20]]
    return "\n".join(lines), pnl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="IST date, default today")
    args = ap.parse_args()
    cfg = load_config()
    day = args.date or datetime.now(ZoneInfo(cfg["report"]["timezone"])).strftime("%Y-%m-%d")
    body, _ = build(cfg, day)
    try:
        from bot.crowd_watch import summary
        body += "\n\n## Research: retail positioning forward test\n\n" + summary(cfg)
    except Exception as e:
        body += f"\n\n(retail forward test unavailable: {type(e).__name__})"
    analysis = llm.ask_text(load_secrets().anthropic_api_key, SYSTEM, body)
    body += "\n\n## Analysis (suggestions only)\n\n" + (analysis or "_Claude analysis skipped: set ANTHROPIC_API_KEY in .env to enable._")
    out = ROOT / cfg["report"]["dir"] / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body + "\n")
    print(body)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
