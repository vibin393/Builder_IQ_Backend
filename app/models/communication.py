"""RFIs, submittals, meetings and correspondence.

The formal question-and-approval chain a construction project runs on. Procore's
depth here is the single biggest gap between it and a homebuilder platform, and
the part most worth importing: these records are what a builder relies on months
later when somebody asks who approved what, and when.

The concept that carries all of it is **ball in court** — at any moment exactly
one party owes the next move, and the record says who.
"""

import uuid
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.rfi import RFIActivity, RFIResponse
    from app.models.submittal import SubmittalActivity, SubmittalRevision


class BallInCourt(StrEnum):
    BUILDER = "builder"
    TRADE = "trade"
    ARCHITECT = "architect"
    ENGINEER = "engineer"
    BUYER = "buyer"
    CLOSED = "closed"

# Which user role actually answers when the ball sits in each court.
#
# Procore ships one combined "Architect/Engineer" template rather than two, and
# the same person usually holds both hats on a house, so ARCHITECT serves both
# courts. BUILDER is any internal role; CLOSED is nobody.
COURT_ROLE = {
    BallInCourt.ARCHITECT: "architect",
    BallInCourt.ENGINEER: "architect",
    BallInCourt.BUYER: "homeowner",
    BallInCourt.TRADE: "trade",
    BallInCourt.BUILDER: None,   # any internal role
    BallInCourt.CLOSED: None,    # nobody owes anything
}


class RFIStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"
    VOID = "void"


#: Statuses that no longer sit in anyone's court, and drop out of the open queue.
RFI_CLOSED_STATUSES = (RFIStatus.CLOSED, RFIStatus.VOID)


class RFICategory(StrEnum):
    """What the question is about. Drives filtering and the discipline it routes to."""

    DESIGN = "design"                # a drawing is unclear or incomplete
    STRUCTURAL = "structural"        # loads, foundations, steel
    ARCHITECTURAL = "architectural"  # layout, finishes, setting-out
    MEP = "mep"                      # mechanical, electrical, plumbing
    CIVILS = "civils"               # drainage, groundworks, roads
    COORDINATION = "coordination"    # two trades or two drawings disagree
    SITE_CONDITION = "site_condition"  # what is in the ground differs from the drawing
    MATERIALS = "materials"          # a specified product is unavailable or wrong
    BUILDING_CONTROL = "building_control"  # a regs or warranty-inspector query
    OTHER = "other"


class SubmittalStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    APPROVED_AS_NOTED = "approved_as_noted"   # usable, with corrections
    REVISE_AND_RESUBMIT = "revise_and_resubmit"
    REJECTED = "rejected"


#: Statuses the reviewer has finished with, from the register's point of view. A
#: submittal here needs no more action from the builder.
SUBMITTAL_CLOSED_STATUSES = (SubmittalStatus.APPROVED, SubmittalStatus.REJECTED)
#: Statuses that sit in the review queue - the builder owes a decision.
SUBMITTAL_REVIEW_STATUSES = (SubmittalStatus.SUBMITTED, SubmittalStatus.UNDER_REVIEW)


class SubmittalType(StrEnum):
    """What kind of thing is being put forward. Procore's standard submittal types,
    trimmed to what a housebuilder actually raises."""

    PRODUCT_DATA = "product_data"        # a spec sheet for a named product
    SHOP_DRAWING = "shop_drawing"        # a fabricator's drawing (trusses, steel, joinery)
    SAMPLE = "sample"                    # a physical sample (tile, brick, worktop)
    MOCK_UP = "mock_up"                  # a built sample panel
    CERTIFICATE = "certificate"          # a test/compliance certificate
    METHOD_STATEMENT = "method_statement"  # how a trade intends to do the work
    O_AND_M = "o_and_m"                  # operation & maintenance manual
    WARRANTY_DOC = "warranty_doc"        # a manufacturer warranty document
    OTHER = "other"


#: Working days the reviewer is assumed to need. Used to work backwards from the
#: date a product is needed on site to the date it must be submitted by, so the
#: register can flag procurement that is running out of runway before it is late.
SUBMITTAL_REVIEW_ALLOWANCE_DAYS = 10


class MeetingStatus(StrEnum):
    SCHEDULED = "scheduled"
    HELD = "held"
    CANCELLED = "cancelled"


class MeetingType(StrEnum):
    """What kind of meeting it is. Procore's standard set, trimmed to a housebuilder's."""

    PRE_CONSTRUCTION = "pre_construction"   # kick-off before a start on site
    COORDINATION = "coordination"           # the recurring trade coordination
    OAC = "oac"                             # owner / architect / contractor
    PROGRESS = "progress"                   # progress / site review
    PRE_INSTALL = "pre_install"             # pre-installation for one trade
    SAFETY = "safety"
    HANDOVER = "handover"                   # pre-completion / handover to the buyer
    OTHER = "other"


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# ============================================================== RFIs =======

class RFI(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A formal question that blocks work until it is answered."""

    __tablename__ = "rfis"
    __table_args__ = (UniqueConstraint("tenant_id", "rfi_number", name="uq_rfis_tenant_number"),)

    rfi_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    question: Mapped[str] = mapped_column(String(4000), nullable=False)

    status: Mapped[RFIStatus] = mapped_column(
        Enum(RFIStatus, name="rfi_status", values_callable=lambda e: [m.value for m in e]),
        default=RFIStatus.DRAFT,
        nullable=False,
        index=True,
    )
    ball_in_court: Mapped[BallInCourt] = mapped_column(
        Enum(BallInCourt, name="ball_in_court", values_callable=lambda e: [m.value for m in e]),
        default=BallInCourt.BUILDER,
        nullable=False,
        index=True,
    )
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, name="comm_priority", values_callable=lambda e: [m.value for m in e]),
        default=Priority.NORMAL,
        nullable=False,
    )
    category: Mapped[RFICategory] = mapped_column(
        Enum(RFICategory, name="rfi_category", values_callable=lambda e: [m.value for m in e]),
        default=RFICategory.OTHER,
        nullable=False,
        index=True,
    )

    # Where on the home the question bites, and the spec/drawing detail it hangs off.
    # Both are what makes an RFI findable months later without opening it.
    location: Mapped[str | None] = mapped_column(String(200))
    spec_reference: Mapped[str | None] = mapped_column(String(120))

    raised_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    drawing_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drawings.id", ondelete="SET NULL")
    )

    raised_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date, index=True)
    answered_on: Mapped[date | None] = mapped_column(Date)
    closed_on: Mapped[date | None] = mapped_column(Date)

    # An RFI that stops work is the one that costs money; flagging it is what
    # makes the overdue list worth reading.
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_impact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_impact_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # The official answer, denormalised from the response marked official. Kept on
    # the RFI itself so the change-order flow and the log read it without a join.
    answer: Mapped[str | None] = mapped_column(String(4000))
    answered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # The conversation and the audit trail. selectin, like the warranty children, so
    # a list or a detail read never lazy-loads under async and never N+1s.
    responses: Mapped[list["RFIResponse"]] = relationship(
        back_populates="rfi",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RFIResponse.created_at",
    )
    activity: Mapped[list["RFIActivity"]] = relationship(
        back_populates="rfi",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RFIActivity.created_at.desc()",
    )

    @property
    def is_open(self) -> bool:
        return self.status not in RFI_CLOSED_STATUSES and self.status is not RFIStatus.ANSWERED

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_on
            and self.status in (RFIStatus.OPEN, RFIStatus.DRAFT)
            and self.due_on < date.today()
        )

    @property
    def days_open(self) -> int | None:
        if not self.raised_on:
            return None
        end = self.answered_on or date.today()
        return (end - self.raised_on).days

    @property
    def days_to_answer(self) -> int | None:
        """How long the question actually took, once answered. Feeds the response-time
        report; None while it is still open."""
        if not self.raised_on or not self.answered_on:
            return None
        return (self.answered_on - self.raised_on).days

    @property
    def response_count(self) -> int:
        return len(self.responses)

    def __repr__(self) -> str:
        return f"<RFI {self.rfi_number} {self.status}>"


# ========================================================= submittals =====

class Submittal(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A product or material put forward for approval before it is installed.

    In production housing this is how a trade proves the tile they intend to fit
    is the tile the buyer selected, before a hundred square metres of the wrong
    one goes down.
    """

    __tablename__ = "submittals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "submittal_number", name="uq_submittals_tenant_number"),
    )

    submittal_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    # Submittals are often per plan rather than per home, since the same product
    # goes into every house built to that plan.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    spec_section: Mapped[str | None] = mapped_column(String(64))
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    model_number: Mapped[str | None] = mapped_column(String(120))

    submittal_type: Mapped[SubmittalType] = mapped_column(
        Enum(SubmittalType, name="submittal_type", values_callable=lambda e: [m.value for m in e]),
        default=SubmittalType.PRODUCT_DATA,
        nullable=False,
        index=True,
    )
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, name="comm_priority", values_callable=lambda e: [m.value for m in e]),
        default=Priority.NORMAL,
        nullable=False,
    )

    status: Mapped[SubmittalStatus] = mapped_column(
        Enum(
            SubmittalStatus,
            name="submittal_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SubmittalStatus.DRAFT,
        nullable=False,
        index=True,
    )
    ball_in_court: Mapped[BallInCourt] = mapped_column(
        Enum(BallInCourt, name="ball_in_court", values_callable=lambda e: [m.value for m in e]),
        default=BallInCourt.TRADE,
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    # The option a buyer chose that this product is meant to satisfy.
    option_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("options.id", ondelete="SET NULL")
    )

    submitted_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date, index=True)
    returned_on: Mapped[date | None] = mapped_column(Date)
    approved_on: Mapped[date | None] = mapped_column(Date)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    review_notes: Mapped[str | None] = mapped_column(String(2000))
    storage_key: Mapped[str | None] = mapped_column(String(500))

    # Procurement: the date the product is needed on site, and the manufacturing/
    # delivery lead time. Together they say the date it must be submitted by - the
    # figure that makes a submittal register worth keeping rather than a filing cabinet.
    required_on_site: Mapped[date | None] = mapped_column(Date, index=True)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    revisions: Mapped[list["SubmittalRevision"]] = relationship(
        back_populates="submittal",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SubmittalRevision.revision",
    )
    activity: Mapped[list["SubmittalActivity"]] = relationship(
        back_populates="submittal",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SubmittalActivity.created_at.desc()",
    )

    @property
    def is_open(self) -> bool:
        """Not yet finished with, from the register's point of view."""
        return self.status not in SUBMITTAL_CLOSED_STATUSES

    @property
    def is_overdue(self) -> bool:
        open_states = (
            SubmittalStatus.SUBMITTED,
            SubmittalStatus.UNDER_REVIEW,
            SubmittalStatus.REVISE_AND_RESUBMIT,
        )
        return bool(self.due_on and self.status in open_states and self.due_on < date.today())

    @property
    def order_by_date(self) -> date | None:
        """The last day this can be ordered and still arrive when it is needed."""
        if self.required_on_site is None:
            return None
        return self.required_on_site - timedelta(days=self.lead_time_days)

    @property
    def submit_by_date(self) -> date | None:
        """The last day this can be submitted and still leave time to review it and
        then order it before it is needed on site."""
        order_by = self.order_by_date
        if order_by is None:
            return None
        return order_by - timedelta(days=SUBMITTAL_REVIEW_ALLOWANCE_DAYS)

    @property
    def is_at_risk(self) -> bool:
        """Still needs approval, and there is no longer enough runway to review and
        procure it before it is needed. The early-warning the overdue flag can't give:
        overdue looks at the review due date, this looks at the site."""
        return bool(self.is_open and self.submit_by_date and self.submit_by_date < date.today())

    @property
    def review_turnaround_days(self) -> int | None:
        """How long the last review round took, once returned. Feeds the turnaround
        report; None while it is still out for review."""
        if not self.submitted_on or not self.returned_on:
            return None
        return (self.returned_on - self.submitted_on).days

    def __repr__(self) -> str:
        return f"<Submittal {self.submittal_number} {self.status}>"


# =========================================================== meetings =====

class Meeting(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """An agenda that becomes minutes, and minutes that carry actions."""

    __tablename__ = "meetings"

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    meeting_type: Mapped[MeetingType] = mapped_column(
        Enum(MeetingType, name="meeting_type", values_callable=lambda e: [m.value for m in e]),
        default=MeetingType.COORDINATION,
        nullable=False,
        index=True,
    )
    meeting_date: Mapped[date | None] = mapped_column(Date, index=True)
    location: Mapped[str | None] = mapped_column(String(200))
    attendees: Mapped[str | None] = mapped_column(String(2000))

    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="meeting_status", values_callable=lambda e: [m.value for m in e]),
        default=MeetingStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(String(4000))
    chaired_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # A follow-up meeting points back at the one it continues, so a recurring series
    # (and the actions carried between them) can be followed.
    previous_meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id", ondelete="SET NULL")
    )

    items: Mapped[list["MeetingItem"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MeetingItem.sequence",
    )

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def open_actions(self) -> int:
        return sum(1 for i in self.items if i.is_action and not i.completed_on)

    @property
    def overdue_actions(self) -> int:
        return sum(1 for i in self.items if i.is_overdue)


class MeetingItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """An agenda line. Marked as an action, it becomes something owed by someone."""

    __tablename__ = "meeting_items"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    discussion: Mapped[str | None] = mapped_column(String(2000))

    is_action: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    due_on: Mapped[date | None] = mapped_column(Date)
    completed_on: Mapped[date | None] = mapped_column(Date)
    # When an open action is carried into a follow-up meeting, the new item points back
    # at the one it came from, so an action that has rolled over three meetings is visible.
    carried_from_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting_items.id", ondelete="SET NULL")
    )

    meeting: Mapped[Meeting] = relationship(back_populates="items")

    @property
    def is_open_action(self) -> bool:
        return self.is_action and self.completed_on is None

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open_action and self.due_on and self.due_on < date.today())


# ===================================================== correspondence =====

class Correspondence(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Anything said in writing that someone may need to produce later.

    Deliberately a thin, catch-all log rather than a messaging product: the
    value is the audit trail, not a chat client nobody would use.
    """

    __tablename__ = "correspondence"

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(String(4000))
    # note, email, call or letter
    kind: Mapped[str] = mapped_column(String(40), default="note", nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="outbound", nullable=False)

    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    occurred_on: Mapped[date | None] = mapped_column(Date, index=True)

    # A letter that demands a reply is not just a record - it is a clock. Tracking the
    # reply is what turns the log into a chase list instead of a filing cabinet.
    requires_response: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_due: Mapped[date | None] = mapped_column(Date, index=True)
    responded_on: Mapped[date | None] = mapped_column(Date)

    @property
    def is_awaiting_response(self) -> bool:
        return bool(self.requires_response and self.responded_on is None)

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.is_awaiting_response and self.response_due and self.response_due < date.today()
        )
