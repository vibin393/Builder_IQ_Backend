"""Purchase orders: committing money to a trade on a home.

Issuing a PO is what turns a budget number into a real obligation, so it is
guarded by role here and by two business rules in the service tier. Overriding
either rule is allowed, and recorded on the PO with the reason.

API tier only. No SQL, no business rules - see `app.services.purchasing`.
"""

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequirePurchasing, RequireStaff
from app.models.purchase_order import PurchaseOrderStatus
from app.schemas.job import (
    PurchaseOrderCreate,
    PurchaseOrderRead,
    PurchaseOrderSummary,
    PurchaseOrderUpdate,
)
from app.services.purchasing import PurchasingService

router = APIRouter(prefix="/purchase-orders", tags=["purchasing"])


@router.get("/summary", response_model=PurchaseOrderSummary)
async def summary(db: DbSession, user: RequireStaff):
    """Committed, drafts, open, late, billed-to-date and the overrides on record."""
    return await PurchasingService(db, user.tenant_id).summary()


@router.get("", response_model=list[PurchaseOrderRead])
async def list_purchase_orders(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    status_filter: PurchaseOrderStatus | None = Query(default=None, alias="status"),
    overdue_only: bool = False,
):
    return await PurchasingService(db, user.tenant_id).list(
        job_id=job_id, vendor_id=vendor_id, status=status_filter, overdue_only=overdue_only
    )


@router.post("", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED)
async def create_purchase_order(
    payload: PurchaseOrderCreate, db: DbSession, user: RequirePurchasing
):
    """Draft a PO. Drafts are not commitments - issuing it is what commits the money."""
    return await PurchasingService(db, user.tenant_id).create(
        job_id=payload.job_id,
        vendor_id=payload.vendor_id,
        lines=payload.lines,
        notes=payload.notes,
        retainage_percent=payload.retainage_percent,
        required_by=payload.required_by,
    )


@router.get("/{po_id}", response_model=PurchaseOrderRead)
async def get_purchase_order(po_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await PurchasingService(db, user.tenant_id).get(po_id)


@router.patch("/{po_id}", response_model=PurchaseOrderRead)
async def update_purchase_order(
    po_id: uuid.UUID, payload: PurchaseOrderUpdate, db: DbSession, user: RequirePurchasing
):
    """Edit a draft: notes, required-by, retainage, or replace its lines."""
    fields = payload.model_dump(exclude_unset=True, exclude={"lines"})
    return await PurchasingService(db, user.tenant_id).update_draft(
        po_id, fields=fields, lines=payload.lines
    )


@router.post("/{po_id}/issue", response_model=PurchaseOrderRead)
async def issue_purchase_order(
    po_id: uuid.UUID,
    db: DbSession,
    user: RequirePurchasing,
    allow_over_budget: bool = Query(
        default=False,
        description="Issue even if this PO takes committed cost past the revised budget.",
    ),
    skip_compliance: bool = Query(
        default=False,
        description="Issue even though the trade is not cleared. Records a deliberate override.",
    ),
    override_reason: str | None = Query(
        default=None, max_length=500, description="Why the override was justified."
    ),
):
    """Issue the PO to the trade. This is the moment the money becomes committed.

    Two guards run before it moves: the trade must be cleared to work, and the
    commitment must fit the remaining budget. Both refuse by default and both
    can be overridden deliberately; an override is written onto the PO.
    """
    return await PurchasingService(db, user.tenant_id).issue(
        po_id,
        allow_over_budget=allow_over_budget,
        skip_compliance=skip_compliance,
        override_reason=override_reason,
        issued_by_id=user.id,
    )


@router.post("/{po_id}/acknowledge", response_model=PurchaseOrderRead)
async def acknowledge_purchase_order(po_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Record that the trade has confirmed the PO."""
    return await PurchasingService(db, user.tenant_id).acknowledge(po_id)


@router.post("/{po_id}/complete", response_model=PurchaseOrderRead)
async def complete_purchase_order(po_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Mark the scope delivered."""
    return await PurchasingService(db, user.tenant_id).complete(po_id)


@router.post("/{po_id}/void", response_model=PurchaseOrderRead)
async def void_purchase_order(po_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Void a PO, releasing its committed cost back to the budget."""
    return await PurchasingService(db, user.tenant_id).void(po_id)
