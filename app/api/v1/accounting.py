"""The accounting hand-off and the homeowner portal.

**Accounting** is a one-way hand-off, not a live sync. That is deliberate: a real two-way
integration has to answer who owns the supplier master, what happens on partial
failure, and how reconciliation works — none of which should be guessed at. What this
side can guarantee is that every invoice, payment and retention release reaches the
ledger exactly once, as a balanced journal, in a numbered batch the controller confirms.
See `app.models.accounting` and `app.services.accounting`.

**The homeowner portal** is the narrowest surface in the system: their home, their
warranty claims, nothing else. A homeowner must never see a cost, a budget or a trade's
price.
"""

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    CurrentUser,
    DbSession,
    RequireAccounting,
    RequirePurchasing,
    RequireStaff,
)
from app.models.accounting import ExportBatch, ExportFormat, ExportKind, ExportStatus
from app.models.contract import Buyer, Contract
from app.models.cost_code import CostCode
from app.models.estimate import ClientUpdate
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceStatus
from app.models.job import Job
from app.models.owner_invoice import OwnerInvoice, OwnerInvoiceStatus
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder
from app.models.selection import Selection, SelectionApprovalStatus, SelectionChoice
from app.models.user import UserRole
from app.models.vendor import Vendor
from app.models.warranty import WarrantyRequest
from app.schemas.accounting import (
    AccountingSettingsRead,
    AccountingSettingsUpdate,
    AccountingSummary,
    BatchCreate,
    BatchDetail,
    BatchPost,
    BatchRead,
    BatchVoid,
    CISReturn,
    CISReturnRow,
    ExportQueue,
    MappingRead,
    MappingRow,
    ReadyCount,
)
from app.schemas.homeowner import BuyerInvoice, BuyerUpdate
from app.schemas.operations import WarrantyRequestCreate, WarrantyRequestRead
from app.schemas.signoff import BuyerApprove, BuyerDecline, SelectionSignoffRead
from app.services import accounting as acct
from app.services.signoff import SignoffService

router = APIRouter()

# ==============================================================================
# Accounting hand-off
# ==============================================================================

acct_router = APIRouter(prefix="/accounting", tags=["accounting"])

_KIND_NOUN = {
    ExportKind.INVOICES: "invoices",
    ExportKind.PAYMENTS: "payments",
    ExportKind.RETENTION: "retention releases",
}
ZERO = Decimal("0")


async def _next_batch_number(db, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count()).select_from(ExportBatch).where(ExportBatch.tenant_id == tenant_id)
    )
    return f"EXP-{(count or 0) + 1:05d}"


async def _load_batch(db, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> ExportBatch:
    batch = await db.scalar(
        select(ExportBatch).where(ExportBatch.id == batch_id, ExportBatch.tenant_id == tenant_id)
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return batch


async def _batch_detail(db, tenant_id: uuid.UUID, batch: ExportBatch) -> BatchDetail:
    """A batch with the journal it carries. A void batch has released its items, so it
    keeps its snapshot totals and shows no lines."""
    cfg = await acct.settings_for(db, tenant_id)
    lk = await acct.lookups(db, tenant_id)
    invoices = await acct.items_in_batch(db, tenant_id, batch.kind, batch.id)
    items, journal = acct.build(invoices, batch.kind, cfg, lk)
    debits, credits = acct.totals(journal)
    return BatchDetail(
        **BatchRead.model_validate(batch).model_dump(),
        items=items,
        journal=journal,
        debits=debits,
        credits=credits,
    )


@acct_router.get("/summary", response_model=AccountingSummary)
async def accounting_summary(db: DbSession, user: RequirePurchasing):
    """What is ready for the ledger, what is stuck, and what has gone but is unconfirmed."""
    today = date.today()
    cfg = await acct.settings_for(db, user.tenant_id)
    ready: dict[str, ReadyCount] = {}
    waiting_total = 0
    for kind in ExportKind:
        rows, waiting = await acct.candidates(db, user.tenant_id, kind, today)
        waiting_total += waiting
        ready[kind.value] = ReadyCount(
            count=len(rows), value=sum((acct.item_amount(i, kind) for i in rows), ZERO)
        )

    live = [
        b
        for b in await db.scalars(
            select(ExportBatch).where(ExportBatch.tenant_id == user.tenant_id)
        )
        if b.status is not ExportStatus.VOID
    ]
    month_start = today.replace(day=1)

    unmapped = await db.scalar(
        select(func.count())
        .select_from(CostCode)
        .where(
            CostCode.tenant_id == user.tenant_id,
            CostCode.is_active.is_(True),
            CostCode.is_archived.is_(False),
            CostCode.gl_account.is_(None),
        )
    )

    # Committed on a PO and not yet invoiced: the encumbrance the ledger cannot see.
    pos = await db.scalars(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(
            PurchaseOrder.tenant_id == user.tenant_id,
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
        )
    )
    invoices = list(
        await db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.tenant_id == user.tenant_id)
        )
    )
    billed: dict[uuid.UUID, Decimal] = {}
    for inv in invoices:
        if inv.status in ACTUAL_STATUSES:
            billed[inv.purchase_order_id] = billed.get(inv.purchase_order_id, ZERO) + inv.total
    open_commitments = sum((max(po.total - billed.get(po.id, ZERO), ZERO) for po in pos), ZERO)

    start, end = acct.tax_month(today)
    cis = sum(
        (
            i.cis_deduction
            for i in invoices
            if i.status is InvoiceStatus.PAID and i.paid_date and start <= i.paid_date <= end
        ),
        ZERO,
    )
    await db.commit()  # settings may have been created on first use

    return AccountingSummary(
        ready=ready,
        waiting=waiting_total,
        unposted_batches=sum(1 for b in live if b.status is ExportStatus.EXPORTED),
        last_export_on=max((b.created_at.date() for b in live), default=None),
        exported_this_month=sum(
            (b.total for b in live if b.created_at.date() >= month_start), ZERO
        ),
        unmapped_cost_codes=unmapped or 0,
        open_commitments=open_commitments,
        cis_this_period=cis,
        locked_through=cfg.locked_through,
    )


@acct_router.get("/queue", response_model=ExportQueue)
async def export_queue(
    db: DbSession,
    user: RequirePurchasing,
    kind: ExportKind = ExportKind.INVOICES,
    through_date: date | None = None,
):
    """What the next batch of this kind would carry, as the journal it would post."""
    through = through_date or date.today()
    cfg = await acct.settings_for(db, user.tenant_id)
    rows, waiting = await acct.candidates(db, user.tenant_id, kind, through)
    items, journal = acct.build(rows, kind, cfg, await acct.lookups(db, user.tenant_id))
    debits, credits = acct.totals(journal)
    await db.commit()
    return ExportQueue(
        kind=kind,
        through_date=through,
        items=items,
        journal=journal,
        debits=debits,
        credits=credits,
        balanced=debits == credits,
        waiting=waiting,
    )


@acct_router.get("/batches", response_model=list[BatchRead])
async def list_batches(
    db: DbSession,
    user: RequirePurchasing,
    kind: ExportKind | None = None,
    status_filter: ExportStatus | None = Query(default=None, alias="status"),
):
    stmt = select(ExportBatch).where(ExportBatch.tenant_id == user.tenant_id)
    if kind is not None:
        stmt = stmt.where(ExportBatch.kind == kind)
    if status_filter is not None:
        stmt = stmt.where(ExportBatch.status == status_filter)
    return list(await db.scalars(stmt.order_by(ExportBatch.created_at.desc())))


@acct_router.post("/batches", response_model=BatchDetail, status_code=status.HTTP_201_CREATED)
async def create_batch(payload: BatchCreate, db: DbSession, user: RequireAccounting):
    """Sweep everything ready of one kind into a numbered batch.

    Each item is stamped with the batch, so it never appears in another one. Refused
    when there is nothing to send, and when the journal would not balance.
    """
    through = payload.through_date or date.today()
    if through > date.today():
        raise HTTPException(
            status_code=422, detail="A batch cannot sweep in items dated in the future."
        )
    cfg = await acct.settings_for(db, user.tenant_id)
    rows, waiting = await acct.candidates(db, user.tenant_id, payload.kind, through)
    if not rows:
        extra = f" {waiting} are waiting for their invoice to be exported first." if waiting else ""
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No {_KIND_NOUN[payload.kind]} are ready to export.{extra}",
        )
    _, journal = acct.build(rows, payload.kind, cfg, await acct.lookups(db, user.tenant_id))
    debits, credits = acct.totals(journal)
    if debits != credits:  # cannot happen by construction; refuse loudly if it ever does
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The journal does not balance (Dr {debits} / Cr {credits}). Not exported.",
        )

    batch = ExportBatch(
        tenant_id=user.tenant_id,
        batch_number=await _next_batch_number(db, user.tenant_id),
        kind=payload.kind,
        through_date=through,
        item_count=len(rows),
        total=sum((acct.item_amount(i, payload.kind) for i in rows), ZERO),
        notes=payload.notes,
        created_by_id=user.id,
    )
    db.add(batch)
    await db.flush()
    column = acct.batch_column(payload.kind).key
    for inv in rows:
        setattr(inv, column, batch.id)
    await db.commit()
    await db.refresh(batch)
    return await _batch_detail(db, user.tenant_id, batch)


@acct_router.get("/batches/{batch_id}", response_model=BatchDetail)
async def get_batch(batch_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    return await _batch_detail(db, user.tenant_id, await _load_batch(db, user.tenant_id, batch_id))


@acct_router.get("/batches/{batch_id}/download")
async def download_batch(
    batch_id: uuid.UUID,
    db: DbSession,
    user: RequirePurchasing,
    fmt: ExportFormat | None = Query(default=None, alias="format"),
):
    """The batch as an import file. Re-downloading is safe - it is the same batch - and
    downloads are counted, so a double import is easy to spot."""
    batch = await _load_batch(db, user.tenant_id, batch_id)
    if batch.status is ExportStatus.VOID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This batch was voided; its items are back in the queue. Nothing to import.",
        )
    cfg = await acct.settings_for(db, user.tenant_id)
    chosen = fmt or cfg.default_format
    detail = await _batch_detail(db, user.tenant_id, batch)
    body = acct.render(detail.journal, chosen, batch.batch_number)

    batch.download_count += 1
    batch.last_downloaded_at = datetime.now(UTC)
    filename = f"{batch.batch_number}-{batch.kind.value}-{chosen.value}.csv"
    await db.commit()
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@acct_router.post("/batches/{batch_id}/post", response_model=BatchRead)
async def mark_posted(
    batch_id: uuid.UUID, payload: BatchPost, db: DbSession, user: RequireAccounting
):
    """The controller confirms the batch imported cleanly. From here it is in the ledger."""
    batch = await _load_batch(db, user.tenant_id, batch_id)
    if batch.status is not ExportStatus.EXPORTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only an exported batch can be marked posted; this one is {batch.status.value}."
            ),
        )
    batch.status = ExportStatus.POSTED
    batch.posted_at = datetime.now(UTC)
    batch.posted_by_id = user.id
    batch.ledger_reference = (payload.ledger_reference or "").strip() or None
    await db.commit()
    await db.refresh(batch)
    return batch


@acct_router.post("/batches/{batch_id}/void", response_model=BatchRead)
async def void_batch(
    batch_id: uuid.UUID, payload: BatchVoid, db: DbSession, user: RequireAccounting
):
    """Withdraw a batch that was never imported. Its items go back in the queue.

    Refused once posted: the ledger has it, and the fix is a reversing journal there.
    Refused too if a later payment or release batch already relies on its invoices.
    """
    batch = await _load_batch(db, user.tenant_id, batch_id)
    if batch.status is not ExportStatus.EXPORTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A posted batch is in the ledger. Reverse it there with a journal; "
                "it cannot be withdrawn here."
                if batch.status is ExportStatus.POSTED
                else "This batch is already void."
            ),
        )
    column = acct.batch_column(batch.kind).key
    invoices = await acct.items_in_batch(db, user.tenant_id, batch.kind, batch.id)
    if batch.kind is ExportKind.INVOICES:
        dependent = [
            i.invoice_number
            for i in invoices
            if i.payment_batch_id is not None or i.retention_batch_id is not None
        ]
        if dependent:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Payments or releases on {', '.join(dependent[:5])} were exported "
                    "after this batch. Void those batches first."
                ),
            )
    for inv in invoices:
        setattr(inv, column, None)
    batch.status = ExportStatus.VOID
    batch.voided_at = datetime.now(UTC)
    batch.void_reason = payload.reason
    await db.commit()
    await db.refresh(batch)
    return batch


@acct_router.get("/settings", response_model=AccountingSettingsRead)
async def get_settings(db: DbSession, user: RequirePurchasing):
    cfg = await acct.settings_for(db, user.tenant_id)
    await db.commit()
    await db.refresh(cfg)
    return cfg


@acct_router.patch("/settings", response_model=AccountingSettingsRead)
async def update_settings(
    payload: AccountingSettingsUpdate, db: DbSession, user: RequireAccounting
):
    """Control accounts, the default file format, and the period lock."""
    cfg = await acct.settings_for(db, user.tenant_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("locked_through") and data["locked_through"] >= date.today():
        raise HTTPException(
            status_code=422, detail="Only a period that has already ended can be locked."
        )
    for key, value in data.items():
        if value is None and key != "locked_through":
            continue
        setattr(cfg, key, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def _mapping(db, tenant_id: uuid.UUID) -> list[MappingRead]:
    codes = await db.scalars(
        select(CostCode)
        .where(CostCode.tenant_id == tenant_id, CostCode.is_archived.is_(False))
        .order_by(CostCode.code)
    )
    return [
        MappingRead(
            cost_code_id=c.id,
            code=c.code,
            name=c.name,
            division=c.division,
            gl_account=c.gl_account,
            is_active=c.is_active,
        )
        for c in codes
    ]


@acct_router.get("/mapping", response_model=list[MappingRead])
async def get_mapping(db: DbSession, user: RequirePurchasing):
    """Cost codes against their nominal codes in the accounting package."""
    return await _mapping(db, user.tenant_id)


@acct_router.put("/mapping", response_model=list[MappingRead])
async def set_mapping(rows: list[MappingRow], db: DbSession, user: RequireAccounting):
    """Set nominal codes for any number of cost codes. Blank clears one."""
    ids = [r.cost_code_id for r in rows]
    codes = {
        c.id: c
        for c in await db.scalars(
            select(CostCode).where(CostCode.tenant_id == user.tenant_id, CostCode.id.in_(ids))
        )
    }
    if any(i not in codes for i in ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cost code")
    for r in rows:
        codes[r.cost_code_id].gl_account = (r.gl_account or "").strip() or None
    await db.commit()
    return await _mapping(db, user.tenant_id)


@acct_router.get("/cis-return", response_model=CISReturn)
async def cis_return(
    db: DbSession,
    user: RequirePurchasing,
    period: date | None = Query(
        default=None, description="Any date in the tax month; defaults to the current one."
    ),
):
    """The monthly CIS return (CIS300) figures: per trade paid in the tax month, what was
    paid, how much of it was materials, and what was deducted for HMRC."""
    start, end = acct.tax_month(period or date.today())
    paid = await db.scalars(
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(
            Invoice.tenant_id == user.tenant_id,
            Invoice.status == InvoiceStatus.PAID,
            Invoice.paid_date >= start,
            Invoice.paid_date <= end,
        )
    )
    vendors = {
        v.id: v for v in await db.scalars(select(Vendor).where(Vendor.tenant_id == user.tenant_id))
    }
    rows: dict[uuid.UUID, CISReturnRow] = {}
    for inv in paid:
        # Only labour brings a payment into CIS. A pure materials supplier is not on it.
        v = vendors.get(inv.vendor_id)
        if not inv.labour_amount or v is None:
            continue
        row = rows.get(v.id)
        if row is None:
            problems = []
            if not v.cis_status:
                problems.append("CIS status never verified with HMRC")
            if not v.utr:
                problems.append("No UTR on record")
            row = rows[v.id] = CISReturnRow(
                vendor_id=v.id,
                supplier_code=v.code,
                supplier_name=v.name,
                utr=v.utr,
                cis_status=v.cis_status.value if v.cis_status else None,
                payments=0,
                gross_paid=ZERO,
                materials=ZERO,
                labour=ZERO,
                deducted=ZERO,
                problems=problems,
            )
        gross = inv.total - inv.retention_withheld
        labour = min(inv.labour_amount, gross)
        row.payments += 1
        row.gross_paid += gross
        row.labour += labour
        row.materials += gross - labour
        row.deducted += inv.cis_deduction

    out = sorted(rows.values(), key=lambda r: r.supplier_name)
    return CISReturn(
        period_start=start,
        period_end=end,
        due_date=acct.return_due(end),
        rows=out,
        gross_paid=sum((r.gross_paid for r in out), ZERO),
        materials=sum((r.materials for r in out), ZERO),
        deducted=sum((r.deducted for r in out), ZERO),
    )


@acct_router.get("/vendors.csv")
async def vendor_master_csv(db: DbSession, user: RequirePurchasing):
    """Supplier master export, for setting the accounting package up, with the CIS
    details the package needs to run the scheme."""
    vendors = await db.scalars(
        select(Vendor).where(Vendor.tenant_id == user.tenant_id).order_by(Vendor.code)
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["code", "name", "type", "contact", "email", "phone", "utr", "cis_status", "active"]
    )
    for v in vendors:
        writer.writerow(
            [
                v.code,
                v.name,
                v.vendor_type,
                v.contact_name or "",
                v.email or "",
                v.phone or "",
                v.utr or "",
                v.cis_status.value if v.cis_status else "",
                "yes" if v.is_active else "no",
            ]
        )
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="suppliers.csv"'},
    )


# ==============================================================================
# Homeowner portal
# ==============================================================================

home_router = APIRouter(prefix="/homeowner", tags=["homeowner portal"])


async def _buyer_for(db, user) -> Buyer:
    """Resolve the homeowner's buyer record by email.

    Placeholder, like the trade portal's vendor lookup: this needs a real
    `buyer_users` join table before it ships. Email is neither stable nor unique
    enough to hang access control on.
    """
    if user.role is not UserRole.HOMEOWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The homeowner portal is for homeowner accounts.",
        )
    buyer = await db.scalar(
        select(Buyer).where(
            Buyer.tenant_id == user.tenant_id,
            Buyer.email.isnot(None),
            Buyer.email == user.email,
        )
    )
    if buyer is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not linked to a buyer record.",
        )
    return buyer


@home_router.get("/me")
async def homeowner_home(db: DbSession, user: CurrentUser):
    """The homeowner's own home: progress and warranty. No costs, ever."""
    buyer = await _buyer_for(db, user)

    contracts = list(await db.scalars(select(Contract).where(Contract.buyer_id == buyer.id)))
    homes = []
    for c in contracts:
        job = await db.get(Job, c.job_id)
        if job is None:
            continue
        open_claims = len(
            list(
                await db.scalars(
                    select(WarrantyRequest).where(
                        WarrantyRequest.job_id == job.id,
                        WarrantyRequest.status.notin_(("resolved", "declined")),
                    )
                )
            )
        )
        homes.append(
            {
                "job_id": job.id,
                "job_number": job.job_number,
                "status": job.status,
                "projected_completion": job.projected_completion,
                "closing_date": job.closing_date,
                # The buyer sees what they agreed to pay - never what it cost to build.
                "contract_total": c.total_price,
                "open_warranty_claims": open_claims,
            }
        )

    return {"buyer_name": buyer.full_name, "homes": homes}


@home_router.get("/warranty", response_model=list[WarrantyRequestRead])
async def homeowner_warranty(db: DbSession, user: CurrentUser):
    buyer = await _buyer_for(db, user)
    rows = await db.scalars(
        select(WarrantyRequest)
        .options(selectinload(WarrantyRequest.service_orders))
        .where(WarrantyRequest.buyer_id == buyer.id)
        .order_by(WarrantyRequest.created_at.desc())
    )
    return list(rows)


@home_router.post(
    "/warranty", response_model=WarrantyRequestRead, status_code=status.HTTP_201_CREATED
)
async def homeowner_submit_claim(payload: WarrantyRequestCreate, db: DbSession, user: CurrentUser):
    """Submit a claim on a home this homeowner actually owns."""
    buyer = await _buyer_for(db, user)

    contract = await db.scalar(
        select(Contract).where(Contract.job_id == payload.job_id, Contract.buyer_id == buyer.id)
    )
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That is not your home.")

    count = await db.scalar(
        select(func.count())
        .select_from(WarrantyRequest)
        .where(WarrantyRequest.tenant_id == user.tenant_id)
    )
    req = WarrantyRequest(
        tenant_id=user.tenant_id,
        request_number=f"WR{(count or 0) + 1:05d}",
        job_id=payload.job_id,
        buyer_id=buyer.id,
        title=payload.title,
        description=payload.description,
        location=payload.location,
        priority=payload.priority,
        submitted_date=date.today(),
    )
    db.add(req)
    await db.commit()

    return await db.scalar(
        select(WarrantyRequest)
        .options(selectinload(WarrantyRequest.service_orders))
        .where(WarrantyRequest.id == req.id)
    )


async def _buyer_job_ids(db, buyer) -> set[uuid.UUID]:
    """The jobs this buyer actually owns — the gate on everything below."""
    rows = await db.scalars(select(Contract.job_id).where(Contract.buyer_id == buyer.id))
    return set(rows)


async def _owned_selection(db, user, job_ids, selection_id) -> Selection:
    """Load a selection and confirm it belongs to one of the buyer's homes.

    404, not 403, when it is someone else's: a buyer must not be able to probe
    which selection ids exist on other people's jobs.
    """
    sel = await db.scalar(
        select(Selection).where(Selection.tenant_id == user.tenant_id, Selection.id == selection_id)
    )
    if sel is None or sel.job_id not in job_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That is not your selection."
        )
    return sel


@home_router.get("/selections", response_model=list[SelectionSignoffRead])
async def homeowner_selections(db: DbSession, user: CurrentUser):
    """Every selection released to this buyer — awaiting them, or already decided.

    Not-yet-submitted selections are hidden: the builder has not released them.
    """
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    if not job_ids:
        return []
    rows = list(
        await db.scalars(
            select(Selection)
            .options(selectinload(Selection.option), selectinload(Selection.choices))
            .where(
                Selection.tenant_id == user.tenant_id,
                Selection.job_id.in_(job_ids),
                Selection.approval_status != SelectionApprovalStatus.NOT_SUBMITTED,
            )
            .order_by(Selection.approval_status, Selection.selected_date.desc())
            .execution_options(populate_existing=True)
        )
    )
    return [SelectionSignoffRead.of(s) for s in rows]


@home_router.post("/selections/{selection_id}/approve", response_model=SelectionSignoffRead)
async def homeowner_approve_selection(
    selection_id: uuid.UUID, payload: BuyerApprove, db: DbSession, user: CurrentUser
):
    """The buyer approves a material with a typed signature — self-service."""
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    await _owned_selection(db, user, job_ids, selection_id)
    svc = SignoffService(db, user.tenant_id)
    sel = await svc.approve(selection_id, signature=payload.signature, comment=payload.comment)
    sel.approved_by_buyer_id = buyer.id
    await db.commit()
    return SelectionSignoffRead.of(sel)


@home_router.post("/selections/{selection_id}/decline", response_model=SelectionSignoffRead)
async def homeowner_decline_selection(
    selection_id: uuid.UUID, payload: BuyerDecline, db: DbSession, user: CurrentUser
):
    """The buyer declines a material, with a reason."""
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    await _owned_selection(db, user, job_ids, selection_id)
    svc = SignoffService(db, user.tenant_id)
    sel = await svc.decline(
        selection_id, reason=payload.reason, signature=payload.signature, comment=payload.comment
    )
    return SelectionSignoffRead.of(sel)


@home_router.post("/selections/choices/{choice_id}/select", response_model=SelectionSignoffRead)
async def homeowner_pick_choice(choice_id: uuid.UUID, db: DbSession, user: CurrentUser):
    """The buyer picks one priced choice on a selection that offers several."""
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    choice = await db.scalar(
        select(SelectionChoice).where(
            SelectionChoice.tenant_id == user.tenant_id, SelectionChoice.id == choice_id
        )
    )
    if choice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Choice not found.")
    await _owned_selection(db, user, job_ids, choice.selection_id)
    svc = SignoffService(db, user.tenant_id)
    sel = await svc.select_choice(choice_id)
    return SelectionSignoffRead.of(sel)


@home_router.get("/invoices", response_model=list[BuyerInvoice])
async def homeowner_invoices(db: DbSession, user: CurrentUser):
    """The buyer's issued invoices — price, paid and balance only, never cost.

    Opening the list marks any still-`sent` invoice as `viewed`, recording the
    date the owner first saw it (BuilderTrend's "Viewed by Owner On").
    """
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    if not job_ids:
        return []
    rows = list(
        await db.scalars(
            select(OwnerInvoice)
            .options(selectinload(OwnerInvoice.lines), selectinload(OwnerInvoice.payments))
            .where(
                OwnerInvoice.tenant_id == user.tenant_id,
                OwnerInvoice.job_id.in_(job_ids),
                OwnerInvoice.status != OwnerInvoiceStatus.DRAFT,
            )
            .order_by(OwnerInvoice.issue_date.desc().nullslast(), OwnerInvoice.created_at.desc())
            .execution_options(populate_existing=True)
        )
    )
    today = date.today()
    changed = False
    for inv in rows:
        if inv.status is OwnerInvoiceStatus.SENT:
            inv.status = OwnerInvoiceStatus.VIEWED
            inv.viewed_by_owner_on = today
            changed = True
    if changed:
        await db.commit()
    return [BuyerInvoice.of(inv) for inv in rows]


@home_router.get("/updates", response_model=list[BuyerUpdate])
async def homeowner_updates(db: DbSession, user: CurrentUser):
    """Published client updates on the buyer's homes — the builder's broadcast feed."""
    buyer = await _buyer_for(db, user)
    job_ids = await _buyer_job_ids(db, buyer)
    if not job_ids:
        return []
    rows = list(
        await db.scalars(
            select(ClientUpdate)
            .where(
                ClientUpdate.tenant_id == user.tenant_id,
                ClientUpdate.job_id.in_(job_ids),
                ClientUpdate.is_published.is_(True),
            )
            .order_by(ClientUpdate.posted_on.desc(), ClientUpdate.created_at.desc())
        )
    )
    return [BuyerUpdate.of(u) for u in rows]


# ==============================================================================
# Extra analytics the new modules make possible
# ==============================================================================

analytics_router = APIRouter(prefix="/reports", tags=["reporting"])


@analytics_router.get("/satisfaction")
async def satisfaction(db: DbSession, user: RequireStaff):
    """NPS and category ratings from homeowner surveys."""
    from app.models.marketing import Survey

    rows = list(
        await db.scalars(
            select(Survey).where(
                Survey.tenant_id == user.tenant_id, Survey.would_recommend.isnot(None)
            )
        )
    )
    if not rows:
        return {
            "responses": 0,
            "nps": None,
            "promoters": 0,
            "passives": 0,
            "detractors": 0,
            "ratings": {},
        }

    promoters = sum(1 for r in rows if r.nps_band == "promoter")
    passives = sum(1 for r in rows if r.nps_band == "passive")
    detractors = sum(1 for r in rows if r.nps_band == "detractor")
    n = len(rows)

    def avg(attr: str) -> float | None:
        vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "responses": n,
        "nps": round(((promoters - detractors) / n) * 100),
        "promoters": promoters,
        "passives": passives,
        "detractors": detractors,
        "ratings": {
            "sales": avg("rating_sales"),
            "construction": avg("rating_construction"),
            "quality": avg("rating_quality"),
            "warranty": avg("rating_warranty"),
        },
    }


@analytics_router.get("/campaign-performance")
async def campaign_performance(db: DbSession, user: RequireStaff):
    """Campaign delivery joined to what each campaign actually produced.

    Delegates to the marketing service rather than recomputing attribution. This
    used to match leads to campaigns with a live case-insensitive comparison of a
    free-text tag against the campaign code - which meant a rename silently
    detached every lead, and it would now disagree with the campaigns screen,
    which reads the stored link. Two attribution paths that differ is worse than
    either of them.
    """
    from app.services.marketing import MarketingService

    return await MarketingService(db, user.tenant_id).performance()


router.include_router(acct_router)
router.include_router(home_router)
router.include_router(analytics_router)
