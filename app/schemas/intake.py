"""Request and response shapes for automated lead capture."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.crm import LeadSource
from app.models.intake import IntakeKind, IntakeStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================== sources =====


class IntakeSourceCreate(BaseModel):
    code: str = Field(min_length=2, max_length=48, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    name: str = Field(min_length=1, max_length=200)
    kind: IntakeKind = IntakeKind.WEB_FORM

    #: Which LeadSource to record. Held per channel rather than derived from the
    #: kind, because a builder may run two web forms - the website and a
    #: parade-of-homes microsite - and want them attributed separately.
    lead_source: LeadSource = LeadSource.WEBSITE

    signing_secret: str | None = Field(default=None, max_length=200)
    default_community_id: uuid.UUID | None = None
    default_campaign: str | None = Field(default=None, max_length=200)
    default_realtor_id: uuid.UUID | None = None
    auto_assign: bool = True
    field_map: dict[str, str] | None = None
    honeypot_field: str | None = Field(default=None, max_length=64)
    rate_limit_per_minute: int = Field(default=30, ge=0, le=6000)


class IntakeSourceUpdate(BaseModel):
    code: str | None = Field(
        default=None, min_length=2, max_length=48, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    lead_source: LeadSource | None = None
    signing_secret: str | None = Field(default=None, max_length=200)
    default_community_id: uuid.UUID | None = None
    default_campaign: str | None = Field(default=None, max_length=200)
    default_realtor_id: uuid.UUID | None = None
    auto_assign: bool | None = None
    field_map: dict[str, str] | None = None
    honeypot_field: str | None = Field(default=None, max_length=64)
    rate_limit_per_minute: int | None = Field(default=None, ge=0, le=6000)


class IntakeSourceRead(ORMModel):
    """A channel as it is read back. The key is absent by construction - only its
    last four characters, so a builder can tell which key is in use without the
    key being recoverable from the API."""

    id: uuid.UUID
    code: str
    name: str
    kind: IntakeKind
    lead_source: str
    key_hint: str
    is_active: bool
    default_community_id: uuid.UUID | None
    default_campaign: str | None
    default_realtor_id: uuid.UUID | None
    auto_assign: bool
    field_map: dict[str, Any] | None
    honeypot_field: str | None
    rate_limit_per_minute: int
    last_received_at: datetime | None
    received_count: int
    accepted_count: int
    rejected_count: int
    failed_count: int
    is_archived: bool


class IntakeSourceCreated(BaseModel):
    """The response to creating a channel or rotating its key.

    `key` appears here and nowhere else in the API, ever. It is stored as a hash,
    so this response is the only opportunity to record it - which is stated in
    `warning` rather than left for somebody to discover.
    """

    source: IntakeSourceRead
    key: str
    post_url: str
    warning: str = (
        "Copy this key now. It is stored only as a hash and cannot be shown again - "
        "if it is lost it must be rotated, which immediately stops the old one working."
    )


# =============================================================== events =====


class IntakeEventRead(ORMModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: IntakeStatus
    #: The payload exactly as it arrived. This is what somebody reads to fix a
    #: broken mapping, so it is returned in full rather than summarised.
    payload: dict[str, Any]
    remote_ip: str | None
    external_ref: str | None
    lead_id: uuid.UUID | None
    error: str | None
    created_at: datetime
    needs_attention: bool


class IntakeAck(BaseModel):
    """What a public caller is told.

    Deliberately thin. A webhook does not need our lead id and should not be given
    one: an unauthenticated endpoint that hands back internal identifiers is a way
    to enumerate them. `received` is true whenever the payload is safely stored,
    including when we could not read it - because from the sender's point of view
    it did arrive, and telling a portal otherwise makes it retry forever.
    """

    received: bool
    status: IntakeStatus
    detail: str | None = None


class SourceHealth(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    kind: str
    is_active: bool
    last_received_at: datetime | None
    received_count: int
    accepted_count: int
    rejected_count: int
    failed_count: int


class IntakeHealth(BaseModel):
    """Is every channel still delivering.

    A channel that silently stops is the failure nobody notices - no error, just
    fewer leads than last month and no reason why. `last_received_at` per source is
    what makes it visible.
    """

    sources: list[SourceHealth]
    events_by_status: dict[str, int]
    needs_attention: int


class ReplayResult(BaseModel):
    status: IntakeStatus
    lead_id: uuid.UUID | None
    detail: str | None
