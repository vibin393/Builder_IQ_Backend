"""Automated lead capture: the public endpoint, and the screens that watch it.

This module has two halves that could hardly be less alike.

The **public half** is one route, unauthenticated, reachable from the open
internet. It answers a webhook or a website form. It never raises for a bad
payload: a 500 teaches a portal either to give up or to retry forever, and both
lose leads. Anything it cannot read is stored and flagged, and the caller is told
the enquiry arrived - which is true.

The **private half** is ordinary authenticated configuration and monitoring:
channels, their keys, the failed queue, and replay.

Keeping them in one file is deliberate. The public route's defences only make
sense next to the settings that configure them, and a reviewer should not have to
open two files to check that the thing facing the internet is the thing being rate
limited.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.models.intake import IntakeStatus
from app.schemas.common import ArchiveRequest, Page, PageParams, page_params
from app.schemas.intake import (
    IntakeAck,
    IntakeEventRead,
    IntakeHealth,
    IntakeSourceCreate,
    IntakeSourceCreated,
    IntakeSourceRead,
    IntakeSourceUpdate,
    ReplayResult,
)
from app.services.intake import IntakeService, rate_limited, resolve_source

router = APIRouter(tags=["intake"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


def _caller(request: Request) -> str:
    """Who is calling, for the rate limiter.

    `X-Forwarded-For` is trusted only for its first entry, and only because this
    will sit behind a reverse proxy in production. It is spoofable by a direct
    caller, which is acceptable here: the limiter is a brake on volume, not an
    access control, and the honeypot and key do the real work.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ========================================================= public route =====


@router.post(
    "/intake/hooks/{code}",
    response_model=IntakeAck,
    summary="Receive a lead from a website form, portal or ad platform",
)
async def receive_lead(
    code: str,
    payload: dict,
    request: Request,
    db: DbSession,
    x_intake_key: Annotated[str | None, Header()] = None,
):
    """Take one inbound enquiry.

    Authenticated by the channel key in `X-Intake-Key` - or, for platforms that
    cannot set headers, a `key` field in the body. The key identifies both the
    channel and the builder it belongs to, which is why this route needs no login.

    Always answers 200 when the payload was stored, whatever became of it. The
    sender's only question is "did it arrive", and the answer is yes; whether we
    could read it is our problem, and it is on a queue.
    """
    key = x_intake_key or (payload.get("key") if isinstance(payload, dict) else None)
    # Never let the credential become part of the lead's notes.
    body = {k: v for k, v in payload.items() if k != "key"} if isinstance(payload, dict) else {}

    # Resolved before the rate limit, because the limit is configured per channel.
    # A wrong key is a 404 and costs one indexed lookup.
    source = await resolve_source(db, code, key)

    if rate_limited(source.id, _caller(request), source.rate_limit_per_minute):
        return IntakeAck(
            received=False,
            status=IntakeStatus.REJECTED,
            detail="Too many requests from this address. Slow down and resend.",
        )

    service = IntakeService(db, source.tenant_id)
    result = await service.receive(source, body, remote_ip=_caller(request))
    return IntakeAck(received=result.accepted, status=result.status, detail=result.message)


# ====================================================== watching it work =====


@router.get("/intake/health", response_model=IntakeHealth)
async def intake_health(db: DbSession, user: RequireStaff):
    """Is every channel still delivering, and is anything stuck.

    The number to watch is `needs_attention`: payloads we hold and cannot read.
    Each one is a real enquiry from a real person that nobody has replied to.
    """
    return await IntakeService(db, user.tenant_id).health()


@router.get("/intake/events", response_model=Page[IntakeEventRead])
async def list_events(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    status_filter: Annotated[
        IntakeStatus | None, Query(alias="status", description="Only this outcome.")
    ] = None,
    source_id: Annotated[uuid.UUID | None, Query(description="Only this channel.")] = None,
):
    """Everything that has arrived, newest first."""
    filters: dict = {}
    if status_filter is not None:
        filters["status"] = status_filter
    if source_id is not None:
        filters["source_id"] = source_id
    items, total = await IntakeService(db, user.tenant_id).list_events(params, **filters)
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/intake/events/failed", response_model=Page[IntakeEventRead])
async def failed_events(db: DbSession, user: RequireStaff, params: PageQuery):
    """The queue that has to be cleared: enquiries we received and could not read.

    Each row carries the payload exactly as it arrived and the reason it failed, so
    the fix is either a corrected field map or a lead captured by hand from what is
    on the screen. Either way nothing was lost.
    """
    items, total = await IntakeService(db, user.tenant_id).failed_events(params)
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.post("/intake/events/{event_id}/replay", response_model=ReplayResult)
async def replay_event(event_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Run a stored payload through the adapter again, after fixing the mapping.

    This is the reason the raw payload is kept. Correct the field map, replay, and
    the enquiry becomes the lead it should have been.
    """
    result = await IntakeService(db, user.tenant_id).replay(event_id)
    return ReplayResult(status=result.status, lead_id=result.lead_id, detail=result.message)


# ======================================================= configuration =====


@router.get("/intake/sources", response_model=list[IntakeSourceRead])
async def list_sources(db: DbSession, user: RequireStaff):
    return await IntakeService(db, user.tenant_id).list_sources()


@router.post("/intake/sources", response_model=IntakeSourceCreated, status_code=CREATED)
async def create_source(
    payload: IntakeSourceCreate, db: DbSession, admin: RequireAdmin, request: Request
):
    """Add a channel and issue its key.

    Admin only: a channel key is a credential that can create records in this
    builder's account without a login, and handing those out is not a sales task.
    The key is in this response and in no other, because it is stored hashed.
    """
    source, key = await IntakeService(db, admin.tenant_id).create_source(
        actor_id=admin.id, **payload.model_dump()
    )
    return IntakeSourceCreated(
        source=IntakeSourceRead.model_validate(source),
        key=key,
        post_url=str(request.base_url).rstrip("/") + f"/api/v1/intake/hooks/{source.code}",
    )


@router.get("/intake/sources/{source_id}", response_model=IntakeSourceRead)
async def get_source(source_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await IntakeService(db, user.tenant_id).get_source(source_id)


@router.patch("/intake/sources/{source_id}", response_model=IntakeSourceRead)
async def update_source(
    source_id: uuid.UUID, payload: IntakeSourceUpdate, db: DbSession, admin: RequireAdmin
):
    return await IntakeService(db, admin.tenant_id).update_source(
        source_id, actor_id=admin.id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/intake/sources/{source_id}/rotate-key", response_model=IntakeSourceCreated)
async def rotate_key(source_id: uuid.UUID, db: DbSession, admin: RequireAdmin, request: Request):
    """Issue a new key and stop the old one working immediately.

    No grace period. A key is rotated because it leaked, and a window in which the
    leaked key still works is the thing being closed.
    """
    source, key = await IntakeService(db, admin.tenant_id).rotate_key(source_id, actor_id=admin.id)
    return IntakeSourceCreated(
        source=IntakeSourceRead.model_validate(source),
        key=key,
        post_url=str(request.base_url).rstrip("/") + f"/api/v1/intake/hooks/{source.code}",
    )


@router.post("/intake/sources/{source_id}/pause", response_model=IntakeSourceRead)
async def pause_source(source_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    """Stop accepting from this channel without losing its configuration.

    What you reach for when a form starts taking bot traffic: the key stays valid,
    the history stays, and nothing new is captured until it is resumed.
    """
    return await IntakeService(db, admin.tenant_id).set_active(source_id, False, actor_id=admin.id)


@router.post("/intake/sources/{source_id}/resume", response_model=IntakeSourceRead)
async def resume_source(source_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    return await IntakeService(db, admin.tenant_id).set_active(source_id, True, actor_id=admin.id)


@router.post("/intake/sources/{source_id}/archive", response_model=IntakeSourceRead)
async def archive_source(
    source_id: uuid.UUID, payload: ArchiveRequest, db: DbSession, admin: RequireAdmin
):
    """Retire a channel. Its events stay - they are the record of every lead it
    ever produced."""
    return await IntakeService(db, admin.tenant_id).archive_source(
        source_id, admin.id, payload.reason
    )
