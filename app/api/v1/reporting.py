"""Reporting and dashboards.

Every figure here is computed from the operational tables rather than stored, so a
report can never disagree with the records it summarises. That is slower at scale and
correct at every scale; add materialised rollups when a builder's volume demands it, not
before.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.api.deps import DbSession, RequireAdmin, RequireStaff, get_tenant_scoped
from app.models.budget import Budget, BudgetLine
from app.models.change_order import ChangeOrder, ChangeOrderLine, ResponsibleParty
from app.models.communication import RFI, BallInCourt, RFIStatus, Submittal, SubmittalStatus
from app.models.community import Community
from app.models.compliance import Prequalification
from app.models.contract import Contract, ContractStatus
from app.models.crm import Lead, LeadStatus
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceLine
from app.models.job import Job, JobStatus
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder, PurchaseOrderLine
from app.models.safety import Incident, Observation, ObservationStatus
from app.models.schedule import JobTask, TaskStatus
from app.models.vendor import Vendor
from app.models.warranty import ServiceOrder, WarrantyRequest, WarrantyStatus
from app.repositories.safety import OPEN_OBSERVATIONS
from app.services import scorecard as scorecard_service
from app.services.job_cost import PENDING_CO_STATUSES

router = APIRouter(prefix="/reports", tags=["reporting"])

ZERO = Decimal("0")

# A uuid no row can hold, so an empty community matches nothing rather than
# everything - `IN ()` is not valid SQL.
NO_JOB = uuid.UUID(int=0)


@router.get("/dashboard")
async def dashboard(
    db: DbSession,
    user: RequireStaff,
    community_id: uuid.UUID | None = Query(
        default=None, description="Scope every figure to one community."
    ),
):
    """Executive summary: sales, construction, money and warranty in one call.

    With `community_id`, every figure is scoped to that subdivision. A builder
    running four communities does not want one blended number - Maple Ridge
    being 12% over does not show up in a portfolio average that Oakfield is
    carrying.

    Scoping works by narrowing to that community's jobs, then filtering every
    aggregate through them. Records with no job at all - leads, which arrive
    before anyone has picked a lot - are left unscoped and say so.
    """
    t = user.tenant_id

    # The jobs in scope. None means every job in the tenant.
    scoped_job_ids: list[uuid.UUID] | None = None
    if community_id is not None:
        await get_tenant_scoped(db, Community, t, community_id)
        scoped_job_ids = list(
            await db.scalars(
                select(Job.id).where(Job.tenant_id == t, Job.community_id == community_id)
            )
        )

    def by_job(column):
        """Narrow an aggregate to the jobs in scope."""
        if scoped_job_ids is None:
            return []
        # An empty community has no jobs, so nothing should match.
        return [column.in_(scoped_job_ids)] if scoped_job_ids else [column.in_([NO_JOB])]

    async def count(model, *where):
        stmt = select(func.count()).select_from(model).where(model.tenant_id == t, *where)
        return (await db.scalar(stmt)) or 0

    # Sales
    leads_open = await count(Lead, Lead.status.notin_((LeadStatus.CONVERTED, LeadStatus.LOST)))
    leads_converted = await count(Lead, Lead.status == LeadStatus.CONVERTED)
    contracts_signed = await count(
        Contract, Contract.status == ContractStatus.SIGNED, *by_job(Contract.job_id)
    )

    sold_value = (
        await db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        Contract.base_price
                        + Contract.lot_premium
                        + Contract.options_total
                        - Contract.incentives
                    ),
                    0,
                )
            ).where(
                Contract.tenant_id == t,
                Contract.status == ContractStatus.SIGNED,
                *by_job(Contract.job_id),
            )
        )
        or ZERO
    )

    # Construction
    jobs_building = await count(
        Job, Job.status == JobStatus.UNDER_CONSTRUCTION, *by_job(Job.id)
    )
    jobs_closed = await count(Job, Job.status == JobStatus.CLOSED, *by_job(Job.id))
    jobs_total = await count(Job, *by_job(Job.id))

    overdue_tasks = await count(
        JobTask,
        JobTask.status != TaskStatus.COMPLETE,
        JobTask.scheduled_finish < date.today(),
        *by_job(JobTask.job_id),
    )

    # Money
    budget_total = (
        await db.scalar(
            select(func.coalesce(func.sum(BudgetLine.revised_amount), 0))
            .join(Budget, Budget.id == BudgetLine.budget_id)
            .where(BudgetLine.tenant_id == t, *by_job(Budget.job_id))
        )
        or ZERO
    )
    committed_total = (
        await db.scalar(
            select(func.coalesce(func.sum(PurchaseOrderLine.amount), 0))
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(
                PurchaseOrder.tenant_id == t,
                PurchaseOrder.status.in_(COMMITTED_STATUSES),
                *by_job(PurchaseOrder.job_id),
            )
        )
        or ZERO
    )
    actual_total = (
        await db.scalar(
            select(func.coalesce(func.sum(InvoiceLine.amount), 0))
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(
                Invoice.tenant_id == t,
                Invoice.status.in_(ACTUAL_STATUSES),
                *by_job(Invoice.job_id),
            )
        )
        or ZERO
    )

    # Warranty
    warranty_open = await count(
        WarrantyRequest,
        WarrantyRequest.status.notin_((WarrantyStatus.RESOLVED, WarrantyStatus.DECLINED)),
        *by_job(WarrantyRequest.job_id),
    )

    # Money at risk. `pending_change` is cost that is raised but undecided, so it
    # does not appear in `committed` - which is exactly why it is worth surfacing
    # here rather than leaving it to be discovered job by job. Trade-responsible
    # change orders are excluded: those are backcharged, not absorbed.
    pending_change = (
        await db.scalar(
            select(func.coalesce(func.sum(ChangeOrderLine.amount), 0))
            .join(ChangeOrder, ChangeOrder.id == ChangeOrderLine.change_order_id)
            .where(
                ChangeOrder.tenant_id == t,
                ChangeOrder.status.in_(PENDING_CO_STATUSES),
                ChangeOrder.responsible_party != ResponsibleParty.TRADE,
                *by_job(ChangeOrder.job_id),
            )
        )
        or ZERO
    )

    # Retainage is withheld payment, not reduced cost, so it is reported beside
    # `actual` rather than netted off it.
    held_invoices = list(
        await db.scalars(
            select(Invoice).where(
                Invoice.tenant_id == t,
                Invoice.status.in_(ACTUAL_STATUSES),
                Invoice.retainage_percent > 0,
                Invoice.retainage_released_date.is_(None),
                *by_job(Invoice.job_id),
            )
        )
    )
    retainage_held = sum((i.retainage_amount for i in held_invoices), ZERO)

    # Project controls
    rfis_open = await count(RFI, RFI.status == RFIStatus.OPEN, *by_job(RFI.job_id))
    rfis_ours = await count(
        RFI,
        RFI.status == RFIStatus.OPEN,
        RFI.ball_in_court == BallInCourt.BUILDER,
        *by_job(RFI.job_id),
    )
    rfis_overdue = await count(
        RFI, RFI.status == RFIStatus.OPEN, RFI.due_on < date.today(), *by_job(RFI.job_id)
    )
    rfis_blocking = await count(
        RFI, RFI.status == RFIStatus.OPEN, RFI.is_blocking.is_(True), *by_job(RFI.job_id)
    )
    submittals_awaiting = await count(
        Submittal,
        Submittal.status.in_((SubmittalStatus.SUBMITTED, SubmittalStatus.UNDER_REVIEW)),
    )

    # One definition of "open", shared with the Safety screen. Counting only
    # initiated and not-accepted left out the ones sent for review, so the
    # dashboard and the screen reported different numbers for the same word -
    # and a claimed fix nobody has looked at is not a closed one.
    observations_open = await count(
        Observation, Observation.status.in_(OPEN_OBSERVATIONS), *by_job(Observation.job_id)
    )
    # Overdue keeps its narrower set: something sitting with the builder for
    # review is late for the builder, not for the trade, and chasing the trade
    # over it is how the chase list stops being believed.
    observations_overdue = await count(
        Observation,
        Observation.status.in_(
            (ObservationStatus.INITIATED, ObservationStatus.NOT_ACCEPTED)
        ),
        Observation.due_on < date.today(),
        *by_job(Observation.job_id),
    )
    # Reportable incidents the HSE has not been told about, rather than every
    # reportable incident ever. The second number never goes down, so nobody
    # looks at it twice; this one is a list of things somebody has to do.
    incidents_reportable = await count(
        Incident,
        Incident.reportable.is_(True),
        Incident.reported_to_hse_on.is_(None),
        *by_job(Incident.job_id),
    )

    # Trades whose paperwork will stop a purchase order, or is about to.
    prequals = list(
        await db.scalars(select(Prequalification).where(Prequalification.tenant_id == t))
    )
    trades_blocked = sum(1 for p in prequals if not p.is_compliant)
    trades_expiring = sum(1 for p in prequals if p.is_compliant and p.expiring_soon)

    return {
        # What the figures cover, so the UI never has to guess.
        "scope": {
            "community_id": community_id,
            "jobs_in_scope": len(scoped_job_ids) if scoped_job_ids is not None else jobs_total,
            # Leads arrive before anyone has picked a lot, so they belong to no
            # community and stay portfolio-wide however the dashboard is scoped.
            "unscoped": ["open_leads", "converted_leads"] if community_id else [],
        },
        "sales": {
            "open_leads": leads_open,
            "converted_leads": leads_converted,
            "signed_contracts": contracts_signed,
            "sold_value": sold_value,
        },
        "construction": {
            "jobs_total": jobs_total,
            "under_construction": jobs_building,
            "closed": jobs_closed,
            "overdue_tasks": overdue_tasks,
        },
        "money": {
            "budget_revised": budget_total,
            "committed": committed_total,
            "actual": actual_total,
            "uncommitted": budget_total - committed_total,
            # Raised but undecided, and where the book lands if it all gets approved.
            "pending_change": pending_change,
            "forecast": committed_total + pending_change,
            "retainage_held": retainage_held,
        },
        "warranty": {"open_requests": warranty_open},
        "controls": {
            "rfis_open": rfis_open,
            "rfis_in_our_court": rfis_ours,
            "rfis_overdue": rfis_overdue,
            "rfis_blocking": rfis_blocking,
            "submittals_awaiting": submittals_awaiting,
            "observations_open": observations_open,
            "observations_overdue": observations_overdue,
            "incidents_reportable": incidents_reportable,
            "trades_blocked": trades_blocked,
            "trades_expiring": trades_expiring,
        },
    }


@router.get("/trade-scorecard")
async def trade_scorecard(
    db: DbSession,
    user: RequireStaff,
    vendor_id: uuid.UUID | None = Query(
        default=None, description="Score one trade instead of all of them."
    ),
    cost_code_id: uuid.UUID | None = Query(
        default=None, description="Only trades recorded as doing this cost code."
    ),
):
    """Whether a trade is worth re-hiring, across schedule, quality, warranty, safety,
    responsiveness, compliance and billing.

    Only possible because this system holds both halves: the job and the money
    from the homebuilder side, the observations, RFIs and prequalification from
    the project-controls side. Each dimension carries its own sample count, and
    dimensions with no history are excluded rather than scored zero.

    Every read records this month's score (earlier months stay frozen), so each row
    carries last month's overall and the change since.
    """
    from app.models.scorecard import ScorecardSnapshot
    from app.models.vendor import VendorSpecialism

    rows = await scorecard_service.scorecard(db, user.tenant_id, vendor_id)
    if cost_code_id is not None:
        doing = set(
            await db.scalars(
                select(VendorSpecialism.vendor_id).where(
                    VendorSpecialism.tenant_id == user.tenant_id,
                    VendorSpecialism.cost_code_id == cost_code_id,
                )
            )
        )
        rows = [r for r in rows if r["vendor_id"] in doing]

    this_month = date.today().replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    ids = [r["vendor_id"] for r in rows] or [uuid.uuid4()]
    snaps = {
        (s.vendor_id, s.period): s
        for s in await db.scalars(
            select(ScorecardSnapshot).where(
                ScorecardSnapshot.vendor_id.in_(ids),
                ScorecardSnapshot.period.in_((this_month, last_month)),
            )
        )
    }
    for r in rows:
        dims = {k: d["score"] for k, d in r["dimensions"].items()}
        current = snaps.get((r["vendor_id"], this_month))
        if current is None:
            db.add(
                ScorecardSnapshot(
                    tenant_id=user.tenant_id, vendor_id=r["vendor_id"], period=this_month,
                    overall=r["overall"], dimensions=dims,
                )
            )
        else:
            current.overall, current.dimensions = r["overall"], dims
        prev = snaps.get((r["vendor_id"], last_month))
        r["last_month"] = prev.overall if prev is not None else None
        r["change"] = (
            r["overall"] - prev.overall
            if prev is not None and prev.overall is not None and r["overall"] is not None
            else None
        )
    await db.commit()
    return rows


@router.get("/trade-scorecard/weights")
async def scorecard_weights(db: DbSession, user: RequireStaff):
    """How much each dimension counts, and the defaults they started from."""
    return {
        "weights": await scorecard_service.weights_for(db, user.tenant_id),
        "defaults": scorecard_service.WEIGHTS,
    }


@router.put("/trade-scorecard/weights")
async def set_scorecard_weights(payload: dict[str, int], db: DbSession, user: RequireAdmin):
    """Set the builder's own weights. Whole numbers, 0-100 each; they are normalised,
    so they need not add to 100 - but they cannot all be zero."""
    from fastapi import HTTPException

    from app.models.scorecard import ScorecardSettings

    unknown = set(payload) - set(scorecard_service.WEIGHTS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown dimension: {sorted(unknown)[0]}")
    if any(v < 0 or v > 100 for v in payload.values()):
        raise HTTPException(status_code=422, detail="Each weight is between 0 and 100.")
    merged = {**scorecard_service.WEIGHTS, **payload}
    if not any(merged.values()):
        raise HTTPException(status_code=422, detail="At least one dimension has to count.")
    row = await db.scalar(
        select(ScorecardSettings).where(ScorecardSettings.tenant_id == user.tenant_id)
    )
    if row is None:
        db.add(ScorecardSettings(tenant_id=user.tenant_id, weights=merged))
    else:
        row.weights = merged
    await db.commit()
    return {"weights": merged, "defaults": scorecard_service.WEIGHTS}


@router.get("/trade-scorecard/{vendor_id}/history")
async def scorecard_history(vendor_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Month by month, most recent first: overall and each dimension."""
    from app.models.scorecard import ScorecardSnapshot

    await get_tenant_scoped(db, Vendor, user.tenant_id, vendor_id)
    rows = await db.scalars(
        select(ScorecardSnapshot)
        .where(ScorecardSnapshot.vendor_id == vendor_id)
        .order_by(ScorecardSnapshot.period.desc())
        .limit(24)
    )
    return [
        {"period": r.period, "overall": r.overall, "dimensions": r.dimensions} for r in rows
    ]


@router.get("/sales-pipeline")
async def sales_pipeline(db: DbSession, user: RequireStaff):
    """Lead counts by status and by source - which marketing spend actually produces sales."""
    by_status = await db.execute(
        select(Lead.status, func.count())
        .where(Lead.tenant_id == user.tenant_id)
        .group_by(Lead.status)
    )
    by_source = await db.execute(
        select(
            Lead.source,
            func.count(),
            func.sum(case((Lead.status == LeadStatus.CONVERTED, 1), else_=0)),
        )
        .where(Lead.tenant_id == user.tenant_id)
        .group_by(Lead.source)
    )

    sources = []
    for source, total, converted in by_source:
        converted = converted or 0
        sources.append(
            {
                "source": source,
                "leads": total,
                "converted": converted,
                "conversion_rate": round(converted / total, 3) if total else 0.0,
            }
        )

    return {
        "by_status": [{"status": s, "count": c} for s, c in by_status],
        "by_source": sorted(sources, key=lambda r: -r["leads"]),
    }


@router.get("/job-margin")
async def job_margin(db: DbSession, user: RequireStaff, community_id=None):
    """Revenue vs cost per job - the report a builder runs before anything else.

    Cost uses exposure (the greater of committed and actual), so a job is never made to
    look better than it is by invoices lagging behind purchase orders.
    """
    t = user.tenant_id

    stmt = select(Job).where(Job.tenant_id == t)
    if community_id is not None:
        stmt = stmt.where(Job.community_id == community_id)
    jobs = list(await db.scalars(stmt.order_by(Job.job_number)))

    rows = []
    for job in jobs:
        contract = await db.scalar(select(Contract).where(Contract.job_id == job.id))
        revenue = contract.total_price if contract else ZERO

        budget = await db.scalar(select(Budget).where(Budget.job_id == job.id))
        budgeted = ZERO
        if budget is not None:
            budgeted = (
                await db.scalar(
                    select(func.coalesce(func.sum(BudgetLine.revised_amount), 0)).where(
                        BudgetLine.budget_id == budget.id
                    )
                )
                or ZERO
            )

        committed = (
            await db.scalar(
                select(func.coalesce(func.sum(PurchaseOrderLine.amount), 0))
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
                .where(
                    PurchaseOrder.job_id == job.id,
                    PurchaseOrder.status.in_(COMMITTED_STATUSES),
                )
            )
            or ZERO
        )
        actual = (
            await db.scalar(
                select(func.coalesce(func.sum(InvoiceLine.amount), 0))
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.job_id == job.id, Invoice.status.in_(ACTUAL_STATUSES))
            )
            or ZERO
        )

        exposure = max(committed, actual)
        margin = revenue - exposure

        rows.append(
            {
                "job_id": job.id,
                "job_number": job.job_number,
                "status": job.status,
                "revenue": revenue,
                "budget": budgeted,
                "committed": committed,
                "actual": actual,
                "exposure": exposure,
                "margin": margin,
                "margin_pct": round(float(margin / revenue), 4) if revenue else 0.0,
            }
        )

    return {
        "jobs": rows,
        "total_revenue": sum((r["revenue"] for r in rows), ZERO),
        "total_exposure": sum((r["exposure"] for r in rows), ZERO),
        "total_margin": sum((r["margin"] for r in rows), ZERO),
    }


@router.get("/cycle-time")
async def cycle_time(db: DbSession, user: RequireStaff):
    """Days from start to completion per finished job. Every day here is loan interest."""
    jobs = list(
        await db.scalars(
            select(Job).where(
                Job.tenant_id == user.tenant_id,
                Job.start_date.isnot(None),
                Job.actual_completion.isnot(None),
            )
        )
    )

    rows = [
        {
            "job_id": j.id,
            "job_number": j.job_number,
            "start_date": j.start_date,
            "actual_completion": j.actual_completion,
            "cycle_days": (j.actual_completion - j.start_date).days,
        }
        for j in jobs
    ]

    days = [r["cycle_days"] for r in rows]
    return {
        "jobs": rows,
        "completed_jobs": len(rows),
        "average_cycle_days": round(sum(days) / len(days), 1) if days else None,
        "fastest_days": min(days) if days else None,
        "slowest_days": max(days) if days else None,
    }


@router.get("/warranty-by-trade")
async def warranty_by_trade(
    db: DbSession,
    user: RequireStaff,
    days: int = Query(default=365, ge=1, description="Look back window."),
):
    """Which subcontractor generates the callbacks.

    This is the report that changes purchasing decisions, and it only exists because the
    same system holds construction and warranty against one job record.
    """
    since = date.today() - timedelta(days=days)

    rows = await db.execute(
        select(Vendor.code, Vendor.name, func.count(ServiceOrder.id))
        .join(ServiceOrder, ServiceOrder.vendor_id == Vendor.id)
        .join(WarrantyRequest, WarrantyRequest.id == ServiceOrder.request_id)
        .where(
            ServiceOrder.tenant_id == user.tenant_id,
            WarrantyRequest.submitted_date >= since,
        )
        .group_by(Vendor.code, Vendor.name)
        .order_by(func.count(ServiceOrder.id).desc())
    )

    trades = [{"vendor_code": c, "vendor_name": n, "service_orders": k} for c, n, k in rows]
    total = sum(t["service_orders"] for t in trades)

    for t_ in trades:
        t_["share"] = round(t_["service_orders"] / total, 3) if total else 0.0

    return {"since": since, "total_service_orders": total, "trades": trades}


@router.get("/communities")
async def community_summary(db: DbSession, user: RequireStaff):
    """Lot and job counts per community - availability and absorption at a glance."""
    communities = list(
        await db.scalars(
            select(Community).where(Community.tenant_id == user.tenant_id).order_by(Community.name)
        )
    )

    out = []
    for c in communities:
        # Cancelled homes are not homes. And "sold" means a buyer: this used to count
        # every home past planning, so a speculative home under construction with
        # nobody behind it showed as sold.
        jobs = await db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.community_id == c.id, Job.status != JobStatus.CANCELLED)
        )
        sold = await db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.community_id == c.id,
                Job.status != JobStatus.CANCELLED,
                Job.sold_date.isnot(None),
            )
        )
        out.append(
            {
                "community_id": c.id,
                "code": c.code,
                "name": c.name,
                "jobs": jobs or 0,
                "sold_or_building": sold or 0,
            }
        )
    return {"communities": out}
