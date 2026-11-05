"""Estimates, client updates, and the per-job activity feed.

The estimate is a priced, grouped view of a job's work that becomes a proposal.
The client update is the builder's broadcast to the owner. The activity feed is
neither new data nor a new table — it reads what already happened across the job
(comments, updates, invoices, shifts, selections) and orders it into one timeline.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BusinessRuleViolation
from app.models.estimate import ClientUpdate, Estimate, EstimateItem, EstimateStatus
from app.models.messaging import Comment
from app.models.owner_invoice import OwnerInvoice
from app.models.selection import Selection, SelectionApprovalStatus
from app.models.timeclock import Shift
from app.models.user import User
from app.repositories.base import NotFound

_EDITABLE = (EstimateStatus.DRAFT, EstimateStatus.SENT)


class EstimateService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    # ============================================== estimates =====

    def _base(self):
        return (
            select(Estimate)
            .options(selectinload(Estimate.items))
            .where(Estimate.tenant_id == self.tenant_id)
        )

    async def get(self, estimate_id: uuid.UUID) -> Estimate:
        # populate_existing: the estimate may already be in the identity map with a
        # stale (empty) items collection from before a line was added; without this
        # the re-query keeps the stale collection and the totals read as zero.
        est = await self.db.scalar(
            self._base().where(Estimate.id == estimate_id).execution_options(populate_existing=True)
        )
        if est is None:
            raise NotFound("Estimate", estimate_id)
        return est

    async def list(self, job_id: uuid.UUID | None = None) -> list[Estimate]:
        stmt = self._base()
        if job_id is not None:
            stmt = stmt.where(Estimate.job_id == job_id)
        return list(await self.db.scalars(stmt.order_by(Estimate.created_at.desc())))

    async def create(self, **data) -> Estimate:
        est = Estimate(tenant_id=self.tenant_id, **data)
        self.db.add(est)
        await self.db.commit()
        return await self.get(est.id)

    async def update(self, estimate_id: uuid.UUID, **data) -> Estimate:
        est = await self.get(estimate_id)
        if est.status not in _EDITABLE:
            raise BusinessRuleViolation("An accepted or declined estimate cannot be edited.")
        for field, value in data.items():
            setattr(est, field, value)
        await self.db.commit()
        return await self.get(estimate_id)

    async def delete(self, estimate_id: uuid.UUID) -> None:
        est = await self.get(estimate_id)
        await self.db.delete(est)
        await self.db.commit()

    async def set_status(self, estimate_id: uuid.UUID, status: EstimateStatus) -> Estimate:
        est = await self.get(estimate_id)
        if status is EstimateStatus.SENT and not est.items:
            raise BusinessRuleViolation("Add at least one line item before sending a proposal.")
        est.status = status
        if status is EstimateStatus.SENT and est.issued_on is None:
            est.issued_on = date.today()
        await self.db.commit()
        return await self.get(estimate_id)

    # line items
    async def add_item(self, estimate_id: uuid.UUID, **data) -> Estimate:
        est = await self.get(estimate_id)
        if est.status not in _EDITABLE:
            raise BusinessRuleViolation("This estimate can no longer be changed.")
        self.db.add(EstimateItem(tenant_id=self.tenant_id, estimate_id=estimate_id, **data))
        await self.db.commit()
        return await self.get(estimate_id)

    async def update_item(self, item_id: uuid.UUID, **data) -> Estimate:
        item = await self.db.scalar(
            select(EstimateItem).where(
                EstimateItem.tenant_id == self.tenant_id, EstimateItem.id == item_id
            )
        )
        if item is None:
            raise NotFound("Estimate item", item_id)
        for field, value in data.items():
            setattr(item, field, value)
        await self.db.commit()
        return await self.get(item.estimate_id)

    async def delete_item(self, item_id: uuid.UUID) -> Estimate:
        item = await self.db.scalar(
            select(EstimateItem).where(
                EstimateItem.tenant_id == self.tenant_id, EstimateItem.id == item_id
            )
        )
        if item is None:
            raise NotFound("Estimate item", item_id)
        estimate_id = item.estimate_id
        await self.db.delete(item)
        await self.db.commit()
        return await self.get(estimate_id)

    # ============================================ client updates =====

    async def _name_map(self, ids: set) -> dict:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        rows = await self.db.execute(
            select(User.id, User.full_name).where(
                User.tenant_id == self.tenant_id, User.id.in_(ids)
            )
        )
        return {r[0]: r[1] for r in rows}

    async def list_updates(self, job_id: uuid.UUID, *, published_only: bool = False) -> list[dict]:
        stmt = select(ClientUpdate).where(
            ClientUpdate.tenant_id == self.tenant_id, ClientUpdate.job_id == job_id
        )
        if published_only:
            stmt = stmt.where(ClientUpdate.is_published.is_(True))
        rows = list(
            await self.db.scalars(
                stmt.order_by(ClientUpdate.posted_on.desc(), ClientUpdate.created_at.desc())
            )
        )
        names = await self._name_map({u.posted_by_id for u in rows})
        return [self._update_dict(u, names) for u in rows]

    @staticmethod
    def _update_dict(u: ClientUpdate, names: dict) -> dict:
        return {
            "id": u.id,
            "job_id": u.job_id,
            "title": u.title,
            "body": u.body,
            "posted_by_id": u.posted_by_id,
            "posted_by_name": names.get(u.posted_by_id, "Builder"),
            "is_published": u.is_published,
            "posted_on": u.posted_on,
        }

    async def post_update(self, *, posted_by_id, **data) -> dict:
        u = ClientUpdate(tenant_id=self.tenant_id, posted_by_id=posted_by_id, **data)
        self.db.add(u)
        await self.db.commit()
        await self.db.refresh(u)
        names = await self._name_map({posted_by_id})
        return self._update_dict(u, names)

    async def edit_update(self, update_id: uuid.UUID, **data) -> dict:
        u = await self.db.scalar(
            select(ClientUpdate).where(
                ClientUpdate.tenant_id == self.tenant_id, ClientUpdate.id == update_id
            )
        )
        if u is None:
            raise NotFound("Client update", update_id)
        for field, value in data.items():
            setattr(u, field, value)
        await self.db.commit()
        names = await self._name_map({u.posted_by_id})
        return self._update_dict(u, names)

    async def delete_update(self, update_id: uuid.UUID) -> None:
        u = await self.db.scalar(
            select(ClientUpdate).where(
                ClientUpdate.tenant_id == self.tenant_id, ClientUpdate.id == update_id
            )
        )
        if u is None:
            raise NotFound("Client update", update_id)
        await self.db.delete(u)
        await self.db.commit()

    # ============================================= activity feed =====

    async def activity(self, job_id: uuid.UUID, *, limit: int = 60) -> list[dict]:
        """Merge recent events across the job into one timeline, newest first."""
        events: list[dict] = []

        comments = list(
            await self.db.scalars(
                select(Comment)
                .where(Comment.tenant_id == self.tenant_id, Comment.job_id == job_id)
                .order_by(Comment.created_at.desc())
                .limit(limit)
            )
        )
        names = await self._name_map({c.author_id for c in comments})
        for c in comments:
            events.append(
                {
                    "kind": "comment",
                    "at": c.created_at,
                    "actor": names.get(c.author_id, "Someone"),
                    "title": "Comment",
                    "detail": c.body[:160],
                }
            )

        updates = list(
            await self.db.scalars(
                select(ClientUpdate)
                .where(ClientUpdate.tenant_id == self.tenant_id, ClientUpdate.job_id == job_id)
                .order_by(ClientUpdate.created_at.desc())
                .limit(limit)
            )
        )
        unames = await self._name_map({u.posted_by_id for u in updates})
        for u in updates:
            events.append(
                {
                    "kind": "client_update",
                    "at": u.created_at,
                    "actor": unames.get(u.posted_by_id, "Builder"),
                    "title": f"Client update: {u.title}",
                    "detail": u.body[:160],
                }
            )

        invoices = list(
            await self.db.scalars(
                select(OwnerInvoice)
                .where(OwnerInvoice.tenant_id == self.tenant_id, OwnerInvoice.job_id == job_id)
                .order_by(OwnerInvoice.created_at.desc())
                .limit(limit)
            )
        )
        for inv in invoices:
            events.append(
                {
                    "kind": "invoice",
                    "at": inv.created_at,
                    "actor": "Accounting",
                    "title": f"Client invoice {inv.invoice_number} ({inv.status.value})",
                    "detail": f"{inv.title} — {inv.total_price}",
                }
            )

        shifts = list(
            await self.db.scalars(
                select(Shift)
                .where(Shift.tenant_id == self.tenant_id, Shift.job_id == job_id)
                .order_by(Shift.clock_in_at.desc())
                .limit(limit)
            )
        )
        for sh in shifts:
            events.append(
                {
                    "kind": "shift",
                    "at": sh.clock_in_at,
                    "actor": sh.worker_name,
                    "title": "Clocked in" if sh.is_active else "Shift recorded",
                    "detail": f"{sh.total_hours} h" if not sh.is_active else None,
                }
            )

        selections = list(
            await self.db.scalars(
                select(Selection)
                .where(
                    Selection.tenant_id == self.tenant_id,
                    Selection.job_id == job_id,
                    Selection.decided_at.is_not(None),
                )
                .order_by(Selection.decided_at.desc())
                .limit(limit)
            )
        )
        for sel in selections:
            label = (
                "approved"
                if sel.approval_status is SelectionApprovalStatus.APPROVED
                else sel.approval_status.value
            )
            events.append(
                {
                    "kind": "selection",
                    "at": sel.decided_at,
                    "actor": sel.buyer_signature or "Buyer",
                    "title": f"Selection {label}",
                    "detail": None,
                }
            )

        events.sort(key=lambda e: e["at"], reverse=True)
        return events[:limit]
