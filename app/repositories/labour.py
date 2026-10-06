"""Data access for labour."""

import uuid
from datetime import date

from sqlalchemy import func, select

from app.models.safety import Timecard
from app.repositories.base import TenantRepository


class TimecardRepository(TenantRepository[Timecard]):
    model = Timecard
    label = "Timecard"
    search_fields = ("worker_name", "notes")

    async def for_worker_day(
        self, *, worker_name: str, worked_on: date, exclude: uuid.UUID | None = None
    ) -> list[Timecard]:
        """Everything already booked for one person on one day, across every home.

        Across every home on purpose. A day that adds up to twenty-six hours is
        still impossible when the hours are split over two houses, and that is the
        shape the mistake usually takes - the same shift entered twice because
        nobody could see the first entry from the second house's screen.
        """
        stmt = self._base().where(
            func.lower(Timecard.worker_name) == worker_name.strip().lower(),
            Timecard.worked_on == worked_on,
        )
        if exclude is not None:
            stmt = stmt.where(Timecard.id != exclude)
        return list(await self.db.scalars(stmt))

    async def for_scope(
        self, *, job_id: uuid.UUID | None = None, approved: bool | None = None
    ) -> list[Timecard]:
        """Every matching card, unpaged, for the rollups.

        The reports are about all the hours, not the page of them on screen.
        """
        stmt = self._base()
        if job_id is not None:
            stmt = stmt.where(Timecard.job_id == job_id)
        if approved is True:
            stmt = stmt.where(Timecard.approved_on.is_not(None))
        elif approved is False:
            stmt = stmt.where(Timecard.approved_on.is_(None))
        return list(await self.db.scalars(stmt))

    async def counts(self, job_id: uuid.UUID | None = None) -> dict[str, int]:
        stmt = select(
            func.count().label("entries"),
            func.count().filter(Timecard.approved_on.is_(None)).label("pending"),
            func.count().filter(Timecard.hourly_rate.is_(None)).label("unrated"),
            func.count(func.distinct(Timecard.worker_name)).label("workers"),
        ).where(Timecard.tenant_id == self.tenant_id)
        if job_id is not None:
            stmt = stmt.where(Timecard.job_id == job_id)
        row = (await self.db.execute(stmt)).one()
        return {
            "entries": row.entries or 0,
            "pending_approval": row.pending or 0,
            "unrated_entries": row.unrated or 0,
            "workers": row.workers or 0,
        }
