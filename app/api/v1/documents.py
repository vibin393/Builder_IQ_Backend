"""The document register: what exists, where it lives, and when it lapses.

Only metadata lives here; the bytes live in object storage under ``storage_key``. The
value the register adds over a shared drive is two things a folder cannot do: it flags
the contracts, permits, insurance and warranties that cost money when they cannot be
found, and it tracks the expiry dates on the ones that lapse — so a permit is renewed
before it stops work, not after. Newer versions **supersede** the old, which is kept as
the record of what was in force at the time.

  GET  /documents/summary            the register board, including what is lapsing
  POST /documents/{id}/supersede     register the replacement, keep the old as evidence
"""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import or_, select

from app.api.deps import DbSession, RequireStaff, get_tenant_scoped
from app.models.community import Community
from app.models.document import (
    Document,
    DocumentCategory,
    DocumentStatus,
)
from app.models.job import Job
from app.models.plan import Plan
from app.models.vendor import Vendor
from app.schemas.documents import (
    DocumentCreate,
    DocumentRead,
    DocumentSummary,
    DocumentSupersede,
    DocumentUpdate,
)

router = APIRouter(prefix="/documents", tags=["documents"])

#: Statuses kept off the default register view (still reachable by explicit filter).
_HIDDEN = (DocumentStatus.SUPERSEDED, DocumentStatus.ARCHIVED)


async def _load(db, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> Document:
    return await get_tenant_scoped(db, Document, tenant_id, doc_id)


async def _validate_links(db, tenant_id, payload) -> None:
    if getattr(payload, "job_id", None):
        await get_tenant_scoped(db, Job, tenant_id, payload.job_id)
    if getattr(payload, "community_id", None):
        await get_tenant_scoped(db, Community, tenant_id, payload.community_id)
    if getattr(payload, "plan_id", None):
        await get_tenant_scoped(db, Plan, tenant_id, payload.plan_id)
    if getattr(payload, "vendor_id", None):
        await get_tenant_scoped(db, Vendor, tenant_id, payload.vendor_id)


# --- analytics ----------------------------------------------------------------


@router.get("/summary", response_model=DocumentSummary)
async def summary(db: DbSession, user: RequireStaff, job_id: uuid.UUID | None = None):
    """The board: how many live documents, how many carry legal weight, and — the
    point of the register — how many are lapsing or have lapsed."""
    stmt = select(Document).where(Document.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Document.job_id == job_id)
    rows = list(await db.scalars(stmt))

    by_category: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = critical = untied = expiring = expired = 0
    for d in rows:
        by_status[d.status.value] = by_status.get(d.status.value, 0) + 1
        if not d.is_live:
            continue
        total += 1
        by_category[d.category.value] = by_category.get(d.category.value, 0) + 1
        if d.is_critical:
            critical += 1
        if d.job_id is None:
            untied += 1
        if d.is_expired:
            expired += 1
        elif d.is_expiring:
            expiring += 1

    return DocumentSummary(
        total=total,
        critical=critical,
        not_tied_to_home=untied,
        expiring_soon=expiring,
        expired=expired,
        categories_in_use=len(by_category),
        by_category=by_category,
        by_status=by_status,
    )


# --- list / register ----------------------------------------------------------


@router.get("", response_model=list[DocumentRead])
async def list_documents(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    community_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    category: DocumentCategory | None = None,
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
    critical_only: bool = False,
    expiring_only: bool = False,
    expired_only: bool = False,
    search: str | None = Query(default=None, max_length=200),
):
    stmt = select(Document).where(Document.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Document.job_id == job_id)
    if community_id is not None:
        stmt = stmt.where(Document.community_id == community_id)
    if plan_id is not None:
        stmt = stmt.where(Document.plan_id == plan_id)
    if vendor_id is not None:
        stmt = stmt.where(Document.vendor_id == vendor_id)
    if category is not None:
        stmt = stmt.where(Document.category == category)
    if status_filter is not None:
        stmt = stmt.where(Document.status == status_filter)
    else:
        # The register shows what is in force; history is reachable by asking for it.
        stmt = stmt.where(Document.status.notin_(_HIDDEN))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Document.name.ilike(like), Document.reference.ilike(like)))

    rows = list(await db.scalars(stmt.order_by(Document.created_at.desc())))
    if critical_only:
        rows = [d for d in rows if d.is_critical]
    if expiring_only:
        rows = [d for d in rows if d.is_expiring]
    if expired_only:
        rows = [d for d in rows if d.is_expired]
    return rows


@router.post("", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def register_document(payload: DocumentCreate, db: DbSession, user: RequireStaff):
    """Register document metadata. Bytes live in object storage under ``storage_key``."""
    await _validate_links(db, user.tenant_id, payload)
    doc = Document(tenant_id=user.tenant_id, uploaded_by_id=user.id, **payload.model_dump())
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.patch("/{doc_id}", response_model=DocumentRead)
async def update_document(
    doc_id: uuid.UUID, payload: DocumentUpdate, db: DbSession, user: RequireStaff
):
    doc = await _load(db, user.tenant_id, doc_id)
    if doc.status is DocumentStatus.SUPERSEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A superseded document is evidence; supersede the current one instead.",
        )
    await _validate_links(db, user.tenant_id, payload)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post(
    "/{doc_id}/supersede", response_model=DocumentRead, status_code=status.HTTP_201_CREATED
)
async def supersede_document(
    doc_id: uuid.UUID, payload: DocumentSupersede, db: DbSession, user: RequireStaff
):
    """Register the replacement version. The old one is kept and marked superseded, so
    what was in force at any point in time can still be produced. Attachments and links
    carry forward; anything not given on the new version is copied from the old."""
    old = await _load(db, user.tenant_id, doc_id)
    if old.status in _HIDDEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {old.status.value} document cannot be superseded.",
        )

    new = Document(
        tenant_id=user.tenant_id,
        uploaded_by_id=user.id,
        name=payload.name or old.name,
        category=old.category,
        storage_key=payload.storage_key,
        reference=payload.reference or old.reference,
        content_type=payload.content_type or old.content_type,
        size_bytes=payload.size_bytes if payload.size_bytes is not None else old.size_bytes,
        revision=old.revision + 1,
        issued_on=payload.issued_on or date.today(),
        expires_on=payload.expires_on,
        job_id=old.job_id,
        community_id=old.community_id,
        plan_id=old.plan_id,
        vendor_id=old.vendor_id,
        notes=payload.notes if payload.notes is not None else old.notes,
    )
    db.add(new)
    await db.flush()

    old.status = DocumentStatus.SUPERSEDED
    old.superseded_by_id = new.id
    await db.commit()
    await db.refresh(new)
    return new


@router.post("/{doc_id}/archive", response_model=DocumentRead)
async def archive_document(doc_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Withdraw a document from the live register — a mistaken registration, or one no
    longer relevant. Kept, not deleted, so nothing silently disappears from the record."""
    doc = await _load(db, user.tenant_id, doc_id)
    if doc.status is DocumentStatus.SUPERSEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A superseded document is already part of the record.",
        )
    doc.status = DocumentStatus.ARCHIVED
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/{doc_id}/restore", response_model=DocumentRead)
async def restore_document(doc_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Bring an archived document back onto the live register."""
    doc = await _load(db, user.tenant_id, doc_id)
    if doc.status is not DocumentStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only an archived document can be restored; this one is {doc.status.value}.",
        )
    doc.status = DocumentStatus.ACTIVE
    await db.commit()
    await db.refresh(doc)
    return doc
