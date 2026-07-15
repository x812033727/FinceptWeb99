from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from dependencies import get_current_user
from middleware.metrics import WEB_VITAL_SCORE, WEB_VITAL_SECONDS
from services.version_service import get_version_status

router = APIRouter()
User = Annotated[dict, Depends(get_current_user)]


class VersionStatus(BaseModel):
    current: str
    latest: str
    update_available: bool
    html_url: str
    published_at: str


@router.get("/version", response_model=VersionStatus)
async def version(_: User) -> VersionStatus:
    data = await get_version_status()
    return VersionStatus(**data)


# ── Core Web Vitals beacon ────────────────────────────────────────
# Anonymous on purpose — the browser's web-vitals library fires LCP /
# INP / CLS during page load (often before authenticated requests
# can run), and gating this on auth would silently drop the bulk of
# the data. The endpoint only writes to a Prometheus histogram, so
# the worst an unauthenticated client can do is inflate one bucket.

# Names whose value is a duration in seconds vs a unitless score.
_VITAL_TIME_NAMES = {"LCP", "FCP", "TTFB", "INP", "FID"}
_VITAL_SCORE_NAMES = {"CLS"}

# Sanity bounds — drop obviously bogus values so a buggy / hostile
# client can't pollute the metric.
_MAX_VITAL_SECONDS = 60.0
_MAX_VITAL_SCORE = 5.0

# Per-route cardinality cap. The frontend includes the URL path in
# every beacon; we normalize it through a small allowlist of route
# shapes so Prometheus doesn't see thousands of distinct labels.
_ROUTE_PREFIXES = (
    "/dashboard", "/market/", "/stock/", "/screener", "/compare", "/portfolio",
    "/analytics", "/macro", "/watchlist", "/alerts", "/ai",
    "/settings", "/admin", "/login",
)


def _normalize_route(raw: str) -> str:
    """Collapse stock/market/etc. to their route shape so we keep one
    label per page rather than one per stock symbol. Anything that
    doesn't match a known prefix is bucketed under '/other'."""
    for prefix in _ROUTE_PREFIXES:
        if raw == prefix.rstrip("/") or raw.startswith(prefix):
            # /stock/US/AAPL → /stock, /market/US → /market
            return prefix.rstrip("/") or "/"
    return "/other"


class WebVitalReport(BaseModel):
    name: Literal["LCP", "INP", "CLS", "FCP", "TTFB", "FID"]
    value: float = Field(..., ge=0)
    path: str = Field(..., min_length=1, max_length=128)


@router.post("/web-vital", status_code=status.HTTP_204_NO_CONTENT)
async def web_vital(report: WebVitalReport) -> Response:
    if report.name in _VITAL_TIME_NAMES:
        if report.value <= _MAX_VITAL_SECONDS:
            WEB_VITAL_SECONDS.labels(report.name, _normalize_route(report.path)).observe(
                report.value
            )
    elif report.name in _VITAL_SCORE_NAMES:
        if report.value <= _MAX_VITAL_SCORE:
            WEB_VITAL_SCORE.labels(report.name, _normalize_route(report.path)).observe(
                report.value
            )
    # Always 204 — beacon contract is fire-and-forget; any rejection
    # would just be noise the client can't act on.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
