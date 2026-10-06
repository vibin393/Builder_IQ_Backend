"""Schemas for trade prequalification and compliance."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.compliance import DocumentType, PrequalStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Documents ----------------------------------------------------------------


class ComplianceDocumentCreate(BaseModel):
    document_type: DocumentType
    reference: str | None = None
    issuer: str | None = None
    coverage_amount: Money | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    storage_key: str | None = None


class ComplianceDocumentRead(ORMModel):
    id: uuid.UUID
    document_type: DocumentType
    reference: str | None
    issuer: str | None
    coverage_amount: Money | None
    issued_on: date | None
    expires_on: date | None
    is_expired: bool
    days_remaining: int | None


# --- Prequalification ---------------------------------------------------------


class PrequalificationCreate(BaseModel):
    vendor_id: uuid.UUID
    single_job_limit: Money | None = None
    aggregate_limit: Money | None = None
    review_due: date | None = None
    notes: str | None = None


class PrequalificationUpdate(BaseModel):
    single_job_limit: Money | None = None
    aggregate_limit: Money | None = None
    review_due: date | None = None
    notes: str | None = None


class ApprovePayload(BaseModel):
    """Approve outright, or conditionally with a cap or caveat."""

    conditional: bool = False
    single_job_limit: Money | None = None
    aggregate_limit: Money | None = None
    note: str | None = None


class PrequalificationRead(ORMModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    status: PrequalStatus
    single_job_limit: Money | None
    aggregate_limit: Money | None
    reviewed_on: date | None
    review_due: date | None
    notes: str | None
    is_compliant: bool
    review_overdue: bool
    blocking_reason: str | None
    missing_required: list[str]
    documents: list[ComplianceDocumentRead] = []


class ComplianceCheck(BaseModel):
    """Whether a trade may be given work, and why not if not."""

    vendor_id: uuid.UUID
    ok: bool
    reason: str | None = None
    warning: str | None = None
    expired: list[str] = []
    expiring: list[str] = []


class ExpiringDocument(BaseModel):
    vendor_id: uuid.UUID
    vendor_name: str
    vendor_code: str
    document_type: str
    reference: str | None
    expires_on: date
    days_remaining: int | None
    is_expired: bool


class ComplianceSummary(BaseModel):
    prequalified: int
    cleared: int               # on file and the PO gate would pass today
    approved: int
    conditional: int
    would_be_refused: int      # on file but the PO gate would refuse today
    no_record: int             # active trades with no prequalification at all
    review_overdue: int
    missing_required: int      # prequals missing a required cover
    expiring_soon: int         # documents lapsing within 30 days
    expired_documents: int
    cover_gaps: int            # scheduled tasks running past a lapse
    by_status: dict[str, int]


class InviteBody(BaseModel):
    review_due: date | None = None
    note: str | None = Field(default=None, max_length=500)
