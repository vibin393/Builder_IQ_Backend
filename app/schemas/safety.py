"""Request and response shapes for safety and quality."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.safety import (
    ActionPlanStatus,
    IncidentType,
    ObservationStatus,
    ObservationType,
    RiddorCategory,
    Severity,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ==================================================== observations =====


class ObservationCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    location: str | None = Field(default=None, max_length=200)
    observation_type: ObservationType = ObservationType.SAFETY
    severity: Severity = Severity.MEDIUM
    inspection_id: uuid.UUID | None = None
    assigned_vendor_id: uuid.UUID | None = None
    #: Left off, a date is filled in from the severity. An observation with no
    #: date is a note, and these are not notes.
    due_on: date | None = None


class ObservationUpdate(BaseModel):
    """Correcting the details.

    Status is absent and extras are refused. It moves through its own routes,
    which record who accepted the fix - and a PATCH that quietly dropped
    `status: closed` would answer 200 to a request it did not carry out.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    location: str | None = Field(default=None, max_length=200)
    observation_type: ObservationType | None = None
    severity: Severity | None = None
    assigned_vendor_id: uuid.UUID | None = None
    due_on: date | None = None


class AssignObservation(BaseModel):
    vendor_id: uuid.UUID
    due_on: date | None = None


class ResolutionBody(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


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
    raised_by_id: uuid.UUID | None
    due_on: date | None
    closed_on: date | None
    closed_by_id: uuid.UUID | None
    resolution: str | None

    is_open: bool
    is_overdue: bool
    days_overdue: int | None


# ======================================================= incidents =====


class IncidentCreate(BaseModel):
    job_id: uuid.UUID
    incident_type: IncidentType
    title: str = Field(min_length=1, max_length=300)
    occurred_on: date | None = None
    severity: Severity = Severity.MEDIUM
    description: str | None = None
    location: str | None = Field(default=None, max_length=200)
    person_involved: str | None = Field(default=None, max_length=200)
    vendor_id: uuid.UUID | None = None
    lost_time: bool = False
    medical_treatment: bool = False
    immediate_action: str | None = None
    #: Left off, it is suggested from the facts. Stating it overrides the
    #: suggestion - the regulations turn on things this system does not hold.
    riddor_category: RiddorCategory | None = None


class ClassifyIncident(BaseModel):
    riddor_category: RiddorCategory


class HseReport(BaseModel):
    reported_on: date | None = None
    reference: str | None = Field(default=None, max_length=100)


class RootCause(BaseModel):
    root_cause: str = Field(min_length=3, max_length=2000)


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
    riddor_category: RiddorCategory
    reported_to_hse_on: date | None
    hse_reference: str | None
    immediate_action: str | None
    root_cause: str | None
    closed_on: date | None

    #: The day the written report is due with the HSE, from the category.
    riddor_deadline: date | None
    #: Some categories must also be notified by telephone first, so showing the
    #: deadline alone would be reassuring in the wrong direction.
    report_without_delay: bool
    report_overdue: bool
    days_to_report: int | None
    is_open: bool


# ==================================================== action plans =====


class ActionPlanItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    requires_signoff: bool = False
    assigned_to_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    due_on: date | None = None
    sequence: int | None = Field(default=None, ge=1)


class ActionPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    job_id: uuid.UUID | None = None
    due_on: date | None = None
    is_template: bool = False


class StartFromTemplate(BaseModel):
    job_id: uuid.UUID
    due_on: date | None = None


class CompleteStep(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class ActionPlanItemRead(ORMModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    sequence: int
    description: str
    requires_signoff: bool
    assigned_to_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    due_on: date | None
    completed_on: date | None
    completed_by_id: uuid.UUID | None
    signed_off_by_id: uuid.UUID | None
    signed_off_on: date | None
    notes: str | None

    awaiting_signoff: bool
    is_done: bool


class ActionPlanRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID | None
    name: str
    description: str | None
    status: ActionPlanStatus
    due_on: date | None
    owner_id: uuid.UUID | None
    is_template: bool
    template_id: uuid.UUID | None
    started_on: date | None
    items: list[ActionPlanItemRead]

    completed_items: int
    #: Witnessed where a witness was required. The number the meter uses.
    done_items: int
    percent_complete: int
    is_overdue: bool
    next_item: ActionPlanItemRead | None


# ======================================================== summary =====


class SafetySummary(BaseModel):
    """The numbers above the list, counted over everything rather than over the
    page that happens to be on screen."""

    open_observations: int
    awaiting_review: int
    overdue: int
    critical: int
    from_inspections: int

    incidents: int
    open_incidents: int
    reportable: int
    awaiting_hse_report: int
    hse_report_overdue: int
    lost_time: int
    near_misses: int

    action_plans: int
    plans_in_progress: int
    plans_overdue: int
    plan_templates: int
