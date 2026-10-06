"""Jobs: the spine. Contract, selections, budget and job cost all hang off a job.

The endpoint that matters most here is POST /jobs/{id}/selections/{id}/confirm - one
buyer decision moving contract, budget and schedule together, or not at all.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequireAdmin,
    RequirePurchasing,
    RequireSales,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.budget import Budget
from app.models.community import Lot, LotStatus
from app.models.contract import Buyer, Contract, ContractStatus
from app.models.job import Job, JobStatus
from app.models.selection import Selection
from app.schemas.common import Page, PageParams, page_params
from app.schemas.job import (
    AssignSuperintendent,
    BudgetLineRead,
    BudgetRead,
    CancelRequest,
    CloseRequest,
    CompleteRequest,
    ContractCreate,
    ContractRead,
    CycleTimeReport,
    HoldRequest,
    JobCostSummary,
    JobCreate,
    JobRead,
    JobSummary,
    JobUpdate,
    PermitApplication,
    PermitIssued,
    ReopenBuildRequest,
    SelectionCascade,
    SelectionCreate,
    SelectionRead,
    StartBuild,
)
from app.services import budget as budget_service
from app.services import job_cost as job_cost_service
from app.services import selection as selection_service
from app.services.design_studio import DesignStudioService
from app.services.job import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: Paging and free-text search, declared once and reused by every list.
PageQuery = Annotated[PageParams, Depends(page_params)]


# --- Jobs ---------------------------------------------------------------------
#
# The home moves through named lifecycle routes rather than a settable status
# field. That is the whole difference from what was here before: a PATCH that
# accepted `status` let a planned house jump straight to closed, and let somebody
# complete a home that had never started - which makes cycle time negative, and
# one negative number poisons every average built on it.


def _filters(
    status_filter: Annotated[
        JobStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    community_id: Annotated[uuid.UUID | None, Query(description="One community.")] = None,
    plan_id: Annotated[uuid.UUID | None, Query(description="One house type.")] = None,
    superintendent_id: Annotated[
        uuid.UUID | None, Query(description="Whose site this is.")
    ] = None,
) -> dict:
    """The filters every home list shares, declared once."""
    supplied = {
        "status": status_filter,
        "community_id": community_id,
        "plan_id": plan_id,
        "superintendent_id": superintendent_id,
    }
    return {k: v for k, v in supplied.items() if v is not None}


JobFilters = Annotated[dict, Depends(_filters)]


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


# Literal paths first, or FastAPI tries to parse "summary" as a UUID.


@router.get("/summary", response_model=JobSummary)
async def job_summary(db: DbSession, user: RequireStaff):
    """The counters above the list, in one request rather than five."""
    return await JobService(db, user.tenant_id).summary()


@router.get("/cycle-time", response_model=CycleTimeReport)
async def cycle_time(db: DbSession, user: RequireStaff):
    """Start to completion, over homes that actually finished.

    The single figure a homebuilder is judged on. Homes still running are
    excluded - counting them would make the number improve every time a slow
    house finally finishes.
    """
    return await JobService(db, user.tenant_id).cycle_times()


@router.get("/live", response_model=Page[JobRead])
async def live_jobs(db: DbSession, user: RequireStaff, params: PageQuery, filters: JobFilters):
    """Everything not closed, cancelled or out of warranty."""
    items, total = await JobService(db, user.tenant_id).list_live(params, **filters)
    return _page(items, params, total)


@router.get("/building", response_model=Page[JobRead])
async def building_jobs(
    db: DbSession, user: RequireStaff, params: PageQuery, filters: JobFilters
):
    """Homes physically on site, held ones included - a stopped build still
    occupies a plot, a superintendent and a budget."""
    items, total = await JobService(db, user.tenant_id).list_building(params, **filters)
    return _page(items, params, total)


@router.get("/late", response_model=Page[JobRead])
async def late_jobs(db: DbSession, user: RequireStaff, params: PageQuery, filters: JobFilters):
    """Past the frozen projection, worst first."""
    items, total = await JobService(db, user.tenant_id).list_late(params, **filters)
    return _page(items, params, total)


@router.get("", response_model=Page[JobRead])
async def list_jobs(db: DbSession, user: RequireStaff, params: PageQuery, filters: JobFilters):
    """Every home, newest first, paged and searchable by number or permit."""
    items, total = await JobService(db, user.tenant_id).list_jobs(params, **filters)
    return _page(items, params, total)


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreate, db: DbSession, admin: RequireAdmin):
    """Put a home on a plot.

    Refused if the plot already holds a live home. A cancelled one does not
    count - freeing the plot is the entire reason cancelling exists.
    """
    return await JobService(db, admin.tenant_id).create_job(
        actor_id=admin.id, **payload.model_dump()
    )


@router.get("/{job_id}", response_model=JobRead)
async def get_job(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await JobService(db, user.tenant_id).get_job(job_id)


@router.patch("/{job_id}", response_model=JobRead)
async def update_job(job_id: uuid.UUID, payload: JobUpdate, db: DbSession, user: RequireStaff):
    """Correct a home's details. Status is not settable here - it moves through
    the lifecycle routes below."""
    return await JobService(db, user.tenant_id).update_job(
        job_id, actor_id=user.id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/{job_id}/superintendent", response_model=JobRead)
async def assign_superintendent(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: AssignSuperintendent | None = None,
):
    """Put a superintendent on the home. A null id, or no body, takes them off."""
    return await JobService(db, user.tenant_id).assign_superintendent(
        job_id, (payload or AssignSuperintendent()).superintendent_id
    )


# --- the lifecycle ------------------------------------------------------------


@router.post("/{job_id}/permit/apply", response_model=JobRead)
async def apply_for_permit(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: PermitApplication | None = None,
):
    """Record that the application has gone in, and start the clock on it.

    No body means today, which is the usual case.
    """
    return await JobService(db, user.tenant_id).enter_permitting(
        job_id, **(payload or PermitApplication()).model_dump()
    )


@router.post("/{job_id}/permit/issued", response_model=JobRead)
async def permit_issued(
    job_id: uuid.UUID, payload: PermitIssued, db: DbSession, user: RequireStaff
):
    """The permit came through.

    Deliberately does not start the build. A permit in hand is not work having
    begun, and conflating them is how a start date becomes fiction.
    """
    return await JobService(db, user.tenant_id).record_permit(
        job_id, number=payload.number, issued_on=payload.issued_on
    )


@router.post("/{job_id}/start", response_model=JobRead)
async def start_build(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: StartBuild | None = None,
):
    """Break ground, and freeze the projection that slip is measured against.

    Refused on an unsold home unless `allow_unsold` is set. Building on spec is a
    real strategy with real risk; the flag is the difference between choosing it
    and clicking it. No body means today and not on spec.
    """
    return await JobService(db, user.tenant_id).start_build(
        job_id, **(payload or StartBuild()).model_dump()
    )


@router.post("/{job_id}/hold", response_model=JobRead)
async def hold_build(job_id: uuid.UUID, payload: HoldRequest, db: DbSession, user: RequireStaff):
    """Stop the build, with a reason from a list that can be counted.

    "We lost forty days to inspections this year" is a sentence a builder wants to
    be able to say. Free text cannot be counted, so the reason is a closed list and
    "other" requires a note.
    """
    return await JobService(db, user.tenant_id).hold(job_id, payload.reason, payload.note)


@router.post("/{job_id}/resume", response_model=JobRead)
async def resume_build(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Lift the hold and bank the days it cost."""
    return await JobService(db, user.tenant_id).resume(job_id)


@router.post("/{job_id}/complete", response_model=JobRead)
async def complete_build(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: CompleteRequest | None = None,
):
    """The house is built - not the same as handed over.

    Between complete and closed sit the final inspection, the snagging list and
    the money. No body means it was finished today.
    """
    return await JobService(db, user.tenant_id).complete(
        job_id, completed_on=(payload or CompleteRequest()).completed_on
    )


@router.post("/{job_id}/reopen", response_model=JobRead)
async def reopen_build(
    job_id: uuid.UUID, payload: ReopenBuildRequest, db: DbSession, user: RequireStaff
):
    """Send a completed home back to the site.

    Clears the completion date, because a home that went back on site was not
    finished and leaving the date would report a cycle time that never happened.
    """
    return await JobService(db, user.tenant_id).reopen_build(job_id, payload.reason)


@router.post("/{job_id}/close", response_model=JobRead)
async def close_job(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireSales,
    payload: CloseRequest | None = None,
):
    """Hand the house over. Title transfers and the warranty clock starts.

    Refused without a signed contract and a completed house - those two are a
    legal problem rather than a data problem. Also refused while any defect on the
    home blocks handover.

    The body is optional because every field on it has a default. Requiring it
    meant an empty close answered 422 before any of those rules were reached,
    which reads as a validation problem rather than the refusal it should be.
    """
    payload = payload or CloseRequest()
    return await JobService(db, user.tenant_id).close(
        job_id, closing_date=payload.closing_date, warranty_months=payload.warranty_months
    )


@router.post("/{job_id}/warranty", response_model=JobRead)
async def enter_warranty(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Move a handed-over home into its warranty period."""
    return await JobService(db, user.tenant_id).enter_warranty(job_id)


@router.post("/{job_id}/cancel", response_model=JobRead)
async def cancel_job(
    job_id: uuid.UUID, payload: CancelRequest, db: DbSession, admin: RequireAdmin
):
    """Abandon the home and give the plot back.

    The plot returns to available and a new home can be started on it - which a
    plain unique constraint on the lot blocked forever. Refused once the house is
    closed, because by then it belongs to somebody else.
    """
    return await JobService(db, admin.tenant_id).cancel(job_id, payload.reason, payload.note)


# --- Contract -----------------------------------------------------------------


@router.post(
    "/{job_id}/contract", response_model=ContractRead, status_code=status.HTTP_201_CREATED
)
async def create_contract(
    job_id: uuid.UUID, payload: ContractCreate, db: DbSession, user: RequireSales
):
    """Sell the home: create the buyer and the contract, and mark the lot sold."""
    job = await get_tenant_scoped(db, Job, user.tenant_id, job_id)

    existing = await db.scalar(select(Contract.id).where(Contract.job_id == job_id))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This job already has a contract."
        )

    # Reuse the buyer where one is named. A lead conversion creates a Buyer, and
    # creating another here made the same person two records - which left orphan
    # buyers behind and broke the lead -> buyer -> contract chain that campaign
    # revenue is traced along.
    if payload.buyer_id:
        buyer = await get_tenant_scoped(db, Buyer, user.tenant_id, payload.buyer_id)
    else:
        buyer_data = payload.buyer.model_dump()
        buyer_data["email"] = str(buyer_data["email"]) if buyer_data["email"] else None
        buyer = Buyer(tenant_id=user.tenant_id, **buyer_data)
        db.add(buyer)
        await db.flush()

    contract = Contract(
        tenant_id=user.tenant_id,
        job_id=job_id,
        buyer_id=buyer.id,
        base_price=payload.base_price,
        lot_premium=payload.lot_premium,
        incentives=payload.incentives,
        options_allowance=payload.options_allowance,
        sales_agent_id=user.id,
    )
    db.add(contract)

    lot = await db.get(Lot, job.lot_id)
    if lot is not None:
        if lot.status is LotStatus.NOT_RELEASED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Plot {lot.lot_number} has not been released for sale. "
                "Release it on the community first.",
            )
        lot.status = LotStatus.SOLD
        lot.clear_hold()   # the reservation has become a sale

    # Before the commit, not after. The service only flushes, so if the sale is
    # refused the whole request rolls back together - the first version committed
    # the contract first and left a signed contract on a home the system did not
    # think was sold.
    await JobService(db, user.tenant_id).mark_sold(job_id)

    await db.commit()
    await db.refresh(contract)
    return contract


@router.get("/{job_id}/contract", response_model=ContractRead)
async def get_contract(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    contract = await db.scalar(
        select(Contract).where(Contract.job_id == job_id, Contract.tenant_id == user.tenant_id)
    )
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No contract on this job")
    return contract


@router.post("/{job_id}/contract/sign", response_model=ContractRead)
async def sign_contract(
    job_id: uuid.UUID,
    db: DbSession,
    user: RequireSales,
    signed_date: date | None = None,
):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    contract = await db.scalar(
        select(Contract).where(Contract.job_id == job_id, Contract.tenant_id == user.tenant_id)
    )
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No contract on this job")
    if contract.status is ContractStatus.SIGNED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Contract is already signed."
        )

    contract.status = ContractStatus.SIGNED
    contract.signed_date = signed_date or date.today()
    await db.commit()
    await db.refresh(contract)
    return contract


# --- Budget -------------------------------------------------------------------


@router.post(
    "/{job_id}/budget", response_model=BudgetRead, status_code=status.HTTP_201_CREATED
)
async def generate_budget(
    job_id: uuid.UUID, db: DbSession, user: RequirePurchasing
):
    """Generate the job budget from the plan's standard costs. Runs once per job."""
    job = await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    budget = await budget_service.generate_budget(db, user.tenant_id, job)
    await db.commit()

    return await _read_budget(db, user.tenant_id, budget.id)


@router.get("/{job_id}/budget", response_model=BudgetRead)
async def get_budget(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    budget = await budget_service.get_budget(db, user.tenant_id, job_id)
    if budget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This job has no budget yet."
        )
    return await _read_budget(db, user.tenant_id, budget.id)


async def _read_budget(db, tenant_id: uuid.UUID, budget_id: uuid.UUID) -> BudgetRead:
    budget = await db.scalar(
        select(Budget).options(selectinload(Budget.lines)).where(Budget.id == budget_id)
    )
    lines = [
        BudgetLineRead(
            id=line.id,
            cost_code_id=line.cost_code_id,
            vendor_id=line.vendor_id,
            original_amount=line.original_amount,
            revised_amount=line.revised_amount,
            variance=line.variance,
            description=line.description,
        )
        for line in sorted(budget.lines, key=lambda line_: str(line_.cost_code_id))
    ]
    return BudgetRead(
        id=budget.id,
        job_id=budget.job_id,
        status=budget.status,
        original_total=budget.original_total,
        revised_total=budget.revised_total,
        lines=lines,
    )


# --- Selections ---------------------------------------------------------------


@router.get("/{job_id}/selections", response_model=list[SelectionRead])
async def list_selections(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    rows = await db.scalars(
        select(Selection).where(
            Selection.job_id == job_id, Selection.tenant_id == user.tenant_id
        )
    )
    return list(rows)


@router.post(
    "/{job_id}/selections", response_model=SelectionRead, status_code=status.HTTP_201_CREATED
)
async def add_selection(
    job_id: uuid.UUID,
    payload: SelectionCreate,
    db: DbSession,
    user: RequireSales,
):
    """Record a buyer's option choice, snapshotting today's catalog price and cost.

    A structural option is refused once the home's cutoff has passed. That gate is
    the most valuable rule in the design studio: without it a buyer is told yes in
    the showroom and a superintendent finds out on site that the staircase moved.
    """
    job = await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    await DesignStudioService(db, user.tenant_id).require_selectable(job_id, payload.option_id)
    selection = await selection_service.add_selection(
        db,
        user.tenant_id,
        job,
        payload.option_id,
        quantity=payload.quantity,
        selected_date=payload.selected_date,
    )
    await db.commit()
    await db.refresh(selection)
    return selection


@router.post("/{job_id}/selections/{selection_id}/confirm", response_model=SelectionCascade)
async def confirm_selection(
    job_id: uuid.UUID,
    selection_id: uuid.UUID,
    db: DbSession,
    user: RequireSales,
):
    """Confirm a selection and cascade it into contract, budget and schedule.

    All three move together or none of them do.
    """
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    selection = await get_tenant_scoped(db, Selection, user.tenant_id, selection_id)
    if selection.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Selection is not on this job"
        )

    result = await selection_service.confirm_selection(db, user.tenant_id, selection)
    await db.commit()
    return result


@router.post("/{job_id}/selections/{selection_id}/cancel")
async def cancel_selection(
    job_id: uuid.UUID,
    selection_id: uuid.UUID,
    db: DbSession,
    user: RequireSales,
):
    """Reverse a selection, undoing its contract, budget and schedule effects."""
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    selection = await get_tenant_scoped(db, Selection, user.tenant_id, selection_id)
    if selection.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Selection is not on this job"
        )

    result = await selection_service.cancel_selection(db, user.tenant_id, selection)
    await db.commit()
    return result


# --- Job costing --------------------------------------------------------------


@router.get("/{job_id}/cost", response_model=JobCostSummary)
async def job_cost(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Budget vs committed, per cost code. The number the whole system exists for."""
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    return await job_cost_service.job_cost_summary(db, user.tenant_id, job_id)
