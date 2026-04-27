"""
Unit tests for the /api/system/web-vital endpoint.

The endpoint is intentionally simple: validate the report shape, drop
out-of-range values, and observe one of two Prometheus histograms.
Tests call the endpoint function directly with WebVitalReport
instances so they don't depend on the full FastAPI client fixture
(which the local dev environment can't bootstrap due to a cffi
shared-library issue noted in CLAUDE.md).
"""
import pytest

from api.system.router import (
    WebVitalReport,
    _MAX_VITAL_SCORE,
    _MAX_VITAL_SECONDS,
    _normalize_route,
    web_vital,
)
from middleware.metrics import WEB_VITAL_SCORE, WEB_VITAL_SECONDS


# ── _normalize_route ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("/dashboard",            "/dashboard"),
    ("/market/US",            "/market"),
    ("/market/TW",            "/market"),
    ("/stock/US/AAPL",        "/stock"),
    ("/stock/TW/2330",        "/stock"),
    ("/screener",             "/screener"),
    ("/portfolio",            "/portfolio"),
    ("/portfolio/abc-123",    "/portfolio"),  # nested portfolio detail
    ("/analytics",            "/analytics"),
    ("/ai",                   "/ai"),
    ("/login",                "/login"),
    # Unknown route shapes get bucketed under /other so we don't
    # explode Prometheus cardinality on a typo or a deep link.
    ("/random-page",          "/other"),
    ("/",                     "/other"),
    ("",                      "/other"),
])
def test_normalize_route_collapses_dynamic_segments_to_route_shape(raw, expected):
    assert _normalize_route(raw) == expected


# ── Beacon helper ────────────────────────────────────────────────

def _hist_count(histogram, labels: tuple[str, str]) -> float:
    """Read the cumulative `_count` sample for a (name, path) bucket."""
    for sample in histogram.collect()[0].samples:
        if sample.name.endswith("_count") and tuple(sample.labels.values()) == labels:
            return sample.value
    return 0.0


# ── Time metrics (LCP / INP / FCP / TTFB) ────────────────────────

@pytest.mark.asyncio
async def test_web_vital_observes_lcp_to_seconds_histogram():
    before = _hist_count(WEB_VITAL_SECONDS, ("LCP", "/stock"))
    await web_vital(WebVitalReport(name="LCP", value=1.42, path="/stock/US/AAPL"))
    after = _hist_count(WEB_VITAL_SECONDS, ("LCP", "/stock"))
    assert after == before + 1


@pytest.mark.asyncio
async def test_web_vital_observes_inp_to_seconds_histogram():
    before = _hist_count(WEB_VITAL_SECONDS, ("INP", "/portfolio"))
    await web_vital(WebVitalReport(name="INP", value=0.12, path="/portfolio"))
    after = _hist_count(WEB_VITAL_SECONDS, ("INP", "/portfolio"))
    assert after == before + 1


@pytest.mark.asyncio
async def test_web_vital_drops_time_value_above_60s_sanity_bound():
    """Bogus / clock-skewed values must not pollute the histogram."""
    before = _hist_count(WEB_VITAL_SECONDS, ("LCP", "/dashboard"))
    await web_vital(WebVitalReport(name="LCP", value=_MAX_VITAL_SECONDS + 0.1, path="/dashboard"))
    assert _hist_count(WEB_VITAL_SECONDS, ("LCP", "/dashboard")) == before


# ── Score metrics (CLS) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_vital_observes_cls_to_score_histogram():
    before = _hist_count(WEB_VITAL_SCORE, ("CLS", "/dashboard"))
    await web_vital(WebVitalReport(name="CLS", value=0.07, path="/dashboard"))
    after = _hist_count(WEB_VITAL_SCORE, ("CLS", "/dashboard"))
    assert after == before + 1


@pytest.mark.asyncio
async def test_web_vital_drops_cls_above_5_sanity_bound():
    before = _hist_count(WEB_VITAL_SCORE, ("CLS", "/market"))
    await web_vital(WebVitalReport(name="CLS", value=_MAX_VITAL_SCORE + 0.1, path="/market/US"))
    assert _hist_count(WEB_VITAL_SCORE, ("CLS", "/market")) == before


# ── Validation ───────────────────────────────────────────────────

def test_web_vital_report_rejects_unknown_metric_name():
    with pytest.raises(ValueError):
        WebVitalReport(name="LARGEST_PAINT", value=1.0, path="/dashboard")  # type: ignore[arg-type]


def test_web_vital_report_rejects_negative_value():
    with pytest.raises(ValueError):
        WebVitalReport(name="LCP", value=-0.1, path="/dashboard")


def test_web_vital_report_rejects_overlong_path():
    """Path is capped at 128 chars to bound label length even when
    _normalize_route falls through to /other."""
    with pytest.raises(ValueError):
        WebVitalReport(name="LCP", value=1.0, path="/x" * 200)


# ── Response shape ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_vital_always_returns_204_for_valid_report():
    """Beacon contract is fire-and-forget — clients can't act on a
    rejection so we always 204 the response regardless of whether
    the value was actually observed."""
    response = await web_vital(WebVitalReport(name="LCP", value=2.0, path="/dashboard"))
    assert response.status_code == 204
