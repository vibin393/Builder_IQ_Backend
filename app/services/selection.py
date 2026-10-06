"""Selections and the cascade they trigger.

Confirming a selection is the single most important transaction in the system. One
buyer decision must move, atomically:

    contract price  ->  options_total
    job budget      ->  revised_amount on each affected cost code
    schedule        ->  projected completion date

If any part fails the whole thing rolls back, because a contract that says one thing
while the budget says another is worse than no data at all.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.contract import Contract
from app.models.job import Job
from app.models.option import Option
from app.models.selection import Selection, SelectionStatus
from app.services import budget as budget_service


async def _load_option(
    db: AsyncSession, tenant_id: uuid.UUID, option_id: uuid.UUID, *, for_new: bool = False
) -> Option:
    """Load an option. Only a *new* choice needs it still on offer: a selection a buyer
    already made has to stay confirmable and cancellable after the option is retired,
    or it is stuck on the job for good."""
    option = await db.scalar(
        select(Option)
        .options(selectinload(Option.cost_lines))
        .where(Option.id == option_id, Option.tenant_id == tenant_id)
    )
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Option not found")
    if for_new and not option.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That option is no longer offered.",
        )
    return option


def _breakdown(selection: Selection, option: Option) -> list[dict]:
    """One unit's cost per cost code, as snapshotted when the option was chosen.

    Selections made before the snapshot existed fall back to the catalog's lines.
    """
    if selection.cost_breakdown is not None:
        return selection.cost_breakdown
    return [
        {"cost_code_id": str(cl.cost_code_id),
         "vendor_id": str(cl.vendor_id) if cl.vendor_id else None,
         "amount": str(cl.amount)}
        for cl in option.cost_lines
    ]


async def add_selection(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    job: Job,
    option_id: uuid.UUID,
    quantity: int = 1,
    selected_date: date | None = None,
) -> Selection:
    """Record a buyer's choice, snapshotting catalog price and cost at this moment."""
    from app.models.option import OptionPlan

    option = await _load_option(db, tenant_id, option_id, for_new=True)

    offered_on = list(
        await db.scalars(select(OptionPlan.plan_id).where(OptionPlan.option_id == option.id))
    )
    if offered_on and job.plan_id not in offered_on:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{option.name} is not offered on this house type.",
        )
    if quantity < 1 or quantity > option.max_quantity:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{option.name} can be taken once."
                if option.max_quantity == 1
                else f"{option.name} can be taken 1 to {option.max_quantity} times."
            ),
        )

    duplicate = await db.scalar(
        select(Selection).where(Selection.job_id == job.id, Selection.option_id == option_id)
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That option is already selected on this job.",
        )

    selection = Selection(
        tenant_id=tenant_id,
        job_id=job.id,
        option_id=option.id,
        quantity=quantity,
        price_at_selection=option.price,
        cost_at_selection=option.total_cost,
        schedule_days_at_selection=option.schedule_days,
        cost_breakdown=[
            {"cost_code_id": str(cl.cost_code_id),
             "vendor_id": str(cl.vendor_id) if cl.vendor_id else None,
             "amount": str(cl.amount)}
            for cl in option.cost_lines
        ],
        selected_date=selected_date or date.today(),
    )
    db.add(selection)
    await db.flush()
    await db.refresh(selection)
    return selection


async def confirm_selection(
    db: AsyncSession, tenant_id: uuid.UUID, selection: Selection
) -> dict[str, object]:
    """Cascade a confirmed selection into contract, budget and schedule.

    Returns a summary of what moved, so the caller can show the buyer and the
    superintendent the same set of consequences.
    """
    if selection.status is SelectionStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Selection is already confirmed."
        )
    if selection.status is SelectionStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot confirm a cancelled selection."
        )

    job = await db.get(Job, selection.job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    option = await _load_option(db, tenant_id, selection.option_id)

    # 1. Contract price.
    contract = await db.scalar(
        select(Contract).where(Contract.job_id == job.id, Contract.tenant_id == tenant_id)
    )
    price_delta = selection.extended_price
    if contract is not None:
        contract.options_total = contract.options_total + price_delta

    # 2. Budget, per cost code, from the breakdown snapshotted when the option was
    #    chosen. The comment here used to promise that; the code read the catalog's
    #    current lines, so a catalog edit in between put a different figure on the
    #    budget than the selection's cost - and cancelling took out yet another.
    budget = await budget_service.get_budget(db, tenant_id, job.id)
    budget_changes: list[dict[str, object]] = []

    if budget is not None:
        for part in _breakdown(selection, option):
            code_id = uuid.UUID(part["cost_code_id"])
            delta = Decimal(part["amount"]) * selection.quantity
            line = await budget_service.adjust_line(
                db,
                tenant_id,
                budget,
                code_id,
                delta,
                vendor_id=uuid.UUID(part["vendor_id"]) if part.get("vendor_id") else None,
                description=f"{option.code} - {option.name}",
            )
            budget_changes.append(
                {
                    "cost_code_id": str(code_id),
                    "delta": delta,
                    "revised_amount": line.revised_amount,
                }
            )

    # 3. Schedule.
    schedule_days = selection.schedule_days_at_selection * selection.quantity
    if schedule_days and job.projected_completion is not None:
        job.projected_completion = job.projected_completion + timedelta(days=schedule_days)

    selection.status = SelectionStatus.CONFIRMED
    await db.flush()

    return {
        "selection_id": selection.id,
        "price_delta": price_delta,
        "cost_delta": selection.extended_cost,
        "schedule_days_delta": schedule_days,
        "contract_options_total": contract.options_total if contract else Decimal("0"),
        "budget_changes": budget_changes,
    }


async def cancel_selection(
    db: AsyncSession, tenant_id: uuid.UUID, selection: Selection
) -> dict[str, object]:
    """Reverse a confirmed selection. Mirrors confirm exactly, with the signs flipped."""
    if selection.status is SelectionStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Selection is already cancelled."
        )

    was_confirmed = selection.status is SelectionStatus.CONFIRMED
    job = await db.get(Job, selection.job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if was_confirmed:
        option = await _load_option(db, tenant_id, selection.option_id)

        contract = await db.scalar(
            select(Contract).where(Contract.job_id == job.id, Contract.tenant_id == tenant_id)
        )
        if contract is not None:
            contract.options_total = contract.options_total - selection.extended_price

        budget = await budget_service.get_budget(db, tenant_id, job.id)
        if budget is not None:
            # Exactly what confirming put on, taken back off.
            for part in _breakdown(selection, option):
                await budget_service.adjust_line(
                    db,
                    tenant_id,
                    budget,
                    uuid.UUID(part["cost_code_id"]),
                    -(Decimal(part["amount"]) * selection.quantity),
                )

        schedule_days = selection.schedule_days_at_selection * selection.quantity
        if schedule_days and job.projected_completion is not None:
            job.projected_completion = job.projected_completion - timedelta(days=schedule_days)

    selection.status = SelectionStatus.CANCELLED
    await db.flush()
    return {"selection_id": selection.id, "reversed": was_confirmed}
