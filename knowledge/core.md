# Core Trading Knowledge (v2)

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
| Hammer / bullish pin bar *(disabled in v2)* | Sellers pushed down, buyers rejected it hard | Lower wick ≥ 2x body, close in the top third |
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
| Compression *(disabled in v2)* | Quiet market, energy building; the breakout tends to be large | ATR below `compression` x its 100-candle median |

## 3b. Extra liquidity levels (tested in v2, currently disabled with weight 0)

| Concept | Meaning | Bot detects |
|---|---|---|
| Previous-day high/low sweep | Yesterday's extremes hold many stops | Low below yesterday's low, close back above (and mirror) |
| Fair value gap (FVG) retest | A 3-candle imbalance tends to get revisited | Price dips into the latest unfilled gap and holds |
| Round numbers | Humans cluster orders at 80,000 / 2,500 / 0.25 | Wick within 0.25 ATR of a round level, close away from it |

## 4. Sessions and time (when big moves happen)

Crypto trades 24/7, but traditional markets drive volume.

| Time (UTC) | IST | Meaning |
|---|---|---|
| 00:00–02:00 | 05:30–07:30 | Asia open: usually quieter |
| 07:00–09:00 | 12:30–14:30 | London open: volume returns, first real moves of the day |
| 13:30–16:00 | 19:00–21:30 | US open (equities 13:30/14:30 UTC, DST-dependent): biggest volume, biggest moves |
| Sat–Sun | weekend | Thin liquidity, fake moves, wicks: lower trust |
| Sun 22:00–Mon 02:00 | Mon 03:30–07:30 | CME bitcoin futures reopen after the weekend gap; price often reacts to the gap |

Bot detects: the hour of the trigger candle. Inside a London/US open window it got `+session_open` (*disabled in v2, no edge*); on a weekend it gets `weekend` (a penalty).

Not simulated yet: scheduled macro events (CPI, FOMC, jobs data). Planned: an event calendar that blocks new entries ±2h around them.

## 5. News

News is a helper, not the strategy. Strong news **in** the trade direction adds `news_aligned`. Strong news **against** it adds `news_against` (a big penalty, which effectively blocks the trade). News is not backtested (no historical headlines).

## 6. Trade management (protect first, then let winners run)

1. **Stop-loss** goes beyond the structure that defines the idea: below the setup's swing low or sweep wick, plus `sl_buffer_atr` x ATR. If that stop is wider than `max_stop_atr` x ATR, skip the trade. If it is tighter than `min_stop_atr` x ATR, widen it (noise would hit it).
2. **TP1** at `tp1_r` x R: book `tp1_fraction` (currently 0%, nothing booked) and move the stop to **breakeven + fees**. From here the trade can't lose.
3. **Trail** the rest behind each new confirmed swing low (or high, for shorts), minus a buffer. This rides the long trends that pay for all the small losses.
4. **Runner target** at `runner_r` x R. This exists because every order must carry a target; the trailing stop normally exits first.
5. Many small losses and a few large wins is the expected shape. A 25–35% win rate is fine if winners average 3R or more.

## 7. Scoring

The setup score is the sum of the weights of every concept present. Required for a long:
- Trend context: higher-timeframe uptrend, or a lower-timeframe uptrend with a higher-timeframe range
- At least one trigger with a non-zero weight: sweep, engulfing, strong candle, or break of structure
- Score ≥ `min_score`

Shorts mirror all of the above.

---
## Research notes (use these to improve the next version)

Backtest: 3 years of Binance BTC/ETH candles, last 30% held out as out-of-sample (OOS).

| Timeframe | In-sample avg R | OOS avg R |
|---|---|---|
| 30m | -0.19 | -0.20 |
| 1h | -0.11 | -0.10 |
| 2h | -0.07 | -0.01 |
| **4h** | **+0.08** | **+0.12** |
| 6h | -0.31 | +0.18 |
| 1d | +0.01 | -0.27 |

4h is the only timeframe positive in both periods, so the bot trades 4h. The edge is small
(profit factor ~1.1) and needs live testnet confirmation.

Ablation on 4h, in-sample only (avg R with one concept removed; baseline +0.076):
- **Helps** (removing it hurts): sweep (-0.016), engulfing (-0.027), at_level (-0.023), ltf_trend (+0.006), discount (+0.022), volume_spike (+0.027), weekend penalty (+0.019), htf_trend (+0.047), strong_candle (+0.036)
- **Neutral or slightly harmful**: pin_bar (+0.107), session_open (+0.101), compression (+0.087). These differences are within noise (about ±0.06R); revisit with more data before changing anything.
- **Management**: TP1 at 2R + 30% booked = +0.076R. Breakeven + trail with no partial booking = +0.140R. TP1 at 1.5R = -0.029R. TP1 at 3R = +0.064R. The 30% partial is the owner's choice; this data suggests booking less (or nothing) earns more.

## Exit research (2026-10-02, BTC+ETH+ADA 4h, 575 trades)

How far trades run before the initial stop:
- 52% reach +1R, but 72% of those come back to the original stop if left unprotected
- 35% reach 2R, 18% reach 5R, 8% reach 10R
- Median time: 1R in 0.5 days, 2R in 1.5 days, 5R in ~4 days

Where the profit comes from: the 6% of trades that reach 5R or more made +316R. That is more than the
whole system's net (+128R). Losers cost -395R. **Without the big winners the system loses money.**

| Exit rule | In-sample avg R | Out-of-sample avg R | OOS win rate | OOS max DD |
|---|---|---|---|---|
| **Current: BE at 2R, trail at 2R, runner 10R** | **+0.234** | +0.196 | 36% | 21% |
| BE at 1R | +0.093 | +0.198 | 48% | 16% |
| Fixed target 2R | -0.076 | -0.013 | 35% | 27% |
| Fixed target 3R | -0.087 | | | |
| 50% booked at 2R | +0.100 | +0.120 | 37% | 18% |
| Step locks (1.5R->BE, 2R->+1R, 3R->+2R) | +0.024 | | | |
| Runner 5R instead of 10R | +0.086 | | | |
| Exit if < 0.5R after 2 days | +0.231 | +0.163 | 35% | 21% |

Conclusion: fixed or near targets cut off the fat tail and turn the system negative. Early breakeven
(1R) gives a smoother ride and a higher win rate but less total profit over 3 years (~90R vs ~128R).

### Changelog
- v1 (2026-10-01): initial version.
- v1.1 (2026-10-01): tp1_fraction 0.30 -> 0.0 (owner-approved trial). 4h backtest: in-sample +0.076R -> +0.140R, out-of-sample +0.115R -> +0.125R per trade.
- v2 (2026-10-01): removed pin bar, session open, compression (all hurt in two ablations). Tested and rejected:
  previous-day high/low sweep, FVG retest, round numbers (each lowered avg R), and lower-timeframe entries
  (4h setup + 1h/30m/15m entry: all negative, the tight stops get eaten by fees and noise). Added ADAUSD,
  max positions 3. Result, BTC+ETH+ADA 4h: in-sample +0.234R, out-of-sample +0.196R per trade
  (+26.6% over ~11 months, max drawdown 21%, ~3.8 trades/week).
