"""Materials selection signoff.

The customer-facing approval of chosen materials. The builder sends a job's
selections for approval with a deadline; the buyer approves each with a typed
signature or declines with a reason. The decision is recorded against the
selection, apart from its contractual status.

The API tier and nothing more. Rules live in `app.services.signoff`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequireStaff
from app.models.selection import SelectionApprovalStatus
from app.schemas.signoff import (
    BuyerApprove,
    BuyerDecline,
    ChoiceCreate,
    ChoiceUpdate,
    RequestApproval,
    SelectionSignoffRead,
    SetAllowance,
    SignoffSummary,
)
from app.services.signoff import SignoffService

router = APIRouter(prefix="/signoff", tags=["selection signoff"])
CREATED = status.HTTP_201_CREATED


@router.get("/summary", response_model=SignoffSummary)
async def signoff_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """The counts above the list: awaiting, approved, declined and overdue."""
    data = await SignoffService(db, user.tenant_id).summary(job_id)
    return SignoffSummary(**data)


@router.get("", response_model=list[SelectionSignoffRead])
async def list_signoff(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID, Query(description="One home (required).")],
    approval_status: Annotated[SelectionApprovalStatus | None, Query()] = None,
):
    """Every selection on a job and where its approval stands."""
    items = await SignoffService(db, user.tenant_id).list_for_job(job_id, approval_status)
    return [SelectionSignoffRead.of(s) for s in items]


@router.post("/jobs/{job_id}/request-approval", response_model=list[SelectionSignoffRead])
async def request_approval(
    job_id: uuid.UUID, payload: RequestApproval, db: DbSession, user: RequireStaff
):
    """Send the job's selections to the buyer for approval.

    Leaving `selection_ids` off sends every not-yet-submitted choice. Selections
    already awaiting or approved are skipped, not restarted.
    """
    items = await SignoffService(db, user.tenant_id).request_approval(
        job_id, deadline=payload.deadline, selection_ids=payload.selection_ids
    )
    return [SelectionSignoffRead.of(s) for s in items]


@router.post("/{selection_id}/approve", response_model=SelectionSignoffRead)
async def approve_selection(
    selection_id: uuid.UUID, payload: BuyerApprove, db: DbSession, user: RequireStaff
):
    """Record the buyer's approval, with their typed name as the signature."""
    sel = await SignoffService(db, user.tenant_id).approve(
        selection_id, signature=payload.signature, comment=payload.comment
    )
    return SelectionSignoffRead.of(sel)


@router.post("/{selection_id}/decline", response_model=SelectionSignoffRead)
async def decline_selection(
    selection_id: uuid.UUID, payload: BuyerDecline, db: DbSession, user: RequireStaff
):
    """Record the buyer declining a choice, with the reason they gave."""
    sel = await SignoffService(db, user.tenant_id).decline(
        selection_id,
        reason=payload.reason,
        signature=payload.signature,
        comment=payload.comment,
    )
    return SelectionSignoffRead.of(sel)


@router.post("/{selection_id}/reset", response_model=SelectionSignoffRead)
async def reset_selection(selection_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Pull a choice back out of signoff so it can be corrected and re-sent."""
    sel = await SignoffService(db, user.tenant_id).reset(selection_id)
    return SelectionSignoffRead.of(sel)


# ---- allowance & choices (BuilderTrend-style selections) ----
@router.patch("/{selection_id}/allowance", response_model=SelectionSignoffRead)
async def set_allowance(
    selection_id: uuid.UUID, payload: SetAllowance, db: DbSession, user: RequireStaff
):
    """Set the selection's allowance (budget) and whether the buyer must pick."""
    sel = await SignoffService(db, user.tenant_id).set_allowance(
        selection_id, **payload.model_dump(exclude_unset=True)
    )
    return SelectionSignoffRead.of(sel)


@router.post("/{selection_id}/choices", response_model=SelectionSignoffRead, status_code=CREATED)
async def add_choice(
    selection_id: uuid.UUID, payload: ChoiceCreate, db: DbSession, user: RequireStaff
):
    """Add a priced choice to the selection's menu."""
    sel = await SignoffService(db, user.tenant_id).add_choice(selection_id, **payload.model_dump())
    return SelectionSignoffRead.of(sel)


@router.patch("/choices/{choice_id}", response_model=SelectionSignoffRead)
async def update_choice(
    choice_id: uuid.UUID, payload: ChoiceUpdate, db: DbSession, user: RequireStaff
):
    sel = await SignoffService(db, user.tenant_id).update_choice(
        choice_id, **payload.model_dump(exclude_unset=True)
    )
    return SelectionSignoffRead.of(sel)


@router.post("/choices/{choice_id}/select", response_model=SelectionSignoffRead)
async def select_choice(choice_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Mark this choice as the buyer's pick (clears the others)."""
    sel = await SignoffService(db, user.tenant_id).select_choice(choice_id)
    return SelectionSignoffRead.of(sel)


@router.delete("/choices/{choice_id}", response_model=SelectionSignoffRead)
async def delete_choice(choice_id: uuid.UUID, db: DbSession, user: RequireStaff):
    sel = await SignoffService(db, user.tenant_id).delete_choice(choice_id)
    return SelectionSignoffRead.of(sel)
