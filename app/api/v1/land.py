"""Land development: the appraisal, the development budget, planning and its obligations,
and handing serviced plots to the community.

Moved out of extended.py, which by then held nothing else. What was there could create
a project and add a budget line - nothing could be edited, `delivered_lots` was never
updated by anything, and there was no planning at all.
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.community import Community, CommunityPhase, Lot, LotStatus
from app.models.land import (
    OPEN_OBLIGATIONS,
    LandBudgetLine,
    LandObligation,
    LandPhaseStatus,
    LandProject,
    ObligationKind,
    ObligationStatus,
    PlanningStatus,
)
from app.schemas.land import (
    DeliverPlots,
    LandBudgetLineCreate,
    LandBudgetLineRead,
    LandBudgetLineUpdate,
    LandProjectCreate,
    LandProjectRead,
    LandProjectUpdate,
    LandSummary,
    ObligationComplete,
    ObligationCreate,
    ObligationRead,
    ObligationUpdate,
)

router = APIRouter(prefix="/land-projects", tags=["land development"])
items_router = APIRouter(tags=["land development"])

HA_PER_ACRE = Decimal("0.404686")
#: A permission lapses if not implemented within three years of approval.
CONSENT_YEARS = 3
PAYMENT_KINDS = (ObligationKind.S106, ObligationKind.CIL)
ZERO = Decimal("0")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _load(db, tenant_id: uuid.UUID, pid: uuid.UUID) -> LandProject:
    p = await db.scalar(
        select(LandProject)
        .options(selectinload(LandProject.budget_lines), selectinload(LandProject.obligations))
        .where(LandProject.id == pid, LandProject.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Land project not found")
    return p


def _read(p: LandProject) -> LandProjectRead:
    r = LandProjectRead.model_validate(p)
    r.obligations = sorted(
        r.obligations,
        key=lambda o: (o.status not in OPEN_OBLIGATIONS, not o.pre_commencement,
                       o.due_date or date.max),
    )
    return r


def _fill_area(data: dict) -> None:
    acres, ha = data.get("acres"), data.get("site_area_ha")
    if acres is not None and ha is None:
        data["site_area_ha"] = (Decimal(acres) * HA_PER_ACRE).quantize(Decimal("0.01"))
    elif ha is not None and acres is None:
        data["acres"] = (Decimal(ha) / HA_PER_ACRE).quantize(Decimal("0.01"))


async def _require_community(db, tenant_id, community_id) -> None:
    if community_id is not None and not await db.scalar(
        select(Community.id).where(Community.id == community_id, Community.tenant_id == tenant_id)
    ):
        raise HTTPException(status_code=422, detail="That community does not exist.")


# --- projects ---------------------------------------------------------------------------


@router.get("", response_model=list[LandProjectRead])
async def list_land_projects(
    db: DbSession,
    user: RequireStaff,
    status_filter: LandPhaseStatus | None = Query(None, alias="status"),
):
    stmt = (
        select(LandProject)
        .options(selectinload(LandProject.budget_lines), selectinload(LandProject.obligations))
        .where(LandProject.tenant_id == user.tenant_id)
    )
    if status_filter is not None:
        stmt = stmt.where(LandProject.status == status_filter)
    return [_read(p) for p in await db.scalars(stmt.order_by(LandProject.code))]


@router.get("/summary", response_model=LandSummary)
async def land_summary(db: DbSession, user: RequireStaff):
    projects = list(
        await db.scalars(
            select(LandProject)
            .options(selectinload(LandProject.budget_lines), selectinload(LandProject.obligations))
            .where(LandProject.tenant_id == user.tenant_id)
        )
    )
    by_status: dict[str, int] = {}
    for p in projects:
        by_status[p.status.value] = by_status.get(p.status.value, 0) + 1
    obligations = [o for p in projects for o in p.obligations if o.status in OPEN_OBLIGATIONS]
    return LandSummary(
        projects=len(projects),
        by_status=by_status,
        acquisition=sum((p.acquisition_cost for p in projects), ZERO),
        budget=sum((p.budget_total for p in projects), ZERO),
        actual=sum((p.actual_total for p in projects), ZERO),
        forecast=sum((p.forecast_total for p in projects), ZERO),
        planned_plots=sum(p.planned_lots or 0 for p in projects),
        delivered_plots=sum(p.delivered_lots for p in projects),
        consents_expiring=sum(
            1 for p in projects
            if p.status is not LandPhaseStatus.COMPLETE
            and p.days_to_consent_expiry is not None
            and p.days_to_consent_expiry <= 180
        ),
        obligations_open=len(obligations),
        obligations_overdue=sum(1 for o in obligations if o.is_overdue),
        pre_commencement_open=sum(1 for o in obligations if o.pre_commencement),
        contributions_due=sum((o.amount or ZERO for o in obligations), ZERO),
    )


@router.post("", response_model=LandProjectRead, status_code=status.HTTP_201_CREATED)
async def create_land_project(payload: LandProjectCreate, db: DbSession, admin: RequireAdmin):
    data = payload.model_dump()
    data["code"] = data["code"].strip().upper()
    if await db.scalar(
        select(LandProject.id).where(
            LandProject.tenant_id == admin.tenant_id, func.upper(LandProject.code) == data["code"]
        )
    ):
        raise _conflict(f"Land project {data['code']} already exists.")
    await _require_community(db, admin.tenant_id, data.get("community_id"))
    _fill_area(data)
    p = LandProject(tenant_id=admin.tenant_id, **data)
    db.add(p)
    await db.commit()
    return _read(await _load(db, admin.tenant_id, p.id))


@router.get("/{project_id}", response_model=LandProjectRead)
async def get_land_project(project_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return _read(await _load(db, user.tenant_id, project_id))


@router.patch("/{project_id}", response_model=LandProjectRead)
async def update_land_project(
    project_id: uuid.UUID, payload: LandProjectUpdate, db: DbSession, admin: RequireAdmin
):
    """Edit the project. Two rules follow UK planning:

    * Starting infrastructure works needs a buildable permission (full, or reserved
      matters approved) and every pre-commencement condition discharged. Starting
      without them is a breach of planning control.
    * Approving a permission sets its expiry three years on, unless a date is given.
    """
    p = await _load(db, admin.tenant_id, project_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("code"):
        data["code"] = data["code"].strip().upper()
        if await db.scalar(
            select(LandProject.id).where(
                LandProject.tenant_id == admin.tenant_id,
                func.upper(LandProject.code) == data["code"],
                LandProject.id != p.id,
            )
        ):
            raise _conflict(f"Land project {data['code']} already exists.")
    if "community_id" in data:
        await _require_community(db, admin.tenant_id, data["community_id"])
    if ("acres" in data) != ("site_area_ha" in data):
        _fill_area(data)

    new_planning = data.get("planning_status")
    buildable = (PlanningStatus.RESERVED_MATTERS, PlanningStatus.FULL)
    if new_planning in buildable and new_planning != p.planning_status:
        approved = data.get("planning_approved_on") or p.planning_approved_on or date.today()
        data["planning_approved_on"] = approved
        if "planning_expires_on" not in data and p.planning_expires_on is None:
            data["planning_expires_on"] = approved.replace(year=approved.year + CONSENT_YEARS)

    starting = data.get("status") is LandPhaseStatus.DEVELOPMENT
    if starting and p.status is not LandPhaseStatus.DEVELOPMENT:
        planning = new_planning or p.planning_status
        if planning not in buildable:
            raise _conflict(
                "Works cannot start without a buildable permission - full planning, or "
                "reserved matters approved."
            )
        blocking = [
            o for o in p.obligations if o.pre_commencement and o.status in OPEN_OBLIGATIONS
        ]
        if blocking:
            refs = ", ".join(o.reference or o.description[:30] for o in blocking[:4])
            raise _conflict(
                f"{len(blocking)} pre-commencement condition"
                f"{'s are' if len(blocking) != 1 else ' is'} not discharged ({refs}). "
                "Starting now would breach planning control."
            )
        data.setdefault("start_date", p.start_date or date.today())

    for key, value in data.items():
        if key in ("code", "name", "status", "delivered_lots", "acquisition_cost",
                   "planning_status") and value is None:
            continue
        setattr(p, key, value)
    await db.commit()
    return _read(await _load(db, admin.tenant_id, project_id))


@router.post("/{project_id}/deliver-plots", response_model=LandProjectRead)
async def deliver_plots(
    project_id: uuid.UUID, payload: DeliverPlots, db: DbSession, admin: RequireAdmin
):
    """Hand serviced plots to the community: they are created there, not yet released
    for sale, and counted as delivered here. Numbers that exist already are skipped."""
    p = await _load(db, admin.tenant_id, project_id)
    if p.community_id is None:
        raise _conflict("Link this land to a community first - that is where the plots go.")
    if not p.is_buildable:
        raise _conflict("Plots cannot be delivered before the permission allows building.")
    if payload.end < payload.start or payload.end - payload.start >= 500:
        raise HTTPException(status_code=422, detail="Give a range of up to 500 plots.")
    if payload.phase_id is not None:
        phase = await db.scalar(
            select(CommunityPhase).where(
                CommunityPhase.id == payload.phase_id, CommunityPhase.tenant_id == admin.tenant_id
            )
        )
        if phase is None or phase.community_id != p.community_id:
            raise HTTPException(status_code=422, detail="That phase is not in this community.")
    existing = {
        n.lower()
        for n in await db.scalars(select(Lot.lot_number).where(Lot.community_id == p.community_id))
    }
    created = 0
    for n in range(payload.start, payload.end + 1):
        number = f"{payload.prefix.strip()}{n}"
        if number.lower() in existing:
            continue
        db.add(
            Lot(
                tenant_id=admin.tenant_id, community_id=p.community_id, lot_number=number,
                status=LotStatus.NOT_RELEASED, phase_id=payload.phase_id, premium=payload.premium,
            )
        )
        created += 1
    if not created:
        raise _conflict("Every plot in that range already exists in the community.")
    p.delivered_lots += created
    await db.commit()
    return _read(await _load(db, admin.tenant_id, project_id))


# --- budget lines -------------------------------------------------------------------------


@router.post("/{project_id}/budget-lines", response_model=LandProjectRead)
async def add_land_budget_line(
    project_id: uuid.UUID, payload: LandBudgetLineCreate, db: DbSession, admin: RequireAdmin
):
    await _load(db, admin.tenant_id, project_id)
    db.add(LandBudgetLine(tenant_id=admin.tenant_id, project_id=project_id, **payload.model_dump()))
    await db.commit()
    return _read(await _load(db, admin.tenant_id, project_id))


async def _budget_line(db, tenant_id, line_id) -> LandBudgetLine:
    ln = await db.scalar(
        select(LandBudgetLine).where(
            LandBudgetLine.id == line_id, LandBudgetLine.tenant_id == tenant_id
        )
    )
    if ln is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget line not found")
    return ln


@items_router.patch("/land-budget-lines/{line_id}", response_model=LandBudgetLineRead)
async def update_land_budget_line(
    line_id: uuid.UUID, payload: LandBudgetLineUpdate, db: DbSession, admin: RequireAdmin
):
    """Correct a budget, or book the actual spend against it."""
    ln = await _budget_line(db, admin.tenant_id, line_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in ("category", "budget_amount", "actual_amount") and value is None:
            continue
        setattr(ln, key, value)
    await db.commit()
    await db.refresh(ln)
    return ln


@items_router.delete("/land-budget-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_land_budget_line(line_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    ln = await _budget_line(db, admin.tenant_id, line_id)
    if ln.actual_amount > 0:
        raise _conflict("Money has been spent against this line. Set its budget instead.")
    await db.delete(ln)
    await db.commit()


# --- planning obligations ---------------------------------------------------------------------


@router.post(
    "/{project_id}/obligations", response_model=ObligationRead, status_code=status.HTTP_201_CREATED
)
async def add_obligation(
    project_id: uuid.UUID, payload: ObligationCreate, db: DbSession, admin: RequireAdmin
):
    await _load(db, admin.tenant_id, project_id)
    o = LandObligation(tenant_id=admin.tenant_id, project_id=project_id, **payload.model_dump())
    db.add(o)
    await db.commit()
    await db.refresh(o)
    return o


async def _obligation(db, tenant_id, oid) -> LandObligation:
    o = await db.scalar(
        select(LandObligation).where(
            LandObligation.id == oid, LandObligation.tenant_id == tenant_id
        )
    )
    if o is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Obligation not found")
    return o


@items_router.patch("/land-obligations/{obligation_id}", response_model=ObligationRead)
async def update_obligation(
    obligation_id: uuid.UUID, payload: ObligationUpdate, db: DbSession, admin: RequireAdmin
):
    o = await _obligation(db, admin.tenant_id, obligation_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in ("kind", "description", "pre_commencement", "status") and value is None:
            continue
        setattr(o, key, value)
    await db.commit()
    await db.refresh(o)
    return o


@items_router.post("/land-obligations/{obligation_id}/complete", response_model=ObligationRead)
async def complete_obligation(
    obligation_id: uuid.UUID, payload: ObligationComplete, db: DbSession, admin: RequireAdmin
):
    """Discharged (a condition, an agreement) or paid (S106, CIL) - or waived."""
    o = await _obligation(db, admin.tenant_id, obligation_id)
    if o.status not in OPEN_OBLIGATIONS:
        raise _conflict(f"This is already {o.status.value}.")
    if payload.waived:
        o.status = ObligationStatus.WAIVED
    elif o.kind in PAYMENT_KINDS and o.amount:
        o.status = ObligationStatus.PAID
    else:
        o.status = ObligationStatus.DISCHARGED
    o.completed_on = payload.completed_on or date.today()
    if payload.notes:
        o.notes = payload.notes
    await db.commit()
    await db.refresh(o)
    return o


@items_router.post("/land-obligations/{obligation_id}/submit", response_model=ObligationRead)
async def submit_obligation(obligation_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    """Sent to the council; awaiting their sign-off."""
    o = await _obligation(db, admin.tenant_id, obligation_id)
    if o.status is not ObligationStatus.OUTSTANDING:
        raise _conflict(f"Only an outstanding item can be submitted; this is {o.status.value}.")
    o.status = ObligationStatus.SUBMITTED
    await db.commit()
    await db.refresh(o)
    return o


@items_router.post("/land-obligations/{obligation_id}/reopen", response_model=ObligationRead)
async def reopen_obligation(obligation_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    o = await _obligation(db, admin.tenant_id, obligation_id)
    if o.status in OPEN_OBLIGATIONS:
        raise _conflict("This is still open.")
    o.status = ObligationStatus.OUTSTANDING
    o.completed_on = None
    await db.commit()
    await db.refresh(o)
    return o


@items_router.delete("/land-obligations/{obligation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_obligation(obligation_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    o = await _obligation(db, admin.tenant_id, obligation_id)
    await db.delete(o)
    await db.commit()

