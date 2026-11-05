"""Business tier for field operations.

The superintendent's half of the system: what happened on site, what has to pass,
and what still needs fixing.

The rules that matter here are about the end of a build, because that is where a
home stops being finishable:

  * **somebody has to check the fix.** A punch item used to go from the trade
    saying it was done straight to complete. Nobody signed off, so a home could be
    handed over on a snag list the builder had only the trade's word for.
  * **not fixing something needs a reason.** `wont_fix` is exactly the status a
    buyer argues about at handover, and "we decided not to" is not an answer.
  * **a failed inspection becomes a punch list in one step.** Retyping thirty
    failed checks by hand is how three of them get missed, and the three that get
    missed are the ones the inspector looks at first on the re-inspection.
  * **open defects stop a handover.** Not cosmetic ones - a buyer will take the
    house and a decorator comes back - but anything above that, because handing
    over a home with a major defect on the list turns a warranty claim into a
    dispute.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.communication import BallInCourt
from app.models.field import (
    UNRESOLVED_PUNCH,
    DailyLog,
    Inspection,
    InspectionItem,
    InspectionResult,
    InspectionType,
    ItemResult,
    PunchItem,
    PunchSeverity,
    PunchStatus,
)
from app.repositories.base import Conflict, TenantRepository
from app.repositories.job import JobRepository
from app.schemas.common import PageParams

#: Where a punch item may go from where it is.
#:
#: The shape of it is the point: nothing reaches `complete` except through
#: `ready_for_review`, so every closed defect has been looked at by somebody other
#: than whoever claims to have fixed it.
PUNCH_MOVES: dict[PunchStatus, tuple[PunchStatus, ...]] = {
    PunchStatus.OPEN: (
        PunchStatus.ASSIGNED,
        PunchStatus.READY_FOR_REVIEW,
        PunchStatus.WONT_FIX,
    ),
    PunchStatus.ASSIGNED: (PunchStatus.READY_FOR_REVIEW, PunchStatus.WONT_FIX),
    # Back to assigned when the fix is rejected, which is the loop that matters.
    PunchStatus.READY_FOR_REVIEW: (
        PunchStatus.COMPLETE,
        PunchStatus.ASSIGNED,
        PunchStatus.WONT_FIX,
    ),
    PunchStatus.COMPLETE: (),
    PunchStatus.WONT_FIX: (PunchStatus.OPEN,),
}


class PunchRepository(TenantRepository[PunchItem]):
    model = PunchItem
    label = "Punch item"
    search_fields = ("description", "location")

    async def page(self, params: PageParams, **filters):
        return await self.paginate(params, order_by=PunchItem.created_at.desc(), **filters)

    async def unresolved_for_job(self, job_id: uuid.UUID) -> list[PunchItem]:
        rows = await self.db.scalars(
            self._base().where(PunchItem.job_id == job_id, PunchItem.status.in_(UNRESOLVED_PUNCH))
        )
        return list(rows)


class InspectionRepository(TenantRepository[Inspection]):
    model = Inspection
    label = "Inspection"
    search_fields = ("name", "inspector")
    default_loads = ("items",)

    async def page(self, params: PageParams, **filters):
        return await self.paginate(
            params, order_by=Inspection.scheduled_date.desc().nullslast(), **filters
        )


class DailyLogRepository(TenantRepository[DailyLog]):
    model = DailyLog
    label = "Daily log"
    search_fields = ("work_performed", "delays", "notes", "weather")

    async def page(self, params: PageParams, **filters):
        return await self.paginate(params, order_by=DailyLog.log_date.desc(), **filters)

    async def for_day(self, job_id: uuid.UUID, on: date) -> DailyLog | None:
        return await self.db.scalar(
            self._base().where(DailyLog.job_id == job_id, DailyLog.log_date == on)
        )


class FieldService:
    """Site operations, scoped to one builder."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.punch = PunchRepository(db, tenant_id)
        self.inspections = InspectionRepository(db, tenant_id)
        self.logs = DailyLogRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)

    # ============================================== the punch list =====

    async def list_punch(self, params: PageParams, **filters):
        return await self.punch.page(params, **filters)

    async def create_punch(self, *, actor_id=None, **data) -> PunchItem:
        """Raise a defect.

        Who it sits with depends on whether it has been given to anybody. A defect
        with no trade against it sits with the builder, because the outstanding
        action is choosing who fixes it - saying it sits with "the trade" when no
        trade has been named is the sort of entry that reads as progress and is not.
        """
        await self.jobs.require_exists(data["job_id"])
        if data.get("vendor_id"):
            from app.repositories.catalog import VendorRepository

            await VendorRepository(self.db, self.tenant_id).require_exists(data["vendor_id"])
            data.setdefault("status", PunchStatus.ASSIGNED)
            data.setdefault("ball_in_court", BallInCourt.TRADE)
        else:
            data.setdefault("ball_in_court", BallInCourt.BUILDER)
        return await self.punch.create(raised_by_id=actor_id, **data)

    async def update_punch(self, item_id: uuid.UUID, **data) -> PunchItem:
        """Correct a defect's details.

        Status is not settable here. It moves through the routes below, which
        check the transition and record who accepted the fix - a PATCH that set it
        would route round the verification step entirely.
        """
        item = await self.punch.get(item_id)
        data.pop("status", None)
        if item.status is PunchStatus.COMPLETE:
            raise Conflict(f"{item.description[:40]} has already been signed off.")
        return await self.punch.update(item, **data)

    def _require_move(self, item: PunchItem, to: PunchStatus) -> None:
        allowed = PUNCH_MOVES[item.status]
        if to in allowed:
            return
        if item.status is PunchStatus.COMPLETE:
            raise Conflict("That item has already been signed off.")
        raise BusinessRuleViolation(
            f"A {item.status.value.replace('_', ' ')} item cannot become "
            f"{to.value.replace('_', ' ')}.",
            "Next steps from here: "
            + (", ".join(s.value.replace("_", " ") for s in allowed) or "none")
            + ".",
        )

    async def assign_punch(
        self, item_id: uuid.UUID, vendor_id: uuid.UUID, due_date: date | None = None
    ) -> PunchItem:
        """Give the defect to a trade.

        Assigning it is what lets the work be backcharged rather than absorbed,
        and it moves the ball into their court - an item nobody owns is exactly
        the kind that sits open until handover week.
        """
        from app.repositories.catalog import VendorRepository

        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.ASSIGNED)
        await VendorRepository(self.db, self.tenant_id).require_exists(vendor_id)

        item.vendor_id = vendor_id
        item.status = PunchStatus.ASSIGNED
        item.ball_in_court = BallInCourt.TRADE
        if due_date:
            item.due_date = due_date
        await self.db.commit()
        return await self.punch.get(item_id)

    async def submit_punch(self, item_id: uuid.UUID, note: str | None = None) -> PunchItem:
        """The trade says it is done.

        Deliberately not the same as complete. This is a claim; the next step is
        somebody checking it.
        """
        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.READY_FOR_REVIEW)
        item.status = PunchStatus.READY_FOR_REVIEW
        item.ball_in_court = BallInCourt.BUILDER
        if note:
            item.resolution_note = note
        await self.db.commit()
        return await self.punch.get(item_id)

    async def verify_punch(self, item_id: uuid.UUID, *, actor_id: uuid.UUID) -> PunchItem:
        """Somebody has looked at the fix and accepted it.

        The step that was missing. Without it a home could be handed over on a
        snag list the builder had only the trade's word for.
        """
        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.COMPLETE)
        item.status = PunchStatus.COMPLETE
        item.completed_date = date.today()
        item.verified_by_id = actor_id
        item.verified_date = date.today()
        item.ball_in_court = BallInCourt.CLOSED
        await self.db.commit()
        return await self.punch.get(item_id)

    async def reject_punch(self, item_id: uuid.UUID, reason: str) -> PunchItem:
        """The fix was not good enough. Back to the trade, with why.

        The loop that matters: without it, a rejected fix is a phone call nobody
        can find later when the same defect is argued about at handover.
        """
        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.ASSIGNED)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Sending a fix back needs a reason.",
                "The trade has to know what is still wrong, and you will want the "
                "record when it is argued about at handover.",
            )
        item.status = PunchStatus.ASSIGNED
        item.ball_in_court = BallInCourt.TRADE
        item.resolution_note = reason.strip()
        await self.db.commit()
        return await self.punch.get(item_id)

    async def wont_fix(self, item_id: uuid.UUID, reason: str) -> PunchItem:
        """Deliberately not fixing it, with the reason on the record.

        The status a buyer argues about at handover, so "we decided not to" is not
        an answer the system will accept.
        """
        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.WONT_FIX)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Declining to fix something needs a reason.",
                "This is the item a buyer will ask about at handover.",
            )
        if item.severity is PunchSeverity.BLOCKING:
            raise BusinessRuleViolation(
                "That defect is marked as blocking.",
                "Blocking means life-safety or habitability. Downgrade the severity "
                "deliberately if that is wrong, rather than declining to fix it.",
            )
        item.status = PunchStatus.WONT_FIX
        item.ball_in_court = BallInCourt.CLOSED
        item.resolution_note = reason.strip()
        await self.db.commit()
        return await self.punch.get(item_id)

    async def reopen_punch(self, item_id: uuid.UUID, reason: str) -> PunchItem:
        """Put a declined defect back on the list.

        Leaving the court closed would be the worst of the three: the item is open
        again and anybody scanning for who owes the next move would not see it.
        """
        item = await self.punch.get(item_id)
        self._require_move(item, PunchStatus.OPEN)
        item.status = PunchStatus.OPEN
        item.ball_in_court = BallInCourt.TRADE if item.vendor_id else BallInCourt.BUILDER
        item.resolution_note = (reason or "").strip() or None
        await self.db.commit()
        return await self.punch.get(item_id)

    # ============================================ blocking handover =====

    async def handover_blockers(self, job_id: uuid.UUID) -> list[PunchItem]:
        """Defects that stop the keys changing hands.

        Cosmetic ones do not: a buyer will take the house and a decorator comes
        back. Anything above that does, because handing over a home with a major
        defect still on the list turns a warranty claim into a dispute.

        The home is proved to be ours first. The punch repository is tenant-scoped
        so nothing leaks either way, but without this the answer to "what is
        blocking somebody else's house" is an empty list - which reads as "nothing
        is" rather than "no such house".
        """
        await self.jobs.require_exists(job_id)
        items = await self.punch.unresolved_for_job(job_id)
        return [i for i in items if i.blocks_handover]

    # ================================================ inspections =====

    async def list_inspections(self, params: PageParams, **filters):
        return await self.inspections.page(params, **filters)

    async def create_inspection(self, **data) -> Inspection:
        await self.jobs.require_exists(data["job_id"])
        return await self.inspections.create(**data)

    async def add_check(self, inspection_id: uuid.UUID, **data) -> InspectionItem:
        inspection = await self.inspections.get(inspection_id)
        if inspection.result is not InspectionResult.SCHEDULED:
            raise Conflict(
                f"{inspection.name} has already been carried out; its checklist is fixed."
            )
        if data.get("sequence") is None:
            highest = await self.db.scalar(
                select(func.max(InspectionItem.sequence)).where(
                    InspectionItem.tenant_id == self.tenant_id,
                    InspectionItem.inspection_id == inspection_id,
                )
            )
            data["sequence"] = (highest or 0) + 1
        item = InspectionItem(tenant_id=self.tenant_id, inspection_id=inspection_id, **data)
        self.db.add(item)
        await self.db.commit()
        return item

    async def record_check(
        self, item_id: uuid.UUID, result: ItemResult, notes: str | None = None
    ) -> InspectionItem:
        item = await self.db.get(InspectionItem, item_id)
        if item is None or item.tenant_id != self.tenant_id:
            from app.repositories.base import NotFound

            raise NotFound("Inspection item", item_id)
        if result is ItemResult.FAIL and not (notes or "").strip():
            raise BusinessRuleViolation(
                "A failed check needs a note.",
                "Somebody has to send a trade back for it, and 'fail' on its own "
                "does not say what to fix.",
            )
        item.result = result
        if notes:
            item.notes = notes.strip()
        await self.db.commit()
        return item

    async def record_result(
        self,
        inspection_id: uuid.UUID,
        result: InspectionResult,
        *,
        failure_reason: str | None = None,
        completed_date: date | None = None,
        fee: Decimal | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Inspection:
        """Record how the inspection went."""
        inspection = await self.inspections.get(inspection_id)
        if inspection.result is not InspectionResult.SCHEDULED:
            raise Conflict(
                f"{inspection.name} has already been recorded as {inspection.result.value}."
            )
        if result is InspectionResult.FAILED and not (failure_reason or "").strip():
            # The headline reason is what a superintendent reads on the list and
            # what goes to the trade, so a failure cannot be recorded without one.
            # Where the checks carry it, it is composed from them rather than
            # retyped - the checklist has already said what went wrong.
            if not inspection.failed_items:
                raise BusinessRuleViolation(
                    "A failed inspection needs a reason.",
                    "Either fail the individual checks or say what went wrong - "
                    "otherwise nobody knows what to send anyone back for.",
                )
            failure_reason = "; ".join(
                i.description for i in sorted(inspection.failed_items, key=lambda i: i.sequence)
            )
        if result is InspectionResult.PASSED and inspection.failed_items:
            raise BusinessRuleViolation(
                f"{len(inspection.failed_items)} check(s) on this inspection are marked failed.",
                "Pass the checks that were actually met, or record the inspection as "
                "failed. An inspection that passed with failed checks on it is a "
                "record nobody can act on.",
            )

        inspection.result = result
        inspection.completed_date = completed_date or date.today()
        if failure_reason:
            inspection.failure_reason = failure_reason.strip()
        if fee is not None:
            inspection.fee = fee

        # A failure is not just a record that a problem existed. Raising the
        # corrective action here is what makes the inspection record show what was
        # done about it - and doing it on the way through means it cannot be
        # forgotten, which is the only version of "automatically" worth having.
        #
        # Imported here rather than at module scope: the safety service imports
        # this module's models, so a top-level import is a cycle.
        from app.services.safety import SafetyService

        await self.db.flush()
        if result is InspectionResult.FAILED:
            await SafetyService(self.db, self.tenant_id).observations_for_failure(
                inspection, actor_id=actor_id
            )

        await self.db.commit()
        return await self.inspections.get(inspection_id)

    async def punch_from_failures(
        self, inspection_id: uuid.UUID, *, actor_id=None
    ) -> list[PunchItem]:
        """Turn every failed check into a defect to fix.

        Retyping thirty failed checks by hand is how three of them get missed, and
        the three that get missed are the ones the inspector looks at first on the
        re-inspection.

        Items already turned into punch items are skipped, so running it twice
        after a partial re-check does not duplicate the list.
        """
        inspection = await self.inspections.get(inspection_id)
        failed = inspection.failed_items
        if not failed:
            raise BusinessRuleViolation(
                f"Nothing failed on {inspection.name}.",
                "Mark the individual checks that failed first.",
            )

        already = set(
            await self.db.scalars(
                select(PunchItem.inspection_item_id).where(
                    PunchItem.tenant_id == self.tenant_id,
                    PunchItem.inspection_item_id.in_([i.id for i in failed]),
                )
            )
        )

        created = []
        for check in failed:
            if check.id in already:
                continue
            created.append(
                self.punch.stage(
                    job_id=inspection.job_id,
                    description=check.description,
                    location=inspection.name,
                    vendor_id=check.vendor_id,
                    raised_by_id=actor_id,
                    inspection_item_id=check.id,
                    # A municipal failure stops the build, so anything from one is
                    # blocking by definition rather than by somebody's judgement.
                    severity=(
                        PunchSeverity.BLOCKING
                        if inspection.inspection_type is InspectionType.MUNICIPAL
                        else PunchSeverity.MAJOR
                    ),
                    status=(PunchStatus.ASSIGNED if check.vendor_id else PunchStatus.OPEN),
                    # Whose check it was, where the checklist recorded one. A check
                    # nobody was named against leaves the builder to decide who
                    # fixes it, same as any other unassigned defect.
                    ball_in_court=(BallInCourt.TRADE if check.vendor_id else BallInCourt.BUILDER),
                )
            )
        await self.db.commit()
        return created

    async def schedule_reinspection(
        self, inspection_id: uuid.UUID, when: date, *, fee: Decimal | None = None
    ) -> Inspection:
        """Book the retry, pointed at the one it is retrying.

        The chain is what answers "how many times did this fail before it passed",
        which is the question that tells a builder whose work is costing them
        re-inspection fees.
        """
        original = await self.inspections.get(inspection_id)
        if original.result is not InspectionResult.FAILED:
            raise Conflict(f"{original.name} did not fail, so there is nothing to re-inspect.")
        return await self.inspections.create(
            job_id=original.job_id,
            inspection_type=original.inspection_type,
            name=f"{original.name} (re-inspection)",
            scheduled_date=when,
            inspector=original.inspector,
            task_id=original.task_id,
            reinspects_id=original.id,
            fee=fee if fee is not None else Decimal("0"),
        )

    # ================================================= daily logs =====

    async def list_logs(self, params: PageParams, **filters):
        return await self.logs.page(params, **filters)

    async def record_log(self, *, actor_id=None, **data) -> DailyLog:
        """File today's log, or update the one already filed.

        An upsert rather than an insert, because a superintendent adding the
        afternoon's events to the morning's entry is the normal case - and posting
        twice used to produce two logs for one day, which makes the record that is
        supposed to substantiate a delay claim contradict itself.
        """
        await self.jobs.require_exists(data["job_id"])
        on = data.get("log_date") or date.today()
        data["log_date"] = on

        if on > date.today():
            raise BusinessRuleViolation(
                "A daily log cannot be filed for a future day.",
                "It records what happened, which has not happened yet.",
            )

        existing = await self.logs.for_day(data["job_id"], on)
        if existing is not None:
            return await self.logs.update(existing, **self._merge_log(existing, data))
        return await self.logs.create(author_id=actor_id, **data)

    # What happened is added to; what was true is corrected. Overwriting the
    # narrative is how the morning's entry disappears when somebody files the
    # afternoon's, which would leave the record thinner than if they had not
    # bothered.
    _LOG_NARRATIVE = ("work_performed", "delays", "notes", "trades_on_site")

    @staticmethod
    def _merge_log(existing: DailyLog, data: dict) -> dict:
        fields = {k: v for k, v in data.items() if k not in ("job_id", "log_date")}
        for key in FieldService._LOG_NARRATIVE:
            new = (fields.get(key) or "").strip()
            if not new:
                fields.pop(key, None)
                continue
            was = (getattr(existing, key) or "").strip()
            if not was:
                fields[key] = new
            elif new in was:
                # Filing the same text twice - a double submit, or somebody
                # pasting the whole entry again rather than the addition.
                fields[key] = was
            else:
                fields[key] = f"{was}\n{new}"
        return fields

    # =================================================== summary =====

    async def summary(self, job_id: uuid.UUID | None = None) -> dict:
        """The numbers above the screen."""
        scope = [PunchItem.tenant_id == self.tenant_id]
        if job_id:
            scope.append(PunchItem.job_id == job_id)

        async def punch_count(*extra) -> int:
            return (
                await self.db.scalar(
                    select(func.count()).select_from(PunchItem).where(*scope, *extra)
                )
                or 0
            )

        insp_scope = [Inspection.tenant_id == self.tenant_id]
        if job_id:
            insp_scope.append(Inspection.job_id == job_id)

        failed = (
            await self.db.scalar(
                select(func.count())
                .select_from(Inspection)
                .where(*insp_scope, Inspection.result == InspectionResult.FAILED)
            )
            or 0
        )

        log_scope = [DailyLog.tenant_id == self.tenant_id]
        if job_id:
            log_scope.append(DailyLog.job_id == job_id)
        logs = (
            await self.db.scalar(select(func.count()).select_from(DailyLog).where(*log_scope)) or 0
        )

        return {
            "open_punch": await punch_count(PunchItem.status.in_(UNRESOLVED_PUNCH)),
            "awaiting_review": await punch_count(PunchItem.status == PunchStatus.READY_FOR_REVIEW),
            "overdue_punch": await punch_count(
                PunchItem.status.in_(UNRESOLVED_PUNCH),
                PunchItem.due_date.is_not(None),
                PunchItem.due_date < date.today(),
            ),
            "blocking_handover": await punch_count(
                PunchItem.status.in_(UNRESOLVED_PUNCH),
                PunchItem.severity != PunchSeverity.COSMETIC,
            ),
            "from_walkthrough": await punch_count(
                PunchItem.status.in_(UNRESOLVED_PUNCH), PunchItem.from_walkthrough.is_(True)
            ),
            "failed_inspections": failed,
            "daily_logs": logs,
        }
