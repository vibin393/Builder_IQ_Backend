"""Change orders, scheduling, invoices, warranty and documents.

Grouped in one module because they share the same shape: a job-scoped record whose
approval or completion moves money or dates somewhere else.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequireAdmin,
    RequirePurchasing,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.change_order import ChangeOrder, ChangeOrderLine, ChangeOrderStatus
from app.models.cost_code import CostCode
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceLine, InvoiceStatus
from app.models.job import Job
from app.models.purchase_order import PurchaseOrder, PurchaseOrderStatus
from app.models.vendor import CIS_RATES, Vendor
from app.schemas.operations import (
    ChangeOrderCascade,
    ChangeOrderCreate,
    ChangeOrderRead,
    InvoiceCreate,
    InvoiceDispute,
    InvoiceRead,
    InvoiceSummary,
)
from app.services import change_order as co_service

router = APIRouter()


async def _next_number(db, model, tenant_id: uuid.UUID, column, prefix: str) -> str:
    count = await db.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )
    return f"{prefix}{(count or 0) + 1:05d}"


# ==============================================================================
# Change orders
# ==============================================================================

co_router = APIRouter(prefix="/jobs/{job_id}/change-orders", tags=["change orders"])


async def _load_co(db, tenant_id: uuid.UUID, co_id: uuid.UUID) -> ChangeOrder:
    co = await db.scalar(
        select(ChangeOrder)
        .options(selectinload(ChangeOrder.cost_lines))
        .where(ChangeOrder.id == co_id, ChangeOrder.tenant_id == tenant_id)
    )
    if co is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change order not found")
    return co


@co_router.get("", response_model=list[ChangeOrderRead])
async def list_change_orders(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)
    rows = await db.scalars(
        select(ChangeOrder)
        .options(selectinload(ChangeOrder.cost_lines))
        .where(ChangeOrder.job_id == job_id, ChangeOrder.tenant_id == user.tenant_id)
        .order_by(ChangeOrder.co_number)
    )
    return list(rows)


@co_router.post("", response_model=ChangeOrderRead, status_code=status.HTTP_201_CREATED)
async def create_change_order(
    job_id: uuid.UUID, payload: ChangeOrderCreate, db: DbSession, user: RequireStaff
):
    """Raise a change order. Nothing moves until it is approved."""
    await get_tenant_scoped(db, Job, user.tenant_id, job_id)

    for line in payload.cost_lines:
        await get_tenant_scoped(db, CostCode, user.tenant_id, line.cost_code_id)

    co = ChangeOrder(
        tenant_id=user.tenant_id,
        co_number=await _next_number(db, ChangeOrder, user.tenant_id, None, "CO"),
        job_id=job_id,
        description=payload.description,
        reason=payload.reason,
        responsible_party=payload.responsible_party,
        price_delta=payload.price_delta,
        schedule_days=payload.schedule_days,
        approval_deadline=payload.approval_deadline,
        client_notes=payload.client_notes,
        internal_notes=payload.internal_notes,
        status=ChangeOrderStatus.PENDING_APPROVAL,
        requested_by_id=user.id,
    )
    db.add(co)
    await db.flush()

    for line in payload.cost_lines:
        db.add(
            ChangeOrderLine(
                tenant_id=user.tenant_id,
                change_order_id=co.id,
                cost_code_id=line.cost_code_id,
                vendor_id=line.vendor_id,
                amount=line.amount,
                description=line.description,
            )
        )

    await db.commit()
    return await _load_co(db, user.tenant_id, co.id)


@co_router.post("/{co_id}/approve", response_model=ChangeOrderCascade)
async def approve_change_order(
    job_id: uuid.UUID, co_id: uuid.UUID, db: DbSession, admin: RequireAdmin
):
    """Approve and cascade. Who pays is decided by the change order's responsible_party."""
    await get_tenant_scoped(db, Job, admin.tenant_id, job_id)
    co = await _load_co(db, admin.tenant_id, co_id)
    if co.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Change order is not on this job"
        )

    result = await co_service.approve(db, admin.tenant_id, co, admin.id)
    await db.commit()
    return result


@co_router.post("/{co_id}/reject", response_model=ChangeOrderRead)
async def reject_change_order(
    job_id: uuid.UUID, co_id: uuid.UUID, db: DbSession, admin: RequireAdmin
):
    await get_tenant_scoped(db, Job, admin.tenant_id, job_id)
    co = await _load_co(db, admin.tenant_id, co_id)
    await co_service.reject(db, co, admin.id)
    await db.commit()
    return await _load_co(db, admin.tenant_id, co_id)


# ==============================================================================
# Scheduling
# ==============================================================================

sched_router = APIRouter(tags=["scheduling"])


# Schedule routes moved to `app.api.v1.schedule` when durations became working
# days and the job gained a forecast distinct from its baseline. What was here
# could generate a schedule and complete a task - so nothing could be started,
# blocked or moved, and a slip quietly rewrote the date the build was being
# judged against.


# ==============================================================================
# Invoices - actual cost
# ==============================================================================

inv_router = APIRouter(prefix="/invoices", tags=["invoices"])


async def _load_invoice(db, tenant_id: uuid.UUID, inv_id: uuid.UUID) -> Invoice:
    inv = await db.scalar(
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(Invoice.id == inv_id, Invoice.tenant_id == tenant_id)
    )
    if inv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return inv


#: Payment terms applied when an invoice does not state a due date.
PAYMENT_TERMS_DAYS = 30


@inv_router.get("/summary", response_model=InvoiceSummary)
async def invoice_summary(db: DbSession, user: RequireStaff):
    """The payables board: what is actual cost, what is waiting, what is late to pay,
    what cannot be paid for want of a waiver, and what is owed to HMRC under CIS."""
    rows = list(
        await db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.tenant_id == user.tenant_id)
        )
    )
    zero = Decimal("0")
    month_start = date.today().replace(day=1)
    by_status: dict[str, int] = {}
    actual = awaiting_value = unpaid_value = overdue_value = retention = cis_month = zero
    awaiting = disputed = unpaid = overdue = waivers = unverified = 0
    for i in rows:
        by_status[i.status.value] = by_status.get(i.status.value, 0) + 1
        if i.status in ACTUAL_STATUSES:
            actual += i.total
            retention += i.retainage_amount
        if i.status is InvoiceStatus.RECEIVED:
            awaiting += 1
            awaiting_value += i.total
        if i.status is InvoiceStatus.DISPUTED:
            disputed += 1
        if i.status is InvoiceStatus.APPROVED:
            unpaid += 1
            unpaid_value += i.net_payable
            if not i.lien_waiver_received:
                waivers += 1
        if i.is_overdue:
            overdue += 1
            overdue_value += i.net_payable
        if i.status is InvoiceStatus.PAID and i.paid_date and i.paid_date >= month_start:
            cis_month += i.cis_deduction
        if (
            i.status not in (InvoiceStatus.VOID, InvoiceStatus.PAID)
            and not i.cis_verified
            and i.labour_amount
        ):
            unverified += 1

    return InvoiceSummary(
        actual_cost=actual,
        awaiting_approval=awaiting,
        awaiting_value=awaiting_value,
        disputed=disputed,
        approved_unpaid=unpaid,
        approved_unpaid_value=unpaid_value,
        overdue_to_pay=overdue,
        overdue_value=overdue_value,
        waivers_missing=waivers,
        retention_held=retention,
        cis_withheld_this_month=cis_month,
        cis_unverified=unverified,
        total=len(rows),
        by_status=by_status,
    )


@inv_router.get("", response_model=list[InvoiceRead])
async def list_invoices(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    purchase_order_id: uuid.UUID | None = None,
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
):
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(Invoice.tenant_id == user.tenant_id)
    )
    if job_id is not None:
        stmt = stmt.where(Invoice.job_id == job_id)
    if purchase_order_id is not None:
        stmt = stmt.where(Invoice.purchase_order_id == purchase_order_id)
    if vendor_id is not None:
        stmt = stmt.where(Invoice.vendor_id == vendor_id)
    if status_filter is not None:
        stmt = stmt.where(Invoice.status == status_filter)

    rows = await db.scalars(stmt.order_by(Invoice.created_at.desc()))
    return list(rows)


@inv_router.post("", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
async def create_invoice(payload: InvoiceCreate, db: DbSession, user: RequirePurchasing):
    """Receive a vendor invoice against a PO. Job and vendor are taken from the PO."""
    po = await get_tenant_scoped(db, PurchaseOrder, user.tenant_id, payload.purchase_order_id)
    # An invoice is billed against a commitment. A draft commits nothing and a void PO
    # no longer does, so there is nothing for the invoice to be billed against.
    if po.status in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.VOID):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{po.po_number} is {po.status.value}; invoices can only be received "
                "against an issued purchase order."
            ),
        )

    duplicate = await db.scalar(
        select(Invoice.id).where(
            Invoice.tenant_id == user.tenant_id,
            func.lower(Invoice.invoice_number) == payload.invoice_number.lower(),
        )
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An invoice with that number already exists.",
        )

    for line in payload.lines:
        await get_tenant_scoped(db, CostCode, user.tenant_id, line.cost_code_id)

    total = sum((line.amount for line in payload.lines), Decimal("0"))
    if payload.labour_amount is not None and payload.labour_amount > total:
        raise HTTPException(
            status_code=422,
            detail="The labour element cannot be more than the invoice total.",
        )

    vendor = await get_tenant_scoped(db, Vendor, user.tenant_id, po.vendor_id)
    invoice_date = payload.invoice_date or date.today()
    inv = Invoice(
        tenant_id=user.tenant_id,
        invoice_number=payload.invoice_number,
        job_id=po.job_id,
        vendor_id=po.vendor_id,
        purchase_order_id=po.id,
        invoice_date=invoice_date,
        # The trade's agreed terms (standard 30 days) unless the invoice says otherwise,
        # so every approved invoice has a date it becomes late against.
        due_date=payload.due_date
        or invoice_date
        + timedelta(
            days=vendor.payment_terms_days
            if vendor.payment_terms_days is not None
            else PAYMENT_TERMS_DAYS
        ),
        lien_waiver_received=payload.lien_waiver_received,
        lien_waiver_date=date.today() if payload.lien_waiver_received else None,
        notes=payload.notes,
        labour_amount=payload.labour_amount,
        # Snapshot the trade's CIS rate. No status on record deducts nothing but is
        # flagged, rather than silently guessing 30%.
        cis_rate=CIS_RATES[vendor.cis_status] if vendor.cis_status else Decimal("0"),
        cis_verified=vendor.cis_status is not None,
        # Inherit the commitment's retainage rate unless this invoice overrides it.
        # Snapshotted here so editing the PO later cannot restate a paid invoice.
        retainage_percent=(
            payload.retainage_percent
            if payload.retainage_percent is not None
            else po.retainage_percent
        ),
    )
    db.add(inv)
    await db.flush()

    for line in payload.lines:
        db.add(
            InvoiceLine(
                tenant_id=user.tenant_id,
                invoice_id=inv.id,
                cost_code_id=line.cost_code_id,
                amount=line.amount,
                description=line.description,
                purchase_order_line_id=line.purchase_order_line_id,
            )
        )

    await db.commit()
    return await _load_invoice(db, user.tenant_id, inv.id)


@inv_router.post("/{invoice_id}/approve", response_model=InvoiceRead)
async def approve_invoice(
    invoice_id: uuid.UUID,
    db: DbSession,
    user: RequirePurchasing,
    allow_over_po: bool = Query(
        default=False, description="Approve even though the invoice exceeds its purchase order."
    ),
    override_reason: str | None = Query(
        default=None, max_length=500, description="Why billing above the PO was agreed."
    ),
):
    """Approve for payment. This is what turns committed cost into actual cost.

    Refuses by default when the invoice total exceeds the PO it is matched to. A vendor
    billing above their PO is the single most common way margin leaks out of a job. An
    override is written onto the invoice with the reason and who approved it.
    """
    inv = await _load_invoice(db, user.tenant_id, invoice_id)

    if inv.status is not InvoiceStatus.RECEIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Resolve the dispute before approving."
                if inv.status is InvoiceStatus.DISPUTED
                else f"Invoice is already {inv.status.value}."
            ),
        )

    po = await db.scalar(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(PurchaseOrder.id == inv.purchase_order_id)
    )
    billed = await db.scalar(
        select(func.coalesce(func.sum(InvoiceLine.amount), 0))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            Invoice.purchase_order_id == inv.purchase_order_id,
            Invoice.status.in_((InvoiceStatus.APPROVED, InvoiceStatus.PAID)),
        )
    )
    over = po is not None and (billed + inv.total) > po.total
    if over and not allow_over_po:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Invoice would bill {billed + inv.total} against a purchase order of "
                f"{po.total}. Raise a change order, or approve with allow_over_po=true."
            ),
        )
    if over:
        inv.over_po_override = True
        inv.override_reason = override_reason

    inv.status = InvoiceStatus.APPROVED
    inv.approved_date = date.today()
    inv.approved_by_id = user.id
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.post("/{invoice_id}/pay", response_model=InvoiceRead)
async def pay_invoice(
    invoice_id: uuid.UUID,
    db: DbSession,
    user: RequirePurchasing,
    payment_reference: str | None = Query(default=None, max_length=120),
):
    """Record the payment. Refused without the waiver on file; the net paid is the total
    less retention held and the CIS deducted for HMRC."""
    inv = await _load_invoice(db, user.tenant_id, invoice_id)
    if inv.status is not InvoiceStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an approved invoice can be paid.",
        )
    if not inv.lien_waiver_received:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No lien waiver on file for this invoice. Record it, then pay.",
        )
    inv.status = InvoiceStatus.PAID
    inv.paid_date = date.today()
    inv.paid_by_id = user.id
    inv.payment_reference = payment_reference
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.post("/{invoice_id}/lien-waiver", response_model=InvoiceRead)
async def record_lien_waiver(invoice_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Record that the waiver has now arrived.

    Payment and retention release both refuse without it — and until this endpoint
    existed the waiver could only be set when the invoice was received, so an invoice
    logged without one could never be paid.
    """
    inv = await _load_invoice(db, user.tenant_id, invoice_id)
    if inv.status is InvoiceStatus.VOID:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invoice is void.")
    inv.lien_waiver_received = True
    inv.lien_waiver_date = date.today()
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.post("/{invoice_id}/dispute", response_model=InvoiceRead)
async def dispute_invoice(
    invoice_id: uuid.UUID, payload: InvoiceDispute, db: DbSession, user: RequirePurchasing
):
    """Hold an invoice back from approval because something on it is wrong — the wrong
    quantity, work not done, a rate that was not agreed. The reason is kept."""
    inv = await _load_invoice(db, user.tenant_id, invoice_id)
    if inv.status is not InvoiceStatus.RECEIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a received invoice can be disputed; this one is {inv.status.value}.",
        )
    inv.status = InvoiceStatus.DISPUTED
    inv.disputed_on = date.today()
    inv.dispute_reason = payload.reason
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.post("/{invoice_id}/resolve", response_model=InvoiceRead)
async def resolve_dispute(invoice_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """The dispute is settled; the invoice returns to the approval queue. The reason it
    was disputed stays on record."""
    inv = await _load_invoice(db, user.tenant_id, invoice_id)
    if inv.status is not InvoiceStatus.DISPUTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This invoice is not in dispute."
        )
    inv.status = InvoiceStatus.RECEIVED
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.post("/{invoice_id}/void", response_model=InvoiceRead)
async def void_invoice(invoice_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Withdraw an invoice logged in error, or one the trade has replaced with a credit
    note and a corrected invoice. An approved or paid invoice is actual cost and cannot
    simply disappear."""
    inv = await _load_invoice(db, user.tenant_id, invoice_id)
    if inv.status not in (InvoiceStatus.RECEIVED, InvoiceStatus.DISPUTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {inv.status.value} invoice cannot be voided.",
        )
    inv.status = InvoiceStatus.VOID
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


@inv_router.get("/retainage/held", response_model=list[InvoiceRead])
async def retainage_held(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = Query(default=None),
):
    """Invoices still holding money back.

    This is the release list at the end of a job: work that was certified and is
    owed, but has not been paid out yet.
    """
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(
            Invoice.tenant_id == user.tenant_id,
            Invoice.status.in_(ACTUAL_STATUSES),
            Invoice.retainage_percent > 0,
            Invoice.retainage_released_date.is_(None),
        )
    )
    if job_id:
        stmt = stmt.where(Invoice.job_id == job_id)
    return list(await db.scalars(stmt.order_by(Invoice.invoice_number)))


@inv_router.post("/{invoice_id}/release-retainage", response_model=InvoiceRead)
async def release_retainage(invoice_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Release the money held back on this invoice.

    Refused unless a lien waiver is on file. Retainage is the builder's last piece
    of leverage to get one, so releasing it without the waiver throws away the only
    reason the money was held.
    """
    inv = await _load_invoice(db, user.tenant_id, invoice_id)

    if inv.status not in ACTUAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Retainage can only be released on an approved or paid invoice.",
        )
    if inv.retainage_percent <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nothing was held back on this invoice.",
        )
    if inv.retainage_released_date is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Retainage on this invoice was already released.",
        )
    if not inv.lien_waiver_received:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No lien waiver on file. Retainage is the leverage for getting one - "
                "record the waiver first."
            ),
        )

    inv.retainage_released_date = date.today()
    await db.commit()
    return await _load_invoice(db, user.tenant_id, invoice_id)


router.include_router(co_router)
router.include_router(inv_router)
