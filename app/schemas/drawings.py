"""Schemas for drawings, specifications and transmittals."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.drawings import (
    DrawingDiscipline,
    SetStatus,
    SpecStatus,
    TransmittalPurpose,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Drawings & sets ----------------------------------------------------------


class DrawingCreate(BaseModel):
    sheet_number: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    discipline: DrawingDiscipline = DrawingDiscipline.ARCHITECTURAL
    storage_key: str | None = None
    notes: str | None = None


class DrawingUpdate(BaseModel):
    sheet_number: str | None = Field(default=None, min_length=1, max_length=32)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    discipline: DrawingDiscipline | None = None
    storage_key: str | None = None
    notes: str | None = None


class DrawingRead(ORMModel):
    id: uuid.UUID
    sheet_number: str
    title: str
    discipline: DrawingDiscipline
    storage_key: str | None
    notes: str | None


class DrawingSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    notes: str | None = None


class DrawingSetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    plan_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    notes: str | None = None


class DrawingSetRead(ORMModel):
    id: uuid.UUID
    set_number: str
    name: str
    revision: int
    plan_id: uuid.UUID | None
    community_id: uuid.UUID | None
    status: SetStatus
    issued_on: date | None
    superseded_by_id: uuid.UUID | None
    notes: str | None
    sheet_count: int
    disciplines: dict[str, int]
    drawings: list[DrawingRead] = []


# --- Specifications -----------------------------------------------------------


class SpecificationCreate(BaseModel):
    section: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    body: str | None = None
    plan_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    storage_key: str | None = None
    # Raise it straight to issued, or hold as a draft.
    issue_now: bool = True


class SpecificationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = None
    plan_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    storage_key: str | None = None


class SpecificationRevise(BaseModel):
    """The changes carried into the new revision. Anything omitted is copied forward."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = None
    cost_code_id: uuid.UUID | None = None
    storage_key: str | None = None


class SpecificationRead(ORMModel):
    id: uuid.UUID
    section: str
    division: str
    title: str
    body: str | None
    revision: int
    status: SpecStatus
    superseded_by_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    storage_key: str | None
    is_current: bool


# --- Transmittals -------------------------------------------------------------


class TransmittalCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    purpose: TransmittalPurpose = TransmittalPurpose.FOR_CONSTRUCTION
    message: str | None = None
    drawing_set_id: uuid.UUID | None = None
    specification_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    response_due: date | None = None
    vendor_ids: list[uuid.UUID] = Field(min_length=1)


class TransmittalRecipientRead(ORMModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    acknowledged_at: datetime | None


class TransmittalRead(ORMModel):
    id: uuid.UUID
    transmittal_number: str
    subject: str
    purpose: TransmittalPurpose
    message: str | None
    drawing_set_id: uuid.UUID | None
    specification_id: uuid.UUID | None
    job_id: uuid.UUID | None
    sent_on: date | None
    response_due: date | None
    acknowledged_count: int
    outstanding: int
    is_overdue: bool
    recipients: list[TransmittalRecipientRead] = []


# --- Analytics ----------------------------------------------------------------


class DrawingsSummary(BaseModel):
    draft_sets: int
    live_sets: int             # issued
    superseded_sets: int
    total_sheets: int          # sheets across live sets
    specifications: int        # current specifications
    draft_specs: int
    transmittals: int
    unacknowledged: int        # recipients yet to confirm, across all transmittals
    overdue_transmittals: int  # past response due with someone still outstanding
    by_discipline: dict[str, int]   # sheets by discipline across live sets
    by_purpose: dict[str, int]
