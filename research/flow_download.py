"""Download Binance futures aggTrades day by day, boil each day down to 1-minute trade-flow bars, delete the raw file.

Output: data/flow/<SYMBOL>_<date>.csv with columns
  minute, open, high, low, close, buy_qty, sell_qty, n_trades, big_buy_usd, big_sell_usd
(buy = the buyer was the aggressor, i.e. a market buy hit the ask). Also data/flow/metrics_<SYMBOL>_<date>.csv (5-min OI, long/short ratios, taker ratio).
"""
import io, os, sys, time, zipfile, datetime as dt, urllib.request
import pandas as pd, numpy as np

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "flow")
BIG_USD = {"BTCUSDT": 100_000, "ETHUSDT": 100_000, "ADAUSDT": 30_000}
BASE = "https://data.binance.vision/data/futures/um/daily/{kind}/{s}/{s}-{kind}-{d}.zip"


def fetch(url, tries=6):
    for k in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=120).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(5)
        except Exception:
            time.sleep(5 * (k + 1))
    return None


def day_flow(sym, d):
    raw = fetch(BASE.format(kind="aggTrades", s=sym, d=d))
    if raw is None:
        return False
    z = zipfile.ZipFile(io.BytesIO(raw))
    parts = []
    with z.open(z.namelist()[0]) as f:
        for ch in pd.read_csv(f, chunksize=1_500_000):
            ch.columns = ["id", "price", "qty", "first", "last", "ts", "maker"]
            ch["maker"] = ch["maker"].astype(str).str.lower().eq("true")
            ch["minute"] = ch["ts"] // 60000 * 60
            usd = ch["price"] * ch["qty"]
            big = usd >= BIG_USD[sym]
            ch["buy_qty"] = np.where(~ch["maker"], ch["qty"], 0.0)
            ch["sell_qty"] = np.where(ch["maker"], ch["qty"], 0.0)
            ch["big_buy_usd"] = np.where(big & ~ch["maker"], usd, 0.0)
            ch["big_sell_usd"] = np.where(big & ch["maker"], usd, 0.0)
            g = ch.groupby("minute")
            parts.append(pd.DataFrame({
                "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(), "close": g["price"].last(),
                "buy_qty": g["buy_qty"].sum(), "sell_qty": g["sell_qty"].sum(), "n_trades": g["price"].size(),
                "big_buy_usd": g["big_buy_usd"].sum(), "big_sell_usd": g["big_sell_usd"].sum()}))
    df = pd.concat(parts)
    g = df.groupby(level=0)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                        "buy_qty": g["buy_qty"].sum(), "sell_qty": g["sell_qty"].sum(), "n_trades": g["n_trades"].sum(),
                        "big_buy_usd": g["big_buy_usd"].sum(), "big_sell_usd": g["big_sell_usd"].sum()})
    out.index.name = "minute"
    out.to_csv(os.path.join(OUT, f"{sym}_{d}.csv"))
    return True


def day_metrics(sym, d):
    raw = fetch(BASE.format(kind="metrics", s=sym, d=d))
    if raw is None:
        return
    z = zipfile.ZipFile(io.BytesIO(raw))
    with z.open(z.namelist()[0]) as f:
        pd.read_csv(f).to_csv(os.path.join(OUT, f"metrics_{sym}_{d}.csv"), index=False)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    end = dt.date.today() - dt.timedelta(days=2)
    for i in range(days):
        d = (end - dt.timedelta(days=i)).isoformat()
        for sym in BIG_USD:
            p = os.path.join(OUT, f"{sym}_{d}.csv")
            if not os.path.exists(p):
                ok = day_flow(sym, d)
                print(d, sym, "ok" if ok else "missing", flush=True)
            if not os.path.exists(os.path.join(OUT, f"metrics_{sym}_{d}.csv")):
                day_metrics(sym, d)
