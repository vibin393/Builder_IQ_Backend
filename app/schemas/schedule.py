"""Request and response shapes for the build schedule."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.schedule import TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================== templates =====


class TemplateTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Both are working days. A ten-day task is ten days somebody turns up, and
    #: the calendar stretches around weekends rather than the trade being booked
    #: onto one.
    offset_days: int = Field(default=0, ge=0)
    duration_days: int = Field(default=1, ge=1)
    sequence: int | None = None
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    is_milestone: bool = False


class TemplateTaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    offset_days: int | None = Field(default=None, ge=0)
    duration_days: int | None = Field(default=None, ge=1)
    sequence: int | None = None
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    is_milestone: bool | None = None


class TemplateTaskRead(ORMModel):
    id: uuid.UUID
    template_id: uuid.UUID
    sequence: int
    name: str
    offset_days: int
    duration_days: int
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    is_milestone: bool


class TemplateCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    plan_id: uuid.UUID | None = None


class TemplateUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    plan_id: uuid.UUID | None = None
    is_active: bool | None = None


class TemplateRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    plan_id: uuid.UUID | None
    is_active: bool
    tasks: list[TemplateTaskRead]


class DuplicateTemplate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


class ReorderRequest(BaseModel):
    """Every task, exactly once, in the order wanted.

    A partial list would leave gaps or duplicates in the sequence, and the
    sequence is what the whole knock-on effect is built from.
    """

    task_ids: list[uuid.UUID] = Field(min_length=1)


# ================================================================= tasks =====


class GenerateRequest(BaseModel):
    template_id: uuid.UUID
    #: `start_date`, matching the Job model and the rest of the API. Renaming it
    #: to `start` bought nothing and broke every existing caller.
    start_date: date | None = None


class JobTaskRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    sequence: int
    name: str
    status: TaskStatus
    scheduled_start: date | None
    scheduled_finish: date | None
    actual_start: date | None
    actual_finish: date | None
    duration_days: int
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    is_milestone: bool
    notes: str | None
    blocked_reason: str | None

    days_late: int
    is_overdue: bool
    days_overdue: int | None
    is_open: bool


class StartRequest(BaseModel):
    actual_start: date | None = None


class BlockRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class SkipRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class CompleteRequest(BaseModel):
    actual_finish: date | None = None


class RescheduleRequest(BaseModel):
    scheduled_start: date
    #: On by default. A task that moves on its own leaves the next trade booked
    #: for a day the site will not be ready.
    cascade: bool = True


class AssignTradeRequest(BaseModel):
    vendor_id: uuid.UUID | None = None


class TaskCompletion(BaseModel):
    task_id: uuid.UUID
    actual_finish: date
    days_late: int
    tasks_shifted: int
    next_task_id: uuid.UUID | None


class RescheduleResult(BaseModel):
    task_id: uuid.UUID
    tasks_shifted: int


# ============================================================== holidays =====


class HolidayCreate(BaseModel):
    on_date: date
    name: str = Field(min_length=1, max_length=120)


class HolidayRead(ORMModel):
    id: uuid.UUID
    on_date: date
    name: str


# =============================================================== summary =====


class ScheduleSummary(BaseModel):
    """The numbers above the schedule. `overdue` and `blocked` are the two that
    mean somebody needs to do something today."""

    templates: int
    in_progress: int
    due_soon: int
    overdue: int
    blocked: int
