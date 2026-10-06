"""Registration and login. Keeps route handlers thin."""

import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.auth import RegisterRequest, TokenPair


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:56] or "tenant"


async def _unique_slug(db: AsyncSession, base: str) -> str:
    slug, suffix = base, 1
    while await db.scalar(select(Tenant.id).where(Tenant.slug == slug)):
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def issue_tokens(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(
            subject=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
        ),
        refresh_token=create_refresh_token(subject=str(user.id)),
    )


async def register_tenant(db: AsyncSession, payload: RegisterRequest) -> tuple[Tenant, User]:
    """Create a tenant and its first OWNER user in one transaction."""
    slug = await _unique_slug(db, slugify(payload.company_name))

    tenant = Tenant(name=payload.company_name, slug=slug)
    db.add(tenant)
    await db.flush()

    user = User(
        tenant_id=tenant.id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole.OWNER,
        # Registering signs the owner straight in.
        last_login_at=datetime.now(UTC),
    )
    db.add(user)
    await db.commit()
    await db.refresh(tenant)
    await db.refresh(user)
    return tenant, user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(func.lower(User.email) == email.lower()))

    # Always run a hash comparison so a missing user and a wrong password cost the same.
    hashed = user.hashed_password if user else "$2b$12$" + "x" * 53
    valid = verify_password(password, hashed)

    if user is None or not valid or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    user.last_login_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.scalar(select(User).where(User.id == user_id))
