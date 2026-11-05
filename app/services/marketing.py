"""Business tier for campaigns.

A campaign screen that reports opens and clicks is reporting the wrong thing. The
question a homebuilder asks about marketing is not "did people open the email" -
it is **what did a lead cost, what did a sale cost, and did the spend come back**.
Three campaigns with identical open rates can differ by an order of magnitude on
cost per sale, and only one of them is worth running again.

So the numbers this module computes are:

  * **cost per lead** - spend divided by the enquiries it produced
  * **cost per sale** - spend divided by the ones that became buyers, which is the
    figure that actually decides next year's budget
  * **attributed revenue** - the contract value of those buyers, traced
    lead → buyer → contract, so a campaign can be compared against what it earned
    rather than only against what it cost

Two things this deliberately does not do:

  * **it does not invent an open rate for a road sign.** Delivery numbers belong
    to email; showing 0% against a hoarding reads as failure rather than as a
    question that does not apply.
  * **it does not send anything.** A delivery provider does that and reports back.
    This records what was spent and what arrived.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.contract import Contract
from app.models.crm import Lead, LeadStatus
from app.models.marketing import (
    MEASURABLE_CHANNELS,
    Campaign,
    CampaignChannel,
    CampaignStatus,
)
from app.repositories.base import Conflict, TenantRepository
from app.repositories.catalog import CommunityRepository
from app.schemas.common import PageParams

#: Where a campaign may go from where it is.
#:
#: `running` exists between scheduled and sent because a sign is up for three
#: months and an email lands in one morning - and delivery numbers arrive while
#: the thing is still going, not only once it has finished.
MOVES: dict[CampaignStatus, tuple[CampaignStatus, ...]] = {
    CampaignStatus.DRAFT: (
        CampaignStatus.SCHEDULED,
        CampaignStatus.RUNNING,
        CampaignStatus.CANCELLED,
    ),
    CampaignStatus.SCHEDULED: (
        CampaignStatus.RUNNING,
        CampaignStatus.DRAFT,
        CampaignStatus.CANCELLED,
    ),
    CampaignStatus.RUNNING: (CampaignStatus.SENT, CampaignStatus.CANCELLED),
    # Finished. A campaign that ran is history; the next one is a new record, which
    # is what Duplicate is for.
    CampaignStatus.SENT: (),
    CampaignStatus.CANCELLED: (),
}


class CampaignRepository(TenantRepository[Campaign]):
    model = Campaign
    label = "Campaign"
    search_fields = ("code", "name", "subject")

    async def all_ordered(self) -> list[Campaign]:
        return await self.list(order_by=Campaign.created_at.desc())

    async def page(self, params: PageParams, **filters):
        return await self.paginate(params, order_by=Campaign.created_at.desc(), **filters)

    async def code_taken_by_other(self, code: str, exclude_id: uuid.UUID) -> bool:
        found = await self.db.scalar(
            select(Campaign.id).where(
                Campaign.tenant_id == self.tenant_id,
                func.lower(Campaign.code) == code.lower(),
                Campaign.id != exclude_id,
            )
        )
        return found is not None


class MarketingService:
    """Campaigns and what they produced, scoped to one builder."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.campaigns = CampaignRepository(db, tenant_id)
        self.communities = CommunityRepository(db, tenant_id)

    # ==================================================== reading them =====

    async def list_campaigns(self, params: PageParams | None = None, **filters):
        if params is None:
            return await self.campaigns.all_ordered()
        return await self.campaigns.page(params, **filters)

    async def get_campaign(self, campaign_id: uuid.UUID) -> Campaign:
        return await self.campaigns.get(campaign_id)

    # ===================================================== building one =====

    async def create_campaign(self, *, actor_id=None, **data) -> Campaign:
        await self.campaigns.require_unique_code(data["code"])
        if data.get("community_id"):
            await self.communities.require_exists(data["community_id"])
        self._require_sensible_dates(data.get("starts_on"), data.get("ends_on"))
        return await self.campaigns.create(**data)

    async def update_campaign(self, campaign_id: uuid.UUID, **data) -> Campaign:
        """Edit a campaign.

        Refused once it has run: the numbers hanging off a finished campaign were
        produced by what it actually said and cost, and editing that afterwards
        turns the report into fiction.
        """
        campaign = await self.campaigns.get(campaign_id)
        if campaign.status is CampaignStatus.SENT:
            raise BusinessRuleViolation(
                f"{campaign.name} has already run.",
                "Its results were produced by what it actually said and cost. "
                "Duplicate it instead - the copy starts as a draft you can change.",
            )
        if campaign.status is CampaignStatus.CANCELLED:
            raise Conflict(f"{campaign.name} was cancelled.")

        new_code = data.get("code")
        if new_code and await self.campaigns.code_taken_by_other(new_code, campaign_id):
            raise Conflict(f"Campaign {new_code} already exists.")
        if data.get("community_id"):
            await self.communities.require_exists(data["community_id"])
        self._require_sensible_dates(
            data.get("starts_on", campaign.starts_on), data.get("ends_on", campaign.ends_on)
        )
        return await self.campaigns.update(campaign, **data)

    async def duplicate(self, campaign_id: uuid.UUID, new_code: str) -> Campaign:
        """Copy a campaign as a fresh draft.

        Most campaigns are last year's campaign with the dates changed. Copying
        keeps the content and the budget and deliberately drops the results - the
        new one has produced nothing yet, and carrying the old numbers across would
        be the most misleading possible default.
        """
        original = await self.campaigns.get(campaign_id)
        await self.campaigns.require_unique_code(new_code)
        return await self.campaigns.create(
            code=new_code,
            name=f"{original.name} (copy)",
            channel=original.channel,
            subject=original.subject,
            body=original.body,
            budget=original.budget,
            community_id=original.community_id,
            notes=original.notes,
            status=CampaignStatus.DRAFT,
        )

    def _require_sensible_dates(self, starts, ends) -> None:
        if starts and ends and ends < starts:
            raise BusinessRuleViolation(
                "The campaign ends before it starts.",
                "Check the dates - as entered it runs for a negative number of days.",
            )

    # ====================================================== the lifecycle =====

    def _require_move(self, campaign: Campaign, to: CampaignStatus) -> None:
        allowed = MOVES[campaign.status]
        if to in allowed:
            return
        if campaign.status is CampaignStatus.SENT:
            raise BusinessRuleViolation(
                f"{campaign.name} has already run.",
                "Duplicate it to run something like it again - the results on this "
                "one belong to what it actually was.",
            )
        if campaign.status is CampaignStatus.CANCELLED:
            raise Conflict(f"{campaign.name} was cancelled.")
        raise BusinessRuleViolation(
            f"{campaign.name} cannot go from {campaign.status.value} to {to.value}.",
            "Next steps from here: " + (", ".join(s.value for s in allowed) or "none") + ".",
        )

    async def schedule(self, campaign_id: uuid.UUID, when: date) -> Campaign:
        """Put a date on it without committing the spend."""
        campaign = await self.campaigns.get(campaign_id)
        self._require_move(campaign, CampaignStatus.SCHEDULED)
        if when < date.today():
            raise BusinessRuleViolation(
                "That date has passed.",
                "Schedule it forward, or launch it now if it is already out.",
            )
        campaign.status = CampaignStatus.SCHEDULED
        campaign.scheduled_for = when
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    async def launch(self, campaign_id: uuid.UUID, *, starts_on: date | None = None) -> Campaign:
        """It is out: handed to the provider, or the sign is up.

        Email needs a subject before it goes anywhere - a campaign that arrives
        with an empty subject line is worse than one that did not arrive.
        """
        campaign = await self.campaigns.get(campaign_id)
        self._require_move(campaign, CampaignStatus.RUNNING)

        if campaign.channel is CampaignChannel.EMAIL and not (campaign.subject or "").strip():
            raise BusinessRuleViolation(
                f"{campaign.name} has no subject line.",
                "An email arriving with an empty subject is worse than one that does not arrive.",
            )

        campaign.status = CampaignStatus.RUNNING
        campaign.starts_on = starts_on or campaign.starts_on or date.today()
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    async def record_delivery(self, campaign_id: uuid.UUID, **counts) -> Campaign:
        """Take the numbers the provider reported back.

        Checked against each other rather than trusted: more opens than sends, or
        more clicks than opens, means the numbers came from somewhere they should
        not have, and a wrong open rate on a report nobody questions is worse than
        an error now.
        """
        campaign = await self.campaigns.get(campaign_id)
        if campaign.status is CampaignStatus.CANCELLED:
            raise Conflict(f"{campaign.name} was cancelled.")
        if not campaign.has_delivery_numbers:
            raise BusinessRuleViolation(
                f"{campaign.name} is a {campaign.channel.value} campaign.",
                "Delivery numbers belong to email. Judge this one on the leads it "
                "produced and what they cost.",
            )

        sent = counts.get("sent_count", campaign.sent_count) or 0
        opened = counts.get("opened_count", campaign.opened_count) or 0
        clicked = counts.get("clicked_count", campaign.clicked_count) or 0
        bounced = counts.get("bounced_count", campaign.bounced_count) or 0

        if opened > sent:
            raise BusinessRuleViolation(
                f"More opens ({opened}) than sends ({sent}).",
                "Check the figures from the provider - as given, the open rate would be over 100%.",
            )
        if clicked > opened:
            raise BusinessRuleViolation(
                f"More clicks ({clicked}) than opens ({opened}).",
                "Somebody has to open an email before they can click it.",
            )
        if bounced > sent:
            raise BusinessRuleViolation(
                f"More bounces ({bounced}) than sends ({sent}).", "Check the figures."
            )

        for field, value in counts.items():
            if value is not None:
                setattr(campaign, field, value)
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    async def record_cost(self, campaign_id: uuid.UUID, cost: Decimal) -> Campaign:
        """What it actually cost, as opposed to what was budgeted."""
        campaign = await self.campaigns.get(campaign_id)
        if cost < 0:
            raise BusinessRuleViolation(
                "A cost cannot be negative.",
                "Use a credit note against the invoice rather than a negative spend.",
            )
        campaign.cost = cost
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    async def complete(self, campaign_id: uuid.UUID, *, ends_on: date | None = None) -> Campaign:
        """It has finished running."""
        campaign = await self.campaigns.get(campaign_id)
        self._require_move(campaign, CampaignStatus.SENT)
        campaign.status = CampaignStatus.SENT
        campaign.sent_date = campaign.sent_date or date.today()
        campaign.ends_on = ends_on or campaign.ends_on or date.today()
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    async def cancel(self, campaign_id: uuid.UUID, reason: str) -> Campaign:
        campaign = await self.campaigns.get(campaign_id)
        self._require_move(campaign, CampaignStatus.CANCELLED)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Cancelling a campaign needs a reason.",
                "Money was committed against it and somebody will ask what happened.",
            )
        campaign.status = CampaignStatus.CANCELLED
        campaign.cancellation_reason = reason.strip()
        await self.db.commit()
        return await self.campaigns.get(campaign_id)

    # ====================================================== attribution =====

    async def attach_leads(self, campaign_id: uuid.UUID) -> int:
        """Link leads whose free-text tag matches this campaign's code.

        Attribution used to be this comparison, performed live in the report, so
        renaming a code detached every lead that came from it without anything
        going wrong anywhere visible. Doing it once and storing the link means a
        rename is just a rename.
        """
        campaign = await self.campaigns.get(campaign_id)
        result = await self.db.execute(
            select(Lead).where(
                Lead.tenant_id == self.tenant_id,
                Lead.campaign_id.is_(None),
                Lead.campaign.is_not(None),
                func.lower(Lead.campaign) == campaign.code.lower(),
            )
        )
        leads = list(result.scalars())
        for lead in leads:
            lead.campaign_id = campaign.id
        await self.db.commit()
        return len(leads)

    async def performance(self) -> dict:
        """Every campaign with what it cost and what it produced.

        The ordering is by cost per sale rather than by leads, because the
        campaign that produced the most enquiries is very often not the one worth
        repeating - and a list sorted by volume quietly argues that it is.
        """
        campaigns = await self.campaigns.all_ordered()
        rows = []
        total_cost = Decimal("0")
        total_revenue = Decimal("0")
        total_leads = total_converted = 0

        for c in campaigns:
            leads, converted, revenue = await self._results_for(c.id)
            total_cost += c.cost
            total_revenue += revenue
            total_leads += leads
            total_converted += converted

            rows.append(
                {
                    "campaign_id": c.id,
                    "code": c.code,
                    "name": c.name,
                    "channel": c.channel.value,
                    "status": c.status.value,
                    "budget": c.budget,
                    "cost": c.cost,
                    "is_over_budget": c.is_over_budget,
                    # Null rather than zero where the channel has none. A 0% open
                    # rate on a road sign reads as failure rather than as a
                    # question that does not apply.
                    "sent": c.sent_count if c.has_delivery_numbers else None,
                    "opened": c.opened_count if c.has_delivery_numbers else None,
                    "clicked": c.clicked_count if c.has_delivery_numbers else None,
                    "open_rate": c.open_rate if c.has_delivery_numbers else None,
                    "click_rate": c.click_rate if c.has_delivery_numbers else None,
                    "leads": leads,
                    "converted": converted,
                    "revenue": revenue,
                    # Null when there is nothing to divide by. "Infinity per lead"
                    # is not a number anybody can act on, and zero would be a lie.
                    "cost_per_lead": _divide(c.cost, leads),
                    "cost_per_sale": _divide(c.cost, converted),
                    "conversion_rate": round(converted / leads * 100, 1) if leads else None,
                    "return_on_spend": (
                        round(float(revenue / c.cost), 2) if c.cost > 0 and revenue else None
                    ),
                }
            )

        # Campaigns that produced a sale first, cheapest per sale at the top; then
        # the ones that produced leads but no sale; then everything else.
        rows.sort(
            key=lambda r: (
                r["cost_per_sale"] is None,
                r["cost_per_sale"] or Decimal("0"),
                -r["leads"],
            )
        )

        return {
            "campaigns": rows,
            "total_cost": total_cost,
            "total_leads": total_leads,
            "total_converted": total_converted,
            "total_revenue": total_revenue,
            "cost_per_lead": _divide(total_cost, total_leads),
            "cost_per_sale": _divide(total_cost, total_converted),
            "return_on_spend": (
                round(float(total_revenue / total_cost), 2)
                if total_cost > 0 and total_revenue
                else None
            ),
        }

    async def _results_for(self, campaign_id: uuid.UUID) -> tuple[int, int, Decimal]:
        """Leads, conversions and contract value produced by one campaign.

        Revenue is traced lead → buyer → contract. That chain is the whole reason
        the lead record survives conversion: without it a campaign can only be
        judged on enquiries, and enquiries are not what pays for the marketing.
        """
        leads = (
            await self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.tenant_id == self.tenant_id, Lead.campaign_id == campaign_id)
            )
            or 0
        )

        converted = (
            await self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.campaign_id == campaign_id,
                    Lead.status == LeadStatus.CONVERTED,
                )
            )
            or 0
        )

        contracts = await self.db.execute(
            select(Contract)
            .join(Lead, Lead.converted_buyer_id == Contract.buyer_id)
            .where(
                Contract.tenant_id == self.tenant_id,
                Lead.tenant_id == self.tenant_id,
                Lead.campaign_id == campaign_id,
            )
        )
        revenue = sum((c.total_price for c in contracts.scalars()), Decimal("0"))
        return leads, converted, revenue

    async def summary(self) -> dict:
        """The strip above the list."""
        counts = {
            row[0].value: row[1]
            for row in await self.db.execute(
                select(Campaign.status, func.count(Campaign.id))
                .where(Campaign.tenant_id == self.tenant_id)
                .group_by(Campaign.status)
            )
        }
        spend = await self.db.scalar(
            select(func.coalesce(func.sum(Campaign.cost), 0)).where(
                Campaign.tenant_id == self.tenant_id
            )
        ) or Decimal("0")
        attributed = (
            await self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.tenant_id == self.tenant_id, Lead.campaign_id.is_not(None))
            )
            or 0
        )
        converted = (
            await self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.campaign_id.is_not(None),
                    Lead.status == LeadStatus.CONVERTED,
                )
            )
            or 0
        )
        #: Leads carrying a tag that matches no campaign. Money spent somewhere
        #: nobody can name, which is worth surfacing rather than silently dropping.
        unmatched = (
            await self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.campaign.is_not(None),
                    Lead.campaign_id.is_(None),
                )
            )
            or 0
        )

        return {
            "by_status": {s.value: counts.get(s.value, 0) for s in CampaignStatus},
            "active": sum(
                counts.get(s.value, 0) for s in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING)
            ),
            "total_spend": spend,
            "leads_attributed": attributed,
            "leads_converted": converted,
            "unmatched_tags": unmatched,
            "cost_per_lead": _divide(spend, attributed),
            "cost_per_sale": _divide(spend, converted),
        }


def _divide(amount: Decimal, by: int) -> Decimal | None:
    """Cost per something, or None when there is no something.

    Zero would be a lie and infinity is not actionable; None lets the screen say
    "no leads yet", which is the truth.
    """
    if not by:
        return None
    return (amount / by).quantize(Decimal("0.01"))


__all__ = ["MEASURABLE_CHANNELS", "CampaignRepository", "MarketingService"]
