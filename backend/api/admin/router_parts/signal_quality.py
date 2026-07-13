from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db

from ..schemas import (
    CategoricalSignalBucket,
    CategoricalSignalRow,
    NumericSignalRow,
    SignalQualityOut,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Empirical signal quality (PR #250 service surface) ───────────


_SIGNAL_QUALITY_LOOKBACK_MAX = 365


@router.get("/signal-quality", response_model=SignalQualityOut)
async def signal_quality(
    _: AdminUser, db: DB,
    lookback: int = 60,
    market: str | None = None,
) -> SignalQualityOut:
    """Return the empirical signal-vs-D5-return correlation report
    (PR #250 service surface) so the AdminPage can render which
    signals actually predict moves vs which are noise.

    Numeric signals (RSI / volume_ratio / industry_rs / …) get
    Pearson r + sign-match accuracy. Categorical signals
    (taifex.trend / day_trading_trend / …) get mean D5 per bucket.

    `lookback` capped at ~1 year — beyond that the dataset starts
    mixing across very different market regimes and the aggregate
    becomes harder to interpret. Operator who needs longer windows
    should run the CLI directly (`python -m scripts.signal_quality_check`).

    Read-only path; same admin gate as the audit endpoint.
    """
    from services.signal_quality_service import compute_signal_quality

    if lookback < 1 or lookback > _SIGNAL_QUALITY_LOOKBACK_MAX:
        raise HTTPException(
            400,
            f"lookback must be in [1, {_SIGNAL_QUALITY_LOOKBACK_MAX}]; "
            f"got {lookback}",
        )

    report = await compute_signal_quality(
        db, lookback_days=lookback, market=market,
    )

    numeric_rows = [
        NumericSignalRow(
            label=row.label,
            path=row.path,
            n=row.n,
            mean_value=row.mean_value,
            mean_d5_return=row.mean_d5_return,
            pearson_r=row.pearson_r,
            sign_match_pct=row.sign_match_pct,
        )
        for row in report.numeric
    ]
    categorical_rows = [
        CategoricalSignalRow(
            label=row.label,
            path=row.path,
            n_total=row.n_total,
            buckets=[
                CategoricalSignalBucket(
                    category=cat,
                    n=bucket.n,
                    mean_d5_return=bucket.mean_d5_return,
                    median_d5_return=bucket.median_d5_return,
                )
                for cat, bucket in row.by_category.items()
            ],
        )
        for row in report.categorical
    ]

    return SignalQualityOut(
        discussions_audited=report.discussions_audited,
        discussion_ids=report.discussion_ids,
        lookback_days=report.lookback_days,
        market=report.market,
        numeric=numeric_rows,
        categorical=categorical_rows,
    )
