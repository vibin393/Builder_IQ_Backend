"""House plans: the specification, elevations, standard costs, price history and pricing.

Moved out of catalog.py when a plan stopped being a code, a name and a price. A plan is
a template: editing it changes what the *next* home is budgeted and priced at, never a
budget or contract already made from it.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.community import CommunityPlan, Lot
from app.models.cost_code import CostCode
from app.models.invoice import ACTUAL_STATUSES, Invoice
from app.models.job import LIVE_STATUSES, Job, JobStatus
from app.models.plan import (
    SQFT_PER_SQM,
    Plan,
    PlanCostLine,
    PlanElevation,
    PlanPriceChange,
)
from app.models.vendor import Vendor
from app.schemas.plans import (
    ElevationCreate,
    ElevationRead,
    ElevationUpdate,
    PlanCopy,
    PlanCostLineCreate,
    PlanCostLineRead,
    PlanCostLineUpdate,
    PlanCreate,
    PlanPortfolio,
    PlanRead,
    PlanStats,
    PlanUpdate,
    PriceChangeRead,
    PricingGuide,
)

router = APIRouter(tags=["plans"])

#: Below this gross margin on the standard cost, a plan is flagged. A UK housebuilder
#: targets well above it; below it, one overrun wipes the profit on the house.
MARGIN_FLOOR_PCT = Decimal("15")
FINISHED = (JobStatus.COMPLETE, JobStatus.CLOSED, JobStatus.WARRANTY)
ZERO = Decimal("0")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return (part / whole * 100).quantize(Decimal("0.1")) if whole else None


def _fill_area(data: dict) -> None:
    """Either measurement fills in the other. Quoting both is normal in the UK; typing
    both is how they end up disagreeing."""
    sqm, sqft = data.get("floor_area_sqm"), data.get("square_feet")
    if sqm is not None and sqft is None:
        data["square_feet"] = int((Decimal(sqm) * SQFT_PER_SQM).to_integral_value())
    elif sqft is not None and sqm is None:
        data["floor_area_sqm"] = (Decimal(sqft) / SQFT_PER_SQM).quantize(Decimal("0.1"))


async def _plan(db, tenant_id: uuid.UUID, plan_id: uuid.UUID) -> Plan:
    p = await db.scalar(select(Plan).where(Plan.id == plan_id, Plan.tenant_id == tenant_id))
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return p


async def _require_unique_code(db, tenant_id, code: str, except_id=None) -> None:
    stmt = select(Plan.id).where(Plan.tenant_id == tenant_id, func.upper(Plan.code) == code.upper())
    if except_id is not None:
        stmt = stmt.where(Plan.id != except_id)
    if await db.scalar(stmt):
        raise _conflict(f"Plan {code} already exists.")


async def _stats_for(db, tenant_id: uuid.UUID, plans: list[Plan]) -> dict[uuid.UUID, PlanStats]:
    """Every plan's numbers in a handful of queries, not a handful per plan."""
    ids = [p.id for p in plans] or [uuid.uuid4()]
    lines: dict[uuid.UUID, list[PlanCostLine]] = defaultdict(list)
    for ln in await db.scalars(select(PlanCostLine).where(PlanCostLine.plan_id.in_(ids))):
        lines[ln.plan_id].append(ln)
    elevations: dict[uuid.UUID, int] = defaultdict(int)
    for e in await db.scalars(select(PlanElevation).where(PlanElevation.plan_id.in_(ids))):
        elevations[e.plan_id] += 1
    communities: dict[uuid.UUID, int] = defaultdict(int)
    for cp in await db.scalars(
        select(CommunityPlan).where(CommunityPlan.plan_id.in_(ids), CommunityPlan.is_active)
    ):
        communities[cp.plan_id] += 1

    jobs = list(
        await db.scalars(
            select(Job).where(
                Job.tenant_id == tenant_id, Job.plan_id.in_(ids),
                Job.status != JobStatus.CANCELLED,
            )
        )
    )
    finished_ids = {j.id for j in jobs if j.status in FINISHED}
    actual_by_job: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    if finished_ids:
        for inv in await db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.job_id.in_(finished_ids), Invoice.status.in_(ACTUAL_STATUSES))
        ):
            actual_by_job[inv.job_id] += inv.total

    out: dict[uuid.UUID, PlanStats] = {}
    for p in plans:
        standard = sum((ln.amount for ln in lines[p.id]), ZERO)
        margin = p.base_price - standard
        mine = [j for j in jobs if j.plan_id == p.id]
        finished = [j for j in mine if j.id in finished_ids and actual_by_job[j.id] > 0]
        avg_actual = (
            (sum((actual_by_job[j.id] for j in finished), ZERO) / len(finished)).quantize(
                Decimal("0.01")
            )
            if finished
            else None
        )
        out[p.id] = PlanStats(
            standard_cost=standard,
            cost_lines=len(lines[p.id]),
            labour_hours=sum((ln.labour_hours or ZERO for ln in lines[p.id]), ZERO),
            margin=margin,
            margin_pct=_pct(margin, p.base_price),
            elevations=elevations[p.id],
            communities=communities[p.id],
            live_homes=sum(1 for j in mine if j.status in LIVE_STATUSES),
            finished_homes=sum(1 for j in mine if j.status in FINISHED),
            avg_actual_cost=avg_actual,
            cost_variance_pct=(
                _pct(avg_actual - standard, standard) if avg_actual is not None else None
            ),
        )
    return out


async def _read(db, tenant_id, plan: Plan) -> PlanRead:
    r = PlanRead.model_validate(plan)
    r.stats = (await _stats_for(db, tenant_id, [plan]))[plan.id]
    return r


# --- plans -------------------------------------------------------------------------


@router.get("/plans", response_model=list[PlanRead])
async def list_plans(
    db: DbSession,
    user: RequireStaff,
    active: bool | None = Query(default=None, description="True: on sale. False: archived."),
):
    stmt = select(Plan).where(Plan.tenant_id == user.tenant_id)
    if active is not None:
        stmt = stmt.where(Plan.is_active.is_(active))
    plans = list(await db.scalars(stmt.order_by(Plan.code)))
    stats = await _stats_for(db, user.tenant_id, plans)
    out = []
    for p in plans:
        r = PlanRead.model_validate(p)
        r.stats = stats[p.id]
        out.append(r)
    return out


@router.get("/plans/summary", response_model=PlanPortfolio)
async def plans_summary(db: DbSession, user: RequireStaff):
    plans = list(await db.scalars(select(Plan).where(Plan.tenant_id == user.tenant_id)))
    stats = await _stats_for(db, user.tenant_id, plans)
    active = [p for p in plans if p.is_active]
    margins = [stats[p.id].margin_pct for p in active if stats[p.id].cost_lines]
    since = datetime.now(UTC) - timedelta(days=90)
    changes = await db.scalar(
        select(func.count()).select_from(PlanPriceChange).where(
            PlanPriceChange.tenant_id == user.tenant_id, PlanPriceChange.created_at >= since
        )
    )
    return PlanPortfolio(
        active=len(active),
        archived=len(plans) - len(active),
        avg_margin_pct=(
            (sum(m for m in margins if m is not None) / len(margins)).quantize(Decimal("0.1"))
            if margins
            else None
        ),
        low_margin=sum(
            1 for p in active
            if stats[p.id].cost_lines
            and stats[p.id].margin_pct is not None
            and stats[p.id].margin_pct < MARGIN_FLOOR_PCT
        ),
        margin_floor_pct=MARGIN_FLOOR_PCT,
        no_costs=sum(1 for p in active if not stats[p.id].cost_lines),
        live_homes=sum(stats[p.id].live_homes for p in plans),
        price_changes_90_days=changes or 0,
    )


@router.post("/plans", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def create_plan(payload: PlanCreate, db: DbSession, admin: RequireAdmin):
    data = payload.model_dump()
    data["code"] = data["code"].strip().upper()
    if data.get("epc_target"):
        data["epc_target"] = data["epc_target"].upper()
    await _require_unique_code(db, admin.tenant_id, data["code"])
    _fill_area(data)
    plan = Plan(tenant_id=admin.tenant_id, **data)
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return await _read(db, admin.tenant_id, plan)


@router.get("/plans/{plan_id}", response_model=PlanRead)
async def get_plan(plan_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await _read(db, user.tenant_id, await _plan(db, user.tenant_id, plan_id))


@router.patch("/plans/{plan_id}", response_model=PlanRead)
async def update_plan(plan_id: uuid.UUID, payload: PlanUpdate, db: DbSession, admin: RequireAdmin):
    """Edit the specification or price. A price change is logged with who and why, and
    only reaches homes sold after it - a signed contract keeps its price."""
    plan = await _plan(db, admin.tenant_id, plan_id)
    data = payload.model_dump(exclude_unset=True)
    reason = data.pop("price_reason", None)
    if data.get("code"):
        data["code"] = data["code"].strip().upper()
        await _require_unique_code(db, admin.tenant_id, data["code"], except_id=plan.id)
    if data.get("epc_target"):
        data["epc_target"] = data["epc_target"].upper()
    # Only fill the other measure when just one was sent; sending both is deliberate.
    if ("floor_area_sqm" in data) != ("square_feet" in data):
        _fill_area(data)

    new_price = data.get("base_price")
    if new_price is not None and new_price != plan.base_price:
        db.add(
            PlanPriceChange(
                tenant_id=admin.tenant_id, plan_id=plan.id, old_price=plan.base_price,
                new_price=new_price, changed_by_id=admin.id, reason=reason,
            )
        )
    for key, value in data.items():
        if key in ("code", "name", "base_price", "has_cloakroom") and value is None:
            continue
        setattr(plan, key, value)
    await db.commit()
    await db.refresh(plan)
    return await _read(db, admin.tenant_id, plan)


@router.post("/plans/{plan_id}/archive", response_model=PlanRead)
async def archive_plan(plan_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    """Take a house type off the price list. Homes already on it carry on; no new home
    can be started on it."""
    plan = await _plan(db, admin.tenant_id, plan_id)
    if not plan.is_active:
        raise _conflict(f"{plan.code} is already archived.")
    plan.is_active = False
    for cp in await db.scalars(select(CommunityPlan).where(CommunityPlan.plan_id == plan.id)):
        cp.is_active = False
    await db.commit()
    await db.refresh(plan)
    return await _read(db, admin.tenant_id, plan)


@router.post("/plans/{plan_id}/restore", response_model=PlanRead)
async def restore_plan(plan_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    plan = await _plan(db, admin.tenant_id, plan_id)
    if plan.is_active:
        raise _conflict(f"{plan.code} is not archived.")
    plan.is_active = True
    await db.commit()
    await db.refresh(plan)
    return await _read(db, admin.tenant_id, plan)


@router.post("/plans/{plan_id}/copy", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def copy_plan(plan_id: uuid.UUID, payload: PlanCopy, db: DbSession, admin: RequireAdmin):
    """A new house type from an existing one: specification, standard costs and
    elevations. The usual way a variant (the Maple with a garage) is set up."""
    src = await _plan(db, admin.tenant_id, plan_id)
    code = payload.code.strip().upper()
    await _require_unique_code(db, admin.tenant_id, code)
    fields = (
        "square_feet", "floor_area_sqm", "bedrooms", "bathrooms", "en_suites", "has_cloakroom",
        "house_type", "storeys", "garage", "parking_spaces", "epc_target", "description",
        "base_price",
    )
    new = Plan(
        tenant_id=admin.tenant_id, code=code, name=payload.name.strip(),
        **{f: getattr(src, f) for f in fields},
    )
    db.add(new)
    await db.flush()
    for ln in await db.scalars(select(PlanCostLine).where(PlanCostLine.plan_id == src.id)):
        db.add(
            PlanCostLine(
                tenant_id=admin.tenant_id, plan_id=new.id, cost_code_id=ln.cost_code_id,
                vendor_id=ln.vendor_id, amount=ln.amount, labour_hours=ln.labour_hours,
                description=ln.description,
            )
        )
    for e in await db.scalars(select(PlanElevation).where(PlanElevation.plan_id == src.id)):
        db.add(
            PlanElevation(
                tenant_id=admin.tenant_id, plan_id=new.id, code=e.code, name=e.name,
                price_adjustment=e.price_adjustment, description=e.description,
                is_active=e.is_active,
            )
        )
    await db.commit()
    await db.refresh(new)
    return await _read(db, admin.tenant_id, new)


@router.get("/plans/{plan_id}/price-history", response_model=list[PriceChangeRead])
async def price_history(plan_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _plan(db, user.tenant_id, plan_id)
    return list(
        await db.scalars(
            select(PlanPriceChange)
            .where(PlanPriceChange.plan_id == plan_id)
            .order_by(PlanPriceChange.created_at.desc())
        )
    )


@router.get("/plans/{plan_id}/pricing", response_model=PricingGuide)
async def pricing_guide(
    plan_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    community_id: uuid.UUID | None = None,
    elevation: str | None = Query(default=None, max_length=32),
    lot_id: uuid.UUID | None = None,
):
    """The price a home should be sold at: this development's price for the plan (or the
    plan's own), plus the elevation, plus the plot premium."""
    plan = await _plan(db, user.tenant_id, plan_id)
    community_price = None
    if community_id is not None:
        cp = await db.scalar(
            select(CommunityPlan).where(
                CommunityPlan.community_id == community_id, CommunityPlan.plan_id == plan.id,
                CommunityPlan.tenant_id == user.tenant_id,
            )
        )
        community_price = cp.base_price if cp is not None else None
    house = community_price if community_price is not None else plan.base_price

    adjustment = ZERO
    code = None
    if elevation:
        e = await db.scalar(
            select(PlanElevation).where(
                PlanElevation.plan_id == plan.id,
                func.upper(PlanElevation.code) == elevation.strip().upper(),
            )
        )
        if e is not None:
            adjustment, code = e.price_adjustment, e.code

    premium = ZERO
    if lot_id is not None:
        lot = await db.scalar(select(Lot).where(Lot.id == lot_id, Lot.tenant_id == user.tenant_id))
        premium = lot.premium if lot is not None else ZERO

    base = house + adjustment
    return PricingGuide(
        plan_price=plan.base_price, community_price=community_price, house_price=house,
        elevation_code=code, elevation_adjustment=adjustment, plot_premium=premium,
        base_price=base, total=base + premium,
    )


# --- standard cost lines ----------------------------------------------------------------


async def _line(db, tenant_id, line_id: uuid.UUID) -> PlanCostLine:
    ln = await db.scalar(
        select(PlanCostLine).where(PlanCostLine.id == line_id, PlanCostLine.tenant_id == tenant_id)
    )
    if ln is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost line not found")
    return ln


async def _require_vendor(db, tenant_id, vendor_id) -> None:
    if vendor_id is None:
        return
    if not await db.scalar(
        select(Vendor.id).where(Vendor.id == vendor_id, Vendor.tenant_id == tenant_id)
    ):
        raise HTTPException(status_code=422, detail="That trade is not in this organisation.")


@router.get("/plans/{plan_id}/cost-lines", response_model=list[PlanCostLineRead])
async def list_plan_cost_lines(plan_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _plan(db, user.tenant_id, plan_id)
    rows = list(await db.scalars(select(PlanCostLine).where(PlanCostLine.plan_id == plan_id)))
    codes = {
        c.id: c.code
        for c in await db.scalars(
            select(CostCode).where(
                CostCode.id.in_([r.cost_code_id for r in rows] or [uuid.uuid4()])
            )
        )
    }
    return sorted(rows, key=lambda r: codes.get(r.cost_code_id, ""))


@router.post(
    "/plans/{plan_id}/cost-lines", response_model=PlanCostLineRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_plan_cost_line(
    plan_id: uuid.UUID, payload: PlanCostLineCreate, db: DbSession, admin: RequireAdmin
):
    await _plan(db, admin.tenant_id, plan_id)
    code = await db.scalar(
        select(CostCode).where(
            CostCode.id == payload.cost_code_id, CostCode.tenant_id == admin.tenant_id
        )
    )
    if code is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost code not found")
    # One line per cost code: the budget is keyed by cost code. This hit the unique
    # index and came back as a server error.
    if await db.scalar(
        select(PlanCostLine.id).where(
            PlanCostLine.plan_id == plan_id, PlanCostLine.cost_code_id == code.id
        )
    ):
        raise _conflict(f"{code.code} is already on this plan. Edit that line instead.")
    await _require_vendor(db, admin.tenant_id, payload.vendor_id)
    line = PlanCostLine(tenant_id=admin.tenant_id, plan_id=plan_id, **payload.model_dump())
    db.add(line)
    await db.commit()
    await db.refresh(line)
    return line


@router.patch("/plan-cost-lines/{line_id}", response_model=PlanCostLineRead)
async def update_plan_cost_line(
    line_id: uuid.UUID, payload: PlanCostLineUpdate, db: DbSession, admin: RequireAdmin
):
    """Correct a standard cost. Budgets already generated keep the figure they took."""
    line = await _line(db, admin.tenant_id, line_id)
    data = payload.model_dump(exclude_unset=True)
    if "vendor_id" in data:
        await _require_vendor(db, admin.tenant_id, data["vendor_id"])
    for key, value in data.items():
        if key == "amount" and value is None:
            continue
        setattr(line, key, value)
    await db.commit()
    await db.refresh(line)
    return line


@router.delete("/plan-cost-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan_cost_line(line_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    line = await _line(db, admin.tenant_id, line_id)
    await db.delete(line)
    await db.commit()


# --- elevations ---------------------------------------------------------------------------


async def _elevation(db, tenant_id, elevation_id: uuid.UUID) -> PlanElevation:
    e = await db.scalar(
        select(PlanElevation).where(
            PlanElevation.id == elevation_id, PlanElevation.tenant_id == tenant_id
        )
    )
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Elevation not found")
    return e


async def _elevation_homes(db, plan_id: uuid.UUID) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for j in await db.scalars(
        select(Job).where(Job.plan_id == plan_id, Job.status != JobStatus.CANCELLED)
    ):
        if j.elevation:
            out[j.elevation.upper()] += 1
    return out


@router.get("/plans/{plan_id}/elevations", response_model=list[ElevationRead])
async def list_elevations(plan_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _plan(db, user.tenant_id, plan_id)
    homes = await _elevation_homes(db, plan_id)
    out = []
    for e in await db.scalars(
        select(PlanElevation).where(PlanElevation.plan_id == plan_id).order_by(PlanElevation.code)
    ):
        r = ElevationRead.model_validate(e)
        r.homes = homes.get(e.code.upper(), 0)
        out.append(r)
    return out


async def _require_unique_elevation(db, plan_id, code: str, except_id=None) -> None:
    stmt = select(PlanElevation.id).where(
        PlanElevation.plan_id == plan_id, func.upper(PlanElevation.code) == code.upper()
    )
    if except_id is not None:
        stmt = stmt.where(PlanElevation.id != except_id)
    if await db.scalar(stmt):
        raise _conflict(f"Elevation {code} already exists on this plan.")


@router.post(
    "/plans/{plan_id}/elevations", response_model=ElevationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_elevation(
    plan_id: uuid.UUID, payload: ElevationCreate, db: DbSession, admin: RequireAdmin
):
    await _plan(db, admin.tenant_id, plan_id)
    code = payload.code.strip().upper()
    await _require_unique_elevation(db, plan_id, code)
    e = PlanElevation(
        tenant_id=admin.tenant_id, plan_id=plan_id, code=code, name=payload.name.strip(),
        price_adjustment=payload.price_adjustment, description=payload.description,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return ElevationRead.model_validate(e)


@router.patch("/plan-elevations/{elevation_id}", response_model=ElevationRead)
async def update_elevation(
    elevation_id: uuid.UUID, payload: ElevationUpdate, db: DbSession, admin: RequireAdmin
):
    e = await _elevation(db, admin.tenant_id, elevation_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("code"):
        data["code"] = data["code"].strip().upper()
        if data["code"] != e.code:
            if (await _elevation_homes(db, e.plan_id)).get(e.code.upper()):
                raise _conflict(
                    f"Homes are recorded as elevation {e.code}. Renaming the code would "
                    "detach them; change the name instead."
                )
            await _require_unique_elevation(db, e.plan_id, data["code"], except_id=e.id)
    for key, value in data.items():
        if key in ("code", "name", "price_adjustment", "is_active") and value is None:
            continue
        setattr(e, key, value)
    await db.commit()
    await db.refresh(e)
    r = ElevationRead.model_validate(e)
    r.homes = (await _elevation_homes(db, e.plan_id)).get(e.code.upper(), 0)
    return r


@router.delete("/plan-elevations/{elevation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_elevation(elevation_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    e = await _elevation(db, admin.tenant_id, elevation_id)
    if (await _elevation_homes(db, e.plan_id)).get(e.code.upper()):
        raise _conflict(f"Homes are built as elevation {e.code}. Withdraw it instead.")
    await db.delete(e)
    await db.commit()
