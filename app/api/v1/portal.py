"""Trade portal: what a subcontractor sees.

A trade user is scoped to one vendor and must see only their own work - their POs, their
scheduled tasks, their service orders. They are not employees, cannot be trained, and
work for several builders, so these endpoints are deliberately few and flat.

Scoping note: a trade user is linked to a Vendor by matching the user's email against the
vendor's contact email. That is a placeholder. Before this ships it needs a real
`vendor_users` join table, because email is neither stable nor unique enough to hang
access control on.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models.job import Job
from app.models.purchase_order import PurchaseOrder, PurchaseOrderStatus
from app.models.schedule import JobTask, TaskStatus
from app.models.user import UserRole
from app.models.vendor import Vendor
from app.models.warranty import ServiceOrder, ServiceOrderStatus
from app.schemas.job import PurchaseOrderRead
from app.schemas.operations import JobTaskRead, ServiceOrderRead

router = APIRouter(prefix="/portal", tags=["trade portal"])


async def _vendor_for(db, user) -> Vendor:
    """Resolve the vendor a trade user belongs to, or refuse."""
    if user.role is not UserRole.TRADE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The trade portal is for vendor accounts.",
        )

    vendor = await db.scalar(
        select(Vendor).where(
            Vendor.tenant_id == user.tenant_id,
            func.lower(Vendor.email) == user.email.lower(),
        )
    )
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not linked to a vendor record.",
        )
    return vendor


@router.get("/me")
async def portal_identity(db: DbSession, user: CurrentUser):
    """Who the portal thinks you are, and a count of what is waiting for you."""
    vendor = await _vendor_for(db, user)

    open_pos = await db.scalar(
        select(func.count())
        .select_from(PurchaseOrder)
        .where(
            PurchaseOrder.vendor_id == vendor.id,
            PurchaseOrder.status.in_(
                (PurchaseOrderStatus.ISSUED, PurchaseOrderStatus.ACKNOWLEDGED)
            ),
        )
    )
    upcoming = await db.scalar(
        select(func.count())
        .select_from(JobTask)
        .where(
            JobTask.vendor_id == vendor.id,
            JobTask.status.in_((TaskStatus.READY, TaskStatus.NOT_STARTED)),
        )
    )
    open_service = await db.scalar(
        select(func.count())
        .select_from(ServiceOrder)
        .where(
            ServiceOrder.vendor_id == vendor.id,
            ServiceOrder.status.in_((ServiceOrderStatus.ASSIGNED, ServiceOrderStatus.SCHEDULED)),
        )
    )

    return {
        "vendor_id": vendor.id,
        "vendor_code": vendor.code,
        "vendor_name": vendor.name,
        "open_purchase_orders": open_pos or 0,
        "upcoming_tasks": upcoming or 0,
        "open_service_orders": open_service or 0,
    }


@router.get("/purchase-orders", response_model=list[PurchaseOrderRead])
async def my_purchase_orders(db: DbSession, user: CurrentUser):
    """Only POs issued to this vendor. Drafts are never visible - they are not commitments."""
    vendor = await _vendor_for(db, user)
    rows = await db.scalars(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(
            PurchaseOrder.vendor_id == vendor.id,
            PurchaseOrder.tenant_id == user.tenant_id,
            PurchaseOrder.status != PurchaseOrderStatus.DRAFT,
        )
        .order_by(PurchaseOrder.po_number)
    )
    return list(rows)


@router.post("/purchase-orders/{po_id}/acknowledge", response_model=PurchaseOrderRead)
async def acknowledge(po_id: uuid.UUID, db: DbSession, user: CurrentUser):
    """The trade confirms they have the PO and will do the work."""
    vendor = await _vendor_for(db, user)
    po = await db.scalar(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(
            PurchaseOrder.id == po_id,
            PurchaseOrder.tenant_id == user.tenant_id,
            PurchaseOrder.vendor_id == vendor.id,
        )
    )
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found"
        )
    if po.status is not PurchaseOrderStatus.ISSUED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot acknowledge a purchase order that is {po.status}.",
        )

    po.status = PurchaseOrderStatus.ACKNOWLEDGED
    po.acknowledged_on = date.today()
    await db.commit()
    await db.refresh(po, ["lines"])
    return po


@router.get("/schedule", response_model=list[JobTaskRead])
async def my_schedule(
    db: DbSession,
    user: CurrentUser,
    days: int = Query(default=14, ge=1, le=90, description="Look-ahead window in days."),
):
    """The one question a trade actually has: which lots am I on, and when."""
    vendor = await _vendor_for(db, user)
    horizon = date.today() + timedelta(days=days)

    rows = await db.scalars(
        select(JobTask)
        .where(
            JobTask.vendor_id == vendor.id,
            JobTask.tenant_id == user.tenant_id,
            JobTask.status != TaskStatus.COMPLETE,
            JobTask.scheduled_start <= horizon,
        )
        .order_by(JobTask.scheduled_start)
    )
    return list(rows)


@router.get("/service-orders", response_model=list[ServiceOrderRead])
async def my_service_orders(db: DbSession, user: CurrentUser):
    vendor = await _vendor_for(db, user)
    rows = await db.scalars(
        select(ServiceOrder)
        .where(ServiceOrder.vendor_id == vendor.id, ServiceOrder.tenant_id == user.tenant_id)
        .order_by(ServiceOrder.scheduled_date)
    )
    return list(rows)


@router.get("/jobs/{job_id}")
async def job_summary(job_id: uuid.UUID, db: DbSession, user: CurrentUser):
    """Minimal job detail for a trade: enough to find the house, nothing financial.

    A subcontractor must never see the contract price or the budget.
    """
    vendor = await _vendor_for(db, user)

    has_work = await db.scalar(
        select(func.count())
        .select_from(PurchaseOrder)
        .where(PurchaseOrder.job_id == job_id, PurchaseOrder.vendor_id == vendor.id)
    )
    has_tasks = await db.scalar(
        select(func.count())
        .select_from(JobTask)
        .where(JobTask.job_id == job_id, JobTask.vendor_id == vendor.id)
    )
    if not (has_work or has_tasks):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    job = await db.scalar(select(Job).where(Job.id == job_id, Job.tenant_id == user.tenant_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return {
        "job_id": job.id,
        "job_number": job.job_number,
        "status": job.status,
        "start_date": job.start_date,
        "projected_completion": job.projected_completion,
    }
