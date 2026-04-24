import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from models.alert import AlertCondition


class AlertCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str = Field(..., pattern="^(US|TW)$")
    condition: AlertCondition
    target_price: float = Field(..., gt=0)


class AlertOut(BaseModel):
    id: uuid.UUID
    symbol: str
    market: str
    condition: AlertCondition
    target_price: float
    triggered: bool
    triggered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
