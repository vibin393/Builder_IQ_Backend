"""Time clock: employee shifts with clock-in/out and location.

Distinct from `Timecard` (app/models/safety.py) on purpose. A timecard is a *claim*
about hours for payroll and job cost — entered after the fact, approved, costed. A
shift is a *fact* about attendance — when a worker clocked in, where they were when
they did, and when they clocked out. One is the money layer; the other is the
presence layer, and the presence layer is what "who is on site right now" and the
employee locator map are built from.

A completed, approved shift can feed a timecard, but they are not the same row: a
worker can be clocked in with no timecard yet, and a timecard can exist for work
that predates the clock.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ShiftStatus(StrEnum):
    ACTIVE = "active"  # clocked in, not yet clocked out — on the clock now
    COMPLETED = "completed"  # clocked out
    CANCELLED = "cancelled"  # voided; never counts


class ShiftType(StrEnum):
    REGULAR = "regular"
    OVERTIME = "overtime"
    BREAK = "break"
    PTO = "pto"  # paid time off / holiday


class OutOfBoundsStatus(StrEnum):
    """Whether the clock-in happened where the work is.

    UNKNOWN, not a boolean, because "we did not check" is a real third answer: the
    device may not have granted location, and recording that as 'in bounds' would be
    a claim nobody made.
    """

    UNKNOWN = "unknown"
    IN_BOUNDS = "in_bounds"
    OUT_OF_BOUNDS = "out_of_bounds"


class Shift(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "shifts"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The employee. Nullable so a sub with no login can still be clocked in by name.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Denormalised display name — always set, so the row reads without a join and
    #: survives the user being removed.
    worker_name: Mapped[str] = mapped_column(String(200), nullable=False)
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[ShiftStatus] = mapped_column(
        Enum(ShiftStatus, name="shift_status", values_callable=lambda e: [m.value for m in e]),
        default=ShiftStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    shift_type: Mapped[ShiftType] = mapped_column(
        Enum(ShiftType, name="shift_type", values_callable=lambda e: [m.value for m in e]),
        default=ShiftType.REGULAR,
        nullable=False,
    )

    clock_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the worker was when they clocked in and out. Degrees, WGS84.
    clock_in_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    clock_in_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    clock_out_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    clock_out_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    out_of_bounds: Mapped[OutOfBoundsStatus] = mapped_column(
        Enum(
            OutOfBoundsStatus,
            name="out_of_bounds_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OutOfBoundsStatus.UNKNOWN,
        nullable=False,
    )

    tags: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(String(1000))

    # Approval — the shift's own sign-off, the same shape as a timecard's.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    invoiced_to_client: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    entered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    job: Mapped["Job"] = relationship()  # noqa: F821

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @property
    def is_active(self) -> bool:
        return self.status is ShiftStatus.ACTIVE and self.clock_out_at is None

    @property
    def duration_minutes(self) -> int:
        """Minutes worked. Up to now for a shift still running, so a live board can
        show a climbing total rather than a blank."""
        start = self._aware(self.clock_in_at)
        if start is None:
            return 0
        end = self._aware(self.clock_out_at) or datetime.now(UTC)
        return max(0, int((end - start).total_seconds() // 60))

    @property
    def total_hours(self) -> Decimal:
        return (Decimal(self.duration_minutes) / Decimal(60)).quantize(Decimal("0.01"))

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None

    def __repr__(self) -> str:
        return f"<Shift {self.worker_name} {self.status} job={self.job_id}>"
