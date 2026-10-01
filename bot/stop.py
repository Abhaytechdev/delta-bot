"""Kill switch.

  python -m bot.stop               halt new trades now, cancel pending entry orders
                                   (stop-loss/take-profit orders on open positions are kept)
  python -m bot.stop --close-all   also close every open position at market
  python -m bot.stop --resume      clear the kill switch
"""

import argparse

from bot.config import load_config, load_secrets
from bot.db import DB
from bot.exchange import Exchange
from bot.risk import HALT_KEY


def main() -> None:
    ap = argparse.ArgumentParser(description="Delta bot kill switch")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--close-all", action="store_true", help="also close all positions at market")
    g.add_argument("--resume", action="store_true", help="clear the kill switch")
    args = ap.parse_args()

    cfg = load_config()
    db = DB(cfg["storage"]["db_path"])
    if args.resume:
        db.delete(HALT_KEY)
        db.event("WARN", "kill switch cleared (resume)")
        print("Kill switch cleared. Bot will trade again at the next candle.")
        return

    db.set(HALT_KEY, "1")  # set first: even if the exchange calls fail, the bot stops entering
    db.event("WARN", "KILL SWITCH activated" + (" with --close-all" if args.close_all else ""))
    print("Kill switch ON: no new trades.")

    ex = Exchange(cfg, load_secrets())
    for o in ex.open_orders():
        if not o.get("stop_order_type") and o.get("reduce_only") is not True:
            ex.client.cancel_order(o["product_id"], o["id"])
            print(f"  cancelled pending entry order {o['id']} ({o['product_symbol']})")
    if args.close_all:
        for p in ex.positions():
            ex.close_position(p)
            print(f"  closed {p.symbol} x{p.size}")
    left = ex.positions()
    print(f"Open positions: {len(left)}" + ("" if args.close_all else " (still protected by their stop-loss/target)"))


if __name__ == "__main__":
    main()
