"""Trade scorecard: one view of whether a subcontractor is worth re-hiring.

This is the feature neither source product can build on its own.

BuildTopia knows a trade's schedule performance and what they were paid, because
it owns the job and the money. Procore knows their safety record, their RFI
responsiveness and whether their paperwork is current, because it owns the project
controls. Neither knows both, so neither can answer the question a builder
actually asks before awarding the next house: *should we use these people again?*

Seven dimensions, each scored 0-100 from records the system already holds:

    schedule        did they finish tasks on the day they said
    quality         punch items raised against them, and whether they cleared them
    warranty        call-backs per home handed over - the defects the buyer found
    safety          observations owed, and incidents they were involved in
    responsiveness  how long RFIs sit with them
    compliance      is their paperwork current enough to issue a PO today
    billing         do they invoice what the purchase order said

The weights are the builder's to set (ScorecardSettings); the defaults are below.

Two deliberate constraints:

  * every dimension reports its own sample count, and a trade with almost no
    history is marked `provisional` rather than being given a confident-looking
    number. A score built on two tasks is noise wearing a suit.
  * dimensions with no data are excluded from the composite rather than scored
    zero. A trade nobody has raised a punch item against has not failed quality;
    it has no quality history, which is a different statement.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.communication import RFI, RFIStatus
from app.models.compliance import Prequalification
from app.models.field import PunchItem, PunchStatus
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceLine
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder, PurchaseOrderLine
from app.models.safety import Incident, Observation, ObservationStatus
from app.models.schedule import JobTask, TaskStatus
from app.models.vendor import Vendor

ZERO = Decimal("0")

# Below this many measured events a dimension is reported but not trusted.
MIN_SAMPLE = 3

# What each dimension is worth in the composite, by default. Schedule and quality
# dominate because they are what a builder feels weekly; warranty call-backs are what
# the buyer feels; compliance is a gate elsewhere (it blocks purchase orders outright)
# so it carries less weight here. A builder can set their own.
WEIGHTS = {
    "schedule": 25,
    "quality": 20,
    "warranty": 15,
    "safety": 20,
    "responsiveness": 8,
    "compliance": 7,
    "billing": 5,
}
DIMENSIONS = tuple(WEIGHTS)


def tier(overall: int | None) -> str | None:
    """A-D, and what it means for the next award."""
    if overall is None:
        return None
    return "A" if overall >= 80 else "B" if overall >= 65 else "C" if overall >= 50 else "D"


async def weights_for(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    from app.models.scorecard import ScorecardSettings

    row = await db.scalar(
        select(ScorecardSettings).where(ScorecardSettings.tenant_id == tenant_id)
    )
    merged = dict(WEIGHTS)
    if row is not None:
        merged.update({k: int(v) for k, v in row.weights.items() if k in WEIGHTS})
    return merged

OPEN_OBSERVATIONS = (ObservationStatus.INITIATED, ObservationStatus.NOT_ACCEPTED)


@dataclass
class Dimension:
    """One measured axis: a score, how much evidence it rests on, and why."""

    score: int | None          # None = no history, excluded from the composite
    sample: int
    detail: str
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def provisional(self) -> bool:
        return self.score is not None and self.sample < MIN_SAMPLE


def _clamp(value: float) -> int:
    return int(max(0, min(100, round(value))))


async def scorecard(
    db: AsyncSession, tenant_id: uuid.UUID, vendor_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """Score every trade, or one of them, with the builder's weights."""
    weights = await weights_for(db, tenant_id)
    vendor_stmt = select(Vendor).where(Vendor.tenant_id == tenant_id)
    if vendor_id:
        vendor_stmt = vendor_stmt.where(Vendor.id == vendor_id)
    vendors = list(await db.scalars(vendor_stmt.order_by(Vendor.name)))
    if not vendors:
        return []

    ids = [v.id for v in vendors]

    # --- schedule ----------------------------------------------------------
    tasks = list(
        await db.scalars(
            select(JobTask).where(
                JobTask.tenant_id == tenant_id,
                JobTask.vendor_id.in_(ids),
                JobTask.status == TaskStatus.COMPLETE,
                JobTask.actual_finish.is_not(None),
                JobTask.scheduled_finish.is_not(None),
            )
        )
    )

    # --- quality -----------------------------------------------------------
    punch = list(
        await db.scalars(
            select(PunchItem).where(
                PunchItem.tenant_id == tenant_id, PunchItem.vendor_id.in_(ids)
            )
        )
    )

    # --- safety ------------------------------------------------------------
    observations = list(
        await db.scalars(
            select(Observation).where(
                Observation.tenant_id == tenant_id,
                Observation.assigned_vendor_id.in_(ids),
            )
        )
    )
    incidents = list(
        await db.scalars(
            select(Incident).where(Incident.tenant_id == tenant_id, Incident.vendor_id.in_(ids))
        )
    )

    # --- responsiveness ----------------------------------------------------
    rfis = list(
        await db.scalars(
            select(RFI).where(RFI.tenant_id == tenant_id, RFI.assigned_vendor_id.in_(ids))
        )
    )

    # --- compliance --------------------------------------------------------
    prequals = {
        p.vendor_id: p
        for p in await db.scalars(
            select(Prequalification)
            .options(selectinload(Prequalification.documents))
            .where(Prequalification.tenant_id == tenant_id, Prequalification.vendor_id.in_(ids))
        )
    }

    # --- billing -----------------------------------------------------------
    committed_rows = await db.execute(
        select(PurchaseOrder.vendor_id, func.coalesce(func.sum(PurchaseOrderLine.amount), 0))
        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.vendor_id.in_(ids),
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
        )
        .group_by(PurchaseOrder.vendor_id)
    )
    committed = {vid: total or ZERO for vid, total in committed_rows}

    invoiced_rows = await db.execute(
        select(Invoice.vendor_id, func.coalesce(func.sum(InvoiceLine.amount), 0))
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.vendor_id.in_(ids),
            Invoice.status.in_(ACTUAL_STATUSES),
        )
        .group_by(Invoice.vendor_id)
    )
    invoiced = {vid: total or ZERO for vid, total in invoiced_rows}

    # --- warranty ---------------------------------------------------------
    # Homes they worked on that have been handed over, and the call-backs raised
    # against them. Per home, so a trade on forty houses is not punished for having
    # more call-backs in total than one on four.
    from app.models.job import Job, JobStatus
    from app.models.warranty import ServiceOrder, ServiceOrderStatus

    handed_over = {JobStatus.CLOSED, JobStatus.WARRANTY}
    worked: dict[uuid.UUID, set] = {}
    for po in await db.scalars(
        select(PurchaseOrder).where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.vendor_id.in_(ids),
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
        )
    ):
        worked.setdefault(po.vendor_id, set()).add(po.job_id)
    done_jobs = {
        j.id
        for j in await db.scalars(
            select(Job).where(
                Job.tenant_id == tenant_id,
                Job.id.in_({jid for js in worked.values() for jid in js} or {uuid.uuid4()}),
                Job.status.in_(handed_over),
            )
        )
    }
    callbacks = list(
        await db.scalars(
            select(ServiceOrder).where(
                ServiceOrder.tenant_id == tenant_id,
                ServiceOrder.vendor_id.in_(ids),
                ServiceOrder.status != ServiceOrderStatus.CANCELLED,
            )
        )
    )

    today = date.today()
    out: list[dict[str, object]] = []

    for v in vendors:
        dims: dict[str, Dimension] = {}

        # ---- schedule: on-time completions, and how late the late ones were
        mine = [t for t in tasks if t.vendor_id == v.id]
        if mine:
            on_time = sum(1 for t in mine if t.days_late <= 0)
            avg_late = sum(max(t.days_late, 0) for t in mine) / len(mine)
            pct = on_time / len(mine) * 100
            # Each average day late costs five points, capped: a trade that is
            # reliably two days late is not the same as one that vanishes.
            dims["schedule"] = Dimension(
                _clamp(pct - min(avg_late * 5, 40)),
                len(mine),
                f"{on_time} of {len(mine)} tasks finished on time"
                + (f", averaging {avg_late:.1f} days late" if avg_late > 0 else ""),
                {"tasks": len(mine), "on_time": on_time, "avg_days_late": round(avg_late, 1)},
            )
        else:
            dims["schedule"] = Dimension(None, 0, "no completed tasks yet")

        # ---- quality: punch items are the direct measure of rework
        pmine = [p for p in punch if p.vendor_id == v.id]
        if pmine:
            closed = sum(1 for p in pmine if p.status is PunchStatus.COMPLETE)
            overdue = sum(
                1
                for p in pmine
                if p.status is not PunchStatus.COMPLETE and p.due_date and p.due_date < today
            )
            walkthrough = sum(1 for p in pmine if p.from_walkthrough)
            # Start from how much they cleared, then penalise what is overdue and
            # what the buyer had to find for them.
            score = (closed / len(pmine)) * 100 - overdue * 12 - walkthrough * 6
            dims["quality"] = Dimension(
                _clamp(score),
                len(pmine),
                f"{closed} of {len(pmine)} punch items cleared"
                + (f", {overdue} overdue" if overdue else "")
                + (f", {walkthrough} found by the buyer" if walkthrough else ""),
                {"items": len(pmine), "closed": closed, "overdue": overdue,
                 "from_walkthrough": walkthrough},
            )
        else:
            dims["quality"] = Dimension(None, 0, "no punch items raised against them")

        # ---- warranty: call-backs per home handed over
        homes = len(worked.get(v.id, set()) & done_jobs)
        cmine = [c for c in callbacks if c.vendor_id == v.id]
        if homes or cmine:
            open_late = sum(
                1 for c in cmine
                if c.status is not ServiceOrderStatus.COMPLETE
                and c.scheduled_date and c.scheduled_date < today
            )
            per_home = len(cmine) / homes if homes else float(len(cmine))
            # Half a call-back per home is ordinary; each whole one costs forty points.
            dims["warranty"] = Dimension(
                _clamp(100 - max(per_home - 0.5, 0) * 40 - open_late * 10),
                max(homes, len(cmine)),
                f"{len(cmine)} call-back{'s' if len(cmine) != 1 else ''} on "
                f"{homes} home{'s' if homes != 1 else ''} handed over"
                + (f", {open_late} late to fix" if open_late else ""),
                {"homes": homes, "callbacks": len(cmine), "per_home": round(per_home, 2),
                 "late": open_late},
            )
        else:
            dims["warranty"] = Dimension(None, 0, "no homes handed over yet")

        # ---- safety: observations owed, plus any incident they were part of
        omine = [o for o in observations if o.assigned_vendor_id == v.id]
        imine = [i for i in incidents if i.vendor_id == v.id]
        sample = len(omine) + len(imine)
        if sample:
            open_overdue = sum(
                1 for o in omine if o.status in OPEN_OBSERVATIONS and o.is_overdue
            )
            critical = sum(
                1 for o in omine if o.severity.value == "critical" and o.status in OPEN_OBSERVATIONS
            )
            reportable = sum(1 for i in imine if i.reportable)
            lost_time = sum(1 for i in imine if i.lost_time)
            score = 100 - open_overdue * 15 - critical * 20 - reportable * 25 - lost_time * 15
            bits = []
            if omine:
                bits.append(f"{len(omine)} observation{'s' if len(omine) != 1 else ''}")
            if open_overdue:
                bits.append(f"{open_overdue} overdue")
            if reportable:
                bits.append(f"{reportable} reportable incident{'s' if reportable != 1 else ''}")
            dims["safety"] = Dimension(
                _clamp(score), sample, ", ".join(bits) or "clean",
                {"observations": len(omine), "overdue": open_overdue,
                 "incidents": len(imine), "reportable": reportable},
            )
        else:
            dims["safety"] = Dimension(None, 0, "nothing on record")

        # ---- responsiveness: how long questions sit with them
        rmine = [r for r in rfis if r.assigned_vendor_id == v.id]
        answered = [r for r in rmine if r.days_open is not None]
        if answered:
            avg_days = sum(r.days_open or 0 for r in answered) / len(answered)
            still_open = sum(1 for r in rmine if r.status is RFIStatus.OPEN)
            overdue = sum(1 for r in rmine if r.is_overdue)
            # A week is par. Every day beyond that costs eight points.
            dims["responsiveness"] = Dimension(
                _clamp(100 - max(avg_days - 7, 0) * 8 - overdue * 10),
                len(rmine),
                f"{avg_days:.0f} days average on {len(rmine)} RFI"
                f"{'s' if len(rmine) != 1 else ''}"
                + (f", {overdue} overdue" if overdue else ""),
                {"rfis": len(rmine), "avg_days_open": round(avg_days, 1),
                 "still_open": still_open, "overdue": overdue},
            )
        else:
            dims["responsiveness"] = Dimension(None, 0, "no RFIs assigned to them")

        # ---- compliance: the same check the purchase-order gate runs
        pq = prequals.get(v.id)
        if pq is None:
            dims["compliance"] = Dimension(
                None, 0, "no prequalification record",
                {"status": None, "compliant": None},
            )
        else:
            expiring = len(pq.expiring_soon)
            expired = len(pq.expired_documents)
            if not pq.is_compliant:
                score = 0
                if expired:
                    # Approved months ago, lapsed since: say so, rather than
                    # printing "blocked: approved", which reads as a bug.
                    detail = f"blocked: {expired} lapsed document{'s' if expired != 1 else ''}"
                else:
                    detail = f"blocked: prequalification is {pq.status.value.replace('_', ' ')}"
            else:
                score = 100 - expiring * 20
                detail = "current" + (
                    f", {expiring} document{'s' if expiring != 1 else ''} lapsing soon"
                    if expiring else ""
                )
            dims["compliance"] = Dimension(
                _clamp(score), 1, detail,
                {"status": pq.status.value, "compliant": pq.is_compliant,
                 "expiring": expiring, "expired": expired},
            )

        # ---- billing: do they invoice what the PO said
        com = committed.get(v.id, ZERO)
        inv = invoiced.get(v.id, ZERO)
        if com > 0:
            ratio = float(inv / com)
            # Under-billing is not a fault - work simply is not finished. Only
            # billing *above* the commitment is scored against them.
            over = max(ratio - 1.0, 0.0)
            if over > 0:
                detail = f"invoiced {ratio * 100:.0f}% of committed - billing above the order"
            elif ratio < 0.999:
                detail = (
                    f"invoiced {ratio * 100:.0f}% of committed - the rest is unbilled work, "
                    "which is not scored against them"
                )
            else:
                detail = "invoiced exactly what was committed"
            dims["billing"] = Dimension(
                _clamp(100 - over * 200),
                1,
                detail,
                {"committed": str(com), "invoiced": str(inv), "ratio": round(ratio, 3)},
            )
        else:
            dims["billing"] = Dimension(None, 0, "nothing committed yet")

        # ---- composite: weighted over the dimensions that actually have data
        scored = {k: d for k, d in dims.items() if d.score is not None}
        if scored:
            weight_sum = sum(weights[k] for k in scored)
            overall = (
                round(sum(dims[k].score * weights[k] for k in scored) / weight_sum)
                if weight_sum
                else None
            )
        else:
            overall = None

        evidence = sum(d.sample for d in dims.values())
        out.append({
            "vendor_id": v.id,
            "vendor_name": v.name,
            "overall": overall,
            "tier": tier(overall),
            # True when there is too little history to lean on the number.
            "provisional": overall is not None and evidence < MIN_SAMPLE * 2,
            "evidence": evidence,
            "dimensions": {
                k: {
                    "score": d.score,
                    "sample": d.sample,
                    "detail": d.detail,
                    "provisional": d.provisional,
                    "facts": d.facts,
                }
                for k, d in dims.items()
            },
        })

    # Best first, but anything unscored sinks to the bottom rather than topping
    # the list on a null.
    out.sort(key=lambda r: (r["overall"] is None, -(r["overall"] or 0)))
    return out
