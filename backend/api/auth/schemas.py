from pydantic import BaseModel, EmailStr, Field, field_validator
from datetime import datetime
from uuid import UUID


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime
    ai_requests_remaining: int | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class APIKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    expires_days: int | None = Field(default=None, ge=1, le=3650)  # None = never expires


class APIKeyCreateResponse(BaseModel):
    id: UUID
    name: str
    key: str              # raw key — shown once only
    expires_at: datetime | None


class APIKeyListItem(BaseModel):
    id: UUID
    name: str
    last_used_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
