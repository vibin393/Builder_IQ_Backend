"""Sales: leads, the activity trail, and the realtors who send them.

The API tier and nothing more. Each handler reads the request, checks the role,
calls the service and shapes the response. There is no SQL in this file and no
business rule - those live in `app.services.crm` and `app.repositories.crm`.

Why there are more routes here than CRUD
----------------------------------------
Because a salesperson's actions are not CRUD. "Assign this to me", "mark it lost
because they went elsewhere on price", "log the call and book the next one" are
each one intention, and each is one request. Expressing them as PATCHes onto a
lead would push the rules into the caller, which is exactly where they get
forgotten.

Route order matters: the literal paths (/summary, /overdue, /unassigned) are
declared before /{lead_id}, or FastAPI would try to parse "summary" as a UUID.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireSales, RequireStaff
from app.models.crm import LeadRating, LeadSource, LeadStatus
from app.schemas.common import ArchiveRequest, Page, PageParams, page_params
from app.schemas.crm import (
    AssignRequest,
    BulkAssignRequest,
    BulkAssignResult,
    DuplicateCheck,
    DuplicateMatch,
    FollowUpRequest,
    LeadActivityCreate,
    LeadActivityRead,
    LeadCreate,
    LeadDetail,
    LeadRead,
    LeadSummary,
    LeadUpdate,
    LostRequest,
    RatingRequest,
    RealtorCreate,
    RealtorPerformance,
    RealtorRead,
    RealtorUpdate,
    ReopenRequest,
    StatusChange,
)
from app.services.crm import CrmService

router = APIRouter(tags=["crm"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


def _filters(
    status_filter: Annotated[
        LeadStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    source: Annotated[LeadSource | None, Query(description="How they found us.")] = None,
    rating: Annotated[LeadRating | None, Query(description="Hot, warm or cold.")] = None,
    assigned_to_id: Annotated[
        uuid.UUID | None, Query(description="Whose pipeline this is in.")
    ] = None,
    community_id: Annotated[uuid.UUID | None, Query(description="Community of interest.")] = None,
    realtor_id: Annotated[uuid.UUID | None, Query(description="Referring agent.")] = None,
) -> dict:
    """The filters every lead list shares, declared once.

    Returned as a dict of only what was supplied, because the repository treats
    None as "no filter" and passing all six every time would be noise.
    """
    supplied = {
        "status": status_filter,
        "source": source,
        "rating": rating,
        "assigned_to_id": assigned_to_id,
        "community_id": community_id,
        "realtor_id": realtor_id,
    }
    return {k: v for k, v in supplied.items() if v is not None}


LeadFilters = Annotated[dict, Depends(_filters)]


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


# =========================================================== the queues =====

# Declared before /leads/{lead_id} so these words are not parsed as UUIDs.


@router.get("/leads/summary", response_model=LeadSummary)
async def lead_summary(db: DbSession, user: RequireStaff):
    """The counters above the list, and the funnel behind them.

    One request rather than four, because a dashboard that fires four is four
    times as likely to show numbers taken a second apart.
    """
    return await CrmService(db, user.tenant_id).summary()


@router.get("/leads/overdue", response_model=Page[LeadRead])
async def overdue_leads(db: DbSession, user: RequireStaff, params: PageQuery, filters: LeadFilters):
    """The call list: open leads whose follow-up date has passed, worst first.

    Ordered by how long they have been neglected rather than by when they came
    in, because the top of this list is what somebody should be ringing now.
    """
    items, total = await CrmService(db, user.tenant_id).list_overdue(params, **filters)
    return _page(items, params, total)


@router.get("/leads/unassigned", response_model=Page[LeadRead])
async def unassigned_leads(
    db: DbSession, user: RequireStaff, params: PageQuery, filters: LeadFilters
):
    """Open leads with nobody's name on them - oldest first, since the oldest
    unowned lead is the one most likely to have gone cold."""
    items, total = await CrmService(db, user.tenant_id).list_unassigned(params, **filters)
    return _page(items, params, total)


@router.get("/leads/open", response_model=Page[LeadRead])
async def open_leads(db: DbSession, user: RequireStaff, params: PageQuery, filters: LeadFilters):
    """The live pipeline: everything not yet converted or lost."""
    items, total = await CrmService(db, user.tenant_id).list_open(params, **filters)
    return _page(items, params, total)


# ================================================================ leads =====


@router.get("/leads", response_model=Page[LeadRead])
async def list_leads(db: DbSession, user: RequireStaff, params: PageQuery, filters: LeadFilters):
    """Every lead, newest first, paged and searchable.

    Free text matches name, email, either phone number, campaign and town - the
    things somebody actually types when hunting for a lead they half remember.
    """
    items, total = await CrmService(db, user.tenant_id).list_leads(params, **filters)
    return _page(items, params, total)


@router.post("/leads/check-duplicates", response_model=list[DuplicateMatch])
async def check_duplicates(payload: DuplicateCheck, db: DbSession, user: RequireStaff):
    """Existing leads that look like this person, before the form is submitted.

    Advisory by design. The same family enquiring about two plans is legitimate,
    and a hard block on a walk-in standing at the desk is worse than a duplicate
    row - so this warns and lets a person decide.
    """
    return await CrmService(db, user.tenant_id).check_duplicates(**payload.model_dump())


@router.post("/leads", response_model=LeadDetail, status_code=CREATED)
async def create_lead(payload: LeadCreate, db: DbSession, user: RequireStaff):
    """Capture an enquiry.

    Open to any staff user rather than sales only, so website and walk-in intake
    can post here without anybody being handed a sales role to answer a phone.
    """
    return await CrmService(db, user.tenant_id).create_lead(
        actor_id=user.id, **payload.model_dump()
    )


@router.get("/leads/{lead_id}", response_model=LeadDetail)
async def get_lead(lead_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await CrmService(db, user.tenant_id).get_lead(lead_id)


@router.patch("/leads/{lead_id}", response_model=LeadDetail)
async def update_lead(lead_id: uuid.UUID, payload: LeadUpdate, db: DbSession, user: RequireSales):
    """Edit a lead's details. Only the fields supplied change.

    Status is not settable here - it moves through /status, which checks the
    transition is legal and writes the history entry.
    """
    return await CrmService(db, user.tenant_id).update_lead(
        lead_id, actor_id=user.id, **payload.model_dump(exclude_unset=True)
    )


@router.get("/leads/{lead_id}/duplicates", response_model=list[DuplicateMatch])
async def duplicates_of(lead_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Other leads that look like this one - the merge candidates."""
    service = CrmService(db, user.tenant_id)
    lead = await service.get_lead(lead_id)
    return await service.check_duplicates(
        email=lead.email, phone=lead.phone, mobile=lead.mobile, exclude_id=lead.id
    )


# ====================================================== working a lead =====


@router.post("/leads/{lead_id}/status", response_model=LeadDetail)
async def change_status(
    lead_id: uuid.UUID, payload: StatusChange, db: DbSession, user: RequireSales
):
    """Move a lead through the funnel.

    Refuses moves that skip qualification, which is where the builder finds out
    whether the buyer can actually pay. The refusal names the legal next steps
    rather than just saying no.
    """
    return await CrmService(db, user.tenant_id).set_status(
        lead_id,
        payload.status,
        actor_id=user.id,
        reason=payload.reason,
        lost_to=payload.lost_to,
    )


@router.post("/leads/{lead_id}/assign", response_model=LeadDetail)
async def assign_lead(
    lead_id: uuid.UUID, payload: AssignRequest, db: DbSession, user: RequireSales
):
    """Put somebody's name on a lead. A null id unassigns it."""
    return await CrmService(db, user.tenant_id).assign(
        lead_id, payload.assigned_to_id, actor_id=user.id
    )


@router.post("/leads/{lead_id}/claim", response_model=LeadDetail)
async def claim_lead(lead_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Take a lead yourself.

    Its own route rather than /assign with your own id, because "I am picking
    this up" is the single most common action on an unassigned lead and should
    cost one click.
    """
    return await CrmService(db, user.tenant_id).assign(lead_id, user.id, actor_id=user.id)


@router.post("/leads/bulk-assign", response_model=BulkAssignResult)
async def bulk_assign(payload: BulkAssignRequest, db: DbSession, user: RequireSales):
    """Distribute a batch of leads.

    Reports what happened rather than failing on the first problem: a manager
    handing out forty leads should not lose thirty-nine because one had already
    converted.
    """
    return await CrmService(db, user.tenant_id).bulk_assign(
        payload.lead_ids, payload.assigned_to_id, actor_id=user.id
    )


@router.post("/leads/{lead_id}/follow-up", response_model=LeadDetail)
async def schedule_follow_up(
    lead_id: uuid.UUID, payload: FollowUpRequest, db: DbSession, user: RequireSales
):
    """Set the date this lead is next to be chased."""
    return await CrmService(db, user.tenant_id).schedule_follow_up(
        lead_id, payload.next_follow_up, actor_id=user.id, note=payload.note
    )


@router.post("/leads/{lead_id}/rating", response_model=LeadDetail)
async def set_rating(lead_id: uuid.UUID, payload: RatingRequest, db: DbSession, user: RequireSales):
    """Hot, warm or cold - the salesperson's read on whether this will close."""
    return await CrmService(db, user.tenant_id).set_rating(
        lead_id, payload.rating, actor_id=user.id
    )


# ============================================================== endings =====


@router.post("/leads/{lead_id}/lost", response_model=LeadDetail)
async def mark_lost(lead_id: uuid.UUID, payload: LostRequest, db: DbSession, user: RequireSales):
    """Close a lead off, with the reason that makes it useful later.

    The reason is required by the schema. "We lost it" is not information; "they
    went elsewhere on price" changes what the business does next quarter.
    """
    return await CrmService(db, user.tenant_id).mark_lost(
        lead_id, payload.reason, lost_to=payload.lost_to, actor_id=user.id
    )


@router.post("/leads/{lead_id}/reopen", response_model=LeadDetail)
async def reopen_lead(
    lead_id: uuid.UUID, payload: ReopenRequest, db: DbSession, user: RequireSales
):
    """Bring a lost lead back. Returns to contacted, not new - somebody has
    already spoken to them and the queues should not pretend otherwise."""
    return await CrmService(db, user.tenant_id).reopen(lead_id, actor_id=user.id, note=payload.note)


@router.post("/leads/{lead_id}/convert", response_model=LeadDetail)
async def convert_lead(lead_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Turn a lead into a buyer, keeping the link back to this record.

    That link is why the lead survives conversion: without it the business can
    only answer "which marketing produced enquiries", not "which produced
    closings", and those have different answers.
    """
    return await CrmService(db, user.tenant_id).convert(lead_id, actor_id=user.id)


@router.post("/leads/{lead_id}/archive", response_model=LeadDetail)
async def archive_lead(
    lead_id: uuid.UUID, payload: ArchiveRequest, db: DbSession, user: RequireSales
):
    """Hide a junk enquiry - the bot submissions and wrong numbers.

    Refused on a converted lead: that record is the only trace of where a real
    buyer came from.
    """
    return await CrmService(db, user.tenant_id).archive_lead(lead_id, user.id, payload.reason)


@router.post("/leads/{lead_id}/restore", response_model=LeadDetail)
async def restore_lead(lead_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await CrmService(db, user.tenant_id).restore_lead(lead_id)


# =========================================================== activities =====


@router.get("/leads/{lead_id}/activities", response_model=list[LeadActivityRead])
async def list_activities(lead_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """The full trail, newest first - including the entries the system wrote."""
    return await CrmService(db, user.tenant_id).list_activities(lead_id)


@router.post("/leads/{lead_id}/activities", response_model=LeadActivityRead, status_code=CREATED)
async def log_activity(
    lead_id: uuid.UUID, payload: LeadActivityCreate, db: DbSession, user: RequireSales
):
    """Record a touch, and let it move the lead on.

    A real contact on a new lead moves it to contacted; an outcome of "booked"
    moves it to appointment; `last_contacted_on` advances. The next follow-up can
    be set in the same request, because logging a call and booking the next one
    is one action to the person doing it.
    """
    return await CrmService(db, user.tenant_id).log_activity(
        lead_id,
        actor_id=user.id,
        activity_type=payload.activity_type,
        summary=payload.summary,
        occurred_on=payload.occurred_on,
        outcome=payload.outcome,
        next_follow_up=payload.next_follow_up,
    )


# ============================================================= realtors =====


@router.get("/realtors", response_model=list[RealtorRead])
async def list_realtors(db: DbSession, user: RequireStaff):
    """Every agent, unpaged - callers use this to fill the picker on a lead form,
    and a picker missing half its options is worse than a slow one."""
    return await CrmService(db, user.tenant_id).list_realtors()


@router.get("/realtors/search", response_model=Page[RealtorRead])
async def search_realtors(db: DbSession, user: RequireStaff, params: PageQuery):
    items, total = await CrmService(db, user.tenant_id).list_realtors(params)
    return _page(items, params, total)


@router.get("/realtors/performance", response_model=list[RealtorPerformance])
async def realtor_performance(db: DbSession, user: RequireStaff):
    """Which agents are actually worth cultivating.

    Ranked by conversions rather than enquiries: the agent who sends three buyers
    outranks the one who sends thirty browsers, and a raw count would say the
    opposite.
    """
    return await CrmService(db, user.tenant_id).realtor_performance()


@router.post("/realtors", response_model=RealtorRead, status_code=CREATED)
async def create_realtor(payload: RealtorCreate, db: DbSession, user: RequireSales):
    return await CrmService(db, user.tenant_id).create_realtor(
        actor_id=user.id, **payload.model_dump()
    )


@router.get("/realtors/{realtor_id}", response_model=RealtorRead)
async def get_realtor(realtor_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await CrmService(db, user.tenant_id).get_realtor(realtor_id)


@router.patch("/realtors/{realtor_id}", response_model=RealtorRead)
async def update_realtor(
    realtor_id: uuid.UUID, payload: RealtorUpdate, db: DbSession, user: RequireSales
):
    return await CrmService(db, user.tenant_id).update_realtor(
        realtor_id, actor_id=user.id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/realtors/{realtor_id}/archive", response_model=RealtorRead)
async def archive_realtor(
    realtor_id: uuid.UUID, payload: ArchiveRequest, db: DbSession, user: RequireSales
):
    """Retire an agent so they stop appearing in the picker.

    Every lead they ever sent keeps pointing at them - that is the difference
    from deleting. Refused while they hold open leads, because there is a
    commission riding on each one.
    """
    return await CrmService(db, user.tenant_id).archive_realtor(realtor_id, user.id, payload.reason)


@router.post("/realtors/{realtor_id}/restore", response_model=RealtorRead)
async def restore_realtor(realtor_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await CrmService(db, user.tenant_id).restore_realtor(realtor_id)
