"""Business tier for homes: the lifecycle of one house.

Until now a home's status was a field on a PATCH. Anybody could move a planned
house straight to closed, reopen a cancelled one, or mark a home complete that had
never started - and the dates that the whole business is measured by were set by
hand, so cycle time was whatever somebody typed.

The rules here exist because each one, left out, costs something real:

  * **a home cannot be started before it is sold.** Starting on spec is a
    deliberate decision with money behind it, not something that happens because
    a superintendent clicked the wrong row. It is allowed, but explicitly.
  * **a home cannot be completed before it is started.** A completion date earlier
    than a start date makes cycle time negative, and one negative number poisons
    the average everyone reports on.
  * **closing needs a signed contract and a completed house.** Title transfers
    once; doing it against an unbuilt home is not a data problem, it is a legal
    one.
  * **the projection is frozen when the build starts.** A projected completion
    that is quietly moved every time it slips measures nothing. Changing it later
    is possible and is recorded as a re-baseline.
  * **a hold needs a reason from a closed list.** The point of holds is to be able
    to say "we lost forty days to inspections this year". Free text cannot be
    counted.
  * **cancelling frees the lot.** That is the whole reason a builder cancels
    rather than deletes.

Nothing here imports FastAPI or writes SQL directly for its own sake; it raises
domain errors and the API tier decides what they mean in HTTP.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.community import LotStatus
from app.models.job import (
    BUILDING_STATUSES,
    LIVE_STATUSES,
    CancellationReason,
    HoldReason,
    Job,
    JobStatus,
)
from app.repositories.base import Conflict, NotFound
from app.repositories.catalog import CommunityRepository, LotRepository, PlanRepository
from app.repositories.job import ContractRepository, JobRepository
from app.schemas.common import PageParams

#: Where a home may go from where it is.
#:
#: Read down the left column: a planned home is sold or cancelled, and nothing
#: else. What is deliberately missing is every shortcut - planned straight to
#: under construction, sold straight to complete - because each of those skips a
#: step that exists to catch something. Permitting is optional and can be stepped
#: over, since not every jurisdiction and not every builder tracks it here.
ALLOWED: dict[JobStatus, tuple[JobStatus, ...]] = {
    # Straight to site is legal from planned, because that is what building on
    # spec is. It is not *free*: `start_build` refuses it without an explicit
    # flag. The distinction is deliberate - the table says which moves exist, the
    # service says which ones need somebody to mean it.
    JobStatus.PLANNED: (
        JobStatus.SOLD,
        JobStatus.PERMITTING,
        JobStatus.UNDER_CONSTRUCTION,
        JobStatus.CANCELLED,
    ),
    JobStatus.SOLD: (
        JobStatus.PERMITTING,
        JobStatus.UNDER_CONSTRUCTION,
        JobStatus.CANCELLED,
    ),
    JobStatus.PERMITTING: (JobStatus.UNDER_CONSTRUCTION, JobStatus.CANCELLED),
    JobStatus.UNDER_CONSTRUCTION: (
        JobStatus.ON_HOLD,
        JobStatus.COMPLETE,
        JobStatus.CANCELLED,
    ),
    JobStatus.ON_HOLD: (JobStatus.UNDER_CONSTRUCTION, JobStatus.CANCELLED),
    JobStatus.COMPLETE: (JobStatus.CLOSED, JobStatus.UNDER_CONSTRUCTION),
    # Closing is the handover. From there the only road is warranty.
    JobStatus.CLOSED: (JobStatus.WARRANTY,),
    JobStatus.WARRANTY: (),
    # Terminal, but the lot is free and a new job can be started on it.
    JobStatus.CANCELLED: (),
}

#: How long a new home is under warranty, in months. A default rather than a rule:
#: the figure is set per contract in most of the industry, and this is what gets
#: used when nobody says otherwise.
DEFAULT_WARRANTY_MONTHS = 24


class JobService:
    """Every operation on a home, scoped to one builder for its whole lifetime."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.jobs = JobRepository(db, tenant_id)
        self.contracts = ContractRepository(db, tenant_id)
        self.communities = CommunityRepository(db, tenant_id)
        self.lots = LotRepository(db, tenant_id)
        self.plans = PlanRepository(db, tenant_id)

    # ==================================================== reading homes =====

    async def list_jobs(self, params: PageParams | None = None, **filters):
        if params is None:
            return await self.jobs.all_ordered()
        return await self.jobs.page(params, **filters)

    async def list_live(self, params: PageParams, **filters):
        return await self.jobs.page_in_statuses(params, LIVE_STATUSES, **filters)

    async def list_building(self, params: PageParams, **filters):
        """Homes physically on site, held ones included - a stopped build is still
        occupying a plot, a superintendent and a budget."""
        return await self.jobs.page_in_statuses(params, BUILDING_STATUSES, **filters)

    async def list_late(self, params: PageParams, **filters):
        return await self.jobs.page_late(params, **filters)

    async def get_job(self, job_id: uuid.UUID) -> Job:
        return await self.jobs.get(job_id)

    async def summary(self) -> dict:
        """The numbers above the list."""
        return await self.jobs.counts_for_dashboard()

    # ===================================================== starting one =====

    async def create_job(self, *, actor_id: uuid.UUID | None = None, **data) -> Job:
        """Put a home on a lot.

        The lot and the plan must be this builder's, and the lot must be free.
        Free means "no live job on it" - a cancelled job leaves the plot available,
        which is the entire point of cancelling rather than deleting.
        """
        await self.communities.require_exists(data["community_id"])
        lot = await self.lots.require_exists(data["lot_id"])
        data["elevation"] = await self._check_plan(data["plan_id"], data.get("elevation"))

        if lot.community_id != data["community_id"]:
            raise BusinessRuleViolation(
                "That plot is not in the community you picked.",
                "Choose a plot from the same community, or change the community.",
            )

        existing = await self.jobs.live_on_lot(data["lot_id"])
        if existing is not None:
            raise Conflict(
                f"Plot {lot.lot_number} already has home {existing.job_number} on it."
            )

        if data.get("superintendent_id"):
            await self._require_colleague(data["superintendent_id"])

        data["job_number"] = await self.jobs.next_number()
        job = await self.jobs.create(**data)

        # The plot's *sales* position does not change: a home started with no buyer
        # is a speculative home, still for sale. This used to set the plot to "held",
        # which took every spec home off the market. The live job is what stops a
        # second home going on the plot.
        await self.db.commit()
        return await self.jobs.get(job.id)

    async def update_job(self, job_id: uuid.UUID, *, actor_id=None, **data) -> Job:
        """Correct a home's details.

        Status is not settable here. It moves through the lifecycle routes, which
        check the transition and set the dates that go with it; a PATCH that set
        status would route round every rule in this module.
        """
        job = await self.jobs.get(job_id)
        data.pop("status", None)

        # Re-baselining the projection is allowed and is worth being explicit
        # about - it is the number slip is measured against.
        if data.get("superintendent_id"):
            await self._require_colleague(data["superintendent_id"])
        if data.get("plan_id") or data.get("elevation"):
            plan_id = data.get("plan_id") or job.plan_id
            elevation = data["elevation"] if "elevation" in data else job.elevation
            checked = await self._check_plan(
                plan_id, elevation,
                new_home=bool(data.get("plan_id")) and data.get("plan_id") != job.plan_id,
            )
            if "elevation" in data or data.get("plan_id"):
                data["elevation"] = checked
        if data.get("plan_id") and data["plan_id"] != job.plan_id:
            if job.status not in (JobStatus.PLANNED, JobStatus.SOLD):
                raise BusinessRuleViolation(
                    f"{job.job_number} is already {job.status.value.replace('_', ' ')}; "
                    "its plan cannot be swapped.",
                    "Cancel this home and start a new one on the plot if the buyer "
                    "wants a different house.",
                )
        return await self.jobs.update(job, **data)

    async def assign_superintendent(
        self, job_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> Job:
        """Put a superintendent on the home, or take one off."""
        job = await self.jobs.get(job_id)
        if user_id:
            await self._require_colleague(user_id)
        job.superintendent_id = user_id
        await self.db.commit()
        return await self.jobs.get(job_id)

    # ===================================================== the lifecycle =====

    def _require_transition(self, job: Job, to: JobStatus) -> None:
        allowed = ALLOWED[job.status]
        if to in allowed:
            return
        if job.status is JobStatus.CANCELLED:
            raise BusinessRuleViolation(
                f"{job.job_number} was cancelled.",
                "Its plot is free - start a new home on it rather than reviving "
                "this record, so the cancellation stays on the books.",
            )
        if job.status is JobStatus.WARRANTY:
            raise BusinessRuleViolation(
                f"{job.job_number} is in warranty and its build is finished.",
                "Raise a warranty request instead.",
            )
        raise BusinessRuleViolation(
            f"{job.job_number} cannot go from {job.status.value.replace('_', ' ')} "
            f"to {to.value.replace('_', ' ')}.",
            "Next steps from here: "
            + (", ".join(s.value.replace("_", " ") for s in allowed) or "none")
            + ".",
        )

    async def mark_sold(self, job_id: uuid.UUID, *, sold_on: date | None = None) -> Job:
        """Record that the home now has a buyer behind it.

        Deliberately **not** a transition through the status table, and that is
        the correction of a real modelling mistake. Treating "sold" as a stage in
        a linear pipeline meant a home already in permitting could not be sold -
        which is nonsense for any builder who puts a plot through planning and
        finds a buyer afterwards, and doubly so for a spec home sold halfway
        through its build.

        `status` tracks **construction** progress. Selling is an event that can
        land at any point before handover, so:

          * a home still on the drawing board moves to `sold`, because that is a
            real change in what it is
          * a home already permitting, building or finished keeps its status and
            simply records the date - it is no less under construction for having
            found a buyer

        Refused only where it would be meaningless: a cancelled home nobody is
        building, or one already handed over to somebody else.
        """
        job = await self.jobs.get(job_id)

        if job.status is JobStatus.CANCELLED:
            raise BusinessRuleViolation(
                f"{job.job_number} was cancelled.",
                "Start a new home on the plot and sell that one.",
            )
        if job.status in (JobStatus.CLOSED, JobStatus.WARRANTY):
            raise BusinessRuleViolation(
                f"{job.job_number} has already been handed over.",
                "It belongs to its buyer now.",
            )
        if job.sold_date is not None:
            raise Conflict(f"{job.job_number} has already been sold.")

        # Sold-to-start is the gap a sales director argues about, and it cannot be
        # measured if the date is never written down.
        job.sold_date = sold_on or date.today()
        if job.status is JobStatus.PLANNED:
            job.status = JobStatus.SOLD

        await self.db.flush()
        return job

    async def enter_permitting(self, job_id: uuid.UUID, **data) -> Job:
        """Record that the permit application has gone in."""
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.PERMITTING)
        job.status = JobStatus.PERMITTING
        job.permit_applied_on = data.get("permit_applied_on") or date.today()
        if data.get("permit_number"):
            job.permit_number = data["permit_number"]
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def record_permit(self, job_id: uuid.UUID, *, number: str, issued_on=None) -> Job:
        """The permit came through. Does not move the status by itself - a permit
        in hand is not the same as work having begun, and conflating them is how a
        start date becomes fiction."""
        job = await self.jobs.get(job_id)
        if job.status is JobStatus.CANCELLED:
            raise Conflict(f"{job.job_number} was cancelled.")
        job.permit_number = number
        job.permit_issued_on = issued_on or date.today()
        if job.permit_applied_on and job.permit_issued_on < job.permit_applied_on:
            raise BusinessRuleViolation(
                "A permit cannot be issued before it was applied for.",
                f"The application went in on {job.permit_applied_on}.",
            )
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def start_build(
        self,
        job_id: uuid.UUID,
        *,
        start_date: date | None = None,
        projected_completion: date | None = None,
        allow_unsold: bool = False,
    ) -> Job:
        """Break ground.

        Refused on an unsold home unless the caller says so deliberately. Building
        on spec is a real strategy and a real risk, and the difference between
        choosing it and clicking it by mistake is worth one extra flag.
        """
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.UNDER_CONSTRUCTION)

        if job.status is JobStatus.ON_HOLD:
            return await self.resume(job_id)

        if not job.is_sold and not allow_unsold:
            raise BusinessRuleViolation(
                f"{job.job_number} has not been sold.",
                "Starting an unsold home is building on spec - allowed, but say so "
                "deliberately rather than by accident.",
            )

        when = start_date or date.today()
        if when > date.today():
            raise BusinessRuleViolation(
                "A build cannot start in the future.",
                "Use the projected completion date for planning; the start date "
                "records what happened.",
            )

        job.status = JobStatus.UNDER_CONSTRUCTION
        job.start_date = when
        # Frozen here. This is the baseline slip is measured against, and a
        # baseline set after the fact measures nothing.
        if projected_completion:
            job.projected_completion = projected_completion
        if job.projected_completion and job.projected_completion < when:
            raise BusinessRuleViolation(
                "The projected completion is before the start date.",
                "Check the dates - as entered this home is finished before it begins.",
            )
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def hold(
        self, job_id: uuid.UUID, reason: HoldReason, note: str | None = None
    ) -> Job:
        """Stop the build, with a reason that can be counted later."""
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.ON_HOLD)

        if reason is HoldReason.OTHER and not (note or "").strip():
            raise BusinessRuleViolation(
                "A hold reason of 'other' needs a note.",
                "The point of the reason list is to count days lost per cause. "
                "'Other' with nothing written is the same as no reason at all.",
            )

        job.status = JobStatus.ON_HOLD
        job.hold_reason = reason
        job.hold_note = note
        job.on_hold_since = date.today()
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def resume(self, job_id: uuid.UUID) -> Job:
        """Lift the hold and bank the days it cost."""
        job = await self.jobs.get(job_id)
        if job.status is not JobStatus.ON_HOLD:
            raise Conflict(f"{job.job_number} is not on hold.")

        if job.on_hold_since:
            # Accumulated rather than replaced, so a home stopped three times
            # carries the total rather than only the last one.
            job.days_on_hold += max(0, (date.today() - job.on_hold_since).days)
        job.status = JobStatus.UNDER_CONSTRUCTION
        job.hold_reason = None
        job.hold_note = None
        job.on_hold_since = None
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def complete(self, job_id: uuid.UUID, *, completed_on: date | None = None) -> Job:
        """The house is built.

        Not the same as closed: the buyer does not own it yet, and between these
        two sit the final inspection, the snagging list and the money.
        """
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.COMPLETE)

        when = completed_on or date.today()
        if not job.start_date:
            raise BusinessRuleViolation(
                f"{job.job_number} has no start date, so it cannot be completed.",
                "Start the build first - a completion with no start makes the "
                "cycle time meaningless.",
            )
        if when < job.start_date:
            raise BusinessRuleViolation(
                "The completion date is before the start date.",
                f"This home started on {job.start_date}.",
            )
        if when > date.today():
            raise BusinessRuleViolation(
                "A home cannot be completed in the future.",
                "Record it on the day it happens.",
            )

        job.status = JobStatus.COMPLETE
        job.actual_completion = when
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def reopen_build(self, job_id: uuid.UUID, reason: str) -> Job:
        """Send a completed home back to the site.

        It happens: a final inspection fails, or snagging turns out to be real
        work. The completion date is cleared rather than kept, because a home that
        goes back on site was not finished, and leaving the date would report a
        cycle time that never happened.
        """
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.UNDER_CONSTRUCTION)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Reopening a completed home needs a reason.",
                "It undoes a completion date that has already been reported.",
            )
        job.status = JobStatus.UNDER_CONSTRUCTION
        job.actual_completion = None
        job.notes = f"{job.notes or ''}\nReopened: {reason.strip()}".strip()
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def close(
        self,
        job_id: uuid.UUID,
        *,
        closing_date: date | None = None,
        warranty_months: int = DEFAULT_WARRANTY_MONTHS,
    ) -> Job:
        """Hand the house over. Title transfers and the warranty clock starts.

        The two gates are the ones that would be a legal problem rather than a
        data problem: there must be a signed contract, and the house must be
        finished.
        """
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.CLOSED)

        contract = await self.contracts.for_job(job_id)
        if contract is None:
            raise BusinessRuleViolation(
                f"{job.job_number} has no contract.",
                "Title cannot transfer without one. Sell the home first.",
            )
        if not contract.signed_date:
            raise BusinessRuleViolation(
                f"The contract on {job.job_number} has not been signed.",
                "Get the signature on file before closing - this is the document "
                "the handover rests on.",
            )

        # Defects that are not merely cosmetic stop the keys changing hands.
        # Handing over a home with a major defect still on the snag list turns a
        # warranty claim into a dispute, and the buyer has the list.
        from app.services.field import FieldService

        blockers = await FieldService(self.db, self.tenant_id).handover_blockers(job_id)
        if blockers:
            worst = ", ".join(b.description[:40] for b in blockers[:3])
            raise BusinessRuleViolation(
                f"{job.job_number} has {len(blockers)} unresolved defect"
                f"{'s' if len(blockers) != 1 else ''}: {worst}"
                + ("…" if len(blockers) > 3 else "."),
                "Fix and sign them off first, or mark the cosmetic ones as such - "
                "a buyer will take a house with a scuff on it and not one with a "
                "leak.",
            )

        when = closing_date or date.today()
        if job.actual_completion and when < job.actual_completion:
            raise BusinessRuleViolation(
                "The closing date is before the home was finished.",
                f"It was completed on {job.actual_completion}.",
            )

        job.status = JobStatus.CLOSED
        job.closing_date = when
        # The warranty runs from handover, not from completion: the buyer cannot
        # report a fault in a house they do not have yet.
        job.warranty_expires_on = _add_months(when, warranty_months)

        lot = await self.lots.find(job.lot_id)
        if lot is not None:
            lot.status = LotStatus.CLOSED
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def enter_warranty(self, job_id: uuid.UUID) -> Job:
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.WARRANTY)
        job.status = JobStatus.WARRANTY
        await self.db.commit()
        return await self.jobs.get(job_id)

    async def cancel(
        self,
        job_id: uuid.UUID,
        reason: CancellationReason,
        note: str | None = None,
    ) -> Job:
        """Abandon the home and give the plot back.

        Refused once the house is closed, because at that point it belongs to
        somebody else. Everything before that can be cancelled - including a home
        halfway through construction, which is a real and expensive event that the
        system should be able to record rather than force somebody to fake.
        """
        job = await self.jobs.get(job_id)
        self._require_transition(job, JobStatus.CANCELLED)

        if reason is CancellationReason.OTHER and not (note or "").strip():
            raise BusinessRuleViolation(
                "A cancellation reason of 'other' needs a note.",
                "Cancellations are counted by cause; an uncounted one teaches "
                "nobody anything.",
            )

        job.status = JobStatus.CANCELLED
        job.cancelled_on = date.today()
        job.cancellation_reason = reason
        job.cancellation_note = note

        # The whole point: the plot goes back on the market. The partial unique
        # index lets a new home be started here, which a plain UNIQUE(lot_id)
        # would have blocked forever.
        lot = await self.lots.find(job.lot_id)
        if lot is not None and lot.status is LotStatus.SOLD:
            lot.status = LotStatus.AVAILABLE
        await self.db.commit()
        return await self.jobs.get(job_id)

    # ========================================================= helpers =====

    async def _check_plan(
        self, plan_id: uuid.UUID, elevation: str | None, *, new_home: bool = True
    ) -> str | None:
        """The plan must be on the price list, and the elevation one it is built with.

        A plan with no elevations set up still takes free text, as it always did. Once
        it has them, a typo is refused - "B" and "b " are the same front, "Brik" is not
        a front at all - and the code is stored as the plan spells it.
        """
        from sqlalchemy import select

        from app.models.plan import PlanElevation

        plan = await self.plans.require_exists(plan_id)
        if new_home and not plan.is_active:
            raise BusinessRuleViolation(
                f"{plan.code} {plan.name} is archived.",
                "Archived house types are kept for the homes already built. Restore it, "
                "or pick a current plan.",
            )
        if not elevation or not elevation.strip():
            return None
        fronts = list(
            await self.db.scalars(
                select(PlanElevation).where(
                    PlanElevation.plan_id == plan.id, PlanElevation.is_active.is_(True)
                )
            )
        )
        if not fronts:
            return elevation.strip()
        match = next((e for e in fronts if e.code.upper() == elevation.strip().upper()), None)
        if match is None:
            raise BusinessRuleViolation(
                f"{plan.code} is not built with elevation {elevation.strip()}.",
                "It is offered as " + ", ".join(e.code for e in fronts) + ".",
            )
        return match.code

    async def _require_colleague(self, user_id: uuid.UUID) -> None:
        from app.models.user import User

        found = await self.db.scalar(
            select(User.id).where(User.id == user_id, User.tenant_id == self.tenant_id)
        )
        if found is None:
            raise NotFound("User", user_id)

    async def cycle_times(self) -> dict:
        """Average cycle time, and how it splits by community.

        Only completed homes count. Including the ones still running would report
        a number that improves every time a slow house finally finishes, which is
        the opposite of what it means.

        Aggregated in Python rather than SQL: date subtraction is spelled
        differently on PostgreSQL and SQLite, and branching on the dialect inside
        a query is the kind of cleverness that breaks on the platform you are not
        looking at. Completed homes are a small set; this is not the slow part.
        """
        rows = await self.db.execute(
            select(Job.community_id, Job.start_date, Job.actual_completion).where(
                Job.tenant_id == self.tenant_id,
                Job.start_date.is_not(None),
                Job.actual_completion.is_not(None),
                Job.status != JobStatus.CANCELLED,
            )
        )
        per_community: dict[uuid.UUID, list[int]] = {}
        for community_id, start, finish in rows:
            per_community.setdefault(community_id, []).append((finish - start).days)

        by_community = [
            {
                "community_id": cid,
                "homes": len(days),
                "average_days": round(sum(days) / len(days)),
            }
            for cid, days in per_community.items()
        ]
        by_community.sort(key=lambda r: -r["homes"])

        every = [d for days in per_community.values() for d in days]
        return {
            "homes_measured": len(every),
            "average_days": round(sum(every) / len(every)) if every else None,
            "by_community": by_community,
        }


def _add_months(start: date, months: int) -> date:
    """Calendar-month arithmetic, clamped to the end of a short month.

    24 months from 31 January is 31 January; 1 month from 31 January is the 28th
    or 29th. Getting this wrong puts a warranty expiry a day out, which somebody
    will eventually argue about.
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]
