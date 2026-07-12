"""Registry of direct publisher RSS feeds for the news subsystem (G5).

Until now all news came from a single Google News RSS query
(`data/tw/google_news_tw_connector.py`), which aggregates several
Taiwanese publishers but only exposes a Google-rewritten title +
redirect link (no article body, publisher buried in a title suffix).
This registry adds **direct** publisher feeds alongside it, so we get
first-party links (full-text extractable — G5 phase 2) and broader,
less-deduped coverage.

Each feed is polled by `tasks/ingest_news_feeds.py`, parsed by the
generic `data/rss_connector.py`, and written through the SAME
`insert_news_articles` path as Google News. Dedup is
`sha256(normalised_title + canonical_link)`, so an article that shows
up in both Google News and its origin feed collapses to one row — the
direct-feed version wins whichever lands first; both carry the same
title so the dedup hash matches regardless of source.

Feed URLs were reachability-verified before being enabled (HTTP 200 +
valid RSS with a healthy item count). Publishers that block datacenter
IPs or 404 their advertised feed (工商時報, MoneyDJ) are intentionally
omitted; flip `enabled=False` (or add new entries) as feeds change —
this is a code change, matching the existing `DEFAULT_QUERY` convention.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsSource:
    """One RSS feed. `key` is the stable `news_articles.source` tag
    (≤24 chars to fit the column); `name` is the display publisher."""

    key: str
    name: str
    url: str
    market: str          # "TW" | "GLOBAL"
    lang: str            # e.g. "zh-TW"
    enabled: bool = True
    # Some publishers put the article body in the RSS <description>;
    # others (most) only a teaser. Informational for the full-text
    # phase — not consumed yet.
    full_description: bool = False


# Direct publisher feeds. Google News RSS stays on its own task
# (`ingest_news_tw` / `ingest_news_international`) and is deliberately
# NOT registered here — it's an aggregator, not a first-party feed.
SOURCES: tuple[NewsSource, ...] = (
    NewsSource(
        key="cnyes",
        name="鉅亨網",
        url="https://news.cnyes.com/rss/v1/news/category/headline",
        market="TW",
        lang="zh-TW",
    ),
    NewsSource(
        key="udn_money",
        name="經濟日報",
        url="https://money.udn.com/rssfeed/news/1001/5590?ch=money",
        market="TW",
        lang="zh-TW",
    ),
    NewsSource(
        key="ltn_ec",
        name="自由財經",
        url="https://news.ltn.com.tw/rss/business.xml",
        market="TW",
        lang="zh-TW",
    ),
    NewsSource(
        key="cna_finance",
        name="中央社財經",
        url="https://feeds.feedburner.com/rsscna/finance",
        market="TW",
        lang="zh-TW",
    ),
)


def all_sources() -> tuple[NewsSource, ...]:
    return SOURCES


def enabled_sources(market: str | None = None) -> list[NewsSource]:
    """Registered feeds that are enabled, optionally filtered by market."""
    return [
        s for s in SOURCES
        if s.enabled and (market is None or s.market == market)
    ]


def get_source(key: str) -> NewsSource | None:
    for s in SOURCES:
        if s.key == key:
            return s
    return None
