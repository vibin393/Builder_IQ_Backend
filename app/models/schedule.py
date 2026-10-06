"""Construction scheduling: templates, job schedules, tasks.

A production builder does not schedule each house by hand. A ScheduleTemplate holds the
sequence once (with offsets and durations in days), and every job generates its own task
list from it. Completing a task early or late shifts everything downstream that depends
on it, which is how a real construction schedule behaves.
"""

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class TaskStatus(StrEnum):
    NOT_STARTED = "not_started"
    READY = "ready"            # predecessors complete, trade can start
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class Holiday(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A day this builder does not work, beyond weekends.

    Per builder rather than compiled in, because a builder in Dorset stops for the
    August bank holiday and one in Texas does not. Durations in the schedule are
    working days, so these dates stretch the calendar around every task.
    """

    __tablename__ = "schedule_holidays"
    __table_args__ = (
        UniqueConstraint("tenant_id", "on_date", name="uq_schedule_holidays_tenant_date"),
    )

    on_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    def __repr__(self) -> str:
        return f"<Holiday {self.on_date} {self.name}>"


class ScheduleTemplate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "schedule_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_schedule_templates_tenant_code"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional: a template specific to one plan. Null means it applies to any plan.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tasks: Mapped[list["ScheduleTemplateTask"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<ScheduleTemplate {self.code}>"


class ScheduleTemplateTask(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One step in the standard build sequence.

    `offset_days` is from the job start; `duration_days` is how long the trade needs.
    Kept as simple offsets rather than a dependency graph on purpose - a graph is the
    right long-term model, but offsets are correct for a production builder's repeatable
    sequence and far easier to configure.
    """

    __tablename__ = "schedule_template_tasks"

    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schedule_templates.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    offset_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    template: Mapped[ScheduleTemplate] = relationship(back_populates="tasks")


class JobTask(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A scheduled task on one specific house."""

    __tablename__ = "job_tasks"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", values_callable=lambda e: [m.value for m in e]),
        default=TaskStatus.NOT_STARTED,
        nullable=False,
        index=True,
    )

    scheduled_start: Mapped[date | None] = mapped_column(Date, index=True)
    scheduled_finish: Mapped[date | None] = mapped_column(Date)
    actual_start: Mapped[date | None] = mapped_column(Date)
    actual_finish: Mapped[date | None] = mapped_column(Date)

    duration_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    # The trade responsible. Drives what a vendor sees in the trade portal.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
    #: Why the trade cannot start. `BLOCKED` existed as a status with nothing able
    #: to set it, so a superintendent had no way to record that the plot was
    #: waterlogged or the materials had not landed - and a schedule that cannot
    #: say why it stopped is a schedule nobody trusts.
    blocked_reason: Mapped[str | None] = mapped_column(String(500))

    @property
    def days_late(self) -> int:
        """Positive when the task finished later than scheduled.

        Calendar days, deliberately: this reports what happened, and a week of
        delay is a week whether or not somebody was going to work the weekend.
        Durations are planned in working days; slippage is felt in real ones.
        """
        if self.actual_finish and self.scheduled_finish:
            return (self.actual_finish - self.scheduled_finish).days
        return 0

    @property
    def is_overdue(self) -> bool:
        """Should have finished and has not.

        What the behind-schedule list is made of, and it becomes true on the day
        rather than when somebody runs a report.
        """
        return bool(
            self.scheduled_finish
            and self.status
            not in (TaskStatus.COMPLETE, TaskStatus.SKIPPED)
            and self.scheduled_finish < date.today()
        )

    @property
    def days_overdue(self) -> int | None:
        if not self.is_overdue or not self.scheduled_finish:
            return None
        return (date.today() - self.scheduled_finish).days

    @property
    def is_open(self) -> bool:
        return self.status not in (TaskStatus.COMPLETE, TaskStatus.SKIPPED)

    def __repr__(self) -> str:
        return f"<JobTask {self.sequence} {self.name} ({self.status})>"
