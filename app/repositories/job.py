"""Data access for homes and the records that hang off them."""

import uuid

from sqlalchemy import func, select

from app.models.budget import Budget, BudgetLine
from app.models.contract import Buyer, Contract
from app.models.job import BUILDING_STATUSES, LIVE_STATUSES, Job, JobStatus
from app.models.selection import Selection
from app.repositories.base import TenantRepository


class JobRepository(TenantRepository[Job]):
    model = Job
    label = "Home"
    #: A home is hunted for by its number, its permit, or the plot it sits on -
    #: and the plot is on another table, so lot number is handled separately in
    #: `page` rather than pretended to be a column here.
    search_fields = ("job_number", "permit_number", "elevation")

    async def all_ordered(self) -> list[Job]:
        return await self.list(order_by=Job.job_number)

    async def page(self, params, **filters):
        """Newest first. A home list is read from the top - the one started this
        week is the one somebody is asking about."""
        return await self.paginate(params, order_by=Job.job_number.desc(), **filters)

    async def page_in_statuses(self, params, statuses, **filters):
        return await self._page_with(params, Job.status.in_(statuses), **filters)

    async def page_late(self, params, **filters):
        """Homes past their frozen projection, worst first.

        Cancelled ones are excluded: a home nobody is building cannot be late.
        Completed ones are kept, because a home that finished three weeks late is
        exactly what a post-mortem is looking for.
        """
        return await self._page_with(
            params,
            Job.projected_completion.is_not(None),
            Job.status.notin_((JobStatus.CANCELLED,)),
            func.coalesce(Job.actual_completion, func.current_date()) > Job.projected_completion,
            order_by=Job.projected_completion.asc(),
            **filters,
        )

    async def _page_with(self, params, *conditions, order_by=None, **filters):
        """paginate(), plus conditions the base class deliberately cannot express.

        The base offers equality filters, which is all most features need. These
        queries need IN clauses and a date comparison, so they build on the
        tenant-scoped statement rather than around it.
        """
        stmt = self._filtered(**filters)
        stmt = self._searched(stmt, params.search)
        for condition in conditions:
            stmt = stmt.where(condition)

        total = await self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(order_by if order_by is not None else Job.job_number.desc())
        stmt = stmt.order_by(Job.id)
        rows = await self.db.scalars(stmt.offset(params.offset).limit(params.size))
        return list(rows), total

    async def live_on_lot(self, lot_id: uuid.UUID) -> Job | None:
        """The home occupying this plot, if any.

        Cancelled jobs do not count - that is the whole reason cancelling exists
        rather than deleting, and it is what the partial unique index enforces at
        the database level.
        """
        return await self.db.scalar(
            self._base().where(Job.lot_id == lot_id, Job.status != JobStatus.CANCELLED)
        )

    async def counts_for_dashboard(self) -> dict:
        """The numbers above the list.

        `late` and `on_hold` are the two that represent money burning: a home past
        its date and a home that has stopped.
        """
        base = [Job.tenant_id == self.tenant_id]

        async def n(*extra) -> int:
            return (
                await self.db.scalar(select(func.count()).select_from(Job).where(*base, *extra))
                or 0
            )

        by_status = {
            row[0].value: row[1]
            for row in await self.db.execute(
                select(Job.status, func.count(Job.id))
                .where(Job.tenant_id == self.tenant_id)
                .group_by(Job.status)
            )
        }
        return {
            "live": await n(Job.status.in_(LIVE_STATUSES)),
            "building": await n(Job.status.in_(BUILDING_STATUSES)),
            "on_hold": await n(Job.status == JobStatus.ON_HOLD),
            "late": await n(
                Job.projected_completion.is_not(None),
                Job.status.notin_((JobStatus.CANCELLED,)),
                func.coalesce(Job.actual_completion, func.current_date())
                > Job.projected_completion,
            ),
            "awaiting_permit": await n(
                Job.permit_applied_on.is_not(None), Job.permit_issued_on.is_(None)
            ),
            "by_status": {s.value: by_status.get(s.value, 0) for s in JobStatus},
        }

    async def for_community(self, community_id: uuid.UUID) -> list[Job]:
        return await self.list(community_id=community_id, order_by=Job.job_number)

    async def ids_for_community(self, community_id: uuid.UUID) -> list[uuid.UUID]:
        """Just the ids, for scoping other aggregates to one community."""
        from sqlalchemy import select

        return list(
            await self.db.scalars(
                select(Job.id).where(
                    Job.tenant_id == self.tenant_id, Job.community_id == community_id
                )
            )
        )

    async def next_number(self) -> str:
        from sqlalchemy import select

        last = await self.db.scalar(
            select(Job.job_number)
            .where(Job.tenant_id == self.tenant_id)
            .order_by(Job.job_number.desc())
            .limit(1)
        )
        nxt = (int(last.removeprefix("J")) + 1) if last else 1
        return f"J{nxt:05d}"


class ContractRepository(TenantRepository[Contract]):
    model = Contract
    label = "Contract"
    default_loads = ("buyer",)

    async def for_job(self, job_id: uuid.UUID) -> Contract | None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        return await self.db.scalar(
            select(Contract)
            .options(selectinload(Contract.buyer))
            .where(Contract.tenant_id == self.tenant_id, Contract.job_id == job_id)
        )


class BuyerRepository(TenantRepository[Buyer]):
    model = Buyer
    label = "Buyer"


class BudgetRepository(TenantRepository[Budget]):
    model = Budget
    label = "Budget"
    default_loads = ("lines",)

    async def for_job(self, job_id: uuid.UUID) -> Budget | None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        return await self.db.scalar(
            select(Budget)
            .options(selectinload(Budget.lines))
            .where(Budget.tenant_id == self.tenant_id, Budget.job_id == job_id)
        )


class BudgetLineRepository(TenantRepository[BudgetLine]):
    model = BudgetLine
    label = "Budget line"

    async def for_budget(self, budget_id: uuid.UUID) -> list[BudgetLine]:
        return await self.list(budget_id=budget_id)


class SelectionRepository(TenantRepository[Selection]):
    model = Selection
    label = "Selection"

    async def for_job(self, job_id: uuid.UUID) -> list[Selection]:
        return await self.list(job_id=job_id)
