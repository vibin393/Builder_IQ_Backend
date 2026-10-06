"""RFIs: the formal question-and-answer chain, with a thread and an audit trail.

The concept that carries the module is **ball in court** — at any moment exactly one
party owes the next move, and every record says who. An RFI opens in the builder's
court, gathers responses from whoever can answer, and closes only once an official
answer has been recorded and signed off. A costly answer can become a priced change
order without anyone retyping what was decided.

  POST /rfis/{id}/responses      add to the thread; the official one answers it
  POST /rfis/{id}/answer         the one-shot official answer (kept for the simple path)
  POST /rfis/{id}/change-order   turn a costly answer into money against a named party
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    RequireDesignTeam,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.change_order import ChangeOrder, ChangeOrderLine, ChangeOrderStatus
from app.models.communication import RFI as RFIModel
from app.models.communication import (
    RFI_CLOSED_STATUSES,
    BallInCourt,
    Priority,
    RFICategory,
    RFIStatus,
)
from app.models.cost_code import CostCode
from app.models.job import Job
from app.models.rfi import RFIActivityKind, RFIResponse
from app.models.vendor import Vendor
from app.schemas.rfi import (
    RFIAnswer,
    RFIChangeOrderRequest,
    RFICreate,
    RFIRead,
    RFIReassign,
    RFIResponseCreate,
    RFISummary,
    RFIUpdate,
)
from app.services import rfi as rfi_service

router = APIRouter(prefix="/rfis", tags=["rfis"])


# --- helpers ------------------------------------------------------------------


async def _next_number(db, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count()).select_from(RFIModel).where(RFIModel.tenant_id == tenant_id)
    )
    return f"RFI{(count or 0) + 1:05d}"


async def _load(db, tenant_id: uuid.UUID, rfi_id: uuid.UUID) -> RFIModel:
    # populate_existing so the thread and the log re-load after a write in the same
    # request; without it the identity-mapped object keeps its first eager-loaded
    # collections and a just-added response or activity entry goes missing.
    obj = await db.scalar(
        select(RFIModel)
        .where(RFIModel.id == rfi_id, RFIModel.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFI not found")
    return obj


# --- analytics ----------------------------------------------------------------


@router.get("/summary", response_model=RFISummary)
async def summary(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
):
    """The board the coordinator reads first: what is open, what is late, what is
    stopping work, and what we owe an answer on."""
    stmt = select(RFIModel).where(RFIModel.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(RFIModel.job_id == job_id)
    rows = list(await db.scalars(stmt))

    today = date.today()
    week = today + timedelta(days=7)
    by_status: dict[str, int] = {}
    by_court: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    aging = dict.fromkeys(rfi_service.AGING_BUCKETS, 0)

    open_count = answered = closed = overdue = blocking = ours = unassigned = due_week = 0
    answer_spans: list[int] = []

    for r in rows:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        by_court[r.ball_in_court.value] = by_court.get(r.ball_in_court.value, 0) + 1
        by_category[r.category.value] = by_category.get(r.category.value, 0) + 1
        by_priority[r.priority.value] = by_priority.get(r.priority.value, 0) + 1

        if r.days_to_answer is not None:
            answer_spans.append(r.days_to_answer)

        if r.status is RFIStatus.ANSWERED:
            answered += 1
        if r.status is RFIStatus.CLOSED:
            closed += 1

        is_live = r.status in (RFIStatus.OPEN, RFIStatus.DRAFT)
        if is_live:
            open_count += 1
            aging[rfi_service.aging_bucket(r.days_open)] += 1
            if r.is_overdue:
                overdue += 1
            if r.is_blocking:
                blocking += 1
            if r.ball_in_court is BallInCourt.BUILDER:
                ours += 1
            if r.assigned_vendor_id is None:
                unassigned += 1
            if r.due_on and today <= r.due_on <= week:
                due_week += 1

    avg = round(sum(answer_spans) / len(answer_spans), 1) if answer_spans else None

    return RFISummary(
        total=len(rows),
        open=open_count,
        answered=answered,
        closed=closed,
        overdue=overdue,
        blocking=blocking,
        in_our_court=ours,
        unassigned=unassigned,
        due_this_week=due_week,
        avg_response_days=avg,
        by_status=by_status,
        by_court=by_court,
        by_category=by_category,
        by_priority=by_priority,
        aging=aging,
    )


# --- list / create ------------------------------------------------------------


@router.get("", response_model=list[RFIRead])
async def list_rfis(
    db: DbSession,
    user: RequireDesignTeam,
    job_id: uuid.UUID | None = None,
    status_filter: RFIStatus | None = Query(default=None, alias="status"),
    ball_in_court: BallInCourt | None = None,
    priority: Priority | None = None,
    category: RFICategory | None = None,
    overdue_only: bool = False,
    blocking_only: bool = False,
):
    stmt = select(RFIModel).where(RFIModel.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(RFIModel.job_id == job_id)
    if status_filter is not None:
        stmt = stmt.where(RFIModel.status == status_filter)
    if ball_in_court is not None:
        stmt = stmt.where(RFIModel.ball_in_court == ball_in_court)
    if priority is not None:
        stmt = stmt.where(RFIModel.priority == priority)
    if category is not None:
        stmt = stmt.where(RFIModel.category == category)
    if blocking_only:
        stmt = stmt.where(RFIModel.is_blocking.is_(True))

    rows = list(await db.scalars(stmt.order_by(RFIModel.rfi_number)))
    if overdue_only:
        rows = [r for r in rows if r.is_overdue]
    return rows


@router.post("", response_model=RFIRead, status_code=status.HTTP_201_CREATED)
async def raise_rfi(payload: RFICreate, db: DbSession, user: RequireStaff):
    """Raise a question. It opens immediately, with a response due date set from the
    priority when none was given, and the ball in the court that owes the answer."""
    await get_tenant_scoped(db, Job, user.tenant_id, payload.job_id)
    if payload.assigned_vendor_id:
        await get_tenant_scoped(db, Vendor, user.tenant_id, payload.assigned_vendor_id)

    today = date.today()
    data = payload.model_dump()
    ball = data.pop("ball_in_court")
    obj = RFIModel(
        tenant_id=user.tenant_id,
        rfi_number=await _next_number(db, user.tenant_id),
        raised_by_id=user.id,
        raised_on=today,
        status=RFIStatus.OPEN,
        ball_in_court=ball,
        **data,
    )
    obj.due_on = obj.due_on or rfi_service.due_date_for(today, obj.priority)
    db.add(obj)
    await db.flush()
    db.add(
        rfi_service.log(
            user.tenant_id, obj.id, RFIActivityKind.RAISED,
            f"Raised, ball with {ball.value}.", user.id,
        )
    )
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.get("/{rfi_id}", response_model=RFIRead)
async def get_rfi(rfi_id: uuid.UUID, db: DbSession, user: RequireDesignTeam):
    return await _load(db, user.tenant_id, rfi_id)


@router.patch("/{rfi_id}", response_model=RFIRead)
async def update_rfi(rfi_id: uuid.UUID, payload: RFIUpdate, db: DbSession, user: RequireStaff):
    obj = await _load(db, user.tenant_id, rfi_id)
    data = payload.model_dump(exclude_unset=True)
    if payload.assigned_vendor_id:
        await get_tenant_scoped(db, Vendor, user.tenant_id, payload.assigned_vendor_id)

    court_changed = "ball_in_court" in data and data["ball_in_court"] != obj.ball_in_court
    for field, value in data.items():
        setattr(obj, field, value)

    if court_changed:
        db.add(
            rfi_service.log(
                user.tenant_id, obj.id, RFIActivityKind.REASSIGNED,
                f"Ball moved to {obj.ball_in_court.value}.", user.id,
            )
        )
    else:
        db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.EDITED, None, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{rfi_id}/reassign", response_model=RFIRead)
async def reassign_rfi(
    rfi_id: uuid.UUID, payload: RFIReassign, db: DbSession, user: RequireStaff
):
    """Move the ball to another court. The reason, if given, joins the log."""
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status in RFI_CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This RFI is {obj.status.value}; there is no ball to move.",
        )
    obj.ball_in_court = payload.ball_in_court
    detail = f"Ball moved to {payload.ball_in_court.value}."
    if payload.note:
        detail += f" {payload.note}"
    db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.REASSIGNED, detail, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


# --- thread -------------------------------------------------------------------


def _apply_official(obj: RFIModel, resp: RFIResponse, user_id: uuid.UUID) -> None:
    """Mirror an official response onto the RFI: it becomes the answer, the clock
    stops, and the ball goes back to whoever raised it."""
    obj.answer = resp.body
    obj.cost_impact = resp.cost_impact
    obj.schedule_impact_days = resp.schedule_impact_days
    obj.status = RFIStatus.ANSWERED
    obj.answered_on = date.today()
    obj.answered_by_id = user_id
    obj.ball_in_court = BallInCourt.TRADE if obj.assigned_vendor_id else BallInCourt.BUILDER


@router.post(
    "/{rfi_id}/responses", response_model=RFIRead, status_code=status.HTTP_201_CREATED
)
async def add_response(
    rfi_id: uuid.UUID, payload: RFIResponseCreate, db: DbSession, user: RequireDesignTeam
):
    """Add a response to the thread. Marking it official records it as the answer and
    hands the ball back; there can only be one official response at a time."""
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status in RFI_CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This RFI is {obj.status.value}; reopen it before adding a response.",
        )

    resp = RFIResponse(
        tenant_id=user.tenant_id,
        rfi_id=obj.id,
        author_id=user.id,
        court=payload.court,
        body=payload.body,
        is_official=payload.is_official,
        cost_impact=payload.cost_impact,
        schedule_impact_days=payload.schedule_impact_days,
    )
    db.add(resp)

    if payload.is_official:
        # Only one official answer stands: demote any earlier one.
        for prior in obj.responses:
            prior.is_official = False
        _apply_official(obj, resp, user.id)
        db.add(
            rfi_service.log(
                user.tenant_id, obj.id, RFIActivityKind.ANSWERED,
                f"Official answer from {payload.court.value}.", user.id,
            )
        )
    else:
        db.add(
            rfi_service.log(
                user.tenant_id, obj.id, RFIActivityKind.RESPONDED,
                f"Response from {payload.court.value}.", user.id,
            )
        )

    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{rfi_id}/answer", response_model=RFIRead)
async def answer_rfi(rfi_id: uuid.UUID, payload: RFIAnswer, db: DbSession, user: RequireDesignTeam):
    """Answer it in one shot. The ball goes back to whoever asked, and the clock stops.

    A convenience over posting an official response: it records the same official
    response in the thread, so the two paths cannot tell a different story later.
    """
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status in (RFIStatus.CLOSED, RFIStatus.VOID):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"This RFI is {obj.status.value}."
        )

    for prior in obj.responses:
        prior.is_official = False
    resp = RFIResponse(
        tenant_id=user.tenant_id,
        rfi_id=obj.id,
        author_id=user.id,
        court=BallInCourt.ARCHITECT,
        body=payload.answer,
        is_official=True,
        cost_impact=payload.cost_impact,
        schedule_impact_days=payload.schedule_impact_days,
    )
    db.add(resp)
    _apply_official(obj, resp, user.id)
    db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.ANSWERED, None, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


# --- lifecycle ----------------------------------------------------------------


@router.post("/{rfi_id}/close", response_model=RFIRead)
async def close_rfi(rfi_id: uuid.UUID, db: DbSession, user: RequireStaff):
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status is not RFIStatus.ANSWERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An RFI must be answered before it can be closed.",
        )
    obj.status = RFIStatus.CLOSED
    obj.ball_in_court = BallInCourt.CLOSED
    obj.closed_on = date.today()
    db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.CLOSED, None, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{rfi_id}/reopen", response_model=RFIRead)
async def reopen_rfi(rfi_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Put an answered or closed RFI back in play — the answer did not hold, or a new
    question rode in on the old one. The thread and the original answer are kept."""
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status not in (RFIStatus.ANSWERED, RFIStatus.CLOSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only an answered or closed RFI can be reopened; this one is {obj.status.value}."
            ),
        )
    obj.status = RFIStatus.OPEN
    obj.ball_in_court = BallInCourt.BUILDER
    obj.closed_on = None
    db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.REOPENED, None, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{rfi_id}/void", response_model=RFIRead)
async def void_rfi(rfi_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Withdraw a question that should not have been asked — a duplicate, or one
    overtaken by events. Kept, not deleted, so the numbering stays honest."""
    obj = await _load(db, user.tenant_id, rfi_id)
    if obj.status is RFIStatus.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A closed RFI is already settled; it cannot be voided.",
        )
    obj.status = RFIStatus.VOID
    obj.ball_in_court = BallInCourt.CLOSED
    db.add(rfi_service.log(user.tenant_id, obj.id, RFIActivityKind.VOIDED, None, user.id))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


# --- change order from an answer ----------------------------------------------


async def _next_co_number(db, tenant_id: uuid.UUID) -> str:
    """Next CO number for the tenant. Mirrors the one in operations.py rather than
    importing it, so the two routers stay independent."""
    last = await db.scalar(
        select(ChangeOrder.co_number)
        .where(ChangeOrder.tenant_id == tenant_id)
        .order_by(ChangeOrder.co_number.desc())
        .limit(1)
    )
    nxt = (int(last.removeprefix("CO")) + 1) if last else 1
    return f"CO{nxt:05d}"


@router.post("/{rfi_id}/change-order", status_code=status.HTTP_201_CREATED)
async def raise_change_order_from_rfi(
    rfi_id: uuid.UUID,
    payload: RFIChangeOrderRequest,
    db: DbSession,
    user: RequireStaff,
):
    """Turn an answered RFI that costs money into a change order.

    This join is the thing neither source product can make. Procore has RFIs but no
    buyer to bill, so a costly answer lands in a report. A homebuilder system has the
    buyer and the contract but no RFIs, so the question never got asked in a tracked
    way. Here the answer carries a cost, and the cost becomes a priced change against a
    named party — with the RFI quoted as the reason, so six months later it is still
    obvious why the price moved.
    """
    rfi = await _load(db, user.tenant_id, rfi_id)

    if rfi.status not in (RFIStatus.ANSWERED, RFIStatus.CLOSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Answer the RFI first - a change order needs to say what was decided.",
        )
    if not rfi.cost_impact and not payload.cost_lines and payload.price_delta == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This RFI was answered with no cost impact. Raise a change order only "
                "when the answer actually moves money."
            ),
        )

    for line in payload.cost_lines:
        await get_tenant_scoped(db, CostCode, user.tenant_id, line.cost_code_id)

    co = ChangeOrder(
        tenant_id=user.tenant_id,
        co_number=await _next_co_number(db, user.tenant_id),
        job_id=rfi.job_id,
        description=f"{rfi.rfi_number}: {rfi.subject}",
        reason=payload.reason or (rfi.answer or "Raised from an RFI answer."),
        responsible_party=payload.responsible_party,
        price_delta=payload.price_delta,
        schedule_days=rfi.schedule_impact_days,
        status=ChangeOrderStatus.PENDING_APPROVAL,
        requested_by_id=user.id,
    )
    db.add(co)
    await db.flush()

    for line in payload.cost_lines:
        db.add(
            ChangeOrderLine(
                tenant_id=user.tenant_id,
                change_order_id=co.id,
                cost_code_id=line.cost_code_id,
                amount=line.amount,
                description=line.description,
            )
        )

    db.add(
        rfi_service.log(
            user.tenant_id, rfi.id, RFIActivityKind.CHANGE_ORDER,
            f"{co.co_number} raised from the answer.", user.id,
        )
    )
    await db.commit()
    await db.refresh(co)
    return {
        "change_order_id": co.id,
        "co_number": co.co_number,
        "job_id": co.job_id,
        "rfi_number": rfi.rfi_number,
        "schedule_days": co.schedule_days,
        "responsible_party": co.responsible_party.value,
        "detail": (
            f"{co.co_number} raised from {rfi.rfi_number}, pending approval. "
            "Nothing moves until it is approved."
        ),
    }
