"""Schemas for the submittal module: the item, its revisions, its log and analytics."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.communication import BallInCourt, Priority, SubmittalStatus, SubmittalType
from app.models.submittal import SubmittalActivityKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Create / update ----------------------------------------------------------


class SubmittalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    job_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    submittal_type: SubmittalType = SubmittalType.PRODUCT_DATA
    priority: Priority = Priority.NORMAL
    description: str | None = None
    spec_section: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    option_id: uuid.UUID | None = None
    due_on: date | None = None
    required_on_site: date | None = None
    lead_time_days: int = Field(default=0, ge=0, le=365)
    storage_key: str | None = None
    # Raise it straight into the review queue, or keep it as a draft.
    submit_now: bool = True


class SubmittalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    submittal_type: SubmittalType | None = None
    priority: Priority | None = None
    description: str | None = None
    spec_section: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    option_id: uuid.UUID | None = None
    due_on: date | None = None
    required_on_site: date | None = None
    lead_time_days: int | None = Field(default=None, ge=0, le=365)


class SubmittalReview(BaseModel):
    status: SubmittalStatus
    review_notes: str | None = None


class SubmittalResubmit(BaseModel):
    """The trade sends a corrected revision back after a revise-and-resubmit."""

    storage_key: str | None = None
    note: str | None = Field(default=None, max_length=500)


class SubmittalReassign(BaseModel):
    ball_in_court: BallInCourt
    note: str | None = Field(default=None, max_length=500)


# --- Revisions / activity -----------------------------------------------------


class SubmittalRevisionRead(ORMModel):
    id: uuid.UUID
    submittal_id: uuid.UUID
    revision: int
    outcome: SubmittalStatus
    submitted_on: date | None
    returned_on: date | None
    reviewer_id: uuid.UUID | None
    review_notes: str | None
    created_at: datetime


class SubmittalActivityRead(ORMModel):
    id: uuid.UUID
    submittal_id: uuid.UUID
    author_id: uuid.UUID | None
    kind: SubmittalActivityKind
    detail: str | None
    created_at: datetime


# --- Read ---------------------------------------------------------------------


class SubmittalRead(ORMModel):
    id: uuid.UUID
    submittal_number: str
    job_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    title: str
    description: str | None
    spec_section: str | None
    manufacturer: str | None
    model_number: str | None
    submittal_type: SubmittalType
    priority: Priority
    status: SubmittalStatus
    ball_in_court: BallInCourt
    revision: int
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    option_id: uuid.UUID | None
    submitted_on: date | None
    due_on: date | None
    returned_on: date | None
    approved_on: date | None
    reviewer_id: uuid.UUID | None
    review_notes: str | None
    required_on_site: date | None
    lead_time_days: int
    is_overdue: bool
    order_by_date: date | None
    submit_by_date: date | None
    is_at_risk: bool
    review_turnaround_days: int | None
    revisions: list[SubmittalRevisionRead] = []
    activity: list[SubmittalActivityRead] = []


# --- Analytics ----------------------------------------------------------------


class SubmittalSummary(BaseModel):
    total: int
    draft: int
    awaiting_review: int       # submitted + under_review — the builder owes a decision
    being_revised: int
    approved: int
    rejected: int
    overdue: int               # past the review due date
    at_risk: int               # procurement runway running out
    ball_with_trade: int       # open, waiting on the trade to (re)submit
    avg_review_days: float | None
    by_status: dict[str, int]
    by_type: dict[str, int]
    by_court: dict[str, int]
    by_priority: dict[str, int]
