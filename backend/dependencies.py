import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt_handler import decode_access_token
from db.session import get_db
from models.user import APIKey, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    api_key: str | None = Depends(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Authenticate via Bearer JWT or X-API-Key header.
    Returns a dict with 'id' (str UUID) and 'role'.
    """
    # ── Try Bearer JWT ────────────────────────────────────────────
    if token:
        try:
            payload = decode_access_token(token)
            return {"id": payload["sub"], "role": payload["role"]}
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ── Try API Key ───────────────────────────────────────────────
    if api_key:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        row = await db.scalar(
            select(APIKey).where(APIKey.key_hash == key_hash)
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        if row.expires_at and row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")

        # Update last_used_at without a full commit overhead
        row.last_used_at = datetime.now(timezone.utc)

        user = await db.get(User, row.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

        return {"id": str(user.id), "role": user.role.value}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_analyst(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] not in ("analyst", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return user
