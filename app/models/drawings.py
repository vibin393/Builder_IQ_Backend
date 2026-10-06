"""Drawings, specifications and transmittals.

The gap I flagged at the very start of this project and never closed: a trade
bids against a specific revision of a plan set. When the drawings are revised,
whoever already priced or started work from the old set has to be told — and the
builder needs to be able to prove they were told.

A transmittal is that proof. It records who was sent which revision, when, and
whether they acknowledged it.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class DrawingDiscipline(StrEnum):
    ARCHITECTURAL = "architectural"
    STRUCTURAL = "structural"
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    CIVIL = "civil"
    LANDSCAPE = "landscape"
    OTHER = "other"


class SetStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"        # live: this is what trades should be building from
    SUPERSEDED = "superseded"
    VOID = "void"


class SpecStatus(StrEnum):
    """A specification runs the same supersede-not-overwrite lifecycle as a drawing
    set, for the same reason: what a trade priced against has to stay provable."""

    DRAFT = "draft"
    ISSUED = "issued"
    SUPERSEDED = "superseded"


class TransmittalPurpose(StrEnum):
    """Why the documents were sent - the single most useful field on a transmittal,
    because 'for construction' and 'for review' carry very different obligations."""

    FOR_CONSTRUCTION = "for_construction"
    FOR_REVIEW = "for_review"
    FOR_APPROVAL = "for_approval"
    FOR_INFORMATION = "for_information"
    FOR_RECORD = "for_record"


class DrawingSet(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One issued revision of a plan set. Sets supersede each other; they are
    never edited in place, because a superseded set is the evidence of what a
    trade was working from at the time."""

    __tablename__ = "drawing_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "set_number", name="uq_drawing_sets_tenant_number"),
    )

    set_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[SetStatus] = mapped_column(
        Enum(SetStatus, name="drawing_set_status", values_callable=lambda e: [m.value for m in e]),
        default=SetStatus.DRAFT,
        nullable=False,
        index=True,
    )
    issued_on: Mapped[date | None] = mapped_column(Date)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drawing_sets.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(1000))

    drawings: Mapped[list["Drawing"]] = relationship(
        back_populates="drawing_set", cascade="all, delete-orphan", lazy="selectin",
        order_by="Drawing.sheet_number",
    )

    @property
    def sheet_count(self) -> int:
        return len(self.drawings)

    @property
    def disciplines(self) -> dict[str, int]:
        """How many sheets of each discipline the set holds — the one-line contents."""
        out: dict[str, int] = {}
        for d in self.drawings:
            out[d.discipline.value] = out.get(d.discipline.value, 0) + 1
        return out

    def __repr__(self) -> str:
        return f"<DrawingSet {self.set_number} r{self.revision} {self.status}>"


class Drawing(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A single sheet within a set."""

    __tablename__ = "drawings"

    drawing_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drawing_sets.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sheet_number: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    discipline: Mapped[DrawingDiscipline] = mapped_column(
        Enum(
            DrawingDiscipline,
            name="drawing_discipline",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DrawingDiscipline.ARCHITECTURAL,
        nullable=False,
        index=True,
    )
    storage_key: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(String(1000))

    drawing_set: Mapped[DrawingSet] = relationship(back_populates="drawings")


class Specification(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A written specification section, keyed to a cost code where it maps."""

    __tablename__ = "specifications"

    section: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(String(8000))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    status: Mapped[SpecStatus] = mapped_column(
        Enum(SpecStatus, name="spec_status", values_callable=lambda e: [m.value for m in e]),
        default=SpecStatus.ISSUED,
        nullable=False,
        index=True,
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("specifications.id", ondelete="SET NULL")
    )

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    storage_key: Mapped[str | None] = mapped_column(String(500))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def division(self) -> str:
        """The CSI division the section falls under — the first token of the number,
        so '09 30 00' groups under '09'. Drives the by-division view."""
        return self.section.strip().split()[0] if self.section.strip() else ""


class Transmittal(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A recorded issue of documents to named recipients.

    The point is provable distribution. Without it, "we sent you the revised
    drawings" is one party's word against another's.
    """

    __tablename__ = "transmittals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "transmittal_number", name="uq_transmittals_tenant_number"),
    )

    transmittal_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str | None] = mapped_column(String(2000))
    purpose: Mapped[TransmittalPurpose] = mapped_column(
        Enum(
            TransmittalPurpose,
            name="transmittal_purpose",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TransmittalPurpose.FOR_CONSTRUCTION,
        nullable=False,
        index=True,
    )

    drawing_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drawing_sets.id", ondelete="SET NULL"), index=True
    )
    specification_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("specifications.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    sent_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    sent_on: Mapped[date | None] = mapped_column(Date, index=True)
    response_due: Mapped[date | None] = mapped_column(Date)

    recipients: Mapped[list["TransmittalRecipient"]] = relationship(
        back_populates="transmittal", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def acknowledged_count(self) -> int:
        return sum(1 for r in self.recipients if r.acknowledged_at)

    @property
    def outstanding(self) -> int:
        return len(self.recipients) - self.acknowledged_count

    @property
    def is_overdue(self) -> bool:
        """Someone has still not confirmed receipt, and the response date has passed.
        This is the transmittal that has to be chased before work goes wrong."""
        return bool(self.outstanding and self.response_due and self.response_due < date.today())


class TransmittalRecipient(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One trade on the distribution list, and whether they confirmed receipt."""

    __tablename__ = "transmittal_recipients"
    __table_args__ = (
        UniqueConstraint("transmittal_id", "vendor_id", name="uq_transmittal_recipient"),
    )

    transmittal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transmittals.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transmittal: Mapped[Transmittal] = relationship(back_populates="recipients")
