"""Safety: observations, incidents, action plans and timecards.

Procore's best structural idea in this area is that a **failed inspection item
raises an observation**, and an observation is a tracked thing owed by someone
with a due date — not a note. That chain is what turns an inspection from a
record of a problem into a record of its resolution.

Timecards sit here because labour hours against a cost code are what make field
productivity measurable: budgeted hours versus hours actually burned.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ObservationType(StrEnum):
    SAFETY = "safety"
    QUALITY = "quality"
    COMMISSIONING = "commissioning"
    WORK_TO_COMPLETE = "work_to_complete"


class ObservationStatus(StrEnum):
    INITIATED = "initiated"
    READY_FOR_REVIEW = "ready_for_review"
    CLOSED = "closed"
    NOT_ACCEPTED = "not_accepted"  # the fix was inspected and failed


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentType(StrEnum):
    INJURY = "injury"
    NEAR_MISS = "near_miss"
    PROPERTY_DAMAGE = "property_damage"
    ENVIRONMENTAL = "environmental"
    ILLNESS = "illness"


class RiddorCategory(StrEnum):
    """What makes an incident reportable to the HSE, and how fast.

    The Reporting of Injuries, Diseases and Dangerous Occurrences Regulations are
    what decide this in the UK, not somebody's judgement on the day - which is why
    `reportable` being a free tick box was the wrong shape. The categories carry
    different deadlines and that is the whole point of naming them.
    """

    NOT_REPORTABLE = "not_reportable"
    FATALITY = "fatality"
    SPECIFIED_INJURY = "specified_injury"
    OVER_7_DAY = "over_7_day"
    OCCUPATIONAL_DISEASE = "occupational_disease"
    DANGEROUS_OCCURRENCE = "dangerous_occurrence"


#: Days from the incident to the deadline for the HSE report. An over-seven-day
#: incapacity gets fifteen because the seven days have to elapse before anybody
#: knows it qualifies; everything else is ten.
RIDDOR_DEADLINE_DAYS: dict[RiddorCategory, int] = {
    RiddorCategory.FATALITY: 10,
    RiddorCategory.SPECIFIED_INJURY: 10,
    RiddorCategory.OVER_7_DAY: 15,
    RiddorCategory.OCCUPATIONAL_DISEASE: 10,
    RiddorCategory.DANGEROUS_OCCURRENCE: 10,
}

#: The ones that must also be notified without delay, by telephone, before the
#: written report. Recorded so the screen can say so rather than implying that
#: ten days is the only obligation.
RIDDOR_WITHOUT_DELAY = frozenset(
    {
        RiddorCategory.FATALITY,
        RiddorCategory.SPECIFIED_INJURY,
        RiddorCategory.DANGEROUS_OCCURRENCE,
    }
)


class ActionPlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETE = "complete"
    OVERDUE = "overdue"


# ======================================================= observations =====


class Observation(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Something seen on site that somebody now owes a fix for."""

    __tablename__ = "observations"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    location: Mapped[str | None] = mapped_column(String(200))

    observation_type: Mapped[ObservationType] = mapped_column(
        Enum(
            ObservationType,
            name="observation_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ObservationType.SAFETY,
        nullable=False,
        index=True,
    )
    status: Mapped[ObservationStatus] = mapped_column(
        Enum(
            ObservationStatus,
            name="observation_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ObservationStatus.INITIATED,
        nullable=False,
        index=True,
    )
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="observation_severity", values_callable=lambda e: [m.value for m in e]),
        default=Severity.MEDIUM,
        nullable=False,
        index=True,
    )

    # Where it came from. An observation raised by a failed inspection keeps the
    # link, so the inspection record shows what was done about it.
    inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspections.id", ondelete="SET NULL"), index=True
    )
    drawing_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drawings.id", ondelete="SET NULL")
    )

    assigned_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    raised_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    due_on: Mapped[date | None] = mapped_column(Date, index=True)
    closed_on: Mapped[date | None] = mapped_column(Date)
    resolution: Mapped[str | None] = mapped_column(String(2000))

    # Who accepted the fix, which is not the same person as whoever did it. An
    # observation that closed on the word of the trade who owed it was the same
    # hole the punch list had.
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_open(self) -> bool:
        """Still owed. Ready for review counts: somebody has claimed a fix and
        nobody has looked at it, which is not the same as done."""
        return self.status is not ObservationStatus.CLOSED

    @property
    def is_overdue(self) -> bool:
        open_states = (ObservationStatus.INITIATED, ObservationStatus.NOT_ACCEPTED)
        return bool(self.due_on and self.status in open_states and self.due_on < date.today())

    @property
    def days_overdue(self) -> int | None:
        if not self.is_overdue or self.due_on is None:
            return None
        return (date.today() - self.due_on).days

    def __repr__(self) -> str:
        return f"<Observation {self.title[:30]} {self.status}>"


# ========================================================== incidents =====


class Incident(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """An injury, illness, near miss or damage event.

    Near misses are recorded on purpose. They are the cheapest possible data
    about what is about to go wrong.
    """

    __tablename__ = "incidents"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    incident_type: Mapped[IncidentType] = mapped_column(
        Enum(IncidentType, name="incident_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="observation_severity", values_callable=lambda e: [m.value for m in e]),
        default=Severity.MEDIUM,
        nullable=False,
        index=True,
    )

    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000))
    location: Mapped[str | None] = mapped_column(String(200))

    person_involved: Mapped[str | None] = mapped_column(String(200))
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )

    # These three drive statutory reporting.
    lost_time: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    medical_treatment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reportable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Which RIDDOR category, and whether the report has actually gone in. The tick
    # box said an incident ought to be reported; neither of those said whether
    # anybody did it, which is the only part the HSE cares about.
    riddor_category: Mapped[RiddorCategory] = mapped_column(
        Enum(
            RiddorCategory,
            name="riddor_category",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=RiddorCategory.NOT_REPORTABLE,
        nullable=False,
        index=True,
    )
    reported_to_hse_on: Mapped[date | None] = mapped_column(Date)
    hse_reference: Mapped[str | None] = mapped_column(String(100))

    immediate_action: Mapped[str | None] = mapped_column(String(2000))
    root_cause: Mapped[str | None] = mapped_column(String(2000))
    reported_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_on: Mapped[date | None] = mapped_column(Date)

    @property
    def riddor_deadline(self) -> date | None:
        """The day the written report is due with the HSE."""
        days = RIDDOR_DEADLINE_DAYS.get(self.riddor_category)
        return self.occurred_on + timedelta(days=days) if days else None

    @property
    def report_without_delay(self) -> bool:
        """Some categories must be notified by telephone before the written report,
        so "ten days" would be a dangerously reassuring thing to show alone."""
        return self.riddor_category in RIDDOR_WITHOUT_DELAY

    @property
    def report_overdue(self) -> bool:
        deadline = self.riddor_deadline
        return bool(deadline and not self.reported_to_hse_on and deadline < date.today())

    @property
    def days_to_report(self) -> int | None:
        """Negative once the deadline has passed. None when nothing is owed."""
        deadline = self.riddor_deadline
        if deadline is None or self.reported_to_hse_on:
            return None
        return (deadline - date.today()).days

    @property
    def is_open(self) -> bool:
        return self.closed_on is None


# ======================================================= action plans =====


class ActionPlan(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A named set of steps that must be signed off, in order.

    Used for the things a builder does the same way on every home: a pre-pour
    checklist, a pre-drywall walk, a handover sequence.
    """

    __tablename__ = "action_plans"

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[ActionPlanStatus] = mapped_column(
        Enum(
            ActionPlanStatus,
            name="action_plan_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ActionPlanStatus.DRAFT,
        nullable=False,
        index=True,
    )
    due_on: Mapped[date | None] = mapped_column(Date)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # A template has no job and is copied onto one when it is started.
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    # Which template this plan was started from. Without it the sentence "copied
    # onto a job when it is started" was true of nothing that could be looked up:
    # you could not ask which homes had had the pre-pour checklist run.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plans.id", ondelete="SET NULL"), index=True
    )
    started_on: Mapped[date | None] = mapped_column(Date)

    items: Mapped[list["ActionPlanItem"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ActionPlanItem.sequence",
        foreign_keys="ActionPlanItem.plan_id",
    )

    @property
    def completed_items(self) -> int:
        """Steps somebody has marked done, whether or not they have been witnessed."""
        return sum(1 for i in self.items if i.completed_on)

    @property
    def done_items(self) -> int:
        """Steps that are actually finished - witnessed, where a witness is required."""
        return sum(1 for i in self.items if i.is_done)

    @property
    def percent_complete(self) -> int:
        """Measured on finished steps, not merely claimed ones.

        Counting a step that is waiting on its witness would show a plan at 100%
        beside a status of active, and the reader has to decide which of the two
        to believe.
        """
        return round(self.done_items / len(self.items) * 100) if self.items else 0

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_on
            and self.status is not ActionPlanStatus.COMPLETE
            and self.due_on < date.today()
        )

    @property
    def next_item(self) -> "ActionPlanItem | None":
        """The step the plan is waiting on.

        These run in order, so "what is outstanding" is one step, not a list. A
        pre-pour checklist that lets you sign the pour off before the rebar check
        is a form, not a checklist.
        """
        return next((i for i in self.items if not i.completed_on), None)


class ActionPlanItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "action_plan_items"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("action_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    # A step that has to be signed off by someone other than whoever did it.
    requires_signoff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    due_on: Mapped[date | None] = mapped_column(Date)
    completed_on: Mapped[date | None] = mapped_column(Date)
    # Two people, deliberately. The comment on requires_signoff above says the
    # sign-off is by someone other than whoever did the work, and with a single
    # column that could not be checked - so the code set both to the same user and
    # the rule existed only as a sentence.
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    signed_off_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    signed_off_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(1000))

    plan: Mapped[ActionPlan] = relationship(back_populates="items", foreign_keys=[plan_id])

    @property
    def awaiting_signoff(self) -> bool:
        return bool(self.requires_signoff and self.completed_on and not self.signed_off_on)

    @property
    def is_done(self) -> bool:
        """Done means signed off where a sign-off is required."""
        if not self.completed_on:
            return False
        return bool(self.signed_off_on) if self.requires_signoff else True


# ========================================================== timecards =====


class Timecard(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Labour hours against a job and a cost code.

    Hours against a cost code are what make field productivity measurable: the
    budget says a house should take so many framing hours, and this says what it
    actually took.
    """

    __tablename__ = "timecards"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL"), index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_tasks.id", ondelete="SET NULL")
    )

    worked_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    worker_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    overtime_hours: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), nullable=False
    )
    hourly_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(String(500))

    entered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_on: Mapped[date | None] = mapped_column(Date, index=True)
    # Who approved it. The date existed with nothing to set it and nobody
    # attached, which makes an approval a fact with no author.
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def total_hours(self) -> Decimal:
        return self.hours + self.overtime_hours

    @property
    def is_approved(self) -> bool:
        return self.approved_on is not None

    @property
    def labour_cost(self) -> Decimal | None:
        """Overtime at time and a half, which is the common default.

        None, not zero, when no rate is known. Zero would be a claim that the work
        was free, and it is the claim that quietly drags an average rate down: the
        hours still count, so the same money is divided over more of them.
        """
        if self.hourly_rate is None:
            return None
        return self.hours * self.hourly_rate + self.overtime_hours * self.hourly_rate * Decimal(
            "1.5"
        )
