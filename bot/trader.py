"""Main trading loop: python -m bot.trader

Every POLL_S seconds: sync account, protect/expire positions, record closed trades.
After each new candle closes: evaluate the strategy per pair and enter if allowed.
"""

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone

from bot.config import ROOT, load_config, load_secrets
from bot.db import DB
from bot.exchange import RESOLUTION_SECONDS, Exchange, Position
from bot.feed import PriceFeed
from bot.indicators import add_indicators, to_frame
from bot.news import NewsMonitor
from bot.risk import RiskManager, size_position
from bot.strategy import Signal, evaluate

log = logging.getLogger("bot")

POLL_S = 30
CANDLES = 400            # history for indicators (needs > ema_slow)
CANDLE_DELAY_S = 10      # wait after candle close for the exchange to finalise it
ENTRY_WAIT_S = 60        # how long a post-only limit entry may rest
MAX_CHASE_ATR = 0.25     # skip market fallback if price ran this many ATRs past the signal
SNAPSHOT_S = 300
STALE_SIGNAL_S = 600     # only act on a candle within 10 min of its close


def setup_logging(cfg: dict) -> None:
    path = ROOT / cfg["logging"]["file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.WatchedFileHandler(path)  # plays well with logrotate
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(cfg["logging"]["level"])
    root.handlers = [fh, sh]
    for noisy in ("urllib3", "websocket", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        secrets = load_secrets()
        self.ex = Exchange(cfg, secrets)
        self.db = DB(cfg["storage"]["db_path"])
        self.risk = RiskManager(cfg, self.db)
        self.news = NewsMonitor(cfg, secrets.anthropic_api_key)
        self.feed = PriceFeed(cfg["exchange"]["ws_url"], cfg["trading"]["pairs"])
        self.pairs = cfg["trading"]["pairs"]
        self.tf = cfg["trading"]["timeframe"]
        self.tf_s = RESOLUTION_SECONDS[self.tf]
        self.max_hold_s = cfg["risk"]["max_hold_hours"] * 3600
        self._last_bar = int(self.db.get("last_bar", "0"))
        self._last_snapshot = 0.0
        self._halt_logged = False
        self._stop = False

    # ---------- helpers ----------
    def note(self, level: str, msg: str) -> None:
        getattr(log, level.lower() if level != "WARN" else "warning")(msg)
        self.db.event(level, msg)

    def price(self, symbol: str) -> float:
        return self.feed.price(symbol) or self.ex.mark_price(symbol)

    # ---------- lifecycle ----------
    def setup(self) -> None:
        for s in self.pairs:
            self.ex.set_leverage(s, self.cfg["risk"]["max_leverage"])
        self.feed.start()
        bal, _ = self.ex.balance_usd()
        self.note("INFO", f"bot started on {self.cfg['exchange']['environment']}, balance {bal:.2f} USD, "
                          f"pairs {self.pairs} {self.tf}")

    def run(self, once: bool = False) -> None:
        self.setup()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                self.note("ERROR", f"tick failed: {type(e).__name__}: {e}")
            if once:
                break
            time.sleep(POLL_S)
        self.feed.stop()
        self.note("INFO", "bot stopped")

    def tick(self) -> None:
        now = time.time()
        bal, _ = self.ex.balance_usd()
        equity = bal + self.ex.unrealized_pnl()
        self.risk.roll_day(now, equity)
        if now - self._last_snapshot >= SNAPSHOT_S:
            self.db.snapshot(bal, equity - bal)
            self._last_snapshot = now

        positions = {p.symbol: p for p in self.ex.positions()}
        self.record_closed(positions)
        self.protect(positions)
        self.expire(positions)
        self.risk.daily_loss_hit(equity)

        if self.risk.halted():
            if not self._halt_logged:
                self.note("WARN", "kill switch active: no new trades")
                self._halt_logged = True
            return
        self._halt_logged = False

        bar = int(now - CANDLE_DELAY_S) // self.tf_s * self.tf_s  # start of current candle
        if bar > self._last_bar:
            self._last_bar = bar
            self.db.set("last_bar", bar)
            if now - bar <= STALE_SIGNAL_S:  # after a restart, don't act on an old candle
                self.on_candle_close(positions, bal, equity)

    # ---------- position management ----------
    def record_closed(self, positions: dict[str, Position]) -> None:
        """Trades open in the DB but flat on the exchange were closed (SL/TP/manual)."""
        for t in self.db.open_trades():
            if t["symbol"] in positions or t["entry_price"] is None:
                continue
            pnl, fees, exit_price = self.realized(t)
            if exit_price is None:
                reason = t["exit_reason"] or "unknown"
            elif t["exit_reason"]:
                reason = t["exit_reason"]
            else:
                reason = "take_profit" if abs(exit_price - t["take_profit"]) < abs(exit_price - t["stop_loss"]) else "stop_loss"
            self.db.update_trade(t["id"], status="closed", closed_at=time.time(), exit_price=exit_price,
                                 exit_reason=reason, pnl=pnl, fees=fees)
            self.note("INFO", f"CLOSED {t['symbol']} {t['side']} x{t['size']} @ {exit_price} ({reason}) "
                              f"pnl {pnl:+.2f} USD (fees {fees:.2f})")

    def realized(self, t) -> tuple[float, float, float | None]:
        """Net P&L, fees, avg exit price from exchange fills since the trade opened."""
        pid = self.ex.product(t["symbol"]).id
        fills = self.ex.client.fills({"product_ids": str(pid)}, page_size=50)["result"]
        since = t["opened_at"] - 5
        ts = lambda f: datetime.fromisoformat(f["created_at"].replace("Z", "+00:00")).timestamp()
        fills = sorted((f for f in fills if ts(f) >= since), key=ts)
        exit_side = "sell" if t["side"] == "buy" else "buy"

        def take(side: str) -> list[tuple[float, int, float]]:
            """(price, qty, fee) for the first `size` contracts filled on `side`."""
            out, left = [], t["size"]
            for f in fills:
                if f["side"] == side and left > 0:
                    n = min(int(f["size"]), left)
                    out.append((float(f["price"]), n, float(f["commission"]) * n / int(f["size"])))
                    left -= n
            return out

        entries, exits = take(t["side"]), take(exit_side)
        fees = sum(x[2] for x in entries + exits)
        if not exits:
            return -fees, fees, None
        qty = sum(x[1] for x in exits)
        exit_price = sum(x[0] * x[1] for x in exits) / qty
        direction = 1 if t["side"] == "buy" else -1
        gross = (exit_price - t["entry_price"]) * direction * qty * t["contract_value"]
        return gross - fees, fees, exit_price

    def protect(self, positions: dict[str, Position]) -> None:
        """Every open position must have a stop-loss covering its full size; else add one or close."""
        trades = {t["symbol"]: t for t in self.db.open_trades()}
        for sym, pos in positions.items():
            orders = self.ex.open_orders(sym)
            sl_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "stop_loss_order")
            tp_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "take_profit_order")
            if sl_size >= abs(pos.size) and tp_size >= abs(pos.size):
                continue
            t = trades.get(sym)
            long = pos.size > 0
            if t:
                sl, tp = t["stop_loss"], t["take_profit"]
            else:
                self.note("WARN", f"unmanaged {sym} position x{pos.size}; attaching default SL/TP")
                df = add_indicators(to_frame(self.ex.candles(sym, self.tf, CANDLES)), self.cfg["strategy"])
                d = self.cfg["risk"]["stop_atr_mult"] * float(df["atr"].iloc[-1])
                sl = pos.entry_price - d if long else pos.entry_price + d
                tp = pos.entry_price + d * self.cfg["risk"]["reward_risk"] * (1 if long else -1)
            try:
                for o in orders:
                    if o.get("stop_order_type") in ("stop_loss_order", "take_profit_order"):
                        self.ex.cancel_order(sym, o["id"])
                self.ex.place_position_bracket(sym, sl, tp, long)
                self.note("WARN", f"{sym}: SL/TP did not cover position x{pos.size}; re-attached SL {sl:.2f} TP {tp:.2f}")
            except Exception as e:
                self.note("ERROR", f"{sym}: could not attach SL/TP ({e}); closing position")
                self.close(pos, "no_stop_loss")

    def expire(self, positions: dict[str, Position]) -> None:
        for t in self.db.open_trades():
            pos = positions.get(t["symbol"])
            if pos and time.time() - t["opened_at"] >= self.max_hold_s:
                self.note("INFO", f"{t['symbol']}: max hold {self.cfg['risk']['max_hold_hours']}h reached, closing")
                self.close(pos, "max_hold", trade_id=t["id"])

    def close(self, pos: Position, reason: str, trade_id: int | None = None) -> None:
        self.ex.close_position(pos)
        if trade_id is None:
            for t in self.db.open_trades():
                if t["symbol"] == pos.symbol:
                    trade_id = t["id"]
        if trade_id is not None:
            self.db.update_trade(trade_id, exit_reason=reason)

    # ---------- entries ----------
    def on_candle_close(self, positions: dict[str, Position], balance: float, equity: float) -> None:
        scores = self.news.get()
        self.db.set("news", ";".join(f"{p}={s:+.2f}" for p, s in scores.items()) + f" ({self.news.source}, "
                    f"{self.news.headline_count} headlines)")
        open_syms = set(positions)
        for sym in self.pairs:
            df = add_indicators(to_frame(self.ex.candles(sym, self.tf, CANDLES)), self.cfg["strategy"])
            r = df.iloc[-1]
            log.info("%s close=%.2f ema50=%.2f ema200=%.2f rsi=%.1f atr=%.2f news=%+.2f",
                     sym, r.close, r.ema_mid, r.ema_slow, r.rsi, r.atr, scores.get(sym, 0))
            sig = evaluate(df, self.cfg, scores.get(sym, 0.0))
            if not sig:
                continue
            ok, why = self.risk.can_open(sym, open_syms, equity)
            if not ok:
                self.note("INFO", f"{sym} {sig.side} signal skipped: {why}")
                continue
            if self.enter(sym, sig, balance, positions):
                open_syms.add(sym)

    def enter(self, sym: str, sig: Signal, balance: float, positions: dict[str, Position]) -> bool:
        product = self.ex.product(sym)
        open_notional = sum(abs(p.size) * self.ex.product(p.symbol).contract_value * self.price(p.symbol)
                            for p in positions.values())
        sizing = size_position(balance, sig.price, sig.stop_loss, product.contract_value,
                               self.cfg["risk"], open_notional)
        if sizing.contracts < 1:
            self.note("INFO", f"{sym} {sig.side} signal skipped: {sizing.note}")
            return False
        long = sig.side == "buy"
        opened_at = time.time()
        filled = self._limit_entry(sym, sig, sizing.contracts)
        if not filled:
            px = self.price(sym)
            ran = (px - sig.price) * (1 if long else -1)
            beyond = (px <= sig.stop_loss or px >= sig.take_profit)
            if ran > MAX_CHASE_ATR * sig.atr or beyond:
                self.note("INFO", f"{sym} {sig.side}: price moved to {px:.2f}, not chasing")
                return False
            self.ex.place_entry(sym, sig.side, sizing.contracts, sig.stop_loss, sig.take_profit)
            time.sleep(2)
        pos = next((p for p in self.ex.positions() if p.symbol == sym), None)
        if not pos:
            self.note("WARN", f"{sym} {sig.side}: entry not filled")
            return False
        self.db.open_trade(
            symbol=sym, side=sig.side, size=abs(pos.size), contract_value=product.contract_value,
            entry_price=pos.entry_price, stop_loss=sig.stop_loss, take_profit=sig.take_profit,
            risk_usd=abs(pos.entry_price - sig.stop_loss) * abs(pos.size) * product.contract_value,
            reason=sig.reason, opened_at=opened_at,
        )
        positions[sym] = pos
        self.note("INFO", f"OPENED {sym} {sig.side} x{abs(pos.size)} @ {pos.entry_price} SL {sig.stop_loss:.2f} "
                          f"TP {sig.take_profit:.2f} risk ${sizing.risk_usd:.2f} [{sig.reason}]"
                          + (f" ({sizing.note})" if sizing.note else ""))
        self.protect({sym: pos})
        return True

    def _limit_entry(self, sym: str, sig: Signal, size: int) -> bool:
        """Post-only limit at the touch. True if (at least partly) filled."""
        bid, ask = self.ex.best_bid_ask(sym)
        try:
            o = self.ex.place_entry(sym, sig.side, size, sig.stop_loss, sig.take_profit,
                                    limit_price=bid if sig.side == "buy" else ask)
        except Exception as e:
            log.info("%s post-only entry rejected (%s); will use market", sym, e)
            return False
        deadline = time.time() + ENTRY_WAIT_S
        while time.time() < deadline:
            time.sleep(5)
            o = self.ex.order(o["id"])
            if o["state"] in ("closed", "cancelled"):
                break
        if o["state"] == "open":
            try:
                self.ex.cancel_order(sym, o["id"])
            except Exception:
                pass
            o = self.ex.order(o["id"])
        unfilled = int(o.get("unfilled_size", o["size"]))
        return unfilled < int(o["size"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg)
    Trader(cfg).run(once=args.once)


if __name__ == "__main__":
    main()
