"""Automated lead intake: the channels leads arrive through by themselves.

Two records, and the second one is the important one.

`IntakeSource` is a configured channel - your website form, a portal feed, a
Facebook lead form, an enquiries inbox. Each holds its own key, so a leaked key
can be revoked without taking every channel down with it, and so a channel that
starts sending rubbish can be identified and switched off on its own.

`IntakeEvent` is every payload that arrived, stored **raw and before anything is
parsed**. This is the part that stops automated intake from quietly losing
business. A portal changes a field name, our parser breaks, and without this
table the enquiry is simply gone - nobody knows a lead was missed, because the
only evidence was a POST body that was thrown away. With it, the payload sits in
a failed queue with the error next to it, somebody fixes the mapping, and it is
replayed. Nothing is lost that arrived.

Why a key rather than a login
-----------------------------
A website form cannot hold a password. The channel authenticates with a key that
is generated once, shown once and stored only as a hash, exactly like an API
token - because that is what it is. A signed webhook (Meta, Google) is verified
by signature instead, and the key then only identifies which source it is.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.mixins import ArchivableMixin, AuditedMixin

#: JSONB on PostgreSQL, plain JSON on SQLite so the test suite can run.
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class IntakeKind(StrEnum):
    """What is on the other end, which decides how the payload is read.

    The kind is not cosmetic: each one has its own field names, its own way of
    proving the request is genuine, and its own idea of what a phone number looks
    like. One adapter per kind, chosen by this value.
    """

    #: Your own website form posting straight to us. The one channel fully under
    #: the builder's control, and the foundation the others reuse.
    WEB_FORM = "web_form"
    RIGHTMOVE = "rightmove"
    ZOOPLA = "zoopla"
    ONTHEMARKET = "onthemarket"
    META_LEAD_ADS = "meta_lead_ads"
    GOOGLE_LEAD_FORM = "google_lead_form"
    #: An enquiries inbox polled over IMAP. The fallback for portals that only
    #: ever send an email, and the most fragile of the lot.
    EMAIL = "email"
    #: Anything else, mapped by the field map on the source record.
    GENERIC = "generic"


class IntakeStatus(StrEnum):
    """What became of one inbound payload."""

    #: Parsed, and a new lead was created.
    ACCEPTED = "accepted"
    #: Parsed, but it was the same person as an existing lead. Linked, not
    #: duplicated - with nobody at the keyboard there is no one to judge.
    DUPLICATE = "duplicate"
    #: Deliberately refused: spam trap tripped, or missing a name entirely.
    REJECTED = "rejected"
    #: We could not read it. The payload is kept so it can be fixed and replayed.
    FAILED = "failed"
    #: Replayed successfully after a fix.
    REPLAYED = "replayed"


#: Statuses that mean somebody needs to look at this.
NEEDS_ATTENTION = (IntakeStatus.FAILED,)


class IntakeSource(UUIDMixin, TenantMixin, TimestampMixin, ArchivableMixin, AuditedMixin, Base):
    """One configured channel that leads arrive through."""

    __tablename__ = "intake_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_intake_sources_tenant_code"),
    )

    code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # String rather than a database enum, unlike the rest of this schema. This
    # set grows every time a connector is added, and a Postgres enum cannot have
    # a value removed and needs a migration outside a transaction to gain one.
    # The set of lead statuses is a business rule; the set of connectors is a
    # backlog.
    kind: Mapped[IntakeKind] = mapped_column(
        String(32), nullable=False, index=True
    )

    #: SHA-256 of the key. The key itself is shown once at creation and never
    #: again - if it is lost, it is rotated, not recovered.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The last four characters of the key, so a builder can tell which key a
    #: source is using without the key being readable.
    key_hint: Mapped[str] = mapped_column(String(8), nullable=False, default="")

    #: Shared secret for signature-verified webhooks (Meta, Google). Null for
    #: channels that authenticate with the key alone.
    signing_secret: Mapped[str | None] = mapped_column(String(200))

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )

    # -- what the channel cannot tell us --------------------------------------
    # A website form knows nothing about communities or campaigns. These fill the
    # gaps so a lead arrives attributed rather than anonymous.
    default_community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL")
    )
    default_campaign: Mapped[str | None] = mapped_column(String(200))
    #: The realtor this channel belongs to, for an agent's own feed.
    default_realtor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("realtors.id", ondelete="SET NULL")
    )
    #: Which LeadSource to record. Held per source rather than derived from the
    #: kind, because a builder may run two web forms and want them attributed
    #: differently - one on the website, one on a parade-of-homes microsite.
    lead_source: Mapped[str] = mapped_column(String(32), nullable=False, default="website")

    #: Hand the lead to somebody automatically. Off means it lands in the
    #: unassigned queue for a manager to distribute.
    auto_assign: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: For GENERIC and EMAIL: which incoming field means which of ours.
    field_map: Mapped[dict | None] = mapped_column(JsonColumn)

    #: A hidden form field that a human never fills in. Anything that arrives
    #: with it populated is a bot, and is rejected rather than archived later.
    honeypot_field: Mapped[str | None] = mapped_column(String(64))

    #: Most requests per minute from one caller. An unauthenticated endpoint on
    #: the public internet will be found; this is what stops it being a hole.
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False
    )

    # -- observability --------------------------------------------------------
    # Kept on the source so "has the website form gone quiet" is answerable at a
    # glance. A channel that silently stops is the failure nobody notices.
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    events: Mapped[list["IntakeEvent"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    @property
    def is_healthy(self) -> bool:
        """Nothing has failed since the last thing that worked."""
        return self.failed_count == 0

    def __repr__(self) -> str:
        return f"<IntakeSource {self.code} ({self.kind})>"


class IntakeEvent(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One inbound payload, stored before it is understood.

    The raw body is kept whatever happens. That is the whole point: a parser that
    breaks must cost a delay, not a lead.
    """

    __tablename__ = "intake_events"

    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("intake_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[IntakeStatus] = mapped_column(
        String(16), nullable=False, index=True, default=IntakeStatus.ACCEPTED
    )

    #: Exactly what arrived, untouched.
    payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    #: Who sent it, for rate limiting and for spotting one address flooding us.
    remote_ip: Mapped[str | None] = mapped_column(String(64), index=True)

    #: The channel's own id for this enquiry, where it has one. This is what
    #: makes a resend idempotent - a portal that retries must not produce a
    #: second lead.
    external_ref: Mapped[str | None] = mapped_column(String(200), index=True)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    #: Why it was rejected, or what broke. Read straight off the failed queue, so
    #: it says what to fix rather than that something went wrong.
    error: Mapped[str | None] = mapped_column(String(1000))

    source: Mapped["IntakeSource"] = relationship(back_populates="events")

    @property
    def needs_attention(self) -> bool:
        return self.status in NEEDS_ATTENTION

    def __repr__(self) -> str:
        return f"<IntakeEvent {self.status} source={self.source_id}>"
