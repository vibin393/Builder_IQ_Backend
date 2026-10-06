import uuid

import jwt
from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.core.security import decode_token
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenPair
from app.schemas.user import UserRead
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DbSession) -> TokenPair:
    """Sign up a new builder (tenant) and its first owner account."""
    _, user = await auth_service.register_tenant(db, payload)
    return auth_service.issue_tokens(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    user = await auth_service.authenticate(db, payload.email, payload.password)
    return auth_service.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
    )
    try:
        claims = decode_token(payload.refresh_token)
    except jwt.PyJWTError as exc:
        raise invalid from exc

    if claims.get("type") != "refresh":
        raise invalid

    user = await auth_service.get_user_by_id(db, uuid.UUID(claims["sub"]))
    if user is None or not user.is_active:
        raise invalid

    return auth_service.issue_tokens(user)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
