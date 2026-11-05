"""Drawings, specifications and transmittals.

The gap this closes: a trade prices and builds against a specific revision of a set.
When the drawings change, whoever priced the old set has to be told, and the builder
has to be able to prove it. Issued sets and specs therefore **supersede** each other
rather than being overwritten — the old one is the evidence — and a **transmittal**
records who was sent which revision, for what purpose, and whether they confirmed it.

  POST /drawing-sets/{id}/revise     supersede an issued set with the next revision
  POST /specifications/{id}/revise   the same lifecycle for a written spec
  POST /transmittals                 provable distribution to named trades
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequireDesignTeam,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.cost_code import CostCode
from app.models.drawings import (
    Drawing,
    DrawingSet,
    SetStatus,
    Specification,
    SpecStatus,
    Transmittal,
    TransmittalRecipient,
)
from app.models.plan import Plan
from app.models.vendor import Vendor
from app.schemas.drawings import (
    DrawingCreate,
    DrawingSetCreate,
    DrawingSetRead,
    DrawingSetUpdate,
    DrawingsSummary,
    DrawingUpdate,
    SpecificationCreate,
    SpecificationRead,
    SpecificationRevise,
    SpecificationUpdate,
    TransmittalCreate,
    TransmittalRead,
)

router = APIRouter(tags=["drawings"])


# --- helpers ------------------------------------------------------------------


async def _next_number(db, model, tenant_id: uuid.UUID, prefix: str) -> str:
    count = await db.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )
    return f"{prefix}{(count or 0) + 1:05d}"


async def _load_set(db, tenant_id: uuid.UUID, set_id: uuid.UUID) -> DrawingSet:
    obj = await db.scalar(
        select(DrawingSet)
        .options(selectinload(DrawingSet.drawings))
        .where(DrawingSet.id == set_id, DrawingSet.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Drawing set not found")
    return obj


async def _load_transmittal(db, tenant_id: uuid.UUID, t_id: uuid.UUID) -> Transmittal:
    obj = await db.scalar(
        select(Transmittal)
        .options(selectinload(Transmittal.recipients))
        .where(Transmittal.id == t_id, Transmittal.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transmittal not found")
    return obj


# --- analytics ----------------------------------------------------------------


@router.get("/drawings/summary", response_model=DrawingsSummary)
async def summary(db: DbSession, user: RequireStaff):
    """The board across all three tabs: what is live, what is kept as evidence, how many
    sheets by discipline, the spec count, and the transmittals still to be confirmed."""
    tenant = user.tenant_id
    sets = list(
        await db.scalars(
            select(DrawingSet)
            .options(selectinload(DrawingSet.drawings))
            .where(DrawingSet.tenant_id == tenant)
        )
    )
    draft_sets = live_sets = superseded_sets = total_sheets = 0
    by_discipline: dict[str, int] = {}
    for s in sets:
        if s.status is SetStatus.DRAFT:
            draft_sets += 1
        elif s.status is SetStatus.ISSUED:
            live_sets += 1
            total_sheets += s.sheet_count
            for disc, n in s.disciplines.items():
                by_discipline[disc] = by_discipline.get(disc, 0) + n
        elif s.status is SetStatus.SUPERSEDED:
            superseded_sets += 1

    specs = list(await db.scalars(select(Specification).where(Specification.tenant_id == tenant)))
    current_specs = sum(1 for sp in specs if sp.status is SpecStatus.ISSUED)
    draft_specs = sum(1 for sp in specs if sp.status is SpecStatus.DRAFT)

    transmittals = list(
        await db.scalars(
            select(Transmittal)
            .options(selectinload(Transmittal.recipients))
            .where(Transmittal.tenant_id == tenant)
        )
    )
    unacknowledged = sum(t.outstanding for t in transmittals)
    overdue = sum(1 for t in transmittals if t.is_overdue)
    by_purpose: dict[str, int] = {}
    for t in transmittals:
        by_purpose[t.purpose.value] = by_purpose.get(t.purpose.value, 0) + 1

    return DrawingsSummary(
        draft_sets=draft_sets,
        live_sets=live_sets,
        superseded_sets=superseded_sets,
        total_sheets=total_sheets,
        specifications=current_specs,
        draft_specs=draft_specs,
        transmittals=len(transmittals),
        unacknowledged=unacknowledged,
        overdue_transmittals=overdue,
        by_discipline=by_discipline,
        by_purpose=by_purpose,
    )


# --- drawing sets -------------------------------------------------------------


@router.get("/drawing-sets", response_model=list[DrawingSetRead])
async def list_drawing_sets(
    db: DbSession,
    user: RequireDesignTeam,
    plan_id: uuid.UUID | None = None,
    community_id: uuid.UUID | None = None,
    status_filter: SetStatus | None = Query(default=None, alias="status"),
):
    stmt = (
        select(DrawingSet)
        .options(selectinload(DrawingSet.drawings))
        .where(DrawingSet.tenant_id == user.tenant_id)
    )
    if plan_id is not None:
        stmt = stmt.where(DrawingSet.plan_id == plan_id)
    if community_id is not None:
        stmt = stmt.where(DrawingSet.community_id == community_id)
    if status_filter is not None:
        stmt = stmt.where(DrawingSet.status == status_filter)
    return list(await db.scalars(stmt.order_by(DrawingSet.set_number)))


@router.post("/drawing-sets", response_model=DrawingSetRead, status_code=status.HTTP_201_CREATED)
async def create_drawing_set(payload: DrawingSetCreate, db: DbSession, user: RequireStaff):
    if payload.plan_id:
        await get_tenant_scoped(db, Plan, user.tenant_id, payload.plan_id)
    obj = DrawingSet(
        tenant_id=user.tenant_id,
        set_number=await _next_number(db, DrawingSet, user.tenant_id, "DS"),
        **payload.model_dump(),
    )
    db.add(obj)
    await db.commit()
    return await _load_set(db, user.tenant_id, obj.id)


@router.patch("/drawing-sets/{set_id}", response_model=DrawingSetRead)
async def update_drawing_set(
    set_id: uuid.UUID, payload: DrawingSetUpdate, db: DbSession, user: RequireStaff
):
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is not SetStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a draft set can be edited; an issued set is superseded, never changed.",
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


@router.post(
    "/drawing-sets/{set_id}/drawings",
    response_model=DrawingSetRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_drawing(set_id: uuid.UUID, payload: DrawingCreate, db: DbSession, user: RequireStaff):
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is not SetStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sheets can only be added while the set is a draft.",
        )
    db.add(Drawing(tenant_id=user.tenant_id, drawing_set_id=set_id, **payload.model_dump()))
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


@router.patch("/drawing-sets/{set_id}/drawings/{drawing_id}", response_model=DrawingSetRead)
async def update_drawing(
    set_id: uuid.UUID,
    drawing_id: uuid.UUID,
    payload: DrawingUpdate,
    db: DbSession,
    user: RequireStaff,
):
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is not SetStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sheets can only be edited while the set is a draft.",
        )
    sheet = next((d for d in obj.drawings if d.id == drawing_id), None)
    if sheet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sheet not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(sheet, field, value)
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


@router.delete("/drawing-sets/{set_id}/drawings/{drawing_id}", response_model=DrawingSetRead)
async def remove_drawing(
    set_id: uuid.UUID, drawing_id: uuid.UUID, db: DbSession, user: RequireStaff
):
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is not SetStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sheets can only be removed while the set is a draft.",
        )
    sheet = next((d for d in obj.drawings if d.id == drawing_id), None)
    if sheet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sheet not found")
    await db.delete(sheet)
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


@router.post("/drawing-sets/{set_id}/issue", response_model=DrawingSetRead)
async def issue_drawing_set(set_id: uuid.UUID, db: DbSession, user: RequireStaff):
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is not SetStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"This set is already {obj.status.value}."
        )
    if not obj.drawings:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A set with no sheets cannot be issued.",
        )
    obj.status = SetStatus.ISSUED
    obj.issued_on = date.today()
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


@router.post("/drawing-sets/{set_id}/revise", response_model=DrawingSetRead)
async def revise_drawing_set(set_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Supersede an issued set with the next revision.

    The old set is never edited — it is the evidence of what trades were pricing and
    building from at the time. Its sheets are carried forward into the new draft.
    """
    old = await _load_set(db, user.tenant_id, set_id)
    if old.status is not SetStatus.ISSUED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only an issued set can be revised; this one is {old.status.value}.",
        )

    new = DrawingSet(
        tenant_id=user.tenant_id,
        set_number=await _next_number(db, DrawingSet, user.tenant_id, "DS"),
        name=old.name,
        revision=old.revision + 1,
        plan_id=old.plan_id,
        community_id=old.community_id,
        notes=old.notes,
    )
    db.add(new)
    await db.flush()

    for d in old.drawings:
        db.add(
            Drawing(
                tenant_id=user.tenant_id,
                drawing_set_id=new.id,
                sheet_number=d.sheet_number,
                title=d.title,
                discipline=d.discipline,
                notes=d.notes,
            )
        )

    old.status = SetStatus.SUPERSEDED
    old.superseded_by_id = new.id
    await db.commit()
    return await _load_set(db, user.tenant_id, new.id)


@router.post("/drawing-sets/{set_id}/void", response_model=DrawingSetRead)
async def void_drawing_set(set_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Withdraw a set that should not stand — a draft abandoned, or an issue pulled
    before anyone built from it. Kept, not deleted, so the numbering stays honest."""
    obj = await _load_set(db, user.tenant_id, set_id)
    if obj.status is SetStatus.SUPERSEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A superseded set is already part of the record; it cannot be voided.",
        )
    obj.status = SetStatus.VOID
    await db.commit()
    return await _load_set(db, user.tenant_id, set_id)


# --- specifications -----------------------------------------------------------


async def _load_spec(db, tenant_id: uuid.UUID, spec_id: uuid.UUID) -> Specification:
    obj = await get_tenant_scoped(db, Specification, tenant_id, spec_id)
    return obj


@router.get("/specifications", response_model=list[SpecificationRead])
async def list_specifications(
    db: DbSession,
    user: RequireDesignTeam,
    plan_id: uuid.UUID | None = None,
    current_only: bool = False,
):
    stmt = select(Specification).where(Specification.tenant_id == user.tenant_id)
    if plan_id is not None:
        stmt = stmt.where(Specification.plan_id == plan_id)
    if current_only:
        stmt = stmt.where(Specification.is_current.is_(True))
    return list(await db.scalars(stmt.order_by(Specification.section)))


@router.post(
    "/specifications", response_model=SpecificationRead, status_code=status.HTTP_201_CREATED
)
async def create_specification(payload: SpecificationCreate, db: DbSession, user: RequireStaff):
    if payload.cost_code_id:
        await get_tenant_scoped(db, CostCode, user.tenant_id, payload.cost_code_id)
    data = payload.model_dump(exclude={"issue_now"})
    obj = Specification(
        tenant_id=user.tenant_id,
        status=SpecStatus.ISSUED if payload.issue_now else SpecStatus.DRAFT,
        **data,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.get("/specifications/{spec_id}", response_model=SpecificationRead)
async def get_specification(spec_id: uuid.UUID, db: DbSession, user: RequireDesignTeam):
    return await _load_spec(db, user.tenant_id, spec_id)


@router.patch("/specifications/{spec_id}", response_model=SpecificationRead)
async def update_specification(
    spec_id: uuid.UUID, payload: SpecificationUpdate, db: DbSession, user: RequireStaff
):
    obj = await _load_spec(db, user.tenant_id, spec_id)
    if obj.status is SpecStatus.SUPERSEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A superseded spec is evidence; revise the current one instead of editing it.",
        )
    if payload.cost_code_id:
        await get_tenant_scoped(db, CostCode, user.tenant_id, payload.cost_code_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/specifications/{spec_id}/issue", response_model=SpecificationRead)
async def issue_specification(spec_id: uuid.UUID, db: DbSession, user: RequireStaff):
    obj = await _load_spec(db, user.tenant_id, spec_id)
    if obj.status is not SpecStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a draft spec can be issued; this one is {obj.status.value}.",
        )
    obj.status = SpecStatus.ISSUED
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/specifications/{spec_id}/revise", response_model=SpecificationRead)
async def revise_specification(
    spec_id: uuid.UUID, payload: SpecificationRevise, db: DbSession, user: RequireStaff
):
    """Supersede an issued spec with the next revision. The old one is kept as the record
    of what trades priced against; anything not changed is carried forward."""
    old = await _load_spec(db, user.tenant_id, spec_id)
    if old.status is not SpecStatus.ISSUED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only an issued spec can be revised; this one is {old.status.value}.",
        )
    if payload.cost_code_id:
        await get_tenant_scoped(db, CostCode, user.tenant_id, payload.cost_code_id)

    new = Specification(
        tenant_id=user.tenant_id,
        section=old.section,
        title=payload.title or old.title,
        body=payload.body if payload.body is not None else old.body,
        revision=old.revision + 1,
        status=SpecStatus.ISSUED,
        plan_id=old.plan_id,
        cost_code_id=payload.cost_code_id or old.cost_code_id,
        storage_key=payload.storage_key or old.storage_key,
    )
    db.add(new)
    await db.flush()

    old.status = SpecStatus.SUPERSEDED
    old.is_current = False
    old.superseded_by_id = new.id
    await db.commit()
    await db.refresh(new)
    return new


# --- transmittals -------------------------------------------------------------


@router.get("/transmittals", response_model=list[TransmittalRead])
async def list_transmittals(
    db: DbSession,
    user: RequireStaff,
    overdue_only: bool = False,
):
    rows = list(
        await db.scalars(
            select(Transmittal)
            .options(selectinload(Transmittal.recipients))
            .where(Transmittal.tenant_id == user.tenant_id)
            .order_by(Transmittal.transmittal_number)
        )
    )
    if overdue_only:
        rows = [t for t in rows if t.is_overdue]
    return rows


@router.post("/transmittals", response_model=TransmittalRead, status_code=status.HTTP_201_CREATED)
async def send_transmittal(payload: TransmittalCreate, db: DbSession, user: RequireStaff):
    """Issue documents to named trades, and record who was sent what, for what purpose."""
    data = payload.model_dump()
    vendor_ids = data.pop("vendor_ids")

    obj = Transmittal(
        tenant_id=user.tenant_id,
        transmittal_number=await _next_number(db, Transmittal, user.tenant_id, "TR"),
        sent_by_id=user.id,
        sent_on=date.today(),
        **data,
    )
    db.add(obj)
    await db.flush()

    for vid in vendor_ids:
        await get_tenant_scoped(db, Vendor, user.tenant_id, vid)
        db.add(TransmittalRecipient(tenant_id=user.tenant_id, transmittal_id=obj.id, vendor_id=vid))

    await db.commit()
    return await _load_transmittal(db, user.tenant_id, obj.id)


@router.post("/transmittals/{t_id}/acknowledge/{vendor_id}", response_model=TransmittalRead)
async def acknowledge_transmittal(
    t_id: uuid.UUID, vendor_id: uuid.UUID, db: DbSession, user: RequireStaff
):
    """A trade confirms receipt. This is the proof the distribution happened."""
    obj = await _load_transmittal(db, user.tenant_id, t_id)
    recipient = next((r for r in obj.recipients if r.vendor_id == vendor_id), None)
    if recipient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That trade is not on this transmittal."
        )
    recipient.acknowledged_at = datetime.now(UTC)
    await db.commit()
    return await _load_transmittal(db, user.tenant_id, t_id)
