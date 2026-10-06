"""Schemas for meetings, their agenda/action items, and correspondence."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.communication import MeetingStatus, MeetingType


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Meeting items ------------------------------------------------------------


class MeetingItemCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    discussion: str | None = None
    is_action: bool = False
    owner_id: uuid.UUID | None = None
    due_on: date | None = None


class MeetingItemUpdate(BaseModel):
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    discussion: str | None = None
    is_action: bool | None = None
    owner_id: uuid.UUID | None = None
    due_on: date | None = None


class MeetingItemRead(ORMModel):
    id: uuid.UUID
    sequence: int
    topic: str
    discussion: str | None
    is_action: bool
    owner_id: uuid.UUID | None
    due_on: date | None
    completed_on: date | None
    carried_from_id: uuid.UUID | None
    is_open_action: bool
    is_overdue: bool


# --- Meetings -----------------------------------------------------------------


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    meeting_type: MeetingType = MeetingType.COORDINATION
    job_id: uuid.UUID | None = None
    meeting_date: date | None = None
    location: str | None = None
    attendees: str | None = None
    notes: str | None = None


class MeetingUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    meeting_type: MeetingType | None = None
    job_id: uuid.UUID | None = None
    meeting_date: date | None = None
    location: str | None = None
    attendees: str | None = None
    notes: str | None = None


class MeetingRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID | None
    title: str
    meeting_type: MeetingType
    meeting_date: date | None
    location: str | None
    attendees: str | None
    status: MeetingStatus
    notes: str | None
    chaired_by_id: uuid.UUID | None
    previous_meeting_id: uuid.UUID | None
    item_count: int
    open_actions: int
    overdue_actions: int
    items: list[MeetingItemRead] = []


# --- Correspondence -----------------------------------------------------------


class CorrespondenceCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str | None = None
    kind: str = "note"
    direction: str = "outbound"
    job_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    buyer_id: uuid.UUID | None = None
    occurred_on: date | None = None
    requires_response: bool = False
    response_due: date | None = None


class CorrespondenceRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID | None
    subject: str
    body: str | None
    kind: str
    direction: str
    vendor_id: uuid.UUID | None
    buyer_id: uuid.UUID | None
    author_id: uuid.UUID | None
    occurred_on: date | None
    requires_response: bool
    response_due: date | None
    responded_on: date | None
    is_awaiting_response: bool
    is_overdue: bool


# --- Analytics ----------------------------------------------------------------


class MeetingSummary(BaseModel):
    meetings: int
    scheduled: int
    held: int
    open_actions: int
    overdue_actions: int
    correspondence: int
    awaiting_response: int      # correspondence that demanded a reply and hasn't had one
    overdue_correspondence: int
    by_type: dict[str, int]
