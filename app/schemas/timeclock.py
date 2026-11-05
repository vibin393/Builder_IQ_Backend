"""Request and response shapes for the time clock."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.timeclock import OutOfBoundsStatus, ShiftStatus, ShiftType

Hours = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Coord = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.6f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ClockIn(BaseModel):
    """Start a shift now."""

    job_id: uuid.UUID
    worker_name: str = Field(min_length=1, max_length=200)
    user_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    shift_type: ShiftType = ShiftType.REGULAR
    lat: Decimal | None = Field(default=None, ge=-90, le=90)
    lng: Decimal | None = Field(default=None, ge=-180, le=180)
    out_of_bounds: OutOfBoundsStatus = OutOfBoundsStatus.UNKNOWN
    notes: str | None = Field(default=None, max_length=1000)


class ClockOut(BaseModel):
    lat: Decimal | None = Field(default=None, ge=-90, le=90)
    lng: Decimal | None = Field(default=None, ge=-180, le=180)


class ShiftCreate(BaseModel):
    """Enter a shift by hand — both ends known, e.g. a correction or a paper slip."""

    job_id: uuid.UUID
    worker_name: str = Field(min_length=1, max_length=200)
    user_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    shift_type: ShiftType = ShiftType.REGULAR
    clock_in_at: datetime
    clock_out_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ShiftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_name: str | None = Field(default=None, min_length=1, max_length=200)
    cost_code_id: uuid.UUID | None = None
    shift_type: ShiftType | None = None
    clock_in_at: datetime | None = None
    clock_out_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=1000)
    invoiced_to_client: bool | None = None


class ShiftRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    user_id: uuid.UUID | None
    worker_name: str
    cost_code_id: uuid.UUID | None
    status: ShiftStatus
    shift_type: ShiftType
    clock_in_at: datetime
    clock_out_at: datetime | None
    clock_in_lat: Coord | None
    clock_in_lng: Coord | None
    clock_out_lat: Coord | None
    clock_out_lng: Coord | None
    out_of_bounds: OutOfBoundsStatus
    notes: str | None
    approved_at: datetime | None
    approved_by_id: uuid.UUID | None
    invoiced_to_client: bool

    duration_minutes: int
    total_hours: Hours
    is_active: bool
    is_approved: bool


class ShiftMapPoint(BaseModel):
    """One pin on the employee locator map."""

    id: uuid.UUID
    job_id: uuid.UUID
    worker_name: str
    status: ShiftStatus
    lat: Coord
    lng: Coord
    clock_in_at: datetime
    out_of_bounds: OutOfBoundsStatus


class TimeClockSummary(BaseModel):
    job_id: uuid.UUID | None
    on_the_clock: int  # active shifts right now
    workers_today: int  # distinct workers with a shift today
    hours_today: Hours  # total hours clocked today (running shifts count up to now)
    pending_approval: int  # completed, not yet approved
    out_of_bounds: int  # shifts flagged clocked-in away from the job
