"""Time clock: shifts, the live board, and the employee locator.

The one rule that matters here: a worker has at most one shift open at a time.
Everything else is bookkeeping around that — clock in opens it, clock out closes
it, and a second clock-in while one is open is the mistake the service exists to
refuse.
"""

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.timeclock import OutOfBoundsStatus, Shift, ShiftStatus
from app.repositories.job import JobRepository
from app.repositories.timeclock import ShiftRepository
from app.schemas.common import PageParams


class TimeClockService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.shifts = ShiftRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)

    # -- reads ---------------------------------------------------------------

    async def list_shifts(self, params: PageParams, **filters):
        return await self.shifts.paginate(params, order_by=Shift.clock_in_at.desc(), **filters)

    async def get(self, shift_id: uuid.UUID) -> Shift:
        return await self.shifts.get(shift_id)

    async def active(self, job_id: uuid.UUID | None = None) -> list[Shift]:
        """Who is on the clock right now."""
        return await self.shifts.active(job_id)

    async def map_points(self, job_id: uuid.UUID | None = None) -> list[Shift]:
        """Active shifts that carry a clock-in location — the locator pins."""
        return [
            s
            for s in await self.shifts.active(job_id)
            if s.clock_in_lat is not None and s.clock_in_lng is not None
        ]

    async def summary(self, job_id: uuid.UUID | None = None) -> dict:
        start_of_today = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)

        base = select(Shift).where(Shift.tenant_id == self.tenant_id)
        if job_id is not None:
            base = base.where(Shift.job_id == job_id)

        on_clock = await self.db.scalar(
            select(func.count()).select_from(
                base.where(Shift.status == ShiftStatus.ACTIVE).subquery()
            )
        )
        pending = await self.db.scalar(
            select(func.count()).select_from(
                base.where(
                    Shift.status == ShiftStatus.COMPLETED, Shift.approved_at.is_(None)
                ).subquery()
            )
        )
        oob = await self.db.scalar(
            select(func.count()).select_from(
                base.where(Shift.out_of_bounds == OutOfBoundsStatus.OUT_OF_BOUNDS).subquery()
            )
        )
        workers_today = await self.db.scalar(
            select(func.count(func.distinct(Shift.worker_name))).select_from(
                base.where(Shift.clock_in_at >= start_of_today).subquery()
            )
        )
        # Hours today — sum durations in Python so a running shift counts up to now.
        today_shifts = list(
            await self.db.scalars(
                (base.where(Shift.clock_in_at >= start_of_today)).where(
                    Shift.status != ShiftStatus.CANCELLED
                )
            )
        )
        hours_today = sum((s.total_hours for s in today_shifts), start=Decimal("0"))

        return {
            "job_id": job_id,
            "on_the_clock": on_clock or 0,
            "workers_today": workers_today or 0,
            "hours_today": hours_today,
            "pending_approval": pending or 0,
            "out_of_bounds": oob or 0,
        }

    # -- writes --------------------------------------------------------------

    async def clock_in(self, *, actor_id: uuid.UUID | None = None, **data) -> Shift:
        await self.jobs.require_exists(data["job_id"])
        existing = await self.shifts.active_for_worker(
            user_id=data.get("user_id"), worker_name=data["worker_name"]
        )
        if existing is not None:
            raise BusinessRuleViolation(
                f"{data['worker_name']} is already clocked in. Clock out first."
            )
        lat = data.pop("lat", None)
        lng = data.pop("lng", None)
        return await self.shifts.create(
            status=ShiftStatus.ACTIVE,
            clock_in_at=datetime.now(UTC),
            clock_in_lat=lat,
            clock_in_lng=lng,
            entered_by_id=actor_id,
            **data,
        )

    async def clock_out(self, shift_id: uuid.UUID, *, lat=None, lng=None) -> Shift:
        shift = await self.shifts.get(shift_id)
        if shift.status is not ShiftStatus.ACTIVE:
            raise BusinessRuleViolation("That shift is not open.")
        shift.clock_out_at = datetime.now(UTC)
        shift.clock_out_lat = lat
        shift.clock_out_lng = lng
        shift.status = ShiftStatus.COMPLETED
        await self.db.commit()
        return await self.shifts.get(shift_id)

    async def create_manual(self, *, actor_id: uuid.UUID | None = None, **data) -> Shift:
        await self.jobs.require_exists(data["job_id"])
        clock_out = data.get("clock_out_at")
        if clock_out is not None and clock_out <= data["clock_in_at"]:
            raise BusinessRuleViolation("Clock-out must be after clock-in.")
        status = ShiftStatus.COMPLETED if clock_out else ShiftStatus.ACTIVE
        return await self.shifts.create(status=status, entered_by_id=actor_id, **data)

    async def amend(self, shift_id: uuid.UUID, **data) -> Shift:
        shift = await self.shifts.get(shift_id)
        if shift.is_approved:
            raise BusinessRuleViolation(
                "Approved shifts cannot be edited. Withdraw the approval first."
            )
        # Keep status honest with the clock-out time if it is changed.
        if "clock_out_at" in data:
            co = data["clock_out_at"]
            if co is not None and co <= (data.get("clock_in_at") or shift.clock_in_at):
                raise BusinessRuleViolation("Clock-out must be after clock-in.")
            shift.status = ShiftStatus.COMPLETED if co else ShiftStatus.ACTIVE
        for field, value in data.items():
            setattr(shift, field, value)
        await self.db.commit()
        return await self.shifts.get(shift_id)

    async def delete(self, shift_id: uuid.UUID) -> None:
        shift = await self.shifts.get(shift_id)
        if shift.is_approved:
            raise BusinessRuleViolation(
                "Approved shifts cannot be deleted. Withdraw the approval first."
            )
        await self.db.delete(shift)
        await self.db.commit()

    async def approve(self, shift_id: uuid.UUID, *, actor_id: uuid.UUID) -> Shift:
        shift = await self.shifts.get(shift_id)
        if shift.status is not ShiftStatus.COMPLETED:
            raise BusinessRuleViolation("Only a completed shift can be approved.")
        shift.approved_at = datetime.now(UTC)
        shift.approved_by_id = actor_id
        await self.db.commit()
        return await self.shifts.get(shift_id)

    async def unapprove(self, shift_id: uuid.UUID) -> Shift:
        shift = await self.shifts.get(shift_id)
        shift.approved_at = None
        shift.approved_by_id = None
        await self.db.commit()
        return await self.shifts.get(shift_id)
