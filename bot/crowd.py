"""Crowd positioning data (the "psychology" behind price), Binance USDT perps as a proxy:

- funding rate (who pays to hold: crowded longs pay shorts), history from fapi
- open interest, top-trader and global long/short ratios, taker buy/sell ratio,
  from data.binance.vision daily "metrics" files (5-minute rows, from ~2021)

  python -m bot.crowd --days 2190     # download/refresh the cache in data/crowd/
"""

import argparse
import concurrent.futures as cf
import io
import time
import urllib.request
import zipfile
from datetime import date, timedelta

import pandas as pd

from bot.config import ROOT

LIVE_RETAIL = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={s}&period=4h&limit=1"
METRICS = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
DIR = ROOT / "data" / "crowd"
SYMBOL_MAP = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT", "ADAUSD": "ADAUSDT", "SOLUSD": "SOLUSDT",
              "XRPUSD": "XRPUSDT", "DOGEUSD": "DOGEUSDT"}


def _fetch_day(sym: str, d: date) -> pd.DataFrame | None:
    path = DIR / sym / f"{d}.csv"
    if path.exists():
        return pd.read_csv(path) if path.stat().st_size else None
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            raw = urllib.request.urlopen(METRICS.format(s=sym, d=d), timeout=30).read()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                df = pd.read_csv(z.open(z.namelist()[0]))
            df.to_csv(path, index=False)
            return df
        except urllib.error.HTTPError as e:
            if e.code == 404:
                path.write_text("")  # no file for that day: remember it
                return None
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def download(symbol: str, days: int, workers: int = 8) -> None:
    sym = SYMBOL_MAP[symbol]
    end = date.today() - timedelta(days=1)
    dates = [end - timedelta(days=i) for i in range(days)]
    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda d: _fetch_day(sym, d), dates))


def metrics(symbol: str, tf_seconds: int) -> pd.DataFrame:
    """Cached metrics resampled to candle buckets (value at the end of each candle). Index = candle open time."""
    sym = SYMBOL_MAP[symbol]
    files = sorted((DIR / sym).glob("*.csv"))
    parts = [pd.read_csv(f) for f in files if f.stat().st_size]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df["ts"] = (pd.to_datetime(df["create_time"], utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta("1s")
    df = df.sort_values("ts").drop_duplicates("ts")
    df["bucket"] = df["ts"] // tf_seconds * tf_seconds
    cols = {"sum_open_interest_value": "oi_usd", "sum_toptrader_long_short_ratio": "top_ls",
            "count_long_short_ratio": "global_ls", "sum_taker_long_short_vol_ratio": "taker_ratio"}
    agg = df.groupby("bucket").agg({"sum_open_interest_value": "last", "sum_toptrader_long_short_ratio": "last",
                                     "count_long_short_ratio": "last", "sum_taker_long_short_vol_ratio": "mean"})
    return agg.rename(columns=cols)


def live_retail_ratio(symbol: str) -> float:
    """Latest Binance global (retail) long/short account ratio for the pair."""
    import json
    data = json.load(urllib.request.urlopen(LIVE_RETAIL.format(s=SYMBOL_MAP[symbol]), timeout=15))
    return float(data[-1]["longShortRatio"])


def retail_percentile(symbol: str, value: float, days: int = 180) -> float | None:
    """Share of the last `days` of cached 4h readings below `value` (needs the data/crowd cache)."""
    m = metrics(symbol, 14400)
    if m.empty:
        return None
    hist = m["global_ls"].dropna()
    hist = hist[hist.index >= hist.index.max() - days * 86400]
    return float((hist < value).mean()) if len(hist) > 100 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2190)
    ap.add_argument("--pairs", nargs="+", default=["BTCUSD", "ETHUSD", "ADAUSD"])
    args = ap.parse_args()
    for s in args.pairs:
        t = time.time()
        download(s, args.days)
        n = len([f for f in (DIR / SYMBOL_MAP[s]).glob("*.csv") if f.stat().st_size])
        print(f"{s}: {n} days cached ({time.time() - t:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
