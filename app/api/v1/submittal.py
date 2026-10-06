"""Submittals: what a trade proposes to install, approved before it is ordered.

A submittal is raised (often against the buyer's chosen option), sent for review, and
returned as approved, approved-as-noted, rejected, or revise-and-resubmit — which hands
it back to the trade for a new revision. Every round is kept, so the history of what was
proposed and how it came back survives. The register works backwards from the date each
product is needed on site to flag the ones whose procurement is running out of runway,
which is the reason to keep a submittal log rather than a filing cabinet.

  POST /submittals/{id}/review     the reviewer's decision; revise-and-resubmit loops it
  POST /submittals/{id}/resubmit   the trade's corrected revision comes back
  GET  /submittals/summary         the register board, including procurement at-risk
"""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    RequireDesignTeam,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.communication import (
    SUBMITTAL_CLOSED_STATUSES,
    BallInCourt,
    Priority,
    Submittal,
    SubmittalStatus,
    SubmittalType,
)
from app.models.cost_code import CostCode
from app.models.job import Job
from app.models.option import Option
from app.models.plan import Plan
from app.models.submittal import SubmittalActivityKind, SubmittalRevision
from app.models.vendor import Vendor
from app.schemas.submittal import (
    SubmittalCreate,
    SubmittalRead,
    SubmittalReassign,
    SubmittalResubmit,
    SubmittalReview,
    SubmittalSummary,
    SubmittalUpdate,
)
from app.services import submittal as submittal_service

router = APIRouter(prefix="/submittals", tags=["submittals"])

DECISION_KIND = {
    SubmittalStatus.APPROVED: SubmittalActivityKind.APPROVED,
    SubmittalStatus.APPROVED_AS_NOTED: SubmittalActivityKind.APPROVED_AS_NOTED,
    SubmittalStatus.REVISE_AND_RESUBMIT: SubmittalActivityKind.REVISE_AND_RESUBMIT,
    SubmittalStatus.REJECTED: SubmittalActivityKind.REJECTED,
}
DECISIONS = tuple(DECISION_KIND)


# --- helpers ------------------------------------------------------------------


async def _next_number(db, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count()).select_from(Submittal).where(Submittal.tenant_id == tenant_id)
    )
    return f"SUB{(count or 0) + 1:05d}"


async def _load(db, tenant_id: uuid.UUID, sub_id: uuid.UUID) -> Submittal:
    # populate_existing so revisions and the log re-load after a write in the same
    # request; without it the identity-mapped object keeps its first eager-loaded
    # collections and a just-added row goes missing.
    obj = await db.scalar(
        select(Submittal)
        .where(Submittal.id == sub_id, Submittal.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submittal not found")
    return obj


async def _validate_links(db, tenant_id, payload) -> None:
    if getattr(payload, "job_id", None):
        await get_tenant_scoped(db, Job, tenant_id, payload.job_id)
    if getattr(payload, "plan_id", None):
        await get_tenant_scoped(db, Plan, tenant_id, payload.plan_id)
    if getattr(payload, "vendor_id", None):
        await get_tenant_scoped(db, Vendor, tenant_id, payload.vendor_id)
    if getattr(payload, "cost_code_id", None):
        await get_tenant_scoped(db, CostCode, tenant_id, payload.cost_code_id)
    if getattr(payload, "option_id", None):
        await get_tenant_scoped(db, Option, tenant_id, payload.option_id)


# --- analytics ----------------------------------------------------------------


@router.get("/summary", response_model=SubmittalSummary)
async def summary(db: DbSession, user: RequireStaff, job_id: uuid.UUID | None = None):
    """The register board: the review queue, what is overdue, what is being revised,
    and — the one the schedule cares about — what is running out of runway to procure."""
    stmt = select(Submittal).where(Submittal.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Submittal.job_id == job_id)
    rows = list(await db.scalars(stmt))

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_court: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    draft = awaiting = revising = approved = rejected = overdue = at_risk = with_trade = 0
    turnarounds: list[int] = []

    for s in rows:
        by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
        by_type[s.submittal_type.value] = by_type.get(s.submittal_type.value, 0) + 1
        by_court[s.ball_in_court.value] = by_court.get(s.ball_in_court.value, 0) + 1
        by_priority[s.priority.value] = by_priority.get(s.priority.value, 0) + 1

        if s.review_turnaround_days is not None:
            turnarounds.append(s.review_turnaround_days)

        if s.status is SubmittalStatus.DRAFT:
            draft += 1
        elif s.status in (SubmittalStatus.SUBMITTED, SubmittalStatus.UNDER_REVIEW):
            awaiting += 1
        elif s.status is SubmittalStatus.REVISE_AND_RESUBMIT:
            revising += 1
        elif s.status is SubmittalStatus.APPROVED:
            approved += 1
        elif s.status is SubmittalStatus.REJECTED:
            rejected += 1

        if s.is_overdue:
            overdue += 1
        if s.is_at_risk:
            at_risk += 1
        if s.is_open and s.ball_in_court is BallInCourt.TRADE:
            with_trade += 1

    avg = round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None

    return SubmittalSummary(
        total=len(rows),
        draft=draft,
        awaiting_review=awaiting,
        being_revised=revising,
        approved=approved,
        rejected=rejected,
        overdue=overdue,
        at_risk=at_risk,
        ball_with_trade=with_trade,
        avg_review_days=avg,
        by_status=by_status,
        by_type=by_type,
        by_court=by_court,
        by_priority=by_priority,
    )


# --- list / create ------------------------------------------------------------


@router.get("", response_model=list[SubmittalRead])
async def list_submittals(
    db: DbSession,
    user: RequireDesignTeam,
    job_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
    status_filter: SubmittalStatus | None = Query(default=None, alias="status"),
    ball_in_court: BallInCourt | None = None,
    submittal_type: SubmittalType | None = Query(default=None, alias="type"),
    priority: Priority | None = None,
    overdue_only: bool = False,
    at_risk_only: bool = False,
):
    stmt = select(Submittal).where(Submittal.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Submittal.job_id == job_id)
    if plan_id is not None:
        stmt = stmt.where(Submittal.plan_id == plan_id)
    if status_filter is not None:
        stmt = stmt.where(Submittal.status == status_filter)
    if ball_in_court is not None:
        stmt = stmt.where(Submittal.ball_in_court == ball_in_court)
    if submittal_type is not None:
        stmt = stmt.where(Submittal.submittal_type == submittal_type)
    if priority is not None:
        stmt = stmt.where(Submittal.priority == priority)

    rows = list(await db.scalars(stmt.order_by(Submittal.submittal_number)))
    if overdue_only:
        rows = [s for s in rows if s.is_overdue]
    if at_risk_only:
        rows = [s for s in rows if s.is_at_risk]
    return rows


@router.post("", response_model=SubmittalRead, status_code=status.HTTP_201_CREATED)
async def create_submittal(payload: SubmittalCreate, db: DbSession, user: RequireStaff):
    """Raise a submittal. It goes straight into the review queue unless kept as a draft;
    the review due date is set from the priority when none is given."""
    await _validate_links(db, user.tenant_id, payload)

    today = date.today()
    data = payload.model_dump(exclude={"submit_now"})
    obj = Submittal(
        tenant_id=user.tenant_id,
        submittal_number=await _next_number(db, user.tenant_id),
        submitted_by_id=user.id,
        **data,
    )
    if payload.submit_now:
        obj.status = SubmittalStatus.SUBMITTED
        obj.submitted_on = today
        obj.due_on = obj.due_on or submittal_service.due_date_for(today, obj.priority)
        obj.ball_in_court = BallInCourt.BUILDER
    else:
        obj.status = SubmittalStatus.DRAFT
        obj.ball_in_court = BallInCourt.TRADE

    db.add(obj)
    await db.flush()
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.CREATED,
        f"Raised as {obj.submittal_type.value}.", user.id,
    ))
    if payload.submit_now:
        db.add(submittal_service.log(
            user.tenant_id, obj.id, SubmittalActivityKind.SUBMITTED,
            "Submitted for review.", user.id,
        ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.get("/{sub_id}", response_model=SubmittalRead)
async def get_submittal(sub_id: uuid.UUID, db: DbSession, user: RequireDesignTeam):
    return await _load(db, user.tenant_id, sub_id)


@router.patch("/{sub_id}", response_model=SubmittalRead)
async def update_submittal(
    sub_id: uuid.UUID, payload: SubmittalUpdate, db: DbSession, user: RequireStaff
):
    obj = await _load(db, user.tenant_id, sub_id)
    await _validate_links(db, user.tenant_id, payload)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.EDITED, None, user.id
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


# --- workflow -----------------------------------------------------------------


@router.post("/{sub_id}/submit", response_model=SubmittalRead)
async def submit_submittal(sub_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Send a draft in for review."""
    obj = await _load(db, user.tenant_id, sub_id)
    if obj.status is not SubmittalStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a draft can be submitted; this one is {obj.status.value}.",
        )
    today = date.today()
    obj.status = SubmittalStatus.SUBMITTED
    obj.submitted_on = today
    obj.due_on = obj.due_on or submittal_service.due_date_for(today, obj.priority)
    obj.ball_in_court = BallInCourt.BUILDER
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.SUBMITTED, "Submitted for review.", user.id
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{sub_id}/start-review", response_model=SubmittalRead)
async def start_review(sub_id: uuid.UUID, db: DbSession, user: RequireDesignTeam):
    """Pick it up for review. Records who is reviewing and moves it under review."""
    obj = await _load(db, user.tenant_id, sub_id)
    if obj.status is not SubmittalStatus.SUBMITTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a submitted item can be picked up; this one is {obj.status.value}.",
        )
    obj.status = SubmittalStatus.UNDER_REVIEW
    obj.reviewer_id = user.id
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.REVIEW_STARTED, "Review started.", user.id
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{sub_id}/review", response_model=SubmittalRead)
async def review_submittal(
    sub_id: uuid.UUID, payload: SubmittalReview, db: DbSession, user: RequireDesignTeam
):
    """Return a decision.

    Every decision closes the current round and writes it to the revision history, so
    what was proposed and how it came back is never lost. "Revise and resubmit" then
    bumps the revision and hands it back to the trade; the other three are terminal.
    """
    obj = await _load(db, user.tenant_id, sub_id)
    if payload.status not in DECISIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A review decision must be approved, approved-as-noted, "
                   "revise-and-resubmit or rejected.",
        )
    if obj.status not in submittal_service.IN_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a submitted or under-review item can be decided; "
                   f"this one is {obj.status.value}.",
        )

    today = date.today()
    obj.status = payload.status
    obj.review_notes = payload.review_notes
    obj.returned_on = today
    obj.reviewer_id = user.id

    # Close the round into the history.
    db.add(SubmittalRevision(
        tenant_id=user.tenant_id,
        submittal_id=obj.id,
        revision=obj.revision,
        outcome=payload.status,
        submitted_on=obj.submitted_on,
        returned_on=today,
        reviewer_id=user.id,
        review_notes=payload.review_notes,
        storage_key=obj.storage_key,
    ))

    if payload.status is SubmittalStatus.REVISE_AND_RESUBMIT:
        obj.revision += 1
        obj.ball_in_court = BallInCourt.TRADE
        obj.submitted_on = None       # the new round has not been submitted yet
        obj.returned_on = None
    else:
        obj.ball_in_court = BallInCourt.CLOSED
        if payload.status is SubmittalStatus.APPROVED:
            obj.approved_on = today

    db.add(submittal_service.log(
        user.tenant_id, obj.id, DECISION_KIND[payload.status],
        payload.review_notes or None, user.id,
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{sub_id}/resubmit", response_model=SubmittalRead)
async def resubmit_submittal(
    sub_id: uuid.UUID, payload: SubmittalResubmit, db: DbSession, user: RequireStaff
):
    """The trade sends the corrected revision back. It re-enters the review queue at the
    bumped revision number, so the loop is traceable rather than a silent overwrite."""
    obj = await _load(db, user.tenant_id, sub_id)
    if obj.status is not SubmittalStatus.REVISE_AND_RESUBMIT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only an item sent back to revise can be resubmitted; "
                   f"this one is {obj.status.value}.",
        )
    today = date.today()
    obj.status = SubmittalStatus.SUBMITTED
    obj.submitted_on = today
    obj.returned_on = None
    obj.review_notes = None
    obj.due_on = submittal_service.due_date_for(today, obj.priority)
    obj.ball_in_court = BallInCourt.BUILDER
    if payload.storage_key:
        obj.storage_key = payload.storage_key
    detail = f"Revision {obj.revision} resubmitted."
    if payload.note:
        detail += f" {payload.note}"
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.RESUBMITTED, detail, user.id
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)


@router.post("/{sub_id}/reassign", response_model=SubmittalRead)
async def reassign_submittal(
    sub_id: uuid.UUID, payload: SubmittalReassign, db: DbSession, user: RequireStaff
):
    """Move the ball to another court, with an optional reason for the log."""
    obj = await _load(db, user.tenant_id, sub_id)
    if obj.status in SUBMITTAL_CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This submittal is {obj.status.value}; there is no ball to move.",
        )
    obj.ball_in_court = payload.ball_in_court
    detail = f"Ball moved to {payload.ball_in_court.value}."
    if payload.note:
        detail += f" {payload.note}"
    db.add(submittal_service.log(
        user.tenant_id, obj.id, SubmittalActivityKind.REASSIGNED, detail, user.id
    ))
    await db.commit()
    return await _load(db, user.tenant_id, obj.id)
