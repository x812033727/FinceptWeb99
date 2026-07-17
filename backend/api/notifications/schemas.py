"""Schemas for Web Push subscriptions and Email/LINE preferences."""
import uuid
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PushKeys(BaseModel):
    """Client encryption keys from `PushSubscription.toJSON().keys`."""
    p256dh: str = Field(..., min_length=1, max_length=512)
    auth: str = Field(..., min_length=1, max_length=512)

    model_config = ConfigDict(extra="forbid")


class PushSubscribeIn(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)
    keys: PushKeys
    user_agent: str | None = Field(None, max_length=255)


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)


class PushSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint: str
    user_agent: str | None
    created_at: datetime
    last_success_at: datetime | None
    failed_count: int


class VapidPublicKeyOut(BaseModel):
    """`public_key` is None when the deployment has no VAPID keys —
    the frontend surfaces a "server not configured" state instead of
    calling pushManager.subscribe with garbage."""
    configured: bool
    public_key: str | None


EventKind = Literal["price_alert", "strategy_health", "daily_picks_ready"]


class ChannelUpdateIn(BaseModel):
    enabled: bool
    event_kinds: list[EventKind] = Field(
        default_factory=lambda: [
            "price_alert", "strategy_health", "daily_picks_ready",
        ]
    )
    daily_digest: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("event_kinds")
    @classmethod
    def unique_nonempty(cls, value: list[EventKind]) -> list[EventKind]:
        if not value:
            raise ValueError("Select at least one notification event kind")
        return list(dict.fromkeys(value))


class NotificationChannelOut(BaseModel):
    kind: Literal["email", "line"]
    enabled: bool
    verified: bool
    configured: bool
    destination_hint: str | None
    event_kinds: list[EventKind]
    daily_digest: bool
    failed_count: int
    last_success_at: datetime | None


class LineBindingOut(BaseModel):
    token: str
    expires_at: datetime
    instruction: str


class ChannelTestOut(BaseModel):
    delivered: bool
