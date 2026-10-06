"""Business tier for sales: leads, realtors, and the trail behind both.

A lead list is easy. What makes this feature rather than a table is the handful
of rules underneath it, each of which exists because the alternative loses money
or loses information:

  * **a lost lead needs a reason.** "We lost it" is not information. "They bought
    from a competitor on price" changes what the sales manager does next month,
    so the reason is required rather than encouraged.
  * **status moves forward, not sideways.** A lead cannot go from new straight to
    converted: the qualification step is where the builder finds out whether the
    buyer can actually pay, and skipping it is how a job gets started for
    somebody who never completes.
  * **conversion requires contact details.** The lead becomes a Buyer, and a
    buyer with no email cannot be sent a selection deadline.
  * **every status change and assignment is logged automatically.** A history
    that contains only what somebody remembered to type is not a history.
  * **duplicates are flagged, not blocked.** The same family enquiring about two
    plans is legitimate; so is a genuine second enquiry. The system says "this
    looks like a duplicate of X" and lets a person decide, because a hard block
    on a walk-in standing at the desk is worse than a duplicate row.

Nothing in this module imports FastAPI or writes SQL. It raises domain errors and
the API tier decides what they mean in HTTP.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.crm import (
    OPEN_STATUSES,
    ActivityOutcome,
    ActivityType,
    Lead,
    LeadStatus,
)
from app.repositories.base import Conflict, NotFound
from app.repositories.catalog import CommunityRepository, PlanRepository
from app.repositories.crm import (
    LeadActivityRepository,
    LeadRepository,
    RealtorRepository,
)
from app.schemas.common import PageParams

#: Which statuses a lead may move to from where it is now.
#:
#: Forward through the funnel, or out of it. What is deliberately missing is the
#: jump from NEW to CONVERTED: qualification is where the builder discovers
#: whether the buyer can pay, and a job started for somebody who cannot is the
#: single most expensive mistake this feature can prevent.
#:
#: Backwards moves are allowed only as far as CONTACTED, because a lead that
#: cooled off is worked again from there - but not back to NEW, which would erase
#: the fact that somebody has already spoken to them.
ALLOWED_TRANSITIONS: dict[LeadStatus, tuple[LeadStatus, ...]] = {
    LeadStatus.NEW: (LeadStatus.CONTACTED, LeadStatus.QUALIFIED, LeadStatus.LOST),
    LeadStatus.CONTACTED: (LeadStatus.QUALIFIED, LeadStatus.APPOINTMENT, LeadStatus.LOST),
    LeadStatus.QUALIFIED: (
        LeadStatus.APPOINTMENT,
        LeadStatus.CONVERTED,
        LeadStatus.CONTACTED,
        LeadStatus.LOST,
    ),
    LeadStatus.APPOINTMENT: (
        LeadStatus.QUALIFIED,
        LeadStatus.CONVERTED,
        LeadStatus.CONTACTED,
        LeadStatus.LOST,
    ),
    # Terminal. A lost lead that comes back is reopened explicitly, which logs
    # the fact; a converted one is a buyer now and belongs to Contracts.
    LeadStatus.CONVERTED: (),
    LeadStatus.LOST: (),
}

#: Activity types that mean a person actually made contact, and so should move a
#: new lead to contacted and refresh "last contacted". A note to oneself does not.
CONTACT_TYPES = (
    ActivityType.CALL,
    ActivityType.EMAIL,
    ActivityType.TEXT,
    ActivityType.VISIT,
    ActivityType.APPOINTMENT,
)


class CrmService:
    """Every sales operation, scoped to one builder for its whole lifetime."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.leads = LeadRepository(db, tenant_id)
        self.activities = LeadActivityRepository(db, tenant_id)
        self.realtors = RealtorRepository(db, tenant_id)
        self.communities = CommunityRepository(db, tenant_id)
        self.plans = PlanRepository(db, tenant_id)

    # ==================================================== reading leads =====

    async def list_leads(self, params: PageParams | None = None, **filters):
        if params is None:
            return await self.leads.all_ordered()
        return await self.leads.page(params, **filters)

    async def list_open(self, params: PageParams, **filters):
        return await self.leads.page_open_only(params, **filters)

    async def list_overdue(self, params: PageParams, **filters):
        """The call list. Open leads whose follow-up date has passed, worst first."""
        return await self.leads.page_overdue(params, **filters)

    async def list_unassigned(self, params: PageParams, **filters):
        return await self.leads.page_unassigned(params, **filters)

    async def get_lead(self, lead_id: uuid.UUID) -> Lead:
        return await self.leads.get(lead_id)

    async def summary(self) -> dict:
        """The numbers above the list, plus the funnel.

        Assembled here rather than in the router because "open" means a specific
        set of statuses, and that definition belongs with the rules that use it.
        """
        counts = await self.leads.counts_for_dashboard()
        return {
            **counts,
            "by_status": await self.leads.status_counts(),
            "by_source": [
                {"source": s, "leads": n, "converted": c}
                for s, n, c in await self.leads.source_counts()
            ],
        }

    # ==================================================== capturing them =====

    async def check_duplicates(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        mobile: str | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[Lead]:
        """Leads that look like the same person. Advisory, never a block."""
        return await self.leads.find_duplicates(
            email=email, phone=phone, mobile=mobile, exclude_id=exclude_id
        )

    async def create_lead(self, *, actor_id: uuid.UUID | None = None, **data) -> Lead:
        """Capture an enquiry.

        Deliberately permissive about what is known: a walk-in may give a name
        and nothing else, and refusing that loses the lead entirely. The one hard
        requirement is that any record it points at belongs to this builder.
        """
        await self._require_references(data)
        data["email"] = str(data["email"]) if data.get("email") else None

        await self._link_campaign(data)
        lead = await self.leads.create(created_by_id=actor_id, **data)
        await self._log(
            lead,
            actor_id,
            ActivityType.NOTE,
            f"Lead captured from {lead.source.value.replace('_', ' ')}.",
            system=True,
        )
        return await self.leads.get(lead.id)

    async def update_lead(self, lead_id: uuid.UUID, *, actor_id=None, **data) -> Lead:
        """Edit a lead's details.

        Status is not editable here. It moves through `set_status`, which checks
        the transition and writes the history - letting a PATCH set it directly
        would route round both.
        """
        lead = await self.leads.get(lead_id)
        data.pop("status", None)
        await self._require_references(data)
        if data.get("email"):
            data["email"] = str(data["email"])
        return await self.leads.update(lead, updated_by_id=actor_id, **data)

    async def _link_campaign(self, data: dict) -> None:
        """Attach the lead to a campaign whose code matches its tag.

        Done at capture rather than left to somebody pressing Attach leads later.
        A lead arriving tagged "spring-parade" belongs to the campaign called
        "spring-parade", and making that a manual step means attribution is only
        as good as whoever remembers to do it - which is how a marketing report
        ends up understating everything.

        The tag stays either way: inbound channels send whatever string was
        configured in them and know nothing about our ids, so it is the evidence
        of what actually arrived.
        """
        if data.get("campaign_id") or not data.get("campaign"):
            return
        from sqlalchemy import func, select

        from app.models.marketing import Campaign

        match = await self.db.scalar(
            select(Campaign.id).where(
                Campaign.tenant_id == self.tenant_id,
                func.lower(Campaign.code) == str(data["campaign"]).strip().lower(),
            )
        )
        if match:
            data["campaign_id"] = match

    async def _require_references(self, data: dict) -> None:
        """Every foreign key must point at something this builder owns.

        Checked before the write rather than left to the database, so the caller
        gets "no such community" instead of an integrity error.
        """
        for field, repo in (
            ("community_id", self.communities),
            ("plan_id", self.plans),
            ("realtor_id", self.realtors),
        ):
            if data.get(field):
                await repo.require_exists(data[field])

    # ===================================================== working them =====

    async def set_status(
        self,
        lead_id: uuid.UUID,
        new_status: LeadStatus,
        *,
        actor_id: uuid.UUID | None = None,
        reason: str | None = None,
        lost_to: str | None = None,
    ) -> Lead:
        """Move a lead through the funnel, with the transition checked and logged."""
        lead = await self.leads.get(lead_id)
        old = lead.status

        if new_status is old:
            return lead  # idempotent; not worth a history entry
        self._require_transition(lead, new_status)

        if new_status is LeadStatus.LOST:
            return await self.mark_lost(lead_id, reason, lost_to=lost_to, actor_id=actor_id)
        if new_status is LeadStatus.CONVERTED:
            raise BusinessRuleViolation(
                "A lead becomes a buyer through conversion, not a status change.",
                "Use Convert to buyer, which creates the buyer record and keeps "
                "the link back to this lead.",
            )

        lead.status = new_status
        await self.db.commit()

        await self._log(
            lead,
            actor_id,
            ActivityType.STATUS_CHANGE,
            f"Status changed from {old.value} to {new_status.value}"
            + (f": {reason}" if reason else "."),
            system=True,
        )
        return await self.leads.get(lead_id)

    def _require_transition(self, lead: Lead, new_status: LeadStatus) -> None:
        allowed = ALLOWED_TRANSITIONS[lead.status]
        if new_status in allowed:
            return
        if lead.status is LeadStatus.CONVERTED:
            raise BusinessRuleViolation(
                f"{lead.full_name} has already been converted to a buyer.",
                "Changes now belong on the buyer and their contract, not the lead.",
            )
        if lead.status is LeadStatus.LOST:
            raise BusinessRuleViolation(
                f"{lead.full_name} is marked lost.",
                "Reopen the lead first if they have come back to you.",
            )
        raise BusinessRuleViolation(
            f"A lead cannot move from {lead.status.value} to {new_status.value}.",
            "Next steps from here: " + ", ".join(s.value for s in allowed) + ".",
        )

    async def assign(
        self, lead_id: uuid.UUID, user_id: uuid.UUID | None, *, actor_id=None
    ) -> Lead:
        """Put somebody's name on a lead, or take it off.

        A user_id of None unassigns, which is a real action - a salesperson
        leaving needs their pipeline handed back to the pool.
        """
        lead = await self.leads.get(lead_id)
        if not lead.is_open:
            raise BusinessRuleViolation(
                f"{lead.full_name} is {lead.status.value} and no longer being worked.",
                "Reopen the lead if it needs an owner again.",
            )
        if user_id:
            await self._require_colleague(user_id)

        was = lead.assigned_to_id
        if was == user_id:
            return lead
        lead.assigned_to_id = user_id
        await self.db.commit()

        await self._log(
            lead,
            actor_id,
            ActivityType.ASSIGNMENT,
            "Assigned." if user_id else "Assignment cleared.",
            system=True,
        )
        return await self.leads.get(lead_id)

    async def bulk_assign(
        self, lead_ids: list[uuid.UUID], user_id: uuid.UUID | None, *, actor_id=None
    ) -> dict:
        """Assign a batch. Reports what happened rather than failing on the first
        problem: a manager distributing forty leads should not lose the other
        thirty-nine because one was already converted."""
        leads = await self.leads.by_ids(lead_ids)
        if user_id:
            await self._require_colleague(user_id)

        assigned, skipped = 0, []
        for lead in leads:
            if not lead.is_open:
                skipped.append(f"{lead.full_name} ({lead.status.value})")
                continue
            if lead.assigned_to_id == user_id:
                continue
            lead.assigned_to_id = user_id
            self.activities.stage(
                lead_id=lead.id,
                user_id=actor_id,
                activity_type=ActivityType.ASSIGNMENT,
                outcome=ActivityOutcome.NONE,
                summary="Assigned in bulk." if user_id else "Assignment cleared in bulk.",
                occurred_on=date.today(),
                is_system=True,
            )
            assigned += 1

        await self.db.commit()
        return {
            "assigned": assigned,
            "skipped": skipped,
            "not_found": len(lead_ids) - len(leads),
        }

    async def schedule_follow_up(
        self, lead_id: uuid.UUID, when: date, *, actor_id=None, note: str | None = None
    ) -> Lead:
        """Set the date this lead is next to be chased."""
        lead = await self.leads.get(lead_id)
        if not lead.is_open:
            raise BusinessRuleViolation(
                f"{lead.full_name} is {lead.status.value}; there is nothing to follow up.",
                "Reopen the lead first.",
            )
        lead.next_follow_up = when
        await self.db.commit()
        await self._log(
            lead,
            actor_id,
            ActivityType.NOTE,
            note or f"Follow-up set for {when.isoformat()}.",
            system=True,
        )
        return await self.leads.get(lead_id)

    async def set_rating(self, lead_id: uuid.UUID, rating, *, actor_id=None) -> Lead:
        lead = await self.leads.get(lead_id)
        lead.rating = rating
        await self.db.commit()
        return await self.leads.get(lead_id)

    # ==================================================== the trail =====

    async def list_activities(self, lead_id: uuid.UUID):
        # Prove the lead is ours before filtering by it, or the lead_id filter
        # alone would happily read another builder's history.
        await self.leads.require_exists(lead_id)
        return await self.activities.for_lead(lead_id)

    async def log_activity(
        self,
        lead_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        activity_type: ActivityType,
        summary: str,
        occurred_on: date | None = None,
        outcome: ActivityOutcome = ActivityOutcome.NONE,
        next_follow_up: date | None = None,
    ):
        """Record a touch, and let it move the lead on.

        Three side effects, all of them the reason a person bothers logging:

          * a real contact on a brand-new lead moves it to contacted, so the
            "nobody has rung these" queue is honest
          * `last_contacted_on` advances, which is what "sitting for 14 days"
            is measured from
          * an outcome of "booked" moves the lead to appointment, because that is
            what booking one means
        """
        lead = await self.leads.get(lead_id)
        when = occurred_on or date.today()
        if when > date.today():
            raise BusinessRuleViolation(
                "An activity cannot be logged in the future.",
                "Use Schedule follow-up for something that has not happened yet.",
            )

        activity = self.activities.stage(
            lead_id=lead.id,
            user_id=actor_id,
            activity_type=activity_type,
            outcome=outcome,
            summary=summary,
            occurred_on=when,
            is_system=False,
        )

        if activity_type in CONTACT_TYPES:
            if lead.last_contacted_on is None or when > lead.last_contacted_on:
                lead.last_contacted_on = when
            if lead.status is LeadStatus.NEW:
                lead.status = LeadStatus.CONTACTED

        if outcome is ActivityOutcome.BOOKED and lead.status in (
            LeadStatus.CONTACTED,
            LeadStatus.QUALIFIED,
        ):
            lead.status = LeadStatus.APPOINTMENT
        # An outcome of "not interested" deliberately does *not* mark the lead
        # lost. That needs a reason, and "they said no on the phone" is the
        # salesperson's to write rather than ours to invent.

        if next_follow_up:
            lead.next_follow_up = next_follow_up

        await self.db.commit()
        return await self.activities.get(activity.id)

    async def _log(
        self,
        lead: Lead,
        actor_id: uuid.UUID | None,
        activity_type: ActivityType,
        summary: str,
        *,
        system: bool = True,
    ) -> None:
        """Write a system entry. Separate from `log_activity` because these never
        move the lead on - they record that it already moved."""
        self.activities.stage(
            lead_id=lead.id,
            user_id=actor_id,
            activity_type=activity_type,
            outcome=ActivityOutcome.NONE,
            summary=summary,
            occurred_on=date.today(),
            is_system=system,
        )
        await self.db.commit()

    # ======================================================== endings =====

    async def mark_lost(
        self,
        lead_id: uuid.UUID,
        reason: str | None,
        *,
        lost_to: str | None = None,
        actor_id=None,
    ) -> Lead:
        """Close a lead off, with the reason that makes it useful later.

        The reason is required. A pipeline full of leads marked lost with no
        reason tells the business nothing; the same pipeline annotated with
        "price", "timing", "went with a competitor" tells it where it is losing.
        """
        lead = await self.leads.get(lead_id)
        if lead.status is LeadStatus.CONVERTED:
            raise BusinessRuleViolation(
                f"{lead.full_name} has already been converted to a buyer.",
                "Cancel the contract instead if the sale has fallen through.",
            )
        if lead.status is LeadStatus.LOST:
            raise Conflict(f"{lead.full_name} is already marked lost.")
        if not reason or not reason.strip():
            raise BusinessRuleViolation(
                "A lost lead needs a reason.",
                "Price, timing, specification, or went elsewhere - whatever it "
                "was. Without it this lead teaches the business nothing.",
            )

        lead.status = LeadStatus.LOST
        lead.lost_reason = reason.strip()
        lead.lost_to = lost_to
        lead.lost_on = date.today()
        lead.next_follow_up = None  # nothing left to chase
        await self.db.commit()

        await self._log(
            lead,
            actor_id,
            ActivityType.STATUS_CHANGE,
            f"Marked lost: {reason.strip()}"
            + (f" (to {lost_to})" if lost_to else ""),
        )
        return await self.leads.get(lead_id)

    async def reopen(self, lead_id: uuid.UUID, *, actor_id=None, note=None) -> Lead:
        """Bring a lost lead back. They rang again; it happens.

        Returns to CONTACTED rather than NEW, because somebody has spoken to them
        and pretending otherwise would put them back in the untouched queue.
        """
        lead = await self.leads.get(lead_id)
        if lead.status is LeadStatus.CONVERTED:
            raise BusinessRuleViolation(
                f"{lead.full_name} is a buyer now.",
                "There is nothing to reopen - work the contract instead.",
            )
        if lead.is_open:
            raise Conflict(f"{lead.full_name} is already open.")

        lead.status = LeadStatus.CONTACTED
        lead.lost_reason = None
        lead.lost_to = None
        lead.lost_on = None
        await self.db.commit()

        await self._log(
            lead,
            actor_id,
            ActivityType.STATUS_CHANGE,
            note or "Lead reopened.",
        )
        return await self.leads.get(lead_id)

    async def convert(self, lead_id: uuid.UUID, *, actor_id=None):
        """Turn a lead into a buyer, keeping the link.

        The link is the whole reason the lead record survives conversion: without
        it, "which marketing spend produced closings" cannot be answered, only
        "which produced enquiries", and those are different questions with
        different answers.
        """
        from app.models.contract import Buyer

        lead = await self.leads.get(lead_id)

        if lead.converted_buyer_id is not None:
            raise Conflict(f"{lead.full_name} has already been converted.")
        if lead.status is LeadStatus.LOST:
            raise BusinessRuleViolation(
                f"{lead.full_name} is marked lost.",
                "Reopen the lead first, then convert it.",
            )
        if lead.status is LeadStatus.NEW:
            raise BusinessRuleViolation(
                f"Nobody has qualified {lead.full_name} yet.",
                "Qualification is where you find out whether they can actually "
                "pay. Contact and qualify the lead first.",
            )
        # A buyer is a party to a contract. One with no way to reach them cannot
        # be sent a selection deadline or a payment request.
        if not (lead.email or lead.phone or lead.mobile):
            raise BusinessRuleViolation(
                f"{lead.full_name} has no email or phone number.",
                "A buyer needs contact details - they will be sent selection "
                "deadlines and payment requests.",
            )

        buyer = Buyer(
            tenant_id=self.tenant_id,
            first_name=lead.first_name,
            last_name=lead.last_name,
            email=lead.email,
            phone=lead.phone or lead.mobile,
        )
        self.db.add(buyer)
        await self.db.flush()

        lead.converted_buyer_id = buyer.id
        lead.status = LeadStatus.CONVERTED
        lead.converted_on = date.today()
        lead.next_follow_up = None
        await self.db.commit()

        await self._log(
            lead, actor_id, ActivityType.STATUS_CHANGE, "Converted to buyer."
        )
        return await self.leads.get(lead_id)

    async def archive_lead(self, lead_id: uuid.UUID, actor_id, reason=None) -> Lead:
        """Hide a lead without destroying it.

        For junk: the bot submissions and the wrong numbers. Refused on a
        converted lead, because that record is the only trace of where a real
        buyer came from.
        """
        lead = await self.leads.get(lead_id)
        if lead.status is LeadStatus.CONVERTED:
            raise BusinessRuleViolation(
                f"{lead.full_name} converted to a buyer.",
                "This record is the only trace of where that buyer came from, so "
                "it stays. Archive is for junk enquiries.",
            )
        return await self.leads.archive(lead, actor_id, reason)

    async def restore_lead(self, lead_id: uuid.UUID) -> Lead:
        return await self.leads.restore(await self.leads.get(lead_id))

    # ======================================================= realtors =====

    async def list_realtors(self, params: PageParams | None = None):
        if params is None:
            return await self.realtors.all_ordered()
        return await self.realtors.page(params)

    async def realtor_performance(self) -> list[dict]:
        """Every agent with what they have actually delivered.

        Ordered by conversions rather than enquiries, because the agent who sends
        three buyers is worth more than the one who sends thirty browsers.
        """
        agents = await self.realtors.all_ordered()
        counts = await self.realtors.lead_counts()
        rows = []
        for a in agents:
            sent, converted = counts.get(a.id, (0, 0))
            rows.append(
                {
                    "id": a.id,
                    "code": a.code,
                    "name": a.name,
                    "brokerage": a.brokerage,
                    "email": a.email,
                    "phone": a.phone,
                    "commission_percent": a.commission_percent,
                    "leads_sent": sent,
                    "leads_converted": converted,
                    # Left as None rather than 0 when they have sent nothing: a
                    # new agent has no rate yet, which is not the same as 0%.
                    "conversion_rate": round(converted / sent * 100, 1) if sent else None,
                }
            )
        rows.sort(key=lambda r: (-r["leads_converted"], -r["leads_sent"], r["name"]))
        return rows

    async def get_realtor(self, realtor_id: uuid.UUID):
        return await self.realtors.get(realtor_id)

    async def create_realtor(self, *, actor_id=None, **data):
        await self.realtors.require_unique_code(data["code"])
        if data.get("email"):
            data["email"] = str(data["email"])
        return await self.realtors.create(created_by_id=actor_id, **data)

    async def update_realtor(self, realtor_id: uuid.UUID, *, actor_id=None, **data):
        agent = await self.realtors.get(realtor_id)
        new_code = data.get("code")
        if new_code and await self.realtors.code_taken_by_other(new_code, realtor_id):
            raise Conflict(f"Realtor {new_code} already exists.")
        if data.get("email"):
            data["email"] = str(data["email"])
        return await self.realtors.update(agent, updated_by_id=actor_id, **data)

    async def archive_realtor(self, realtor_id: uuid.UUID, actor_id, reason=None):
        """Retire an agent.

        Allowed with history - every lead they ever sent keeps pointing at them,
        which is the difference from deleting. What archiving stops is their name
        appearing in the picker on a new enquiry. Open leads are a different
        matter: those are live work with a commission attached.
        """
        agent = await self.realtors.get(realtor_id)
        open_leads = await self._open_leads_for_realtor(realtor_id)
        if open_leads:
            raise BusinessRuleViolation(
                f"{agent.name} still has {open_leads} open lead"
                f"{'s' if open_leads != 1 else ''}.",
                "Close or reassign those first - there is a commission riding on "
                "each of them.",
            )
        return await self.realtors.archive(agent, actor_id, reason)

    async def restore_realtor(self, realtor_id: uuid.UUID):
        return await self.realtors.restore(await self.realtors.get(realtor_id))

    async def _open_leads_for_realtor(self, realtor_id: uuid.UUID) -> int:
        from sqlalchemy import func, select

        return await self.db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.tenant_id == self.tenant_id,
                Lead.realtor_id == realtor_id,
                Lead.status.in_(OPEN_STATUSES),
            )
        ) or 0

    # ========================================================= helpers =====

    async def _require_colleague(self, user_id: uuid.UUID) -> None:
        """A lead can only be assigned to somebody who works for this builder."""
        from sqlalchemy import select

        from app.models.user import User

        found = await self.db.scalar(
            select(User.id).where(User.id == user_id, User.tenant_id == self.tenant_id)
        )
        if found is None:
            raise NotFound("User", user_id)
