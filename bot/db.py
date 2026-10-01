"""SQLite storage: trades, events, equity snapshots, and key/value state."""

import sqlite3
import time
from pathlib import Path

from bot.config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size INTEGER NOT NULL,
    contract_value REAL NOT NULL,
    entry_price REAL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    risk_usd REAL NOT NULL,
    reason TEXT,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed
    opened_at REAL NOT NULL,
    closed_at REAL,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,                                  -- net of fees, USD
    fees REAL
);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL, balance REAL NOT NULL, unrealized REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY, value TEXT
);
"""


class DB:
    def __init__(self, path: str | Path):
        path = Path(path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # state
    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?, ?)", (key, str(value)))

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM state WHERE key=?", (key,))

    # events / equity
    def event(self, level: str, msg: str) -> None:
        self.conn.execute("INSERT INTO events VALUES(?, ?, ?)", (time.time(), level, msg))

    def snapshot(self, balance: float, unrealized: float) -> None:
        self.conn.execute("INSERT INTO equity VALUES(?, ?, ?)", (time.time(), balance, unrealized))

    # trades
    def open_trade(self, **t) -> int:
        cols = ", ".join(t)
        q = ", ".join("?" * len(t))
        return self.conn.execute(f"INSERT INTO trades({cols}) VALUES({q})", tuple(t.values())).lastrowid

    def update_trade(self, trade_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))

    def open_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY opened_at").fetchall()

    def trades_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND closed_at>=? AND closed_at<? ORDER BY closed_at",
            (start, end),
        ).fetchall()

    def equity_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM equity WHERE ts>=? AND ts<? ORDER BY ts", (start, end)
        ).fetchall()
