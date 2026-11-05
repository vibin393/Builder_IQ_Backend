"""Messaging: comments on records, and an internal message inbox."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequireStaff
from app.schemas.messaging import (
    CommentCreate,
    CommentRead,
    InboxSummary,
    MessageCreate,
    MessageRead,
    ReplyCreate,
    ThreadSummary,
)
from app.services.messaging import MessagingService

router = APIRouter(tags=["messaging"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


# ---- comments ----
@router.get("/comments", response_model=list[CommentRead])
async def list_comments(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID, Query(description="One home (required).")],
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """Threaded comments on a home, or on a record within it."""
    return await MessagingService(db, user.tenant_id).list_comments(
        job_id=job_id, entity_type=entity_type, entity_id=entity_id
    )


@router.post("/comments", response_model=CommentRead, status_code=CREATED)
async def add_comment(payload: CommentCreate, db: DbSession, user: RequireStaff):
    return await MessagingService(db, user.tenant_id).add_comment(
        author_id=user.id, **payload.model_dump()
    )


@router.delete("/comments/{comment_id}", status_code=NO_CONTENT)
async def delete_comment(comment_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await MessagingService(db, user.tenant_id).delete_comment(comment_id, actor_id=user.id)


# ---- messages ----
@router.get("/messages/summary", response_model=InboxSummary)
async def inbox_summary(db: DbSession, user: RequireStaff):
    data = await MessagingService(db, user.tenant_id).inbox_summary(user_id=user.id)
    return InboxSummary(**data)


@router.get("/messages/inbox", response_model=list[ThreadSummary])
async def inbox(db: DbSession, user: RequireStaff):
    """Your inbox — one row per thread, most recent first."""
    return await MessagingService(db, user.tenant_id).inbox(user_id=user.id)


@router.get("/messages/sent", response_model=list[ThreadSummary])
async def sent(db: DbSession, user: RequireStaff):
    return await MessagingService(db, user.tenant_id).sent(user_id=user.id)


@router.get("/messages/threads/{thread_id}", response_model=list[MessageRead])
async def read_thread(thread_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Every message in a thread, oldest first. Marks your copies as read."""
    return await MessagingService(db, user.tenant_id).thread(thread_id, user_id=user.id)


@router.post("/messages", response_model=MessageRead, status_code=CREATED)
async def send_message(payload: MessageCreate, db: DbSession, user: RequireStaff):
    return await MessagingService(db, user.tenant_id).send(
        sender_id=user.id, **payload.model_dump()
    )


@router.post("/messages/threads/{thread_id}/reply", response_model=MessageRead, status_code=CREATED)
async def reply(thread_id: uuid.UUID, payload: ReplyCreate, db: DbSession, user: RequireStaff):
    return await MessagingService(db, user.tenant_id).reply(
        thread_id=thread_id, sender_id=user.id, body=payload.body
    )
