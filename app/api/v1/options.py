"""The option catalog: what a buyer can add to a home, at what price and what cost.

Moved out of catalog.py. An option is a price, a per-cost-code cost, a schedule effect
and a structural flag; changing it changes the *next* selection - a selection snapshots
its price, cost and cost breakdown when it is made.
"""

import uuid
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.cost_code import CostCode
from app.models.job import Job, JobStatus
from app.models.option import (
    Option,
    OptionCategory,
    OptionCostLine,
    OptionPlan,
    OptionPriceChange,
)
from app.models.plan import Plan
from app.models.selection import Selection, SelectionStatus
from app.models.vendor import Vendor
from app.schemas.options import (
    OptionCopy,
    OptionCostLineCreate,
    OptionCostLineRead,
    OptionCostLineUpdate,
    OptionCreate,
    OptionDetail,
    OptionPlansSet,
    OptionPortfolio,
    OptionPriceChangeRead,
    OptionRead,
    OptionSelectionRow,
    OptionStats,
    OptionUpdate,
    PopularOption,
)

router = APIRouter(tags=["options"])

#: Options are where a builder makes its margin; below this, an upgrade is a favour.
MARGIN_FLOOR_PCT = Decimal("20")
ZERO = Decimal("0")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return (part / whole * 100).quantize(Decimal("0.1")) if whole else None


async def _option(db, tenant_id, option_id: uuid.UUID) -> Option:
    o = await db.scalar(
        select(Option)
        .options(selectinload(Option.cost_lines))
        .where(Option.id == option_id, Option.tenant_id == tenant_id)
    )
    if o is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Option not found")
    return o


async def _require_unique_code(db, tenant_id, code: str, except_id=None) -> None:
    stmt = select(Option.id).where(
        Option.tenant_id == tenant_id, func.upper(Option.code) == code.upper()
    )
    if except_id is not None:
        stmt = stmt.where(Option.id != except_id)
    if await db.scalar(stmt):
        raise _conflict(f"Option {code} already exists.")


async def _set_plans(db, tenant_id, option_id, plan_ids: list[uuid.UUID]) -> None:
    ids = list(dict.fromkeys(plan_ids))
    if ids:
        found = set(
            await db.scalars(select(Plan.id).where(Plan.tenant_id == tenant_id, Plan.id.in_(ids)))
        )
        if len(found) != len(ids):
            raise HTTPException(status_code=422, detail="One of those plans does not exist.")
    for row in await db.scalars(select(OptionPlan).where(OptionPlan.option_id == option_id)):
        await db.delete(row)
    await db.flush()
    for pid in ids:
        db.add(OptionPlan(tenant_id=tenant_id, option_id=option_id, plan_id=pid))


async def _stats(db, tenant_id, options: list[Option]) -> dict[uuid.UUID, OptionStats]:
    ids = [o.id for o in options] or [uuid.uuid4()]
    plans: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for op in await db.scalars(select(OptionPlan).where(OptionPlan.option_id.in_(ids))):
        plans[op.option_id].add(op.plan_id)
    sels = list(
        await db.scalars(
            select(Selection).where(
                Selection.tenant_id == tenant_id, Selection.option_id.in_(ids),
                Selection.status != SelectionStatus.CANCELLED,
            )
        )
    )
    # Sold homes, and their plans, for the attach rate.
    sold = list(
        await db.scalars(
            select(Job).where(
                Job.tenant_id == tenant_id, Job.sold_date.isnot(None),
                Job.status != JobStatus.CANCELLED,
            )
        )
    )
    out = {}
    for o in options:
        cost = sum((cl.amount for cl in o.cost_lines), ZERO)
        mine = [s for s in sels if s.option_id == o.id]
        confirmed = [s for s in mine if s.status is SelectionStatus.CONFIRMED]
        eligible = [j for j in sold if not plans[o.id] or j.plan_id in plans[o.id]]
        homes_with = {s.job_id for s in confirmed}
        out[o.id] = OptionStats(
            cost=cost,
            margin=o.price - cost,
            margin_pct=_pct(o.price - cost, o.price),
            cost_lines=len(o.cost_lines),
            confirmed=sum(s.quantity for s in confirmed),
            pending=sum(1 for s in mine if s.status is SelectionStatus.PENDING),
            revenue=sum((s.extended_price for s in confirmed), ZERO),
            attach_rate_pct=_pct(Decimal(len(homes_with)), Decimal(len(eligible))),
            plans=len(plans[o.id]),
        )
    return out


async def _detail(db, tenant_id, o: Option) -> OptionDetail:
    stats = (await _stats(db, tenant_id, [o]))[o.id]
    plan_ids = list(
        await db.scalars(select(OptionPlan.plan_id).where(OptionPlan.option_id == o.id))
    )
    return OptionDetail(
        **OptionRead.model_validate(o).model_dump(exclude={"stats"}),
        stats=stats,
        cost_lines=[OptionCostLineRead.model_validate(cl) for cl in o.cost_lines],
        total_cost=o.total_cost,
        plan_ids=plan_ids,
    )


# --- catalog ----------------------------------------------------------------------------


@router.get("/options", response_model=list[OptionRead])
async def list_options(
    db: DbSession,
    user: RequireStaff,
    category: OptionCategory | None = None,
    active: bool | None = None,
    search: str | None = Query(default=None, max_length=100),
    plan_id: uuid.UUID | None = Query(
        default=None, description="Only options that can go on this house type."
    ),
):
    stmt = (
        select(Option)
        .options(selectinload(Option.cost_lines))
        .where(Option.tenant_id == user.tenant_id)
    )
    if category is not None:
        stmt = stmt.where(Option.category == category)
    if active is not None:
        stmt = stmt.where(Option.is_active.is_(active))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Option.code.ilike(like), Option.name.ilike(like)))
    options = list(await db.scalars(stmt.order_by(Option.category, Option.code)))
    if plan_id is not None:
        restricted: dict[uuid.UUID, set] = defaultdict(set)
        ids = [o.id for o in options] or [uuid.uuid4()]
        for op in await db.scalars(select(OptionPlan).where(OptionPlan.option_id.in_(ids))):
            restricted[op.option_id].add(op.plan_id)
        options = [o for o in options if not restricted[o.id] or plan_id in restricted[o.id]]
    stats = await _stats(db, user.tenant_id, options)
    out = []
    for o in options:
        r = OptionRead.model_validate(o)
        r.stats = stats[o.id]
        out.append(r)
    return out


@router.get("/options/summary", response_model=OptionPortfolio)
async def options_summary(db: DbSession, user: RequireStaff):
    options = list(
        await db.scalars(
            select(Option)
            .options(selectinload(Option.cost_lines))
            .where(Option.tenant_id == user.tenant_id)
        )
    )
    stats = await _stats(db, user.tenant_id, options)
    active = [o for o in options if o.is_active]
    priced = [o for o in active if stats[o.id].cost_lines and o.price > 0]
    margins = [stats[o.id].margin_pct for o in priced if stats[o.id].margin_pct is not None]
    by_category: dict[str, int] = {}
    for o in active:
        by_category[o.category.value] = by_category.get(o.category.value, 0) + 1
    top = sorted(options, key=lambda o: (-stats[o.id].confirmed, -stats[o.id].revenue))[:5]
    return OptionPortfolio(
        active=len(active),
        archived=len(options) - len(active),
        by_category=by_category,
        avg_margin_pct=(
            (sum(margins) / len(margins)).quantize(Decimal("0.1")) if margins else None
        ),
        low_margin=sum(
            1 for o in priced
            if stats[o.id].margin_pct is not None and stats[o.id].margin_pct < MARGIN_FLOOR_PCT
        ),
        margin_floor_pct=MARGIN_FLOOR_PCT,
        no_costs=sum(1 for o in active if o.price > 0 and not stats[o.id].cost_lines),
        confirmed_revenue=sum((stats[o.id].revenue for o in options), ZERO),
        pending_selections=sum(stats[o.id].pending for o in options),
        top=[
            PopularOption(
                option_id=o.id, code=o.code, name=o.name,
                confirmed=stats[o.id].confirmed, revenue=stats[o.id].revenue,
            )
            for o in top
            if stats[o.id].confirmed
        ],
    )


@router.post("/options", response_model=OptionDetail, status_code=status.HTTP_201_CREATED)
async def create_option(payload: OptionCreate, db: DbSession, admin: RequireAdmin):
    data = payload.model_dump()
    plan_ids = data.pop("plan_ids")
    data["code"] = data["code"].strip().upper()
    await _require_unique_code(db, admin.tenant_id, data["code"])
    o = Option(tenant_id=admin.tenant_id, **data)
    db.add(o)
    await db.flush()
    await _set_plans(db, admin.tenant_id, o.id, plan_ids)
    await db.commit()
    return await _detail(db, admin.tenant_id, await _option(db, admin.tenant_id, o.id))


@router.get("/options/{option_id}", response_model=OptionDetail)
async def get_option(option_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await _detail(db, user.tenant_id, await _option(db, user.tenant_id, option_id))


@router.patch("/options/{option_id}", response_model=OptionDetail)
async def update_option(
    option_id: uuid.UUID, payload: OptionUpdate, db: DbSession, admin: RequireAdmin
):
    """Edit an option. A price change is logged; selections already made keep theirs."""
    o = await _option(db, admin.tenant_id, option_id)
    data = payload.model_dump(exclude_unset=True)
    reason = data.pop("price_reason", None)
    if data.get("code"):
        data["code"] = data["code"].strip().upper()
        await _require_unique_code(db, admin.tenant_id, data["code"], except_id=o.id)
    new_price = data.get("price")
    if new_price is not None and new_price != o.price:
        db.add(
            OptionPriceChange(
                tenant_id=admin.tenant_id, option_id=o.id, old_price=o.price,
                new_price=new_price, changed_by_id=admin.id, reason=reason,
            )
        )
    required = ("code", "name", "category", "price", "schedule_days", "is_structural",
                "max_quantity")
    for key, value in data.items():
        if key in required and value is None:
            continue
        setattr(o, key, value)
    await db.commit()
    return await _detail(db, admin.tenant_id, await _option(db, admin.tenant_id, o.id))


@router.post("/options/{option_id}/archive", response_model=OptionDetail)
async def archive_option(option_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    """Stop offering it. Selections already made on homes are unaffected."""
    o = await _option(db, admin.tenant_id, option_id)
    if not o.is_active:
        raise _conflict(f"{o.code} is already archived.")
    o.is_active = False
    await db.commit()
    return await _detail(db, admin.tenant_id, await _option(db, admin.tenant_id, o.id))


@router.post("/options/{option_id}/restore", response_model=OptionDetail)
async def restore_option(option_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    o = await _option(db, admin.tenant_id, option_id)
    if o.is_active:
        raise _conflict(f"{o.code} is not archived.")
    o.is_active = True
    await db.commit()
    return await _detail(db, admin.tenant_id, await _option(db, admin.tenant_id, o.id))


@router.post(
    "/options/{option_id}/copy", response_model=OptionDetail, status_code=status.HTTP_201_CREATED
)
async def copy_option(
    option_id: uuid.UUID, payload: OptionCopy, db: DbSession, admin: RequireAdmin
):
    src = await _option(db, admin.tenant_id, option_id)
    code = payload.code.strip().upper()
    await _require_unique_code(db, admin.tenant_id, code)
    fields = ("description", "category", "price", "schedule_days", "is_structural",
              "max_quantity", "unit", "supplier_ref")
    new = Option(
        tenant_id=admin.tenant_id, code=code, name=payload.name.strip(),
        **{f: getattr(src, f) for f in fields},
    )
    db.add(new)
    await db.flush()
    for cl in src.cost_lines:
        db.add(
            OptionCostLine(
                tenant_id=admin.tenant_id, option_id=new.id, cost_code_id=cl.cost_code_id,
                vendor_id=cl.vendor_id, amount=cl.amount, description=cl.description,
            )
        )
    plan_ids = list(
        await db.scalars(select(OptionPlan.plan_id).where(OptionPlan.option_id == src.id))
    )
    await _set_plans(db, admin.tenant_id, new.id, plan_ids)
    await db.commit()
    return await _detail(db, admin.tenant_id, await _option(db, admin.tenant_id, new.id))


@router.put("/options/{option_id}/plans", response_model=OptionDetail)
async def set_option_plans(
    option_id: uuid.UUID, payload: OptionPlansSet, db: DbSession, admin: RequireAdmin
):
    """The house types it is offered on. An empty list offers it on every plan."""
    o = await _option(db, admin.tenant_id, option_id)
    await _set_plans(db, admin.tenant_id, o.id, payload.plan_ids)
    await db.commit()
    return await _detail(db, admin.tenant_id, o)


@router.get("/options/{option_id}/price-history", response_model=list[OptionPriceChangeRead])
async def option_price_history(option_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _option(db, user.tenant_id, option_id)
    return list(
        await db.scalars(
            select(OptionPriceChange)
            .where(OptionPriceChange.option_id == option_id)
            .order_by(OptionPriceChange.created_at.desc())
        )
    )


@router.get("/options/{option_id}/selections", response_model=list[OptionSelectionRow])
async def option_selections(option_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Every home it has been chosen on, at the price and cost it was chosen at."""
    await _option(db, user.tenant_id, option_id)
    rows = list(
        await db.scalars(
            select(Selection)
            .where(Selection.option_id == option_id, Selection.tenant_id == user.tenant_id)
            .order_by(Selection.selected_date.desc())
        )
    )
    jobs = {
        j.id: j
        for j in await db.scalars(
            select(Job).where(Job.id.in_([r.job_id for r in rows] or [uuid.uuid4()]))
        )
    }
    return [
        OptionSelectionRow(
            selection_id=r.id, job_id=r.job_id,
            job_number=jobs[r.job_id].job_number if r.job_id in jobs else "—",
            status=r.status.value, quantity=r.quantity,
            price_at_selection=r.price_at_selection, cost_at_selection=r.cost_at_selection,
            selected_date=r.selected_date,
        )
        for r in rows
    ]


# --- cost lines ---------------------------------------------------------------------------


async def _line(db, tenant_id, line_id: uuid.UUID) -> OptionCostLine:
    ln = await db.scalar(
        select(OptionCostLine).where(
            OptionCostLine.id == line_id, OptionCostLine.tenant_id == tenant_id
        )
    )
    if ln is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost line not found")
    return ln


async def _require_vendor(db, tenant_id, vendor_id) -> None:
    if vendor_id is not None and not await db.scalar(
        select(Vendor.id).where(Vendor.id == vendor_id, Vendor.tenant_id == tenant_id)
    ):
        raise HTTPException(status_code=422, detail="That trade is not in this organisation.")


@router.post(
    "/options/{option_id}/cost-lines", response_model=OptionCostLineRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_option_cost_line(
    option_id: uuid.UUID, payload: OptionCostLineCreate, db: DbSession, admin: RequireAdmin
):
    await _option(db, admin.tenant_id, option_id)
    code = await db.scalar(
        select(CostCode).where(
            CostCode.id == payload.cost_code_id, CostCode.tenant_id == admin.tenant_id
        )
    )
    if code is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost code not found")
    if await db.scalar(
        select(OptionCostLine.id).where(
            OptionCostLine.option_id == option_id, OptionCostLine.cost_code_id == code.id
        )
    ):
        raise _conflict(f"{code.code} is already on this option. Edit that line instead.")
    await _require_vendor(db, admin.tenant_id, payload.vendor_id)
    line = OptionCostLine(tenant_id=admin.tenant_id, option_id=option_id, **payload.model_dump())
    db.add(line)
    await db.commit()
    await db.refresh(line)
    return line


@router.patch("/option-cost-lines/{line_id}", response_model=OptionCostLineRead)
async def update_option_cost_line(
    line_id: uuid.UUID, payload: OptionCostLineUpdate, db: DbSession, admin: RequireAdmin
):
    """Correct a cost. Selections already made keep the cost they were chosen at."""
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


@router.delete("/option-cost-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_option_cost_line(line_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    line = await _line(db, admin.tenant_id, line_id)
    await db.delete(line)
    await db.commit()
