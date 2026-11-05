"""Labour and productivity: hours booked against a home and a cost code.

Four rules worth knowing before reading the routes:

  * hours are compared against what the home was budgeted to take, which is the
    whole point - what was spent is the half you already knew
  * an entry with no rate costs an unknown amount, not nothing, so its hours are
    reported apart from the money rather than dragging the average rate down
  * approving an entry needs a rate on it, and an approved entry cannot be edited
    underneath its own approval
  * one person cannot be booked for more than sixteen hours in a day across every
    home, because above that it is nearly always the same shift typed twice

The API tier and nothing more. Rules live in `app.services.labour`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireStaff
from app.schemas.common import Page, PageParams, page_params
from app.schemas.labour import (
    ApprovalResult,
    ApproveMany,
    LabourSummary,
    ProductivityReport,
    SetBudgetedHours,
    TimecardCreate,
    TimecardRead,
    TimecardUpdate,
    VendorRow,
    WorkerRow,
)
from app.services.labour import LabourService

router = APIRouter(prefix="/timecards", tags=["labour"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/summary", response_model=LabourSummary)
async def labour_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """The numbers above the list, counted over everything rather than the page."""
    return await LabourService(db, user.tenant_id).summary(job_id)


@router.get("/productivity", response_model=ProductivityReport)
async def productivity(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """Hours and labour cost by cost code, against what the home was budgeted.

    Budgeted hours only appear for one home. Summing targets across the business
    would produce a number that looks like a company labour budget and is not one,
    because homes at different stages have spent different fractions of theirs.
    """
    return await LabourService(db, user.tenant_id).productivity(job_id)


@router.get("/by-worker", response_model=list[WorkerRow])
async def by_worker(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """Who was on site, for how long, and what it cost."""
    return await LabourService(db, user.tenant_id).by_worker(job_id)


@router.get("/by-vendor", response_model=list[VendorRow])
async def by_vendor(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """What each trade's labour came to, and how much of it was overtime."""
    return await LabourService(db, user.tenant_id).by_vendor(job_id)


@router.get("", response_model=Page[TimecardRead])
async def list_timecards(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    vendor_id: Annotated[uuid.UUID | None, Query(description="One trade.")] = None,
    cost_code_id: Annotated[uuid.UUID | None, Query()] = None,
):
    filters = {
        k: v
        for k, v in {
            "job_id": job_id,
            "vendor_id": vendor_id,
            "cost_code_id": cost_code_id,
        }.items()
        if v is not None
    }
    items, total = await LabourService(db, user.tenant_id).list_timecards(params, **filters)
    return _page(items, params, total)


@router.post("", response_model=TimecardRead, status_code=CREATED)
async def log_hours(payload: TimecardCreate, db: DbSession, user: RequireStaff):
    """Book hours against a home and a cost code.

    Leave the rate off and the trade's standard rate is used.
    """
    return await LabourService(db, user.tenant_id).record(actor_id=user.id, **payload.model_dump())


@router.patch("/{card_id}", response_model=TimecardRead)
async def amend_timecard(
    card_id: uuid.UUID, payload: TimecardUpdate, db: DbSession, user: RequireStaff
):
    """Correct an entry. Refused once it has been approved."""
    return await LabourService(db, user.tenant_id).amend(
        card_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/{card_id}", status_code=NO_CONTENT)
async def delete_timecard(card_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Remove an entry that should never have been made.

    Refused once approved - withdraw the approval first, so deleting signed-off
    hours is always a deliberate second step.
    """
    await LabourService(db, user.tenant_id).delete(card_id)


@router.post("/{card_id}/approve", response_model=TimecardRead)
async def approve_timecard(card_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Sign the hours off.

    Refused with no rate on the entry: approving it would turn unknown money into
    nought money, and the nought is the one that reaches the job cost.
    """
    return await LabourService(db, user.tenant_id).approve(card_id, actor_id=user.id)


@router.post("/{card_id}/withdraw-approval", response_model=TimecardRead)
async def withdraw_approval(card_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Take the approval back, so the entry can be corrected."""
    return await LabourService(db, user.tenant_id).withdraw_approval(card_id)


@router.post("/approve", response_model=ApprovalResult)
async def approve_many(payload: ApproveMany, db: DbSession, user: RequireStaff):
    """Sign off a week's worth at once.

    Reports what it could not do rather than refusing the batch. One entry missing
    a rate should not keep the other thirty-nine waiting.
    """
    return await LabourService(db, user.tenant_id).approve_many(
        payload.timecard_ids, actor_id=user.id
    )


# ============================================== the target =====


@router.put("/budgeted-hours/{job_id}", response_model=ProductivityReport)
async def set_budgeted_hours(
    job_id: uuid.UUID,
    payload: list[SetBudgetedHours],
    db: DbSession,
    user: RequireStaff,
):
    """Set what this home's labour is measured against, per cost code.

    Lives here rather than under the budget because it is the target this screen
    exists to compare with, and a builder setting it is thinking about hours
    rather than about money. Passing null clears a target, which is different from
    setting it to nought: no target means the row reports actuals with no variance,
    where nought would claim the work was supposed to take no time at all.
    """
    return await LabourService(db, user.tenant_id).set_budgeted_hours(
        job_id, [(row.cost_code_id, row.budgeted_hours) for row in payload]
    )
