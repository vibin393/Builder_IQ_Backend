"""Bid management: getting priced work from trades before committing to it.

A bid package bundles a scope and a cost code and goes out to several trades. Their
prices come back and are **levelled** — low against high against the builder's own
estimate — and one is awarded, which raises the purchase order at the quoted price, so
the number the trade quoted is the number the builder commits to. Re-keying that by hand
is where prices quietly drift.

  POST /bid-packages/{id}/bids/{bid_id}/award   award, and raise the PO at the bid price
  GET  /bid-packages/summary                    the board, with coverage and savings
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequirePurchasing,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.bidding import Bid, BidPackage, BidPackageStatus, BidStatus
from app.models.cost_code import CostCode
from app.models.job import Job
from app.models.plan import Plan
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.schemas.bidding import (
    AwardResult,
    BidPackageCreate,
    BidPackageRead,
    BidPackageUpdate,
    BidRead,
    BidSubmit,
    BidSummary,
    InviteRequest,
)
from app.services import numbering

router = APIRouter(prefix="/bid-packages", tags=["bidding"])


async def _next_number(db, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count()).select_from(BidPackage).where(BidPackage.tenant_id == tenant_id)
    )
    return f"BP{(count or 0) + 1:05d}"


async def _load_package(db, tenant_id: uuid.UUID, pkg_id: uuid.UUID) -> BidPackage:
    # populate_existing: this package may already be in the identity map with a stale
    # bids collection (invite adds rows to one we just loaded). Without it the reload
    # returns the cached, empty list.
    pkg = await db.scalar(
        select(BidPackage)
        .options(selectinload(BidPackage.bids))
        .where(BidPackage.id == pkg_id, BidPackage.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid package not found")
    return pkg


# --- analytics ----------------------------------------------------------------


@router.get("/summary", response_model=BidSummary)
async def summary(db: DbSession, user: RequireStaff):
    """The board: what is out, what is late, how many trades are responding, and what
    awarding at the low bid has saved against the builder's own estimates."""
    rows = list(
        await db.scalars(
            select(BidPackage)
            .options(selectinload(BidPackage.bids))
            .where(BidPackage.tenant_id == user.tenant_id)
        )
    )
    draft = out = awarded = cancelled = overdue = invited = submitted = 0
    awarded_value = Decimal("0")
    savings = Decimal("0")
    for p in rows:
        if p.status is BidPackageStatus.DRAFT:
            draft += 1
        elif p.status is BidPackageStatus.OUT_FOR_BID:
            out += 1
        elif p.status is BidPackageStatus.AWARDED:
            awarded += 1
        elif p.status is BidPackageStatus.CANCELLED:
            cancelled += 1
        if p.is_overdue:
            overdue += 1
        invited += p.invited_count
        submitted += p.submitted_count
        if p.awarded_amount is not None:
            awarded_value += p.awarded_amount
            if p.savings_vs_estimate is not None:
                savings += p.savings_vs_estimate

    response_rate = round(submitted / invited, 2) if invited else None

    return BidSummary(
        packages=len(rows),
        draft=draft,
        out_for_bid=out,
        awarded=awarded,
        cancelled=cancelled,
        overdue=overdue,
        invited=invited,
        submitted=submitted,
        response_rate=response_rate,
        awarded_value=awarded_value,
        savings=savings,
    )


# --- packages -----------------------------------------------------------------


@router.get("", response_model=list[BidPackageRead])
async def list_packages(
    db: DbSession,
    user: RequireStaff,
    status_filter: BidPackageStatus | None = Query(default=None, alias="status"),
    overdue_only: bool = False,
):
    stmt = (
        select(BidPackage)
        .options(selectinload(BidPackage.bids))
        .where(BidPackage.tenant_id == user.tenant_id)
    )
    if status_filter is not None:
        stmt = stmt.where(BidPackage.status == status_filter)
    rows = list(await db.scalars(stmt.order_by(BidPackage.package_number)))
    if overdue_only:
        rows = [p for p in rows if p.is_overdue]
    return rows


@router.post("", response_model=BidPackageRead, status_code=status.HTTP_201_CREATED)
async def create_package(payload: BidPackageCreate, db: DbSession, user: RequirePurchasing):
    await get_tenant_scoped(db, CostCode, user.tenant_id, payload.cost_code_id)
    if payload.plan_id:
        await get_tenant_scoped(db, Plan, user.tenant_id, payload.plan_id)
    pkg = BidPackage(
        tenant_id=user.tenant_id,
        package_number=await _next_number(db, user.tenant_id),
        **payload.model_dump(),
    )
    db.add(pkg)
    await db.commit()
    return await _load_package(db, user.tenant_id, pkg.id)


@router.get("/{package_id}", response_model=BidPackageRead)
async def get_package(package_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await _load_package(db, user.tenant_id, package_id)


@router.patch("/{package_id}", response_model=BidPackageRead)
async def update_package(
    package_id: uuid.UUID, payload: BidPackageUpdate, db: DbSession, user: RequirePurchasing
):
    pkg = await _load_package(db, user.tenant_id, package_id)
    if pkg.status in (BidPackageStatus.AWARDED, BidPackageStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {pkg.status.value} package cannot be edited.",
        )
    data = payload.model_dump(exclude_unset=True)
    if data.get("plan_id"):
        await get_tenant_scoped(db, Plan, user.tenant_id, data["plan_id"])
    for field, value in data.items():
        setattr(pkg, field, value)
    await db.commit()
    return await _load_package(db, user.tenant_id, package_id)


@router.post("/{package_id}/cancel", response_model=BidPackageRead)
async def cancel_package(package_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Withdraw a package that will not be awarded. Kept, not deleted, so the numbering
    and the bids received stay on the record."""
    pkg = await _load_package(db, user.tenant_id, package_id)
    if pkg.status is BidPackageStatus.AWARDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An awarded package cannot be cancelled; its purchase order stands.",
        )
    pkg.status = BidPackageStatus.CANCELLED
    await db.commit()
    return await _load_package(db, user.tenant_id, package_id)


# --- bids ---------------------------------------------------------------------


@router.post("/{package_id}/invite", response_model=BidPackageRead)
async def invite_trades(
    package_id: uuid.UUID, payload: InviteRequest, db: DbSession, user: RequirePurchasing
):
    """Invite trades to bid. Each invitation is a Bid row awaiting a price."""
    pkg = await _load_package(db, user.tenant_id, package_id)
    if pkg.status in (BidPackageStatus.AWARDED, BidPackageStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This package is {pkg.status.value}.",
        )

    existing = {b.vendor_id for b in pkg.bids}
    for vendor_id in payload.vendor_ids:
        if vendor_id in existing:
            continue
        await get_tenant_scoped(db, Vendor, user.tenant_id, vendor_id)
        db.add(
            Bid(
                tenant_id=user.tenant_id,
                package_id=pkg.id,
                vendor_id=vendor_id,
                revision_bid=pkg.document_revision,
            )
        )

    pkg.status = BidPackageStatus.OUT_FOR_BID
    await db.commit()
    return await _load_package(db, user.tenant_id, package_id)


@router.post("/{package_id}/bids/{bid_id}/submit", response_model=BidRead)
async def submit_bid(
    package_id: uuid.UUID,
    bid_id: uuid.UUID,
    payload: BidSubmit,
    db: DbSession,
    user: RequireStaff,
):
    """Record a trade's price. Stamped with the document revision it was quoted against."""
    pkg = await _load_package(db, user.tenant_id, package_id)
    bid = await get_tenant_scoped(db, Bid, user.tenant_id, bid_id)
    if bid.package_id != package_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bid is not on this package"
        )
    bid.amount = payload.amount
    bid.notes = payload.notes
    bid.status = BidStatus.SUBMITTED
    bid.submitted_date = date.today()
    bid.revision_bid = pkg.document_revision
    await db.commit()
    await db.refresh(bid)
    return bid


@router.post("/{package_id}/bids/{bid_id}/decline", response_model=BidRead)
async def decline_bid(package_id: uuid.UUID, bid_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Record that a trade has declined to bid (a 'no bid'). It stops showing as an
    outstanding invitation and counts toward coverage as a response."""
    bid = await get_tenant_scoped(db, Bid, user.tenant_id, bid_id)
    if bid.package_id != package_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bid is not on this package"
        )
    if bid.status is BidStatus.AWARDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An awarded bid cannot be declined."
        )
    bid.status = BidStatus.DECLINED
    bid.amount = None
    await db.commit()
    await db.refresh(bid)
    return bid


@router.post("/{package_id}/revise", response_model=BidPackageRead)
async def revise_documents(package_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Bump the document revision. Bids quoted against the old one become stale."""
    pkg = await _load_package(db, user.tenant_id, package_id)
    pkg.document_revision += 1
    await db.commit()
    return await _load_package(db, user.tenant_id, package_id)


@router.post("/{package_id}/bids/{bid_id}/award", response_model=AwardResult)
async def award_bid(
    package_id: uuid.UUID,
    bid_id: uuid.UUID,
    db: DbSession,
    user: RequirePurchasing,
    job_id: uuid.UUID | None = Query(
        default=None, description="Raise the purchase order against this job."
    ),
):
    """Award the package and, when a job is named, raise the PO at the bid price."""
    pkg = await _load_package(db, user.tenant_id, package_id)
    bid = next((b for b in pkg.bids if b.id == bid_id), None)
    if bid is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")
    if bid.amount is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That bid has no price yet."
        )
    if bid.revision_bid != pkg.document_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That bid was priced against revision {bid.revision_bid}, but the package is "
                f"now at revision {pkg.document_revision}. Re-solicit before awarding."
            ),
        )

    not_awarded = 0
    for b in pkg.bids:
        if b.id == bid_id:
            b.status = BidStatus.AWARDED
        elif b.status is BidStatus.SUBMITTED:
            b.status = BidStatus.NOT_AWARDED
            not_awarded += 1

    pkg.status = BidPackageStatus.AWARDED
    savings = pkg.estimate - bid.amount if pkg.estimate is not None else None

    po = None
    if job_id is not None:
        await get_tenant_scoped(db, Job, user.tenant_id, job_id)
        po = PurchaseOrder(
            tenant_id=user.tenant_id,
            po_number=await numbering.next_po_number(db, user.tenant_id),
            job_id=job_id,
            vendor_id=bid.vendor_id,
            notes=f"Awarded from bid package {pkg.package_number}",
        )
        db.add(po)
        await db.flush()
        db.add(
            PurchaseOrderLine(
                tenant_id=user.tenant_id,
                purchase_order_id=po.id,
                cost_code_id=pkg.cost_code_id,
                amount=bid.amount,
                description=pkg.name,
            )
        )

    await db.commit()

    return AwardResult(
        bid_id=bid.id,
        vendor_id=bid.vendor_id,
        amount=bid.amount,
        purchase_order_id=po.id if po else None,
        po_number=po.po_number if po else None,
        not_awarded=not_awarded,
        savings_vs_estimate=savings,
    )
