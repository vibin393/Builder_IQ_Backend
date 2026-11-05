"""Sales: lead proposals, the sales calendar, and the lead map."""

import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequireSales, RequireStaff
from app.models.lead_proposal import LeadProposalStatus
from app.schemas.sales import (
    CalendarEvent,
    LeadMapPoint,
    LeadProposalCreate,
    LeadProposalRead,
    LeadProposalUpdate,
    ProposalItemCreate,
    ProposalItemUpdate,
    ProposalTemplateCreate,
    ProposalTemplateRead,
    SaveAsTemplate,
)
from app.services.sales import SalesService

router = APIRouter(tags=["sales"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


# ---- lead proposals ----
@router.get("/lead-proposals", response_model=list[LeadProposalRead])
async def list_lead_proposals(
    db: DbSession,
    user: RequireStaff,
    lead_id: Annotated[uuid.UUID | None, Query()] = None,
    proposal_status: Annotated[LeadProposalStatus | None, Query()] = None,
):
    return await SalesService(db, user.tenant_id).list_proposals(
        lead_id=lead_id, status=proposal_status
    )


@router.get("/lead-proposals/{proposal_id}", response_model=LeadProposalRead)
async def get_lead_proposal(proposal_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await SalesService(db, user.tenant_id).get_proposal(proposal_id)


@router.post("/lead-proposals", response_model=LeadProposalRead, status_code=CREATED)
async def create_lead_proposal(payload: LeadProposalCreate, db: DbSession, user: RequireSales):
    return await SalesService(db, user.tenant_id).create_proposal(
        created_by_id=user.id, **payload.model_dump()
    )


@router.patch("/lead-proposals/{proposal_id}", response_model=LeadProposalRead)
async def update_lead_proposal(
    proposal_id: uuid.UUID, payload: LeadProposalUpdate, db: DbSession, user: RequireSales
):
    return await SalesService(db, user.tenant_id).update_proposal(
        proposal_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/lead-proposals/{proposal_id}", status_code=NO_CONTENT)
async def delete_lead_proposal(proposal_id: uuid.UUID, db: DbSession, user: RequireSales):
    await SalesService(db, user.tenant_id).delete_proposal(proposal_id)


@router.post("/lead-proposals/{proposal_id}/send", response_model=LeadProposalRead)
async def send_lead_proposal(proposal_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await SalesService(db, user.tenant_id).set_proposal_status(
        proposal_id, LeadProposalStatus.SENT
    )


@router.post("/lead-proposals/{proposal_id}/accept", response_model=LeadProposalRead)
async def accept_lead_proposal(proposal_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await SalesService(db, user.tenant_id).set_proposal_status(
        proposal_id, LeadProposalStatus.ACCEPTED
    )


@router.post("/lead-proposals/{proposal_id}/decline", response_model=LeadProposalRead)
async def decline_lead_proposal(proposal_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await SalesService(db, user.tenant_id).set_proposal_status(
        proposal_id, LeadProposalStatus.DECLINED
    )


# ---- proposal line items ----
@router.post(
    "/lead-proposals/{proposal_id}/items", response_model=LeadProposalRead, status_code=CREATED
)
async def add_proposal_item(
    proposal_id: uuid.UUID, payload: ProposalItemCreate, db: DbSession, user: RequireSales
):
    return await SalesService(db, user.tenant_id).add_item(proposal_id, **payload.model_dump())


@router.patch("/proposal-items/{item_id}", response_model=LeadProposalRead)
async def update_proposal_item(
    item_id: uuid.UUID, payload: ProposalItemUpdate, db: DbSession, user: RequireSales
):
    return await SalesService(db, user.tenant_id).update_item(
        item_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/proposal-items/{item_id}", response_model=LeadProposalRead)
async def delete_proposal_item(item_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await SalesService(db, user.tenant_id).delete_item(item_id)


# ---- proposal templates ----
@router.get("/proposal-templates", response_model=list[ProposalTemplateRead])
async def list_proposal_templates(db: DbSession, user: RequireStaff):
    return await SalesService(db, user.tenant_id).list_templates()


@router.get("/proposal-templates/{template_id}", response_model=ProposalTemplateRead)
async def get_proposal_template(template_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await SalesService(db, user.tenant_id).get_template(template_id)


@router.post("/proposal-templates", response_model=ProposalTemplateRead, status_code=CREATED)
async def create_proposal_template(
    payload: ProposalTemplateCreate, db: DbSession, user: RequireSales
):
    return await SalesService(db, user.tenant_id).create_template(
        name=payload.name,
        description=payload.description,
        items=[i.model_dump() for i in payload.items],
    )


@router.delete("/proposal-templates/{template_id}", status_code=NO_CONTENT)
async def delete_proposal_template(template_id: uuid.UUID, db: DbSession, user: RequireSales):
    await SalesService(db, user.tenant_id).delete_template(template_id)


@router.post(
    "/lead-proposals/{proposal_id}/apply-template/{template_id}", response_model=LeadProposalRead
)
async def apply_template(
    proposal_id: uuid.UUID, template_id: uuid.UUID, db: DbSession, user: RequireSales
):
    """Append a template's line items onto the proposal."""
    return await SalesService(db, user.tenant_id).apply_template(proposal_id, template_id)


@router.post(
    "/lead-proposals/{proposal_id}/save-as-template",
    response_model=ProposalTemplateRead,
    status_code=CREATED,
)
async def save_proposal_as_template(
    proposal_id: uuid.UUID, payload: SaveAsTemplate, db: DbSession, user: RequireSales
):
    """Save this proposal's line items as a reusable template."""
    return await SalesService(db, user.tenant_id).save_as_template(
        proposal_id, name=payload.name, description=payload.description
    )


# ---- sales calendar ----
@router.get("/sales-calendar", response_model=list[CalendarEvent])
async def sales_calendar(
    db: DbSession,
    user: RequireStaff,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
):
    """Follow-ups, design sessions and proposal expiries across a date range."""
    start = start or date.today()
    end = end or (start + timedelta(days=30))
    return await SalesService(db, user.tenant_id).calendar(start=start, end=end)


# ---- lead map ----
@router.get("/leads/map", response_model=list[LeadMapPoint])
async def lead_map(db: DbSession, user: RequireStaff):
    """Leads that carry geocoded coordinates, for the lead map."""
    return await SalesService(db, user.tenant_id).map_points()
