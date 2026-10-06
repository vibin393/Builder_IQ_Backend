"""Meetings and correspondence.

A meeting is an agenda that becomes minutes, and minutes that carry actions — each with
an owner and a date, so nothing agreed in a room is quietly dropped. The two things that
make it more than a notepad: an action can be **completed** (the queue the old screen
could show but never close), and the open actions can be **carried forward** into a
follow-up meeting, so a rolling coordination series never loses track of what is still
owed. Correspondence is the audit trail of what was formally said to whom, with the ones
that demand a reply tracked as a chase list.

  POST /meetings/items/{id}/complete   close an action agreed in a meeting
  POST /meetings/{id}/follow-up        open the next meeting, carrying the open actions
"""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    DbSession,
    RequireDesignTeam,
    RequireStaff,
    get_tenant_scoped,
)
from app.models.communication import (
    Correspondence,
    Meeting,
    MeetingItem,
    MeetingStatus,
    MeetingType,
)
from app.models.job import Job
from app.schemas.meeting import (
    CorrespondenceCreate,
    CorrespondenceRead,
    MeetingCreate,
    MeetingItemCreate,
    MeetingItemUpdate,
    MeetingRead,
    MeetingSummary,
    MeetingUpdate,
)

router = APIRouter(tags=["meetings"])


# --- helpers ------------------------------------------------------------------


async def _load_meeting(db, tenant_id: uuid.UUID, m_id: uuid.UUID) -> Meeting:
    obj = await db.scalar(
        select(Meeting)
        .options(selectinload(Meeting.items))
        .where(Meeting.id == m_id, Meeting.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return obj


# --- analytics ----------------------------------------------------------------


@router.get("/meetings/summary", response_model=MeetingSummary)
async def summary(db: DbSession, user: RequireStaff, job_id: uuid.UUID | None = None):
    """The board: how many meetings, how many still to hold, how many actions are open
    and how many have slipped, plus the correspondence still awaiting a reply."""
    tenant = user.tenant_id
    m_stmt = (
        select(Meeting).options(selectinload(Meeting.items)).where(Meeting.tenant_id == tenant)
    )
    if job_id is not None:
        m_stmt = m_stmt.where(Meeting.job_id == job_id)
    meetings = list(await db.scalars(m_stmt))

    scheduled = held = open_actions = overdue_actions = 0
    by_type: dict[str, int] = {}
    for m in meetings:
        by_type[m.meeting_type.value] = by_type.get(m.meeting_type.value, 0) + 1
        if m.status is MeetingStatus.SCHEDULED:
            scheduled += 1
        elif m.status is MeetingStatus.HELD:
            held += 1
        open_actions += m.open_actions
        overdue_actions += m.overdue_actions

    c_stmt = select(Correspondence).where(Correspondence.tenant_id == tenant)
    if job_id is not None:
        c_stmt = c_stmt.where(Correspondence.job_id == job_id)
    corr = list(await db.scalars(c_stmt))
    awaiting = sum(1 for c in corr if c.is_awaiting_response)
    overdue_corr = sum(1 for c in corr if c.is_overdue)

    return MeetingSummary(
        meetings=len(meetings),
        scheduled=scheduled,
        held=held,
        open_actions=open_actions,
        overdue_actions=overdue_actions,
        correspondence=len(corr),
        awaiting_response=awaiting,
        overdue_correspondence=overdue_corr,
        by_type=by_type,
    )


# --- meetings -----------------------------------------------------------------


@router.get("/meetings", response_model=list[MeetingRead])
async def list_meetings(
    db: DbSession,
    user: RequireDesignTeam,
    job_id: uuid.UUID | None = None,
    status_filter: MeetingStatus | None = Query(default=None, alias="status"),
    meeting_type: MeetingType | None = Query(default=None, alias="type"),
):
    stmt = (
        select(Meeting)
        .options(selectinload(Meeting.items))
        .where(Meeting.tenant_id == user.tenant_id)
    )
    if job_id is not None:
        stmt = stmt.where(Meeting.job_id == job_id)
    if status_filter is not None:
        stmt = stmt.where(Meeting.status == status_filter)
    if meeting_type is not None:
        stmt = stmt.where(Meeting.meeting_type == meeting_type)
    return list(await db.scalars(stmt.order_by(Meeting.meeting_date.desc())))


@router.post("/meetings", response_model=MeetingRead, status_code=status.HTTP_201_CREATED)
async def create_meeting(payload: MeetingCreate, db: DbSession, user: RequireStaff):
    if payload.job_id:
        await get_tenant_scoped(db, Job, user.tenant_id, payload.job_id)
    obj = Meeting(tenant_id=user.tenant_id, chaired_by_id=user.id, **payload.model_dump())
    db.add(obj)
    await db.commit()
    return await _load_meeting(db, user.tenant_id, obj.id)


@router.get("/meetings/{meeting_id}", response_model=MeetingRead)
async def get_meeting(meeting_id: uuid.UUID, db: DbSession, user: RequireDesignTeam):
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.patch("/meetings/{meeting_id}", response_model=MeetingRead)
async def update_meeting(
    meeting_id: uuid.UUID, payload: MeetingUpdate, db: DbSession, user: RequireStaff
):
    obj = await _load_meeting(db, user.tenant_id, meeting_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("job_id"):
        await get_tenant_scoped(db, Job, user.tenant_id, data["job_id"])
    for field, value in data.items():
        setattr(obj, field, value)
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.post("/meetings/{meeting_id}/hold", response_model=MeetingRead)
async def mark_meeting_held(meeting_id: uuid.UUID, db: DbSession, user: RequireStaff):
    obj = await _load_meeting(db, user.tenant_id, meeting_id)
    if obj.status is MeetingStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A cancelled meeting cannot be held."
        )
    obj.status = MeetingStatus.HELD
    if obj.meeting_date is None:
        obj.meeting_date = date.today()
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.post("/meetings/{meeting_id}/cancel", response_model=MeetingRead)
async def cancel_meeting(meeting_id: uuid.UUID, db: DbSession, user: RequireStaff):
    obj = await _load_meeting(db, user.tenant_id, meeting_id)
    if obj.status is MeetingStatus.HELD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A meeting that was held cannot be cancelled; its minutes stand.",
        )
    obj.status = MeetingStatus.CANCELLED
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.post(
    "/meetings/{meeting_id}/follow-up",
    response_model=MeetingRead,
    status_code=status.HTTP_201_CREATED,
)
async def schedule_follow_up(
    meeting_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    meeting_date: date | None = Query(default=None),
):
    """Open the next meeting in the series and carry the open actions into it.

    This is what stops a rolling coordination meeting from losing track: every action
    still owed comes across as an item on the new agenda, linked back to where it was
    first raised, so an action that has rolled over three meetings is visible as one.
    """
    old = await _load_meeting(db, user.tenant_id, meeting_id)

    new = Meeting(
        tenant_id=user.tenant_id,
        job_id=old.job_id,
        title=f"{old.title} (follow-up)",
        meeting_type=old.meeting_type,
        meeting_date=meeting_date,
        location=old.location,
        attendees=old.attendees,
        status=MeetingStatus.SCHEDULED,
        chaired_by_id=user.id,
        previous_meeting_id=old.id,
    )
    db.add(new)
    await db.flush()

    seq = 1
    for it in old.items:
        if it.is_open_action:
            db.add(
                MeetingItem(
                    tenant_id=user.tenant_id,
                    meeting_id=new.id,
                    sequence=seq,
                    topic=it.topic,
                    discussion=it.discussion,
                    is_action=True,
                    owner_id=it.owner_id,
                    due_on=it.due_on,
                    carried_from_id=it.id,
                )
            )
            seq += 1

    await db.commit()
    return await _load_meeting(db, user.tenant_id, new.id)


# --- meeting items ------------------------------------------------------------


@router.post("/meetings/{meeting_id}/items", response_model=MeetingRead)
async def add_meeting_item(
    meeting_id: uuid.UUID, payload: MeetingItemCreate, db: DbSession, user: RequireStaff
):
    meeting = await _load_meeting(db, user.tenant_id, meeting_id)
    db.add(
        MeetingItem(
            tenant_id=user.tenant_id,
            meeting_id=meeting_id,
            sequence=len(meeting.items) + 1,
            **payload.model_dump(),
        )
    )
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.patch("/meetings/{meeting_id}/items/{item_id}", response_model=MeetingRead)
async def update_meeting_item(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: MeetingItemUpdate,
    db: DbSession,
    user: RequireStaff,
):
    meeting = await _load_meeting(db, user.tenant_id, meeting_id)
    item = next((i for i in meeting.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.delete("/meetings/{meeting_id}/items/{item_id}", response_model=MeetingRead)
async def remove_meeting_item(
    meeting_id: uuid.UUID, item_id: uuid.UUID, db: DbSession, user: RequireStaff
):
    meeting = await _load_meeting(db, user.tenant_id, meeting_id)
    item = next((i for i in meeting.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    await db.delete(item)
    await db.commit()
    return await _load_meeting(db, user.tenant_id, meeting_id)


@router.post("/meetings/items/{item_id}/complete", response_model=MeetingRead)
async def complete_action(item_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Close an action agreed in a meeting. The queue the old screen could count but
    never clear."""
    item = await get_tenant_scoped(db, MeetingItem, user.tenant_id, item_id)
    if not item.is_action:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is a discussion item, not an action; there is nothing to complete.",
        )
    item.completed_on = item.completed_on or date.today()
    await db.commit()
    return await _load_meeting(db, user.tenant_id, item.meeting_id)


@router.post("/meetings/items/{item_id}/reopen", response_model=MeetingRead)
async def reopen_action(item_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Reopen an action that was marked done but was not, in fact, done."""
    item = await get_tenant_scoped(db, MeetingItem, user.tenant_id, item_id)
    item.completed_on = None
    await db.commit()
    return await _load_meeting(db, user.tenant_id, item.meeting_id)


# --- correspondence -----------------------------------------------------------


@router.get("/correspondence", response_model=list[CorrespondenceRead])
async def list_correspondence(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    kind: str | None = None,
    awaiting_only: bool = False,
):
    stmt = select(Correspondence).where(Correspondence.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Correspondence.job_id == job_id)
    if kind is not None:
        stmt = stmt.where(Correspondence.kind == kind)
    rows = list(await db.scalars(stmt.order_by(Correspondence.created_at.desc())))
    if awaiting_only:
        rows = [c for c in rows if c.is_awaiting_response]
    return rows


@router.post(
    "/correspondence", response_model=CorrespondenceRead, status_code=status.HTTP_201_CREATED
)
async def log_correspondence(payload: CorrespondenceCreate, db: DbSession, user: RequireStaff):
    data = payload.model_dump()
    data["occurred_on"] = data["occurred_on"] or date.today()
    obj = Correspondence(tenant_id=user.tenant_id, author_id=user.id, **data)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/correspondence/{corr_id}/respond", response_model=CorrespondenceRead)
async def mark_responded(corr_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Record that the reply this correspondence was waiting on has now happened."""
    obj = await get_tenant_scoped(db, Correspondence, user.tenant_id, corr_id)
    obj.responded_on = obj.responded_on or date.today()
    obj.requires_response = True
    await db.commit()
    await db.refresh(obj)
    return obj
