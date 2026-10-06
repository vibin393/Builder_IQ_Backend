"""The compliance gate.

Prequalification records are only worth keeping if something checks them. This is
what the purchase-order flow calls before it commits money to a trade.

The rule: a trade must be approved, and nothing on file may have lapsed, before
the builder can issue them work. Like the budget guard, it refuses by default and
allows a recorded override — blocking people outright just means they work around
the system, and then the record is worthless.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.compliance import Prequalification, PrequalStatus
from app.models.vendor import Vendor


class ComplianceResult:
    """Why a trade is or is not clear to be given work."""

    def __init__(
        self,
        ok: bool,
        reason: str | None = None,
        expired: list[str] | None = None,
        expiring: list[str] | None = None,
        warning: str | None = None,
    ):
        self.ok = ok
        self.reason = reason
        self.expired = expired or []
        self.expiring = expiring or []
        # Allowed, but something is worth saying out loud.
        self.warning = warning

    def as_detail(self, vendor_name: str) -> str:
        parts = [f"{vendor_name} is not cleared for work: {self.reason}."]
        if self.expired:
            parts.append("Lapsed: " + ", ".join(self.expired) + ".")
        return " ".join(parts)


async def check_vendor(
    db: AsyncSession, tenant_id: uuid.UUID, vendor_id: uuid.UUID
) -> ComplianceResult:
    """Is this trade clear to hold a purchase order today?"""
    prequal = await db.scalar(
        select(Prequalification)
        .options(selectinload(Prequalification.documents))
        .where(
            Prequalification.vendor_id == vendor_id,
            Prequalification.tenant_id == tenant_id,
        )
    )

    # No record at all is a warning, not a refusal.
    #
    # A builder switching this module on has sixty existing trades and no packs
    # on file. Blocking every purchase order on day one guarantees the module
    # gets turned off again. The gate refuses what it has actually checked and
    # found wanting - a rejection, a lapsed certificate, an overdue review -
    # and flags what it has not seen at all.
    if prequal is None:
        return ComplianceResult(
            ok=True,
            warning="no prequalification record exists for this trade",
        )

    if prequal.status is PrequalStatus.REJECTED:
        return ComplianceResult(ok=False, reason="prequalification was rejected")

    if prequal.status not in (PrequalStatus.APPROVED, PrequalStatus.CONDITIONAL):
        return ComplianceResult(
            ok=False, reason=f"prequalification is {prequal.status.value.replace('_', ' ')}"
        )

    expired = [
        f"{d.document_type.value.replace('_', ' ')} (expired {d.expires_on})"
        for d in prequal.expired_documents
    ]
    if expired:
        return ComplianceResult(
            ok=False,
            reason=f"{len(expired)} document(s) have lapsed",
            expired=expired,
        )

    if prequal.review_due and prequal.review_due < date.today():
        return ComplianceResult(
            ok=False, reason=f"annual review was due {prequal.review_due}"
        )

    expiring = [
        f"{d.document_type.value.replace('_', ' ')} (expires {d.expires_on})"
        for d in prequal.expiring_soon
    ]
    return ComplianceResult(ok=True, expiring=expiring)


async def expiring_across_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, within_days: int = 30
) -> list[dict[str, object]]:
    """Everything lapsing soon, so someone can chase it before work stops.

    Sorted by how little time is left, because that is the order to work in.
    """
    horizon = date.today() + timedelta(days=within_days)

    rows = await db.scalars(
        select(Prequalification)
        .options(selectinload(Prequalification.documents))
        .where(Prequalification.tenant_id == tenant_id)
    )
    vendors = {
        v.id: v for v in await db.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id))
    }

    out: list[dict[str, object]] = []
    for prequal in rows:
        vendor = vendors.get(prequal.vendor_id)
        for doc in prequal.documents:
            if doc.expires_on and doc.expires_on <= horizon:
                out.append(
                    {
                        "vendor_id": prequal.vendor_id,
                        "vendor_name": vendor.name if vendor else "Unknown",
                        "vendor_code": vendor.code if vendor else "?",
                        "document_type": doc.document_type.value,
                        "reference": doc.reference,
                        "expires_on": doc.expires_on,
                        "days_remaining": doc.days_remaining,
                        "is_expired": doc.is_expired,
                    }
                )

    out.sort(key=lambda r: r["expires_on"])
    return out


async def refresh_expired_status(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Move approved records with lapsed documents to EXPIRED.

    Run on a schedule. Without it a record stays green in the list view while the
    gate refuses it, which reads as a bug rather than a lapsed certificate.
    """
    rows = list(
        await db.scalars(
            select(Prequalification)
            .options(selectinload(Prequalification.documents))
            .where(
                Prequalification.tenant_id == tenant_id,
                Prequalification.status.in_(
                    (PrequalStatus.APPROVED, PrequalStatus.CONDITIONAL)
                ),
            )
        )
    )

    changed = 0
    for prequal in rows:
        if prequal.expired_documents:
            prequal.status = PrequalStatus.EXPIRED
            changed += 1

    if changed:
        await db.flush()
    return changed


async def cover_gaps_on_schedule(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """Tasks booked to a trade whose cover runs out before the work finishes.

    The compliance gate refuses a purchase order at the moment it is issued,
    which is the right place to stop money. It is the wrong place to find out:
    by then the trade is booked, the schedule is built around them, and the
    choice is to delay or to override.

    This looks the other way down the same relationship - from the schedule
    forward to the certificate - and reports the tasks that will run past a
    lapse. It is the question neither source product can ask, because one holds
    the schedule and the other holds the certificate.
    """
    from app.models.schedule import JobTask, TaskStatus

    stmt = select(JobTask).where(
        JobTask.tenant_id == tenant_id,
        JobTask.vendor_id.is_not(None),
        JobTask.status != TaskStatus.COMPLETE,
        JobTask.scheduled_finish.is_not(None),
    )
    if job_id:
        stmt = stmt.where(JobTask.job_id == job_id)
    tasks = list(await db.scalars(stmt))
    if not tasks:
        return []

    vendor_ids = {t.vendor_id for t in tasks}
    prequals = {
        p.vendor_id: p
        for p in await db.scalars(
            select(Prequalification)
            .options(selectinload(Prequalification.documents))
            .where(
                Prequalification.tenant_id == tenant_id,
                Prequalification.vendor_id.in_(vendor_ids),
            )
        )
    }
    vendors = {
        v.id: v
        for v in await db.scalars(
            select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.id.in_(vendor_ids))
        )
    }

    gaps: list[dict[str, object]] = []
    for task in tasks:
        prequal = prequals.get(task.vendor_id)
        if prequal is None:
            # No record at all is the compliance module's problem, not the
            # schedule's. Flagging it here too would just be noise.
            continue

        for doc in prequal.documents:
            if doc.expires_on is None or doc.expires_on >= task.scheduled_finish:
                continue
            gaps.append({
                "task_id": task.id,
                "task_name": task.name,
                "job_id": task.job_id,
                "scheduled_start": task.scheduled_start,
                "scheduled_finish": task.scheduled_finish,
                "vendor_id": task.vendor_id,
                "vendor_name": vendors[task.vendor_id].name if task.vendor_id in vendors else "?",
                "document_type": doc.document_type.value,
                "expires_on": doc.expires_on,
                "days_uncovered": (task.scheduled_finish - doc.expires_on).days,
                "already_lapsed": doc.is_expired,
            })

    # Worst first: already lapsed, then the longest stretch of uncovered work.
    gaps.sort(key=lambda g: (not g["already_lapsed"], -int(g["days_uncovered"])))
    return gaps
