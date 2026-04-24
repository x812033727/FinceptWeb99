import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from models.alert import AlertCondition


class AlertCreate(BaseModel):
    symbol: str = Field(..., max_length=20)
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
