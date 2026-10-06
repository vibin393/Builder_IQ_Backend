"""Request and response shapes for field operations."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.communication import BallInCourt
from app.models.field import (
    InspectionResult,
    InspectionType,
    ItemResult,
    PunchSeverity,
    PunchStatus,
)

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================ punch list =====


class PunchCreate(BaseModel):
    job_id: uuid.UUID
    description: str = Field(min_length=1, max_length=2000)
    location: str | None = Field(default=None, max_length=200)
    #: A scuff and a cracked soil pipe are both defects and only one of them
    #: should stop a handover.
    severity: PunchSeverity = PunchSeverity.MINOR
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    due_date: date | None = None
    from_walkthrough: bool = False


class PunchUpdate(BaseModel):
    """Correcting a defect's details.

    Status and ball-in-court are absent: they move through their own routes, which
    check the transition and record who accepted the fix. Extras are refused rather
    than ignored, because a PATCH that quietly drops `status: complete` answers 200
    to a request it did not carry out.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1, max_length=2000)
    location: str | None = Field(default=None, max_length=200)
    severity: PunchSeverity | None = None
    cost_code_id: uuid.UUID | None = None
    due_date: date | None = None


class PunchRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    description: str
    location: str | None
    status: PunchStatus
    severity: PunchSeverity
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    raised_by_id: uuid.UUID | None
    ball_in_court: BallInCourt
    from_walkthrough: bool
    due_date: date | None
    completed_date: date | None
    verified_by_id: uuid.UUID | None
    verified_date: date | None
    resolution_note: str | None
    inspection_item_id: uuid.UUID | None

    is_unresolved: bool
    is_overdue: bool
    days_overdue: int | None
    #: Whether this one alone stops the keys changing hands.
    blocks_handover: bool


class AssignPunch(BaseModel):
    vendor_id: uuid.UUID
    due_date: date | None = None


class PunchNote(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class PunchReason(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


# =========================================================== inspections =====


class InspectionCreate(BaseModel):
    job_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    inspection_type: InspectionType = InspectionType.MUNICIPAL
    scheduled_date: date | None = None
    inspector: str | None = Field(default=None, max_length=200)
    task_id: uuid.UUID | None = None
    fee: Money = Field(default=Decimal("0"), ge=0)


class CheckCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    sequence: int | None = None
    vendor_id: uuid.UUID | None = None


class CheckRead(ORMModel):
    id: uuid.UUID
    inspection_id: uuid.UUID
    sequence: int
    description: str
    result: ItemResult
    notes: str | None
    vendor_id: uuid.UUID | None


class CheckResult(BaseModel):
    result: ItemResult
    #: Required on a failure: somebody has to send a trade back for it, and
    #: "fail" on its own does not say what to fix.
    notes: str | None = Field(default=None, max_length=1000)


class InspectionRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    inspection_type: InspectionType
    name: str
    result: InspectionResult
    scheduled_date: date | None
    completed_date: date | None
    inspector: str | None
    failure_reason: str | None
    task_id: uuid.UUID | None
    reinspects_id: uuid.UUID | None
    fee: Money
    items: list[CheckRead]

    checked_items: int
    #: A failed municipal inspection is a legal stop, not just bad news.
    is_blocking: bool


class InspectionOutcome(BaseModel):
    result: InspectionResult
    failure_reason: str | None = Field(default=None, max_length=2000)
    completed_date: date | None = None
    fee: Money | None = Field(default=None, ge=0)


class ReinspectionRequest(BaseModel):
    scheduled_date: date
    fee: Money = Field(default=Decimal("0"), ge=0)


# ============================================================ daily logs =====


class DailyLogCreate(BaseModel):
    job_id: uuid.UUID
    log_date: date | None = None
    weather: str | None = Field(default=None, max_length=120)
    crew_count: int | None = Field(default=None, ge=0)
    trades_on_site: str | None = Field(default=None, max_length=500)
    work_performed: str | None = Field(default=None, max_length=4000)
    delays: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


class DailyLogRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    log_date: date
    author_id: uuid.UUID | None
    weather: str | None
    crew_count: int | None
    trades_on_site: str | None
    work_performed: str | None
    delays: str | None
    notes: str | None


# =============================================================== summary =====


class FieldSummary(BaseModel):
    """The numbers above the screen.

    `blocking_handover` is the one that matters at the end of a build: defects
    that are not merely cosmetic and will stop the keys changing hands.
    """

    open_punch: int
    awaiting_review: int
    overdue_punch: int
    blocking_handover: int
    from_walkthrough: int
    failed_inspections: int
    daily_logs: int
