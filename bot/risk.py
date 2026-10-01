"""Risk manager: position sizing and pre-trade checks.

Sizing: contracts = floor(balance * risk% / (stop distance * contract value)),
then capped so total notional (incl. open positions) <= balance * max_leverage. Risk can only go down, never up.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

HALT_KEY = "halt"  # kill switch (set by `python -m bot.stop`)
DAY_KEY = "day"
DAY_START_EQUITY_KEY = "day_start_equity"
DAY_HALT_KEY = "day_halted"


@dataclass(frozen=True)
class Sizing:
    contracts: int
    risk_usd: float
    notional_usd: float
    note: str = ""


def size_position(
    balance: float, price: float, stop_loss: float, contract_value: float, risk_cfg: dict,
    open_notional: float = 0.0,
) -> Sizing:
    stop_dist = abs(price - stop_loss)
    if balance <= 0 or stop_dist <= 0 or price <= 0:
        return Sizing(0, 0, 0, "invalid inputs")
    risk_budget = balance * risk_cfg["risk_per_trade_pct"] / 100
    per_contract_risk = stop_dist * contract_value
    contracts = math.floor(risk_budget / per_contract_risk)
    note = ""
    headroom = balance * risk_cfg["max_leverage"] - open_notional
    max_by_leverage = max(0, math.floor(headroom / (price * contract_value)))
    if contracts > max_by_leverage:
        contracts, note = max_by_leverage, "capped by max leverage"
    if contracts < 1:
        return Sizing(0, 0, 0, "1 contract would exceed risk budget")
    return Sizing(contracts, contracts * per_contract_risk, contracts * price * contract_value, note)


def ist_day(ts: float, tz: str = "Asia/Kolkata") -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.risk = cfg["risk"]
        self.db = db
        self.tz = cfg["report"]["timezone"]

    def halted(self) -> bool:
        return self.db.get(HALT_KEY) == "1"

    def roll_day(self, now: float, equity: float) -> None:
        """At the first check of each IST day, record starting equity and clear the daily halt."""
        day = ist_day(now, self.tz)
        if self.db.get(DAY_KEY) != day:
            self.db.set(DAY_KEY, day)
            self.db.set(DAY_START_EQUITY_KEY, equity)
            self.db.delete(DAY_HALT_KEY)
            self.db.event("INFO", f"new trading day {day}, start equity {equity:.2f}")

    def daily_loss_hit(self, equity: float) -> bool:
        if self.db.get(DAY_HALT_KEY) == "1":
            return True
        start = float(self.db.get(DAY_START_EQUITY_KEY, equity))
        loss_pct = (start - equity) / start * 100 if start > 0 else 0
        if loss_pct >= self.risk["daily_loss_limit_pct"]:
            self.db.set(DAY_HALT_KEY, "1")
            self.db.event("WARN", f"daily loss limit hit: -{loss_pct:.2f}% (start {start:.2f}, now {equity:.2f})")
            return True
        return False

    def can_open(self, symbol: str, open_symbols: set[str], equity: float) -> tuple[bool, str]:
        if self.halted():
            return False, "kill switch active"
        if self.daily_loss_hit(equity):
            return False, "daily loss limit reached"
        if symbol in open_symbols:
            return False, f"already have a {symbol} position"
        if len(open_symbols) >= self.risk["max_open_positions"]:
            return False, "max open positions reached"
        return True, ""
