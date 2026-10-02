"""Decision engine: turns the knowledge base into scored, explained swing setups.

Two modes:
- single timeframe: context (trend, zones) and trigger come from the same candles
- multi-timeframe: context from the setup timeframe (e.g. 4h), trigger + stop from the
  entry timeframe (e.g. 1h). Context columns are prefixed ctx_ and only use setup
  candles that have fully closed (no lookahead).

analyze()  -> feature columns for every candle (call once)
setup_at() -> Setup (side, SL, TP1, runner, reasons) for one closed candle, or None
"""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from bot import candles, levels, psychology, sessions, structure
from bot.config import ROOT
from bot.indicators import add_indicators

KNOWLEDGE_PATH = ROOT / "knowledge" / "core.yaml"
INDICATOR_PARAMS = {"ema_fast": 20, "ema_mid": 50, "ema_slow": 200, "rsi_len": 14, "atr_len": 14}
WARMUP = 210
CTX_COLS = ["trend", "htf_trend", "atr", "sh1", "sl1", "leg_low", "leg_high",
            "overext_up", "overext_dn", "compression"]


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


def _features(df: pd.DataFrame, k: dict, tf_seconds: int) -> pd.DataFrame:
    s = k["structure"]
    df = add_indicators(df.reset_index(drop=True), INDICATOR_PARAMS)
    df = structure.annotate(df, s["swing_left"], s["swing_right"], s["zone_memory"])
    df["htf_trend"] = structure.htf_trend(df, tf_seconds, s["htf_factor"], s["swing_left"], s["swing_right"])
    df = candles.annotate(df)
    df = psychology.annotate(df, k["psychology"])
    df = levels.annotate(df)
    df["warm"] = df.index >= WARMUP  # indicators (EMA200 etc.) need history before they mean anything
    return sessions.annotate(df, tf_seconds)


def analyze(df: pd.DataFrame, k: dict, tf_seconds: int,
            setup_df: pd.DataFrame | None = None, setup_tf_seconds: int | None = None) -> pd.DataFrame:
    f = _features(df, k, tf_seconds)
    zm = k["structure"]["zone_memory"]
    ctx = CTX_COLS + [f"zl{z}" for z in range(1, zm + 1)] + [f"zh{z}" for z in range(1, zm + 1)]
    if setup_df is None:
        for c in ctx:
            f["ctx_" + c] = f[c]
        f["mtf"] = False
        return f
    h = _features(setup_df, k, setup_tf_seconds)
    h = h[ctx + ["time"]].rename(columns={c: "ctx_" + c for c in ctx})
    h["avail"] = h["time"] + setup_tf_seconds  # a setup candle is usable once it has closed
    f["close_time"] = f["time"] + tf_seconds
    m = pd.merge_asof(f.sort_values("close_time"), h.drop(columns="time").sort_values("avail"),
                      left_on="close_time", right_on="avail", direction="backward")
    m["mtf"] = True
    return m.drop(columns=["close_time", "avail"]).reset_index(drop=True)


TRIGGERS = (  # (long column, short column, weight key, label)
    ("sweep_low", "sweep_high", "sweep", "liquidity sweep reclaimed"),
    ("bull_engulf", "bear_engulf", "engulfing", "engulfing"),
    ("hammer", "shooting_star", "pin_bar", "pin bar rejection"),
    ("strong_bull", "strong_bear", "strong_candle", "strong candle"),
    ("bos_up", "bos_dn", "bos", "break of structure"),
    ("sweep_pdl", "sweep_pdh", "pd_sweep", "previous-day level swept"),
    ("fvg_long", "fvg_short", "fvg", "FVG retest"),
    ("round_bounce", "round_reject", "round_level", "round-number reaction"),
)


def _score(r, k: dict, d: int, news: float, news_strong: float) -> tuple[float, list[str], bool]:
    """Score for direction d (+1 long / -1 short). Returns (score, reasons, required_ok)."""
    w, s = k["weights"], k["structure"]
    score, why = 0.0, []

    def add(key, label):
        nonlocal score
        if w.get(key, 0):
            score += w[key]
            why.append(label)

    htf_ok = r.ctx_htf_trend == d
    ltf_ok = r.ctx_trend == d and r.ctx_htf_trend != -d
    if htf_ok:
        add("htf_trend", "HTF " + ("uptrend" if d > 0 else "downtrend"))
    if r.ctx_trend == d:
        add("ltf_trend", "HH/HL" if d > 0 else "LH/LL")
    if not (htf_ok or ltf_ok):
        return score, why, False
    e = k["entry"]
    if e.get("require_ltf_trend") and r.ctx_trend != d:
        return score, why, False  # setup-timeframe structure must agree
    if e.get("max_chase_atr") is not None and r.recent_move * d > e["max_chase_atr"]:
        return score, why, False  # price already ran in our direction: chasing
    if r.mtf and r.trend == d:
        add("entry_trend", "entry-TF structure agrees")

    zones = [getattr(r, f"ctx_{'zl' if d > 0 else 'zh'}{z}") for z in range(1, s["zone_memory"] + 1)]
    edge = r.low if d > 0 else r.high
    dist = min((abs(edge - z) for z in zones if z == z), default=float("inf"))
    if dist <= s["zone_atr"] * r.ctx_atr:
        add("at_level", "at support" if d > 0 else "at resistance")
    if d > 0 and r.ctx_sh1 > r.ctx_leg_low and (r.ctx_sh1 - r.low) / (r.ctx_sh1 - r.ctx_leg_low) >= s["min_retrace"]:
        add("discount", "pullback into discount")
    if d < 0 and r.ctx_leg_high > r.ctx_sl1 and (r.high - r.ctx_sl1) / (r.ctx_leg_high - r.ctx_sl1) >= s["min_retrace"]:
        add("discount", "rally into premium")

    triggers = 0
    for col_l, col_s, key, label in TRIGGERS:
        if w.get(key, 0) and getattr(r, col_l if d > 0 else col_s):
            add(key, label)
            triggers += 1

    if r.volume_spike:
        add("volume_spike", "volume spike")
    if r.ctx_compression:
        add("compression", "after compression")
    if r.session_open:
        add("session_open", "session open")
    if r.weekend:
        add("weekend", "weekend")
    if r.ctx_overext_up if d > 0 else r.ctx_overext_dn:
        add("overextended", "overextended (FOMO risk)")
    if abs(news) >= news_strong:
        add("news_aligned" if news * d > 0 else "news_against", f"news {news:+.2f}")
    return score, why, triggers > 0


def setup_from_rows(rows: list, i: int, k: dict, news: float = 0.0, news_strong: float = 0.5) -> Setup | None:
    """rows: list of itertuples() records; evaluates rows[i] (a closed candle)."""
    r = rows[i]
    if i < 2 or not r.warm or not r.atr > 0 or not r.ctx_atr > 0 or r.ctx_sh1 != r.ctx_sh1 or r.sh1 != r.sh1:
        return None
    m = k["management"]
    best = None
    for d in (1, -1):
        score, why, ok = _score(r, k, d, news, news_strong)
        if not ok or score < k["entry"]["min_score"]:
            continue
        window = rows[i - 2:i + 1]
        price = float(r.close)
        if d > 0:
            stop = min(x.low for x in window) - m["sl_buffer_atr"] * r.atr
        else:
            stop = max(x.high for x in window) + m["sl_buffer_atr"] * r.atr
        dist = abs(price - stop)
        if dist > m["max_stop_atr"] * r.ctx_atr:
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


def setup_at(df: pd.DataFrame, i: int, k: dict, news: float = 0.0, news_strong: float = 0.5) -> Setup | None:
    if i < 2:
        return None
    rows = list(df.iloc[i - 2:i + 1].itertuples(index=False))
    return setup_from_rows(rows, 2, k, news, news_strong)


def trail_level(r, side: str, k: dict) -> float | None:
    """Trailing stop from the latest confirmed swing (entry TF, or setup TF when trail_on: context)."""
    ctx = k["management"].get("trail_on", "entry") == "context"
    low, high, atr = (r.ctx_sl1, r.ctx_sh1, r.ctx_atr) if ctx else (r.sl1, r.sh1, r.atr)
    buf = k["management"]["trail_buffer_atr"] * atr
    if side == "buy":
        return None if pd.isna(low) else float(low - buf)
    return None if pd.isna(high) else float(high + buf)
