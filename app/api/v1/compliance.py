"""Trade prequalification and the compliance gate.

Procore's strongest idea that neither BuildTopia nor most homebuilder platforms have: a
trade must be qualified before they are given work, and their insurance has to still be
in date on the day the work is committed. The purchase-order flow calls this module's
gate before it commits money; the register here is what that gate reads.

  POST /prequalification/{id}/approve        clears a trade for work
  GET  /prequalification/summary             the board, including who the gate would refuse
  GET  /prequalification/cover-gaps          work booked to run past a lapse
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequirePurchasing,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.compliance import ComplianceDocument, Prequalification, PrequalStatus
from app.models.vendor import Vendor
from app.schemas.compliance import (
    ApprovePayload,
    ComplianceCheck,
    ComplianceDocumentCreate,
    ComplianceSummary,
    ExpiringDocument,
    InviteBody,
    PrequalificationCreate,
    PrequalificationRead,
    PrequalificationUpdate,
)
from app.services import compliance as compliance_service

router = APIRouter(prefix="/prequalification", tags=["prequalification"])

#: The annual re-examination window applied when a review is renewed.
REVIEW_PERIOD_DAYS = 365


async def _load_pq(db, tenant_id: uuid.UUID, pq_id: uuid.UUID) -> Prequalification:
    obj = await db.scalar(
        select(Prequalification)
        .options(selectinload(Prequalification.documents))
        .where(Prequalification.id == pq_id, Prequalification.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Prequalification not found"
        )
    return obj


# --- analytics ----------------------------------------------------------------


@router.get("/summary", response_model=ComplianceSummary)
async def summary(db: DbSession, user: RequireStaff):
    """The board: who is approved, who the purchase-order gate would refuse today, who
    has no record at all, and how much paperwork is lapsing."""
    tenant = user.tenant_id
    rows = list(
        await db.scalars(
            select(Prequalification)
            .options(selectinload(Prequalification.documents))
            .where(Prequalification.tenant_id == tenant)
        )
    )
    by_status: dict[str, int] = {}
    approved = conditional = refused = review_overdue = missing = expiring = expired_docs = 0
    for p in rows:
        by_status[p.status.value] = by_status.get(p.status.value, 0) + 1
        if p.status is PrequalStatus.APPROVED:
            approved += 1
        elif p.status is PrequalStatus.CONDITIONAL:
            conditional += 1
        if not p.is_compliant:
            refused += 1
        if p.review_overdue:
            review_overdue += 1
        if p.missing_required:
            missing += 1
        expiring += len(p.expiring_soon)
        expired_docs += len(p.expired_documents)

    # Active trades with no prequalification record at all.
    vendor_ids = set(await db.scalars(select(Vendor.id).where(Vendor.tenant_id == tenant)))
    recorded = {p.vendor_id for p in rows}
    no_record = len(vendor_ids - recorded)

    gaps = await compliance_service.cover_gaps_on_schedule(db, tenant)

    return ComplianceSummary(
        prequalified=len(rows),
        cleared=len(rows) - refused,
        approved=approved,
        conditional=conditional,
        would_be_refused=refused,
        no_record=no_record,
        review_overdue=review_overdue,
        missing_required=missing,
        expiring_soon=expiring,
        expired_documents=expired_docs,
        cover_gaps=len(gaps),
        by_status=by_status,
    )


# --- register -----------------------------------------------------------------


@router.get("", response_model=list[PrequalificationRead])
async def list_prequalifications(db: DbSession, user: RequireStaff):
    rows = await db.scalars(
        select(Prequalification)
        .options(selectinload(Prequalification.documents))
        .where(Prequalification.tenant_id == user.tenant_id)
    )
    return list(rows)


@router.post("", response_model=PrequalificationRead, status_code=status.HTTP_201_CREATED)
async def create_prequalification(
    payload: PrequalificationCreate, db: DbSession, user: RequirePurchasing
):
    await get_tenant_scoped(db, Vendor, user.tenant_id, payload.vendor_id)
    existing = await db.scalar(
        select(Prequalification.id).where(Prequalification.vendor_id == payload.vendor_id)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That trade already has a prequalification record.",
        )
    obj = Prequalification(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(obj)
    await db.commit()
    return await _load_pq(db, user.tenant_id, obj.id)


@router.patch("/{pq_id}", response_model=PrequalificationRead)
async def update_prequalification(
    pq_id: uuid.UUID, payload: PrequalificationUpdate, db: DbSession, user: RequirePurchasing
):
    obj = await _load_pq(db, user.tenant_id, pq_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


# --- documents ----------------------------------------------------------------


@router.post(
    "/{pq_id}/documents",
    response_model=PrequalificationRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_compliance_document(
    pq_id: uuid.UUID, payload: ComplianceDocumentCreate, db: DbSession, user: RequirePurchasing
):
    """Record a certificate. Its expiry is what the purchase-order gate reads."""
    obj = await _load_pq(db, user.tenant_id, pq_id)
    db.add(
        ComplianceDocument(
            tenant_id=user.tenant_id, prequalification_id=pq_id, **payload.model_dump()
        )
    )
    # A fresh certificate can bring an expired record back into compliance.
    if obj.status is PrequalStatus.EXPIRED and not obj.expired_documents:
        obj.status = PrequalStatus.APPROVED
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.delete("/{pq_id}/documents/{doc_id}", response_model=PrequalificationRead)
async def remove_compliance_document(
    pq_id: uuid.UUID, doc_id: uuid.UUID, db: DbSession, user: RequirePurchasing
):
    obj = await _load_pq(db, user.tenant_id, pq_id)
    doc = next((d for d in obj.documents if d.id == doc_id), None)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.delete(doc)
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


# --- lifecycle ----------------------------------------------------------------


@router.post("/{pq_id}/invite", response_model=PrequalificationRead)
async def invite_prequalification(
    pq_id: uuid.UUID, payload: InviteBody, db: DbSession, user: RequirePurchasing
):
    """Ask a trade to submit their pack. Moves a fresh (or lapsed) record into the queue."""
    obj = await _load_pq(db, user.tenant_id, pq_id)
    obj.status = PrequalStatus.INVITED
    if payload.review_due:
        obj.review_due = payload.review_due
    if payload.note:
        obj.notes = payload.note
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.post("/{pq_id}/submit", response_model=PrequalificationRead)
async def submit_prequalification(pq_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Record that the trade has returned their pack. It now waits for review."""
    obj = await _load_pq(db, user.tenant_id, pq_id)
    obj.status = PrequalStatus.SUBMITTED
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.post("/{pq_id}/start-review", response_model=PrequalificationRead)
async def start_review(pq_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    obj = await _load_pq(db, user.tenant_id, pq_id)
    obj.status = PrequalStatus.UNDER_REVIEW
    obj.reviewed_by_id = user.id
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.post("/{pq_id}/approve", response_model=PrequalificationRead)
async def approve_prequalification(
    pq_id: uuid.UUID, db: DbSession, user: RequirePurchasing, payload: ApprovePayload | None = None
):
    """Clear a trade for work. Refused while anything on file has lapsed. Approving
    conditionally records a cap or caveat but still clears the gate."""
    obj = await _load_pq(db, user.tenant_id, pq_id)
    payload = payload or ApprovePayload()

    expired = obj.expired_documents
    if expired:
        names = ", ".join(d.document_type.value.replace("_", " ") for d in expired)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve while documents have lapsed: {names}.",
        )

    obj.status = PrequalStatus.CONDITIONAL if payload.conditional else PrequalStatus.APPROVED
    if payload.single_job_limit is not None:
        obj.single_job_limit = payload.single_job_limit
    if payload.aggregate_limit is not None:
        obj.aggregate_limit = payload.aggregate_limit
    if payload.note:
        obj.notes = payload.note
    obj.reviewed_by_id = user.id
    obj.reviewed_on = date.today()
    if obj.review_due is None or obj.review_due < date.today():
        obj.review_due = date.today() + timedelta(days=REVIEW_PERIOD_DAYS)
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.post("/{pq_id}/reject", response_model=PrequalificationRead)
async def reject_prequalification(
    pq_id: uuid.UUID, db: DbSession, user: RequirePurchasing, reason: str | None = Query(None)
):
    obj = await _load_pq(db, user.tenant_id, pq_id)
    obj.status = PrequalStatus.REJECTED
    obj.reviewed_by_id = user.id
    obj.reviewed_on = date.today()
    if reason:
        obj.notes = reason
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


@router.post("/{pq_id}/renew-review", response_model=PrequalificationRead)
async def renew_review(pq_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Re-examine a record and push the annual review out a year. Refused while a
    document has lapsed — renewing the review does not paper over expired cover."""
    obj = await _load_pq(db, user.tenant_id, pq_id)
    if obj.expired_documents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A document has lapsed; renew the certificate before the review.",
        )
    obj.reviewed_by_id = user.id
    obj.reviewed_on = date.today()
    obj.review_due = date.today() + timedelta(days=REVIEW_PERIOD_DAYS)
    if obj.status is PrequalStatus.EXPIRED:
        obj.status = PrequalStatus.APPROVED
    await db.commit()
    return await _load_pq(db, user.tenant_id, pq_id)


# --- the gate, the chase list, the cover gaps ---------------------------------


@router.get("/check/{vendor_id}", response_model=ComplianceCheck)
async def check_compliance(vendor_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Is this trade clear to be given work today?"""
    result = await compliance_service.check_vendor(db, user.tenant_id, vendor_id)
    return ComplianceCheck(
        vendor_id=vendor_id,
        ok=result.ok,
        reason=result.reason,
        warning=result.warning,
        expired=result.expired,
        expiring=result.expiring,
    )


@router.get("/cover-gaps")
async def cover_gaps(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = Query(default=None, description="Limit to one home."),
):
    """Scheduled work that runs past the trade's insurance expiry.

    The purchase-order gate refuses at the point money moves, which is correct but late:
    by then the trade is booked and the schedule is built around them. This asks the same
    question from the schedule's side, far enough ahead that chasing a renewal is still an
    option.
    """
    return await compliance_service.cover_gaps_on_schedule(db, user.tenant_id, job_id)


@router.get("/expiring", response_model=list[ExpiringDocument])
async def expiring_documents(
    db: DbSession, user: RequireStaff, within_days: int = Query(30, ge=1, le=365)
):
    """Everything lapsing soon, soonest first — the chase list."""
    return await compliance_service.expiring_across_tenant(db, user.tenant_id, within_days)
