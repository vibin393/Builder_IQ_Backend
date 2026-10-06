"""Data access for purchase orders."""

import uuid

from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from app.repositories.base import TenantRepository


class PurchaseOrderRepository(TenantRepository[PurchaseOrder]):
    model = PurchaseOrder
    label = "Purchase order"
    # A purchase order without its lines is not a purchase order - the total,
    # the budget check and the response all need them.
    default_loads = ("lines",)

    async def search(
        self,
        *,
        job_id: uuid.UUID | None = None,
        vendor_id: uuid.UUID | None = None,
        status: PurchaseOrderStatus | None = None,
    ) -> list[PurchaseOrder]:
        return await self.list(
            job_id=job_id,
            vendor_id=vendor_id,
            status=status,
            order_by=PurchaseOrder.po_number,
        )

    async def next_number(self) -> str:
        """Next PO number for this builder. Numbers are per-tenant, not global."""
        from sqlalchemy import select

        last = await self.db.scalar(
            select(PurchaseOrder.po_number)
            .where(PurchaseOrder.tenant_id == self.tenant_id)
            .order_by(PurchaseOrder.po_number.desc())
            .limit(1)
        )
        nxt = (int(last.removeprefix("PO")) + 1) if last else 1
        return f"PO{nxt:05d}"


class PurchaseOrderLineRepository(TenantRepository[PurchaseOrderLine]):
    model = PurchaseOrderLine
    label = "Purchase order line"
