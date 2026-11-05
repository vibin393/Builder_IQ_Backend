"""Business tier for purchase orders.

Issuing a purchase order is the moment a budget number becomes a real
obligation, so it is the most guarded write in the system. Two gates run before
the money moves:

  * the trade must be cleared to work - prequalified, with nothing lapsed and no
    overdue review. Committing work to a trade whose liability cover has expired
    puts the exposure on the builder.
  * the commitment must fit inside what is left on every cost code it touches.

Both refuse by default and both can be overridden deliberately. That is not
softness: a guard that cannot be overridden gets worked around outside the
system, and then there is no record of the decision at all. An override here is
a recorded act; a purchase order raised in a spreadsheet is not.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.purchase_order import (
    COMMITTED_STATUSES,
    PurchaseOrder,
    PurchaseOrderStatus,
)
from app.repositories.catalog import CostCodeRepository, VendorRepository
from app.repositories.job import JobRepository
from app.repositories.purchasing import (
    PurchaseOrderLineRepository,
    PurchaseOrderRepository,
)
from app.services import compliance as compliance_service
from app.services import job_cost as job_cost_service


class PurchasingService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.orders = PurchaseOrderRepository(db, tenant_id)
        self.lines = PurchaseOrderLineRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)
        self.vendors = VendorRepository(db, tenant_id)
        self.cost_codes = CostCodeRepository(db, tenant_id)

    # -- reads ---------------------------------------------------------------

    async def list(self, *, overdue_only: bool = False, **filters) -> list[PurchaseOrder]:
        rows = await self.orders.search(**filters)
        return [p for p in rows if p.is_overdue] if overdue_only else rows

    async def get(self, po_id: uuid.UUID) -> PurchaseOrder:
        return await self.orders.get(po_id)

    async def summary(self) -> dict:
        """The board: what is committed, what is still a draft, what is late, and how
        much of the commitment has turned into approved actual cost."""
        rows = await self.orders.search()
        by_status: dict[str, int] = {}
        committed = draft_value = billed = remaining = Decimal("0")
        drafts = open_ = awaiting = overdue = overrides = 0
        for p in rows:
            by_status[p.status.value] = by_status.get(p.status.value, 0) + 1
            if p.status is PurchaseOrderStatus.DRAFT:
                drafts += 1
                draft_value += p.total
            if p.status in COMMITTED_STATUSES:
                committed += p.total
                billed += p.billed_to_date
                remaining += p.remaining_to_bill
            if p.status in (PurchaseOrderStatus.ISSUED, PurchaseOrderStatus.ACKNOWLEDGED):
                open_ += 1
            if p.status is PurchaseOrderStatus.ISSUED:
                awaiting += 1
            if p.is_overdue:
                overdue += 1
            if p.compliance_override or p.budget_override:
                overrides += 1
        return {
            "committed": committed,
            "draft_value": draft_value,
            "drafts": drafts,
            "open": open_,
            "awaiting_acknowledgement": awaiting,
            "overdue": overdue,
            "billed_to_date": billed,
            "remaining_to_bill": remaining,
            "overrides": overrides,
            "total": len(rows),
            "by_status": by_status,
        }

    # -- writes --------------------------------------------------------------

    async def create(
        self, *, job_id, vendor_id, lines, notes=None, retainage_percent=0, required_by=None
    ):
        """Draft a purchase order. A draft commits nothing."""
        await self.jobs.require_exists(job_id)
        await self.vendors.require_exists(vendor_id)
        for line in lines:
            await self.cost_codes.require_exists(line.cost_code_id)

        po = self.orders.stage(
            po_number=await self.orders.next_number(),
            job_id=job_id,
            vendor_id=vendor_id,
            notes=notes,
            retainage_percent=retainage_percent,
            required_by=required_by,
        )
        # Flush so the lines have a purchase_order_id to point at, but stay in
        # one transaction: a PO with half its lines is not a thing.
        await self.orders.flush()

        for line in lines:
            self.lines.stage(
                purchase_order_id=po.id,
                cost_code_id=line.cost_code_id,
                amount=line.amount,
                description=line.description,
            )

        await self.orders.commit()
        return await self.orders.get(po.id)

    async def update_draft(self, po_id: uuid.UUID, *, fields: dict, lines=None) -> PurchaseOrder:
        """Edit a draft. Issued POs are never edited — a change order supersedes them."""
        po = await self.orders.get(po_id)
        if po.status is not PurchaseOrderStatus.DRAFT:
            raise BusinessRuleViolation(
                f"Purchase order is {po.status}; only a draft can be edited.",
                "Raise a change order to alter an issued commitment.",
            )
        for field, value in fields.items():
            setattr(po, field, value)

        if lines is not None:
            for line in lines:
                await self.cost_codes.require_exists(line.cost_code_id)
            for old in list(po.lines):
                await self.db.delete(old)
            await self.orders.flush()
            for line in lines:
                self.lines.stage(
                    purchase_order_id=po.id,
                    cost_code_id=line.cost_code_id,
                    amount=line.amount,
                    description=line.description,
                )
        await self.orders.commit()
        await self.orders.refresh(po, "lines")
        return po

    async def issue(
        self,
        po_id: uuid.UUID,
        *,
        allow_over_budget: bool = False,
        skip_compliance: bool = False,
        override_reason: str | None = None,
        issued_by_id: uuid.UUID | None = None,
    ) -> PurchaseOrder:
        """Commit the money. Both gates run here.

        An override is only recorded when the gate would actually have refused — a
        flag set on a PO that would have passed anyway is noise, not a decision.
        """
        po = await self.orders.get(po_id)

        if po.status is not PurchaseOrderStatus.DRAFT:
            raise BusinessRuleViolation(f"Purchase order is already {po.status}.")

        compliance_refusal = await self._compliance_refusal(po)
        if compliance_refusal:
            if not skip_compliance:
                raise compliance_refusal
            po.compliance_override = True

        budget_refusal = await self._budget_refusal(po)
        if budget_refusal:
            if not allow_over_budget:
                raise budget_refusal
            po.budget_override = True

        if (po.compliance_override or po.budget_override) and override_reason:
            po.override_reason = override_reason

        po.status = PurchaseOrderStatus.ISSUED
        po.issued_date = date.today()
        po.issued_by_id = issued_by_id
        await self.orders.commit()
        return await self.orders.get(po_id)

    async def acknowledge(self, po_id: uuid.UUID) -> PurchaseOrder:
        """The trade has confirmed the PO — recorded by the builder, e.g. by phone. The
        trade can also do this themselves from the portal."""
        po = await self.orders.get(po_id)
        if po.status is not PurchaseOrderStatus.ISSUED:
            raise BusinessRuleViolation(
                f"Only an issued purchase order can be acknowledged; this one is {po.status}."
            )
        po.status = PurchaseOrderStatus.ACKNOWLEDGED
        po.acknowledged_on = date.today()
        await self.orders.commit()
        return await self.orders.get(po_id)

    async def complete(self, po_id: uuid.UUID) -> PurchaseOrder:
        """The scope is delivered. The commitment stays committed; it is simply done."""
        po = await self.orders.get(po_id)
        if po.status not in (PurchaseOrderStatus.ISSUED, PurchaseOrderStatus.ACKNOWLEDGED):
            raise BusinessRuleViolation(
                f"Only an issued or acknowledged purchase order can be completed; "
                f"this one is {po.status}."
            )
        po.status = PurchaseOrderStatus.COMPLETE
        po.completed_on = date.today()
        await self.orders.commit()
        return await self.orders.get(po_id)

    async def void(self, po_id: uuid.UUID) -> PurchaseOrder:
        """Void it, releasing the committed cost back to the budget.

        Refused once invoices have been approved against it: voiding then would drop
        committed cost below money already spent, and the job's cost report would lie.
        """
        po = await self.orders.get(po_id)
        if po.status is PurchaseOrderStatus.VOID:
            raise BusinessRuleViolation("Purchase order is already void.")
        if po.billed_to_date > 0:
            raise BusinessRuleViolation(
                f"{po.po_number} has approved invoices against it; it cannot be voided.",
                "Complete it instead, or reverse the invoices first.",
            )
        po.status = PurchaseOrderStatus.VOID
        await self.orders.commit()
        return await self.orders.get(po_id)

    # -- the gates -----------------------------------------------------------

    async def _compliance_refusal(self, po: PurchaseOrder) -> BusinessRuleViolation | None:
        result = await compliance_service.check_vendor(self.db, self.tenant_id, po.vendor_id)
        if result.ok:
            return None
        vendor = await self.vendors.find(po.vendor_id)
        return BusinessRuleViolation(
            result.as_detail(vendor.name if vendor else "This trade"),
            "Resolve it, or re-issue with skip_compliance=true to record an override.",
        )

    async def _budget_refusal(self, po: PurchaseOrder) -> BusinessRuleViolation | None:
        summary = await job_cost_service.job_cost_summary(self.db, self.tenant_id, po.job_id)
        remaining = {line["cost_code_id"]: line["uncommitted"] for line in summary["lines"]}

        breaches = [line for line in po.lines if line.amount > remaining.get(line.cost_code_id, 0)]
        if not breaches:
            return None
        return BusinessRuleViolation(
            "Issuing this PO would commit more than the remaining budget on "
            f"{len(breaches)} cost code(s).",
            "Raise the budget with a change order, or re-issue with allow_over_budget=true.",
        )
