"""Warranty and homeowner care.

The claim lifecycle in one place: log -> acknowledge (SLA clock) -> assess (is it in
cover?) -> dispatch trades -> visit -> resolve -> close. The two reports that justify the
module - overdue against SLA, and callbacks by trade - are computed here from the same
records, so they can never disagree with the queue the staff actually work.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, select

from app.api.deps import (
    DbSession,
    RequireStaff,
    RequireWarranty,
    get_tenant_scoped,
)
from app.models.contract import Contract
from app.models.cost_code import CostCode
from app.models.job import Job
from app.models.user import User
from app.models.vendor import Vendor
from app.models.warranty import (
    CLOSED_STATUSES,
    AppointmentKind,
    AppointmentStatus,
    CoverageStatus,
    Priority,
    ServiceOrder,
    ServiceOrderStatus,
    WarrantyAppointment,
    WarrantyCategory,
    WarrantyNote,
    WarrantyRequest,
    WarrantyStatus,
)
from app.schemas.warranty import (
    AppointmentCreate,
    AppointmentRead,
    AppointmentUpdate,
    AssessPayload,
    CoverageRead,
    NoteCreate,
    NoteRead,
    ServiceOrderCreate,
    ServiceOrderRead,
    TradeCallback,
    WarrantyRequestCreate,
    WarrantyRequestRead,
    WarrantyRequestUpdate,
    WarrantySummary,
)
from app.services import warranty as warranty_service

router = APIRouter(prefix="/warranty", tags=["warranty"])


# --- helpers ------------------------------------------------------------------


async def _next_number(db, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count())
        .select_from(WarrantyRequest)
        .where(WarrantyRequest.tenant_id == tenant_id)
    )
    return f"WR{(count or 0) + 1:05d}"


async def _load_request(db, tenant_id: uuid.UUID, req_id: uuid.UUID) -> WarrantyRequest:
    req = await db.scalar(
        select(WarrantyRequest).where(
            WarrantyRequest.id == req_id, WarrantyRequest.tenant_id == tenant_id
        )
    )
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Warranty request not found"
        )
    return req


async def _load_order(db, tenant_id: uuid.UUID, order_id: uuid.UUID) -> ServiceOrder:
    order = await db.scalar(
        select(ServiceOrder).where(ServiceOrder.id == order_id, ServiceOrder.tenant_id == tenant_id)
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service order not found")
    return order


# --- dashboard ----------------------------------------------------------------


@router.get("/summary", response_model=WarrantySummary)
async def summary(
    db: DbSession,
    user: RequireStaff,
    days: int = Query(default=365, ge=1, le=3650, description="Callback look-back window."),
):
    """Everything the warranty manager reads first thing: the queue, the SLA breaches,
    the money owed by trades, and who keeps coming back."""
    tenant = user.tenant_id
    today = date.today()

    requests = list(
        await db.scalars(select(WarrantyRequest).where(WarrantyRequest.tenant_id == tenant))
    )

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_coverage: dict[str, int] = {}
    open_count = overdue = emergency_open = unassessed = resolved_last_30 = 0
    resolution_spans: list[int] = []
    cutoff_30 = today - timedelta(days=30)

    for r in requests:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        by_category[r.category.value] = by_category.get(r.category.value, 0) + 1
        by_coverage[r.coverage.value] = by_coverage.get(r.coverage.value, 0) + 1

        is_open = r.status not in CLOSED_STATUSES
        if is_open:
            open_count += 1
            if r.due_date and r.due_date < today:
                overdue += 1
            if r.priority is Priority.EMERGENCY:
                emergency_open += 1
            if r.coverage is CoverageStatus.PENDING:
                unassessed += 1
        if r.resolved_date and r.resolved_date >= cutoff_30:
            resolved_last_30 += 1
        if r.resolved_date and r.submitted_date:
            resolution_spans.append((r.resolved_date - r.submitted_date).days)

    avg_resolution = (
        round(sum(resolution_spans) / len(resolution_spans), 1) if resolution_spans else None
    )

    # Open service orders and back-charge money outstanding.
    open_so = await db.scalar(
        select(func.count())
        .select_from(ServiceOrder)
        .where(
            ServiceOrder.tenant_id == tenant,
            ServiceOrder.status.in_((ServiceOrderStatus.ASSIGNED, ServiceOrderStatus.SCHEDULED)),
        )
    )
    back_charge_total = await db.scalar(
        select(func.coalesce(func.sum(ServiceOrder.cost_amount), 0)).where(
            ServiceOrder.tenant_id == tenant,
            ServiceOrder.back_charge.is_(True),
        )
    )
    upcoming = await db.scalar(
        select(func.count())
        .select_from(WarrantyAppointment)
        .where(
            WarrantyAppointment.tenant_id == tenant,
            WarrantyAppointment.status == AppointmentStatus.SCHEDULED,
            WarrantyAppointment.scheduled_for >= today,
        )
    )

    callbacks = await _callbacks_by_trade(db, tenant, days)

    return WarrantySummary(
        total=len(requests),
        open=open_count,
        overdue=overdue,
        emergency_open=emergency_open,
        unassessed=unassessed,
        resolved_last_30=resolved_last_30,
        avg_resolution_days=avg_resolution,
        service_orders_open=open_so or 0,
        back_charge_total=back_charge_total or 0,
        appointments_upcoming=upcoming or 0,
        by_status=by_status,
        by_category=by_category,
        by_coverage=by_coverage,
        callbacks_by_trade=callbacks,
    )


async def _callbacks_by_trade(db, tenant: uuid.UUID, days: int) -> list[TradeCallback]:
    since = date.today() - timedelta(days=days)
    rows = await db.execute(
        select(
            Vendor.id,
            Vendor.code,
            Vendor.name,
            func.count(ServiceOrder.id),
            func.sum(case((ServiceOrder.back_charge.is_(True), 1), else_=0)),
        )
        .join(ServiceOrder, ServiceOrder.vendor_id == Vendor.id)
        .join(WarrantyRequest, WarrantyRequest.id == ServiceOrder.request_id)
        .where(
            ServiceOrder.tenant_id == tenant,
            WarrantyRequest.submitted_date >= since,
        )
        .group_by(Vendor.id, Vendor.code, Vendor.name)
        .order_by(func.count(ServiceOrder.id).desc())
    )
    parsed = [
        {
            "vendor_id": vid,
            "vendor_code": code,
            "vendor_name": name,
            "orders": k,
            "bc": int(bc or 0),
        }
        for vid, code, name, k, bc in rows
    ]
    total = sum(p["orders"] for p in parsed)
    return [
        TradeCallback(
            vendor_id=p["vendor_id"],
            vendor_code=p["vendor_code"],
            vendor_name=p["vendor_name"],
            service_orders=p["orders"],
            back_charges=p["bc"],
            share=round(p["orders"] / total, 3) if total else 0.0,
        )
        for p in parsed
    ]


@router.get("/coverage", response_model=CoverageRead)
async def coverage(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """The warranty windows for a home, measured from its completion date."""
    job = await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    windows = warranty_service.coverage_windows(job.closing_date)
    today = date.today()
    return CoverageRead(
        job_id=job.id,
        closing_date=windows["closing_date"],
        defects_until=windows["defects_until"],
        structural_until=windows["structural_until"],
        defects_active=bool(windows["defects_until"] and windows["defects_until"] >= today),
        structural_active=bool(
            windows["structural_until"] and windows["structural_until"] >= today
        ),
    )


# --- requests -----------------------------------------------------------------


@router.get("/requests", response_model=list[WarrantyRequestRead])
async def list_requests(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    status_filter: WarrantyStatus | None = Query(default=None, alias="status"),
    priority: Priority | None = None,
    category: WarrantyCategory | None = None,
    coverage_filter: CoverageStatus | None = Query(default=None, alias="coverage"),
    overdue: bool | None = None,
):
    stmt = select(WarrantyRequest).where(WarrantyRequest.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(WarrantyRequest.job_id == job_id)
    if status_filter is not None:
        stmt = stmt.where(WarrantyRequest.status == status_filter)
    if priority is not None:
        stmt = stmt.where(WarrantyRequest.priority == priority)
    if category is not None:
        stmt = stmt.where(WarrantyRequest.category == category)
    if coverage_filter is not None:
        stmt = stmt.where(WarrantyRequest.coverage == coverage_filter)
    if overdue:
        stmt = stmt.where(
            WarrantyRequest.status.notin_(CLOSED_STATUSES),
            WarrantyRequest.due_date.is_not(None),
            WarrantyRequest.due_date < date.today(),
        )

    rows = await db.scalars(stmt.order_by(WarrantyRequest.created_at.desc()))
    return list(rows)


@router.post("/requests", response_model=WarrantyRequestRead, status_code=status.HTTP_201_CREATED)
async def create_request(payload: WarrantyRequestCreate, db: DbSession, user: RequireStaff):
    """Raise a warranty claim on a home. The buyer on the contract is attached
    automatically, and the SLA response clock starts from today by priority."""
    job = await get_tenant_scoped(db, Job, user.tenant_id, payload.job_id)
    contract = await db.scalar(select(Contract).where(Contract.job_id == job.id))

    today = date.today()
    req = WarrantyRequest(
        tenant_id=user.tenant_id,
        request_number=await _next_number(db, user.tenant_id),
        job_id=job.id,
        buyer_id=contract.buyer_id if contract else None,
        title=payload.title,
        description=payload.description,
        location=payload.location,
        priority=payload.priority,
        category=payload.category,
        reported_via=payload.reported_via,
        submitted_date=today,
        due_date=warranty_service.due_date_for(today, payload.priority),
    )
    db.add(req)
    await db.commit()
    return await _load_request(db, user.tenant_id, req.id)


@router.get("/requests/{request_id}", response_model=WarrantyRequestRead)
async def get_request(request_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await _load_request(db, user.tenant_id, request_id)


@router.patch("/requests/{request_id}", response_model=WarrantyRequestRead)
async def update_request(
    request_id: uuid.UUID,
    payload: WarrantyRequestUpdate,
    db: DbSession,
    user: RequireWarranty,
):
    """Edit the claim itself. Changing priority re-sets the SLA target from the original
    submission date, so a re-triage to Emergency is held to the emergency promise."""
    req = await _load_request(db, user.tenant_id, request_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(req, field, value)
    if "priority" in data and req.submitted_date:
        req.due_date = warranty_service.due_date_for(req.submitted_date, req.priority)
    await db.commit()
    return await _load_request(db, user.tenant_id, req.id)


@router.post("/requests/{request_id}/acknowledge", response_model=WarrantyRequestRead)
async def acknowledge_request(request_id: uuid.UUID, db: DbSession, user: RequireWarranty):
    """Customer care has responded. Records the date the SLA is judged against."""
    req = await _load_request(db, user.tenant_id, request_id)
    if req.status in CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This claim is already closed.",
        )
    req.acknowledged_date = req.acknowledged_date or date.today()
    if req.status is WarrantyStatus.SUBMITTED:
        req.status = WarrantyStatus.ACKNOWLEDGED
    await db.commit()
    return await _load_request(db, user.tenant_id, req.id)


@router.post("/requests/{request_id}/assess", response_model=WarrantyRequestRead)
async def assess_request(
    request_id: uuid.UUID, payload: AssessPayload, db: DbSession, user: RequireWarranty
):
    """Record the coverage decision. A decline reason closes the claim as declined;
    otherwise the claim is triaged and ready for a trade to be dispatched."""
    req = await _load_request(db, user.tenant_id, request_id)
    if req.status in CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This claim is already closed.",
        )

    req.coverage = payload.coverage
    req.assessment_notes = payload.assessment_notes

    if payload.decline_reason:
        req.status = WarrantyStatus.DECLINED
        req.decline_reason = payload.decline_reason
        req.resolved_date = req.resolved_date or date.today()
    elif req.status in (WarrantyStatus.SUBMITTED, WarrantyStatus.ACKNOWLEDGED):
        req.status = WarrantyStatus.TRIAGED

    await db.commit()
    return await _load_request(db, user.tenant_id, req.id)


@router.post("/requests/{request_id}/close", response_model=WarrantyRequestRead)
async def close_request(request_id: uuid.UUID, db: DbSession, user: RequireWarranty):
    """Sign the claim off once the homeowner is satisfied."""
    req = await _load_request(db, user.tenant_id, request_id)
    if req.status is WarrantyStatus.DECLINED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A declined claim cannot be closed; it is already terminal.",
        )
    req.status = WarrantyStatus.CLOSED
    req.closed_date = date.today()
    if req.resolved_date is None:
        req.resolved_date = date.today()
    await db.commit()
    return await _load_request(db, user.tenant_id, req.id)


# --- service orders -----------------------------------------------------------


@router.post(
    "/requests/{request_id}/service-orders",
    response_model=ServiceOrderRead,
    status_code=status.HTTP_201_CREATED,
)
async def dispatch_service_order(
    request_id: uuid.UUID, payload: ServiceOrderCreate, db: DbSession, user: RequireStaff
):
    """Dispatch work to a trade. Attaching the vendor is what makes callback reporting work."""
    req = await _load_request(db, user.tenant_id, request_id)
    if req.status in (WarrantyStatus.RESOLVED, WarrantyStatus.CLOSED, WarrantyStatus.DECLINED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot dispatch work on a resolved, closed or declined claim.",
        )
    if payload.vendor_id:
        await get_tenant_scoped(db, Vendor, user.tenant_id, payload.vendor_id)
    if payload.cost_code_id:
        await get_tenant_scoped(db, CostCode, user.tenant_id, payload.cost_code_id)

    order = ServiceOrder(tenant_id=user.tenant_id, request_id=req.id, **payload.model_dump())
    db.add(order)

    if req.status in (WarrantyStatus.SUBMITTED, WarrantyStatus.ACKNOWLEDGED):
        req.status = WarrantyStatus.TRIAGED

    await db.commit()
    await db.refresh(order)
    return order


@router.post("/service-orders/{order_id}/complete", response_model=ServiceOrderRead)
async def complete_service_order(
    order_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    resolution: str | None = Query(default=None),
    cost_amount: float | None = Query(default=None, ge=0),
    back_charge: bool = Query(default=False),
):
    """Complete the work, optionally recording the cost and whether it is charged back to
    the trade. When every order on a request is done, the request resolves; while some
    remain, the request sits in progress."""
    order = await _load_order(db, user.tenant_id, order_id)
    order.status = ServiceOrderStatus.COMPLETE
    order.completed_date = date.today()
    order.resolution = resolution
    if cost_amount is not None:
        order.cost_amount = cost_amount
    order.back_charge = back_charge

    req = await _load_request(db, user.tenant_id, order.request_id)
    remaining = [
        o
        for o in req.service_orders
        if o.status not in (ServiceOrderStatus.COMPLETE, ServiceOrderStatus.CANCELLED)
    ]
    if not remaining:
        req.status = WarrantyStatus.RESOLVED
        req.resolved_date = date.today()
    elif req.status not in CLOSED_STATUSES:
        req.status = WarrantyStatus.IN_PROGRESS

    await db.commit()
    await db.refresh(order)
    return order


@router.post("/service-orders/{order_id}/cancel", response_model=ServiceOrderRead)
async def cancel_service_order(order_id: uuid.UUID, db: DbSession, user: RequireWarranty):
    order = await _load_order(db, user.tenant_id, order_id)
    if order.status is ServiceOrderStatus.COMPLETE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A completed service order cannot be cancelled.",
        )
    order.status = ServiceOrderStatus.CANCELLED
    await db.commit()
    await db.refresh(order)
    return order


# --- appointments -------------------------------------------------------------


@router.post(
    "/requests/{request_id}/appointments",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def book_appointment(
    request_id: uuid.UUID, payload: AppointmentCreate, db: DbSession, user: RequireWarranty
):
    """Book a visit to the home. A service visit on a triaged claim moves it in progress."""
    req = await _load_request(db, user.tenant_id, request_id)
    if payload.vendor_id:
        await get_tenant_scoped(db, Vendor, user.tenant_id, payload.vendor_id)
    if payload.assignee_id:
        await get_tenant_scoped(db, User, user.tenant_id, payload.assignee_id)

    appt = WarrantyAppointment(tenant_id=user.tenant_id, request_id=req.id, **payload.model_dump())
    db.add(appt)

    if payload.kind is AppointmentKind.SERVICE_VISIT and req.status in (
        WarrantyStatus.ACKNOWLEDGED,
        WarrantyStatus.TRIAGED,
    ):
        req.status = WarrantyStatus.IN_PROGRESS

    await db.commit()
    await db.refresh(appt)
    return appt


@router.patch("/appointments/{appointment_id}", response_model=AppointmentRead)
async def update_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentUpdate,
    db: DbSession,
    user: RequireWarranty,
):
    appt = await get_tenant_scoped(db, WarrantyAppointment, user.tenant_id, appointment_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(appt, field, value)
    await db.commit()
    await db.refresh(appt)
    return appt


# --- notes / activity log -----------------------------------------------------


@router.get("/requests/{request_id}/notes", response_model=list[NoteRead])
async def list_notes(request_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await _load_request(db, user.tenant_id, request_id)
    rows = await db.scalars(
        select(WarrantyNote)
        .where(WarrantyNote.request_id == request_id, WarrantyNote.tenant_id == user.tenant_id)
        .order_by(WarrantyNote.created_at.desc())
    )
    return list(rows)


@router.post(
    "/requests/{request_id}/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_note(request_id: uuid.UUID, payload: NoteCreate, db: DbSession, user: RequireStaff):
    """Add to the claim's activity log. Homeowner-visible notes surface in the portal."""
    req = await _load_request(db, user.tenant_id, request_id)
    note = WarrantyNote(
        tenant_id=user.tenant_id,
        request_id=req.id,
        author_id=user.id,
        body=payload.body,
        visibility=payload.visibility,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note
