import uuid
from datetime import datetime
from pydantic import BaseModel


class AdminUserItem(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleUpdate(BaseModel):
    role: str  # "viewer" | "analyst" | "admin"


class ActiveUpdate(BaseModel):
    is_active: bool


class SystemStats(BaseModel):
    total_users: int
    active_users: int
    users_by_role: dict[str, int]
    total_alerts: int
    total_watchlists: int
