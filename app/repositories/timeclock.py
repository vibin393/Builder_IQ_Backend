"""Data access for the time clock."""

import uuid

from sqlalchemy import func

from app.models.timeclock import Shift, ShiftStatus
from app.repositories.base import TenantRepository


class ShiftRepository(TenantRepository[Shift]):
    model = Shift
    label = "Shift"
    search_fields = ("worker_name", "notes")
    default_loads = ("job",)

    async def active_for_worker(
        self, *, user_id: uuid.UUID | None, worker_name: str
    ) -> Shift | None:
        """The open shift this worker already has, if any — the thing a second
        clock-in must not create a duplicate of."""
        stmt = self._base().where(Shift.status == ShiftStatus.ACTIVE)
        if user_id is not None:
            stmt = stmt.where(Shift.user_id == user_id)
        else:
            stmt = stmt.where(
                func.lower(Shift.worker_name) == worker_name.strip().lower(),
                Shift.user_id.is_(None),
            )
        return await self.db.scalar(stmt)

    async def active(self, job_id: uuid.UUID | None = None) -> list[Shift]:
        stmt = self._base().where(Shift.status == ShiftStatus.ACTIVE)
        if job_id is not None:
            stmt = stmt.where(Shift.job_id == job_id)
        return list(await self.db.scalars(stmt.order_by(Shift.clock_in_at.desc())))
