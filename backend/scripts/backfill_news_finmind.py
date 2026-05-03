"""One-shot TW news backfill from FinMind paid history.

The hourly Google News RSS ingest only sees the last ~14 days of
articles, and FinMind's `TaiwanStockNews` was paywalled in 2026-04 so
the free-tier path returns empty. With a paid sponsor token, this
script pulls the entire historical archive (FinMind starts ~2017) into
`news_articles` so backtest discussions anchored at a past date can
read the news_sentiment block instead of falling back to the "archive
doesn't reach this date" warning.

Run from inside the backend dir:

    # one-time setup: paid token in env (or admin-panel-stored DB key)
    export FINMIND_TOKEN=<paid-tier-token>

    # backfill the last 2 years
    python -m scripts.backfill_news_finmind --start 2024-01-01 --end 2026-04-30

    # narrower window (e.g. just the discussion's anchor week)
    python -m scripts.backfill_news_finmind --start 2026-03-01 --end 2026-03-31

The script walks the date range in 30-day chunks (FinMind responses
get unwieldy past that), upserts via `insert_news_articles` whose
sha256(title+link) dedup makes re-runs idempotent — running the same
range twice writes nothing the second time, no manual cleanup needed.

Sentiment scoring: new rows arrive with `sentiment_score=NULL`. The
existing hourly `tasks.score_news_sentiment` cron picks them up over
time at the configured `SENTIMENT_DAILY_LLM_CALL_CAP` rate (default
100/day). For a fresh ~50k-row backfill that's ~500 days at the
default cap — bump the cap via the AdminPage RuntimeTunablesCard
(takes effect in 60s) for a one-time burn.
"""
import argparse
import asyncio
import logging
import sys
from datetime import UTC, date, datetime, timedelta

import data.tw.finmind_connector as finmind
from db.session import AsyncSessionLocal
from services.ingest.repository import NewsArticleRow, insert_news_articles

log = logging.getLogger(__name__)

# Chunk size — keep monthly so a single FinMind call returns at most a
# few thousand rows. Going wider risks payload truncation; going narrower
# wastes calls without changing total volume.
_CHUNK_DAYS = 30


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _parse_published_at(raw: str | None) -> datetime | None:
    """FinMind's `date` field is `YYYY-MM-DD HH:MM:SS` (no tz). Treat
    as UTC since FinMind doesn't expose source-of-truth timezone for
    historical Taiwanese news; differences from real publish-time fall
    inside the 48h sentiment aggregation window anyway."""
    if not raw:
        return None
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        elif " " in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw + "T00:00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _to_row(item: dict, market: str) -> NewsArticleRow | None:
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None
    published_at = _parse_published_at(item.get("published_at"))
    if published_at is None:
        return None
    return NewsArticleRow(
        market=market,
        symbol=item.get("symbol") or None,
        published_at=published_at,
        title=title,
        link=link,
        publisher=(item.get("source_name") or None),
        summary=(item.get("description") or None),
        payload=None,
        source="finmind_backfill",
    )


async def _process_chunk(
    chunk_start: date, chunk_end: date, *, market: str,
) -> tuple[int, int]:
    """Pull one chunk from FinMind, parse, insert. Returns
    `(fetched, inserted_attempted)` — the second figure is rows
    handed to insert_news_articles; actual inserts are smaller when
    sha256 dedup matches existing rows."""
    items = await finmind.get_news(
        chunk_start.isoformat(),
        symbol="",
        end_date=chunk_end.isoformat(),
    )
    fetched = len(items)
    if not items:
        return 0, 0

    rows: list[NewsArticleRow] = []
    for item in items:
        row = _to_row(item, market)
        if row is not None:
            rows.append(row)

    if not rows:
        return fetched, 0

    async with AsyncSessionLocal() as db:
        await insert_news_articles(db, rows)
    return fetched, len(rows)


async def backfill(
    start: date, end: date, *, market: str = "TW",
) -> tuple[int, int]:
    """Walk `start..end` in `_CHUNK_DAYS` chunks. Returns
    `(total_fetched, total_attempted)`. Per-chunk failures are logged
    and skipped; the script continues with the next chunk so a single
    transient FinMind hiccup doesn't abort an overnight backfill."""
    total_fetched = 0
    total_attempted = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS - 1), end)
        log.info(
            "backfill.chunk_start",
            extra={
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
            },
        )
        try:
            fetched, attempted = await _process_chunk(
                cursor, chunk_end, market=market,
            )
        except Exception as exc:
            log.error(
                "backfill.chunk_failed",
                extra={
                    "start": cursor.isoformat(),
                    "end": chunk_end.isoformat(),
                    "error": str(exc),
                },
            )
            cursor = chunk_end + timedelta(days=1)
            continue

        total_fetched += fetched
        total_attempted += attempted
        log.info(
            "backfill.chunk_done",
            extra={
                "fetched": fetched,
                "attempted": attempted,
                "running_total_attempted": total_attempted,
            },
        )
        cursor = chunk_end + timedelta(days=1)

    return total_fetched, total_attempted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", type=_parse_date, required=True,
                        help="ISO date (YYYY-MM-DD) — backfill start, inclusive")
    parser.add_argument("--end", type=_parse_date, required=True,
                        help="ISO date (YYYY-MM-DD) — backfill end, inclusive")
    parser.add_argument("--market", default="TW",
                        help="market code stamped on rows (default: TW)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.start > args.end:
        print("error: --start must be on or before --end", file=sys.stderr)
        return 2

    fetched, attempted = asyncio.run(
        backfill(args.start, args.end, market=args.market),
    )
    print(
        f"\n[ok] FinMind returned {fetched} rows; "
        f"{attempted} passed parsing and were handed to insert_news_articles "
        f"(actual inserts smaller — sha256 dedup drops overlap with existing rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
