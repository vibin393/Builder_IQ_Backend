"""Cost inbox: capture incoming costs, code them, then push to a bill.

An item is captured with whatever is known, coded with a job and cost code when
someone gets to it, and leaves the inbox when it is pushed to a bill or rejected.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.cost_inbox import CostInboxItem, CostInboxStatus
from app.repositories.base import NotFound
from app.schemas.common import PageParams

ZERO = Decimal("0")
_OPEN = (CostInboxStatus.NEW, CostInboxStatus.CODED)


class CostInboxService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    def _base(self):
        return select(CostInboxItem).where(CostInboxItem.tenant_id == self.tenant_id)

    async def get(self, item_id: uuid.UUID) -> CostInboxItem:
        item = await self.db.scalar(self._base().where(CostInboxItem.id == item_id))
        if item is None:
            raise NotFound("Cost inbox item", item_id)
        return item

    async def list(self, params: PageParams, *, status=None, job_id=None):
        stmt = self._base()
        if status is not None:
            stmt = stmt.where(CostInboxItem.status == status)
        if job_id is not None:
            stmt = stmt.where(CostInboxItem.job_id == job_id)
        total = await self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(CostInboxItem.purchase_date.desc().nullslast(), CostInboxItem.id)
        rows = list(await self.db.scalars(stmt.offset(params.offset).limit(params.size)))
        return rows, total

    async def summary(self) -> dict:
        rows = list(await self.db.scalars(self._base()))
        return {
            "count": len(rows),
            "new": sum(1 for r in rows if r.status is CostInboxStatus.NEW),
            "coded": sum(1 for r in rows if r.status is CostInboxStatus.CODED),
            "pushed": sum(1 for r in rows if r.status is CostInboxStatus.PUSHED),
            "rejected": sum(1 for r in rows if r.status is CostInboxStatus.REJECTED),
            "open_amount": sum((r.amount or ZERO for r in rows if r.status in _OPEN), start=ZERO),
        }

    async def create(self, **data) -> CostInboxItem:
        item = CostInboxItem(tenant_id=self.tenant_id, **data)
        self.db.add(item)
        await self.db.commit()
        return await self.get(item.id)

    async def update(self, item_id: uuid.UUID, **data) -> CostInboxItem:
        item = await self.get(item_id)
        if item.status in (CostInboxStatus.PUSHED, CostInboxStatus.REJECTED):
            raise BusinessRuleViolation("A pushed or rejected item can no longer be edited.")
        for field, value in data.items():
            setattr(item, field, value)
        # Auto-advance NEW -> CODED once it has a job and cost code.
        if item.status is CostInboxStatus.NEW and item.is_coded:
            item.status = CostInboxStatus.CODED
        await self.db.commit()
        return await self.get(item_id)

    async def reject(self, item_id: uuid.UUID) -> CostInboxItem:
        item = await self.get(item_id)
        item.status = CostInboxStatus.REJECTED
        await self.db.commit()
        return await self.get(item_id)

    async def reopen(self, item_id: uuid.UUID) -> CostInboxItem:
        item = await self.get(item_id)
        item.status = CostInboxStatus.CODED if item.is_coded else CostInboxStatus.NEW
        item.invoice_id = None
        await self.db.commit()
        return await self.get(item_id)

    async def push(self, item_id: uuid.UUID) -> CostInboxItem:
        """Mark the item as pushed to a bill.

        Requires it to be coded (a job and a cost code), because a bill with no cost
        code is exactly the uncoded cost the inbox exists to prevent.
        """
        item = await self.get(item_id)
        if item.status is CostInboxStatus.PUSHED:
            raise BusinessRuleViolation("This item has already been pushed.")
        if not item.is_coded:
            raise BusinessRuleViolation(
                "Assign a job and a cost code before pushing this to a bill."
            )
        item.status = CostInboxStatus.PUSHED
        await self.db.commit()
        return await self.get(item_id)

    async def delete(self, item_id: uuid.UUID) -> None:
        item = await self.get(item_id)
        await self.db.delete(item)
        await self.db.commit()
