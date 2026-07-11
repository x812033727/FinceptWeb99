"""Request/response schemas for the Web Push subscription endpoints
(PR-D3). Mirrors the browser `PushSubscription.toJSON()` shape."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
