"""The build schedule: generating it, and keeping it honest as it slips.

What a schedule is for
----------------------
A superintendent runs eight houses and twenty trades, and the only thing holding
that together is a set of dates everybody believes. The moment those dates stop
being true the schedule is worse than useless, because people still act on it.

So the two operations that matter are generating the programme and rippling a
slip through it - and both of them were subtly wrong.

**Durations are working days.** They were calendar days, so a ten-day framing task
starting on a Thursday finished on a Sunday. Over a 115-day build the error
compounds to roughly a third of the programme, and every date after the first
weekend was fiction.

**A slip moves the forecast, not the promise.** Completing a task late used to
move the job's `projected_completion` - the date the Job model documents as the
frozen baseline that lateness is measured against. Moving it meant the job was
never late: the target retreated ahead of the slip. There are two dates now, and
the gap between them is how far behind the build is.

Why offsets rather than a dependency graph
------------------------------------------
Kept from the original design and still right for a production builder: the
sequence repeats house after house, and offsets are far easier to configure than
a graph. What a graph would buy - a true critical path - is not what a
superintendent asks. They ask what is due this week and what has stopped.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.job import Job, JobStatus
from app.models.schedule import (
    Holiday,
    JobTask,
    ScheduleTemplate,
    ScheduleTemplateTask,
    TaskStatus,
)
from app.repositories.base import Conflict, NotFound, TenantRepository
from app.services.calendar import add_working_days, next_working_day, shift_working_days

#: How far ahead "coming up" looks. A fortnight is the window a superintendent
#: actually plans in: far enough to book a trade, near enough to still be true.
LOOKAHEAD_DAYS = 14

#: Statuses a task can be worked from.
OPEN_TASKS = (
    TaskStatus.NOT_STARTED,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
)


class HolidayRepository(TenantRepository[Holiday]):
    model = Holiday
    label = "Holiday"

    async def all_ordered(self) -> list[Holiday]:
        return await self.list(order_by=Holiday.on_date)

    async def dates(self) -> frozenset[date]:
        rows = await self.db.scalars(
            select(Holiday.on_date).where(Holiday.tenant_id == self.tenant_id)
        )
        return frozenset(rows)


class TemplateRepository(TenantRepository[ScheduleTemplate]):
    model = ScheduleTemplate
    label = "Schedule template"
    search_fields = ("code", "name")

    async def all_ordered(self) -> list[ScheduleTemplate]:
        return await self.list(order_by=ScheduleTemplate.code)


class TaskRepository(TenantRepository[JobTask]):
    model = JobTask
    label = "Task"
    search_fields = ("name", "notes")

    async def for_job(self, job_id: uuid.UUID) -> list[JobTask]:
        return await self.list(job_id=job_id, order_by=JobTask.sequence)

    async def downstream_of(self, task: JobTask) -> list[JobTask]:
        """Unfinished tasks behind this one in the sequence."""
        rows = await self.db.scalars(
            self._base()
            .where(
                JobTask.job_id == task.job_id,
                JobTask.sequence > task.sequence,
                JobTask.status.notin_((TaskStatus.COMPLETE, TaskStatus.SKIPPED)),
            )
            .order_by(JobTask.sequence)
        )
        return list(rows)


class ScheduleService:
    """The build programme, scoped to one builder."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.templates = TemplateRepository(db, tenant_id)
        self.tasks = TaskRepository(db, tenant_id)
        self.holidays = HolidayRepository(db, tenant_id)

    async def _non_working(self) -> frozenset[date]:
        return await self.holidays.dates()

    # ================================================ templates =====

    async def list_templates(self) -> list[ScheduleTemplate]:
        return await self.templates.all_ordered()

    async def get_template(self, template_id: uuid.UUID) -> ScheduleTemplate:
        return await self.templates.get(template_id)

    async def create_template(self, **data) -> ScheduleTemplate:
        await self.templates.require_unique_code(data["code"])
        return await self.templates.create(**data)

    async def update_template(self, template_id: uuid.UUID, **data) -> ScheduleTemplate:
        template = await self.templates.get(template_id)
        return await self.templates.update(template, **data)

    async def duplicate_template(self, template_id: uuid.UUID, code: str, name: str):
        """Copy a template and its tasks.

        A builder's second house type is usually the first with three steps
        changed, and retyping forty tasks to get there is how a template ends up
        wrong.
        """
        original = await self.templates.get(template_id)
        await self.templates.require_unique_code(code)
        copy = self.templates.stage(code=code, name=name, plan_id=original.plan_id)
        await self.db.flush()

        for task in sorted(original.tasks, key=lambda t: t.sequence):
            self.db.add(
                ScheduleTemplateTask(
                    tenant_id=self.tenant_id,
                    template_id=copy.id,
                    sequence=task.sequence,
                    name=task.name,
                    offset_days=task.offset_days,
                    duration_days=task.duration_days,
                    cost_code_id=task.cost_code_id,
                    vendor_id=task.vendor_id,
                    is_milestone=task.is_milestone,
                )
            )
        await self.db.commit()
        return await self.templates.get(copy.id)

    async def add_template_task(self, template_id: uuid.UUID, **data) -> ScheduleTemplateTask:
        await self.templates.require_exists(template_id)
        if data.get("sequence") is None:
            # Appended rather than made to collide. A template with two tasks at
            # sequence 7 generates a schedule whose order depends on the database.
            highest = await self.db.scalar(
                select(func.max(ScheduleTemplateTask.sequence)).where(
                    ScheduleTemplateTask.tenant_id == self.tenant_id,
                    ScheduleTemplateTask.template_id == template_id,
                )
            )
            data["sequence"] = (highest or 0) + 1

        task = ScheduleTemplateTask(
            tenant_id=self.tenant_id, template_id=template_id, **data
        )
        self.db.add(task)
        await self.db.commit()
        return task

    async def update_template_task(self, task_id: uuid.UUID, **data) -> ScheduleTemplateTask:
        task = await self.db.get(ScheduleTemplateTask, task_id)
        if task is None or task.tenant_id != self.tenant_id:
            raise NotFound("Template task", task_id)
        for field, value in data.items():
            if value is not None:
                setattr(task, field, value)
        await self.db.commit()
        return task

    async def delete_template_task(self, task_id: uuid.UUID) -> None:
        """Remove a step from the standard sequence.

        A hard delete, unusually for this system: a template task references no
        money and no history. The schedules already generated from it are separate
        records and are untouched.
        """
        task = await self.db.get(ScheduleTemplateTask, task_id)
        if task is None or task.tenant_id != self.tenant_id:
            raise NotFound("Template task", task_id)
        await self.db.delete(task)
        await self.db.commit()

    async def reorder_template(
        self, template_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> ScheduleTemplate:
        """Renumber the sequence to the order given.

        Every task must appear. A partial reorder would leave gaps or duplicates,
        and the sequence is what the whole ripple depends on.
        """
        template = await self.templates.get(template_id)
        existing = {t.id for t in template.tasks}
        if set(task_ids) != existing:
            raise BusinessRuleViolation(
                "The reordered list does not match the template's tasks.",
                "Send every task exactly once - the sequence is what the schedule "
                "and its knock-on effects are built from.",
            )
        by_id = {t.id: t for t in template.tasks}
        for position, task_id in enumerate(task_ids, start=1):
            by_id[task_id].sequence = position
        await self.db.commit()
        return await self.templates.get(template_id)

    # ================================================ generation =====

    async def generate(
        self, job: Job, template_id: uuid.UUID, start: date | None = None
    ) -> list[JobTask]:
        """Build one job's task list from a template.

        Refused when a schedule already exists, rather than quietly producing a
        second set of dates for the same house - which is the sort of thing nobody
        notices until two trades turn up on the same morning.
        """
        template = await self.templates.get(template_id)
        if not template.tasks:
            raise BusinessRuleViolation(
                f"{template.name} has no tasks.",
                "Add the build sequence to the template first.",
            )

        existing = await self.tasks.count(job_id=job.id)
        if existing:
            raise Conflict(
                f"{job.job_number} already has a schedule of {existing} tasks."
            )

        if job.status is JobStatus.CANCELLED:
            raise BusinessRuleViolation(
                f"{job.job_number} was cancelled.", "There is nothing left to build."
            )

        holidays = await self._non_working()
        begin = next_working_day(start or job.start_date or date.today(), holidays)

        created: list[JobTask] = []
        for template_task in sorted(template.tasks, key=lambda t: t.sequence):
            # Offsets and durations are both working days, so the calendar
            # stretches around weekends rather than the trade being booked onto
            # one.
            task_start = (
                shift_working_days(begin, template_task.offset_days, holidays)
                if template_task.offset_days
                else begin
            )
            task_finish = add_working_days(
                task_start, template_task.duration_days, holidays
            )
            created.append(
                self.tasks.stage(
                    job_id=job.id,
                    sequence=template_task.sequence,
                    name=template_task.name,
                    scheduled_start=task_start,
                    scheduled_finish=task_finish,
                    duration_days=template_task.duration_days,
                    cost_code_id=template_task.cost_code_id,
                    vendor_id=template_task.vendor_id,
                    is_milestone=template_task.is_milestone,
                    # The first task is ready the day the job starts; the rest wait
                    # for the one in front of them.
                    status=(
                        TaskStatus.READY
                        if template_task.sequence
                        == min(t.sequence for t in template.tasks)
                        else TaskStatus.NOT_STARTED
                    ),
                )
            )

        # The forecast, not the promise. `projected_completion` is the baseline and
        # is left exactly where it was.
        finishes = [t.scheduled_finish for t in created if t.scheduled_finish]
        if finishes:
            job.forecast_completion = max(finishes)

        await self.db.commit()
        return await self.tasks.for_job(job.id)

    # =============================================== working it =====

    async def list_tasks(self, job_id: uuid.UUID) -> list[JobTask]:
        return await self.tasks.for_job(job_id)

    async def start_task(self, task_id: uuid.UUID, when: date | None = None) -> JobTask:
        """A trade is on site.

        Recorded separately from completion because the gap between them is the
        only honest way to know a task is running long before it finishes late.
        """
        task = await self.tasks.get(task_id)
        if task.status is TaskStatus.COMPLETE:
            raise Conflict(f"{task.name} is already complete.")
        if task.status is TaskStatus.SKIPPED:
            raise Conflict(f"{task.name} was skipped.")

        task.actual_start = when or date.today()
        task.status = TaskStatus.IN_PROGRESS
        task.blocked_reason = None
        await self.db.commit()
        return await self.tasks.get(task_id)

    async def block_task(self, task_id: uuid.UUID, reason: str) -> JobTask:
        """The trade cannot start, and why.

        `BLOCKED` existed as a status that nothing could set, so a superintendent
        had no way to record a waterlogged plot or materials that had not landed -
        and a schedule that cannot say why it stopped is one nobody trusts.
        """
        task = await self.tasks.get(task_id)
        if task.status in (TaskStatus.COMPLETE, TaskStatus.SKIPPED):
            raise Conflict(f"{task.name} is already {task.status.value}.")
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "A blocked task needs a reason.",
                "Somebody will ask why the trade did not turn up, and the answer "
                "should be on the task rather than in a phone call.",
            )
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = reason.strip()
        await self.db.commit()
        return await self.tasks.get(task_id)

    async def unblock_task(self, task_id: uuid.UUID) -> JobTask:
        task = await self.tasks.get(task_id)
        if task.status is not TaskStatus.BLOCKED:
            raise Conflict(f"{task.name} is not blocked.")
        task.status = TaskStatus.IN_PROGRESS if task.actual_start else TaskStatus.READY
        task.blocked_reason = None
        await self.db.commit()
        return await self.tasks.get(task_id)

    async def skip_task(self, task_id: uuid.UUID, reason: str) -> JobTask:
        """This house does not need this step.

        Distinct from completing it: a skipped task did not happen, and counting it
        as done would report work that nobody did.
        """
        task = await self.tasks.get(task_id)
        if task.status is TaskStatus.COMPLETE:
            raise Conflict(f"{task.name} is already complete.")
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "A skipped task needs a reason.",
                "It is part of the standard sequence, so leaving it out is a "
                "decision about this house.",
            )
        task.status = TaskStatus.SKIPPED
        task.notes = f"{task.notes or ''}\nSkipped: {reason.strip()}".strip()
        await self.db.commit()
        return await self.tasks.get(task_id)

    async def complete_task(
        self, task_id: uuid.UUID, actual_finish: date | None = None
    ) -> dict:
        """Mark a task done and push everything behind it by the slip.

        The ripple is what makes the schedule worth keeping. A superintendent
        marking framing three days late has to move the twenty trades behind it
        without anybody retyping dates, or the schedule goes stale and every date
        in the system becomes fiction.
        """
        task = await self.tasks.get(task_id)
        if task.status is TaskStatus.COMPLETE:
            raise Conflict(f"{task.name} is already complete.")
        if task.status is TaskStatus.SKIPPED:
            raise Conflict(f"{task.name} was skipped and did not happen.")

        finish = actual_finish or date.today()
        if finish > date.today():
            raise BusinessRuleViolation(
                "A task cannot be completed in the future.",
                "Record it on the day the trade finishes.",
            )
        if task.actual_start and finish < task.actual_start:
            raise BusinessRuleViolation(
                "The finish date is before the task started.",
                f"It started on {task.actual_start}.",
            )

        holidays = await self._non_working()
        task.actual_finish = finish
        if task.actual_start is None:
            task.actual_start = task.scheduled_start
        task.status = TaskStatus.COMPLETE
        task.blocked_reason = None

        # Calendar days: a week of delay is a week. Durations are planned in
        # working days, but a slip is felt in real ones.
        slip = (finish - task.scheduled_finish).days if task.scheduled_finish else 0

        downstream = await self.tasks.downstream_of(task)
        if slip > 0:
            for t in downstream:
                if t.scheduled_start:
                    t.scheduled_start = next_working_day(
                        t.scheduled_start + timedelta(days=slip), holidays
                    )
                if t.scheduled_finish:
                    t.scheduled_finish = next_working_day(
                        t.scheduled_finish + timedelta(days=slip), holidays
                    )

            job = await self.db.get(Job, task.job_id)
            if job is not None:
                # The forecast moves. The baseline does not - that is the whole
                # point of having both, and moving the promise meant the job was
                # never late because the target retreated ahead of the slip.
                finishes = [t.scheduled_finish for t in downstream if t.scheduled_finish]
                if finishes:
                    job.forecast_completion = max(finishes)

        # The next task in sequence becomes the trade's to start.
        if downstream and downstream[0].status is TaskStatus.NOT_STARTED:
            downstream[0].status = TaskStatus.READY

        await self.db.commit()
        refreshed = await self.tasks.get(task_id)
        return {
            "task_id": refreshed.id,
            "actual_finish": finish,
            "days_late": slip,
            "tasks_shifted": len(downstream) if slip > 0 else 0,
            "next_task_id": downstream[0].id if downstream else None,
        }

    async def reschedule_task(
        self, task_id: uuid.UUID, start: date, *, cascade: bool = True
    ) -> dict:
        """Move one task, and optionally everything behind it.

        Cascading is the default because a task that moves on its own leaves the
        next trade booked for a day the site will not be ready - which is exactly
        how a schedule loses people's trust.
        """
        task = await self.tasks.get(task_id)
        if task.status in (TaskStatus.COMPLETE, TaskStatus.SKIPPED):
            raise Conflict(f"{task.name} is already {task.status.value}.")

        holidays = await self._non_working()
        old_start = task.scheduled_start
        task.scheduled_start = next_working_day(start, holidays)
        task.scheduled_finish = add_working_days(
            task.scheduled_start, task.duration_days, holidays
        )

        shifted = 0
        if cascade and old_start:
            moved = (task.scheduled_start - old_start).days
            if moved:
                for t in await self.tasks.downstream_of(task):
                    if t.scheduled_start:
                        t.scheduled_start = next_working_day(
                            t.scheduled_start + timedelta(days=moved), holidays
                        )
                    if t.scheduled_finish:
                        t.scheduled_finish = next_working_day(
                            t.scheduled_finish + timedelta(days=moved), holidays
                        )
                    shifted += 1

        await self._refresh_forecast(task.job_id)
        await self.db.commit()
        return {"task_id": task.id, "tasks_shifted": shifted}

    async def assign_trade(self, task_id: uuid.UUID, vendor_id: uuid.UUID | None) -> JobTask:
        """Put a trade on a task, or take one off."""
        task = await self.tasks.get(task_id)
        if vendor_id:
            from app.repositories.catalog import VendorRepository

            await VendorRepository(self.db, self.tenant_id).require_exists(vendor_id)
        task.vendor_id = vendor_id
        await self.db.commit()
        return await self.tasks.get(task_id)

    async def _refresh_forecast(self, job_id: uuid.UUID) -> None:
        latest = await self.db.scalar(
            select(func.max(JobTask.scheduled_finish)).where(
                JobTask.tenant_id == self.tenant_id, JobTask.job_id == job_id
            )
        )
        job = await self.db.get(Job, job_id)
        if job is not None and latest:
            job.forecast_completion = latest

    # ================================================= the queues =====

    async def lookahead(self, days: int = LOOKAHEAD_DAYS, **filters) -> list[JobTask]:
        """What is due in the next fortnight, across every house.

        The question a superintendent actually asks, and there was no way to ask
        it: the only view was one job's task list, so planning the week meant
        opening eight of them.
        """
        horizon = date.today() + timedelta(days=days)
        stmt = (
            self.tasks._base()
            .where(
                JobTask.status.in_(OPEN_TASKS),
                JobTask.scheduled_start.is_not(None),
                JobTask.scheduled_start <= horizon,
            )
            .order_by(JobTask.scheduled_start, JobTask.sequence)
        )
        for field, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(JobTask, field) == value)
        return list(await self.db.scalars(stmt))

    async def behind(self, **filters) -> list[JobTask]:
        """Tasks that should have finished and have not."""
        stmt = (
            self.tasks._base()
            .where(
                JobTask.status.in_(OPEN_TASKS),
                JobTask.scheduled_finish.is_not(None),
                JobTask.scheduled_finish < date.today(),
            )
            .order_by(JobTask.scheduled_finish)
        )
        for field, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(JobTask, field) == value)
        return list(await self.db.scalars(stmt))

    async def summary(self) -> dict:
        """The numbers above the schedule."""
        today = date.today()
        horizon = today + timedelta(days=LOOKAHEAD_DAYS)

        async def n(*conditions) -> int:
            return await self.db.scalar(
                select(func.count())
                .select_from(JobTask)
                .where(JobTask.tenant_id == self.tenant_id, *conditions)
            ) or 0

        blocked = await n(JobTask.status == TaskStatus.BLOCKED)
        overdue = await n(
            JobTask.status.in_(OPEN_TASKS),
            JobTask.scheduled_finish.is_not(None),
            JobTask.scheduled_finish < today,
        )
        due_soon = await n(
            JobTask.status.in_(OPEN_TASKS),
            JobTask.scheduled_start.is_not(None),
            JobTask.scheduled_start >= today,
            JobTask.scheduled_start <= horizon,
        )
        in_progress = await n(JobTask.status == TaskStatus.IN_PROGRESS)

        templates = await self.db.scalar(
            select(func.count())
            .select_from(ScheduleTemplate)
            .where(ScheduleTemplate.tenant_id == self.tenant_id)
        ) or 0

        return {
            "templates": templates,
            "in_progress": in_progress,
            "due_soon": due_soon,
            "overdue": overdue,
            "blocked": blocked,
        }

    # ================================================== holidays =====

    async def list_holidays(self) -> list[Holiday]:
        return await self.holidays.all_ordered()

    async def add_holiday(self, on_date: date, name: str) -> Holiday:
        if await self.db.scalar(
            select(Holiday.id).where(
                Holiday.tenant_id == self.tenant_id, Holiday.on_date == on_date
            )
        ):
            raise Conflict(f"{on_date} is already a non-working day.")
        return await self.holidays.create(on_date=on_date, name=name)

    async def remove_holiday(self, holiday_id: uuid.UUID) -> None:
        """Take a day back.

        Deliberately does not reschedule anything. Existing dates were agreed with
        trades who have the old day in their diaries, and silently moving twenty
        houses because somebody corrected a typo would be worse than the typo.
        """
        holiday = await self.holidays.get(holiday_id)
        await self.db.delete(holiday)
        await self.db.commit()
