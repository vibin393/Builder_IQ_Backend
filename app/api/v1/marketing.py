"""Campaigns: what marketing spent, and what it produced.

Nothing here sends anything. A delivery provider does that and reports back; this
records what was committed, what arrived, and - the part that was missing - what
it cost per lead and per sale.

The API tier and nothing more. Rules live in `app.services.marketing`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireSales, RequireStaff
from app.models.marketing import CampaignChannel, CampaignStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.marketing import (
    AttachResult,
    CampaignCreate,
    CampaignRead,
    CampaignUpdate,
    CancelRequest,
    CompleteRequest,
    CostRequest,
    DeliveryRequest,
    DuplicateRequest,
    LaunchRequest,
    MarketingSummary,
    PerformanceReport,
    ScheduleRequest,
)
from app.services.marketing import MarketingService

router = APIRouter(prefix="/campaigns", tags=["marketing"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


# Literal paths first, or FastAPI parses "summary" as a UUID.


@router.get("/summary", response_model=MarketingSummary)
async def marketing_summary(db: DbSession, user: RequireStaff):
    """The strip above the list, including spend per lead and per sale."""
    return await MarketingService(db, user.tenant_id).summary()


@router.get("/performance", response_model=PerformanceReport)
async def performance(db: DbSession, user: RequireStaff):
    """What each campaign cost and what it produced.

    Revenue is traced lead to buyer to contract, so a campaign can be compared
    against what it earned rather than only against what it cost. That chain is
    the whole reason a lead record survives conversion.
    """
    return await MarketingService(db, user.tenant_id).performance()


@router.get("", response_model=Page[CampaignRead])
async def list_campaigns(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    status_filter: Annotated[
        CampaignStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    channel: Annotated[CampaignChannel | None, Query(description="One channel.")] = None,
):
    filters = {
        k: v for k, v in {"status": status_filter, "channel": channel}.items() if v is not None
    }
    items, total = await MarketingService(db, user.tenant_id).list_campaigns(params, **filters)
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.post("", response_model=CampaignRead, status_code=CREATED)
async def create_campaign(payload: CampaignCreate, db: DbSession, user: RequireSales):
    return await MarketingService(db, user.tenant_id).create_campaign(**payload.model_dump())


@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await MarketingService(db, user.tenant_id).get_campaign(campaign_id)


@router.patch("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    campaign_id: uuid.UUID, payload: CampaignUpdate, db: DbSession, user: RequireSales
):
    """Edit a campaign.

    Refused once it has run: the numbers hanging off a finished campaign were
    produced by what it actually said and cost, and editing that afterwards turns
    the report into fiction. Duplicate it instead.
    """
    return await MarketingService(db, user.tenant_id).update_campaign(
        campaign_id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/{campaign_id}/duplicate", response_model=CampaignRead, status_code=CREATED)
async def duplicate(
    campaign_id: uuid.UUID, payload: DuplicateRequest, db: DbSession, user: RequireSales
):
    """Copy a campaign as a fresh draft.

    Most campaigns are last year's with the dates changed. The copy keeps the
    content and the budget and deliberately drops the results - carrying those
    across would be the most misleading possible default.
    """
    return await MarketingService(db, user.tenant_id).duplicate(campaign_id, payload.code)


# --- the lifecycle ------------------------------------------------------------


@router.post("/{campaign_id}/schedule", response_model=CampaignRead)
async def schedule(
    campaign_id: uuid.UUID, payload: ScheduleRequest, db: DbSession, user: RequireSales
):
    """Put a date on it without committing the spend."""
    return await MarketingService(db, user.tenant_id).schedule(campaign_id, payload.scheduled_for)


@router.post("/{campaign_id}/launch", response_model=CampaignRead)
async def launch(campaign_id: uuid.UUID, payload: LaunchRequest, db: DbSession, user: RequireSales):
    """It is out: handed to the provider, or the sign is up.

    Email is refused without a subject line - one arriving with an empty subject
    is worse than one that does not arrive.
    """
    return await MarketingService(db, user.tenant_id).launch(
        campaign_id, starts_on=payload.starts_on
    )


@router.post("/{campaign_id}/delivery", response_model=CampaignRead)
async def record_delivery(
    campaign_id: uuid.UUID, payload: DeliveryRequest, db: DbSession, user: RequireSales
):
    """Take the numbers the provider reported back.

    Checked against each other rather than trusted: more opens than sends means a
    wrong open rate on a report nobody questions, which is worse than an error now.
    """
    return await MarketingService(db, user.tenant_id).record_delivery(
        campaign_id, **payload.model_dump(exclude_none=True)
    )


@router.post("/{campaign_id}/cost", response_model=CampaignRead)
async def record_cost(
    campaign_id: uuid.UUID, payload: CostRequest, db: DbSession, user: RequireSales
):
    """What it actually cost, as opposed to what was budgeted."""
    return await MarketingService(db, user.tenant_id).record_cost(campaign_id, payload.cost)


@router.post("/{campaign_id}/complete", response_model=CampaignRead)
async def complete(
    campaign_id: uuid.UUID, payload: CompleteRequest, db: DbSession, user: RequireSales
):
    """It has finished running."""
    return await MarketingService(db, user.tenant_id).complete(campaign_id, ends_on=payload.ends_on)


@router.post("/{campaign_id}/cancel", response_model=CampaignRead)
async def cancel(campaign_id: uuid.UUID, payload: CancelRequest, db: DbSession, user: RequireSales):
    """Stop a campaign. A reason is required - money was committed against it."""
    return await MarketingService(db, user.tenant_id).cancel(campaign_id, payload.reason)


@router.post("/{campaign_id}/attach-leads", response_model=AttachResult)
async def attach_leads(campaign_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Link leads whose campaign tag matches this campaign's code.

    Attribution used to be that comparison performed live in the report, so
    renaming a code detached every lead that came from it without anything going
    wrong anywhere visible. Doing it once and storing the link means a rename is
    just a rename.
    """
    attached = await MarketingService(db, user.tenant_id).attach_leads(campaign_id)
    return AttachResult(attached=attached)
