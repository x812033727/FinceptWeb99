"""Detection helpers for FinMind tier / paywall responses.

FinMind moves datasets between tiers from time to time (see
PR #183 for the 2026-04 `TaiwanStockMonthRevenue` migration). The
upstream wire signal is consistent: HTTP 4xx with a JSON body that
identifies the failure as a tier mismatch rather than a server-side
error. We centralize the check so:

  - any FinMind-backed cron can `if is_paywall_response(exc): …` to
    treat the failure as a known-permanent skip rather than a
    transient outage with auto-backoff
  - the canonical phrase list lives in one place — if FinMind ever
    rewords their response, we update one regex / tuple, not N
    cron files
  - the same detector covers per-symbol queries (which might get
    paywalled later) and market-wide queries

Pulled out of `tasks/ingest_revenue_tw.py`'s private helpers in
PR #188 so the other FinMind callers (`ingest_ohlcv_tw`,
`tw_etf_yields_refresh`, future) can adopt the same pattern in
one line.
"""
from __future__ import annotations

import httpx

# Phrases FinMind returns in the response body when the requesting
# token is on a tier that doesn't have access to the dataset. As of
# 2026-04 the market-wide `TaiwanStockMonthRevenue` query (`data_id=""`)
# is sponsor-only — the body reads:
#
#   "Your level is register. Please update your user level. Detail
#    information: https://finmindtrade.com/analysis/#/Sponsor/sponsor"
#
# Match against substrings so a wording tweak on FinMind's side
# doesn't silently un-detect — `requires paid` and `sponsor` are
# the two wide nets.
_PAYWALL_HINTS: tuple[str, ...] = (
    "your level is register",
    "please update your user level",
    "requires paid",
    "sponsor",
)


def extract_body_message(exc: BaseException) -> str:
    """Pull the FinMind body's ``msg`` / ``message`` / ``detail``
    out of an :class:`httpx.HTTPStatusError`, or fall back to a
    ``body_msg`` attribute on custom FinMind exceptions
    (e.g. :class:`FinMindSilentDeny` raised when every day in a
    multi-day TaiwanStockNews window comes back with HTTP 200 +
    body status != 200 — i.e. silent paywall).

    Returns ``""`` when:
      - ``exc`` isn't an HTTPStatusError or carrier of ``body_msg``
      - the response body isn't JSON
      - none of the three known keys carry a string

    The empty-string return is intentional: callers chain into
    :func:`looks_like_paywall` which treats `""` as "not a paywall",
    so a FinMind error with a missing body falls through to the
    transient-failure path (auto-backoff arms) — the safest default.
    """
    direct = getattr(exc, "body_msg", None)
    if isinstance(direct, str) and direct:
        return direct
    if not isinstance(exc, httpx.HTTPStatusError):
        return ""
    try:
        body = exc.response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("msg", "message", "detail"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def looks_like_paywall(body_msg: str) -> bool:
    """``True`` iff the FinMind response body matches one of the
    canonical paywall phrases. Case-insensitive."""
    if not body_msg:
        return False
    needle = body_msg.lower()
    return any(hint in needle for hint in _PAYWALL_HINTS)


def is_paywall_response(exc: BaseException) -> bool:
    """Convenience: ``True`` iff the exception is a FinMind tier
    mismatch.

    Composes :func:`extract_body_message` + :func:`looks_like_paywall`
    so the typical cron callsite is just:

        try:
            rows = await finmind.get_some_dataset(...)
        except Exception as exc:
            if is_paywall_response(exc):
                # treat as known-permanent skip
                ...
            else:
                # transient — record_failure + auto-backoff
                ...
    """
    return looks_like_paywall(extract_body_message(exc))


def raise_if_silent_denied(rows: list, *, days: int = 1) -> list:
    """Promote a silent-deny (HTTP 200 + body.status != 200) into
    :class:`FinMindSilentDeny` so the task layer can surface it as
    `ok=False` instead of `ok=True row_count=0`.

    FinMind's silent-deny returns 200 OK with an empty data array;
    the connector exposes the body's ``msg`` via the
    ``consume_last_silent_deny`` contextvar. Tasks call this helper
    immediately after a FinMind read; it's a no-op when the call
    was a real success or a legitimate empty.

    Returns ``rows`` unchanged so it composes inline:

        rows = raise_if_silent_denied(await finmind.get_x(...))
    """
    # Imported here (instead of at module top) so the import graph
    # stays one-way: callers depend on `finmind_paywall`, which
    # depends on `finmind_connector` — not the other way around.
    from data.tw.finmind_connector import (
        FinMindSilentDeny,
        consume_last_silent_deny,
    )

    silent = consume_last_silent_deny()
    if silent and not rows:
        raise FinMindSilentDeny(silent, days=days)
    return rows
