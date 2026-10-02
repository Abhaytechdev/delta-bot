"""Forward test of the retail long/short signal recorded by the live bot (research only).

  python -m bot.crowd_watch      # what happened 7 days after each reading?

Backtest finding to confirm: when retail is very long (180-day percentile >= 0.8),
price tends to fall over the next 7 days (knowledge/core.md, crowd research).
"""

import time

from bot.config import load_config, load_secrets
from bot.db import DB
from bot.exchange import Exchange

HORIZON = 7 * 86400


def summary(cfg: dict, ex: Exchange | None = None) -> str:
    db = DB(cfg["storage"]["db_path"])
    rows = db.conn.execute("SELECT * FROM crowd_obs WHERE pct IS NOT NULL ORDER BY ts").fetchall()
    total = len(rows)
    done = [r for r in rows if r["ts"] + HORIZON <= time.time()]
    if not done:
        return f"Retail signal forward test: {total} readings recorded, none older than 7 days yet."
    ex = ex or Exchange(cfg, load_secrets())
    closes = {}
    for sym in {r["symbol"] for r in done}:
        closes[sym] = {c["time"]: c["close"] for c in ex.candles(sym, "4h", 1300)}
    groups = {"retail very long (>=0.8)": [], "retail very short (<=0.2)": [], "neutral": []}
    for r in done:
        target = int(r["ts"] + HORIZON) // 14400 * 14400
        later = closes[r["symbol"]].get(target)
        if later is None:
            continue
        ret = later / r["price"] - 1
        key = "retail very long (>=0.8)" if r["pct"] >= 0.8 else "retail very short (<=0.2)" if r["pct"] <= 0.2 else "neutral"
        groups[key].append(ret)
    lines = [f"Retail signal forward test ({total} readings, {len(done)} with a 7-day result):"]
    for k, v in groups.items():
        if v:
            lines.append(f"  {k:26} n={len(v):3}  avg 7-day return {sum(v) / len(v) * 100:+.2f}%  "
                         f"down {sum(x < 0 for x in v) / len(v) * 100:.0f}% of the time")
    lines.append("  (expected from backtest: 'very long' lower than neutral)")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary(load_config()))
