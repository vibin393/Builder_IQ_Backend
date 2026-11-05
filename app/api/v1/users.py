"""Tenant user administration, and each user's own profile.

Every admin query is filtered by the caller's tenant_id from the JWT. The /me
routes are scoped to the signed-in user and are open to every role, including
trade and homeowner accounts - anyone may edit their own name and picture.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, DbSession, RequireAdmin, RequireStaff
from app.core.security import hash_password, verify_password
from app.models.user import User, UserRole
from app.schemas.common import Message, Page
from app.schemas.user import (
    PasswordChange,
    PasswordReset,
    ProfileUpdate,
    UserCreate,
    UserRead,
    UserSummary,
    UserUpdate,
)
from app.services import avatar as avatar_service

router = APIRouter(prefix="/users", tags=["users"])

#: Active accounts unused for this long are flagged: likely leavers.
DORMANT_DAYS = 90


@router.get("", response_model=Page[UserRead])
async def list_users(
    db: DbSession,
    current: RequireStaff,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=200),
    search: str | None = Query(default=None, max_length=100),
    role: UserRole | None = None,
    active: bool | None = None,
) -> Page[UserRead]:
    scope = select(User).where(User.tenant_id == current.tenant_id)
    if search:
        like = f"%{search.strip().lower()}%"
        scope = scope.where(
            or_(func.lower(User.full_name).like(like), func.lower(User.email).like(like))
        )
    if role is not None:
        scope = scope.where(User.role == role)
    if active is not None:
        scope = scope.where(User.is_active.is_(active))

    total = await db.scalar(select(func.count()).select_from(scope.subquery()))
    rows = await db.scalars(
        scope.order_by(User.is_active.desc(), User.full_name).offset((page - 1) * size).limit(size)
    )

    return Page[UserRead](
        items=[UserRead.model_validate(u) for u in rows],
        total=total or 0,
        page=page,
        size=size,
    )


@router.get("/summary", response_model=UserSummary)
async def users_summary(db: DbSession, current: RequireStaff) -> UserSummary:
    users = list(await db.scalars(select(User).where(User.tenant_id == current.tenant_id)))
    live = [u for u in users if u.is_active]
    cutoff = datetime.now(UTC) - timedelta(days=DORMANT_DAYS)
    by_role: dict[str, int] = {}
    for u in live:
        by_role[u.role.value] = by_role.get(u.role.value, 0) + 1

    def dormant(u) -> bool:
        seen = u.last_login_at
        if seen is None:
            return False
        if seen.tzinfo is None:  # SQLite drops the zone
            seen = seen.replace(tzinfo=UTC)
        return seen < cutoff

    return UserSummary(
        total=len(users),
        active=len(live),
        inactive=len(users) - len(live),
        external=sum(1 for u in live if u.is_external),
        owners=sum(1 for u in live if u.role is UserRole.OWNER),
        by_role=by_role,
        never_signed_in=sum(1 for u in live if u.last_login_at is None),
        dormant=sum(1 for u in live if dormant(u)),
    )


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: DbSession, admin: RequireAdmin) -> UserRead:
    exists = await db.scalar(
        select(User.id).where(
            User.tenant_id == admin.tenant_id,
            func.lower(User.email) == payload.email.lower(),
        )
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this organisation",
        )
    if payload.role is UserRole.OWNER and admin.role is not UserRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can make someone an owner.",
        )

    user = User(
        tenant_id=admin.tenant_id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        job_title=payload.job_title,
        phone=payload.phone,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


# --- the signed-in user's own profile ----------------------------------------
#
# Declared before /{user_id} so "me" is never parsed as a UUID. These use
# CurrentUser rather than RequireStaff: a trade or homeowner account should be
# able to change their own name and picture too.


@router.get("/me", response_model=UserRead)
async def read_own_profile(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", response_model=UserRead)
async def update_own_profile(payload: ProfileUpdate, db: DbSession, user: CurrentUser) -> UserRead:
    """Update your own details.

    Deliberately cannot change role, tenant or active status - those are an
    administrator's decision, not the account holder's.
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.post("/me/avatar", response_model=UserRead)
async def upload_own_avatar(
    db: DbSession, user: CurrentUser, file: UploadFile = File(...)
) -> UserRead:
    """Replace your profile picture.

    The file is decoded, centre-cropped, downsized and re-encoded before it is
    written, so what lands on disk is a known-good 256px JPEG with no EXIF.
    """
    previous = user.avatar_url
    user.avatar_url = await avatar_service.save_avatar(file, user.id)
    await db.commit()
    await db.refresh(user)

    # Only after the new one is committed, so a failed write leaves the old
    # picture in place rather than none at all.
    avatar_service.delete_avatar(previous)
    return UserRead.model_validate(user)


@router.delete("/me/avatar", response_model=UserRead)
async def remove_own_avatar(db: DbSession, user: CurrentUser) -> UserRead:
    previous = user.avatar_url
    user.avatar_url = None
    await db.commit()
    await db.refresh(user)
    avatar_service.delete_avatar(previous)
    return UserRead.model_validate(user)


@router.post("/me/password", response_model=Message)
async def change_own_password(payload: PasswordChange, db: DbSession, user: CurrentUser) -> Message:
    """Change your own password. The current one is required, always."""
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not your current password.",
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new password must be different.",
        )

    user.hashed_password = hash_password(payload.new_password)
    await db.commit()
    return Message(detail="Password changed.")


async def _get_tenant_user(db: DbSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    user = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/{user_id}", response_model=UserRead)
async def get_user(user_id: uuid.UUID, db: DbSession, current: RequireStaff) -> UserRead:
    user = await _get_tenant_user(db, current.tenant_id, user_id)
    return UserRead.model_validate(user)


async def _active_owners(db, tenant_id) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant_id, User.role == UserRole.OWNER, User.is_active.is_(True)
            )
        )
        or 0
    )


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID, payload: UserUpdate, db: DbSession, admin: RequireAdmin
) -> UserRead:
    """Change someone's name, role or access.

    Three rules, each closing a way to lose control of the account:
      * only an owner grants or takes away the owner role - an admin could otherwise
        promote themselves;
      * nobody deactivates or demotes themselves - the usual way the last
        administrator locks everyone out;
      * the last active owner stays an active owner.
    """
    user = await _get_tenant_user(db, admin.tenant_id, user_id)
    data = payload.model_dump(exclude_unset=True)
    new_role = data.get("role")
    role_changes = new_role is not None and new_role != user.role
    deactivating = data.get("is_active") is False and user.is_active

    if role_changes and UserRole.OWNER in (new_role, user.role):
        if admin.role is not UserRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an owner can grant or remove the owner role.",
            )
    if user.id == admin.id and (deactivating or role_changes):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You cannot deactivate yourself or change your own role. Ask another administrator."
            ),
        )
    losing_owner = user.role is UserRole.OWNER and user.is_active and (deactivating or role_changes)
    if losing_owner and await _active_owners(db, admin.tenant_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the last active owner. Make someone else an owner first.",
        )

    for field, value in data.items():
        if field in ("full_name", "role", "is_active") and value is None:
            continue
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=Message)
async def reset_password(
    user_id: uuid.UUID, payload: PasswordReset, db: DbSession, admin: RequireAdmin
) -> Message:
    """Set a temporary password for someone locked out. Tell them to change it."""
    user = await _get_tenant_user(db, admin.tenant_id, user_id)
    if user.role is UserRole.OWNER and admin.role is not UserRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can reset an owner's password.",
        )
    user.hashed_password = hash_password(payload.new_password)
    await db.commit()
    return Message(detail=f"Password reset for {user.full_name}.")
