"""Request and response shapes for labour and productivity."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

#: Money as a fixed-2dp string, so a float never rounds a rate on its way out.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
#: Hours to 2dp. Quarter and half hours are normal; thirds are not.
Hours = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ==================================================== timecards =====


class TimecardCreate(BaseModel):
    job_id: uuid.UUID
    worker_name: str = Field(min_length=1, max_length=200)
    hours: Decimal = Field(gt=0, le=24)
    worked_on: date | None = None
    overtime_hours: Decimal = Field(default=Decimal("0"), ge=0, le=24)
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    #: Left off, the trade's standard rate is used. Retyping it on every entry is
    #: how an average rate drifts without anybody deciding it should.
    hourly_rate: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class TimecardUpdate(BaseModel):
    """Correcting an entry.

    Approval is absent and extras are refused: it moves through its own routes,
    which record who signed the hours off.
    """

    model_config = ConfigDict(extra="forbid")

    worker_name: str | None = Field(default=None, min_length=1, max_length=200)
    hours: Decimal | None = Field(default=None, gt=0, le=24)
    worked_on: date | None = None
    overtime_hours: Decimal | None = Field(default=None, ge=0, le=24)
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    hourly_rate: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class ApproveMany(BaseModel):
    timecard_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class TimecardRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    task_id: uuid.UUID | None
    worked_on: date
    worker_name: str
    hours: Hours
    overtime_hours: Hours
    hourly_rate: Money | None
    notes: str | None
    entered_by_id: uuid.UUID | None
    approved_on: date | None
    approved_by_id: uuid.UUID | None

    total_hours: Hours
    #: None, not zero, when no rate is known. Zero would claim the work was free.
    labour_cost: Money | None
    is_approved: bool


class SkippedApproval(BaseModel):
    timecard_id: uuid.UUID
    reason: str


class ApprovalResult(BaseModel):
    """What a batch approval actually did.

    Refusing the whole batch because one of forty has no rate means the other
    thirty-nine wait, so the ones that can go through do.
    """

    approved: list[TimecardRead]
    skipped: list[SkippedApproval]


# ================================================= productivity =====


class ProductivityRow(BaseModel):
    cost_code_id: uuid.UUID | None
    cost_code: str
    cost_code_name: str
    hours: Hours
    labour_cost: Money
    entries: int
    #: Hours booked with no rate. Their cost is unknown, not nought.
    unrated_hours: Hours
    #: Hours nobody has signed off yet.
    pending_hours: Hours

    #: What the home was supposed to take. None where no target was set - a
    #: variance against zero is just the actual with a minus sign in front.
    budgeted_hours: Hours | None
    budgeted_cost: Money | None
    hours_variance: Hours | None
    cost_variance: Money | None
    #: Over the hours that have a rate, not over all of them.
    average_rate: Money | None


class ProductivityReport(BaseModel):
    job_id: uuid.UUID | None
    total_hours: Hours
    total_labour_cost: Money
    overtime_hours: Hours
    unrated_hours: Hours
    pending_hours: Hours
    average_rate: Money | None
    budgeted_hours: Hours | None
    budgeted_cost: Money | None
    hours_variance: Hours | None
    cost_variance: Money | None
    rows: list[ProductivityRow]


class WorkerRow(BaseModel):
    worker_name: str
    hours: Hours
    overtime_hours: Hours
    labour_cost: Money
    unrated_hours: Hours
    entries: int
    days_on_site: int
    average_day: Hours | None
    average_rate: Money | None
    #: The trade they belong to, where they only ever belong to one.
    vendor_id: uuid.UUID | None
    #: More than one usually means two people with the same name.
    vendor_count: int


class VendorRow(BaseModel):
    vendor_id: uuid.UUID | None
    hours: Hours
    overtime_hours: Hours
    labour_cost: Money
    unrated_hours: Hours
    entries: int
    workers: int
    average_rate: Money | None
    #: What share of their hours is overtime. A trade running at a third overtime
    #: is either short-handed or billing you for being.
    overtime_share: Decimal | None


class LabourSummary(BaseModel):
    entries: int
    workers: int
    pending_approval: int
    unrated_entries: int

    total_hours: Hours
    total_labour_cost: Money
    overtime_hours: Hours
    unrated_hours: Hours
    pending_hours: Hours
    average_rate: Money | None
    budgeted_hours: Hours | None
    hours_variance: Hours | None
    cost_variance: Money | None


# ============================================== budgeted hours =====


class SetBudgetedHours(BaseModel):
    """The target a home's labour is measured against."""

    cost_code_id: uuid.UUID
    budgeted_hours: Decimal | None = Field(default=None, ge=0)
