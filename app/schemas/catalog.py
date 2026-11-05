"""Schemas for the configuration catalog: cost codes, vendors, communities, plans, options."""

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.cost_code import CostType
from app.models.vendor import CISStatus, VendorType

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Cost codes ---------------------------------------------------------------


class CostCodeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    division: str | None = Field(default=None, max_length=100)
    gl_account: str | None = Field(default=None, max_length=20)
    cost_type: CostType | None = None


class CostCodeUpdate(BaseModel):
    """Every field optional: a PATCH says what changed, not what everything is.

    The code itself is editable because a mistyped cost code is the single most
    expensive typo in this system - everything reconciles through it.
    """

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    division: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    gl_account: str | None = Field(default=None, max_length=20)
    cost_type: CostType | None = None


class CostCodeRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    division: str | None
    is_active: bool
    gl_account: str | None = None
    cost_type: CostType | None = None
    is_archived: bool
    archive_reason: str | None
    created_by_id: uuid.UUID | None
    updated_by_id: uuid.UUID | None


# --- Vendors ------------------------------------------------------------------


class VendorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    vendor_type: VendorType = VendorType.TRADE
    contact_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    #: What this trade charges an hour. Used as the default on a timecard, so the
    #: rate is not retyped on every entry - retyping is how an average rate drifts
    #: without anybody deciding it should.
    labour_rate: Decimal | None = Field(default=None, ge=0)
    cis_status: CISStatus | None = None
    utr: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    company_number: str | None = Field(default=None, max_length=8)
    vat_number: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=200)
    payment_terms_days: int | None = Field(default=None, ge=0, le=120)
    notes: str | None = Field(default=None, max_length=2000)


class VendorUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    vendor_type: VendorType | None = None
    contact_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    is_active: bool | None = None
    #: What this trade charges an hour. Used as the default on a timecard, so the
    #: rate is not retyped on every entry - retyping is how an average rate drifts
    #: without anybody deciding it should.
    labour_rate: Decimal | None = Field(default=None, ge=0)
    cis_status: CISStatus | None = None
    utr: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    company_number: str | None = Field(default=None, max_length=8)
    vat_number: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=200)
    payment_terms_days: int | None = Field(default=None, ge=0, le=120)
    notes: str | None = Field(default=None, max_length=2000)


class VendorRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    vendor_type: VendorType
    contact_name: str | None
    email: str | None
    phone: str | None
    is_active: bool
    is_archived: bool
    archive_reason: str | None
    created_by_id: uuid.UUID | None
    updated_by_id: uuid.UUID | None
    labour_rate: Decimal | None
    cis_status: CISStatus | None = None
    utr: str | None = None
    address: str | None = None
    postcode: str | None = None
    company_number: str | None = None
    vat_number: str | None = None
    website: str | None = None
    payment_terms_days: int | None = None
    notes: str | None = None


# --- Communities and lots -----------------------------------------------------

# Community and plot schemas moved to app/schemas/communities.py.


# --- Plans --------------------------------------------------------------------

# Plan schemas moved to app/schemas/plans.py.


# --- Options ------------------------------------------------------------------

# Option schemas moved to app/schemas/options.py.
