"""Design studio appointments and e-signature requests.

Both sit between the contract and the build: the appointment is where the buyer spends
their options budget, and the signature request is what makes the resulting addendum
binding.

E-signature here is a *tracking* record. Actually sending for signature means calling a
provider (DocuSign and similar); `provider` and `external_id` are where that plugs in.
Nothing in this model asserts legal validity on its own.
"""

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETE = "complete"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


#: An appointment that has not happened yet. A job may hold only one of these -
#: two live bookings for the same house is a diary conflict, not a feature.
OPEN_APPOINTMENTS = (AppointmentStatus.SCHEDULED,)


class SignatureStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    VIEWED = "viewed"
    SIGNED = "signed"
    DECLINED = "declined"
    EXPIRED = "expired"


class DocumentKind(StrEnum):
    CONTRACT = "contract"
    ADDENDUM = "addendum"
    CHANGE_ORDER = "change_order"
    WARRANTY = "warranty"
    OTHER = "other"


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    The columns are `DateTime(timezone=True)`, which PostgreSQL honours and SQLite
    does not - so the same value comes back aware in production and naive under
    test, and subtracting one from `datetime.now(UTC)` raises on only one of them.
    Normalising here means the property behaves the same on both rather than being
    correct on the platform that happens to be watched.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class DesignAppointment(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The design studio session where a buyer chooses their options."""

    __tablename__ = "design_appointments"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )
    consultant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int] = mapped_column(default=180, nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(
            AppointmentStatus,
            name="appointment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AppointmentStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    location: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(String(2000))

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why it did not happen. Free text would do for one appointment; across a
    #: year it is the difference between "buyers keep rescheduling" being a
    #: suspicion and being a number.
    outcome_note: Mapped[str | None] = mapped_column(String(500))
    #: The booking this one replaced. A chain of these is how "this buyer has
    #: moved three times" becomes visible instead of anecdotal.
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("design_appointments.id", ondelete="SET NULL")
    )

    # Structural options have to be chosen before framing, so the studio needs a
    # visible cut-off rather than a convention people remember.
    structural_cutoff: Mapped[date | None] = mapped_column(Date)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_APPOINTMENTS

    @property
    def structural_cutoff_passed(self) -> bool:
        """Past the point where the frame is going up and structural options are
        no longer a choice."""
        return bool(self.structural_cutoff and self.structural_cutoff < date.today())

    @property
    def days_to_cutoff(self) -> int | None:
        """How long the buyer has left to decide anything structural.

        The number a design consultant leads with, because after it the answer
        stops being "we can price that" and becomes "no".
        """
        if not self.structural_cutoff:
            return None
        return (self.structural_cutoff - date.today()).days

    def __repr__(self) -> str:
        return f"<DesignAppointment {self.job_id} {self.status}>"


class SignatureRequest(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "signature_requests"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_kind: Mapped[DocumentKind] = mapped_column(
        Enum(DocumentKind, name="document_kind", values_callable=lambda e: [m.value for m in e]),
        default=DocumentKind.CONTRACT,
        nullable=False,
    )
    document_name: Mapped[str] = mapped_column(String(300), nullable=False)
    signer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    signer_email: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[SignatureStatus] = mapped_column(
        Enum(
            SignatureStatus,
            name="signature_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SignatureStatus.DRAFT,
        nullable=False,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_on: Mapped[date | None] = mapped_column(Date)
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why the buyer refused. The most valuable field on this record and the one
    #: most likely to be skipped, because a declined addendum means the selections
    #: behind it are wrong and somebody has to find out how.
    decline_reason: Mapped[str | None] = mapped_column(String(500))
    void_reason: Mapped[str | None] = mapped_column(String(500))
    #: How many times we have chased. Counted so "we sent it once in March" cannot
    #: be claimed when the record says otherwise.
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the real signing happened. Empty until a provider is wired up.
    provider: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(200))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )

    @property
    def is_outstanding(self) -> bool:
        """Sent and not yet resolved. What the chasing list is made of."""
        return self.status in (SignatureStatus.SENT, SignatureStatus.VIEWED)

    @property
    def days_outstanding(self) -> int | None:
        if not self.sent_at or not self.is_outstanding:
            return None
        return (datetime.now(UTC) - _aware(self.sent_at)).days

    @property
    def is_expired(self) -> bool:
        """Past its date and still unsigned. Derived rather than stored, so it
        becomes true on the day rather than when a job happens to run."""
        return bool(self.expires_on and self.is_outstanding and self.expires_on < date.today())

    def __repr__(self) -> str:
        return f"<SignatureRequest {self.document_name} {self.status}>"
