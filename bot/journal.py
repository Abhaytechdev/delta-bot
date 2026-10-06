"""Trade journal (research only, never affects trading).

While a trade is open the bot samples it about once a minute (`sample`) and at each 4h close (`bar`),
storing price, R, stop, and market context. After the trade closes, `review` works out from exchange
candles how far it went for and against us (MFE / MAE in R), how long it took to reach 1R / 2R, and what
price did after the exit (did the trail/stop leave profit on the table?). Run:

    python -m bot.journal            # review closed trades that have no review yet
    python -m bot.journal --all      # redo every closed trade

Everything here is wrapped by the caller in try/except: a journal failure must never stop trading.
"""

import argparse
import json
import time

from bot import manage
from bot.config import load_config
from bot.db import DB
from bot.exchange import Exchange

SAMPLE_S = 60
AFTER_S = (4 * 3600, 24 * 3600, 72 * 3600)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_track (
    trade_id INTEGER NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, r REAL NOT NULL,
    sl REAL, unrealized REAL, ctx TEXT
);
CREATE INDEX IF NOT EXISTS trade_track_id ON trade_track(trade_id, ts);
CREATE TABLE IF NOT EXISTS trade_review (
    trade_id INTEGER PRIMARY KEY, mfe_r REAL, mae_r REAL, final_r REAL, minutes_to_1r REAL,
    minutes_to_2r REAL, hours_open REAL, after_4h_r REAL, after_24h_r REAL, after_72h_r REAL,
    best_after_72h_r REAL, note TEXT
);
"""


def ensure(db: DB) -> None:
    db.conn.executescript(SCHEMA)


def sample(db: DB, t, price: float, unrealized: float, last: dict, ctx: dict | None = None) -> None:
    """One row per open trade about every SAMPLE_S seconds (`last` is the caller's trade_id -> ts memory)."""
    now = time.time()
    if ctx is None and now - last.get(t["id"], 0) < SAMPLE_S:
        return
    last[t["id"]] = now
    sl0 = abs(t["entry_price"] - t["stop_loss"])
    d = 1 if t["side"] == "buy" else -1
    r = (price - t["entry_price"]) * d / sl0 if sl0 else 0.0
    db.conn.execute("INSERT INTO trade_track VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (t["id"], now, price, r, t["sl_current"] or t["stop_loss"], unrealized,
                     json.dumps(ctx) if ctx else None))


def review(db: DB, ex: Exchange, t) -> dict:
    sl0 = abs(t["entry_price"] - t["stop_loss"])
    d = 1 if t["side"] == "buy" else -1
    rr = lambda p: (p - t["entry_price"]) * d / sl0
    opened, closed = t["opened_at"], t["closed_at"]
    span_h = (time.time() - opened) / 3600 + 2
    candles = ex.candles(t["symbol"], "15m", min(int(span_h * 4) + 8, 6000))
    during = [c for c in candles if opened - 900 <= c["time"] <= closed]
    fav = lambda c: c["high"] if d == 1 else c["low"]
    adv = lambda c: c["low"] if d == 1 else c["high"]
    mfe = max([rr(fav(c)) for c in during] + [0.0])
    mae = min([rr(adv(c)) for c in during] + [0.0])

    def minutes_to(level):
        for c in during:
            if rr(fav(c)) >= level:
                return max(0.0, (c["time"] - opened) / 60)
        return None

    after = {}
    for s in AFTER_S:
        cs = [c for c in candles if closed <= c["time"] <= closed + s]
        if candles and candles[-1]["time"] + 900 < closed + s:
            after[s] = (None, None)  # that window is not over yet
        else:
            after[s] = (rr(cs[-1]["close"]) if cs else None, max((rr(fav(c)) for c in cs), default=None))
    final = (t["pnl"] / t["risk_usd"]) if t["risk_usd"] and t["pnl"] is not None else None
    row = dict(trade_id=t["id"], mfe_r=mfe, mae_r=mae, final_r=final, minutes_to_1r=minutes_to(1.0),
               minutes_to_2r=minutes_to(2.0), hours_open=(closed - opened) / 3600,
               after_4h_r=after[AFTER_S[0]][0], after_24h_r=after[AFTER_S[1]][0], after_72h_r=after[AFTER_S[2]][0],
               best_after_72h_r=after[AFTER_S[2]][1], note=verdict(mfe, mae, final, t["exit_reason"]))
    db.conn.execute(f"INSERT OR REPLACE INTO trade_review({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                    tuple(row.values()))
    return row


def verdict(mfe: float, mae: float, final: float | None, exit_reason: str | None) -> str:
    """Plain-words reading of the numbers. Descriptive only: one trade never justifies a rule change."""
    if final is None:
        return "no P&L"
    if final < 0 and mfe >= 1.0:
        return f"was +{mfe:.1f}R at best, then lost: a trail/breakeven earlier might have saved it (one trade, not proof)"
    if final < 0 and mfe < 0.5:
        return "never worked: entry was wrong or early; the stop did its job"
    if final > 0 and mfe - final > 2.0:
        return f"gave back {mfe - final:.1f}R from the peak (normal for a trailing exit)"
    if final > 0:
        return "kept most of the move"
    return "small loss/flat"


def fmt(t, r: dict) -> str:
    f = lambda v, p="+.2f": "n/a" if v is None else format(v, p)
    return (f"#{t['id']} {t['symbol']} {t['side']} {t['exit_reason']}: final {f(r['final_r'])}R | best {f(r['mfe_r'])}R, "
            f"worst {f(r['mae_r'])}R | 1R after {f(r['minutes_to_1r'], '.0f')} min, 2R after {f(r['minutes_to_2r'], '.0f')} min | "
            f"price after exit (4h/24h/72h): {f(r['after_4h_r'])}/{f(r['after_24h_r'])}/{f(r['after_72h_r'])}R "
            f"(best within 72h {f(r['best_after_72h_r'])}R)\n    -> {r['note']}")


def main() -> None:
    from bot.config import load_secrets
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="redo every closed trade, not just new ones")
    args = ap.parse_args()
    cfg = load_config()
    db, ex = DB(cfg["storage"]["db_path"]), Exchange(cfg, load_secrets())
    ensure(db)
    done = {r[0]: r for r in db.conn.execute(
        "SELECT trade_id, after_72h_r FROM trade_review")}
    for t in db.conn.execute("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at"):
        if not args.all and t["id"] in done and done[t["id"]][1] is not None:
            continue  # reviewed with the full 72h-after window
        if "test" in (t["exit_reason"] or "") or (t["exit_reason"] or "").startswith("manual_close") or not t["risk_usd"] or t["risk_usd"] < 0.5:
            continue
        print(fmt(t, review(db, ex, t)))


if __name__ == "__main__":
    main()
