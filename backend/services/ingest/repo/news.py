"""News articles + corporate announcements (TW MOPS 重大訊息)."""
import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.news_article import NewsArticle

log = logging.getLogger(__name__)


# ── News articles ──────────────────────────────────────────────────

# Strip whitespace + punctuation noise so the same article republished
# with trailing dots / smart quotes still hashes identically.
_NOISE_RE = re.compile(r"[\s　\.,;:!?。，！？“”‘’]+")


def _normalize_title(title: str) -> str:
    return _NOISE_RE.sub("", (title or "").lower())


def _canonical_link(link: str) -> str:
    """Strip query-string tracking params (utm_*, ref=...) so the same
    article shared via different campaigns deduplicates correctly."""
    if "?" not in link:
        return link.strip()
    base, _, qs = link.partition("?")
    keep = [
        kv for kv in qs.split("&")
        if kv and not kv.split("=", 1)[0].lower().startswith(("utm_", "ref", "fbclid", "gclid"))
    ]
    return base + (("?" + "&".join(keep)) if keep else "")


def compute_dedup_hash(title: str, link: str) -> str:
    raw = _normalize_title(title) + "|" + _canonical_link(link)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewsArticleRow:
    market: str
    symbol: str | None
    published_at: datetime
    title: str
    link: str
    publisher: str | None
    summary: str | None
    payload: dict[str, Any] | None
    source: str

    @property
    def dedup_hash(self) -> str:
        return compute_dedup_hash(self.title, self.link)


async def insert_news_articles(
    db: AsyncSession, rows: Iterable[NewsArticleRow],
) -> int:
    """Bulk insert with on-conflict-do-nothing on `dedup_hash`. Returns
    the number of input rows passed (not the number actually inserted —
    duplicates are silently dropped at the DB layer).

    Chunks the INSERT into batches of `_INSERT_NEWS_CHUNK_ROWS` so we
    don't blow past PostgreSQL's wire-protocol limit of 32767 query
    parameters per statement (`asyncpg.InterfaceError`). NewsArticleRow
    has 10 fields so 2 000 rows = 20 000 params, comfortably below the
    cap with headroom for future field additions. A 30-day backtest
    backfill of a busy news symbol can easily yield 5 000+ rows
    (~250/day × 31 days), which previously aborted the entire batch
    with zero rows persisted.
    """
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "published_at": r.published_at,
            "title": r.title,
            "link": r.link,
            "publisher": r.publisher,
            "summary": r.summary,
            "payload": r.payload,
            "source": r.source,
            "dedup_hash": r.dedup_hash,
        }
        for r in rows
        if r.title and r.link
    ]
    if not payload:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert

    for i in range(0, len(payload), _INSERT_NEWS_CHUNK_ROWS):
        chunk = payload[i:i + _INSERT_NEWS_CHUNK_ROWS]
        stmt = insert_fn(NewsArticle).values(chunk).on_conflict_do_nothing(
            index_elements=["dedup_hash"],
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


# PostgreSQL's wire protocol caps a single statement at 32767 query
# parameters. NewsArticleRow has 10 fields → 2 000 rows = 20 000
# params, leaving headroom. A FinMind day-by-day backfill over a
# busy 31-day window can return 5 000+ articles for one popular
# symbol, which used to crash the whole batch with
# `asyncpg.InterfaceError`.
_INSERT_NEWS_CHUNK_ROWS = 2_000


async def read_recent_news(
    db: AsyncSession,
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 20,
    max_age_days: int = 30,
    include_sentiment: bool = False,
) -> list[dict[str, Any]]:
    """Return the most recent articles for `(market, symbol)` newer than
    `max_age_days`. `symbol=None` matches market-wide articles
    (NULL symbol) only; pass an explicit symbol to fetch per-symbol news.

    Output shape mirrors `tw_market_service.get_news` so the caller can
    return the list verbatim. With `include_sentiment=True` each row
    additionally carries `sentiment_score` (-1..+1 or None) and
    `sentiment_label` (`bullish` / `bearish` / `neutral` or None) — used
    by the dashboard's RecentTWNews card to render colored badges
    without an extra round-trip.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.market == market, NewsArticle.published_at >= cutoff)
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    if symbol is None:
        stmt = stmt.where(NewsArticle.symbol.is_(None))
    else:
        stmt = stmt.where(NewsArticle.symbol == symbol)
    rows = (await db.scalars(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "title":        r.title,
            "publisher":    r.publisher or "",
            "link":         r.link,
            "published_at": r.published_at.isoformat(),
            "thumbnail":    (r.payload or {}).get("thumbnail") if r.payload else None,
            "data_source":  r.source,
        }
        if include_sentiment:
            item["sentiment_score"] = r.sentiment_score
            item["sentiment_label"] = r.sentiment_label
        out.append(item)
    return out


async def read_news_needing_body(
    db: AsyncSession,
    *,
    source_keys: Sequence[str],
    limit: int = 30,
    max_age_days: int = 7,
) -> list[tuple[int, str]]:
    """(id, link) of recent articles from `source_keys` that haven't had
    a full-text extraction attempt yet (`body_fetched_at IS NULL`),
    newest first. Scoped to the full-text-capable direct-feed sources so
    we never try to fetch a Google-News redirect link."""
    if not source_keys:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(NewsArticle.id, NewsArticle.link)
        .where(
            NewsArticle.source.in_(list(source_keys)),
            NewsArticle.body_fetched_at.is_(None),
            NewsArticle.published_at >= cutoff,
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    return [(int(r[0]), r[1]) for r in (await db.execute(stmt)).all()]


async def update_news_body(
    db: AsyncSession, article_id: int, body: str | None
) -> None:
    """Persist an extraction result. Always stamps `body_fetched_at` (so
    a NULL-body attempt isn't retried); `body` may be None on a failed /
    empty extract."""
    await db.execute(
        sa_update(NewsArticle)
        .where(NewsArticle.id == article_id)
        .values(body=body, body_fetched_at=datetime.now(UTC))
    )
    await db.commit()


async def insert_news_articles_autosession(rows: Iterable[NewsArticleRow]) -> int:
    """Open own session + insert. Errors logged + swallowed."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await insert_news_articles(db, rows)
    except Exception as exc:
        log.warning("ingest.news.write_error",
                    extra={"market": rows[0].market, "count": len(rows), "error": str(exc)})
        return 0


async def read_recent_news_autosession(
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 20,
    max_age_days: int = 30,
    include_sentiment: bool = False,
) -> list[dict[str, Any]]:
    """Open own session + read. Errors logged; returns [] so the read
    path falls through cleanly to upstream."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_recent_news(
                db, market,
                symbol=symbol, limit=limit, max_age_days=max_age_days,
                include_sentiment=include_sentiment,
            )
    except Exception as exc:
        log.warning("ingest.news.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


# ── Corporate announcements (TW MOPS 重大訊息, PR-D1) ───────────────


@dataclass(frozen=True)
class CorporateAnnouncementRow:
    """Canonical insert shape for `corporate_announcements`. Mirrors
    `NewsArticleRow` everywhere it makes sense (sentiment fields are
    populated downstream by the same scorer); the meaningful
    differences are `category` (required) and `body` (separate from
    title because MOPS' multi-paragraph disclosures benefit from
    being quoted verbatim into the discussion ctx)."""
    market: str
    symbol: str
    announced_at: datetime
    category: str
    title: str
    body: str | None
    source_url: str | None
    source: str
    dedup_hash: str


async def insert_corporate_announcements(
    db: AsyncSession,
    rows: Iterable[CorporateAnnouncementRow],
) -> int:
    """Bulk insert with on-conflict-do-nothing on `dedup_hash`. Same
    chunking strategy as `insert_news_articles` (PG wire-protocol
    32 767 param cap; 11 fields per row → 2 000 rows = 22 000 params,
    headroom-comfortable)."""
    from models.corporate_announcement import CorporateAnnouncement

    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "announced_at": r.announced_at,
            "category": r.category,
            "title": r.title,
            "body": r.body,
            "source_url": r.source_url,
            "source": r.source,
            "dedup_hash": r.dedup_hash,
        }
        for r in rows
        if r.title and r.symbol
    ]
    if not payload:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert

    for i in range(0, len(payload), _INSERT_NEWS_CHUNK_ROWS):
        chunk = payload[i:i + _INSERT_NEWS_CHUNK_ROWS]
        stmt = insert_fn(CorporateAnnouncement).values(chunk).on_conflict_do_nothing(
            index_elements=["dedup_hash"],
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


async def insert_corporate_announcements_autosession(
    rows: Iterable[CorporateAnnouncementRow],
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await insert_corporate_announcements(db, rows)
    except Exception as exc:
        log.warning(
            "ingest.announcements.write_error",
            extra={
                "market": rows[0].market, "count": len(rows),
                "error": str(exc),
            },
        )
        return 0


async def read_recent_announcements(
    db: AsyncSession,
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 10,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    """Read the most recent material-info disclosures for
    `(market, symbol)` newer than `max_age_days`. Used by the
    discussion ctx block (PR-D4 wires it). Default window is 7
    days because MOPS material info loses signal value fast — the
    market has already priced it in within a session or two for
    most categories.

    Returns the rows as plain dicts so the ctx path can serialize
    cleanly without an ORM dependency. Sentiment columns are
    included so personas can see "this earnings disclosure scored
    +0.6 (bullish)" inline.
    """
    from models.corporate_announcement import CorporateAnnouncement

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(CorporateAnnouncement)
        .where(
            CorporateAnnouncement.market == market,
            CorporateAnnouncement.announced_at >= cutoff,
        )
        .order_by(CorporateAnnouncement.announced_at.desc())
        .limit(limit)
    )
    if symbol is not None:
        stmt = stmt.where(CorporateAnnouncement.symbol == symbol)
    rows = (await db.scalars(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "symbol":              r.symbol,
            "announced_at":        r.announced_at.isoformat(),
            "category":            r.category,
            "title":               r.title,
            "body":                r.body,
            "source_url":          r.source_url,
            "sentiment_score":     r.sentiment_score,
            "sentiment_label":     r.sentiment_label,
        })
    return out


async def read_recent_announcements_autosession(
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 10,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    try:
        async with AsyncSessionLocal() as db:
            return await read_recent_announcements(
                db, market,
                symbol=symbol, limit=limit, max_age_days=max_age_days,
            )
    except Exception as exc:
        log.warning(
            "ingest.announcements.read_error",
            extra={"market": market, "symbol": symbol, "error": str(exc)},
        )
        return []
