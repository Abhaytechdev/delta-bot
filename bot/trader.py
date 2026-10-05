"""Main trading loop: python -m bot.trader

Every POLL_S seconds: sync account, book TP1 / move stop to breakeven, keep every
position protected, record closed trades.
After each new candle closes: trail stops behind new swings, then look for setups
(knowledge/core.md via bot.brain) and enter if the risk manager allows.
"""

import argparse
import fcntl
import requests
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime

from bot import brain, crowd, manage
from bot.config import ROOT, load_config, load_secrets
from bot.db import DB
from bot.exchange import RESOLUTION_SECONDS, Exchange, Position
from bot.feed import PriceFeed
from bot.indicators import to_frame
from bot.news import NewsMonitor
from bot.risk import RiskManager, size_position

log = logging.getLogger("bot")

POLL_S = 10
CANDLES = 2400           # ~400 days of 4h: indicators, higher-timeframe structure and a converged daily EMA200
CANDLE_DELAY_S = 10      # wait after candle close for the exchange to finalise it
ENTRY_WAIT_S = 60        # how long a post-only limit entry may rest
MAX_CHASE_ATR = 0.25     # skip market fallback if price ran this many ATRs past the signal
SNAPSHOT_S = 300
STALE_SIGNAL_S = 900     # only act on a candle within 15 min of its close


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
        self.news = NewsMonitor(cfg)  # keyword scoring only: no Claude in live trading
        self.feed = PriceFeed(cfg["exchange"]["ws_url"], cfg["trading"]["pairs"], cfg["exchange"]["environment"])
        self.k = brain.load_knowledge()
        self.m = self.k["management"]
        self.pairs = cfg["trading"]["pairs"]
        self.tf = cfg["trading"]["timeframe"]
        self.tf_s = RESOLUTION_SECONDS[self.tf]
        self._last_bar = int(self.db.get("last_bar", "0"))
        self._last_snapshot = 0.0
        self._halt_logged = False
        self._stop = False

    # ---------- helpers ----------
    def note(self, level: str, msg: str) -> None:
        getattr(log, {"WARN": "warning"}.get(level, level.lower()))(msg)
        self.db.event(level, msg)

    def price(self, symbol: str) -> float:
        return self.feed.price(symbol) or self.ex.mark_price(symbol)

    def analyze(self, symbol: str):
        return brain.analyze(to_frame(self.ex.candles(symbol, self.tf, CANDLES)), self.k, self.tf_s)

    # ---------- lifecycle ----------
    def setup(self) -> None:
        for s in self.pairs:
            self.ex.set_leverage(s, self.cfg["risk"]["max_leverage"])
        self.feed.start()
        bal, _ = self.ex.balance_usd()
        env = self.cfg["exchange"]["environment"]
        self.note("INFO", f"bot started on {'*** LIVE (REAL MONEY) ***' if env == 'live' else env}, balance {bal:.2f} USD, "
                          f"pairs {self.pairs} {self.tf}, knowledge v{self.k['version']}")

    def run(self, once: bool = False) -> None:
        self.setup()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        while not self._stop:
            try:
                self.tick()
            except Exception as e:  # e.g. exchange outage; exchange-side SL/TP keep positions protected
                msg = str(e).split("<html>")[0].strip()[:200]
                self.note("ERROR", f"tick failed: {type(e).__name__}: {msg}")
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
        self.manage_tp1(positions)
        self.protect(positions)
        self.expire(positions)
        self.risk.daily_loss_hit(equity)

        bar = int(now - CANDLE_DELAY_S) // self.tf_s * self.tf_s  # start of the current candle
        if bar <= self._last_bar:
            return
        if now - bar > STALE_SIGNAL_S:  # after a restart, don't act on an old candle
            self._last_bar = bar
            self.db.set("last_bar", bar)
            return
        frames = {s: self.analyze(s) for s in self.pairs}  # may raise on an API hiccup: retried on the next tick
        self._last_bar = bar
        self.db.set("last_bar", bar)
        self.record_crowd(frames)
        self.trail(positions, frames)
        if self.risk.halted():
            if not self._halt_logged:
                self.note("WARN", "kill switch active: no new trades")
                self._halt_logged = True
            return
        self._halt_logged = False
        self.look_for_setups(positions, frames, bal, equity)

    def record_crowd(self, frames: dict) -> None:
        """Research only (no effect on trading): log retail long/short positioning for later evaluation."""
        for sym in self.pairs:
            try:
                ratio = crowd.live_retail_ratio(sym)
                pct = crowd.retail_percentile(sym, ratio)
                price = float(frames[sym]["close"].iloc[-1])
                self.db.conn.execute("INSERT INTO crowd_obs VALUES(?, ?, ?, ?, ?)", (time.time(), sym, ratio, pct, price))
                tag = " (retail very long)" if pct is not None and pct >= 0.8 else \
                      " (retail very short)" if pct is not None and pct <= 0.2 else ""
                log.info("%s retail long/short %.2f, 180-day percentile %s%s", sym, ratio,
                         f"{pct:.2f}" if pct is not None else "n/a", tag)
            except Exception as e:
                log.warning("%s crowd data unavailable: %s", sym, type(e).__name__)

    # ---------- position management ----------
    def open_trade(self, symbol: str):
        return next((t for t in self.db.open_trades() if t["symbol"] == symbol), None)

    def record_closed(self, positions: dict[str, Position]) -> None:
        """Trades open in the DB but flat on the exchange were closed (SL / trail / target / manual)."""
        for t in self.db.open_trades():
            if t["symbol"] in positions:
                continue
            for o in self.ex.open_orders(t["symbol"]):  # leftover TP1 order, if any
                try:
                    self.ex.cancel_order(t["symbol"], o["id"])
                except Exception:
                    pass
            pnl, fees, exit_price = self.realized(t)
            reason = t["exit_reason"]
            if not reason and exit_price is not None:
                reason = manage.classify_exit(t["side"], exit_price, t["sl_current"] or t["stop_loss"],
                                              t["take_profit"], bool(t["tp1_done"]))
            r = pnl / t["risk_usd"] if t["risk_usd"] else 0
            self.db.update_trade(t["id"], status="closed", closed_at=time.time(), exit_price=exit_price,
                                 exit_reason=reason or "unknown", pnl=pnl, fees=fees)
            self.note("INFO", f"CLOSED {t['symbol']} {t['side']} x{t['size']} ({reason}) "
                              f"pnl {pnl:+.2f} USD = {r:+.2f}R (fees {fees:.2f})")

    def realized(self, t) -> tuple[float, float, float | None]:
        """Net P&L, fees, avg final exit price from exchange fills since the trade opened."""
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
        direction = 1 if t["side"] == "buy" else -1
        gross = sum((px - t["entry_price"]) * direction * n * t["contract_value"] for px, n, _ in exits)
        return gross - fees, fees, exits[-1][0]

    def move_stop(self, t, pos: Position, new_sl: float, why: str) -> None:
        long = t["side"] == "buy"
        mark = self.price(t["symbol"])
        if (new_sl >= mark) if long else (new_sl <= mark):
            return  # would trigger immediately; keep the current stop
        sl_orders = [o for o in self.ex.open_orders(t["symbol"]) if o.get("stop_order_type") == "stop_loss_order"]
        if not sl_orders:
            return  # protect() will re-attach
        prices = [float(o["stop_price"]) for o in sl_orders]
        on_exchange = max(prices) if long else min(prices)
        if (on_exchange >= new_sl) if long else (on_exchange <= new_sl):
            return  # the stop on the exchange is already as tight or tighter (e.g. moved by hand): never loosen it
        for o in sl_orders:
            self.ex.edit_stop(t["symbol"], o["id"], new_sl, long)
        self.db.update_trade(t["id"], sl_current=new_sl)
        self.note("INFO", f"{t['symbol']}: stop moved to {new_sl:.2f} ({why})")

    def manage_tp1(self, positions: dict[str, Position]) -> None:
        """TP1 reached (partial filled, or price touched it) -> stop to breakeven + fees."""
        for t in self.db.open_trades():
            pos = positions.get(t["symbol"])
            if not pos or t["tp1"] is None:
                continue
            long = t["side"] == "buy"
            be = t["entry_price"] * (1 + self.m["be_fee_buffer"] if long else 1 - self.m["be_fee_buffer"])
            if not t["tp1_done"]:
                mark = self.price(t["symbol"])
                reached = abs(pos.size) < t["size"] or ((mark >= t["tp1"]) if long else (mark <= t["tp1"]))
                if not reached:
                    continue
                self.db.update_trade(t["id"], tp1_done=1)
                self.note("INFO", f"{t['symbol']}: TP1 reached ({abs(pos.size)}/{t['size']} contracts left)")
            cur = t["sl_current"] or t["stop_loss"]
            if (cur < be) if long else (cur > be):  # retried every tick until the stop is at breakeven
                self.move_stop(t, pos, be, "breakeven after TP1")

    def trail(self, positions: dict[str, Position], frames: dict) -> None:
        for t in self.db.open_trades():
            pos = positions.get(t["symbol"])
            if not pos or not t["tp1_done"]:
                continue
            lvl = brain.trail_level(frames[t["symbol"]].iloc[-1], t["side"], self.k)
            cur = t["sl_current"] or t["stop_loss"]
            better = lvl is not None and ((lvl > cur) if t["side"] == "buy" else (lvl < cur))
            if better:
                self.move_stop(t, pos, lvl, "trailing behind latest swing")

    def adopt_manual_stop(self, sym: str, pos: Position, orders: list) -> None:
        """If the stop on the exchange is tighter than the bot's record (changed by hand), adopt it so
        the records, the trailing logic and the exit label all follow the stop that is really in force."""
        t = self.open_trade(sym)
        prices = [float(o["stop_price"]) for o in orders if o.get("stop_order_type") == "stop_loss_order"]
        if not t or not prices:
            return
        long = pos.size > 0
        on_exchange = max(prices) if long else min(prices)
        cur = t["sl_current"] or t["stop_loss"]
        tol = abs(cur) * 1e-4  # tick rounding
        if (on_exchange > cur + tol) if long else (on_exchange < cur - tol):
            self.db.update_trade(t["id"], sl_current=on_exchange)
            self.note("INFO", f"{sym}: the stop on the exchange ({on_exchange:g}) is tighter than the bot's record "
                              f"({cur:.5f}); it was changed by hand. Adopting it, trailing will never loosen it")

    def protect(self, positions: dict[str, Position]) -> None:
        """Every open position must have a stop-loss and target covering its full size; else add or close."""
        for sym, pos in positions.items():
            orders = self.ex.open_orders(sym)
            sl_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "stop_loss_order")
            tp_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "take_profit_order")
            if sl_size >= abs(pos.size) and tp_size >= abs(pos.size):
                self.adopt_manual_stop(sym, pos, orders)
                continue
            time.sleep(3)  # the exchange can lag right after a fill or an edit: look again before acting
            orders = self.ex.open_orders(sym)
            sl_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "stop_loss_order")
            tp_size = sum(int(o["size"]) for o in orders if o.get("stop_order_type") == "take_profit_order")
            if sl_size >= abs(pos.size) and tp_size >= abs(pos.size):
                continue
            t = self.open_trade(sym)
            long = pos.size > 0
            if t:
                sl, tp = t["sl_current"] or t["stop_loss"], t["take_profit"]
            else:
                self.note("WARN", f"unmanaged {sym} position x{pos.size}; attaching default SL/TP")
                atr = float(self.analyze(sym)["atr"].iloc[-1])
                d = 2 * atr
                sl = pos.entry_price - d if long else pos.entry_price + d
                tp = pos.entry_price + d * self.m["runner_r"] * (1 if long else -1)
            if self.attach_bracket(sym, pos, sl, tp, orders):
                self.note("WARN", f"{sym}: SL/TP did not cover position x{pos.size}; re-attached SL {sl:.4f} TP {tp:.4f}")

    def attach_bracket(self, sym: str, pos: Position, sl: float, tp: float, orders: list | None = None) -> bool:
        """Replace any SL/TP orders with one bracket covering the whole position. Closes it if that fails."""
        try:
            for o in (orders if orders is not None else self.ex.open_orders(sym)):
                if o.get("stop_order_type") in ("stop_loss_order", "take_profit_order"):
                    self.ex.cancel_order(sym, o["id"])
            self.ex.place_position_bracket(sym, sl, tp, pos.size > 0)
            return True
        except Exception as e:
            self.note("ERROR", f"{sym}: could not attach SL/TP ({e}); closing position")
            self.close(pos, "no_stop_loss")
            return False

    def expire(self, positions: dict[str, Position]) -> None:
        for t in self.db.open_trades():
            pos = positions.get(t["symbol"])
            if pos and time.time() - t["opened_at"] >= self.m["max_hold_days"] * 86400:
                self.note("INFO", f"{t['symbol']}: max hold {self.m['max_hold_days']} days reached, closing")
                self.close(pos, "max_hold", trade_id=t["id"])

    def close(self, pos: Position, reason: str, trade_id: int | None = None) -> None:
        self.ex.close_position(pos)
        t = self.open_trade(pos.symbol)
        trade_id = trade_id or (t["id"] if t else None)
        if trade_id is not None:
            self.db.update_trade(trade_id, exit_reason=reason)

    # ---------- entries ----------
    def look_for_setups(self, positions: dict[str, Position], frames: dict, balance: float, equity: float) -> None:
        scores = self.news.get()
        self.db.set("news", ";".join(f"{p}={s:+.2f}" for p, s in scores.items())
                    + f" ({self.news.source}, {self.news.headline_count} headlines)")
        open_syms = set(positions)
        for sym in self.pairs:
            df = frames[sym]
            r = df.iloc[-1]
            log.info("%s close=%.2f trend=%+d htf=%+d atr=%.2f news=%+.2f",
                     sym, r.close, r.trend, r.htf_trend, r.atr, scores.get(sym, 0))
            st = brain.setup_at(df, len(df) - 1, self.k, scores.get(sym, 0.0), self.cfg["news"]["strong_threshold"])
            if not st:
                continue
            ok, why = self.risk.can_open(sym, open_syms, equity)
            if not ok:
                self.note("INFO", f"{sym} {st.side} setup skipped: {why} [{st.reason}]")
                continue
            if self.enter(sym, st, balance, positions):
                open_syms.add(sym)

    def enter(self, sym: str, st: brain.Setup, balance: float, positions: dict[str, Position]) -> bool:
        product = self.ex.product(sym)
        open_notional = sum(abs(p.size) * self.ex.product(p.symbol).contract_value * self.price(p.symbol)
                            for p in positions.values())
        risk_cfg = dict(self.cfg["risk"], risk_per_trade_pct=self.cfg["risk"]["risk_per_trade_pct"] * st.risk_mult)
        sizing = size_position(balance, st.price, st.stop_loss, product.contract_value, risk_cfg, open_notional)
        if sizing.contracts < 1:
            self.note("INFO", f"{sym} {st.side} setup skipped: {sizing.note} [{st.reason}]")
            return False
        long = st.side == "buy"
        opened_at = time.time()
        limit_filled = self._limit_entry(sym, st, sizing.contracts)
        px = self.price(sym)
        ran = (px - st.price) * (1 if long else -1)
        beyond = (px <= st.stop_loss or px >= st.tp1) if long else (px >= st.stop_loss or px <= st.tp1)
        too_far = ran > MAX_CHASE_ATR * st.atr or beyond  # never chase a setup that already ran away
        if not limit_filled:
            if too_far:
                self.note("INFO", f"{sym} {st.side}: price moved to {px:.4f}, not chasing")
                return False
            try:
                self.ex.place_entry(sym, st.side, sizing.contracts, st.stop_loss, st.runner_tp)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.note("WARN", f"{sym}: market entry timed out ({type(e).__name__}); checking the exchange")
            time.sleep(3)
        else:
            # Thin books fill a post-only order only partly. Top up the rest at market (same SL/TP),
            # keeping total risk within the planned budget, so the trade is not left at a fraction of its size.
            cur = next((p for p in self.ex.positions() if p.symbol == sym), None)
            have = abs(cur.size) if cur else 0
            if cur and have < sizing.contracts:
                per_c = abs(px - st.stop_loss) * product.contract_value
                spent = have * abs(cur.entry_price - st.stop_loss) * product.contract_value
                rem = min(sizing.contracts - have, int((sizing.risk_usd - spent) // per_c) if per_c > 0 else 0)
                if rem >= 1 and not too_far:
                    try:
                        self.ex.place_market(sym, st.side, rem)  # no bracket allowed on an existing position
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                        self.note("WARN", f"{sym}: top-up timed out ({type(e).__name__}); checking the exchange")
                    time.sleep(2)
                    cur = next((p for p in self.ex.positions() if p.symbol == sym), cur)
                    self.attach_bracket(sym, cur, st.stop_loss, st.runner_tp)  # one SL/TP over the whole position
                    self.note("INFO", f"{sym}: limit order filled {have}/{sizing.contracts}; topped up {rem} at market")
                else:
                    self.note("WARN", f"{sym}: only {have}/{sizing.contracts} contracts filled and no top-up "
                                      f"({'price moved' if too_far else 'risk budget'}); keeping the partial position")
        pos = next((p for p in self.ex.positions() if p.symbol == sym), None)
        if not pos:
            self.note("WARN", f"{sym} {st.side}: entry not filled")
            return False
        size = abs(pos.size)
        d = 1 if long else -1
        dist = abs(pos.entry_price - st.stop_loss)
        tp1 = pos.entry_price + d * dist * self.m["tp1_r"]
        tp1_size = int(round(size * self.m["tp1_fraction"]))
        if not 0 < tp1_size < size:
            tp1_size = 0  # too small to split: TP1 only moves the stop to breakeven
        self.db.open_trade(
            symbol=sym, side=st.side, size=size, contract_value=product.contract_value,
            entry_price=pos.entry_price, stop_loss=st.stop_loss, take_profit=st.runner_tp,
            risk_usd=dist * size * product.contract_value, reason=st.reason, opened_at=opened_at,
            tp1=tp1, tp1_size=tp1_size, sl_current=st.stop_loss,
        )
        positions[sym] = pos
        if tp1_size:
            self.ex.place_reduce_limit(sym, "sell" if long else "buy", tp1_size, tp1)
        self.note("INFO", f"OPENED {sym} {st.side} x{size}/{sizing.contracts} planned @ {pos.entry_price} "
                          f"SL {st.stop_loss:.4f} TP1 {tp1:.4f} ({tp1_size} contracts) runner {st.runner_tp:.4f} "
                          f"risk ${dist * size * product.contract_value:.2f} of ${sizing.risk_usd:.2f} planned [{st.reason}]")
        self.protect({sym: pos})
        return True

    def _limit_entry(self, sym: str, st: brain.Setup, size: int) -> bool:
        """Post-only limit at the touch. True if (at least partly) filled."""
        bid, ask = self.ex.best_bid_ask(sym)
        try:
            o = self.ex.place_entry(sym, st.side, size, st.stop_loss, st.runner_tp,
                                    limit_price=bid if st.side == "buy" else ask)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # Outcome unknown: the order may exist. Check before doing anything else (never double-enter).
            self.note("WARN", f"{sym}: limit entry timed out ({type(e).__name__}); checking the exchange")
            time.sleep(3)
            if any(p.symbol == sym for p in self.ex.positions()):
                return True
            resting = [x for x in self.ex.open_orders(sym) if not x.get("stop_order_type") and not x.get("reduce_only")]
            if not resting:
                return False
            o = resting[-1]
        except Exception as e:  # explicit rejection (e.g. post-only would cross): the order does not exist
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
    lock = open(ROOT / "data" / f"trader-{cfg['exchange']['environment']}.lock", "w")
    try:  # two bots on one account would double every order
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another bot instance is already running")
    setup_logging(cfg)
    Trader(cfg).run(once=args.once)


if __name__ == "__main__":
    main()
