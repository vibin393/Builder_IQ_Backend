"""Job costing: budget vs committed vs actual, per cost code.

This is the number the CFO buys the system for. It is always derived, never stored, so it
cannot drift away from the budget lines, POs and invoices it summarises.

    revised   what the house is budgeted to cost, after selections and change orders
    committed what has been promised to vendors on issued POs
    actual    what has been invoiced and approved
    exposure  max(committed, actual) - what the job is really on the hook for
    pending   cost on change orders raised but not yet approved
    forecast  exposure + pending - where the job lands if everything in flight lands

`forecast` is the number that lets a superintendent act. `exposure` only moves when
a change order is approved, which is after the decision has been made; a cost that is
visibly coming should be visible before it is committed, not after.

Retainage is tracked alongside but never reduces `actual`. The work was done and the
cost is real; what is withheld is the payment.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget, BudgetLine
from app.models.change_order import (
    ChangeOrder,
    ChangeOrderLine,
    ChangeOrderStatus,
    ResponsibleParty,
)
from app.models.cost_code import CostCode
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceLine
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder, PurchaseOrderLine

# Raised, costed, but not yet decided. Not committed money - but money the job can
# see coming.
PENDING_CO_STATUSES = (ChangeOrderStatus.DRAFT, ChangeOrderStatus.PENDING_APPROVAL)

ZERO = Decimal("0")


async def committed_by_cost_code(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Sum of issued PO lines per cost code. Draft and void POs are not commitments."""
    rows = await db.execute(
        select(PurchaseOrderLine.cost_code_id, func.sum(PurchaseOrderLine.amount))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(
            PurchaseOrder.job_id == job_id,
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
        )
        .group_by(PurchaseOrderLine.cost_code_id)
    )
    return {code_id: total or ZERO for code_id, total in rows}


async def actual_by_cost_code(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Sum of approved and paid invoice lines per cost code."""
    rows = await db.execute(
        select(InvoiceLine.cost_code_id, func.sum(InvoiceLine.amount))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            Invoice.job_id == job_id,
            Invoice.tenant_id == tenant_id,
            Invoice.status.in_(ACTUAL_STATUSES),
        )
        .group_by(InvoiceLine.cost_code_id)
    )
    return {code_id: total or ZERO for code_id, total in rows}


async def pending_change_by_cost_code(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Cost on change orders that are raised but not yet approved.

    Deliberately excludes trade-responsible change orders: those are backcharged,
    so they should never raise the builder's expected cost.
    """
    rows = await db.execute(
        select(ChangeOrderLine.cost_code_id, func.sum(ChangeOrderLine.amount))
        .join(ChangeOrder, ChangeOrder.id == ChangeOrderLine.change_order_id)
        .where(
            ChangeOrder.job_id == job_id,
            ChangeOrder.tenant_id == tenant_id,
            ChangeOrder.status.in_(PENDING_CO_STATUSES),
            ChangeOrder.responsible_party != ResponsibleParty.TRADE,
        )
        .group_by(ChangeOrderLine.cost_code_id)
    )
    return {code_id: total or ZERO for code_id, total in rows}


async def retainage_totals(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[Decimal, Decimal]:
    """(held, released) across the job's approved and paid invoices."""
    invoices = list(
        await db.scalars(
            select(Invoice).where(
                Invoice.job_id == job_id,
                Invoice.tenant_id == tenant_id,
                Invoice.status.in_(ACTUAL_STATUSES),
            )
        )
    )
    held = sum((i.retainage_amount for i in invoices), ZERO)
    released = sum(
        (
            (i.total * i.retainage_percent / Decimal("100")).quantize(Decimal("0.01"))
            for i in invoices
            if i.retainage_released_date is not None
        ),
        ZERO,
    )
    return held, released


async def job_cost_summary(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, object]:
    """Per-cost-code and whole-job rollup of budget, commitment, actual and exposure."""
    budget = await db.scalar(
        select(Budget).where(Budget.job_id == job_id, Budget.tenant_id == tenant_id)
    )

    lines: list[BudgetLine] = []
    if budget is not None:
        lines = list(
            await db.scalars(
                select(BudgetLine).where(
                    BudgetLine.budget_id == budget.id, BudgetLine.tenant_id == tenant_id
                )
            )
        )

    committed = await committed_by_cost_code(db, tenant_id, job_id)
    actual = await actual_by_cost_code(db, tenant_id, job_id)
    pending = await pending_change_by_cost_code(db, tenant_id, job_id)
    retained, released = await retainage_totals(db, tenant_id, job_id)

    code_ids = (
        {line.cost_code_id for line in lines} | set(committed) | set(actual) | set(pending)
    )
    codes = (
        {c.id: c for c in await db.scalars(select(CostCode).where(CostCode.id.in_(code_ids)))}
        if code_ids
        else {}
    )

    by_code: dict[uuid.UUID, BudgetLine] = {line.cost_code_id: line for line in lines}
    rows: list[dict[str, object]] = []

    for code_id in code_ids:
        line = by_code.get(code_id)
        original = line.original_amount if line else ZERO
        revised = line.revised_amount if line else ZERO
        com = committed.get(code_id, ZERO)
        act = actual.get(code_id, ZERO)
        pend = pending.get(code_id, ZERO)
        code = codes.get(code_id)
        exposure = max(com, act)

        rows.append(
            {
                "cost_code_id": code_id,
                "code": code.code if code else "?",
                "name": code.name if code else "Unknown cost code",
                "original_amount": original,
                "revised_amount": revised,
                "committed_amount": com,
                "actual_amount": act,
                # Positive = still to commit. Negative = committed over budget.
                "uncommitted": revised - com,
                # What the job is really exposed to: an invoice above its PO still counts.
                "exposure": exposure,
                "variance": revised - exposure,
                # Raised but undecided, and where the code lands if it is approved.
                "pending_change": pend,
                "forecast": exposure + pend,
                "forecast_variance": revised - (exposure + pend),
            }
        )

    rows.sort(key=lambda r: str(r["code"]))

    original_total = sum((r["original_amount"] for r in rows), ZERO)
    revised_total = sum((r["revised_amount"] for r in rows), ZERO)
    committed_total = sum((r["committed_amount"] for r in rows), ZERO)
    actual_total = sum((r["actual_amount"] for r in rows), ZERO)
    exposure_total = sum((r["exposure"] for r in rows), ZERO)
    pending_total = sum((r["pending_change"] for r in rows), ZERO)
    forecast_total = exposure_total + pending_total

    return {
        "job_id": job_id,
        "has_budget": budget is not None,
        "original_total": original_total,
        "revised_total": revised_total,
        "committed_total": committed_total,
        "actual_total": actual_total,
        "exposure_total": exposure_total,
        "uncommitted_total": revised_total - committed_total,
        "variance_total": revised_total - exposure_total,
        "over_budget": exposure_total > revised_total,
        # Pending change orders: visible before they are committed, not after.
        "pending_change_total": pending_total,
        "forecast_total": forecast_total,
        "forecast_variance_total": revised_total - forecast_total,
        # True when the job is still inside budget today but will not be if
        # everything already raised gets approved. This is the early warning.
        "forecast_over_budget": forecast_total > revised_total,
        "will_go_over": forecast_total > revised_total >= exposure_total,
        # Withheld payment. Never nets off actual cost.
        "retainage_held": retained,
        "retainage_released": released,
        "lines": rows,
    }
