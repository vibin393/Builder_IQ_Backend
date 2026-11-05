"""Lead proposals, the sales calendar, and the lead map."""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BusinessRuleViolation
from app.models.crm import Lead
from app.models.lead_proposal import (
    LeadProposal,
    LeadProposalStatus,
    ProposalItem,
    ProposalTemplate,
    ProposalTemplateItem,
)
from app.models.sales_ops import AppointmentStatus, DesignAppointment
from app.repositories.base import NotFound

#: The line fields copied when applying/saving a template, so one change to the
#: line shape does not silently drop a field from the copy.
_LINE_FIELDS = (
    "group_name",
    "description",
    "unit",
    "quantity",
    "unit_cost",
    "markup_percent",
    "sort_order",
)

_CANCELLED = getattr(AppointmentStatus, "CANCELLED", None)


class SalesService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def _lead(self, lead_id: uuid.UUID) -> Lead:
        lead = await self.db.scalar(
            select(Lead).where(Lead.tenant_id == self.tenant_id, Lead.id == lead_id)
        )
        if lead is None:
            raise NotFound("Lead", lead_id)
        return lead

    async def _lead_names(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        rows = await self.db.execute(
            select(Lead.id, Lead.first_name, Lead.last_name).where(
                Lead.tenant_id == self.tenant_id, Lead.id.in_(ids)
            )
        )
        return {r[0]: f"{r[1]} {r[2]}" for r in rows}

    # -- proposals --
    def _proposal_base(self):
        return (
            select(LeadProposal)
            .options(selectinload(LeadProposal.items))
            .where(LeadProposal.tenant_id == self.tenant_id)
        )

    async def _load(self, proposal_id: uuid.UUID) -> LeadProposal:
        p = await self.db.scalar(
            self._proposal_base()
            .where(LeadProposal.id == proposal_id)
            .execution_options(populate_existing=True)
        )
        if p is None:
            raise NotFound("Lead proposal", proposal_id)
        return p

    async def list_proposals(self, *, lead_id=None, status=None) -> list[dict]:
        stmt = self._proposal_base()
        if lead_id is not None:
            stmt = stmt.where(LeadProposal.lead_id == lead_id)
        if status is not None:
            stmt = stmt.where(LeadProposal.status == status)
        rows = list(await self.db.scalars(stmt.order_by(LeadProposal.created_at.desc())))
        names = await self._lead_names({p.lead_id for p in rows})
        return [self._proposal_dict(p, names) for p in rows]

    @staticmethod
    def _proposal_dict(p: LeadProposal, names: dict) -> dict:
        return {
            "id": p.id,
            "lead_id": p.lead_id,
            "lead_name": names.get(p.lead_id, "Lead"),
            "title": p.title,
            "amount": p.amount,
            "total": p.total,
            "status": p.status,
            "sent_on": p.sent_on,
            "valid_until": p.valid_until,
            "decided_on": p.decided_on,
            "notes": p.notes,
            "is_expired": p.is_expired,
            "items": sorted(p.items, key=lambda i: (i.sort_order, i.description)),
        }

    async def get_proposal(self, proposal_id: uuid.UUID) -> dict:
        p = await self._load(proposal_id)
        names = await self._lead_names({p.lead_id})
        return self._proposal_dict(p, names)

    async def create_proposal(self, *, created_by_id, **data) -> dict:
        await self._lead(data["lead_id"])
        p = LeadProposal(tenant_id=self.tenant_id, created_by_id=created_by_id, **data)
        self.db.add(p)
        await self.db.commit()
        return await self.get_proposal(p.id)

    async def update_proposal(self, proposal_id: uuid.UUID, **data) -> dict:
        p = await self.db.scalar(
            select(LeadProposal).where(
                LeadProposal.tenant_id == self.tenant_id, LeadProposal.id == proposal_id
            )
        )
        if p is None:
            raise NotFound("Lead proposal", proposal_id)
        if p.status in (LeadProposalStatus.ACCEPTED, LeadProposalStatus.DECLINED):
            raise BusinessRuleViolation("A decided proposal cannot be edited.")
        for field, value in data.items():
            setattr(p, field, value)
        await self.db.commit()
        return await self.get_proposal(proposal_id)

    async def delete_proposal(self, proposal_id: uuid.UUID) -> None:
        p = await self.db.scalar(
            select(LeadProposal).where(
                LeadProposal.tenant_id == self.tenant_id, LeadProposal.id == proposal_id
            )
        )
        if p is None:
            raise NotFound("Lead proposal", proposal_id)
        await self.db.delete(p)
        await self.db.commit()

    async def set_proposal_status(self, proposal_id: uuid.UUID, status: LeadProposalStatus) -> dict:
        p = await self.db.scalar(
            select(LeadProposal).where(
                LeadProposal.tenant_id == self.tenant_id, LeadProposal.id == proposal_id
            )
        )
        if p is None:
            raise NotFound("Lead proposal", proposal_id)
        p.status = status
        if status is LeadProposalStatus.SENT and p.sent_on is None:
            p.sent_on = date.today()
        if status in (LeadProposalStatus.ACCEPTED, LeadProposalStatus.DECLINED):
            p.decided_on = date.today()
        await self.db.commit()
        return await self.get_proposal(proposal_id)

    # -- proposal line items --
    _LOCKED = (LeadProposalStatus.ACCEPTED, LeadProposalStatus.DECLINED)

    async def add_item(self, proposal_id: uuid.UUID, **data) -> dict:
        p = await self._load(proposal_id)
        if p.status in self._LOCKED:
            raise BusinessRuleViolation("A decided proposal can no longer be changed.")
        self.db.add(ProposalItem(tenant_id=self.tenant_id, proposal_id=proposal_id, **data))
        await self.db.commit()
        return await self.get_proposal(proposal_id)

    async def update_item(self, item_id: uuid.UUID, **data) -> dict:
        item = await self.db.scalar(
            select(ProposalItem).where(
                ProposalItem.tenant_id == self.tenant_id, ProposalItem.id == item_id
            )
        )
        if item is None:
            raise NotFound("Proposal item", item_id)
        for field, value in data.items():
            setattr(item, field, value)
        await self.db.commit()
        return await self.get_proposal(item.proposal_id)

    async def delete_item(self, item_id: uuid.UUID) -> dict:
        item = await self.db.scalar(
            select(ProposalItem).where(
                ProposalItem.tenant_id == self.tenant_id, ProposalItem.id == item_id
            )
        )
        if item is None:
            raise NotFound("Proposal item", item_id)
        proposal_id = item.proposal_id
        await self.db.delete(item)
        await self.db.commit()
        return await self.get_proposal(proposal_id)

    # -- proposal templates --
    def _template_base(self):
        return (
            select(ProposalTemplate)
            .options(selectinload(ProposalTemplate.items))
            .where(ProposalTemplate.tenant_id == self.tenant_id)
        )

    async def list_templates(self) -> list[ProposalTemplate]:
        return list(await self.db.scalars(self._template_base().order_by(ProposalTemplate.name)))

    async def get_template(self, template_id: uuid.UUID) -> ProposalTemplate:
        t = await self.db.scalar(
            self._template_base()
            .where(ProposalTemplate.id == template_id)
            .execution_options(populate_existing=True)
        )
        if t is None:
            raise NotFound("Proposal template", template_id)
        return t

    async def create_template(self, *, name, description=None, items=None) -> ProposalTemplate:
        t = ProposalTemplate(tenant_id=self.tenant_id, name=name, description=description)
        self.db.add(t)
        await self.db.flush()
        for row in items or []:
            self.db.add(
                ProposalTemplateItem(
                    tenant_id=self.tenant_id,
                    template_id=t.id,
                    **{k: row[k] for k in _LINE_FIELDS if k in row},
                )
            )
        await self.db.commit()
        return await self.get_template(t.id)

    async def delete_template(self, template_id: uuid.UUID) -> None:
        t = await self.db.scalar(
            select(ProposalTemplate).where(
                ProposalTemplate.tenant_id == self.tenant_id, ProposalTemplate.id == template_id
            )
        )
        if t is None:
            raise NotFound("Proposal template", template_id)
        await self.db.delete(t)
        await self.db.commit()

    async def apply_template(self, proposal_id: uuid.UUID, template_id: uuid.UUID) -> dict:
        """Copy a template's line items onto a proposal (appended to any already there)."""
        p = await self._load(proposal_id)
        if p.status in self._LOCKED:
            raise BusinessRuleViolation("A decided proposal can no longer be changed.")
        template = await self.get_template(template_id)
        base = max((i.sort_order for i in p.items), default=-1) + 1
        for n, ti in enumerate(sorted(template.items, key=lambda i: i.sort_order)):
            self.db.add(
                ProposalItem(
                    tenant_id=self.tenant_id,
                    proposal_id=proposal_id,
                    group_name=ti.group_name,
                    description=ti.description,
                    unit=ti.unit,
                    quantity=ti.quantity,
                    unit_cost=ti.unit_cost,
                    markup_percent=ti.markup_percent,
                    sort_order=base + n,
                )
            )
        await self.db.commit()
        return await self.get_proposal(proposal_id)

    async def save_as_template(
        self, proposal_id: uuid.UUID, *, name, description=None
    ) -> ProposalTemplate:
        """Capture a proposal's current line items as a reusable template."""
        p = await self._load(proposal_id)
        if not p.items:
            raise BusinessRuleViolation("This proposal has no line items to save.")
        t = ProposalTemplate(tenant_id=self.tenant_id, name=name, description=description)
        self.db.add(t)
        await self.db.flush()
        for pi in p.items:
            self.db.add(
                ProposalTemplateItem(
                    tenant_id=self.tenant_id,
                    template_id=t.id,
                    group_name=pi.group_name,
                    description=pi.description,
                    unit=pi.unit,
                    quantity=pi.quantity,
                    unit_cost=pi.unit_cost,
                    markup_percent=pi.markup_percent,
                    sort_order=pi.sort_order,
                )
            )
        await self.db.commit()
        return await self.get_template(t.id)

    # -- calendar --
    async def calendar(self, *, start: date, end: date) -> list[dict]:
        events: list[dict] = []

        leads = list(
            await self.db.scalars(
                select(Lead).where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.next_follow_up.is_not(None),
                    Lead.next_follow_up >= start,
                    Lead.next_follow_up <= end,
                )
            )
        )
        for lead in leads:
            events.append(
                {
                    "date": lead.next_follow_up,
                    "kind": "follow_up",
                    "ref_id": lead.id,
                    "title": f"Follow up: {lead.full_name}",
                    "detail": lead.status.value if lead.status else None,
                }
            )

        # Expected closes — the lead's projected sales date, weighted by confidence.
        closing = list(
            await self.db.scalars(
                select(Lead).where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.projected_sales_date.is_not(None),
                    Lead.projected_sales_date >= start,
                    Lead.projected_sales_date <= end,
                    Lead.is_archived.is_(False),
                )
            )
        )
        for lead in closing:
            conf = f"{lead.confidence}%" if lead.confidence is not None else "—"
            events.append(
                {
                    "date": lead.projected_sales_date,
                    "kind": "projected_close",
                    "ref_id": lead.id,
                    "title": f"Projected close: {lead.full_name}",
                    "detail": f"confidence {conf}",
                }
            )

        appts = list(
            await self.db.scalars(
                select(DesignAppointment).where(
                    DesignAppointment.tenant_id == self.tenant_id,
                    DesignAppointment.scheduled_for.is_not(None),
                )
            )
        )
        for a in appts:
            d = a.scheduled_for.date()
            if not (start <= d <= end):
                continue
            if _CANCELLED is not None and a.status is _CANCELLED:
                continue
            events.append(
                {
                    "date": d,
                    "kind": "appointment",
                    "ref_id": a.id,
                    "title": "Design studio session",
                    "detail": a.scheduled_for.strftime("%H:%M"),
                }
            )

        props = list(
            await self.db.scalars(
                select(LeadProposal).where(
                    LeadProposal.tenant_id == self.tenant_id,
                    LeadProposal.status == LeadProposalStatus.SENT,
                    LeadProposal.valid_until.is_not(None),
                    LeadProposal.valid_until >= start,
                    LeadProposal.valid_until <= end,
                )
            )
        )
        names = await self._lead_names({p.lead_id for p in props})
        for p in props:
            events.append(
                {
                    "date": p.valid_until,
                    "kind": "proposal_expiry",
                    "ref_id": p.id,
                    "title": f"Proposal expires: {names.get(p.lead_id, 'Lead')}",
                    "detail": p.title,
                }
            )

        events.sort(key=lambda e: e["date"])
        return events

    # -- map --
    async def map_points(self) -> list[dict]:
        leads = list(
            await self.db.scalars(
                select(Lead).where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.latitude.is_not(None),
                    Lead.longitude.is_not(None),
                    Lead.is_archived.is_(False),
                )
            )
        )
        return [
            {
                "id": lead.id,
                "name": lead.full_name,
                "lat": lead.latitude,
                "lng": lead.longitude,
                "status": lead.status.value if lead.status else "lead",
                "city": lead.city,
            }
            for lead in leads
        ]
