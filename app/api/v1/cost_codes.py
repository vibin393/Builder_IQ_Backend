"""Cost codes, as a ledger: where each code is used and what money sits on it.

The code record (create, edit, archive) stays in catalog.py. This adds the view a
commercial manager needs before touching the taxonomy - budget, committed and actual
per code across every home - plus bulk import. Registered before catalog.py, so these
fixed paths are not swallowed by `/cost-codes/{code_id}`.
"""

import uuid
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.bidding import BidPackage
from app.models.budget import BudgetLine
from app.models.change_order import ChangeOrderLine
from app.models.cost_code import CostCode, CostType
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceLine
from app.models.option import OptionCostLine
from app.models.plan import PlanCostLine
from app.models.purchase_order import COMMITTED_STATUSES, PurchaseOrder, PurchaseOrderLine
from app.models.vendor import VendorSpecialism

router = APIRouter(tags=["cost codes"])
ZERO = Decimal("0")


class CodeStats(BaseModel):
    budget_original: Decimal = ZERO
    budget_revised: Decimal = ZERO
    committed: Decimal = ZERO
    actual: Decimal = ZERO
    #: Revised budget less the larger of committed and actual: what is left to spend.
    remaining: Decimal = ZERO
    homes: int = 0
    plans: int = 0
    options: int = 0
    trades: int = 0


class CostCodeSummary(BaseModel):
    total: int
    active: int
    inactive: int
    archived: int
    unmapped: int  # no nominal code in the ledger
    unused: int  # active, and nothing has ever landed on it
    over_budget: int  # committed or actual above the revised budget
    by_division: dict[str, int]
    by_type: dict[str, int]


class ImportRow(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    division: str | None = Field(default=None, max_length=100)
    cost_type: CostType | None = None
    gl_account: str | None = Field(default=None, max_length=20)


class ImportRequest(BaseModel):
    rows: list[ImportRow] = Field(min_length=1, max_length=1000)


class ImportResult(BaseModel):
    created: int
    skipped: list[str]  # codes that already existed


class UsageRow(BaseModel):
    where: str
    count: int


async def _stats(db, tenant_id) -> dict[uuid.UUID, CodeStats]:
    out: dict[uuid.UUID, CodeStats] = defaultdict(CodeStats)

    for code_id, original, revised, homes in await db.execute(
        select(
            BudgetLine.cost_code_id,
            func.coalesce(func.sum(BudgetLine.original_amount), 0),
            func.coalesce(func.sum(BudgetLine.revised_amount), 0),
            func.count(func.distinct(BudgetLine.budget_id)),
        )
        .where(BudgetLine.tenant_id == tenant_id)
        .group_by(BudgetLine.cost_code_id)
    ):
        s = out[code_id]
        s.budget_original, s.budget_revised, s.homes = original, revised, homes

    for code_id, total in await db.execute(
        select(PurchaseOrderLine.cost_code_id, func.coalesce(func.sum(PurchaseOrderLine.amount), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
        )
        .group_by(PurchaseOrderLine.cost_code_id)
    ):
        out[code_id].committed = total

    for code_id, total in await db.execute(
        select(InvoiceLine.cost_code_id, func.coalesce(func.sum(InvoiceLine.amount), 0))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.tenant_id == tenant_id, Invoice.status.in_(ACTUAL_STATUSES))
        .group_by(InvoiceLine.cost_code_id)
    ):
        out[code_id].actual = total

    for model, attr in (
        (PlanCostLine, "plans"),
        (OptionCostLine, "options"),
        (VendorSpecialism, "trades"),
    ):
        for code_id, n in await db.execute(
            select(model.cost_code_id, func.count())
            .where(model.tenant_id == tenant_id)
            .group_by(model.cost_code_id)
        ):
            setattr(out[code_id], attr, n)

    for s in out.values():
        s.remaining = s.budget_revised - max(s.committed, s.actual)
    return out


@router.get("/cost-codes/stats", response_model=dict[uuid.UUID, CodeStats])
async def cost_code_stats(db: DbSession, user: RequireStaff):
    """Per code: budget, committed, actual and remaining across every home, and how
    many plans, options and trades use it. Codes with nothing on them are omitted."""
    return await _stats(db, user.tenant_id)


@router.get("/cost-codes/summary", response_model=CostCodeSummary)
async def cost_code_summary(db: DbSession, user: RequireStaff):
    codes = list(await db.scalars(select(CostCode).where(CostCode.tenant_id == user.tenant_id)))
    stats = await _stats(db, user.tenant_id)
    live = [c for c in codes if not c.is_archived]
    by_division: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for c in live:
        by_division[c.division or "Unassigned"] = by_division.get(c.division or "Unassigned", 0) + 1
        t = c.cost_type.value if c.cost_type else "unset"
        by_type[t] = by_type.get(t, 0) + 1

    def over(s):
        return bool(s and s.budget_revised and max(s.committed, s.actual) > s.budget_revised)

    def used(c):
        s = stats.get(c.id)
        return bool(s and (s.homes or s.committed or s.actual or s.plans or s.options))

    return CostCodeSummary(
        total=len(codes),
        active=sum(1 for c in live if c.is_active),
        inactive=sum(1 for c in live if not c.is_active),
        archived=len(codes) - len(live),
        unmapped=sum(1 for c in live if c.is_active and not c.gl_account),
        unused=sum(1 for c in live if c.is_active and not used(c)),
        over_budget=sum(1 for c in live if over(stats.get(c.id))),
        by_division=by_division,
        by_type=by_type,
    )


@router.post("/cost-codes/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
async def import_cost_codes(payload: ImportRequest, db: DbSession, admin: RequireAdmin):
    """Load a cost code list in one go - the usual way a builder moves off a spreadsheet.
    Codes that already exist are skipped, not overwritten, so the import can be re-run."""
    existing = {
        c.upper()
        for c in await db.scalars(
            select(CostCode.code).where(CostCode.tenant_id == admin.tenant_id)
        )
    }
    created, skipped = 0, []
    for row in payload.rows:
        code = row.code.strip()
        if code.upper() in existing:
            skipped.append(code)
            continue
        existing.add(code.upper())
        db.add(
            CostCode(
                tenant_id=admin.tenant_id,
                code=code,
                name=row.name.strip(),
                division=(row.division or "").strip() or None,
                cost_type=row.cost_type,
                gl_account=(row.gl_account or "").strip() or None,
                created_by_id=admin.id,
            )
        )
        created += 1
    await db.commit()
    return ImportResult(created=created, skipped=skipped)


@router.get("/cost-codes/{code_id}/usage", response_model=list[UsageRow])
async def cost_code_usage(code_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Everything that points at this code - what archiving it would strand."""
    if not await db.scalar(
        select(CostCode.id).where(CostCode.id == code_id, CostCode.tenant_id == user.tenant_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost code not found")
    out = []
    for model, label in (
        (BudgetLine, "Budget lines"),
        (PurchaseOrderLine, "Purchase order lines"),
        (InvoiceLine, "Invoice lines"),
        (PlanCostLine, "Plan standard costs"),
        (OptionCostLine, "Option cost lines"),
        (ChangeOrderLine, "Change order lines"),
        (BidPackage, "Bid packages"),
        (VendorSpecialism, "Trades that do it"),
    ):
        n = await db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.tenant_id == user.tenant_id, model.cost_code_id == code_id)
        )
        if n:
            out.append(UsageRow(where=label, count=n))
    return out
