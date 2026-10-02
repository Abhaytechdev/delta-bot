"""Open-trade management rules, shared by the backtest and the live bot.

All levels are in R (1R = distance from entry to the initial stop). Keys in
knowledge/core.yaml `management`:
  be_r            move stop to breakeven (+fees) once price has gone this many R in favour
  lock_steps      [[trigger_R, lock_R], ...] e.g. [[2, 1]] = at +2R lock in +1R
  trail_start_r   start trailing behind confirmed swings once price has gone this far
  chandelier_atr  also trail at (best price - N x ATR); 0 = off
  partials        [[R, fraction], ...] book part of the position at these levels
  target_r        close everything at this R (0 = no fixed target, only the far runner)
  runner_r        far target that always sits on the exchange (every order needs one)
  stale_candles / stale_r   exit if after N candles the trade never reached stale_r (0 = off)
"""

from bot import brain


def r_multiple(side: str, entry: float, sl0: float, price: float) -> float:
    d = 1 if side == "buy" else -1
    return (price - entry) * d / abs(entry - sl0)


def level(side: str, entry: float, sl0: float, r: float) -> float:
    d = 1 if side == "buy" else -1
    return entry + d * r * abs(entry - sl0)


def new_stop(side: str, entry: float, sl0: float, cur_sl: float, best: float, row, m: dict, k: dict) -> float:
    """Most protective stop allowed after price has reached `best` (never loosens)."""
    long = side == "buy"
    best_r = r_multiple(side, entry, sl0, best)
    cands = [cur_sl]
    if m.get("be_r") and best_r >= m["be_r"]:
        cands.append(entry * (1 + m["be_fee_buffer"]) if long else entry * (1 - m["be_fee_buffer"]))
    for trig, lock in m.get("lock_steps") or []:
        if best_r >= trig:
            cands.append(level(side, entry, sl0, lock))
    if m.get("trail_start_r") is not None and best_r >= m["trail_start_r"]:
        lvl = brain.trail_level(row, side, k)
        if lvl is not None:
            cands.append(lvl)
        if m.get("chandelier_atr"):
            cands.append(best - m["chandelier_atr"] * row.atr if long else best + m["chandelier_atr"] * row.atr)
    return max(cands) if long else min(cands)


def final_target_r(m: dict) -> float:
    return m["target_r"] if m.get("target_r") else m["runner_r"]
