"""The build schedule: templates, one house's programme, and what is due.

Durations here are **working days**. They were calendar days, so a ten-day task
starting on a Thursday finished on a Sunday - and across a 115-day build the error
compounded to roughly a third of the programme.

A slip moves the job's **forecast**, never its baseline. Moving the baseline meant
the job was never late, because the target retreated ahead of the slip.

The API tier and nothing more. Rules live in `app.services.schedule`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequireStaff, get_tenant_scoped
from app.models.job import Job
from app.schemas.schedule import (
    AssignTradeRequest,
    BlockRequest,
    CompleteRequest,
    DuplicateTemplate,
    GenerateRequest,
    HolidayCreate,
    HolidayRead,
    JobTaskRead,
    ReorderRequest,
    RescheduleRequest,
    RescheduleResult,
    ScheduleSummary,
    SkipRequest,
    StartRequest,
    TaskCompletion,
    TemplateCreate,
    TemplateRead,
    TemplateTaskCreate,
    TemplateTaskRead,
    TemplateTaskUpdate,
    TemplateUpdate,
)
from app.services.schedule import ScheduleService

router = APIRouter(tags=["schedule"])
CREATED = status.HTTP_201_CREATED


# ================================================================ overview =====


@router.get("/schedule/summary", response_model=ScheduleSummary)
async def schedule_summary(db: DbSession, user: RequireStaff):
    """The numbers above the schedule, in one request."""
    return await ScheduleService(db, user.tenant_id).summary()


@router.get("/schedule/lookahead", response_model=list[JobTaskRead])
async def lookahead(
    db: DbSession,
    user: RequireStaff,
    days: Annotated[int, Query(ge=1, le=90, description="How far ahead to look.")] = 14,
    vendor_id: Annotated[uuid.UUID | None, Query(description="One trade.")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """What is due in the next fortnight, across every house.

    The question a superintendent actually asks. There was no way to ask it: the
    only view was one job's task list, so planning the week meant opening eight of
    them.
    """
    return await ScheduleService(db, user.tenant_id).lookahead(
        days, vendor_id=vendor_id, job_id=job_id
    )


@router.get("/schedule/behind", response_model=list[JobTaskRead])
async def behind(
    db: DbSession,
    user: RequireStaff,
    vendor_id: Annotated[uuid.UUID | None, Query(description="One trade.")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """Tasks that should have finished and have not, worst first."""
    return await ScheduleService(db, user.tenant_id).behind(vendor_id=vendor_id, job_id=job_id)


# =============================================================== templates =====


@router.get("/schedule-templates", response_model=list[TemplateRead])
async def list_templates(db: DbSession, user: RequireStaff):
    return await ScheduleService(db, user.tenant_id).list_templates()


@router.post("/schedule-templates", response_model=TemplateRead, status_code=CREATED)
async def create_template(payload: TemplateCreate, db: DbSession, user: RequireStaff):
    return await ScheduleService(db, user.tenant_id).create_template(**payload.model_dump())


@router.get("/schedule-templates/{template_id}", response_model=TemplateRead)
async def get_template(template_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await ScheduleService(db, user.tenant_id).get_template(template_id)


@router.patch("/schedule-templates/{template_id}", response_model=TemplateRead)
async def update_template(
    template_id: uuid.UUID, payload: TemplateUpdate, db: DbSession, user: RequireStaff
):
    return await ScheduleService(db, user.tenant_id).update_template(
        template_id, **payload.model_dump(exclude_unset=True)
    )


@router.post(
    "/schedule-templates/{template_id}/duplicate", response_model=TemplateRead, status_code=CREATED
)
async def duplicate_template(
    template_id: uuid.UUID, payload: DuplicateTemplate, db: DbSession, user: RequireStaff
):
    """Copy a template and its tasks.

    A builder's second house type is usually the first with three steps changed,
    and retyping forty tasks to get there is how a template ends up wrong.
    """
    return await ScheduleService(db, user.tenant_id).duplicate_template(
        template_id, payload.code, payload.name
    )


@router.post(
    "/schedule-templates/{template_id}/tasks", response_model=TemplateTaskRead, status_code=CREATED
)
async def add_template_task(
    template_id: uuid.UUID, payload: TemplateTaskCreate, db: DbSession, user: RequireStaff
):
    """Add a step. Appended to the end unless a sequence is given - two tasks at
    the same sequence generate a schedule whose order depends on the database."""
    return await ScheduleService(db, user.tenant_id).add_template_task(
        template_id, **payload.model_dump()
    )


@router.patch("/schedule-templates/tasks/{task_id}", response_model=TemplateTaskRead)
async def update_template_task(
    task_id: uuid.UUID, payload: TemplateTaskUpdate, db: DbSession, user: RequireStaff
):
    return await ScheduleService(db, user.tenant_id).update_template_task(
        task_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/schedule-templates/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template_task(task_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Remove a step from the standard sequence.

    Schedules already generated from this template are separate records and are
    untouched.
    """
    await ScheduleService(db, user.tenant_id).delete_template_task(task_id)


@router.post("/schedule-templates/{template_id}/reorder", response_model=TemplateRead)
async def reorder_template(
    template_id: uuid.UUID, payload: ReorderRequest, db: DbSession, user: RequireStaff
):
    """Renumber the sequence. Every task must appear exactly once."""
    return await ScheduleService(db, user.tenant_id).reorder_template(template_id, payload.task_ids)


# =========================================================== one house =====


@router.post("/jobs/{job_id}/schedule", response_model=list[JobTaskRead], status_code=CREATED)
async def generate_schedule(
    job_id: uuid.UUID, payload: GenerateRequest, db: DbSession, user: RequireStaff
):
    """Generate this home's programme from a template.

    Refused when a schedule already exists, rather than quietly producing a second
    set of dates for the same house - the sort of thing nobody notices until two
    trades turn up on the same morning.
    """
    job = await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    return await ScheduleService(db, user.tenant_id).generate(
        job, payload.template_id, payload.start_date
    )


@router.get("/jobs/{job_id}/schedule", response_model=list[JobTaskRead])
async def job_schedule(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    return await ScheduleService(db, user.tenant_id).list_tasks(job_id)


# ================================================================= tasks =====


@router.post("/tasks/{task_id}/start", response_model=JobTaskRead)
async def start_task(
    task_id: uuid.UUID, db: DbSession, user: RequireStaff, payload: StartRequest | None = None
):
    """A trade is on site.

    Recorded separately from completion, because the gap between the two is the
    only honest way to know a task is running long before it finishes late.
    """
    return await ScheduleService(db, user.tenant_id).start_task(
        task_id, payload.actual_start if payload else None
    )


@router.post("/tasks/{task_id}/block", response_model=JobTaskRead)
async def block_task(task_id: uuid.UUID, payload: BlockRequest, db: DbSession, user: RequireStaff):
    """The trade cannot start, and why.

    A schedule that cannot say why it stopped is one nobody trusts.
    """
    return await ScheduleService(db, user.tenant_id).block_task(task_id, payload.reason)


@router.post("/tasks/{task_id}/unblock", response_model=JobTaskRead)
async def unblock_task(task_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await ScheduleService(db, user.tenant_id).unblock_task(task_id)


@router.post("/tasks/{task_id}/skip", response_model=JobTaskRead)
async def skip_task(task_id: uuid.UUID, payload: SkipRequest, db: DbSession, user: RequireStaff):
    """This house does not need this step.

    Distinct from completing it: a skipped task did not happen, and counting it as
    done would report work nobody did.
    """
    return await ScheduleService(db, user.tenant_id).skip_task(task_id, payload.reason)


@router.post("/tasks/{task_id}/complete", response_model=TaskCompletion)
async def complete_task(
    task_id: uuid.UUID, db: DbSession, user: RequireStaff, payload: CompleteRequest | None = None
):
    """Mark it done and push everything behind it by the slip.

    The ripple is what makes the schedule worth keeping: a superintendent marking
    framing three days late has to move the twenty trades behind it without
    anybody retyping dates.
    """
    return await ScheduleService(db, user.tenant_id).complete_task(
        task_id, payload.actual_finish if payload else None
    )


@router.post("/tasks/{task_id}/reschedule", response_model=RescheduleResult)
async def reschedule_task(
    task_id: uuid.UUID, payload: RescheduleRequest, db: DbSession, user: RequireStaff
):
    """Move a task, and by default everything behind it."""
    return await ScheduleService(db, user.tenant_id).reschedule_task(
        task_id, payload.scheduled_start, cascade=payload.cascade
    )


@router.post("/tasks/{task_id}/trade", response_model=JobTaskRead)
async def assign_trade(
    task_id: uuid.UUID, payload: AssignTradeRequest, db: DbSession, user: RequireStaff
):
    """Put a trade on the task. A null id takes them off."""
    return await ScheduleService(db, user.tenant_id).assign_trade(task_id, payload.vendor_id)


# ============================================================== holidays =====


@router.get("/schedule/holidays", response_model=list[HolidayRead])
async def list_holidays(db: DbSession, user: RequireStaff):
    """Days this builder does not work, beyond weekends."""
    return await ScheduleService(db, user.tenant_id).list_holidays()


@router.post("/schedule/holidays", response_model=HolidayRead, status_code=CREATED)
async def add_holiday(payload: HolidayCreate, db: DbSession, user: RequireStaff):
    """Add a non-working day. Schedules generated afterwards stretch around it."""
    return await ScheduleService(db, user.tenant_id).add_holiday(payload.on_date, payload.name)


@router.delete("/schedule/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_holiday(holiday_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Take a day back.

    Deliberately does not reschedule anything: existing dates were agreed with
    trades who have the old day in their diaries, and silently moving twenty houses
    because somebody corrected a typo would be worse than the typo.
    """
    await ScheduleService(db, user.tenant_id).remove_holiday(holiday_id)
