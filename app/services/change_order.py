"""Change order approval - the second cascade.

Same shape as confirming a selection, with one addition that matters more than anything
else here: `responsible_party` decides whether the buyer is charged.

    BUYER    price and budget both move; margin is preserved
    BUILDER  budget moves, price does not; the builder eats it
    TRADE    neither moves; the cost is backcharged to the subcontractor

Getting this wrong is not a rounding error - it is the difference between a change order
that protects margin and one that quietly destroys it.
"""

import uuid
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_order import ChangeOrder, ChangeOrderStatus, ResponsibleParty
from app.models.contract import Contract
from app.models.job import Job
from app.services import budget as budget_service


async def approve(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    change_order: ChangeOrder,
    approved_by_id: uuid.UUID,
) -> dict[str, object]:
    if change_order.status is ChangeOrderStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Change order is already approved."
        )
    if change_order.status in (ChangeOrderStatus.REJECTED, ChangeOrderStatus.VOID):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve a {change_order.status} change order.",
        )

    job = await db.get(Job, change_order.job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    party = change_order.responsible_party

    # 1. Contract price - only the buyer gets charged.
    price_delta = change_order.price_delta if party is ResponsibleParty.BUYER else 0
    if price_delta:
        contract = await db.scalar(
            select(Contract).where(Contract.job_id == job.id, Contract.tenant_id == tenant_id)
        )
        if contract is not None:
            contract.options_total = contract.options_total + price_delta

    # 2. Budget - moves for buyer- and builder-funded work. A trade backcharge does not
    #    raise the budget; the cost is recovered from the subcontractor instead.
    budget_changes: list[dict[str, object]] = []
    if party is not ResponsibleParty.TRADE:
        budget = await budget_service.get_budget(db, tenant_id, job.id)
        if budget is not None:
            for line in change_order.cost_lines:
                updated = await budget_service.adjust_line(
                    db,
                    tenant_id,
                    budget,
                    line.cost_code_id,
                    line.amount,
                    vendor_id=line.vendor_id,
                    description=f"{change_order.co_number} - {change_order.description[:80]}",
                )
                budget_changes.append(
                    {
                        "cost_code_id": str(line.cost_code_id),
                        "delta": line.amount,
                        "revised_amount": updated.revised_amount,
                    }
                )

    # 3. Schedule.
    if change_order.schedule_days and job.projected_completion is not None:
        job.projected_completion = job.projected_completion + timedelta(
            days=change_order.schedule_days
        )

    change_order.status = ChangeOrderStatus.APPROVED
    change_order.approved_by_id = approved_by_id
    change_order.approved_date = date.today()
    await db.flush()

    return {
        "change_order_id": change_order.id,
        "responsible_party": party.value,
        "price_delta": price_delta,
        "cost_delta": change_order.cost_delta,
        "margin_impact": price_delta - change_order.cost_delta,
        "schedule_days_delta": change_order.schedule_days,
        "budget_changes": budget_changes,
    }


async def reject(
    db: AsyncSession, change_order: ChangeOrder, rejected_by_id: uuid.UUID
) -> ChangeOrder:
    if change_order.status is ChangeOrderStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An approved change order cannot be rejected. Raise a reversing one.",
        )
    change_order.status = ChangeOrderStatus.REJECTED
    change_order.approved_by_id = rejected_by_id
    await db.flush()
    return change_order
