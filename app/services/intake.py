"""Business tier for automated lead capture.

The order of operations is the design
-------------------------------------
Store first, understand second. Every payload is written to `intake_events`
before anything tries to read it, and the event is committed even when the parse
fails. That single ordering is what makes automated intake safe to run:

    receive → store raw → verify → parse → de-duplicate → create → assign → mark

A parser that breaks on a field rename costs a delay and a queue somebody clears.
Parsing first and storing only on success would cost a lead, silently, and the
only evidence would have been a POST body that was thrown away.

Where the rules differ from a person typing
-------------------------------------------
  * **duplicates link rather than warn.** On the capture form a human sees "this
    looks like Dana Reyes" and decides. A webhook has nobody to ask, so a repeat
    enquiry is attached to the existing lead as a new activity - which is what it
    is - instead of making a second record nobody reconciles.
  * **a resend is not a new lead.** Portals retry. The channel's own reference is
    recorded, and a reference already seen is answered with the lead it produced
    the first time. The endpoint is idempotent because the network is not.
  * **spam is rejected, not captured.** A honeypot field a human never sees, and
    a per-source rate limit. Both are on the source record, because the website
    form and a signed Meta webhook do not need the same defences.
  * **a missing name is a failure, not a rejection.** Rejected means "we looked
    and this is rubbish"; failed means "we could not read it and somebody should".
    Conflating them is how a broken mapping gets mistaken for bot traffic and
    ignored for a fortnight.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.crm import ActivityOutcome, ActivityType, Lead, LeadSource
from app.models.intake import IntakeEvent, IntakeKind, IntakeSource, IntakeStatus
from app.repositories.base import Conflict, NotFound, TenantRepository
from app.repositories.crm import LeadActivityRepository, LeadRepository
from app.schemas.common import PageParams
from app.services.assignment import RoundRobin
from app.services.intake_adapters import LeadDraft, SpamRejected, UnreadablePayload, parse

KEY_PREFIX = "cpmt_ik_"


def new_key() -> tuple[str, str, str]:
    """A fresh channel key: the key itself, its hash, and a hint for display.

    Returned once and never reconstructible. Stored as a SHA-256 digest because a
    channel key is an API token in everything but name, and a table of usable
    tokens in plaintext is a table worth stealing.
    """
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_key(key), key[-4:]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ------------------------------------------------------------ rate limiting ---

#: Recent hits per (source, caller). In process memory, which is the honest
#: limitation: with more than one worker each holds its own window, so the real
#: ceiling is the configured limit times the worker count. That is enough to stop
#: a script hammering the endpoint, which is what this is for. A shared limiter
#: belongs in Redis at the edge, and is on the production list with HTTPS.
_HITS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_WINDOW = 60.0


def rate_limited(source_id: uuid.UUID, caller: str, limit: int) -> bool:
    """True when this caller has already had its allowance this minute."""
    if limit <= 0:
        return False
    bucket = _HITS[(str(source_id), caller)]
    now = time.monotonic()
    while bucket and now - bucket[0] > _WINDOW:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def reset_rate_limits() -> None:
    """Clear the windows. For tests, which must not inherit each other's traffic."""
    _HITS.clear()


# ------------------------------------------------------------- repositories ---


class IntakeSourceRepository(TenantRepository[IntakeSource]):
    model = IntakeSource
    label = "Intake source"
    search_fields = ("code", "name")

    async def all_ordered(self, *, include_archived: bool = False):
        return await self.list(order_by=IntakeSource.code, include_archived=include_archived)

    async def by_code(self, code: str) -> IntakeSource | None:
        return await self.db.scalar(
            self._base().where(func.lower(IntakeSource.code) == code.lower())
        )

    async def code_taken_by_other(self, code: str, exclude_id: uuid.UUID) -> bool:
        found = await self.db.scalar(
            select(IntakeSource.id).where(
                IntakeSource.tenant_id == self.tenant_id,
                func.lower(IntakeSource.code) == code.lower(),
                IntakeSource.id != exclude_id,
            )
        )
        return found is not None


class IntakeEventRepository(TenantRepository[IntakeEvent]):
    model = IntakeEvent
    label = "Intake event"
    search_fields = ("external_ref", "error")

    async def page(self, params: PageParams, **filters):
        return await self.paginate(params, order_by=IntakeEvent.created_at.desc(), **filters)

    async def by_external_ref(self, source_id: uuid.UUID, ref: str) -> IntakeEvent | None:
        """A payload this channel has already sent us. The idempotency check."""
        return await self.db.scalar(
            self._base()
            .where(
                IntakeEvent.source_id == source_id,
                IntakeEvent.external_ref == ref,
                IntakeEvent.status.in_(
                    (IntakeStatus.ACCEPTED, IntakeStatus.DUPLICATE, IntakeStatus.REPLAYED)
                ),
            )
            .order_by(IntakeEvent.created_at.desc())
        )

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self.db.execute(
            select(IntakeEvent.status, func.count(IntakeEvent.id))
            .where(IntakeEvent.tenant_id == self.tenant_id)
            .group_by(IntakeEvent.status)
        )
        found = {r[0]: r[1] for r in rows}
        return {s.value: found.get(s.value, found.get(s, 0)) for s in IntakeStatus}


# ------------------------------------------------------------- the outcome ---


class IntakeResult:
    """What happened to one payload, in the shape the caller needs to answer with."""

    def __init__(
        self,
        status: IntakeStatus,
        *,
        lead_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        message: str | None = None,
    ):
        self.status = status
        self.lead_id = lead_id
        self.event_id = event_id
        self.message = message

    @property
    def accepted(self) -> bool:
        return self.status in (IntakeStatus.ACCEPTED, IntakeStatus.DUPLICATE, IntakeStatus.REPLAYED)


# --------------------------------------------------------------- the service ---


class IntakeService:
    """Automated capture, scoped to one builder for its whole lifetime."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.sources = IntakeSourceRepository(db, tenant_id)
        self.events = IntakeEventRepository(db, tenant_id)
        self.leads = LeadRepository(db, tenant_id)
        self.activities = LeadActivityRepository(db, tenant_id)
        self.rota = RoundRobin(db, tenant_id)

    # ===================================================== the hot path =====

    async def receive(
        self,
        source: IntakeSource,
        payload: dict,
        *,
        remote_ip: str | None = None,
    ) -> IntakeResult:
        """Take one inbound payload and do everything that follows from it.

        Never raises for a bad payload. The caller is an unauthenticated webhook
        and a 500 teaches a portal to stop retrying, or to retry forever - both
        worse than an accepted-but-flagged event that a person can see.
        """
        if not source.is_active:
            # No event row: the channel is switched off, and recording traffic
            # from a disabled source would fill the queue with noise nobody asked
            # to see.
            return IntakeResult(
                IntakeStatus.REJECTED, message="This intake source is switched off."
            )

        source.received_count += 1
        source.last_received_at = datetime.now(UTC)

        event = IntakeEvent(
            tenant_id=self.tenant_id,
            source_id=source.id,
            payload=payload,
            remote_ip=remote_ip,
            status=IntakeStatus.ACCEPTED,
        )
        self.db.add(event)
        # Committed before anything tries to understand it. Everything below can
        # fail; this cannot be lost.
        await self.db.commit()

        try:
            draft = self._read(source, payload)
        except SpamRejected as exc:
            return await self._finish(source, event, IntakeStatus.REJECTED, str(exc))
        except UnreadablePayload as exc:
            # Failed, not rejected. Somebody needs to look at this, and the
            # payload is sitting right here for them.
            return await self._finish(source, event, IntakeStatus.FAILED, str(exc))

        event.external_ref = draft.external_ref
        if draft.external_ref:
            seen = await self.events.by_external_ref(source.id, draft.external_ref)
            if seen and seen.id != event.id:
                return await self._finish(
                    source,
                    event,
                    IntakeStatus.DUPLICATE,
                    f"Already received as {draft.external_ref}.",
                    lead_id=seen.lead_id,
                )

        existing = await self._same_person(draft)
        if existing is not None:
            await self._note_repeat(existing, source, draft)
            return await self._finish(
                source,
                event,
                IntakeStatus.DUPLICATE,
                f"Matched the existing lead for {existing.full_name}.",
                lead_id=existing.id,
            )

        lead = await self._create(source, draft)
        return await self._finish(source, event, IntakeStatus.ACCEPTED, None, lead_id=lead.id)

    def _read(self, source: IntakeSource, payload: dict) -> LeadDraft:
        """Spam checks, then the channel's adapter."""
        trap = source.honeypot_field
        if trap and str(payload.get(trap) or "").strip():
            raise SpamRejected(f"The honeypot field {trap!r} was filled in, which only a bot does.")
        return parse(IntakeKind(source.kind), payload, source.field_map)

    async def _same_person(self, draft: LeadDraft) -> Lead | None:
        """An existing lead for whoever this is, or None.

        The same matcher the capture form uses, applied with a different verdict:
        a person is warned and decides, a webhook has nobody to ask and so links.
        The most recent match wins, because that is the one being worked.
        """
        matches = await self.leads.find_duplicates(
            email=draft.email, phone=draft.phone, mobile=draft.mobile
        )
        return matches[0] if matches else None

    async def _note_repeat(self, lead: Lead, source: IntakeSource, draft: LeadDraft) -> None:
        """Record that they enquired again, on the lead they already have.

        A repeat enquiry is not noise - it is the strongest buying signal a lead
        gives. It goes on the trail, it refreshes the contact date, and a lead
        that had gone cold comes back into view.
        """
        detail = draft.notes or "No message."
        self.activities.stage(
            lead_id=lead.id,
            user_id=None,
            activity_type=ActivityType.EMAIL
            if source.kind == IntakeKind.EMAIL
            else ActivityType.NOTE,
            outcome=ActivityOutcome.NONE,
            summary=f"Enquired again via {source.name}: {detail}"[:1000],
            occurred_on=datetime.now(UTC).date(),
            is_system=True,
        )
        # Deliberately not `last_contacted_on`: they contacted us, we did not
        # contact them, and the chasing queue measures the latter.
        if lead.is_open and lead.next_follow_up is None:
            lead.next_follow_up = datetime.now(UTC).date()
        await self.db.commit()

    async def _create(self, source: IntakeSource, draft: LeadDraft) -> Lead:
        """Turn a draft into a lead, filling in what the channel could not know."""
        notes = draft.notes or ""
        if draft.extra:
            # Folded in rather than dropped. A portal's property reference has no
            # column, and losing it means nobody can tie the enquiry back.
            trailer = "\n".join(f"{k}: {v}" for k, v in draft.extra.items())
            notes = f"{notes}\n\n{trailer}".strip()

        owner = await self.rota.next_owner() if source.auto_assign else None

        lead = self.leads.stage(
            first_name=draft.first_name[:120] or "(no name given)",
            last_name=draft.last_name[:120] or "(no surname given)",
            email=draft.email,
            phone=draft.phone,
            mobile=draft.mobile,
            postcode=draft.postcode,
            city=draft.city,
            address=draft.address,
            notes=notes[:2000] or None,
            bedrooms_needed=draft.bedrooms_needed,
            price_min=draft.price_min,
            price_max=draft.price_max,
            desired_move_in=draft.desired_move_in,
            has_home_to_sell=bool(draft.has_home_to_sell),
            is_prequalified=bool(draft.is_prequalified),
            source=LeadSource(source.lead_source),
            campaign=source.default_campaign,
            community_id=source.default_community_id,
            realtor_id=source.default_realtor_id,
            assigned_to_id=owner,
        )
        await self.db.flush()

        self.activities.stage(
            lead_id=lead.id,
            user_id=None,
            activity_type=ActivityType.NOTE,
            outcome=ActivityOutcome.NONE,
            summary=f"Captured automatically from {source.name}.",
            occurred_on=datetime.now(UTC).date(),
            is_system=True,
        )
        if owner:
            self.activities.stage(
                lead_id=lead.id,
                user_id=None,
                activity_type=ActivityType.ASSIGNMENT,
                outcome=ActivityOutcome.NONE,
                summary="Assigned automatically by round-robin.",
                occurred_on=datetime.now(UTC).date(),
                is_system=True,
            )
        await self.db.commit()
        return await self.leads.get(lead.id)

    async def _finish(
        self,
        source: IntakeSource,
        event: IntakeEvent,
        status: IntakeStatus,
        error: str | None,
        *,
        lead_id: uuid.UUID | None = None,
    ) -> IntakeResult:
        event.status = status
        event.error = error
        event.lead_id = lead_id
        if status is IntakeStatus.ACCEPTED:
            source.accepted_count += 1
        elif status is IntakeStatus.REJECTED:
            source.rejected_count += 1
        elif status is IntakeStatus.FAILED:
            source.failed_count += 1
        await self.db.commit()
        return IntakeResult(status, lead_id=lead_id, event_id=event.id, message=error)

    # ======================================================== the queue =====

    async def list_events(self, params: PageParams, **filters):
        return await self.events.page(params, **filters)

    async def failed_events(self, params: PageParams):
        """The queue that has to be watched. Everything here is a lead we have and
        cannot read."""
        return await self.events.page(params, status=IntakeStatus.FAILED)

    async def replay(self, event_id: uuid.UUID) -> IntakeResult:
        """Run a stored payload through the adapter again, after a fix.

        The reason the raw payload is kept. Correct the field map or ship a
        corrected adapter, replay, and the enquiry becomes the lead it should have
        been - with its original arrival time still on the record.
        """
        event = await self.events.get(event_id)
        if event.status not in (IntakeStatus.FAILED, IntakeStatus.REJECTED):
            raise Conflict("Only a failed or rejected payload can be replayed.")

        source = await self.sources.get(event.source_id)
        try:
            draft = self._read(source, event.payload)
        except (SpamRejected, UnreadablePayload) as exc:
            event.error = str(exc)
            await self.db.commit()
            raise BusinessRuleViolation(
                f"Still cannot read this payload: {exc}",
                "Fix the field map on the source, or capture the lead by hand from "
                "the payload shown here.",
            ) from exc

        existing = await self._same_person(draft)
        if existing is not None:
            await self._note_repeat(existing, source, draft)
            return await self._finish(
                source,
                event,
                IntakeStatus.DUPLICATE,
                f"Matched the existing lead for {existing.full_name}.",
                lead_id=existing.id,
            )

        lead = await self._create(source, draft)
        # REPLAYED rather than ACCEPTED, so the queue keeps an honest record of
        # what needed a human and what did not.
        if source.failed_count > 0:
            source.failed_count -= 1
        return await self._finish(source, event, IntakeStatus.REPLAYED, None, lead_id=lead.id)

    async def health(self) -> dict:
        """Is every channel still delivering.

        A channel that silently stops is the failure nobody notices: no error, no
        alert, just fewer leads than last month and no reason why. `last_received_at`
        per source is what makes that visible.
        """
        sources = await self.sources.all_ordered()
        return {
            "sources": [
                {
                    "id": s.id,
                    "code": s.code,
                    "name": s.name,
                    "kind": s.kind,
                    "is_active": s.is_active,
                    "last_received_at": s.last_received_at,
                    "received_count": s.received_count,
                    "accepted_count": s.accepted_count,
                    "rejected_count": s.rejected_count,
                    "failed_count": s.failed_count,
                }
                for s in sources
            ],
            "events_by_status": await self.events.counts_by_status(),
            "needs_attention": await self.events.count(status=IntakeStatus.FAILED),
        }

    # ====================================================== configuration =====

    async def list_sources(self, params: PageParams | None = None):
        if params is None:
            return await self.sources.all_ordered()
        return await self.sources.paginate(params, order_by=IntakeSource.code)

    async def get_source(self, source_id: uuid.UUID) -> IntakeSource:
        return await self.sources.get(source_id)

    async def create_source(self, *, actor_id=None, **data) -> tuple[IntakeSource, str]:
        """Add a channel. Returns the source and its key - the only time the key
        is readable."""
        await self.sources.require_unique_code(data["code"])
        key, digest, hint = new_key()
        source = await self.sources.create(
            created_by_id=actor_id, key_hash=digest, key_hint=hint, **data
        )
        return source, key

    async def update_source(self, source_id: uuid.UUID, *, actor_id=None, **data):
        source = await self.sources.get(source_id)
        new_code = data.get("code")
        if new_code and await self.sources.code_taken_by_other(new_code, source_id):
            raise Conflict(f"Intake source {new_code} already exists.")
        return await self.sources.update(source, updated_by_id=actor_id, **data)

    async def rotate_key(self, source_id: uuid.UUID, *, actor_id=None) -> tuple[IntakeSource, str]:
        """Issue a new key and invalidate the old one immediately.

        No grace period on purpose. A key is rotated because it leaked, and a
        window in which the leaked key still works is the thing you were trying to
        close.
        """
        source = await self.sources.get(source_id)
        key, digest, hint = new_key()
        source.key_hash = digest
        source.key_hint = hint
        source.updated_by_id = actor_id
        await self.db.commit()
        return await self.sources.get(source_id), key

    async def set_active(self, source_id: uuid.UUID, active: bool, *, actor_id=None):
        source = await self.sources.get(source_id)
        source.is_active = active
        source.updated_by_id = actor_id
        await self.db.commit()
        return await self.sources.get(source_id)

    async def archive_source(self, source_id: uuid.UUID, actor_id, reason=None):
        """Retire a channel. Its events stay, because they are the record of every
        lead it ever produced."""
        return await self.sources.archive(await self.sources.get(source_id), actor_id, reason)


# ------------------------------------------------------------- the lookup ---
#
# The one query in this application that is not scoped to a tenant, and it has to
# be. An inbound webhook arrives with no login and no tenant: the key *is* the
# identity, and resolving it is what tells us which builder the lead belongs to.
#
# It is safe because the key is the only thing being trusted. The code in the URL
# narrows the search but proves nothing - the digest comparison is what decides,
# and a key is 32 bytes of `secrets`. Everything downstream is constructed with
# the tenant this returns, so the rest of the request is scoped normally.
#
# Deliberately a module-level function rather than a method on the service, so
# that no tenant-scoped repository has a way to reach outside its tenant. If this
# were a method, `self.tenant_id` would be sitting there inviting misuse.


async def resolve_source(db: AsyncSession, code: str, key: str | None) -> IntakeSource:
    """The channel a public request claims to be, proven by its key.

    Raises the same NotFound whether the code is unknown or the key is wrong.
    Distinguishing them would tell a caller probing the endpoint that a channel by
    that name exists, which is exactly the next thing they want to know.
    """
    if not key or not key.startswith(KEY_PREFIX):
        raise NotFound("Intake source")

    digest = hash_key(key)
    source = await db.scalar(
        select(IntakeSource).where(
            IntakeSource.key_hash == digest,
            func.lower(IntakeSource.code) == (code or "").lower(),
            IntakeSource.is_archived.is_(False),
        )
    )
    if source is None:
        # A comparison against a fixed digest even on the miss, so a wrong key and
        # an unknown code take indistinguishable time.
        secrets.compare_digest(digest, hash_key("no-such-key"))
        raise NotFound("Intake source")
    return source
