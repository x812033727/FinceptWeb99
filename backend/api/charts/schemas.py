import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from schemas.alert import parse_trend_time


class DrawingPoint(BaseModel):
    time: str | None = Field(default=None, max_length=40)
    price: float = Field(..., gt=0)

    model_config = {"extra": "forbid"}


def validate_points(kind: str, points: list[DrawingPoint]) -> None:
    if kind == "horizontal":
        if len(points) != 1:
            raise ValueError("horizontal drawing requires exactly one price point")
    elif kind == "trend":
        if len(points) != 2 or any(point.time is None for point in points):
            raise ValueError("trend drawing requires exactly two time/price points")
        if points[0].time == points[1].time:
            raise ValueError("trend drawing points must use different times")
        try:
            for point in points:
                parse_trend_time(point.time or "")
        except ValueError as exc:
            raise ValueError("trend drawing points require valid timestamps") from exc
    else:
        raise ValueError("kind must be horizontal or trend")


class DrawingCreate(BaseModel):
    market: Literal["US", "TW", "CRYPTO"]
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    kind: Literal["horizontal", "trend"]
    points: list[DrawingPoint]
    label: str | None = Field(default=None, max_length=80)
    color: str = Field(default="#f59e0b", pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def check_points(self) -> "DrawingCreate":
        validate_points(self.kind, self.points)
        self.symbol = self.symbol.upper()
        return self


class DrawingUpdate(BaseModel):
    points: list[DrawingPoint] | None = None
    label: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("points")
    @classmethod
    def points_cannot_be_null(cls, value: list[DrawingPoint] | None) -> list[DrawingPoint]:
        if value is None:
            raise ValueError("points cannot be null")
        return value


class DrawingOut(BaseModel):
    id: uuid.UUID
    market: str
    symbol: str
    kind: str
    points: list[DrawingPoint]
    label: str | None
    color: str
    alert_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DrawingAlertCreate(BaseModel):
    condition: Literal["above", "below"]
    repeat: bool = False
    cooldown_seconds: int = Field(default=0, ge=0, le=7 * 86400)
