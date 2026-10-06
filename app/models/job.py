"""The Job is the home: the spine record every other module hangs off.

One Job = one house on one lot, built to one plan, for one buyer. Money, time,
scope and people all attach here, which is what keeps sales, construction and
warranty describing the same house.

Why the extra dates
-------------------
A builder's whole business is measured in the gaps between them:

  * **sold → start** is how long a signed contract sat waiting for a permit or a
    slot in the schedule. It is the number a sales director argues about.
  * **start → complete** is cycle time, the single figure a homebuilder is judged
    on. It cannot be derived from the schedule, because a schedule is a plan and
    this is what happened.
  * **projected vs actual completion** is slip, and it is only meaningful if the
    projection is frozen rather than quietly moved each time it is missed.

Holds are a first-class state rather than a note, because "under construction but
stopped" and "under construction" are different to everyone who reads this record,
and a build that is halted for three weeks with nobody recording why is how a
schedule becomes fiction.
"""

import uuid
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.selection import Selection


class JobStatus(StrEnum):
    PLANNED = "planned"          # lot + plan assigned, not sold
    SOLD = "sold"                # contract signed
    PERMITTING = "permitting"
    UNDER_CONSTRUCTION = "under_construction"
    ON_HOLD = "on_hold"          # started, then stopped, with a reason
    COMPLETE = "complete"        # built, not yet closed
    CLOSED = "closed"            # title transferred to the buyer
    WARRANTY = "warranty"
    CANCELLED = "cancelled"


#: A home still being worked. Everything else is finished or abandoned.
LIVE_STATUSES = (
    JobStatus.PLANNED,
    JobStatus.SOLD,
    JobStatus.PERMITTING,
    JobStatus.UNDER_CONSTRUCTION,
    JobStatus.ON_HOLD,
    JobStatus.COMPLETE,
)

#: Physically being built. What "how many homes are we running" means.
BUILDING_STATUSES = (
    JobStatus.UNDER_CONSTRUCTION,
    JobStatus.ON_HOLD,
)

#: Sold to somebody. Used to decide whether cancelling needs the contract undone.
SOLD_STATUSES = (
    JobStatus.SOLD,
    JobStatus.PERMITTING,
    JobStatus.UNDER_CONSTRUCTION,
    JobStatus.ON_HOLD,
    JobStatus.COMPLETE,
    JobStatus.CLOSED,
    JobStatus.WARRANTY,
)


class HoldReason(StrEnum):
    """Why a build stopped. A closed list, because the point is to count them.

    "Other" exists and requires a note - a free-text reason nobody can group is
    the same as no reason when somebody asks which of these is costing the most
    days this year.
    """

    WEATHER = "weather"
    INSPECTION = "inspection"
    BUYER_DECISION = "buyer_decision"       # waiting on a selection or a change
    TRADE_UNAVAILABLE = "trade_unavailable"
    MATERIAL_SHORTAGE = "material_shortage"
    PERMIT = "permit"
    FINANCE = "finance"
    OTHER = "other"


class CancellationReason(StrEnum):
    BUYER_WITHDREW = "buyer_withdrew"
    FINANCE_FELL_THROUGH = "finance_fell_through"
    BUILDER_WITHDREW = "builder_withdrew"
    LOT_PROBLEM = "lot_problem"             # survey, ground conditions, access
    PLAN_CHANGED = "plan_changed"           # rebuilt as a different job
    OTHER = "other"


class Job(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_number", name="uq_jobs_tenant_number"),
        # A lot holds at most one *live* job. This was a plain UNIQUE(lot_id),
        # which meant a cancelled job blocked its own lot permanently - the
        # builder could never rebuild on it, which is precisely what happens after
        # a buyer withdraws. A partial index is the fix: cancelled rows drop out
        # of it and the lot is free again.
        # Partial, and spelled for both dialects: PostgreSQL runs this in
        # production and SQLite runs the test suite, and both support partial
        # indexes. Without the sqlite_where the tests would get a plain unique
        # index and the rebuild-after-cancellation case would fail only there.
        Index(
            "uq_jobs_live_lot",
            "lot_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
            sqlite_where=text("status <> 'cancelled'"),
        ),
    )

    job_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    community_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("lots.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: Which elevation of the plan. The same plan in brick and in render is the
    #: same house to the schedule and a different one to the buyer and the budget.
    elevation: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PLANNED,
        nullable=False,
        index=True,
    )
    superintendent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # -- permits --------------------------------------------------------------
    # Held on the job rather than as documents, because "which of my homes are
    # waiting on a permit, and for how long" is asked weekly and must not require
    # opening anything.
    permit_number: Mapped[str | None] = mapped_column(String(64), index=True)
    permit_applied_on: Mapped[date | None] = mapped_column(Date)
    permit_issued_on: Mapped[date | None] = mapped_column(Date)

    # -- the dates the business is measured by --------------------------------
    sold_date: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)
    #: Frozen when the build starts. Moving it every time it slips destroys the
    #: only baseline anybody could measure slip against.
    projected_completion: Mapped[date | None] = mapped_column(Date)
    #: What the schedule currently expects, as opposed to what was promised.
    #: The schedule moves this every time a task slips; `projected_completion`
    #: above never moves, because a baseline that follows the slip measures
    #: nothing. "Three weeks behind" is the gap between these two.
    forecast_completion: Mapped[date | None] = mapped_column(Date)
    actual_completion: Mapped[date | None] = mapped_column(Date)
    closing_date: Mapped[date | None] = mapped_column(Date)
    warranty_expires_on: Mapped[date | None] = mapped_column(Date)

    # -- being stopped --------------------------------------------------------
    hold_reason: Mapped[HoldReason | None] = mapped_column(
        Enum(HoldReason, name="hold_reason", values_callable=lambda e: [m.value for m in e])
    )
    hold_note: Mapped[str | None] = mapped_column(String(500))
    on_hold_since: Mapped[date | None] = mapped_column(Date)
    #: Days lost to holds across the whole build, accumulated each time one is
    #: lifted. Kept as a running total so cycle time can be read net of stoppages
    #: without replaying the history.
    days_on_hold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- being abandoned ------------------------------------------------------
    cancelled_on: Mapped[date | None] = mapped_column(Date)
    cancellation_reason: Mapped[CancellationReason | None] = mapped_column(
        Enum(
            CancellationReason,
            name="cancellation_reason",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    cancellation_note: Mapped[str | None] = mapped_column(String(500))

    notes: Mapped[str | None] = mapped_column(Text)

    selections: Mapped[list["Selection"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    # -- derived --------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_building(self) -> bool:
        return self.status in BUILDING_STATUSES

    @property
    def is_sold(self) -> bool:
        return self.status in SOLD_STATUSES

    @property
    def cycle_time_days(self) -> int | None:
        """Start to completion. The figure a homebuilder is judged on.

        None until the home is finished: a part-built house has no cycle time, and
        reporting days-so-far under the same name would poison the average.
        """
        if not self.start_date or not self.actual_completion:
            return None
        return (self.actual_completion - self.start_date).days

    @property
    def days_building(self) -> int | None:
        """How long this one has been on site so far. For homes still going."""
        if not self.start_date or self.actual_completion:
            return None
        return (date.today() - self.start_date).days

    @property
    def forecast_slip(self) -> int | None:
        """How far the schedule has drifted from what was promised.

        Distinct from `days_late`, which compares against today or the actual
        finish. This is the number a superintendent can act on before the date
        arrives: the schedule already knows it will be late, and says so.
        """
        if not self.projected_completion or not self.forecast_completion:
            return None
        return (self.forecast_completion - self.projected_completion).days

    @property
    def days_late(self) -> int | None:
        """Against the frozen projection. Negative means early.

        Measured to the actual completion once finished, and to today while it is
        still running - a home three weeks past its date is late now, not late
        when somebody finally finishes it.
        """
        if not self.projected_completion:
            return None
        if self.status in (JobStatus.CANCELLED,):
            return None
        end = self.actual_completion or date.today()
        return (end - self.projected_completion).days

    @property
    def is_late(self) -> bool:
        late = self.days_late
        return late is not None and late > 0

    @property
    def days_since_hold(self) -> int | None:
        if self.status is not JobStatus.ON_HOLD or not self.on_hold_since:
            return None
        return (date.today() - self.on_hold_since).days

    @property
    def days_waiting_on_permit(self) -> int | None:
        """Applied but not issued. The queue nobody watches until it bites."""
        if not self.permit_applied_on or self.permit_issued_on:
            return None
        return (date.today() - self.permit_applied_on).days

    def __repr__(self) -> str:
        return f"<Job {self.job_number} ({self.status})>"
