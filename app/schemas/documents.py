"""Schemas for the document register."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentCategory, DocumentStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: DocumentCategory = DocumentCategory.OTHER
    storage_key: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=120)
    content_type: str | None = None
    size_bytes: int | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    job_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    notes: str | None = None


class DocumentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: DocumentCategory | None = None
    reference: str | None = Field(default=None, max_length=120)
    issued_on: date | None = None
    expires_on: date | None = None
    job_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    notes: str | None = None


class DocumentSupersede(BaseModel):
    """Register a newer version that replaces this one. The old one is kept as the
    record of what was in force before. Anything omitted carries forward."""

    storage_key: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    reference: str | None = Field(default=None, max_length=120)
    content_type: str | None = None
    size_bytes: int | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    notes: str | None = None


class DocumentRead(ORMModel):
    id: uuid.UUID
    name: str
    category: DocumentCategory
    status: DocumentStatus
    storage_key: str
    reference: str | None
    content_type: str | None
    size_bytes: int | None
    revision: int
    superseded_by_id: uuid.UUID | None
    issued_on: date | None
    expires_on: date | None
    job_id: uuid.UUID | None
    community_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    uploaded_by_id: uuid.UUID | None
    notes: str | None
    created_at: datetime
    is_critical: bool
    is_expired: bool
    is_expiring: bool
    days_to_expiry: int | None


class DocumentSummary(BaseModel):
    total: int               # live documents (excludes superseded / archived)
    critical: int            # contracts, permits, waivers, warranties, insurance
    not_tied_to_home: int
    expiring_soon: int
    expired: int
    categories_in_use: int
    by_category: dict[str, int]
    by_status: dict[str, int]
