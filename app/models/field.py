"""Field operations: daily logs, inspections and punch lists.

This is the superintendent's half of the system. Punch lists and inspections were the
notable gap in the original feature map - "issues/exceptions" as a single line - and
they are where a home actually stops being finishable.
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
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.communication import BallInCourt


class InspectionType(StrEnum):
    MUNICIPAL = "municipal"  # the city; a failure blocks the build
    THIRD_PARTY = "third_party"  # engineer, energy rater
    INTERNAL_QA = "internal_qa"  # the builder's own checklist
    WALKTHROUGH = "walkthrough"  # with the buyer, before closing


class InspectionResult(StrEnum):
    SCHEDULED = "scheduled"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemResult(StrEnum):
    """Result of one check within an inspection."""

    PENDING = "pending"
    PASS = "pass"  # nosec B105 - inspection result, not a password
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class PunchStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    READY_FOR_REVIEW = "ready_for_review"
    #: Checked by the builder and accepted. Distinct from the trade saying it is
    #: done, which is what READY_FOR_REVIEW means.
    COMPLETE = "complete"
    #: Deliberately not being fixed, with a reason. The status a buyer argues
    #: about at handover, so it carries an explanation.
    WONT_FIX = "wont_fix"


#: Still to be dealt with. What "open punch items" means, and what stops a home
#: being handed over.
UNRESOLVED_PUNCH = (
    PunchStatus.OPEN,
    PunchStatus.ASSIGNED,
    PunchStatus.READY_FOR_REVIEW,
)


class PunchSeverity(StrEnum):
    """How much this one matters.

    A scuff and a cracked soil pipe are both punch items and only one of them
    stops a handover. Without this the list is a flat wall of defects and the
    superintendent sorts it by memory.
    """

    COSMETIC = "cosmetic"
    MINOR = "minor"
    MAJOR = "major"
    #: Life-safety or habitability. These block handover whatever else is agreed.
    BLOCKING = "blocking"


class DailyLog(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """What happened on site today. The record that makes a delay defensible later."""

    __tablename__ = "daily_logs"
    __table_args__ = (
        # Two logs for the same day on the same house makes the record that is
        # supposed to substantiate a delay claim contradict itself.
        UniqueConstraint("job_id", "log_date", name="uq_daily_logs_job_date"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    weather: Mapped[str | None] = mapped_column(String(120))
    # Weather and crew counts are what substantiate a schedule claim against a trade.
    crew_count: Mapped[int | None] = mapped_column(Integer)
    trades_on_site: Mapped[str | None] = mapped_column(String(500))
    work_performed: Mapped[str | None] = mapped_column(String(4000))
    delays: Mapped[str | None] = mapped_column(String(2000))
    notes: Mapped[str | None] = mapped_column(String(2000))

    def __repr__(self) -> str:
        return f"<DailyLog {self.job_id} {self.log_date}>"


class Inspection(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "inspections"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    inspection_type: Mapped[InspectionType] = mapped_column(
        Enum(
            InspectionType,
            name="inspection_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=InspectionType.MUNICIPAL,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    result: Mapped[InspectionResult] = mapped_column(
        Enum(
            InspectionResult,
            name="inspection_result",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=InspectionResult.SCHEDULED,
        nullable=False,
        index=True,
    )
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    completed_date: Mapped[date | None] = mapped_column(Date)
    inspector: Mapped[str | None] = mapped_column(String(200))
    # A failed inspection usually means a re-inspection fee and a schedule hit.
    failure_reason: Mapped[str | None] = mapped_column(String(2000))
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_tasks.id", ondelete="SET NULL")
    )
    #: The failed inspection this one is retrying. A chain of these answers "how
    #: many times did this fail before it passed", which is what tells a builder
    #: whose work is costing them re-inspection fees.
    reinspects_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspections.id", ondelete="SET NULL")
    )
    #: What the re-inspection cost. Municipal re-inspection fees are small and
    #: relentless, and nobody can argue them back to a trade without a number.
    fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"), nullable=False)

    items: Mapped[list["InspectionItem"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def failed_items(self) -> list["InspectionItem"]:
        return [i for i in self.items if i.result is ItemResult.FAIL]

    @property
    def checked_items(self) -> int:
        return sum(1 for i in self.items if i.result is not ItemResult.PENDING)

    @property
    def is_blocking(self) -> bool:
        """A failed municipal inspection stops the build.

        The others are bad news; this one is a legal stop, and the distinction
        matters to whoever is deciding whether to send the next trade.
        """
        return (
            self.result is InspectionResult.FAILED
            and self.inspection_type is InspectionType.MUNICIPAL
        )

    def __repr__(self) -> str:
        return f"<Inspection {self.name} {self.result}>"


class InspectionItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One line on an inspection checklist.

    A framing inspection is thirty checks, not one verdict. Recording the overall
    result alone tells you the house failed; recording the items tells you what to
    send the framer back for.
    """

    __tablename__ = "inspection_items"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("inspections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    result: Mapped[ItemResult] = mapped_column(
        Enum(
            ItemResult,
            name="inspection_item_result",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ItemResult.PENDING,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    # Which trade owns this check, so a failure can be sent straight to them.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )

    inspection: Mapped[Inspection] = relationship(back_populates="items")


class PunchItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A defect to fix before the home can be handed over."""

    __tablename__ = "punch_items"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[PunchStatus] = mapped_column(
        Enum(PunchStatus, name="punch_status", values_callable=lambda e: [m.value for m in e]),
        default=PunchStatus.OPEN,
        nullable=False,
        index=True,
    )
    # Assigning the trade is what lets a punch list be backcharged rather than absorbed.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    raised_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Whose move it is. RFIs and submittals already work this way; a punch item
    # that nobody owns is exactly the kind that sits open until handover week.
    ball_in_court: Mapped[BallInCourt] = mapped_column(
        Enum(BallInCourt, name="ball_in_court", values_callable=lambda e: [m.value for m in e]),
        default=BallInCourt.TRADE,
        nullable=False,
        index=True,
    )
    # Raised at the buyer walkthrough rather than by the builder's own QA.
    from_walkthrough: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    severity: Mapped[PunchSeverity] = mapped_column(
        Enum(
            PunchSeverity,
            name="punch_severity",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PunchSeverity.MINOR,
        nullable=False,
        index=True,
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    completed_date: Mapped[date | None] = mapped_column(Date)

    #: Who accepted the fix, and when. A punch item used to go straight from the
    #: trade saying it was done to complete, so a home could be handed over on a
    #: snag list nobody had checked.
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    verified_date: Mapped[date | None] = mapped_column(Date)
    #: Why it is not being fixed, or why a claimed fix was sent back.
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    #: The failed check this came from, where it came from one. Turning a failed
    #: inspection into a punch list by hand is how items get missed.
    inspection_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspection_items.id", ondelete="SET NULL")
    )

    @property
    def is_unresolved(self) -> bool:
        return self.status in UNRESOLVED_PUNCH

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_date and self.is_unresolved and self.due_date < date.today())

    @property
    def days_overdue(self) -> int | None:
        if not self.is_overdue or not self.due_date:
            return None
        return (date.today() - self.due_date).days

    @property
    def blocks_handover(self) -> bool:
        """Whether this one alone is enough to stop the keys changing hands.

        Cosmetic items do not; a buyer will take the house and a decorator comes
        back. Anything above that does, because handing over a home with a major
        defect on the list is how a warranty claim becomes a dispute.
        """
        return self.is_unresolved and self.severity is not PunchSeverity.COSMETIC

    def __repr__(self) -> str:
        return f"<PunchItem {self.description[:30]} {self.status}>"
