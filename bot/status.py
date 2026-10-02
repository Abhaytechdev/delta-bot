"""Status from the terminal.

  python -m bot.status             balance, positions, today's P&L, bot state
  python -m bot.status --trades N  also list the last N closed trades
"""

import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import load_config, load_secrets
from bot.db import DB
from bot.exchange import Exchange
from bot.risk import DAY_HALT_KEY, DAY_START_EQUITY_KEY, HALT_KEY


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=5)
    args = ap.parse_args()

    cfg = load_config()
    tz = ZoneInfo(cfg["report"]["timezone"])
    db = DB(cfg["storage"]["db_path"])
    ex = Exchange(cfg, load_secrets())

    bal, avail = ex.balance_usd()
    upnl = ex.unrealized_pnl()
    start = float(db.get(DAY_START_EQUITY_KEY, bal + upnl))
    state = "HALTED (kill switch)" if db.get(HALT_KEY) == "1" else \
        "PAUSED for today (daily loss limit)" if db.get(DAY_HALT_KEY) == "1" else "trading"
    print(f"Environment : {cfg['exchange']['environment']}   state: {state}")
    print(f"Balance     : {bal:.2f} USD (available {avail:.2f}), unrealized {upnl:+.2f}")
    print(f"Today       : {bal + upnl - start:+.2f} USD ({(bal + upnl - start) / start * 100 if start else 0:+.2f}%) "
          f"vs day start {start:.2f}")
    print(f"News        : {db.get('news', 'n/a')}")

    trades = {t["symbol"]: t for t in db.open_trades()}
    positions = ex.positions()
    print(f"\nOpen positions ({len(positions)}):")
    for p in positions:
        t = trades.get(p.symbol)
        mark = ex.mark_price(p.symbol)
        extra = (f" SL {t['sl_current'] or t['stop_loss']:.2f} TP1 {t['tp1'] or 0:.2f}"
                 f"{' (done)' if t['tp1_done'] else ''} runner {t['take_profit']:.2f} "
                 f"age {(time.time() - t['opened_at']) / 3600:.1f}h [{t['reason']}]") if t else " (not opened by bot)"
        print(f"  {p.symbol} {'LONG' if p.size > 0 else 'SHORT'} x{abs(p.size)} entry {p.entry_price} mark {mark:.2f}{extra}")

    rows = db.conn.execute("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                           (args.trades,)).fetchall()
    print(f"\nLast {len(rows)} closed trades:")
    for t in rows:
        when = datetime.fromtimestamp(t["closed_at"], tz).strftime("%m-%d %H:%M")
        print(f"  {when} {t['symbol']} {t['side']} x{t['size']} {t['entry_price']} -> {t['exit_price']} "
              f"{t['exit_reason']} pnl {t['pnl']:+.2f}")

    print("\nRecent events:")
    for e in db.conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 8").fetchall()[::-1]:
        msg = e["msg"].split("<html>")[0].strip()[:160]
        print(f"  {datetime.fromtimestamp(e['ts'], tz).strftime('%m-%d %H:%M')} {e['level']:5} {msg}")


if __name__ == "__main__":
    main()
