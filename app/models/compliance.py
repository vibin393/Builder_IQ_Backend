"""Trade prequalification and compliance.

Procore's strongest idea that neither BuildTopia nor most homebuilder platforms
have: a trade must be *qualified* before they can be given work, and their
insurance has to still be in date on the day the work is committed.

This is not paperwork for its own sake. If a subcontractor's liability cover has
lapsed and someone is hurt on their scaffold, the exposure lands on the builder.
Making the purchase-order flow check it is the point of the module.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class PrequalStatus(StrEnum):
    NOT_STARTED = "not_started"
    INVITED = "invited"
    SUBMITTED = "submitted"  # trade has returned their pack
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"  # cleared to bid and to hold purchase orders
    CONDITIONAL = "conditional"  # approved with a cap or a caveat
    REJECTED = "rejected"
    EXPIRED = "expired"  # was approved; a document has since lapsed


class DocumentType(StrEnum):
    # UK trade paperwork — what a British homebuilder actually asks a subcontractor for.
    PUBLIC_LIABILITY = "public_liability"  # third-party injury/damage cover
    EMPLOYERS_LIABILITY = "employers_liability"  # legally required if they have staff
    PROFESSIONAL_INDEMNITY = "professional_indemnity"
    CIS_REGISTRATION = "cis_registration"  # Construction Industry Scheme
    VAT_REGISTRATION = "vat_registration"
    TRADE_ACCREDITATION = "trade_accreditation"  # Gas Safe, NICEIC, FENSA, etc.
    HEALTH_SAFETY_POLICY = "health_safety_policy"
    DBS_CHECK = "dbs_check"  # for work around occupied homes
    # Legacy US-style values kept so records created before the UK rework still read.
    GENERAL_LIABILITY = "general_liability"
    WORKERS_COMP = "workers_comp"
    AUTO_LIABILITY = "auto_liability"
    UMBRELLA = "umbrella"
    TRADE_LICENCE = "trade_licence"
    W9 = "w9"
    BUSINESS_LICENCE = "business_licence"
    SAFETY_PROGRAM = "safety_program"
    OTHER = "other"


#: The two covers every UK trade must hold on file. A prequalification missing either is
#: flagged even while it is technically approved, because it is one claim from a problem.
REQUIRED_DOCUMENT_TYPES = (
    DocumentType.PUBLIC_LIABILITY,
    DocumentType.EMPLOYERS_LIABILITY,
)


class Prequalification(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One trade's standing with this builder."""

    __tablename__ = "prequalifications"
    __table_args__ = (UniqueConstraint("vendor_id", name="uq_prequal_vendor"),)

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[PrequalStatus] = mapped_column(
        Enum(PrequalStatus, name="prequal_status", values_callable=lambda e: [m.value for m in e]),
        default=PrequalStatus.NOT_STARTED,
        nullable=False,
        index=True,
    )

    # A trade cleared for £50k of framing is not thereby cleared for £2m of it.
    single_job_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    aggregate_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_on: Mapped[date | None] = mapped_column(Date)
    # The whole record is re-examined annually even if no document has lapsed.
    review_due: Mapped[date | None] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(String(2000))

    documents: Mapped[list["ComplianceDocument"]] = relationship(
        back_populates="prequalification", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def expired_documents(self) -> list["ComplianceDocument"]:
        today = date.today()
        return [d for d in self.documents if d.expires_on and d.expires_on < today]

    @property
    def expiring_soon(self) -> list["ComplianceDocument"]:
        """Inside 30 days. Chasing a renewal beats blocking a purchase order."""
        today = date.today()
        return [
            d
            for d in self.documents
            if d.expires_on and today <= d.expires_on <= date.fromordinal(today.toordinal() + 30)
        ]

    @property
    def review_overdue(self) -> bool:
        return bool(
            self.status in (PrequalStatus.APPROVED, PrequalStatus.CONDITIONAL)
            and self.review_due
            and self.review_due < date.today()
        )

    @property
    def missing_required(self) -> list[str]:
        """Required cover types with no in-date document on file."""
        held = {
            d.document_type
            for d in self.documents
            if not (d.expires_on and d.expires_on < date.today())
        }
        return [t.value for t in REQUIRED_DOCUMENT_TYPES if t not in held]

    @property
    def blocking_reason(self) -> str | None:
        """Why the purchase-order gate would refuse this trade today, or None if clear.
        The single source of truth the 'PO gate' badge and the gate itself both read."""
        if self.status is PrequalStatus.REJECTED:
            return "prequalification was rejected"
        if self.status not in (PrequalStatus.APPROVED, PrequalStatus.CONDITIONAL):
            return f"prequalification is {self.status.value.replace('_', ' ')}"
        lapsed = self.expired_documents
        if lapsed:
            return f"{len(lapsed)} document(s) have lapsed"
        if self.review_overdue:
            return "annual review is overdue"
        return None

    @property
    def is_compliant(self) -> bool:
        """Clear to hold a purchase order — approved, nothing lapsed, review in date.
        Matches the actual gate so the badge and the refusal never disagree."""
        return self.blocking_reason is None

    def __repr__(self) -> str:
        return f"<Prequalification {self.vendor_id} {self.status}>"


class ComplianceDocument(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A certificate or licence held on file, with the date it stops counting."""

    __tablename__ = "compliance_documents"

    prequalification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prequalifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(
            DocumentType,
            name="compliance_document_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        index=True,
    )
    reference: Mapped[str | None] = mapped_column(String(120))  # policy or licence number
    issuer: Mapped[str | None] = mapped_column(String(200))  # insurer or authority
    coverage_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date, index=True)
    storage_key: Mapped[str | None] = mapped_column(String(500))

    prequalification: Mapped[Prequalification] = relationship(back_populates="documents")

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_on and self.expires_on < date.today())

    @property
    def days_remaining(self) -> int | None:
        if not self.expires_on:
            return None
        return (self.expires_on - date.today()).days
