"""Field operations: the punch list, inspections and the daily log.

The superintendent's half of the system. Three rules here are worth knowing before
reading the routes:

  * a defect reaches `complete` only through `ready_for_review`, so every closed
    one has been checked by somebody other than whoever claims to have fixed it
  * a failed inspection turns into a punch list in one step, because retyping
    thirty failed checks is how three of them get missed
  * defects that are not merely cosmetic stop a handover, which is enforced over
    in the Homes close route

The API tier and nothing more. Rules live in `app.services.field`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireStaff
from app.models.field import InspectionResult, InspectionType, PunchSeverity, PunchStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.field import (
    AssignPunch,
    CheckCreate,
    CheckRead,
    CheckResult,
    DailyLogCreate,
    DailyLogRead,
    FieldSummary,
    InspectionCreate,
    InspectionOutcome,
    InspectionRead,
    PunchCreate,
    PunchNote,
    PunchRead,
    PunchReason,
    PunchUpdate,
    ReinspectionRequest,
)
from app.services.field import FieldService

router = APIRouter(prefix="/field", tags=["field operations"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/summary", response_model=FieldSummary)
async def field_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """The numbers above the screen, for one home or all of them."""
    return await FieldService(db, user.tenant_id).summary(job_id)


# ============================================================= punch list =====


@router.get("/punch-items", response_model=Page[PunchRead])
async def list_punch(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    status_filter: Annotated[
        PunchStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    severity: Annotated[PunchSeverity | None, Query(description="How much it matters.")] = None,
    vendor_id: Annotated[uuid.UUID | None, Query(description="One trade.")] = None,
):
    filters = {
        k: v
        for k, v in {
            "job_id": job_id,
            "status": status_filter,
            "severity": severity,
            "vendor_id": vendor_id,
        }.items()
        if v is not None
    }
    items, total = await FieldService(db, user.tenant_id).list_punch(params, **filters)
    return _page(items, params, total)


@router.get("/punch-items/blocking", response_model=list[PunchRead])
async def blocking_handover(
    job_id: Annotated[uuid.UUID, Query(description="The home.")],
    db: DbSession,
    user: RequireStaff,
):
    """Defects that stop this home being handed over.

    Cosmetic ones are excluded: a buyer will take the house and a decorator comes
    back. Anything above that is here, and the Homes close route refuses while any
    of them are open.
    """
    return await FieldService(db, user.tenant_id).handover_blockers(job_id)


@router.post("/punch-items", response_model=PunchRead, status_code=CREATED)
async def create_punch(payload: PunchCreate, db: DbSession, user: RequireStaff):
    return await FieldService(db, user.tenant_id).create_punch(
        actor_id=user.id, **payload.model_dump()
    )


@router.patch("/punch-items/{item_id}", response_model=PunchRead)
async def update_punch(item_id: uuid.UUID, payload: PunchUpdate, db: DbSession, user: RequireStaff):
    """Correct a defect. Status is not settable here - it moves through the routes
    below, which record who accepted the fix."""
    return await FieldService(db, user.tenant_id).update_punch(
        item_id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/punch-items/{item_id}/assign", response_model=PunchRead)
async def assign_punch(item_id: uuid.UUID, payload: AssignPunch, db: DbSession, user: RequireStaff):
    """Give the defect to a trade.

    Assigning it is what lets the work be backcharged rather than absorbed, and it
    puts the ball in their court - an item nobody owns is the kind that sits open
    until handover week.
    """
    return await FieldService(db, user.tenant_id).assign_punch(
        item_id, payload.vendor_id, payload.due_date
    )


@router.post("/punch-items/{item_id}/submit", response_model=PunchRead)
async def submit_punch(
    item_id: uuid.UUID, db: DbSession, user: RequireStaff, payload: PunchNote | None = None
):
    """The trade says it is done. A claim, not a conclusion - somebody checks next."""
    return await FieldService(db, user.tenant_id).submit_punch(
        item_id, payload.note if payload else None
    )


@router.post("/punch-items/{item_id}/verify", response_model=PunchRead)
async def verify_punch(item_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Sign the fix off.

    The step that was missing: without it a home could be handed over on a snag
    list the builder had only the trade's word for.
    """
    return await FieldService(db, user.tenant_id).verify_punch(item_id, actor_id=user.id)


@router.post("/punch-items/{item_id}/reject", response_model=PunchRead)
async def reject_punch(item_id: uuid.UUID, payload: PunchReason, db: DbSession, user: RequireStaff):
    """Send the fix back, with what is still wrong."""
    return await FieldService(db, user.tenant_id).reject_punch(item_id, payload.reason)


@router.post("/punch-items/{item_id}/wont-fix", response_model=PunchRead)
async def wont_fix(item_id: uuid.UUID, payload: PunchReason, db: DbSession, user: RequireStaff):
    """Decide not to fix it, on the record.

    Refused on anything marked blocking - that means life-safety or habitability,
    and the honest move is to downgrade the severity deliberately if it is wrong.
    """
    return await FieldService(db, user.tenant_id).wont_fix(item_id, payload.reason)


@router.post("/punch-items/{item_id}/reopen", response_model=PunchRead)
async def reopen_punch(item_id: uuid.UUID, payload: PunchReason, db: DbSession, user: RequireStaff):
    return await FieldService(db, user.tenant_id).reopen_punch(item_id, payload.reason)


# ============================================================ inspections =====


@router.get("/inspections", response_model=Page[InspectionRead])
async def list_inspections(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    result: Annotated[InspectionResult | None, Query(description="How it went.")] = None,
    inspection_type: Annotated[InspectionType | None, Query(description="What kind.")] = None,
):
    filters = {
        k: v
        for k, v in {
            "job_id": job_id,
            "result": result,
            "inspection_type": inspection_type,
        }.items()
        if v is not None
    }
    items, total = await FieldService(db, user.tenant_id).list_inspections(params, **filters)
    return _page(items, params, total)


@router.post("/inspections", response_model=InspectionRead, status_code=CREATED)
async def create_inspection(payload: InspectionCreate, db: DbSession, user: RequireStaff):
    return await FieldService(db, user.tenant_id).create_inspection(**payload.model_dump())


@router.post("/inspections/{inspection_id}/items", response_model=CheckRead, status_code=CREATED)
async def add_check(
    inspection_id: uuid.UUID, payload: CheckCreate, db: DbSession, user: RequireStaff
):
    """Add a line to the checklist.

    A framing inspection is thirty checks, not one verdict: recording the overall
    result alone tells you the house failed, recording the items tells you what to
    send the framer back for.
    """
    return await FieldService(db, user.tenant_id).add_check(inspection_id, **payload.model_dump())


@router.post("/inspections/items/{item_id}/result", response_model=CheckRead)
async def record_check(item_id: uuid.UUID, payload: CheckResult, db: DbSession, user: RequireStaff):
    """Record how one check went. A failure needs a note saying what to fix."""
    return await FieldService(db, user.tenant_id).record_check(
        item_id, payload.result, payload.notes
    )


@router.post("/inspections/{inspection_id}/result", response_model=InspectionRead)
async def record_result(
    inspection_id: uuid.UUID, payload: InspectionOutcome, db: DbSession, user: RequireStaff
):
    """Record how the inspection went."""
    return await FieldService(db, user.tenant_id).record_result(
        inspection_id,
        payload.result,
        failure_reason=payload.failure_reason,
        completed_date=payload.completed_date,
        fee=payload.fee,
        actor_id=user.id,
    )


@router.post(
    "/inspections/{inspection_id}/punch-from-failures",
    response_model=list[PunchRead],
    status_code=CREATED,
)
async def punch_from_failures(inspection_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Turn every failed check into a defect to fix.

    Retyping thirty failed checks by hand is how three of them get missed - and the
    three that get missed are the ones the inspector looks at first on the
    re-inspection. Running it twice will not duplicate the list.
    """
    return await FieldService(db, user.tenant_id).punch_from_failures(
        inspection_id, actor_id=user.id
    )


@router.post(
    "/inspections/{inspection_id}/reinspect",
    response_model=InspectionRead,
    status_code=CREATED,
)
async def schedule_reinspection(
    inspection_id: uuid.UUID, payload: ReinspectionRequest, db: DbSession, user: RequireStaff
):
    """Book the retry, pointed at the one it is retrying.

    The chain answers "how many times did this fail before it passed", which tells
    a builder whose work is costing them re-inspection fees.
    """
    return await FieldService(db, user.tenant_id).schedule_reinspection(
        inspection_id, payload.scheduled_date, fee=payload.fee
    )


# ============================================================= daily logs =====


@router.get("/daily-logs", response_model=Page[DailyLogRead])
async def list_logs(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    items, total = await FieldService(db, user.tenant_id).list_logs(params, job_id=job_id)
    return _page(items, params, total)


@router.post("/daily-logs", response_model=DailyLogRead, status_code=CREATED)
async def record_log(payload: DailyLogCreate, db: DbSession, user: RequireStaff):
    """File today's log, or add to the one already filed.

    An upsert: a superintendent adding the afternoon's events to the morning's
    entry is the normal case, and posting twice used to produce two logs for one
    day - which makes the record that is supposed to substantiate a delay claim
    contradict itself.
    """
    return await FieldService(db, user.tenant_id).record_log(
        actor_id=user.id, **payload.model_dump()
    )
