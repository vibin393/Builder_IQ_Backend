"""Schemas for the warranty / homeowner-care module."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.warranty import (
    AppointmentKind,
    AppointmentStatus,
    CoverageStatus,
    NoteVisibility,
    Priority,
    ServiceOrderStatus,
    WarrantyCategory,
    WarrantyStatus,
)

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Requests -----------------------------------------------------------------


class WarrantyRequestCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    location: str | None = None
    priority: Priority = Priority.NORMAL
    category: WarrantyCategory = WarrantyCategory.OTHER
    reported_via: str | None = Field(default="portal", max_length=20)


class WarrantyRequestUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    location: str | None = None
    priority: Priority | None = None
    category: WarrantyCategory | None = None


class AssessPayload(BaseModel):
    """The coverage decision: our bill, the homeowner's, or a goodwill gesture."""

    coverage: CoverageStatus
    assessment_notes: str | None = Field(default=None, max_length=2000)
    decline_reason: str | None = Field(default=None, max_length=500)


# --- Service orders -----------------------------------------------------------


class ServiceOrderCreate(BaseModel):
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    instructions: str | None = None
    scheduled_date: date | None = None
    nte_amount: Money | None = Field(default=None, ge=0)  # not-to-exceed


class ServiceOrderRead(ORMModel):
    id: uuid.UUID
    request_id: uuid.UUID
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    status: ServiceOrderStatus
    instructions: str | None
    scheduled_date: date | None
    completed_date: date | None
    resolution: str | None
    nte_amount: Money | None
    cost_amount: Money | None
    back_charge: bool


# --- Appointments -------------------------------------------------------------


class AppointmentCreate(BaseModel):
    kind: AppointmentKind = AppointmentKind.SERVICE_VISIT
    scheduled_for: date
    slot: str | None = Field(default=None, max_length=40)
    vendor_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class AppointmentUpdate(BaseModel):
    status: AppointmentStatus | None = None
    scheduled_for: date | None = None
    slot: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)
    outcome: str | None = Field(default=None, max_length=1000)


class AppointmentRead(ORMModel):
    id: uuid.UUID
    request_id: uuid.UUID
    kind: AppointmentKind
    status: AppointmentStatus
    scheduled_for: date
    slot: str | None
    vendor_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    notes: str | None
    outcome: str | None


# --- Notes --------------------------------------------------------------------


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    visibility: NoteVisibility = NoteVisibility.INTERNAL


class NoteRead(ORMModel):
    id: uuid.UUID
    request_id: uuid.UUID
    author_id: uuid.UUID | None
    visibility: NoteVisibility
    body: str
    created_at: datetime


# --- Read (detail) ------------------------------------------------------------


class WarrantyRequestRead(ORMModel):
    id: uuid.UUID
    request_number: str
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    status: WarrantyStatus
    priority: Priority
    category: WarrantyCategory
    coverage: CoverageStatus
    title: str
    description: str | None
    location: str | None
    reported_via: str | None
    submitted_date: date | None
    acknowledged_date: date | None
    due_date: date | None
    resolved_date: date | None
    closed_date: date | None
    assessment_notes: str | None
    decline_reason: str | None
    is_overdue: bool = False
    service_orders: list[ServiceOrderRead] = []
    appointments: list[AppointmentRead] = []
    notes: list[NoteRead] = []


# --- Coverage lookup ----------------------------------------------------------


class CoverageRead(BaseModel):
    job_id: uuid.UUID
    closing_date: date | None
    defects_until: date | None
    structural_until: date | None
    defects_active: bool
    structural_active: bool


# --- Dashboard summary --------------------------------------------------------


class TradeCallback(BaseModel):
    vendor_id: uuid.UUID | None = None
    vendor_code: str | None = None
    vendor_name: str
    service_orders: int
    back_charges: int
    share: float


class WarrantySummary(BaseModel):
    total: int
    open: int
    overdue: int
    emergency_open: int
    unassessed: int
    resolved_last_30: int
    avg_resolution_days: float | None
    service_orders_open: int
    back_charge_total: Money
    appointments_upcoming: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    by_coverage: dict[str, int]
    callbacks_by_trade: list[TradeCallback]
