"""Request and response shapes for the design studio."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.sales_ops import AppointmentStatus, DocumentKind, SignatureStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================= allowance =====


class AllowanceSummary(BaseModel):
    """What the buyer may spend, what they have spent, and what is left.

    Pending and confirmed are separated because they mean different things to the
    person reading: confirmed is on a signed addendum, pending is a decision made
    in the room that has not been papered yet. Both count against the budget,
    because a buyer who chose something believes they have committed to it.
    """

    job_id: uuid.UUID
    has_contract: bool
    allowance: Money
    confirmed: Money
    pending: Money
    spent: Money
    remaining: Money
    over_by: Money
    is_over: bool


class SetAllowance(BaseModel):
    amount: Money = Field(ge=0)


# ========================================================== appointments =====


class AppointmentCreate(BaseModel):
    job_id: uuid.UUID
    #: Required. An appointment with no date is a note, and the list showed a row
    #: reading "Not set" because this used to be optional.
    scheduled_for: datetime
    buyer_id: uuid.UUID | None = None
    consultant_id: uuid.UUID | None = None
    duration_minutes: int = Field(default=180, ge=15, le=720)
    location: str | None = Field(default=None, max_length=200)
    structural_cutoff: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class AppointmentRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    consultant_id: uuid.UUID | None
    scheduled_for: datetime | None
    duration_minutes: int
    status: AppointmentStatus
    location: str | None
    notes: str | None
    structural_cutoff: date | None
    completed_at: datetime | None
    outcome_note: str | None
    rescheduled_from_id: uuid.UUID | None

    # Derived: the two things a consultant looks at before the buyer arrives.
    is_open: bool
    structural_cutoff_passed: bool
    days_to_cutoff: int | None


class RescheduleRequest(BaseModel):
    scheduled_for: datetime
    note: str | None = Field(default=None, max_length=500)


class OutcomeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class CutoffRequest(BaseModel):
    structural_cutoff: date


# ============================================================ signatures =====


class SignatureCreate(BaseModel):
    job_id: uuid.UUID
    document_kind: DocumentKind = DocumentKind.ADDENDUM
    document_name: str = Field(min_length=1, max_length=300)
    signer_name: str = Field(min_length=1, max_length=200)
    signer_email: EmailStr
    expires_on: date | None = None


class SignatureRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    document_kind: DocumentKind
    document_name: str
    signer_name: str
    signer_email: str
    status: SignatureStatus
    sent_at: datetime | None
    signed_at: datetime | None
    declined_at: datetime | None
    decline_reason: str | None
    void_reason: str | None
    expires_on: date | None
    reminders_sent: int
    last_reminder_at: datetime | None
    provider: str | None
    external_id: str | None

    is_outstanding: bool
    days_outstanding: int | None
    is_expired: bool


class DeclineRequest(BaseModel):
    """Why the buyer refused.

    Required, because a declined addendum means the selections behind it are
    wrong and the next version will be wrong the same way if nobody says how.
    """

    reason: str = Field(min_length=3, max_length=500)


class VoidRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


# =============================================================== summary =====


class StudioSummary(BaseModel):
    """The counters above the studio.

    `awaiting_signature` is money agreed but not committed; `cutoff_soon` is
    buyers about to lose the ability to change anything structural. Both are work
    for today rather than statistics.
    """

    appointments: dict[str, int]
    signatures: dict[str, int]
    scheduled: int
    awaiting_signature: int
    signed: int
    cutoff_soon: int
