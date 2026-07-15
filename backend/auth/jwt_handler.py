import uuid
from datetime import datetime, timedelta, timezone
import jwt
from jwt import InvalidTokenError as JWTError
from config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: str, role: str) -> str:
    expire = _now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": user_id, "role": role, "exp": expire, "type": "access"},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """Returns (encoded_token, jti) — jti is stored in Redis for revocation."""
    jti = str(uuid.uuid4())
    expire = _now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token = jwt.encode(
        {"sub": user_id, "jti": jti, "exp": expire, "type": "refresh"},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, jti


def decode_access_token(token: str) -> dict:
    """Raises JWTError on invalid/expired token."""
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise JWTError("Not an access token")
    return payload


def decode_refresh_token(token: str) -> dict:
    """Raises JWTError on invalid/expired token."""
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "refresh":
        raise JWTError("Not a refresh token")
    return payload
