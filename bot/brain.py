"""Decision engine: turns the knowledge base into scored, explained swing setups.

analyze()  -> adds every feature column once (structure, candles, psychology, sessions, HTF trend)
setup_at() -> evaluates one closed candle: Setup with SL / TP1 / runner target and the reasons, or None
"""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from bot import candles, psychology, sessions, structure
from bot.config import ROOT
from bot.indicators import add_indicators

KNOWLEDGE_PATH = ROOT / "knowledge" / "core.yaml"
INDICATOR_PARAMS = {"ema_fast": 20, "ema_mid": 50, "ema_slow": 200, "rsi_len": 14, "atr_len": 14}


def load_knowledge(path: Path = KNOWLEDGE_PATH) -> dict:
    return yaml.safe_load(path.read_text())


@dataclass(frozen=True)
class Setup:
    side: str  # buy | sell
    price: float
    stop_loss: float
    tp1: float
    runner_tp: float
    atr: float
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return f"score {self.score:.1f}: " + ", ".join(self.reasons)


def analyze(df: pd.DataFrame, k: dict, tf_seconds: int) -> pd.DataFrame:
    s = k["structure"]
    df = add_indicators(df.reset_index(drop=True), INDICATOR_PARAMS)
    df = structure.annotate(df, s["swing_left"], s["swing_right"], s["zone_memory"])
    df["htf_trend"] = structure.htf_trend(df, tf_seconds, s["htf_factor"], s["swing_left"], s["swing_right"])
    df = candles.annotate(df)
    df = psychology.annotate(df, k["psychology"])
    return sessions.annotate(df, tf_seconds)


def _score(r: pd.Series, k: dict, d: int, news: float, news_strong: float) -> tuple[float, list[str], bool]:
    """Score for direction d (+1 long / -1 short). Returns (score, reasons, required_ok)."""
    w, s = k["weights"], k["structure"]
    score, why = 0.0, []

    def add(key, label):
        nonlocal score
        score += w[key]
        why.append(label)

    htf_ok = r.htf_trend == d
    ltf_ok = r.trend == d and r.htf_trend != -d
    if htf_ok:
        add("htf_trend", "HTF " + ("uptrend" if d > 0 else "downtrend"))
    if r.trend == d:
        add("ltf_trend", "HH/HL" if d > 0 else "LH/LL")
    if not (htf_ok or ltf_ok):
        return score, why, False

    level = r.near_support if d > 0 else r.near_resistance
    if level <= s["zone_atr"] * r.atr:
        add("at_level", "at support" if d > 0 else "at resistance")
    if d > 0 and r.sh1 > r.leg_low and (r.sh1 - r.low) / (r.sh1 - r.leg_low) >= s["min_retrace"]:
        add("discount", "pullback into discount")
    if d < 0 and r.leg_high > r.sl1 and (r.high - r.sl1) / (r.leg_high - r.sl1) >= s["min_retrace"]:
        add("discount", "rally into premium")

    triggers = 0
    for col_l, col_s, key, label in (
        ("sweep_low", "sweep_high", "sweep", "liquidity sweep reclaimed"),
        ("bull_engulf", "bear_engulf", "engulfing", "engulfing"),
        ("hammer", "shooting_star", "pin_bar", "pin bar rejection"),
        ("strong_bull", "strong_bear", "strong_candle", "strong candle"),
        ("bos_up", "bos_dn", "bos", "break of structure"),
    ):
        if r[col_l if d > 0 else col_s]:
            add(key, label)
            triggers += 1

    if r.volume_spike:
        add("volume_spike", "volume spike")
    if r.compression:
        add("compression", "after compression")
    if r.session_open:
        add("session_open", "session open")
    if r.weekend:
        add("weekend", "weekend")
    if r.overext_up if d > 0 else r.overext_dn:
        add("overextended", "overextended (FOMO risk)")
    if abs(news) >= news_strong:
        add("news_aligned" if news * d > 0 else "news_against", f"news {news:+.2f}")
    return score, why, triggers > 0


def setup_at(df: pd.DataFrame, i: int, k: dict, news: float = 0.0, news_strong: float = 0.5) -> Setup | None:
    r = df.iloc[i]
    if pd.isna(r.atr) or r.atr <= 0 or pd.isna(r.sh1) or i < 210:
        return None
    m = k["management"]
    best = None
    for d in (1, -1):
        score, why, ok = _score(r, k, d, news, news_strong)
        if not ok or score < k["entry"]["min_score"]:
            continue
        window = df.iloc[max(0, i - 2):i + 1]
        price = float(r.close)
        if d > 0:
            stop = float(window["low"].min()) - m["sl_buffer_atr"] * r.atr
        else:
            stop = float(window["high"].max()) + m["sl_buffer_atr"] * r.atr
        dist = abs(price - stop)
        if dist > m["max_stop_atr"] * r.atr:
            continue
        dist = max(dist, m["min_stop_atr"] * r.atr)
        cand = Setup(
            side="buy" if d > 0 else "sell", price=price, stop_loss=price - d * dist,
            tp1=price + d * dist * m["tp1_r"], runner_tp=price + d * dist * m["runner_r"],
            atr=float(r.atr), score=score, reasons=tuple(why),
        )
        if best is None or cand.score > best.score:
            best = cand
    return best


def trail_level(r: pd.Series, side: str, k: dict) -> float | None:
    """Trailing stop candidate from the latest confirmed swing (long: below swing low)."""
    buf = k["management"]["trail_buffer_atr"] * r.atr
    if side == "buy":
        return None if pd.isna(r.sl1) else float(r.sl1 - buf)
    return None if pd.isna(r.sh1) else float(r.sh1 + buf)
