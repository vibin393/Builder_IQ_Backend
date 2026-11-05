"""Shared FastAPI dependencies: DB session, current user, role guards.

The tenant is resolved from the verified JWT only. A client never supplies its own
tenant_id - that is the whole basis of tenant isolation.
"""

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)

# HTTP methods that cannot change anything.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DbSession = Annotated[AsyncSession, Depends(get_db)]

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None:
        raise _credentials_error

    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise _credentials_error from exc

    if payload.get("type") != "access":
        raise _credentials_error

    subject = payload.get("sub")
    if not subject:
        raise _credentials_error

    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise _credentials_error from exc

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise _credentials_error

    # Read-only roles are enforced here rather than endpoint by endpoint. A guard
    # that has to be remembered on 180 routes is a guard that will be forgotten on
    # one of them, and that one will be the write that matters.
    if user.is_read_only and request.method not in SAFE_METHODS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"The {user.role.value} role has read-only access. "
                "Ask an owner or admin to make this change."
            ),
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Dependency factory guarding an endpoint by role."""

    def _guard(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this operation",
            )
        return user

    return _guard


# Convenience guards. Use these as the parameter annotation, e.g. `user: RequireSales`,
# never as a default value - FastAPI rejects Depends in both places at once.
RequireAdmin = Annotated[User, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]

RequireSales = Annotated[
    User, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.SALES))
]

# Purchasing and accounting both touch the payables side: purchasing commits the
# money, accounting settles it. Splitting them at this boundary would mean a
# clerk could not approve the invoice for a PO they are chasing.
RequirePurchasing = Annotated[
    User,
    Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.PURCHASING, UserRole.ACCOUNTING)
    ),
]

RequireAccounting = Annotated[
    User, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.ACCOUNTING))
]

RequireWarranty = Annotated[
    User,
    Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.WARRANTY, UserRole.SUPERINTENDENT)
    ),
]

# Everyone who works FOR the builder. Excludes every external role.
#
# Use this, not CurrentUser, on every builder-side endpoint. External accounts live
# in the same tenant, so a bare CurrentUser would let a subcontractor read the job
# budget and the buyer's contract price. EXECUTIVE is included because it reads
# everything; its writes are refused at authentication, not here.
RequireStaff = Annotated[
    User,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.EXECUTIVE,
            UserRole.SALES,
            UserRole.PURCHASING,
            UserRole.ACCOUNTING,
            UserRole.SUPERINTENDENT,
            UserRole.WARRANTY,
        )
    ),
]

# Staff plus the external design team. Architects answer RFIs, review submittals
# and read drawings - and nothing else. Use this only on those surfaces.
RequireDesignTeam = Annotated[
    User,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.EXECUTIVE,
            UserRole.SALES,
            UserRole.PURCHASING,
            UserRole.ACCOUNTING,
            UserRole.SUPERINTENDENT,
            UserRole.WARRANTY,
            UserRole.ARCHITECT,
        )
    ),
]


async def get_tenant_scoped(db: AsyncSession, model: type, tenant_id: uuid.UUID, obj_id: uuid.UUID):
    """Fetch one row by id, scoped to the tenant. 404 if it belongs to anyone else.

    Returning 404 rather than 403 is deliberate: a cross-tenant 403 would confirm the
    record exists.
    """
    obj = await db.scalar(select(model).where(model.id == obj_id, model.tenant_id == tenant_id))
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{model.__name__} not found"
        )
    return obj
