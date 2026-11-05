"""Schemas for the RFI module: the request, its thread, its log and its analytics."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.change_order import ResponsibleParty
from app.models.communication import BallInCourt, Priority, RFICategory, RFIStatus
from app.models.rfi import RFIActivityKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Create / update ----------------------------------------------------------


class RFICreate(BaseModel):
    job_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=4000)
    priority: Priority = Priority.NORMAL
    category: RFICategory = RFICategory.OTHER
    assigned_vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    drawing_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=200)
    spec_reference: str | None = Field(default=None, max_length=120)
    due_on: date | None = None
    is_blocking: bool = False
    ball_in_court: BallInCourt = BallInCourt.BUILDER


class RFIUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    question: str | None = Field(default=None, min_length=1, max_length=4000)
    status: RFIStatus | None = None
    ball_in_court: BallInCourt | None = None
    priority: Priority | None = None
    category: RFICategory | None = None
    location: str | None = Field(default=None, max_length=200)
    spec_reference: str | None = Field(default=None, max_length=120)
    assigned_vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    due_on: date | None = None
    is_blocking: bool | None = None


class RFIReassign(BaseModel):
    ball_in_court: BallInCourt
    note: str | None = Field(default=None, max_length=500)


# --- Thread -------------------------------------------------------------------


class RFIResponseCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    court: BallInCourt = BallInCourt.ARCHITECT
    is_official: bool = False
    cost_impact: bool = False
    schedule_impact_days: int = Field(default=0, ge=0, le=365)


class RFIResponseRead(ORMModel):
    id: uuid.UUID
    rfi_id: uuid.UUID
    author_id: uuid.UUID | None
    court: BallInCourt
    body: str
    is_official: bool
    cost_impact: bool
    schedule_impact_days: int
    created_at: datetime


class RFIAnswer(BaseModel):
    """The one-shot official answer. Keeps the original, simple answer path working."""

    answer: str = Field(min_length=1, max_length=4000)
    cost_impact: bool = False
    schedule_impact_days: int = Field(default=0, ge=0, le=365)


# --- Activity log -------------------------------------------------------------


class RFIActivityRead(ORMModel):
    id: uuid.UUID
    rfi_id: uuid.UUID
    author_id: uuid.UUID | None
    kind: RFIActivityKind
    detail: str | None
    created_at: datetime


# --- Change order -------------------------------------------------------------


class RFICostLine(BaseModel):
    cost_code_id: uuid.UUID
    amount: Decimal
    description: str | None = None


class RFIChangeOrderRequest(BaseModel):
    """Turn an answered, cost-bearing RFI into a change order."""

    responsible_party: ResponsibleParty = ResponsibleParty.BUILDER
    price_delta: Decimal = Decimal("0")
    cost_lines: list[RFICostLine] = []
    reason: str | None = None


# --- Read ---------------------------------------------------------------------


class RFIRead(ORMModel):
    id: uuid.UUID
    rfi_number: str
    job_id: uuid.UUID
    subject: str
    question: str
    status: RFIStatus
    ball_in_court: BallInCourt
    priority: Priority
    category: RFICategory
    location: str | None
    spec_reference: str | None
    assigned_vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    drawing_id: uuid.UUID | None
    raised_on: date | None
    due_on: date | None
    answered_on: date | None
    closed_on: date | None
    is_blocking: bool
    cost_impact: bool
    schedule_impact_days: int
    answer: str | None
    is_overdue: bool
    days_open: int | None
    days_to_answer: int | None
    response_count: int
    responses: list[RFIResponseRead] = []
    activity: list[RFIActivityRead] = []


# --- Analytics ----------------------------------------------------------------


class RFISummary(BaseModel):
    total: int
    open: int
    answered: int
    closed: int
    overdue: int
    blocking: int
    in_our_court: int  # open, ball with the builder — we owe the answer
    unassigned: int  # open, no trade or court owner beyond the builder
    due_this_week: int
    avg_response_days: float | None
    by_status: dict[str, int]
    by_court: dict[str, int]
    by_category: dict[str, int]
    by_priority: dict[str, int]
    aging: dict[str, int]  # open RFIs bucketed by days open
