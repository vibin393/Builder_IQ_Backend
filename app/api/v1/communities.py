"""Developments: communities, their release phases, the plans sold there, and the plots.

Moved out of catalog.py when a community stopped being a code and a name. The sales
position of a plot lives here (release, hold, withdraw); the sale itself and the
handover are still the contract's and the job's, which set `sold` and `closed`.
"""

import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import DbSession, RequireAdmin, RequireSales, RequireStaff
from app.models.community import (
    Community,
    CommunityPhase,
    CommunityPlan,
    CommunityStatus,
    Lot,
    LotStatus,
)
from app.models.crm import Lead
from app.models.job import LIVE_STATUSES, Job, JobStatus
from app.models.plan import Plan
from app.models.user import User
from app.schemas.communities import (
    CommunityCreate,
    CommunityPlanRead,
    CommunityPlanSet,
    CommunityPortfolio,
    CommunityRead,
    CommunityStats,
    CommunityUpdate,
    LotBulkCreate,
    LotBulkResult,
    LotCreate,
    LotHold,
    LotRead,
    LotUpdate,
    PhaseCreate,
    PhaseRead,
    PhaseUpdate,
    PlotCounts,
)

router = APIRouter(tags=["communities"])

#: The longest a plot can be reserved without a sale. Longer, and it is not a hold.
MAX_HOLD_DAYS = 90
EXPIRING_SOON_DAYS = 7
UNSOLD = (LotStatus.AVAILABLE, LotStatus.HELD)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _natural(lot_number: str):
    """Plot 2 before plot 10. A text sort put 10 first."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", lot_number)]


# --- loading and the hold sweep ----------------------------------------------------


async def _community(db, tenant_id: uuid.UUID, community_id: uuid.UUID) -> Community:
    c = await db.scalar(
        select(Community).where(Community.id == community_id, Community.tenant_id == tenant_id)
    )
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Community not found")
    return c


async def _lot(db, tenant_id: uuid.UUID, lot_id: uuid.UUID) -> Lot:
    lot = await db.scalar(select(Lot).where(Lot.id == lot_id, Lot.tenant_id == tenant_id))
    if lot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plot not found")
    return lot


async def _phase(db, tenant_id: uuid.UUID, phase_id: uuid.UUID) -> CommunityPhase:
    p = await db.scalar(
        select(CommunityPhase).where(
            CommunityPhase.id == phase_id, CommunityPhase.tenant_id == tenant_id
        )
    )
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phase not found")
    return p


async def _require_phase_in(db, tenant_id, community_id, phase_id) -> None:
    if phase_id is None:
        return
    phase = await _phase(db, tenant_id, phase_id)
    if phase.community_id != community_id:
        raise HTTPException(status_code=422, detail="That phase belongs to another community.")


async def _require_colleague(db, tenant_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    found = await db.scalar(select(User.id).where(User.id == user_id, User.tenant_id == tenant_id))
    if found is None:
        raise HTTPException(status_code=422, detail="That person is not in this organisation.")


async def sweep_expired_holds(db, tenant_id: uuid.UUID) -> int:
    """Holds past their date lapse back to available. Run on every read of plots, so a
    lapsed reservation never keeps a plot off the market for want of someone noticing."""
    today = date.today()
    lapsed = list(
        await db.scalars(
            select(Lot).where(
                Lot.tenant_id == tenant_id,
                Lot.status == LotStatus.HELD,
                Lot.hold_until.isnot(None),
                Lot.hold_until < today,
            )
        )
    )
    for lot in lapsed:
        lot.status = LotStatus.AVAILABLE
        lot.clear_hold()
    if lapsed:
        await db.commit()
    return len(lapsed)


async def _live_jobs(db, tenant_id: uuid.UUID) -> dict[uuid.UUID, Job]:
    """The job on each plot, cancelled ones excluded (the plot is free again)."""
    rows = await db.scalars(
        select(Job).where(Job.tenant_id == tenant_id, Job.status != JobStatus.CANCELLED)
    )
    return {j.lot_id: j for j in rows}


def _counts(lots) -> PlotCounts:
    c = PlotCounts(total=len(lots))
    for lot in lots:
        setattr(c, lot.status.value, getattr(c, lot.status.value) + 1)
    return c


def _stats(lots: list[Lot], jobs: dict[uuid.UUID, Job]) -> CommunityStats:
    today = date.today()
    since = today - timedelta(days=90)
    counts = _counts(lots)
    live = spec = sold_recent = expiring = 0
    for lot in lots:
        job = jobs.get(lot.id)
        if job is not None and job.status in LIVE_STATUSES:
            live += 1
            if lot.status in UNSOLD and job.sold_date is None:
                spec += 1
        if job is not None and job.sold_date and job.sold_date >= since:
            sold_recent += 1
        if (
            lot.status is LotStatus.HELD
            and lot.hold_until
            and lot.hold_until <= today + timedelta(days=EXPIRING_SOON_DAYS)
        ):
            expiring += 1
    absorption = (Decimal(sold_recent) / Decimal(3)).quantize(Decimal("0.1"))
    unsold = counts.available + counts.held
    return CommunityStats(
        plots=counts,
        live_homes=live,
        spec_homes=spec,
        sales_90_days=sold_recent,
        absorption=absorption,
        months_of_supply=(
            (Decimal(unsold) / absorption).quantize(Decimal("0.1")) if absorption else None
        ),
        holds_expiring_soon=expiring,
    )


async def _lots_by_community(db, tenant_id) -> dict[uuid.UUID, list[Lot]]:
    out: dict[uuid.UUID, list[Lot]] = defaultdict(list)
    for lot in await db.scalars(select(Lot).where(Lot.tenant_id == tenant_id)):
        out[lot.community_id].append(lot)
    return out


def _read(c: Community, stats: CommunityStats | None) -> CommunityRead:
    r = CommunityRead.model_validate(c)
    r.stats = stats
    return r


# --- communities ---------------------------------------------------------------------


@router.get("/communities", response_model=list[CommunityRead])
async def list_communities(
    db: DbSession,
    user: RequireStaff,
    status_filter: CommunityStatus | None = Query(default=None, alias="status"),
):
    """Every development with its plot position and sales pace."""
    await sweep_expired_holds(db, user.tenant_id)
    stmt = select(Community).where(Community.tenant_id == user.tenant_id)
    if status_filter is not None:
        stmt = stmt.where(Community.status == status_filter)
    communities = list(await db.scalars(stmt.order_by(Community.name)))
    lots = await _lots_by_community(db, user.tenant_id)
    jobs = await _live_jobs(db, user.tenant_id)
    return [_read(c, _stats(lots.get(c.id, []), jobs)) for c in communities]


@router.get("/communities/summary", response_model=CommunityPortfolio)
async def communities_summary(db: DbSession, user: RequireStaff):
    await sweep_expired_holds(db, user.tenant_id)
    communities = list(
        await db.scalars(select(Community).where(Community.tenant_id == user.tenant_id))
    )
    by_community = await _lots_by_community(db, user.tenant_id)
    jobs = await _live_jobs(db, user.tenant_id)
    all_lots = [lot for lots in by_community.values() for lot in lots]
    s = _stats(all_lots, jobs)
    by_status: dict[str, int] = {}
    for c in communities:
        by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
    return CommunityPortfolio(
        communities=len(communities),
        by_status=by_status,
        plots=s.plots,
        live_homes=s.live_homes,
        spec_homes=s.spec_homes,
        sales_90_days=s.sales_90_days,
        holds_expiring_soon=s.holds_expiring_soon,
        unreleased_plots=s.plots.not_released,
    )


@router.post("/communities", response_model=CommunityRead, status_code=status.HTTP_201_CREATED)
async def create_community(payload: CommunityCreate, db: DbSession, admin: RequireAdmin):
    code = payload.code.strip().upper()
    taken = await db.scalar(
        select(Community.id).where(
            Community.tenant_id == admin.tenant_id, func.upper(Community.code) == code
        )
    )
    if taken:
        raise _conflict(f"Community {code} already exists.")
    await _require_colleague(db, admin.tenant_id, payload.sales_manager_id)
    await _require_colleague(db, admin.tenant_id, payload.site_manager_id)
    data = payload.model_dump()
    data["code"] = code
    if data.get("postcode"):
        data["postcode"] = data["postcode"].strip().upper()
    c = Community(tenant_id=admin.tenant_id, **data)
    c.is_active = c.status is not CommunityStatus.CLOSED
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _read(c, _stats([], {}))


@router.get("/communities/{community_id}", response_model=CommunityRead)
async def get_community(community_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await sweep_expired_holds(db, user.tenant_id)
    c = await _community(db, user.tenant_id, community_id)
    lots = list(await db.scalars(select(Lot).where(Lot.community_id == c.id)))
    return _read(c, _stats(lots, await _live_jobs(db, user.tenant_id)))


@router.patch("/communities/{community_id}", response_model=CommunityRead)
async def update_community(
    community_id: uuid.UUID, payload: CommunityUpdate, db: DbSession, admin: RequireAdmin
):
    """Edit a development. Marking it sold out or closed is checked against its plots:
    a status that the plots contradict would mislead every report that reads it."""
    c = await _community(db, admin.tenant_id, community_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("code"):
        data["code"] = data["code"].strip().upper()
        taken = await db.scalar(
            select(Community.id).where(
                Community.tenant_id == admin.tenant_id,
                func.upper(Community.code) == data["code"],
                Community.id != c.id,
            )
        )
        if taken:
            raise _conflict(f"Community {data['code']} already exists.")
    await _require_colleague(db, admin.tenant_id, data.get("sales_manager_id"))
    await _require_colleague(db, admin.tenant_id, data.get("site_manager_id"))

    new_status = data.get("status")
    if new_status in (CommunityStatus.SOLD_OUT, CommunityStatus.CLOSED):
        lots = list(await db.scalars(select(Lot).where(Lot.community_id == c.id)))
        unsold = sum(1 for lot in lots if lot.status in UNSOLD)
        if unsold:
            raise _conflict(
                f"{unsold} plot{'s are' if unsold != 1 else ' is'} still for sale or on hold. "
                "Sell or withdraw them first."
            )
        if new_status is CommunityStatus.CLOSED:
            jobs = await _live_jobs(db, admin.tenant_id)
            building = sum(
                1 for lot in lots if lot.id in jobs and jobs[lot.id].status in LIVE_STATUSES
            )
            if building:
                raise _conflict(
                    f"{building} home{'s are' if building != 1 else ' is'} not handed over yet. "
                    "A development closes when it is built out."
                )

    if data.get("postcode"):
        data["postcode"] = data["postcode"].strip().upper()
    for key, value in data.items():
        if key in ("code", "name", "status") and value is None:
            continue
        setattr(c, key, value)
    c.is_active = c.status is not CommunityStatus.CLOSED
    await db.commit()
    return await get_community(community_id, db, admin)


# --- phases -------------------------------------------------------------------------


async def _phase_read(db, phase: CommunityPhase) -> PhaseRead:
    lots = list(await db.scalars(select(Lot).where(Lot.phase_id == phase.id)))
    r = PhaseRead.model_validate(phase)
    r.plots = _counts(lots)
    return r


@router.get("/communities/{community_id}/phases", response_model=list[PhaseRead])
async def list_phases(community_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _community(db, user.tenant_id, community_id)
    phases = await db.scalars(
        select(CommunityPhase)
        .where(CommunityPhase.community_id == community_id)
        .order_by(CommunityPhase.sequence, CommunityPhase.name)
    )
    return [await _phase_read(db, p) for p in phases]


@router.post(
    "/communities/{community_id}/phases",
    response_model=PhaseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_phase(
    community_id: uuid.UUID, payload: PhaseCreate, db: DbSession, admin: RequireAdmin
):
    await _community(db, admin.tenant_id, community_id)
    name = payload.name.strip()
    exists = await db.scalar(
        select(CommunityPhase.id).where(
            CommunityPhase.community_id == community_id,
            func.lower(CommunityPhase.name) == name.lower(),
        )
    )
    if exists:
        raise _conflict(f"{name} already exists on this development.")
    sequence = payload.sequence
    if sequence is None:
        top = await db.scalar(
            select(func.max(CommunityPhase.sequence)).where(
                CommunityPhase.community_id == community_id
            )
        )
        sequence = (top or 0) + 1
    phase = CommunityPhase(
        tenant_id=admin.tenant_id,
        community_id=community_id,
        name=name,
        sequence=sequence,
        planned_release=payload.planned_release,
        notes=payload.notes,
    )
    db.add(phase)
    await db.commit()
    await db.refresh(phase)
    return await _phase_read(db, phase)


@router.patch("/phases/{phase_id}", response_model=PhaseRead)
async def update_phase(
    phase_id: uuid.UUID, payload: PhaseUpdate, db: DbSession, admin: RequireAdmin
):
    phase = await _phase(db, admin.tenant_id, phase_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in ("name", "sequence") and value is None:
            continue
        setattr(phase, key, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(phase)
    return await _phase_read(db, phase)


@router.delete("/phases/{phase_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phase(phase_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    phase = await _phase(db, admin.tenant_id, phase_id)
    in_use = await db.scalar(select(func.count()).select_from(Lot).where(Lot.phase_id == phase.id))
    if in_use:
        raise _conflict(f"{in_use} plots are in {phase.name}. Move them to another phase first.")
    await db.delete(phase)
    await db.commit()


@router.post("/phases/{phase_id}/release", response_model=PhaseRead)
async def release_phase(phase_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Put every unreleased plot in the phase on sale. The first release opens the
    development for sale, if it was still in planning."""
    phase = await _phase(db, user.tenant_id, phase_id)
    lots = list(
        await db.scalars(
            select(Lot).where(Lot.phase_id == phase.id, Lot.status == LotStatus.NOT_RELEASED)
        )
    )
    if not lots:
        raise _conflict(f"{phase.name} has no unreleased plots.")
    today = date.today()
    for lot in lots:
        lot.status = LotStatus.AVAILABLE
        lot.released_on = today
    phase.released_on = today
    community = await _community(db, user.tenant_id, phase.community_id)
    if community.status is CommunityStatus.PLANNING:
        community.status = CommunityStatus.SELLING
    if community.opened_on is None:
        community.opened_on = today
    await db.commit()
    await db.refresh(phase)
    return await _phase_read(db, phase)


# --- plans offered ---------------------------------------------------------------------


@router.get("/communities/{community_id}/plans", response_model=list[CommunityPlanRead])
async def community_plans(community_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _community(db, user.tenant_id, community_id)
    offered = list(
        await db.scalars(select(CommunityPlan).where(CommunityPlan.community_id == community_id))
    )
    plans = {
        p.id: p
        for p in await db.scalars(
            select(Plan).where(Plan.id.in_([o.plan_id for o in offered] or [uuid.uuid4()]))
        )
    }
    homes: dict[uuid.UUID, int] = defaultdict(int)
    for job in await db.scalars(
        select(Job).where(Job.community_id == community_id, Job.status.in_(LIVE_STATUSES))
    ):
        homes[job.plan_id] += 1
    out = []
    for o in offered:
        p = plans.get(o.plan_id)
        if p is None:
            continue
        out.append(
            CommunityPlanRead(
                plan_id=p.id,
                code=p.code,
                name=p.name,
                bedrooms=p.bedrooms,
                plan_base_price=p.base_price,
                base_price=o.base_price,
                effective_price=o.base_price if o.base_price is not None else p.base_price,
                is_active=o.is_active,
                homes=homes.get(p.id, 0),
            )
        )
    return sorted(out, key=lambda r: r.code)


@router.put("/communities/{community_id}/plans", response_model=list[CommunityPlanRead])
async def set_community_plan(
    community_id: uuid.UUID, payload: CommunityPlanSet, db: DbSession, admin: RequireAdmin
):
    """Offer a plan here, or change its price or availability. Idempotent."""
    await _community(db, admin.tenant_id, community_id)
    plan = await db.scalar(
        select(Plan).where(Plan.id == payload.plan_id, Plan.tenant_id == admin.tenant_id)
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    row = await db.scalar(
        select(CommunityPlan).where(
            CommunityPlan.community_id == community_id, CommunityPlan.plan_id == plan.id
        )
    )
    if row is None:
        row = CommunityPlan(tenant_id=admin.tenant_id, community_id=community_id, plan_id=plan.id)
        db.add(row)
    row.base_price = payload.base_price
    row.is_active = payload.is_active
    await db.commit()
    return await community_plans(community_id, db, admin)


@router.delete(
    "/communities/{community_id}/plans/{plan_id}", response_model=list[CommunityPlanRead]
)
async def remove_community_plan(
    community_id: uuid.UUID, plan_id: uuid.UUID, db: DbSession, admin: RequireAdmin
):
    """Stop offering a plan here. Refused while homes of it are being built on the site:
    withdraw it from sale instead, which keeps the record."""
    await _community(db, admin.tenant_id, community_id)
    building = await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.community_id == community_id,
            Job.plan_id == plan_id,
            Job.status.in_(LIVE_STATUSES),
        )
    )
    if building:
        raise _conflict(
            f"{building} homes of this plan are live here. Mark it not for sale instead."
        )
    row = await db.scalar(
        select(CommunityPlan).where(
            CommunityPlan.community_id == community_id, CommunityPlan.plan_id == plan_id
        )
    )
    if row is not None:
        await db.delete(row)
        await db.commit()
    return await community_plans(community_id, db, admin)


# --- plots ------------------------------------------------------------------------------


async def _lot_reads(db, tenant_id: uuid.UUID, lots: list[Lot]) -> list[LotRead]:
    jobs = await _live_jobs(db, tenant_id)
    lead_ids = {lot.held_for_lead_id for lot in lots if lot.held_for_lead_id}
    leads = (
        {lead.id: lead for lead in await db.scalars(select(Lead).where(Lead.id.in_(lead_ids)))}
        if lead_ids
        else {}
    )
    today = date.today()
    out = []
    for lot in sorted(lots, key=lambda x: _natural(x.lot_number)):
        r = LotRead.model_validate(lot)
        job = jobs.get(lot.id)
        if job is not None:
            r.job_id, r.job_number = job.id, job.job_number
            r.job_status, r.plan_id = job.status.value, job.plan_id
            r.is_spec = (
                job.status in LIVE_STATUSES and lot.status in UNSOLD and job.sold_date is None
            )
        if lot.held_for_lead_id in leads:
            r.held_for_name = leads[lot.held_for_lead_id].full_name
        if lot.status is LotStatus.HELD and lot.hold_until:
            r.hold_days_left = (lot.hold_until - today).days
        out.append(r)
    return out


async def _one(db, tenant_id, lot: Lot) -> LotRead:
    await db.refresh(lot)
    return (await _lot_reads(db, tenant_id, [lot]))[0]


@router.get("/communities/{community_id}/lots", response_model=list[LotRead])
async def list_lots(
    community_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    status_filter: LotStatus | None = Query(default=None, alias="status"),
    phase_id: uuid.UUID | None = None,
    buildable: bool = Query(
        default=False, description="Only plots with no live home on them: somewhere to build."
    ),
):
    await _community(db, user.tenant_id, community_id)
    await sweep_expired_holds(db, user.tenant_id)
    stmt = select(Lot).where(Lot.tenant_id == user.tenant_id, Lot.community_id == community_id)
    if status_filter is not None:
        stmt = stmt.where(Lot.status == status_filter)
    if phase_id is not None:
        stmt = stmt.where(Lot.phase_id == phase_id)
    rows = await _lot_reads(db, user.tenant_id, list(await db.scalars(stmt)))
    if buildable:
        rows = [r for r in rows if r.job_id is None and r.status is not LotStatus.CLOSED]
    return rows


async def _require_unique_number(db, community_id, number: str, except_id=None) -> None:
    stmt = select(Lot.id).where(
        Lot.community_id == community_id, func.lower(Lot.lot_number) == number.lower()
    )
    if except_id is not None:
        stmt = stmt.where(Lot.id != except_id)
    if await db.scalar(stmt):
        raise _conflict(f"Plot {number} already exists in this community.")


@router.post(
    "/communities/{community_id}/lots", response_model=LotRead, status_code=status.HTTP_201_CREATED
)
async def create_lot(
    community_id: uuid.UUID, payload: LotCreate, db: DbSession, admin: RequireAdmin
):
    await _community(db, admin.tenant_id, community_id)
    if payload.status not in (LotStatus.AVAILABLE, LotStatus.NOT_RELEASED):
        raise HTTPException(
            status_code=422,
            detail="A new plot is either on sale or not yet released. Holds and sales are "
            "recorded on the plot once it exists.",
        )
    number = payload.lot_number.strip()
    await _require_unique_number(db, community_id, number)
    await _require_phase_in(db, admin.tenant_id, community_id, payload.phase_id)
    data = payload.model_dump()
    data["lot_number"] = number
    if data.get("postcode"):
        data["postcode"] = data["postcode"].strip().upper()
    lot = Lot(tenant_id=admin.tenant_id, community_id=community_id, **data)
    if lot.status is LotStatus.AVAILABLE:
        lot.released_on = date.today()
    db.add(lot)
    await db.commit()
    return await _one(db, admin.tenant_id, lot)


@router.post(
    "/communities/{community_id}/lots/bulk",
    response_model=LotBulkResult,
    status_code=status.HTTP_201_CREATED,
)
async def bulk_create_lots(
    community_id: uuid.UUID, payload: LotBulkCreate, db: DbSession, admin: RequireAdmin
):
    """Lay out a run of plots at once. Numbers that already exist are skipped, not
    duplicated, so the same range can be run again safely."""
    await _community(db, admin.tenant_id, community_id)
    if payload.end < payload.start:
        raise HTTPException(status_code=422, detail="The last plot number is before the first.")
    if payload.end - payload.start >= 500:
        raise HTTPException(status_code=422, detail="Up to 500 plots at a time.")
    if payload.status not in (LotStatus.AVAILABLE, LotStatus.NOT_RELEASED):
        raise HTTPException(status_code=422, detail="New plots are on sale or not released.")
    await _require_phase_in(db, admin.tenant_id, community_id, payload.phase_id)
    existing = {
        n.lower()
        for n in await db.scalars(select(Lot.lot_number).where(Lot.community_id == community_id))
    }
    today = date.today()
    postcode = payload.postcode.strip().upper() if payload.postcode else None
    created, skipped = 0, []
    for n in range(payload.start, payload.end + 1):
        number = f"{payload.prefix.strip()}{n}"
        if number.lower() in existing:
            skipped.append(number)
            continue
        db.add(
            Lot(
                tenant_id=admin.tenant_id,
                community_id=community_id,
                lot_number=number,
                status=payload.status,
                phase_id=payload.phase_id,
                premium=payload.premium,
                postcode=postcode,
                released_on=today if payload.status is LotStatus.AVAILABLE else None,
            )
        )
        created += 1
    await db.commit()
    return LotBulkResult(created=created, skipped=skipped)


@router.patch("/lots/{lot_id}", response_model=LotRead)
async def update_lot(lot_id: uuid.UUID, payload: LotUpdate, db: DbSession, admin: RequireAdmin):
    """Correct a plot. The premium is fixed once sold: the contract priced it."""
    lot = await _lot(db, admin.tenant_id, lot_id)
    data = payload.model_dump(exclude_unset=True)
    if (
        "premium" in data
        and data["premium"] is not None
        and data["premium"] != lot.premium
        and lot.status in (LotStatus.SOLD, LotStatus.CLOSED)
    ):
        raise _conflict("The plot is sold; its premium is part of the contract now.")
    if data.get("lot_number"):
        data["lot_number"] = data["lot_number"].strip()
        await _require_unique_number(db, lot.community_id, data["lot_number"], except_id=lot.id)
    if "phase_id" in data:
        await _require_phase_in(db, admin.tenant_id, lot.community_id, data["phase_id"])
    if data.get("postcode"):
        data["postcode"] = data["postcode"].strip().upper()
    for key, value in data.items():
        if key in ("lot_number", "premium") and value is None:
            continue
        setattr(lot, key, value)
    await db.commit()
    return await _one(db, admin.tenant_id, lot)


@router.post("/lots/{lot_id}/release", response_model=LotRead)
async def release_lot(lot_id: uuid.UUID, db: DbSession, user: RequireSales):
    lot = await _lot(db, user.tenant_id, lot_id)
    if lot.status is not LotStatus.NOT_RELEASED:
        raise _conflict(f"Plot {lot.lot_number} is already {lot.status.value.replace('_', ' ')}.")
    lot.status = LotStatus.AVAILABLE
    lot.released_on = date.today()
    await db.commit()
    return await _one(db, user.tenant_id, lot)


@router.post("/lots/{lot_id}/withdraw", response_model=LotRead)
async def withdraw_lot(lot_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Take an unsold plot off the market. A held plot's hold has to be lifted first, so
    nobody's reservation disappears silently."""
    lot = await _lot(db, user.tenant_id, lot_id)
    if lot.status is not LotStatus.AVAILABLE:
        raise _conflict(
            f"Only a plot on sale can be withdrawn; plot {lot.lot_number} is "
            f"{lot.status.value.replace('_', ' ')}."
        )
    lot.status = LotStatus.NOT_RELEASED
    await db.commit()
    return await _one(db, user.tenant_id, lot)


@router.post("/lots/{lot_id}/hold", response_model=LotRead)
async def hold_lot(lot_id: uuid.UUID, payload: LotHold, db: DbSession, user: RequireSales):
    """Reserve a plot for a prospective buyer, until a date. Lapses on its own after."""
    lot = await _lot(db, user.tenant_id, lot_id)
    today = date.today()
    if lot.status is not LotStatus.AVAILABLE:
        raise _conflict(
            f"Plot {lot.lot_number} is {lot.status.value.replace('_', ' ')}; "
            "only a plot on sale can be held."
        )
    if payload.until < today:
        raise HTTPException(status_code=422, detail="A hold has to end in the future.")
    if payload.until > today + timedelta(days=MAX_HOLD_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"A hold can run for at most {MAX_HOLD_DAYS} days. Longer is a sale.",
        )
    if payload.lead_id is not None:
        lead = await db.scalar(
            select(Lead.id).where(Lead.id == payload.lead_id, Lead.tenant_id == user.tenant_id)
        )
        if lead is None:
            raise HTTPException(status_code=422, detail="That lead is not in this organisation.")
    lot.status = LotStatus.HELD
    lot.hold_until = payload.until
    lot.hold_reason = payload.reason
    lot.held_for_lead_id = payload.lead_id
    lot.held_on = today
    lot.held_by_id = user.id
    await db.commit()
    return await _one(db, user.tenant_id, lot)


@router.post("/lots/{lot_id}/unhold", response_model=LotRead)
async def unhold_lot(lot_id: uuid.UUID, db: DbSession, user: RequireSales):
    lot = await _lot(db, user.tenant_id, lot_id)
    if lot.status is not LotStatus.HELD:
        raise _conflict(f"Plot {lot.lot_number} is not on hold.")
    lot.status = LotStatus.AVAILABLE
    lot.clear_hold()
    await db.commit()
    return await _one(db, user.tenant_id, lot)
