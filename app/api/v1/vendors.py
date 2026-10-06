"""The trades directory: one place to see who a trade is, whether they can be given work,
what they are owed, and how they have performed.

The trade record itself (create, edit, archive) stays in catalog.py. This module adds
what sits around it: contacts, specialisms, the directory view and the 360 overview.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.cost_code import CostCode
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceStatus
from app.models.job import Job
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder
from app.models.vendor import Vendor, VendorContact, VendorSpecialism
from app.models.warranty import ServiceOrder, ServiceOrderStatus
from app.schemas.catalog import VendorRead
from app.services import compliance as compliance_service
from app.services import scorecard as scorecard_service

router = APIRouter(tags=["trades"])
ZERO = Decimal("0")


def tax_year_start(today: date | None = None) -> date:
    """The UK tax year runs from 6 April."""
    today = today or date.today()
    start = date(today.year, 4, 6)
    return start if today >= start else date(today.year - 1, 4, 6)


# --- schemas -------------------------------------------------------------------------


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    is_primary: bool = False


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    is_primary: bool | None = None


class ContactRead(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    name: str
    role: str | None
    email: str | None
    phone: str | None
    is_primary: bool

    model_config = {"from_attributes": True}


class SpecialismsSet(BaseModel):
    cost_code_ids: list[uuid.UUID]


class DirectoryRow(VendorRead):
    specialism_ids: list[uuid.UUID] = []
    compliant: bool | None = None          # None for a supplier: not gated
    compliance_note: str | None = None
    open_commitment: Decimal = ZERO        # on committed POs, not yet invoiced
    outstanding: Decimal = ZERO            # approved, not yet paid (net)
    overdue: int = 0
    contacts: int = 0


class TradesSummary(BaseModel):
    active: int
    archived: int
    trades: int
    suppliers: int
    not_compliant: int
    cis_unverified: int
    open_commitment: Decimal
    outstanding: Decimal
    overdue_invoices: int


class DocRow(BaseModel):
    id: uuid.UUID
    number: str
    job_number: str | None
    status: str
    total: Decimal
    date: date | None


class TradeOverview(BaseModel):
    vendor: VendorRead
    contacts: list[ContactRead]
    specialisms: list[dict]
    compliance: dict
    committed: Decimal
    billed: Decimal
    open_commitment: Decimal
    outstanding: Decimal
    overdue: int
    retention_held: Decimal
    paid_this_tax_year: Decimal
    cis_this_tax_year: Decimal
    tax_year_from: date
    homes: int
    service_orders_open: int
    service_orders_total: int
    score: dict | None
    recent_pos: list[DocRow]
    recent_invoices: list[DocRow]


# --- helpers ----------------------------------------------------------------------------


async def _vendor(db, tenant_id, vendor_id: uuid.UUID) -> Vendor:
    v = await db.scalar(select(Vendor).where(Vendor.id == vendor_id, Vendor.tenant_id == tenant_id))
    if v is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    return v


async def _money(db, tenant_id, vendor_ids: list[uuid.UUID]):
    """Committed, billed, outstanding and overdue per trade, in two queries."""
    ids = vendor_ids or [uuid.uuid4()]
    pos = list(
        await db.scalars(
            select(PurchaseOrder)
            .options(selectinload(PurchaseOrder.lines))
            .where(PurchaseOrder.tenant_id == tenant_id, PurchaseOrder.vendor_id.in_(ids))
        )
    )
    invs = list(
        await db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.tenant_id == tenant_id, Invoice.vendor_id.in_(ids))
        )
    )
    billed_po: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for i in invs:
        if i.status in ACTUAL_STATUSES:
            billed_po[i.purchase_order_id] += i.total
    out: dict[uuid.UUID, dict] = defaultdict(
        lambda: {"committed": ZERO, "billed": ZERO, "open": ZERO, "outstanding": ZERO,
                 "overdue": 0}
    )
    for po in pos:
        if po.status in COMMITTED_STATUSES:
            row = out[po.vendor_id]
            row["committed"] += po.total
            row["open"] += max(po.total - billed_po[po.id], ZERO)
    for i in invs:
        row = out[i.vendor_id]
        if i.status in ACTUAL_STATUSES:
            row["billed"] += i.total
        if i.status is InvoiceStatus.APPROVED:
            row["outstanding"] += i.net_payable
            if i.is_overdue:
                row["overdue"] += 1
    return out, pos, invs


# --- directory and summary ----------------------------------------------------------------


@router.get("/trades/directory", response_model=list[DirectoryRow])
async def directory(
    db: DbSession,
    user: RequireStaff,
    include_archived: bool = False,
    cost_code_id: uuid.UUID | None = Query(default=None, description="Trades that do this."),
    search: str | None = Query(default=None, max_length=100),
):
    """Every trade with what matters before giving them work: cleared or not, what is
    already committed to them, and what they are owed."""
    stmt = select(Vendor).where(Vendor.tenant_id == user.tenant_id)
    if not include_archived:
        stmt = stmt.where(Vendor.is_archived.is_(False))
    vendors = list(await db.scalars(stmt.order_by(Vendor.name)))
    if search:
        q = search.strip().lower()
        vendors = [
            v for v in vendors
            if q in v.name.lower() or q in v.code.lower() or q in (v.email or "").lower()
        ]
    ids = [v.id for v in vendors] or [uuid.uuid4()]
    specs: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for s in await db.scalars(select(VendorSpecialism).where(VendorSpecialism.vendor_id.in_(ids))):
        specs[s.vendor_id].append(s.cost_code_id)
    if cost_code_id is not None:
        vendors = [v for v in vendors if cost_code_id in specs[v.id]]
    contacts: dict[uuid.UUID, int] = defaultdict(int)
    for c in await db.scalars(select(VendorContact).where(VendorContact.vendor_id.in_(ids))):
        contacts[c.vendor_id] += 1
    money, _, _ = await _money(db, user.tenant_id, [v.id for v in vendors])

    out = []
    for v in vendors:
        row = DirectoryRow(**VendorRead.model_validate(v).model_dump())
        row.specialism_ids = specs[v.id]
        row.contacts = contacts[v.id]
        m = money[v.id]
        row.open_commitment = m["open"]
        row.outstanding = m["outstanding"]
        row.overdue = m["overdue"]
        if v.vendor_type.value != "supplier":
            check = await compliance_service.check_vendor(db, user.tenant_id, v.id)
            row.compliant = check.ok
            row.compliance_note = check.reason or check.warning
        out.append(row)
    return out


@router.get("/trades/summary", response_model=TradesSummary)
async def trades_summary(db: DbSession, user: RequireStaff):
    vendors = list(await db.scalars(select(Vendor).where(Vendor.tenant_id == user.tenant_id)))
    live = [v for v in vendors if not v.is_archived]
    money, _, _ = await _money(db, user.tenant_id, [v.id for v in live])
    not_compliant = 0
    for v in live:
        if v.vendor_type.value != "supplier":
            if not (await compliance_service.check_vendor(db, user.tenant_id, v.id)).ok:
                not_compliant += 1
    return TradesSummary(
        active=len(live),
        archived=len(vendors) - len(live),
        trades=sum(1 for v in live if v.vendor_type.value != "supplier"),
        suppliers=sum(1 for v in live if v.vendor_type.value == "supplier"),
        not_compliant=not_compliant,
        cis_unverified=sum(
            1 for v in live if v.vendor_type.value != "supplier" and v.cis_status is None
        ),
        open_commitment=sum((money[v.id]["open"] for v in live), ZERO),
        outstanding=sum((money[v.id]["outstanding"] for v in live), ZERO),
        overdue_invoices=sum(money[v.id]["overdue"] for v in live),
    )


# --- one trade ------------------------------------------------------------------------------


@router.get("/trades/{vendor_id}/overview", response_model=TradeOverview)
async def overview(vendor_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Everything about one trade on one page."""
    v = await _vendor(db, user.tenant_id, vendor_id)
    money, pos, invs = await _money(db, user.tenant_id, [v.id])
    m = money[v.id]
    ty = tax_year_start()
    paid = [i for i in invs if i.status is InvoiceStatus.PAID and i.paid_date and i.paid_date >= ty]

    jobs = {
        j.id: j.job_number
        for j in await db.scalars(
            select(Job).where(
                Job.id.in_({p.job_id for p in pos} | {i.job_id for i in invs} or {uuid.uuid4()})
            )
        )
    }
    orders = list(await db.scalars(select(ServiceOrder).where(ServiceOrder.vendor_id == v.id)))
    check = await compliance_service.check_vendor(db, user.tenant_id, v.id)
    score = await scorecard_service.scorecard(db, user.tenant_id, v.id)
    contacts = list(
        await db.scalars(
            select(VendorContact)
            .where(VendorContact.vendor_id == v.id)
            .order_by(VendorContact.is_primary.desc(), VendorContact.name)
        )
    )
    spec_rows = list(
        await db.scalars(
            select(CostCode)
            .join(VendorSpecialism, VendorSpecialism.cost_code_id == CostCode.id)
            .where(VendorSpecialism.vendor_id == v.id)
            .order_by(CostCode.code)
        )
    )

    def po_row(p):
        return DocRow(id=p.id, number=p.po_number, job_number=jobs.get(p.job_id),
                      status=p.status.value, total=p.total, date=p.issued_date)

    def inv_row(i):
        return DocRow(id=i.id, number=i.invoice_number, job_number=jobs.get(i.job_id),
                      status=i.status.value, total=i.total, date=i.invoice_date)

    return TradeOverview(
        vendor=VendorRead.model_validate(v),
        contacts=[ContactRead.model_validate(c) for c in contacts],
        specialisms=[{"id": c.id, "code": c.code, "name": c.name} for c in spec_rows],
        compliance={
            "applies": v.vendor_type.value != "supplier",
            "ok": check.ok, "reason": check.reason, "warning": check.warning,
            "expired": check.expired, "expiring": check.expiring,
        },
        committed=m["committed"],
        billed=m["billed"],
        open_commitment=m["open"],
        outstanding=m["outstanding"],
        overdue=m["overdue"],
        retention_held=sum((i.retainage_amount for i in invs if i.status in ACTUAL_STATUSES), ZERO),
        paid_this_tax_year=sum((i.total - i.retention_withheld for i in paid), ZERO),
        cis_this_tax_year=sum((i.cis_deduction for i in paid), ZERO),
        tax_year_from=ty,
        homes=len({p.job_id for p in pos if p.status in COMMITTED_STATUSES}),
        service_orders_open=sum(
            1 for o in orders
            if o.status in (ServiceOrderStatus.ASSIGNED, ServiceOrderStatus.SCHEDULED)
        ),
        service_orders_total=len(orders),
        score=score[0] if score else None,
        recent_pos=[po_row(p) for p in sorted(pos, key=lambda p: p.created_at, reverse=True)[:6]],
        recent_invoices=[
            inv_row(i) for i in sorted(invs, key=lambda i: i.created_at, reverse=True)[:6]
        ],
    )


# Contacts are read through the overview, which already carries them.


async def _only_primary(db, vendor_id, keep_id) -> None:
    for c in await db.scalars(
        select(VendorContact).where(
            VendorContact.vendor_id == vendor_id, VendorContact.id != keep_id,
            VendorContact.is_primary.is_(True),
        )
    ):
        c.is_primary = False


@router.post(
    "/trades/{vendor_id}/contacts", response_model=ContactRead, status_code=status.HTTP_201_CREATED
)
async def add_contact(vendor_id: uuid.UUID, payload: ContactIn, db: DbSession, admin: RequireAdmin):
    """Add a person. The first one, or one marked primary, becomes the main contact."""
    v = await _vendor(db, admin.tenant_id, vendor_id)
    existing = await db.scalar(select(VendorContact.id).where(VendorContact.vendor_id == v.id))
    data = payload.model_dump()
    data["email"] = str(data["email"]) if data["email"] else None
    c = VendorContact(tenant_id=admin.tenant_id, vendor_id=v.id, **data)
    if existing is None:
        c.is_primary = True
    db.add(c)
    await db.flush()
    if c.is_primary:
        await _only_primary(db, v.id, c.id)
        v.contact_name = c.name
        v.email = c.email or v.email
        v.phone = c.phone or v.phone
    await db.commit()
    await db.refresh(c)
    return c


@router.patch("/vendor-contacts/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: uuid.UUID, payload: ContactUpdate, db: DbSession, admin: RequireAdmin
):
    c = await db.scalar(
        select(VendorContact).where(
            VendorContact.id == contact_id, VendorContact.tenant_id == admin.tenant_id
        )
    )
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"]:
        data["email"] = str(data["email"])
    for key, value in data.items():
        if key in ("name", "is_primary") and value is None:
            continue
        setattr(c, key, value)
    if c.is_primary:
        # The trade record's own contact fields follow the primary contact, so the
        # portal link and every PO header stay right.
        await _only_primary(db, c.vendor_id, c.id)
        v = await _vendor(db, admin.tenant_id, c.vendor_id)
        v.contact_name = c.name
        v.email = c.email or v.email
        v.phone = c.phone or v.phone
    await db.commit()
    await db.refresh(c)
    return c


@router.delete("/vendor-contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(contact_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    c = await db.scalar(
        select(VendorContact).where(
            VendorContact.id == contact_id, VendorContact.tenant_id == admin.tenant_id
        )
    )
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    await db.delete(c)
    await db.commit()


@router.put("/trades/{vendor_id}/specialisms", response_model=list[dict])
async def set_specialisms(
    vendor_id: uuid.UUID, payload: SpecialismsSet, db: DbSession, admin: RequireAdmin
):
    """The cost codes this trade does. Replaces the list."""
    v = await _vendor(db, admin.tenant_id, vendor_id)
    ids = list(dict.fromkeys(payload.cost_code_ids))
    codes = list(
        await db.scalars(
            select(CostCode).where(
                CostCode.tenant_id == admin.tenant_id, CostCode.id.in_(ids or [uuid.uuid4()])
            )
        )
    )
    if len(codes) != len(ids):
        raise HTTPException(status_code=422, detail="One of those cost codes does not exist.")
    for s in await db.scalars(select(VendorSpecialism).where(VendorSpecialism.vendor_id == v.id)):
        await db.delete(s)
    await db.flush()
    for cid in ids:
        db.add(VendorSpecialism(tenant_id=admin.tenant_id, vendor_id=v.id, cost_code_id=cid))
    await db.commit()
    codes.sort(key=lambda c: c.code)
    return [{"id": c.id, "code": c.code, "name": c.name} for c in codes]
