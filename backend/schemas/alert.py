"""Domain schemas shared between api/alerts and services/alert_service.

Lives outside `api/` so the service layer can import without creating a
services -> api cycle.

PR-D1: `AlertCreate` grew the rule-engine surface —
`condition_type` selects an evaluator, `params` is validated against
the per-type pydantic model in `PARAMS_MODELS` (unknown type or bad
params → 422), and `repeat` + `cooldown_seconds` set re-fire
semantics. The legacy `condition` + `target_price` pair still works:
it normalizes to condition_type price_above / price_below.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from models.alert import AlertCondition

# ── per-condition-type params models ─────────────────────────────


class PctChangeParams(BaseModel):
    """漲跌幅 % — quote change_pct vs. threshold (signed, e.g. -3.5)."""
    pct: float = Field(..., ge=-100, le=1000)

    model_config = {"extra": "forbid"}


class BreakoutParams(BaseModel):
    """突破 N 日高/低 — vs. ohlcv_daily N-day extreme (excl. today)."""
    lookback_days: int = Field(20, ge=2, le=252)

    model_config = {"extra": "forbid"}


class VolumeSurgeParams(BaseModel):
    """量能異常 — today volume >= N-day avg volume × multiple."""
    multiple: float = Field(2.0, gt=1.0, le=100)
    lookback_days: int = Field(20, ge=2, le=252)

    model_config = {"extra": "forbid"}


class StreakParams(BaseModel):
    """外資連 N 日買超 (TW) — evaluated daily after institutional ingest."""
    days: int = Field(..., ge=2, le=60)

    model_config = {"extra": "forbid"}


# condition_type → params model (None = type takes no params).
# Keys are THE canonical list of known condition types; the evaluator
# registry in services/alert_rules.py must stay in sync (tested).
PARAMS_MODELS: dict[str, type[BaseModel] | None] = {
    "price_above": None,
    "price_below": None,
    "pct_change_above": PctChangeParams,
    "pct_change_below": PctChangeParams,
    "breakout_high": BreakoutParams,
    "breakout_low": BreakoutParams,
    "volume_surge": VolumeSurgeParams,
    "foreign_net_buy_streak": StreakParams,
}

TW_ONLY_CONDITION_TYPES = ("foreign_net_buy_streak",)

_LEGACY_TO_TYPE = {
    AlertCondition.above: "price_above",
    AlertCondition.below: "price_below",
}
_TYPE_TO_LEGACY = {v: k for k, v in _LEGACY_TO_TYPE.items()}


def validate_rule_fields(
    condition_type: str,
    params: dict[str, Any] | None,
    *,
    market: str,
    target_price: float | None,
) -> dict[str, Any] | None:
    """Shared create/update validation. Returns normalized params
    (defaults filled). Raises ValueError → pydantic surfaces as 422."""
    if condition_type not in PARAMS_MODELS:
        raise ValueError(
            f"unknown condition_type '{condition_type}' — "
            f"expected one of {sorted(PARAMS_MODELS)}"
        )
    if condition_type in TW_ONLY_CONDITION_TYPES and market != "TW":
        raise ValueError(f"condition_type '{condition_type}' is TW-only")

    model = PARAMS_MODELS[condition_type]
    if model is None:
        if params:
            raise ValueError(f"condition_type '{condition_type}' takes no params")
        if target_price is None:
            raise ValueError(f"condition_type '{condition_type}' requires target_price")
        return None
    if target_price is not None:
        raise ValueError(
            f"condition_type '{condition_type}' does not use target_price"
        )
    # model_validate raises pydantic.ValidationError on bad params;
    # wrap into ValueError so the outer model reports a clean 422.
    try:
        validated = model.model_validate(params or {})
    except Exception as exc:
        raise ValueError(f"invalid params for '{condition_type}': {exc}") from exc
    return validated.model_dump()


class AlertCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str = Field(..., pattern="^(US|TW|CRYPTO)$")
    # Legacy pair — still accepted; normalized into condition_type.
    condition: AlertCondition | None = None
    target_price: float | None = Field(None, gt=0)
    # Rule engine (PR-D1)
    condition_type: str | None = None
    params: dict[str, Any] | None = None
    cooldown_seconds: int = Field(0, ge=0, le=7 * 86400)
    repeat: bool = False

    @model_validator(mode="after")
    def _normalize_rule(self) -> "AlertCreate":
        if self.condition_type is None:
            # Legacy payload: condition + target_price required.
            if self.condition is None:
                raise ValueError("either condition_type or condition is required")
            self.condition_type = _LEGACY_TO_TYPE[self.condition]
        elif (
            self.condition is not None
            and _LEGACY_TO_TYPE[self.condition] != self.condition_type
        ):
            raise ValueError("condition and condition_type disagree")

        self.params = validate_rule_fields(
            self.condition_type, self.params,
            market=self.market, target_price=self.target_price,
        )
        # Keep the legacy column populated for price rules (back compat).
        self.condition = _TYPE_TO_LEGACY.get(self.condition_type)
        return self


class AlertUpdate(BaseModel):
    """Partial update — rule knobs only, not symbol/market identity.
    `params` / `target_price` are re-validated against the alert's
    condition_type in the service (needs the DB row)."""
    target_price: float | None = Field(None, gt=0)
    params: dict[str, Any] | None = None
    cooldown_seconds: int | None = Field(None, ge=0, le=7 * 86400)
    repeat: bool | None = None


class AlertOut(BaseModel):
    id: uuid.UUID
    symbol: str
    market: str
    condition: AlertCondition | None
    target_price: float | None
    condition_type: str
    params: dict[str, Any] | None
    cooldown_seconds: int
    repeat: bool
    last_fired_at: datetime | None
    triggered: bool
    triggered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertEventOut(BaseModel):
    """One fired-alert history row (PR-D5). `kind` is 'price' for
    price-alert firings, 'strategy_health' for strategy degradation
    alerts (PR-D4)."""
    id: uuid.UUID
    alert_id: uuid.UUID | None
    symbol: str
    market: str
    kind: str
    message: str
    fired_at: datetime
    payload: dict | None

    model_config = {"from_attributes": True}
