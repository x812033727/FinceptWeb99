"""Direct unit tests for `services._quote_helpers.sanitize_change_pct`.

The function was extracted in PR #167 so TW + US market services can
share the ±30% daily-move sanity bound that defends against
upstream-junk deltas (TWSE's KY-listed `Change`-slot quirk yielding
+992% headlines, Polygon split-adjusted-vs-unadjusted divergence,
stale snapshot rows, etc.).

Coverage on the US side already exists transitively via
`test_us_market_service_sanity_bound.py`, but that pinned the
in-context behaviour through `_normalize_quote`. These tests pin the
helper itself — boundary behaviour at exactly ±30, None / 0 input,
log emission, and the `market` parameter passing through to the
log record.
"""
from __future__ import annotations

import logging

from services._quote_helpers import (
    DAILY_MOVE_MAX_PCT,
    sanitize_change_pct,
)


def test_constant_is_30_percent():
    """The constant is the contract — every market service depends on it
    as a public sanity threshold. Pin it so a silent re-tune to 50%
    requires editing this test too (i.e. is a deliberate decision)."""
    assert DAILY_MOVE_MAX_PCT == 30.0


def test_passes_through_zero():
    assert sanitize_change_pct("AAPL", "US", 0.0) == 0.0


def test_passes_through_typical_move():
    assert sanitize_change_pct("AAPL", "US", 1.5) == 1.5
    assert sanitize_change_pct("2330", "TW", -2.3) == -2.3


def test_returns_none_for_none_input():
    """`change_pct` may be missing on a snapshot fallback — caller decides
    whether to display em-dash or zero. Don't fabricate a number."""
    assert sanitize_change_pct("AAPL", "US", None) is None


def test_passes_through_at_exactly_the_boundary():
    """+30.0 / -30.0 are the legal limit-up / limit-down for a TW limit
    cycle. Drop only when *strictly* greater than the threshold so
    perfectly-on-limit days aren't NULLed out as 'too volatile'."""
    assert sanitize_change_pct("2330", "TW", 30.0) == 30.0
    assert sanitize_change_pct("2330", "TW", -30.0) == -30.0


def test_drops_just_above_the_boundary():
    """30.01% is upstream junk — no real market sustains a >30% single-day
    close move on a registered ticker without a corporate event we'd
    surface explicitly elsewhere."""
    assert sanitize_change_pct("2330", "TW", 30.01) is None
    assert sanitize_change_pct("2330", "TW", -30.01) is None


def test_drops_implausible_positive_value():
    """The KY-listed +992% bug from PR #142 is the canonical example —
    TWSE OpenAPI puts the prior-day close in the `Change` field for some
    rows, yielding a delta that's >900% in percent units."""
    assert sanitize_change_pct("9105", "TW", 992.5) is None


def test_drops_implausible_negative_value():
    assert sanitize_change_pct("XYZ", "US", -85.0) is None


def test_logs_warning_on_rejection(caplog):
    """When the rail trips, ops needs symbol + market + raw value in the
    log record so they can chase down which upstream tier surfaced the
    junk. Test the actual log emission, not just the return value."""
    with caplog.at_level(logging.WARNING, logger="services._quote_helpers"):
        result = sanitize_change_pct("9105", "TW", 992.5)
    assert result is None
    # One of the records should be our channel.
    matching = [r for r in caplog.records
                if r.message == "quote.change_pct_out_of_bounds"]
    assert matching, f"expected `quote.change_pct_out_of_bounds`, got {[r.message for r in caplog.records]}"
    rec = matching[0]
    assert rec.symbol == "9105"
    assert rec.market == "TW"
    assert rec.change_pct == 992.5


def test_does_not_log_when_value_is_in_range(caplog):
    with caplog.at_level(logging.WARNING, logger="services._quote_helpers"):
        sanitize_change_pct("AAPL", "US", 5.0)
    assert not any(
        r.message == "quote.change_pct_out_of_bounds" for r in caplog.records
    ), "in-range values should not emit the rejection log"


def test_market_parameter_is_passed_through_to_log(caplog):
    """Same value, different markets — verify the log record reflects
    the caller. Used so ops can filter `market="TW"` separately from
    `market="US"` outages in the alerting pipeline."""
    with caplog.at_level(logging.WARNING, logger="services._quote_helpers"):
        sanitize_change_pct("AAPL", "US", 100.0)
        sanitize_change_pct("9105", "TW", 100.0)
    rejected = [r for r in caplog.records
                if r.message == "quote.change_pct_out_of_bounds"]
    assert len(rejected) == 2
    assert {rejected[0].market, rejected[1].market} == {"TW", "US"}
