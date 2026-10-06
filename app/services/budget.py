"""Budget generation and maintenance.

A job budget is generated once from the plan's standard costs, then revised by
confirmed selections and change orders. original_amount is never touched after
generation - the gap between original and revised is the estimator's feedback loop.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget, BudgetLine
from app.models.job import Job
from app.models.plan import PlanCostLine


async def get_budget(db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID) -> Budget | None:
    return await db.scalar(
        select(Budget).where(Budget.job_id == job_id, Budget.tenant_id == tenant_id)
    )


async def generate_budget(db: AsyncSession, tenant_id: uuid.UUID, job: Job) -> Budget:
    """Create the job budget from its plan's standard cost lines.

    Refuses to run twice: regenerating would silently discard change-order history.
    """
    existing = await get_budget(db, tenant_id, job.id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job already has a budget. Revise it via selections or change orders.",
        )

    plan_lines = (
        await db.scalars(
            select(PlanCostLine).where(
                PlanCostLine.plan_id == job.plan_id, PlanCostLine.tenant_id == tenant_id
            )
        )
    ).all()

    if not plan_lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The plan has no standard cost lines, so there is nothing to budget.",
        )

    budget = Budget(tenant_id=tenant_id, job_id=job.id)
    db.add(budget)
    await db.flush()

    for pl in plan_lines:
        db.add(
            BudgetLine(
                tenant_id=tenant_id,
                budget_id=budget.id,
                cost_code_id=pl.cost_code_id,
                vendor_id=pl.vendor_id,
                original_amount=pl.amount,
                revised_amount=pl.amount,
                # The plan's standard hours. The model said they were carried onto the
                # budget; they never were, so every home's labour target was blank.
                budgeted_hours=pl.labour_hours,
                description=pl.description,
            )
        )

    await db.flush()
    await db.refresh(budget)
    return budget


async def adjust_line(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    budget: Budget,
    cost_code_id: uuid.UUID,
    delta: Decimal,
    vendor_id: uuid.UUID | None = None,
    description: str | None = None,
) -> BudgetLine:
    """Apply a delta to the revised amount of one cost code, creating the line if needed.

    A selection can introduce a cost code the base plan never had (an upgrade to a trade
    not otherwise on the job), so the line is created with original_amount = 0 - it was
    never part of the original estimate and must not look as though it was.
    """
    line = await db.scalar(
        select(BudgetLine).where(
            BudgetLine.budget_id == budget.id,
            BudgetLine.cost_code_id == cost_code_id,
            BudgetLine.tenant_id == tenant_id,
        )
    )

    if line is None:
        line = BudgetLine(
            tenant_id=tenant_id,
            budget_id=budget.id,
            cost_code_id=cost_code_id,
            vendor_id=vendor_id,
            original_amount=Decimal("0"),
            revised_amount=Decimal("0"),
            description=description,
        )
        db.add(line)
        await db.flush()

    line.revised_amount = line.revised_amount + delta
    return line
