import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Response, status
from jwt import InvalidTokenError as JWTError
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request

from api.auth.schemas import (
    APIKeyCreateRequest,
    APIKeyCreateResponse,
    APIKeyListItem,
    AcceptInviteRequest,
    ChangePasswordRequest,
    ConsentAcceptRequest,
    ConsentStatus,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionItem,
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
from models.auth_security import AuthInvitation, PasswordResetToken
from models.governance import UserConsent
from services.email_service import send_password_reset_email

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


def _as_utc(value: datetime) -> datetime:
    """SQLite drops timezone offsets; PostgreSQL preserves them."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _set_refresh_cookie(
    response: Response, user_id: str, request: Request | None = None,
) -> str:
    token, jti = create_refresh_token(user_id)
    r = await get_redis()
    # Track jti in a Redis Set for per-user session revocation
    await r.sadd(key_user_sessions(user_id), jti)
    await r.expire(key_user_sessions(user_id), REFRESH_TTL)
    metadata = json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ip_address": request.client.host if request and request.client else None,
        "user_agent": request.headers.get("user-agent") if request else None,
    })
    await cache_set(key_refresh_token(user_id, jti), metadata, REFRESH_TTL)
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


async def _revoke_all_sessions(user_id: str) -> None:
    r = await get_redis()
    session_key = key_user_sessions(user_id)
    jtis = await r.smembers(session_key)
    if jtis:
        keys = [key_refresh_token(user_id, j.decode() if isinstance(j, bytes) else str(j)) for j in jtis]
        await r.delete(*keys)
    await r.delete(session_key)


async def _get_ai_remaining(user_id: str, role: str) -> int | None:
    # Admins are exempt from the daily AI quota (see `_check_quota` in the
    # discussion / ai_agents routers). Return None ("unlimited") so the
    # frontend treats admin as uncapped and skips the multi-round pre-flight
    # quota warning instead of showing a misleading count.
    if role == "admin":
        return None
    r = await get_redis()
    used = await r.get(key_ai_counter(user_id))
    limit = (
        settings.AI_REQUESTS_ANALYST_DAILY
        if role == "analyst"
        else settings.AI_REQUESTS_VIEWER_DAILY
    )
    return limit - int(used or 0)


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest, response: Response, db: AsyncSession = Depends(get_db)):
    if not settings.PUBLIC_REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="Public registration is disabled")
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

    await _set_refresh_cookie(response, str(user.id), request)
    request.state.audit_user_id = str(user.id)
    access_token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/accept-invite", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def accept_invite(
    request: Request,
    body: AcceptInviteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    token_hash = _hash_key(body.token)
    invitation = await db.scalar(
        select(AuthInvitation).where(AuthInvitation.token_hash == token_hash).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        not invitation
        or invitation.used_at is not None
        or _as_utc(invitation.expires_at) < now
        or invitation.email.lower() != str(body.email).lower()
    ):
        raise HTTPException(400, "Invitation is invalid or expired")
    if await db.scalar(select(User.id).where(User.email == str(body.email).lower())):
        raise HTTPException(409, "Email already has an account")

    user = User(
        email=str(body.email).lower(),
        hashed_password=pwd_ctx.hash(body.password),
        role=UserRole(invitation.role),
    )
    invitation.used_at = now
    db.add(user)
    await db.flush()
    await _set_refresh_cookie(response, str(user.id), request)
    request.state.audit_user_id = str(user.id)
    return TokenResponse(access_token=create_access_token(str(user.id), user.role.value))


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.email == body.email))
    if user and user.is_active:
        raw_token = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_key(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(
                minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES
            ),
        ))
        await db.flush()
        background_tasks.add_task(send_password_reset_email, user.email, raw_token)
    # Deliberately identical response for existing and unknown addresses.
    return {"detail": "If the account exists, reset instructions will be sent"}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    reset = await db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _hash_key(body.token))
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if not reset or reset.used_at is not None or _as_utc(reset.expires_at) < now:
        raise HTTPException(400, "Reset token is invalid or expired")
    user = await db.get(User, reset.user_id)
    if not user or not user.is_active:
        raise HTTPException(400, "Reset token is invalid or expired")
    user.hashed_password = pwd_ctx.hash(body.new_password)
    reset.used_at = now
    await _revoke_all_sessions(str(user.id))


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

    await _set_refresh_cookie(response, str(user.id), request)
    request.state.audit_user_id = str(user.id)
    access_token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
async def refresh(
    request: Request,
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

    user = await db.get(User, UUID(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    # Rotate: revoke old jti, issue new refresh token
    await cache_delete(key_refresh_token(user_id, jti))
    await r.srem(key_user_sessions(user_id), jti)
    await _set_refresh_cookie(response, user_id, request)

    access_token = create_access_token(user_id, user.role.value)
    return TokenResponse(access_token=access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def logout(
    request: Request,
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
    user = await db.get(User, UUID(current_user["id"]))
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
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, UUID(current_user["id"]))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not pwd_ctx.verify(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.hashed_password = pwd_ctx.hash(body.new_password)
    await db.commit()
    await _revoke_all_sessions(str(user.id))
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


@router.get("/sessions", response_model=list[SessionItem])
async def list_sessions(
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    current_jti = None
    if refresh_token:
        try:
            current_jti = decode_refresh_token(refresh_token)["jti"]
        except JWTError:
            pass
    r = await get_redis()
    raw_jtis = await r.smembers(key_user_sessions(user_id))
    sessions = []
    for raw_jti in raw_jtis:
        jti = raw_jti.decode() if isinstance(raw_jti, bytes) else str(raw_jti)
        raw_meta = await r.get(key_refresh_token(user_id, jti))
        try:
            meta = json.loads(raw_meta.decode() if isinstance(raw_meta, bytes) else raw_meta or "{}")
        except (TypeError, ValueError):
            meta = {}
        sessions.append(SessionItem(id=jti, current=jti == current_jti, **meta))
    return sessions


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_sessions(
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    await _revoke_all_sessions(current_user["id"])
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    r = await get_redis()
    if not await r.sismember(key_user_sessions(user_id), session_id):
        raise HTTPException(404, "Session not found")
    await r.delete(key_refresh_token(user_id, session_id))
    await r.srem(key_user_sessions(user_id), session_id)


def _required_consents() -> dict[str, str]:
    return {
        "terms": settings.TERMS_VERSION,
        "privacy": settings.PRIVACY_VERSION,
        "ai_data_disclosure": settings.AI_DATA_DISCLOSURE_VERSION,
    }


@router.get("/consents", response_model=list[ConsentStatus])
async def list_consents(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    rows = list((await db.scalars(select(UserConsent).where(UserConsent.user_id == user_id))).all())
    accepted = {(row.document, row.version): row for row in rows}
    return [
        ConsentStatus(
            document=document,
            required_version=version,
            accepted=(document, version) in accepted,
            accepted_at=accepted.get((document, version)).accepted_at
            if (document, version) in accepted else None,
        )
        for document, version in _required_consents().items()
    ]


@router.post("/consents", response_model=ConsentStatus, status_code=status.HTTP_201_CREATED)
async def accept_consent(
    request: Request,
    body: ConsentAcceptRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    required = _required_consents()
    if body.document not in required or body.version != required[body.document]:
        raise HTTPException(400, "Consent document or version is not current")
    user_id = UUID(current_user["id"])
    consent = await db.scalar(select(UserConsent).where(
        UserConsent.user_id == user_id,
        UserConsent.document == body.document,
        UserConsent.version == body.version,
    ))
    if consent is None:
        consent = UserConsent(
            user_id=user_id,
            document=body.document,
            version=body.version,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.add(consent)
        await db.flush()
    return ConsentStatus(
        document=body.document,
        required_version=body.version,
        accepted=True,
        accepted_at=consent.accepted_at,
    )


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
        user_id=UUID(current_user["id"]),
        key_hash=_hash_key(raw),
        name=body.name,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    return APIKeyCreateResponse(id=api_key.id, name=api_key.name, key=raw, expires_at=expires_at)


@router.get("/api-keys", response_model=list[APIKeyListItem])
async def list_api_keys(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(APIKey).where(APIKey.user_id == UUID(current_user["id"])).order_by(APIKey.created_at.desc())
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
    await db.commit()


# ── Per-user LLM provider keys ───────────────────────────────────

from services import llm_key_service as _keys  # noqa: E402
from api.admin.schemas import LLMKeyInfo, LLMKeyUpsert, LLMKeyValidation  # noqa: E402


def _info_to_schema(info: _keys.KeyInfo) -> LLMKeyInfo:
    return LLMKeyInfo(
        provider=info.provider,
        has_key=info.has_key,
        source=info.source,
        masked=info.masked,
        last_validated_at=info.last_validated_at,
        last_validation_ok=info.last_validation_ok,
        last_validation_message=info.last_validation_message,
        updated_at=info.updated_at,
    )


@router.get("/llm-keys", response_model=list[LLMKeyInfo])
async def list_my_llm_keys(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LLMKeyInfo]:
    """List the caller's per-user LLM keys (system rows are not exposed here)."""
    rows = await _keys.list_user_keys(db, UUID(current_user["id"]))
    return [_info_to_schema(r) for r in rows]


@router.put("/llm-keys/{provider}", response_model=LLMKeyInfo)
async def upsert_my_llm_key(
    provider: str,
    body: LLMKeyUpsert,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LLMKeyInfo:
    if provider not in _keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await _keys.upsert_user_key(
            db, UUID(current_user["id"]), provider, body.api_key,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _info_to_schema(info)


@router.delete("/llm-keys/{provider}", status_code=204)
async def delete_my_llm_key(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if provider not in _keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await _keys.delete_user_key(db, UUID(current_user["id"]), provider)


@router.post("/llm-keys/{provider}/test", response_model=LLMKeyValidation)
async def test_my_llm_key(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LLMKeyValidation:
    """Verify the caller's per-user key for `provider`. Falls back to system+env."""
    if provider not in _keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    key = await _keys.resolve_key(db, provider, UUID(current_user["id"]))
    if not key:
        return LLMKeyValidation(ok=False, message="no key configured")
    result = await _keys.validate_key(provider, key)
    await _keys.record_user_validation(db, UUID(current_user["id"]), provider, result)
    return LLMKeyValidation(ok=result.ok, message=result.message)


# ── Per-user LLM usage ───────────────────────────────────────────

from services import llm_usage_service as _usage  # noqa: E402
from api.admin.schemas import (  # noqa: E402
    ToolCallStatOut,
    UsageBucketOut,
    UsageDayPoint,
    UsageSummaryOut,
)


@router.get("/llm-usage", response_model=UsageSummaryOut)
async def my_llm_usage(
    range_days: int = 30,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryOut:
    """The caller's own LLM usage aggregate for the last `range_days` days."""
    range_days = max(1, min(range_days, 365))
    s = await _usage.usage_summary(
        db, range_days=range_days, user_id=UUID(current_user["id"]),
    )
    return UsageSummaryOut(
        range_days=s.range_days,
        user_scoped=s.user_scoped,
        total_requests=s.total_requests,
        total_prompt_tokens=s.total_prompt_tokens,
        total_completion_tokens=s.total_completion_tokens,
        total_cost_usd=s.total_cost_usd,
        by_provider=[UsageBucketOut(**b.__dict__) for b in s.by_provider],
        by_day=[UsageDayPoint(**d) for d in s.by_day],
        total_tool_calls=s.total_tool_calls,
        top_tools=[
            ToolCallStatOut(name=t["name"], count=t["count"])
            for t in s.top_tools
        ],
    )
