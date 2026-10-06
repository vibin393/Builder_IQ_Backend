"""Request and response shapes for sales: leads, realtors, activities.

Two shapes for reading a lead rather than one. `LeadRead` is what a list needs;
`LeadDetail` adds the derived fields a single record's page shows. Sending the
derived fields on a list of two hundred rows means two hundred date calculations
nobody looks at.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.crm import (
    ActivityOutcome,
    ActivityType,
    ContactMethod,
    FinancingType,
    LeadRating,
    LeadSource,
    LeadStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================================================================ leads =====


class LeadCreate(BaseModel):
    """Capturing an enquiry.

    Only the name is required, on purpose. A walk-in gives their name and
    sometimes nothing else, and a form that refuses that loses the lead
    altogether - which is worse than an incomplete record.
    """

    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)

    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    mobile: str | None = Field(default=None, max_length=40)
    preferred_contact: ContactMethod | None = None
    best_time_to_call: str | None = Field(default=None, max_length=100)

    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    postcode: str | None = Field(default=None, max_length=20)

    source: LeadSource = LeadSource.WEBSITE
    campaign: str | None = Field(default=None, max_length=200)
    realtor_id: uuid.UUID | None = None
    referred_by: str | None = Field(default=None, max_length=200)

    community_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    price_min: Decimal | None = Field(default=None, ge=0)
    price_max: Decimal | None = Field(default=None, ge=0)
    bedrooms_needed: int | None = Field(default=None, ge=0, le=20)
    desired_move_in: date | None = None

    is_prequalified: bool = False
    lender_name: str | None = Field(default=None, max_length=200)
    financing_type: FinancingType | None = None
    has_home_to_sell: bool = False

    rating: LeadRating | None = None
    assigned_to_id: uuid.UUID | None = None
    next_follow_up: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("price_max")
    @classmethod
    def _budget_must_make_sense(cls, v, info):
        """A maximum below the minimum is a typo, and one that would silently
        exclude this lead from every plan-matching query."""
        low = info.data.get("price_min")
        if v is not None and low is not None and v < low:
            raise ValueError("Maximum budget cannot be below the minimum.")
        return v


class LeadUpdate(BaseModel):
    """Editing a lead. Everything optional; only what is sent is changed.

    Status is absent deliberately - it moves through /status, which checks the
    transition is legal and writes the history entry.
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    mobile: str | None = Field(default=None, max_length=40)
    preferred_contact: ContactMethod | None = None
    best_time_to_call: str | None = Field(default=None, max_length=100)

    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    postcode: str | None = Field(default=None, max_length=20)

    source: LeadSource | None = None
    campaign: str | None = Field(default=None, max_length=200)
    realtor_id: uuid.UUID | None = None
    referred_by: str | None = Field(default=None, max_length=200)

    community_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    price_min: Decimal | None = Field(default=None, ge=0)
    price_max: Decimal | None = Field(default=None, ge=0)
    bedrooms_needed: int | None = Field(default=None, ge=0, le=20)
    desired_move_in: date | None = None

    is_prequalified: bool | None = None
    lender_name: str | None = Field(default=None, max_length=200)
    financing_type: FinancingType | None = None
    has_home_to_sell: bool | None = None

    rating: LeadRating | None = None
    assigned_to_id: uuid.UUID | None = None
    next_follow_up: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class LeadRead(ORMModel):
    """A lead as a list row."""

    id: uuid.UUID
    first_name: str
    last_name: str
    full_name: str
    email: str | None
    phone: str | None
    mobile: str | None
    preferred_contact: ContactMethod | None
    city: str | None

    status: LeadStatus
    source: LeadSource
    rating: LeadRating | None
    campaign: str | None
    realtor_id: uuid.UUID | None

    community_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    price_min: Decimal | None
    price_max: Decimal | None
    bedrooms_needed: int | None
    desired_move_in: date | None

    is_prequalified: bool
    has_home_to_sell: bool
    financing_type: FinancingType | None

    assigned_to_id: uuid.UUID | None
    last_contacted_on: date | None
    next_follow_up: date | None
    converted_buyer_id: uuid.UUID | None
    is_archived: bool
    notes: str | None

    # Derived, and cheap: both are a comparison against today, and a list is read
    # for exactly these two things - who is overdue and who is still live.
    is_open: bool
    follow_up_overdue: bool


class LeadDetail(LeadRead):
    """A lead as its own page: everything, including the slower derived fields."""

    best_time_to_call: str | None
    address: str | None
    postcode: str | None
    referred_by: str | None
    lender_name: str | None
    converted_on: date | None
    lost_reason: str | None
    lost_to: str | None
    lost_on: date | None
    archive_reason: str | None
    created_by_id: uuid.UUID | None
    updated_by_id: uuid.UUID | None

    days_since_contact: int | None
    is_contingent: bool


# --- actions on a lead --------------------------------------------------------


class StatusChange(BaseModel):
    status: LeadStatus
    reason: str | None = Field(default=None, max_length=500)
    lost_to: str | None = Field(default=None, max_length=200)


class AssignRequest(BaseModel):
    """Who owns this lead. None unassigns, which is a real action - a salesperson
    leaving needs their pipeline handed back to the pool."""

    assigned_to_id: uuid.UUID | None = None


class BulkAssignRequest(BaseModel):
    lead_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    assigned_to_id: uuid.UUID | None = None


class BulkAssignResult(BaseModel):
    """What actually happened. A bulk action that reports only success is lying
    the moment one row was skipped."""

    assigned: int
    skipped: list[str]
    not_found: int


class FollowUpRequest(BaseModel):
    next_follow_up: date
    note: str | None = Field(default=None, max_length=500)


class LostRequest(BaseModel):
    """Why the lead was lost. Required, because a pipeline of leads marked lost
    with no reason tells the business nothing."""

    reason: str = Field(min_length=3, max_length=500)
    lost_to: str | None = Field(default=None, max_length=200)


class RatingRequest(BaseModel):
    rating: LeadRating


class ReopenRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class DuplicateCheck(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    mobile: str | None = Field(default=None, max_length=40)


class DuplicateMatch(ORMModel):
    """Enough to recognise the existing lead, and no more - this is a warning on
    a form, not a record to work from."""

    id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    status: LeadStatus
    source: LeadSource
    created_at: date | None = None
    assigned_to_id: uuid.UUID | None

    @field_validator("created_at", mode="before")
    @classmethod
    def _date_only(cls, v):
        return v.date() if hasattr(v, "date") else v


# --- the summary strip --------------------------------------------------------


class SourceBreakdown(BaseModel):
    source: str
    leads: int
    converted: int


class LeadSummary(BaseModel):
    """The four numbers above the list, plus the funnel behind them.

    `unassigned` and `overdue` are here because they are the two that represent
    money leaking: a lead nobody owns and a lead nobody has rung.
    """

    open: int
    unassigned: int
    overdue: int
    due_today: int
    by_status: dict[str, int]
    by_source: list[SourceBreakdown]


# =========================================================== activities =====


class LeadActivityCreate(BaseModel):
    activity_type: ActivityType = ActivityType.NOTE
    summary: str = Field(min_length=1, max_length=1000)
    occurred_on: date | None = None
    outcome: ActivityOutcome = ActivityOutcome.NONE
    #: Logging a call and booking the next one is one action to a salesperson, so
    #: it is one request here.
    next_follow_up: date | None = None


class LeadActivityRead(ORMModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    user_id: uuid.UUID | None
    activity_type: ActivityType
    outcome: ActivityOutcome
    summary: str
    occurred_on: date | None
    #: True for entries the system wrote, so the UI can show "status moved to
    #: qualified" differently from "Priya rang them".
    is_system: bool


# ============================================================= realtors =====


class RealtorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    brokerage: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    licence_number: str | None = Field(default=None, max_length=64)
    commission_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)


class RealtorUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    brokerage: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    licence_number: str | None = Field(default=None, max_length=64)
    commission_percent: Decimal | None = Field(default=None, ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)


class RealtorRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    brokerage: str | None
    email: str | None
    phone: str | None
    licence_number: str | None
    commission_percent: Decimal
    notes: str | None
    is_archived: bool
    archive_reason: str | None


class RealtorPerformance(BaseModel):
    """An agent with what they have actually delivered.

    `conversion_rate` is None rather than 0 for an agent who has sent nothing: a
    new agent has no rate yet, which is a different fact from a rate of zero.
    """

    id: uuid.UUID
    code: str
    name: str
    brokerage: str | None
    email: str | None
    phone: str | None
    commission_percent: Decimal
    leads_sent: int
    leads_converted: int
    conversion_rate: float | None
