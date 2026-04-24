import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from jose import JWTError
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request

from api.auth.schemas import (
    APIKeyCreateRequest,
    APIKeyCreateResponse,
    APIKeyListItem,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from limiter import limiter
from auth.jwt_handler import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from cache.redis_cache import (
    cache_delete,
    cache_set,
    get_redis,
    key_ai_counter,
    key_refresh_token,
    key_user_sessions,
)
from config import settings
from db.session import get_db
from dependencies import get_current_user
from models.user import APIKey, User, UserRole

router = APIRouter()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

REFRESH_COOKIE = "refresh_token"
REFRESH_TTL = settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400

# Pre-computed bcrypt hash of a random string, used to equalize timing
# when the submitted email doesn't exist — prevents user-enumeration attacks.
_DUMMY_HASH = pwd_ctx.hash(secrets.token_urlsafe(16))


# ── Helpers ───────────────────────────────────────────────────────

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _set_refresh_cookie(response: Response, user_id: str) -> str:
    token, jti = create_refresh_token(user_id)
    r = await get_redis()
    # Track jti in a Redis Set for per-user session revocation
    await r.sadd(key_user_sessions(user_id), jti)
    await r.expire(key_user_sessions(user_id), REFRESH_TTL)
    await cache_set(key_refresh_token(user_id, jti), "1", REFRESH_TTL)
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=not settings.DEBUG,   # HTTPS-only outside debug/dev
        samesite="lax",
        max_age=REFRESH_TTL,
        path="/api/auth",
    )
    return token


async def _get_ai_remaining(user_id: str, role: str) -> int:
    r = await get_redis()
    used = await r.get(key_ai_counter(user_id))
    limit = (
        settings.AI_REQUESTS_ANALYST_DAILY
        if role in ("analyst", "admin")
        else settings.AI_REQUESTS_VIEWER_DAILY
    )
    return limit - int(used or 0)


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest, response: Response, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email,
        hashed_password=pwd_ctx.hash(body.password),
        role=UserRole.viewer,
    )
    db.add(user)
    await db.flush()   # get user.id before commit

    await _set_refresh_cookie(response, str(user.id))
    access_token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == body.email))
    # Always run bcrypt verify — even when user is None — to equalize timing
    # and prevent user-enumeration via response-time analysis.
    target_hash = user.hashed_password if user else _DUMMY_HASH
    password_ok = pwd_ctx.verify(body.password, target_hash)
    if not user or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    await _set_refresh_cookie(response, str(user.id))
    access_token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = decode_refresh_token(refresh_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload["sub"]
    jti = payload["jti"]

    # Verify jti is still valid in Redis (not revoked)
    r = await get_redis()
    valid = await r.get(key_refresh_token(user_id, jti))
    if not valid:
        raise HTTPException(status_code=401, detail="Session revoked")

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    # Rotate: revoke old jti, issue new refresh token
    await cache_delete(key_refresh_token(user_id, jti))
    await r.srem(key_user_sessions(user_id), jti)
    await _set_refresh_cookie(response, user_id)

    access_token = create_access_token(user_id, user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
):
    if refresh_token:
        try:
            payload = decode_refresh_token(refresh_token)
            user_id = payload["sub"]
            jti = payload["jti"]
            r = await get_redis()
            await cache_delete(key_refresh_token(user_id, jti))
            await r.srem(key_user_sessions(user_id), jti)
        except JWTError:
            pass  # expired token is fine — just clear the cookie

    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


@router.get("/me", response_model=UserResponse)
async def me(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, current_user["id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    remaining = await _get_ai_remaining(str(user.id), user.role.value)
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        ai_requests_remaining=remaining,
    )


@router.patch("/me", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, current_user["id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not pwd_ctx.verify(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.hashed_password = pwd_ctx.hash(body.new_password)
    await db.commit()


# ── API Keys ──────────────────────────────────────────────────────

@router.post("/api-keys", response_model=APIKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: APIKeyCreateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw = f"finweb_live_{secrets.token_urlsafe(24)}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_days)
        if body.expires_days
        else None
    )
    api_key = APIKey(
        user_id=current_user["id"],
        key_hash=_hash_key(raw),
        name=body.name,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.flush()
    return APIKeyCreateResponse(id=api_key.id, name=api_key.name, key=raw, expires_at=expires_at)


@router.get("/api-keys", response_model=list[APIKeyListItem])
async def list_api_keys(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(APIKey).where(APIKey.user_id == current_user["id"]).order_by(APIKey.created_at.desc())
    )
    return [
        APIKeyListItem(
            id=k.id, name=k.name,
            last_used_at=k.last_used_at, expires_at=k.expires_at, created_at=k.created_at,
        )
        for k in rows
    ]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = await db.get(APIKey, key_id)
    if not key or str(key.user_id) != current_user["id"]:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
