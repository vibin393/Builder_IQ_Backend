"""Owner (client) invoicing: the receivables side.

Draft, issue, and settle the invoices that make up a job's payment schedule. The
money totals are derived from recorded payments — recording a payment is the only
way the balance moves, so the status always matches the cash.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BusinessRuleViolation
from app.models.contract import Contract
from app.models.owner_invoice import (
    OPEN_STATUSES,
    OwnerInvoice,
    OwnerInvoiceKind,
    OwnerInvoiceLine,
    OwnerInvoiceStatus,
    OwnerPayment,
)
from app.repositories.base import NotFound
from app.repositories.job import JobRepository
from app.schemas.common import PageParams

CENTS = Decimal("0.01")
ZERO = Decimal("0")


class OwnerInvoiceService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.jobs = JobRepository(db, tenant_id)

    def _base(self):
        return (
            select(OwnerInvoice)
            .options(selectinload(OwnerInvoice.payments), selectinload(OwnerInvoice.lines))
            .where(OwnerInvoice.tenant_id == self.tenant_id)
        )

    async def get(self, invoice_id: uuid.UUID) -> OwnerInvoice:
        # populate_existing so a lines/payments collection changed in this session is
        # reloaded, not kept stale (which would make the derived totals read wrong).
        inv = await self.db.scalar(
            self._base()
            .where(OwnerInvoice.id == invoice_id)
            .execution_options(populate_existing=True)
        )
        if inv is None:
            raise NotFound("Owner invoice", invoice_id)
        return inv

    # -- line items --
    async def add_line(self, invoice_id: uuid.UUID, **data) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        if inv.status in (OwnerInvoiceStatus.PAID, OwnerInvoiceStatus.VOID):
            raise BusinessRuleViolation("A paid or void invoice cannot be changed.")
        self.db.add(OwnerInvoiceLine(tenant_id=self.tenant_id, owner_invoice_id=invoice_id, **data))
        await self.db.commit()
        return await self.get(invoice_id)

    async def update_line(self, line_id: uuid.UUID, **data) -> OwnerInvoice:
        line = await self.db.scalar(
            select(OwnerInvoiceLine).where(
                OwnerInvoiceLine.tenant_id == self.tenant_id, OwnerInvoiceLine.id == line_id
            )
        )
        if line is None:
            raise NotFound("Invoice line", line_id)
        for field, value in data.items():
            setattr(line, field, value)
        await self.db.commit()
        return await self.get(line.owner_invoice_id)

    async def delete_line(self, line_id: uuid.UUID) -> OwnerInvoice:
        line = await self.db.scalar(
            select(OwnerInvoiceLine).where(
                OwnerInvoiceLine.tenant_id == self.tenant_id, OwnerInvoiceLine.id == line_id
            )
        )
        if line is None:
            raise NotFound("Invoice line", line_id)
        invoice_id = line.owner_invoice_id
        await self.db.delete(line)
        await self.db.commit()
        return await self.get(invoice_id)

    async def list(self, params: PageParams, *, job_id=None, status=None):
        stmt = self._base()
        if job_id is not None:
            stmt = stmt.where(OwnerInvoice.job_id == job_id)
        if status is not None:
            stmt = stmt.where(OwnerInvoice.status == status)
        total = (
            await self.db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
            or 0
        )
        stmt = stmt.order_by(OwnerInvoice.issue_date.desc().nullslast(), OwnerInvoice.id)
        rows = list(await self.db.scalars(stmt.offset(params.offset).limit(params.size)))
        return rows, total

    async def summary(self, job_id: uuid.UUID | None = None) -> dict:
        stmt = self._base()
        if job_id is not None:
            stmt = stmt.where(OwnerInvoice.job_id == job_id)
        rows = list(await self.db.scalars(stmt))
        today = date.today()
        total_invoiced = sum((r.total_price for r in rows), start=ZERO)
        total_paid = sum((r.amount_paid for r in rows), start=ZERO)
        outstanding = sum((r.balance_due for r in rows if r.status in OPEN_STATUSES), start=ZERO)
        return {
            "job_id": job_id,
            "count": len(rows),
            "draft": sum(1 for r in rows if r.status is OwnerInvoiceStatus.DRAFT),
            "outstanding_count": sum(1 for r in rows if r.status in OPEN_STATUSES),
            "overdue": sum(
                1
                for r in rows
                if r.status in OPEN_STATUSES
                and r.due_date
                and r.due_date < today
                and r.balance_due > ZERO
            ),
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "outstanding": outstanding,
        }

    async def _next_number(self) -> str:
        n = await self.db.scalar(
            select(func.count())
            .select_from(OwnerInvoice)
            .where(OwnerInvoice.tenant_id == self.tenant_id)
        )
        return f"OI-{(n or 0) + 1:04d}"

    async def create(self, **data) -> OwnerInvoice:
        await self.jobs.require_exists(data["job_id"])
        if not data.get("invoice_number"):
            data["invoice_number"] = await self._next_number()
        inv = OwnerInvoice(tenant_id=self.tenant_id, **data)
        self.db.add(inv)
        await self.db.commit()
        return await self.get(inv.id)

    async def generate_schedule(
        self,
        job_id: uuid.UUID,
        *,
        contract_amount: Decimal | None = None,
        deposit_percent: Decimal = Decimal("10"),
        progress_count: int = 3,
        final_percent: Decimal = Decimal("10"),
        first_due: date | None = None,
        interval_days: int = 30,
        retainage_percent: Decimal = Decimal("0"),
    ) -> list[OwnerInvoice]:
        """Turn a contract total into a draft payment schedule: a deposit, N equal
        progress draws, and a final payment, each an invoice the builder then sends.

        This is BuilderTrend's "Payment schedule" in one call. The draws are rounded
        to the cent and the *last* line absorbs the rounding remainder, so the lines
        always sum to the contract amount exactly. One schedule per job: it refuses
        if invoices already exist, rather than silently doubling them.
        """
        await self.jobs.require_exists(job_id)

        existing = await self.db.scalar(
            select(func.count())
            .select_from(OwnerInvoice)
            .where(
                OwnerInvoice.tenant_id == self.tenant_id,
                OwnerInvoice.job_id == job_id,
                OwnerInvoice.status != OwnerInvoiceStatus.VOID,
            )
        )
        if existing:
            raise BusinessRuleViolation(
                "This job already has invoices. Void them before generating a new schedule."
            )

        if contract_amount is None:
            contract = await self.db.scalar(
                select(Contract).where(
                    Contract.tenant_id == self.tenant_id, Contract.job_id == job_id
                )
            )
            # total_price is a derived property, so read it off the loaded row.
            contract_amount = contract.total_price if contract else None
        if not contract_amount or Decimal(contract_amount) <= ZERO:
            raise BusinessRuleViolation(
                "This job has no contract total to schedule — enter an amount."
            )
        if deposit_percent + final_percent > Decimal("100"):
            raise BusinessRuleViolation(
                "The deposit and final percentages together cannot exceed 100%."
            )

        contract_amount = Decimal(contract_amount)
        deposit = (contract_amount * deposit_percent / 100).quantize(CENTS, ROUND_HALF_UP)
        final = (contract_amount * final_percent / 100).quantize(CENTS, ROUND_HALF_UP)
        pool = contract_amount - deposit - final
        each = (
            (pool / progress_count).quantize(CENTS, ROUND_HALF_UP) if progress_count > 0 else ZERO
        )

        plan: list[tuple[OwnerInvoiceKind, str, Decimal]] = []
        if deposit_percent > 0:
            plan.append((OwnerInvoiceKind.DEPOSIT, "Deposit", deposit))
        for i in range(progress_count):
            plan.append(
                (OwnerInvoiceKind.PROGRESS, f"Progress draw {i + 1} of {progress_count}", each)
            )
        if final_percent > 0:
            plan.append((OwnerInvoiceKind.FINAL, "Final payment", final))
        if not plan:
            raise BusinessRuleViolation(
                "Nothing to schedule — set a deposit, draws or a final payment."
            )

        # The last line takes the rounding remainder so the schedule totals exactly.
        diff = contract_amount - sum((a for _, _, a in plan), start=ZERO)
        kind, title, amount = plan[-1]
        plan[-1] = (kind, title, amount + diff)

        base = (
            await self.db.scalar(
                select(func.count())
                .select_from(OwnerInvoice)
                .where(OwnerInvoice.tenant_id == self.tenant_id)
            )
            or 0
        )

        due = first_due or date.today()
        created: list[OwnerInvoice] = []
        for idx, (kind, title, amount) in enumerate(plan):
            retain = (
                (amount * retainage_percent / 100).quantize(CENTS, ROUND_HALF_UP)
                if retainage_percent
                else ZERO
            )
            inv = OwnerInvoice(
                tenant_id=self.tenant_id,
                job_id=job_id,
                invoice_number=f"OI-{base + idx + 1:04d}",
                title=title,
                kind=kind,
                status=OwnerInvoiceStatus.DRAFT,
                subtotal=amount,
                retainage_amount=retain,
                due_date=due,
            )
            self.db.add(inv)
            created.append(inv)
            due = due + timedelta(days=interval_days)

        await self.db.commit()
        return [await self.get(inv.id) for inv in created]

    async def update(self, invoice_id: uuid.UUID, **data) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        if inv.status in (OwnerInvoiceStatus.PAID, OwnerInvoiceStatus.VOID):
            raise BusinessRuleViolation("A paid or void invoice cannot be edited.")
        for field, value in data.items():
            setattr(inv, field, value)
        await self.db.commit()
        return await self.get(invoice_id)

    async def delete(self, invoice_id: uuid.UUID) -> None:
        inv = await self.get(invoice_id)
        if inv.amount_paid > ZERO:
            raise BusinessRuleViolation(
                "This invoice has payments against it. Void it instead of deleting."
            )
        await self.db.delete(inv)
        await self.db.commit()

    async def send(self, invoice_id: uuid.UUID) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        if inv.status is not OwnerInvoiceStatus.DRAFT:
            raise BusinessRuleViolation("Only a draft invoice can be sent.")
        inv.status = OwnerInvoiceStatus.SENT
        inv.issue_date = inv.issue_date or date.today()
        await self.db.commit()
        return await self.get(invoice_id)

    async def mark_viewed(self, invoice_id: uuid.UUID) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        inv.viewed_by_owner_on = inv.viewed_by_owner_on or date.today()
        if inv.status is OwnerInvoiceStatus.SENT:
            inv.status = OwnerInvoiceStatus.VIEWED
        await self.db.commit()
        return await self.get(invoice_id)

    async def void(self, invoice_id: uuid.UUID) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        if inv.status is OwnerInvoiceStatus.PAID:
            raise BusinessRuleViolation("A fully paid invoice cannot be voided.")
        inv.status = OwnerInvoiceStatus.VOID
        await self.db.commit()
        return await self.get(invoice_id)

    async def record_payment(self, invoice_id: uuid.UUID, **data) -> OwnerInvoice:
        inv = await self.get(invoice_id)
        if inv.status in (OwnerInvoiceStatus.DRAFT, OwnerInvoiceStatus.VOID):
            raise BusinessRuleViolation("Send the invoice before recording a payment against it.")
        data["paid_on"] = data.get("paid_on") or date.today()
        payment = OwnerPayment(tenant_id=self.tenant_id, owner_invoice_id=invoice_id, **data)
        self.db.add(payment)
        await self.db.flush()
        await self.db.refresh(inv, ["payments"])
        # Keep status honest with the cash.
        if inv.balance_due <= ZERO:
            inv.status = OwnerInvoiceStatus.PAID
        elif inv.amount_paid > ZERO:
            inv.status = OwnerInvoiceStatus.PARTIAL
        await self.db.commit()
        return await self.get(invoice_id)
