"""Document metadata: plans, contracts, photos, permits.

Only metadata lives here. File bytes belong in object storage; `storage_key` points at
them. Keeping bytes out of the database is what lets a superintendent upload 40 site
photos a day without the database becoming the bottleneck.
"""

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import BigInteger, Date, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class DocumentCategory(StrEnum):
    PLAN = "plan"
    SPECIFICATION = "specification"
    CONTRACT = "contract"
    ADDENDUM = "addendum"
    PURCHASE_ORDER = "purchase_order"
    CHANGE_ORDER = "change_order"
    PERMIT = "permit"
    INSPECTION = "inspection"
    PHOTO = "photo"
    WARRANTY = "warranty"
    INSURANCE = "insurance"
    LIEN_WAIVER = "lien_waiver"
    OTHER = "other"


class DocumentStatus(StrEnum):
    DRAFT = "draft"            # registered but not yet the live version
    ACTIVE = "active"          # the current, in-force document
    SUPERSEDED = "superseded"  # replaced by a newer revision, kept as the record
    ARCHIVED = "archived"      # withdrawn from the register, not deleted


#: Categories that cost money or stop work when they cannot be produced. These are the
#: ones the register exists to protect, and the ones an expiry is worth chasing on.
CRITICAL_CATEGORIES = (
    DocumentCategory.CONTRACT,
    DocumentCategory.PERMIT,
    DocumentCategory.LIEN_WAIVER,
    DocumentCategory.WARRANTY,
    DocumentCategory.INSURANCE,
)

#: How far ahead a lapse is flagged, so a permit or certificate can be renewed before
#: it stops work rather than after.
EXPIRING_WINDOW_DAYS = 30


class Document(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[DocumentCategory] = mapped_column(
        Enum(
            DocumentCategory,
            name="document_category",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DocumentCategory.OTHER,
        nullable=False,
        index=True,
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DocumentStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    # A permit number, contract reference or certificate number - what the document is
    # called by the body that issued it, so it can be found by that as well as by name.
    reference: Mapped[str | None] = mapped_column(String(120))

    # Plans get revised constantly, and trades bid against a specific revision.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )

    # The dates that turn a filing cabinet into a chase list. A permit or an insurance
    # certificate that has lapsed is the document that stops work or costs money.
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date, index=True)

    # Attached to whichever record it belongs to. All optional: a community plan set
    # belongs to no single job.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    # Photos attached to a day's log. Procore's daily log is photo-first, and a
    # written log with no pictures is worth very little in a dispute.
    daily_log_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_logs.id", ondelete="CASCADE"), index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(1000))

    @property
    def is_critical(self) -> bool:
        return self.category in CRITICAL_CATEGORIES

    @property
    def is_live(self) -> bool:
        """Still on the active register — not superseded or archived."""
        return self.status in (DocumentStatus.DRAFT, DocumentStatus.ACTIVE)

    @property
    def days_to_expiry(self) -> int | None:
        if self.expires_on is None:
            return None
        return (self.expires_on - date.today()).days

    @property
    def is_expired(self) -> bool:
        return bool(self.is_live and self.expires_on and self.expires_on < date.today())

    @property
    def is_expiring(self) -> bool:
        """Live, dated, and lapses within the warning window (and not already lapsed)."""
        d = self.days_to_expiry
        return bool(self.is_live and d is not None and 0 <= d <= EXPIRING_WINDOW_DAYS)

    def __repr__(self) -> str:
        return f"<Document {self.name} r{self.revision}>"
