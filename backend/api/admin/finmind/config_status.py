"""Config, status, usage, and diagnostics endpoints for the AdminPage
FinMind proxy.

Covers the resolved env-var config readout, the catalog/coverage status
report, the usage rollup, the connection self-test, and the first-run
setup checklist.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from finmind.models.dataset_source import DatasetSource

from ._shared import AdminUser, FmDb, _ensure_finmind_db_reachable, log, router


class FinmindConfigResponse(BaseModel):
    """Resolved FinMind subsystem config — surfaces the same values
    the lifespan startup log emits, so the operator can verify
    env-var propagation directly in the UI without shell access to
    `docker compose logs backend | grep "finmind config:"`.

    Doesn't touch the DB — readable even when the FinMind clone is
    unreachable, which is exactly when the operator most needs to
    confirm whether their FINMIND_USE_MAIN_DB=true setting reached
    the process."""

    use_main_db: bool
    auto_init: bool
    effective_database_url: str  # password-masked
    schema_: str | None  # `finmind` when sharing main DB on Postgres
    mode: str  # 'separate-container' | 'shared-main-db' | 'sqlite-test'


@router.get(
    "/config",
    response_model=FinmindConfigResponse,
    summary="AdminPage: resolved FinMind env-var settings (no DB query)",
)
async def finmind_config(_: AdminUser) -> FinmindConfigResponse:
    """Mirrors the startup log line. Operator opens AdminPage → sees
    exactly which mode is active without needing to read backend logs."""
    from finmind.config import finmind_settings

    url = finmind_settings.effective_database_url_safe
    schema_val = finmind_settings.schema
    if url.startswith("sqlite"):
        mode = "sqlite-test"
    elif schema_val:
        mode = "shared-main-db"
    else:
        mode = "separate-container"
    return FinmindConfigResponse(
        use_main_db=finmind_settings.FINMIND_USE_MAIN_DB,
        auto_init=finmind_settings.FINMIND_AUTO_INIT,
        effective_database_url=url,
        schema_=schema_val,
        mode=mode,
    )


@router.get(
    "/status",
    summary="AdminPage: catalog + Phase 1 coverage + backfill summary",
)
async def finmind_status(_: AdminUser, db: FmDb) -> dict[str, Any]:
    """Calls into the existing `finmind.scripts.status.collect_status`
    so the AdminPage card and the CLI report show the same numbers.
    Same dataclass, JSON-serialized.

    Probes the DB first so a `ConnectionRefusedError` on a fresh
    deployment surfaces as a clean 503 (with the underlying exception
    class in the body) instead of bubbling up as a generic 500. The
    AdminPage's Setup checklist card already renders the actionable
    fix hint, so the status banner can stay quiet on 503."""
    from dataclasses import asdict

    from finmind.scripts.status import collect_status

    await _ensure_finmind_db_reachable(db)

    report = await collect_status(db)
    payload = asdict(report)
    # Coerce datetime → str for JSON; collect_status' `generated_at`
    # is already a string, but the recent_errors list contains
    # serialized timestamps that are already strings too.
    return payload


@router.get(
    "/usage",
    summary="AdminPage: per-day / per-dataset / per-key usage rollup",
)
async def finmind_usage(
    _: AdminUser,
    db: FmDb,
    days: int = 7,
) -> dict[str, Any]:
    """Powers the UsageCard chart. Window capped at 1-90 days here
    rather than at the route-arg level so the AdminPage can request
    1d/7d/30d ranges via the same endpoint."""
    if not (1 <= days <= 90):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days must be between 1 and 90",
        )

    await _ensure_finmind_db_reachable(db)

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func as sa_func
    from sqlalchemy import text as sa_text

    from finmind.models.billing import ApiUsageEvent

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    by_day_rows = (
        await db.execute(
            select(
                sa_func.strftime("%Y-%m-%d", ApiUsageEvent.ts).label("day")
                if db.bind.dialect.name == "sqlite"
                else sa_func.to_char(ApiUsageEvent.ts, "YYYY-MM-DD").label("day"),
                sa_func.count().label("calls"),
                sa_func.coalesce(
                    sa_func.sum(ApiUsageEvent.row_count), 0
                ).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(sa_text("day"))
            .order_by(sa_text("day"))
        )
    ).all()

    by_dataset_rows = (
        await db.execute(
            select(
                ApiUsageEvent.dataset_code,
                sa_func.count().label("calls"),
                sa_func.coalesce(
                    sa_func.sum(ApiUsageEvent.row_count), 0
                ).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.dataset_code)
            .order_by(sa_func.count().desc())
        )
    ).all()

    return {
        "window_days": days,
        "by_day": [
            {"day": r[0], "calls": int(r[1]), "rows": int(r[2])}
            for r in by_day_rows
        ],
        "by_dataset": [
            {
                "dataset_code": r[0] or "<unknown>",
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_dataset_rows
        ],
    }


# ── FinMind connection self-test ─────────────────────────────────


class TestConnectionResponse(BaseModel):
    ok: bool
    message: str
    token_present: bool
    dataset_tested: str
    rows_returned: int


@router.post(
    "/test-connection",
    response_model=TestConnectionResponse,
    summary="AdminPage: probe FinMind with the current token",
)
async def test_finmind_connection(_: AdminUser) -> TestConnectionResponse:
    """One-shot self-test against FinMind. Hits a small free-tier
    dataset (TaiwanStockInfo, no symbol → tiny payload) so the test
    works regardless of subscription tier.

    Returns a structured result rather than raising on failure so
    the operator sees both the success and the failure modes in the
    same banner shape — easier UX than translating exception types
    into UI strings.

    What this catches:
      - Empty / missing FINMIND_TOKEN (when subscription required)
      - Wrong/expired token (HTTP 401 / 403)
      - Network unreachable (httpx exception)
      - FinMind hourly-quota exhaustion (returns [] silently)
      - Sponsor-tier silent-deny on a paid dataset

    What this DOESN'T catch:
      - Per-dataset paywalls (we test TaiwanStockInfo specifically;
        a customer might still hit a 4xx on TaiwanStockNews etc.)
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    from data.tw.finmind_connector import _get_token, _query

    token = await _get_token()

    end = _date.today()
    start = end - _td(days=30)
    try:
        rows = await _query(
            "TaiwanStockInfo", "",
            start.isoformat(), end.isoformat(),
        )
    except Exception as exc:
        log.exception("finmind connection test failed")
        return TestConnectionResponse(
            ok=False,
            message=(
                f"connector raised: {exc.__class__.__name__}: {exc!s}"
            ),
            token_present=bool(token),
            dataset_tested="TaiwanStockInfo",
            rows_returned=0,
        )

    if not rows:
        # FinMind quota-exhausted or silent-deny path. Token present
        # vs absent changes the diagnosis.
        if not token:
            msg = (
                "no rows returned and FINMIND_TOKEN is empty — "
                "set the token via .env (FINMIND_TOKEN=...) or "
                "Admin → Market Keys → finmind, then retry."
            )
        else:
            msg = (
                "no rows returned despite token being present. "
                "Likely causes: hourly quota exhausted (1500/hr for "
                "sponsor; check the connector's Redis counter) OR "
                "the token isn't valid for this dataset's tier."
            )
        return TestConnectionResponse(
            ok=False,
            message=msg,
            token_present=bool(token),
            dataset_tested="TaiwanStockInfo",
            rows_returned=0,
        )

    return TestConnectionResponse(
        ok=True,
        message=(
            f"connection OK — TaiwanStockInfo returned {len(rows)} "
            f"rows in the test window. Your token works + quota "
            f"isn't exhausted."
        ),
        token_present=bool(token),
        dataset_tested="TaiwanStockInfo",
        rows_returned=len(rows),
    )


# ── Setup checklist (first-run wizard) ──────────────────────────


class SetupCheck(BaseModel):
    """One checkpoint in the operator onboarding flow."""

    key: str            # machine-readable id (db_reachable / catalog_seeded / ...)
    label: str          # human-readable English label for the UI
    passed: bool
    detail: str         # short explanation; empty when passed
    fix_hint: str       # one-line operator action to fix; empty when passed


class SetupStatusResponse(BaseModel):
    """4-step onboarding flow surfaced as a checklist on the AdminPage.

    Order matters — earlier failures gate later ones. The frontend
    walks the list top-down and shows the FIRST unmet step as the
    "next action". Once all four pass, the operator graduates to
    the steady-state status banner."""

    checks: list[SetupCheck]
    next_action: str | None  # the first failing step's fix_hint, or None when all pass


@router.get(
    "/setup-status",
    response_model=SetupStatusResponse,
    summary=(
        "AdminPage: 4-step setup checklist for the first-time operator"
    ),
)
async def setup_status(_: AdminUser, db: FmDb) -> SetupStatusResponse:
    """Aggregates the four onboarding checkpoints:

      1. db_reachable      — finmind_clone DB responds to a SELECT
                             (catches "wrong FINMIND_DATABASE_URL"
                             + "postgres_finmind not started")
      2. catalog_seeded    — dataset_sources has the expected 80 rows
                             (catches "init_db not run yet")
      3. finmind_token_works — connector's _query returns rows for a
                             trivial probe (catches "FINMIND_TOKEN
                             empty / wrong / quota exhausted")
      4. universe_populated — tw_stock_info has rows (catches
                             "TaiwanStockInfo never enabled / never
                             ran" — required for per-symbol cron
                             auto-discovery)

    Each check returns a fix_hint string; the frontend renders the
    first failing one as the headline "next action" prompt."""
    from finmind.config import finmind_settings
    from finmind.dataset_catalog import all_entries
    from finmind.models.master import TwStockInfo

    checks: list[SetupCheck] = []

    # ── 1. DB reachable ────────────────────────────────────────
    try:
        from sqlalchemy import text as _text
        await db.execute(_text("SELECT 1"))
        db_reachable = True
        # On success, surface the effective URL so the operator can
        # tell at a glance which mode is active (Path A1 vs A2).
        db_detail = (
            f"connected to {finmind_settings.effective_database_url_safe}"
            + (
                f" (schema={finmind_settings.schema})"
                if finmind_settings.schema else ""
            )
        )
    except Exception as exc:
        db_reachable = False
        # On failure, include the URL so the operator can verify
        # whether FINMIND_USE_MAIN_DB took effect — port 5433 means
        # still Path A1 (separate container), main host means Path A2.
        db_detail = (
            f"{exc.__class__.__name__}: {exc!s} "
            f"(target={finmind_settings.effective_database_url_safe})"
        )
    checks.append(SetupCheck(
        key="db_reachable",
        label="FinMind clone DB reachable",
        passed=db_reachable,
        detail=db_detail,
        fix_hint=(
            "" if db_reachable else
            "Two options. (A) Run a separate Postgres: "
            "`docker compose --profile finmind up -d postgres_finmind` "
            "(default — needs docker socket access on the deploy host). "
            "(B) Share the main app's Postgres: set "
            "`FINMIND_USE_MAIN_DB=true` in .env so the subsystem binds "
            "to DATABASE_URL with a `finmind` schema for isolation. "
            "Then restart the backend — lifespan auto-init handles the "
            "rest."
        ),
    ))

    # ── 2. Catalog seeded ──────────────────────────────────────
    expected = len(list(all_entries()))
    if db_reachable:
        try:
            seeded_count = (
                await db.execute(
                    select(DatasetSource).order_by(DatasetSource.dataset_code)
                )
            ).scalars().all()
            catalog_seeded = len(seeded_count) == expected
            catalog_detail = (
                "" if catalog_seeded
                else f"only {len(seeded_count)}/{expected} rows in dataset_sources"
            )
        except Exception as exc:
            catalog_seeded = False
            catalog_detail = f"{exc.__class__.__name__}: {exc!s}"
    else:
        # DB unreachable → can't even check; skip but mark failed.
        catalog_seeded = False
        catalog_detail = "skipped (DB not reachable)"
    checks.append(SetupCheck(
        key="catalog_seeded",
        label=f"Catalog seeded ({expected} datasets)",
        passed=catalog_seeded,
        detail=catalog_detail,
        fix_hint=(
            "" if catalog_seeded else
            "Restart the backend — the lifespan auto-init will run "
            "alembic + seed the catalog automatically. (Or run "
            "manually: `cd backend && python -m finmind.scripts.init_db`.)"
        ),
    ))

    # ── 3. FinMind token works ─────────────────────────────────
    if catalog_seeded:
        try:
            from data.tw.finmind_connector import _get_token, _query
            from datetime import date as _date, timedelta as _td

            tk = await _get_token()
            end = _date.today()
            start = end - _td(days=30)
            try:
                rows = await _query(
                    "TaiwanStockInfo", "",
                    start.isoformat(), end.isoformat(),
                )
                token_works = bool(rows)
                if not rows:
                    if not tk:
                        token_detail = "FINMIND_TOKEN empty"
                    else:
                        token_detail = (
                            "no rows returned despite token present "
                            "(quota exhausted or tier issue)"
                        )
                else:
                    token_detail = ""
            except Exception as exc:
                token_works = False
                token_detail = f"{exc.__class__.__name__}: {exc!s}"
        except Exception as exc:
            # Connector import failed → likely deps missing
            token_works = False
            token_detail = (
                f"connector import failed: "
                f"{exc.__class__.__name__}: {exc!s}"
            )
    else:
        token_works = False
        token_detail = "skipped (catalog not seeded)"
    checks.append(SetupCheck(
        key="finmind_token_works",
        label="FinMind token responds to a probe query",
        passed=token_works,
        detail=token_detail,
        fix_hint=(
            "" if token_works else
            "Set FINMIND_TOKEN via .env, OR via Admin → Market Keys → "
            "finmind. Then click 'Test connection' to verify."
        ),
    ))

    # ── 4. Universe populated ──────────────────────────────────
    if catalog_seeded:
        try:
            count = (
                await db.execute(
                    select(TwStockInfo.symbol)
                )
            ).scalars().all()
            universe_populated = len(count) > 0
            universe_detail = (
                "" if universe_populated
                else "tw_stock_info is empty"
            )
        except Exception as exc:
            universe_populated = False
            universe_detail = f"{exc.__class__.__name__}: {exc!s}"
    else:
        universe_populated = False
        universe_detail = "skipped (catalog not seeded)"
    checks.append(SetupCheck(
        key="universe_populated",
        label="Symbol universe populated (tw_stock_info)",
        passed=universe_populated,
        detail=universe_detail,
        fix_hint=(
            "" if universe_populated else
            "Enable `TaiwanStockInfo` in the catalog table below + "
            "click its row's Run button. Once it runs successfully, "
            "the daily cron's per-symbol fan-out will auto-discover "
            "the universe from tw_stock_info."
        ),
    ))

    next_action = next(
        (c.fix_hint for c in checks if not c.passed),
        None,
    )
    return SetupStatusResponse(
        checks=checks,
        next_action=next_action,
    )
