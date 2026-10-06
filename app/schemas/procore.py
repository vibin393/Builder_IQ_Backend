"""Schemas for the Procore-shaped modules.

Prequalification, RFIs, submittals, drawings and transmittals, specifications,
meetings, correspondence, observations, incidents, action plans and timecards.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.safety import (
    ActionPlanStatus,
    IncidentType,
    ObservationStatus,
    ObservationType,
    Severity,
)

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# NOTE: The prequalification / compliance schemas moved to app/schemas/compliance.py
# when trade compliance grew a full lifecycle, required-document coverage and analytics.


# NOTE: The RFI schemas moved to app/schemas/rfi.py when RFIs grew a response
# thread, an activity log and analytics. They live beside the RFI router now.


# NOTE: The submittal schemas moved to app/schemas/submittal.py when submittals grew
# a revision history, an activity log, procurement tracking and analytics.


# NOTE: The drawing-set, specification and transmittal schemas moved to
# app/schemas/drawings.py when the module grew supersede workflows, transmittal
# purpose/overdue tracking and analytics.


# NOTE: The meeting, meeting-item and correspondence schemas moved to
# app/schemas/meeting.py when meetings grew action completion, follow-up carry-forward,
# correspondence follow-up tracking and analytics.


# ==================================================== safety, quality =====

class ObservationCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    location: str | None = None
    observation_type: ObservationType = ObservationType.SAFETY
    severity: Severity = Severity.MEDIUM
    inspection_id: uuid.UUID | None = None
    assigned_vendor_id: uuid.UUID | None = None
    due_on: date | None = None


class ObservationUpdate(BaseModel):
    status: ObservationStatus | None = None
    severity: Severity | None = None
    assigned_vendor_id: uuid.UUID | None = None
    due_on: date | None = None
    resolution: str | None = None


class ObservationRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    description: str | None
    location: str | None
    observation_type: ObservationType
    status: ObservationStatus
    severity: Severity
    inspection_id: uuid.UUID | None
    assigned_vendor_id: uuid.UUID | None
    due_on: date | None
    closed_on: date | None
    resolution: str | None
    is_overdue: bool


class IncidentCreate(BaseModel):
    job_id: uuid.UUID
    incident_type: IncidentType
    title: str = Field(min_length=1, max_length=300)
    occurred_on: date | None = None
    severity: Severity = Severity.MEDIUM
    description: str | None = None
    location: str | None = None
    person_involved: str | None = None
    vendor_id: uuid.UUID | None = None
    lost_time: bool = False
    medical_treatment: bool = False
    reportable: bool = False
    immediate_action: str | None = None


class IncidentRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    incident_type: IncidentType
    severity: Severity
    occurred_on: date
    title: str
    description: str | None
    location: str | None
    person_involved: str | None
    vendor_id: uuid.UUID | None
    lost_time: bool
    medical_treatment: bool
    reportable: bool
    immediate_action: str | None
    root_cause: str | None
    closed_on: date | None


class ActionPlanItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    requires_signoff: bool = False
    assigned_to_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    due_on: date | None = None


class ActionPlanItemRead(ORMModel):
    id: uuid.UUID
    sequence: int
    description: str
    requires_signoff: bool
    assigned_to_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    due_on: date | None
    completed_on: date | None
    notes: str | None


class ActionPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    job_id: uuid.UUID | None = None
    description: str | None = None
    due_on: date | None = None
    is_template: bool = False


class ActionPlanRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID | None
    name: str
    description: str | None
    status: ActionPlanStatus
    due_on: date | None
    is_template: bool
    completed_items: int
    percent_complete: int
    items: list[ActionPlanItemRead] = []


# ========================================================== timecards =====

class TimecardCreate(BaseModel):
    job_id: uuid.UUID
    worker_name: str = Field(min_length=1, max_length=200)
    hours: Decimal = Field(gt=0, le=24)
    worked_on: date | None = None
    overtime_hours: Decimal = Decimal("0")
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    hourly_rate: Decimal | None = None
    notes: str | None = None


class TimecardRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    worked_on: date
    worker_name: str
    hours: Decimal
    overtime_hours: Decimal
    total_hours: Decimal
    hourly_rate: Decimal | None
    labour_cost: Decimal | None
    approved_on: date | None
    notes: str | None


class ProductivityRow(BaseModel):
    cost_code_id: uuid.UUID | None
    cost_code: str
    cost_code_name: str
    hours: Decimal
    labour_cost: Money
    entries: int


class ProductivityReport(BaseModel):
    job_id: uuid.UUID | None
    total_hours: Decimal
    total_labour_cost: Money
    rows: list[ProductivityRow]
