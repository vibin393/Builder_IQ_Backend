import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.SALES
    job_title: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    role: UserRole | None = None
    is_active: bool | None = None
    job_title: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)


class PasswordReset(BaseModel):
    """An administrator setting a temporary password for someone locked out."""

    new_password: str = Field(min_length=8, max_length=128)


class UserSummary(BaseModel):
    total: int
    active: int
    inactive: int
    external: int
    owners: int
    by_role: dict[str, int]
    never_signed_in: int
    dormant: int  # active, and not signed in for 90 days


class ProfileUpdate(BaseModel):
    """What a user may change about themselves. Notably not their role."""

    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    job_title: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    avatar_url: str | None = None
    job_title: str | None = None
    phone: str | None = None
    last_login_at: datetime | None = None
    is_external: bool = False
