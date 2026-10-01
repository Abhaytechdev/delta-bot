# Core Trading Knowledge (v1)

The bot's "understanding" of the market. Every concept below has:
- **Meaning**: what it tells us about the traders behind the price
- **Bot detects**: the exact rule the code uses (`bot/structure.py`, `bot/candles.py`, `bot/psychology.py`, `bot/sessions.py`)
- **Weight**: how much it adds to a setup's score (`knowledge/core.yaml`)

A trade is taken only when enough concepts agree (confluence). One signal alone is noise.
Improve this file over time: change a rule or weight, backtest, keep it only if the
out-of-sample result improves.

---

## 1. Market structure (the map)

Price moves in waves: impulse, pullback, impulse. The turning points are **swings**.

| Concept | Meaning | Bot detects |
|---|---|---|
| Swing high / low | A point where buyers (or sellers) gave up and the other side took over | A candle whose high (low) is the highest (lowest) of `swing_left` candles before and `swing_right` after. Only known `swing_right` candles later, so there's no peeking into the future |
| Uptrend (HH + HL) | Buyers in control: each push goes higher, each dip is bought earlier | Last swing high > previous swing high **and** last swing low > previous swing low |
| Downtrend (LH + LL) | Sellers in control | Mirror of the above |
| Range | Neither side in control | Anything else |
| Break of structure (BOS) | The side in control just proved it again | Close above the last swing high (or below the last swing low) |
| Higher-timeframe trend | The big tide; small waves fail against it | Same structure test on candles `htf_factor` times larger |
| Support / resistance | Prices where many traders acted before and will again (orders, stops, memory) | The last `zone_memory` swing lows/highs; price within `zone_atr` x ATR of one is "at a level" |
| Discount / premium | In an uptrend, buy the pullback (discount), not the top (premium) | Price has retraced at least `min_retrace` of the last swing leg |

**Rule**: trade with the higher-timeframe trend. Buy pullbacks in uptrends at support, and sell rallies in downtrends at resistance.

## 2. Candles (the conversation inside each bar)

A candle only means something **at a level, in context**. A hammer in the middle of nowhere is noise. A hammer on support after a pullback in an uptrend is buyers stepping in.

| Candle | Meaning | Bot detects |
|---|---|---|
| Bullish engulfing | Buyers overwhelmed the previous candle's sellers | Green body fully covers the previous red body |
| Hammer / bullish pin bar | Sellers pushed down, buyers rejected it hard | Lower wick ≥ 2x body, close in the top third |
| Shooting star / bearish pin | Mirror: buyers rejected | Upper wick ≥ 2x body, close in the bottom third |
| Strong bull candle (marubozu) | Conviction, little hesitation | Body ≥ 70% of range and range ≥ 1 ATR |
| Doji | Indecision; meaningful only after a long move | Body ≤ 10% of range |
| Inside bar | Pause, energy building | High/low within the previous candle |
| Close position | Who won the candle | Where the close sits in the high-low range (0 = low, 1 = high) |

## 3. Trading psychology (what other traders are feeling)

| Concept | Meaning | Bot detects |
|---|---|---|
| Liquidity sweep / stop hunt | Stops of late longs sit under the last swing low. Big players push price there to fill orders, then reverse. The trapped sellers then fuel the move up | Low goes below the last confirmed swing low but the candle **closes back above** it |
| Trapped traders (failed breakout) | Breakout buyers above resistance get trapped when price falls back; their exits push it further | Same as the sweep, on the other side |
| FOMO / over-extension | Price far from its average means latecomers are chasing; pullback risk is high | Distance from EMA(50) > `overextended_atr` x ATR in the trade direction: penalty |
| Capitulation | Panic: huge candle, huge volume, long wick. Often marks the end of a move | Range > 2.5 ATR, volume spike, long wick against the move |
| Volume spike | Real participation, not drift | Volume > `volume_spike` x 20-candle average |
| Compression | Quiet market, energy building; the breakout tends to be large | ATR below `compression` x its 100-candle median |

## 4. Sessions and time (when big moves happen)

Crypto trades 24/7, but traditional markets drive volume.

| Time (UTC) | IST | Meaning |
|---|---|---|
| 00:00–02:00 | 05:30–07:30 | Asia open: usually quieter |
| 07:00–09:00 | 12:30–14:30 | London open: volume returns, first real moves of the day |
| 13:30–16:00 | 19:00–21:30 | US open (equities 13:30/14:30 UTC, DST-dependent): biggest volume, biggest moves |
| Sat–Sun | weekend | Thin liquidity, fake moves, wicks: lower trust |
| Sun 22:00–Mon 02:00 | Mon 03:30–07:30 | CME bitcoin futures reopen after the weekend gap; price often reacts to the gap |

Bot detects: the hour of the trigger candle. Inside a London/US open window it gets `+session_open`; on a weekend it gets `weekend` (a penalty).

Not simulated yet: scheduled macro events (CPI, FOMC, jobs data). Planned: an event calendar that blocks new entries ±2h around them.

## 5. News

News is a helper, not the strategy. Strong news **in** the trade direction adds `news_aligned`. Strong news **against** it adds `news_against` (a big penalty, which effectively blocks the trade). News is not backtested (no historical headlines).

## 6. Trade management (protect first, then let winners run)

1. **Stop-loss** goes beyond the structure that defines the idea: below the setup's swing low or sweep wick, plus `sl_buffer_atr` x ATR. If that stop is wider than `max_stop_atr` x ATR, skip the trade. If it is tighter than `min_stop_atr` x ATR, widen it (noise would hit it).
2. **TP1** at `tp1_r` x R: book `tp1_fraction` (30%) and move the stop to **breakeven + fees**. From here the trade can't lose.
3. **Trail** the rest behind each new confirmed swing low (or high, for shorts), minus a buffer. This rides the long trends that pay for all the small losses.
4. **Runner target** at `runner_r` x R. This exists because every order must carry a target; the trailing stop normally exits first.
5. Many small losses and a few large wins is the expected shape. A 25–35% win rate is fine if winners average 3R or more.

## 7. Scoring

The setup score is the sum of the weights of every concept present. Required for a long:
- Trend context: higher-timeframe uptrend, or a lower-timeframe uptrend with a higher-timeframe range
- At least one trigger: sweep, engulfing, pin bar, or a strong candle at a level
- Score ≥ `min_score`

Shorts mirror all of the above.

---
### Changelog
- v1 (2026-10-01): initial version.
