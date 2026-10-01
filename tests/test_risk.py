import time

import pytest

from bot.config import load_config
from bot.db import DB
from bot.risk import HALT_KEY, RiskManager, size_position

RISK = {"risk_per_trade_pct": 1.0, "max_leverage": 3}


def test_sizing_risks_at_most_one_percent():
    # $1000, BTC 84000, stop 840 away, 0.001 BTC/contract -> $0.84/contract -> 11 contracts
    s = size_position(1000, 84000, 83160, 0.001, RISK)
    assert s.contracts == 11 and s.risk_usd <= 10


def test_sizing_capped_by_leverage():
    # very tight stop would want a huge size; leverage cap = 3000/84 = 35 contracts
    s = size_position(1000, 84000, 83990, 0.001, RISK)
    assert s.contracts == 35 and s.notional_usd <= 3000 and "leverage" in s.note


def test_leverage_cap_counts_open_positions():
    s = size_position(1000, 84000, 83990, 0.001, RISK, open_notional=2500)
    assert s.contracts == 5  # (3000 - 2500) / 84


def test_sizing_zero_when_one_contract_too_risky():
    assert size_position(100, 84000, 82000, 0.001, RISK).contracts == 0


@pytest.fixture
def rm(tmp_path):
    return RiskManager(load_config(), DB(tmp_path / "t.db"))


def test_daily_loss_limit(rm):
    rm.roll_day(time.time(), 100.0)
    assert rm.can_open("BTCUSD", set(), 98.0)[0]
    ok, why = rm.can_open("BTCUSD", set(), 96.9)
    assert not ok and "daily" in why
    # stays halted for the day even if equity recovers
    assert not rm.can_open("BTCUSD", set(), 100.0)[0]


def test_max_positions_and_duplicates(rm):
    rm.roll_day(time.time(), 100.0)
    assert not rm.can_open("BTCUSD", {"BTCUSD"}, 100)[0]
    full = {f"P{i}" for i in range(rm.risk["max_open_positions"])}
    assert not rm.can_open("SOLUSD", full, 100)[0]


def test_kill_switch(rm):
    rm.roll_day(time.time(), 100.0)
    rm.db.set(HALT_KEY, "1")
    ok, why = rm.can_open("BTCUSD", set(), 100)
    assert not ok and "kill" in why
