"""News sentiment from free crypto RSS feeds.

Score per pair in [-1, 1]. Uses Claude when ANTHROPIC_API_KEY is set, otherwise a
keyword scorer. |score| >= news.strong_threshold counts as strong news.
"""

import calendar
import logging
import math
import re
import time
from dataclasses import dataclass

import feedparser

from bot import llm

log = logging.getLogger(__name__)

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.theblock.co/rss.xml",
]

COIN_TERMS = {
    "BTCUSD": re.compile(r"\b(bitcoin|btc)\b", re.I),
    "ETHUSD": re.compile(r"\b(ethereum|eth|ether)\b", re.I),
    "ADAUSD": re.compile(r"\b(cardano|ada)\b", re.I),
    "SOLUSD": re.compile(r"\b(solana|sol)\b", re.I),
    "XRPUSD": re.compile(r"\b(xrp|ripple)\b", re.I),
    "DOGEUSD": re.compile(r"\b(dogecoin|doge)\b", re.I),
    "BNBUSD": re.compile(r"\b(bnb|binance coin|bnb chain)\b", re.I),
}
MARKET_TERMS = re.compile(r"\b(crypto|cryptocurrency|market|sec|fed|etf|stablecoin|regulat\w*)\b", re.I)

# (pattern, weight). Strong events ~1.0, mild ~0.4.
LEXICON = [
    (r"\betf (approv\w*|inflows?)\b", 1.0), (r"\bapprov(ed|es|al)\b", 0.6), (r"\b(all[- ]time high|ath|record high)\b", 0.8),
    (r"\b(surge[sd]?|soar\w*|rall(y|ies|ied)|jump\w*|breakout)\b", 0.6), (r"\b(rate cut|cuts rates)\b", 0.7),
    (r"\b(adopt\w*|partnership|launch\w*|upgrade)\b", 0.3), (r"\b(gain\w*|rise[sn]?|climb\w*|bull\w*)\b", 0.4),
    (r"\b(inflows?|accumulat\w*|buys?|bought)\b", 0.4),
    (r"\b(hack\w*|exploit\w*|stolen|drain\w*|breach\w*|security incident)\b", -1.0), (r"\b(ban\w*|crackdown|lawsuit|sues|sued|charges?)\b", -0.8),
    (r"\b(crash\w*|plunge\w*|tumble\w*|collapse\w*|liquidat\w*|sell-?off)\b", -0.8), (r"\b(rate hike|hikes rates)\b", -0.7),
    (r"\b(insolven\w*|bankrupt\w*|default\w*|delist\w*)\b", -0.9), (r"\b(fall[sn]?|drop\w*|slide\w*|slump\w*|bear\w*|fades?)\b", -0.4),
    (r"\b(outflows?|dump\w*|sells?|sold)\b", -0.4), (r"\b(fear|concern\w*|risk\w*|warn\w*)\b", -0.2),
]
LEXICON = [(re.compile(p, re.I), w) for p, w in LEXICON]


@dataclass(frozen=True)
class Headline:
    title: str
    ts: float
    source: str


def fetch_headlines(lookback_hours: float, now: float | None = None) -> list[Headline]:
    now = now or time.time()
    seen, out = set(), []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 delta-bot")
        except Exception as e:
            log.warning("feed failed %s: %s", url, e)
            continue
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            title = (e.get("title") or "").strip()
            if not t or not title:
                continue
            ts = calendar.timegm(t)
            key = title.lower()[:80]
            if now - ts <= lookback_hours * 3600 and key not in seen:
                seen.add(key)
                out.append(Headline(title, ts, url.split("/")[2]))
    return sorted(out, key=lambda h: h.ts, reverse=True)


def keyword_score(title: str) -> float:
    s = sum(w for p, w in LEXICON if p.search(title))
    return max(-1.0, min(1.0, s))


def keyword_scores(headlines: list[Headline], pairs: list[str]) -> dict[str, float]:
    out = {}
    for pair in pairs:
        total = 0.0
        for h in headlines:
            if COIN_TERMS[pair].search(h.title):
                total += keyword_score(h.title)
            elif MARKET_TERMS.search(h.title) and not any(r.search(h.title) for r in COIN_TERMS.values()):
                total += 0.5 * keyword_score(h.title)
        # two strong coin-specific headlines are needed to cross 0.5
        out[pair] = math.tanh(total / 2)
    return out


CLAUDE_SYSTEM = (
    "You score crypto news for a trading bot. For each pair, judge the likely price impact "
    "over the next few hours of the headlines given, from -1 (strongly bearish) to +1 "
    "(strongly bullish). Use |score| >= 0.5 only for genuinely major, market-moving news "
    "(ETF decisions, major hacks, regulation, macro shocks). Routine news is near 0."
)


def claude_scores(api_key: str, headlines: list[Headline], pairs: list[str]) -> dict[str, float] | None:
    if not api_key or not headlines:
        return None
    schema = {
        "type": "object",
        "properties": {p: {"type": "number"} for p in pairs} | {"summary": {"type": "string"}},
        "required": [*pairs, "summary"],
        "additionalProperties": False,
    }
    lines = "\n".join(f"- [{time.strftime('%H:%M', time.gmtime(h.ts))} UTC] {h.title}" for h in headlines[:60])
    res = llm.ask_json(api_key, CLAUDE_SYSTEM, f"Pairs: {', '.join(pairs)}\nHeadlines:\n{lines}", schema)
    if not res:
        return None
    log.info("Claude news summary: %s", res.get("summary", "")[:300])
    return {p: max(-1.0, min(1.0, float(res[p]))) for p in pairs}


class NewsMonitor:
    """Caches scores; refreshes at most every `refresh_s` seconds."""

    def __init__(self, cfg: dict, api_key: str = "", refresh_s: int = 900):
        self.pairs = cfg["trading"]["pairs"]
        self.lookback = cfg["news"]["lookback_hours"]
        self.api_key = api_key
        self.refresh_s = refresh_s
        self.scores: dict[str, float] = {p: 0.0 for p in self.pairs}
        self.source = "none"
        self.headline_count = 0
        self._last = 0.0

    def get(self) -> dict[str, float]:
        if time.time() - self._last >= self.refresh_s:
            self.refresh()
        return self.scores

    def refresh(self) -> None:
        self._last = time.time()
        try:
            hs = fetch_headlines(self.lookback)
        except Exception as e:
            log.warning("news fetch failed: %s", e)
            return
        self.headline_count = len(hs)
        scores = claude_scores(self.api_key, hs, self.pairs)
        self.source = "claude" if scores else "keywords"
        self.scores = scores or keyword_scores(hs, self.pairs)
