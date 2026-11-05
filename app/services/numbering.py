"""Sequential, tenant-scoped document numbers (job numbers, PO numbers).

Deliberately simple: a count-based suffix. Before production this should move to a
per-tenant sequence table so two concurrent requests cannot race to the same number.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.purchase_order import PurchaseOrder


async def next_job_number(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(select(func.count()).select_from(Job).where(Job.tenant_id == tenant_id))
    return f"J{(count or 0) + 1:05d}"


async def next_po_number(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    count = await db.scalar(
        select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.tenant_id == tenant_id)
    )
    return f"PO{(count or 0) + 1:05d}"
