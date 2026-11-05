"""Request and response shapes for comments and messages."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ---- comments ----
class CommentCreate(BaseModel):
    job_id: uuid.UUID
    body: str = Field(min_length=1, max_length=8000)
    parent_id: uuid.UUID | None = None
    entity_type: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None


class CommentRead(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    entity_type: str | None
    entity_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    author_id: uuid.UUID | None
    author_name: str
    body: str
    created_at: datetime


# ---- messages ----
class MessageCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    recipient_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    job_id: uuid.UUID | None = None


class ReplyCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class RecipientRead(BaseModel):
    user_id: uuid.UUID
    user_name: str
    read_at: datetime | None


class MessageRead(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    subject: str
    body: str
    sender_id: uuid.UUID | None
    sender_name: str
    job_id: uuid.UUID | None
    created_at: datetime
    recipients: list[RecipientRead]


class ThreadSummary(BaseModel):
    thread_id: uuid.UUID
    subject: str
    snippet: str
    last_at: datetime
    last_sender: str
    message_count: int
    unread: int
    participants: list[str]


class InboxSummary(BaseModel):
    threads: int
    unread: int
