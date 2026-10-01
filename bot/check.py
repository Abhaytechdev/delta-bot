"""Read-only testnet connection check: python -m bot.check"""

from bot.config import load_config, load_secrets
from bot.exchange import Exchange


def main() -> None:
    cfg = load_config()
    ex = Exchange(cfg, load_secrets())
    print(f"Exchange: {cfg['exchange']['environment']} ({cfg['exchange']['rest_url']})")
    for sym in cfg["trading"]["pairs"]:
        p = ex.product(sym)
        candles = ex.candles(sym, cfg["trading"]["timeframe"], 5)
        print(f"{sym}: id={p.id} contract={p.contract_value} mark={ex.mark_price(sym):.2f} "
              f"last {cfg['trading']['timeframe']} close={candles[-1]['close']}")
    bal, avail = ex.balance_usd()
    print(f"Balance: {bal:.2f} USD (available {avail:.2f})")
    print(f"Open positions: {len(ex.positions())}  open orders: {len(ex.open_orders())}")
    print("OK")


if __name__ == "__main__":
    main()
