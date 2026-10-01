"""Hybrid strategy: technical trend-pullback signal, gated/boosted by news.

- No strong news   -> pure technical signal.
- Strong +ve news  -> long if the technical setup is bullish (even without a pullback
                      trigger); shorts are blocked.
- Strong -ve news  -> mirror image.
Stop = stop_atr_mult x ATR, target = reward_risk x stop distance.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Signal:
    side: str  # "buy" | "sell"
    price: float  # reference price (last close)
    stop_loss: float
    take_profit: float
    atr: float
    reason: str


def technical_bias(row: pd.Series) -> int:
    """+1 bullish setup, -1 bearish setup, 0 neither."""
    if row.ema_mid > row.ema_slow and row.close > row.ema_fast and row.rsi > 50:
        return 1
    if row.ema_mid < row.ema_slow and row.close < row.ema_fast and row.rsi < 50:
        return -1
    return 0


def technical_trigger(prev: pd.Series, row: pd.Series, p: dict) -> int:
    """Trend pullback: in an uptrend RSI dips below the pullback level then recovers above it."""
    up = row.ema_mid > row.ema_slow and row.close > row.ema_slow
    down = row.ema_mid < row.ema_slow and row.close < row.ema_slow
    if up and prev.rsi < p["rsi_long_level"] <= row.rsi:
        return 1
    if down and prev.rsi > p["rsi_short_level"] >= row.rsi:
        return -1
    return 0


def evaluate(df: pd.DataFrame, cfg: dict, news_score: float = 0.0) -> Signal | None:
    """df must already have indicators; uses the last (closed) candle."""
    p, risk = cfg["strategy"], cfg["risk"]
    if len(df) < p["ema_slow"] + 2:
        return None
    prev, row = df.iloc[-2], df.iloc[-1]
    if pd.isna(row.atr) or row.atr <= 0:
        return None

    strong = abs(news_score) >= p["news_strong_threshold"]
    news_dir = (1 if news_score > 0 else -1) if strong else 0

    direction, reason = 0, ""
    if news_dir:
        if technical_bias(row) == news_dir:
            direction, reason = news_dir, f"news {news_score:+.2f} + technical setup"
    else:
        direction = technical_trigger(prev, row, p)
        reason = "technical pullback"
    if not direction:
        return None

    stop_dist = risk["stop_atr_mult"] * row.atr
    price = float(row.close)
    return Signal(
        side="buy" if direction > 0 else "sell",
        price=price,
        stop_loss=price - direction * stop_dist,
        take_profit=price + direction * stop_dist * risk["reward_risk"],
        atr=float(row.atr),
        reason=reason,
    )
