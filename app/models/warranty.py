"""Warranty and homeowner service, after the home closes.

A homeowner raises a WarrantyRequest (a claim); customer care acknowledges it against
an SLA, assesses whether it falls inside the home's warranty cover, then triages it into
one or more ServiceOrders, each dispatched to a trade. Visits to the home are booked as
WarrantyAppointments, and everything said about the claim is kept as a WarrantyNote.

Keeping the trade on the service order is what makes the valuable report possible: which
subcontractor generates the callbacks. Keeping the cost and the back-charge flag on it is
what lets the builder recover that money from the trade at fault.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class WarrantyStatus(StrEnum):
    SUBMITTED = "submitted"  # logged, not yet looked at
    ACKNOWLEDGED = "acknowledged"  # customer care has responded, SLA clock set
    TRIAGED = "triaged"  # assessed and work dispatched
    IN_PROGRESS = "in_progress"  # a trade is actively working it
    RESOLVED = "resolved"  # work done, awaiting close-out
    CLOSED = "closed"  # signed off, homeowner satisfied
    DECLINED = "declined"  # outside warranty coverage


#: Statuses that no longer count against the open/overdue queues.
CLOSED_STATUSES = (WarrantyStatus.RESOLVED, WarrantyStatus.CLOSED, WarrantyStatus.DECLINED)


class ServiceOrderStatus(StrEnum):
    ASSIGNED = "assigned"
    SCHEDULED = "scheduled"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class Priority(StrEnum):
    EMERGENCY = "emergency"  # habitability: no heat, no water, leak
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class WarrantyCategory(StrEnum):
    """The building element at fault. Drives both coverage and the callback report."""

    STRUCTURAL = "structural"  # foundations, load-bearing - the 10-year cover
    ROOFING = "roofing"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    HEATING = "heating"  # boiler, radiators, underfloor
    EXTERIOR = "exterior"  # render, brickwork, windows, doors
    INTERIOR = "interior"  # plaster, decoration, flooring
    JOINERY = "joinery"  # kitchens, wardrobes, internal doors
    APPLIANCE = "appliance"
    DAMP = "damp"  # damp, condensation, water ingress
    DRAINAGE = "drainage"
    LANDSCAPING = "landscaping"  # driveways, paths, fencing, turf
    OTHER = "other"


#: Categories carried by the structural (10-year) cover rather than the 2-year
#: defects cover. Everything else is workmanship, covered for the shorter period.
STRUCTURAL_CATEGORIES = (WarrantyCategory.STRUCTURAL,)


class CoverageStatus(StrEnum):
    PENDING = "pending"  # not yet assessed
    IN_WARRANTY = "in_warranty"  # builder's responsibility, no charge to owner
    OUT_OF_WARRANTY = "out_of_warranty"  # expired or excluded
    GOODWILL = "goodwill"  # out of cover, but done anyway


class AppointmentKind(StrEnum):
    ASSESSMENT = "assessment"  # someone comes to look before dispatching
    SERVICE_VISIT = "service_visit"  # a trade attends to do the work
    INSPECTION = "inspection"  # scheduled warranty inspection (e.g. end of defects period)
    FOLLOW_UP = "follow_up"  # a return visit to confirm the fix held


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"  # homeowner not in - a real, countable cost


class NoteVisibility(StrEnum):
    INTERNAL = "internal"  # staff-only working notes
    HOMEOWNER = "homeowner"  # visible to the homeowner in the portal


def _enum(enum_cls, name):
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class WarrantyRequest(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "warranty_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_number", name="uq_warranty_tenant_number"),
    )

    request_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )

    status: Mapped[WarrantyStatus] = mapped_column(
        _enum(WarrantyStatus, "warranty_status"),
        default=WarrantyStatus.SUBMITTED,
        nullable=False,
        index=True,
    )
    priority: Mapped[Priority] = mapped_column(
        _enum(Priority, "warranty_priority"),
        default=Priority.NORMAL,
        nullable=False,
        index=True,
    )
    category: Mapped[WarrantyCategory] = mapped_column(
        _enum(WarrantyCategory, "warranty_category"),
        default=WarrantyCategory.OTHER,
        nullable=False,
        index=True,
    )
    coverage: Mapped[CoverageStatus] = mapped_column(
        _enum(CoverageStatus, "warranty_coverage"),
        default=CoverageStatus.PENDING,
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000))
    location: Mapped[str | None] = mapped_column(String(200))  # "master bathroom"
    reported_via: Mapped[str | None] = mapped_column(String(20))  # portal, phone, email, site

    submitted_date: Mapped[date | None] = mapped_column(Date)
    acknowledged_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)  # SLA response target
    resolved_date: Mapped[date | None] = mapped_column(Date)
    closed_date: Mapped[date | None] = mapped_column(Date)

    assessment_notes: Mapped[str | None] = mapped_column(String(2000))
    decline_reason: Mapped[str | None] = mapped_column(String(500))

    service_orders: Mapped[list["ServiceOrder"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )
    appointments: Mapped[list["WarrantyAppointment"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="WarrantyAppointment.scheduled_for",
    )
    notes: Mapped[list["WarrantyNote"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="WarrantyNote.created_at.desc()",
    )

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_STATUSES

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.due_date and self.due_date < date.today())

    def __repr__(self) -> str:
        return f"<WarrantyRequest {self.request_number} ({self.status})>"


class ServiceOrder(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Work dispatched to one trade to resolve part of a warranty request."""

    __tablename__ = "service_orders"

    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("warranty_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    status: Mapped[ServiceOrderStatus] = mapped_column(
        _enum(ServiceOrderStatus, "service_order_status"),
        default=ServiceOrderStatus.ASSIGNED,
        nullable=False,
        index=True,
    )
    instructions: Mapped[str | None] = mapped_column(String(2000))
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    completed_date: Mapped[date | None] = mapped_column(Date)
    resolution: Mapped[str | None] = mapped_column(String(2000))

    nte_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # not-to-exceed
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # actual cost
    back_charge: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    request: Mapped[WarrantyRequest] = relationship(back_populates="service_orders")

    def __repr__(self) -> str:
        return f"<ServiceOrder {self.id} ({self.status})>"


class WarrantyAppointment(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A booked visit to the home - assessment, service, inspection or follow-up."""

    __tablename__ = "warranty_appointments"

    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("warranty_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[AppointmentKind] = mapped_column(
        _enum(AppointmentKind, "warranty_appointment_kind"),
        default=AppointmentKind.SERVICE_VISIT,
        nullable=False,
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum(AppointmentStatus, "warranty_appointment_status"),
        default=AppointmentStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    slot: Mapped[str | None] = mapped_column(String(40))  # "AM", "PM", "09:00-12:00"
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    outcome: Mapped[str | None] = mapped_column(String(1000))

    request: Mapped[WarrantyRequest] = relationship(back_populates="appointments")

    def __repr__(self) -> str:
        return f"<WarrantyAppointment {self.kind} {self.scheduled_for}>"


class WarrantyNote(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """An entry in the claim's activity log. The homeowner sees only HOMEOWNER notes."""

    __tablename__ = "warranty_notes"

    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("warranty_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    visibility: Mapped[NoteVisibility] = mapped_column(
        _enum(NoteVisibility, "note_visibility"),
        default=NoteVisibility.INTERNAL,
        nullable=False,
    )
    body: Mapped[str] = mapped_column(String(4000), nullable=False)

    request: Mapped[WarrantyRequest] = relationship(back_populates="notes")

    def __repr__(self) -> str:
        return f"<WarrantyNote {self.id} ({self.visibility})>"
