"""
Creates the initial admin user on first boot if ADMIN_EMAIL + ADMIN_PASSWORD are set.
Called from main.py lifespan after DB is ready.
"""
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.user import User, UserRole

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def seed_admin(db: AsyncSession) -> None:
    if not settings.ADMIN_EMAIL or not settings.ADMIN_PASSWORD:
        return

    existing = await db.scalar(select(User).where(User.email == settings.ADMIN_EMAIL))
    if existing:
        return

    admin = User(
        email=settings.ADMIN_EMAIL,
        hashed_password=pwd_ctx.hash(settings.ADMIN_PASSWORD),
        role=UserRole.admin,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
