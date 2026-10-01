import time

from bot.news import Headline, keyword_score, keyword_scores

PAIRS = ["BTCUSD", "ETHUSD"]


def h(title):
    return Headline(title, time.time(), "test")


def test_keyword_polarity():
    assert keyword_score("Bitcoin ETF approved by SEC") > 0.5
    assert keyword_score("Major exchange hacked, $200M stolen") <= -1.0
    assert keyword_score("Bitcoin developers meet in Lisbon") == 0


def test_single_headline_is_not_strong():
    s = keyword_scores([h("Ethereum bridge hacked")], PAIRS)
    assert -0.5 < s["ETHUSD"] < 0 and s["BTCUSD"] == 0


def test_multiple_strong_headlines_are_strong_and_coin_specific():
    s = keyword_scores([h("Bitcoin ETF inflows hit record high"), h("Bitcoin surges past 100k")], PAIRS)
    assert s["BTCUSD"] >= 0.5 and s["ETHUSD"] == 0


def test_market_wide_news_counts_half():
    s = keyword_scores([h("Crypto market crash wipes out $1B")], PAIRS)
    assert s["BTCUSD"] == s["ETHUSD"] and -0.5 < s["BTCUSD"] < 0
