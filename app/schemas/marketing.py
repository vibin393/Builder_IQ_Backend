"""Request and response shapes for campaigns."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.marketing import CampaignChannel, CampaignStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CampaignCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    #: Email is the only channel with delivery numbers. The rest are money spent
    #: where the only measurable outcome is the leads that arrive afterwards.
    channel: CampaignChannel = CampaignChannel.EMAIL
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=8000)
    budget: Money = Field(default=Decimal("0"), ge=0)
    cost: Money = Field(default=Decimal("0"), ge=0)
    community_id: uuid.UUID | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CampaignUpdate(BaseModel):
    """Editing a campaign. Status is absent - it moves through its own routes."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    channel: CampaignChannel | None = None
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=8000)
    budget: Money | None = Field(default=None, ge=0)
    community_id: uuid.UUID | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CampaignRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    channel: CampaignChannel
    status: CampaignStatus
    subject: str | None
    body: str | None
    budget: Money
    cost: Money
    community_id: uuid.UUID | None
    scheduled_for: date | None
    sent_date: date | None
    starts_on: date | None
    ends_on: date | None
    cancellation_reason: str | None
    notes: str | None

    sent_count: int
    opened_count: int
    clicked_count: int
    bounced_count: int
    unsubscribed_count: int
    open_rate: float
    click_rate: float

    #: Whether an open rate means anything here. A road sign has none, and showing
    #: 0% against one reads as failure rather than as a question that does not apply.
    has_delivery_numbers: bool
    is_over_budget: bool
    is_active: bool


class ScheduleRequest(BaseModel):
    scheduled_for: date


class LaunchRequest(BaseModel):
    starts_on: date | None = None


class CompleteRequest(BaseModel):
    ends_on: date | None = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class DuplicateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CostRequest(BaseModel):
    cost: Money = Field(ge=0)


class DeliveryRequest(BaseModel):
    """What the provider reported back.

    Checked against each other in the service: more opens than sends, or more
    clicks than opens, means the numbers came from somewhere they should not have.
    """

    sent_count: int | None = Field(default=None, ge=0)
    opened_count: int | None = Field(default=None, ge=0)
    clicked_count: int | None = Field(default=None, ge=0)
    bounced_count: int | None = Field(default=None, ge=0)
    unsubscribed_count: int | None = Field(default=None, ge=0)


class CampaignResult(BaseModel):
    """One campaign with what it cost and what it produced.

    Costs per lead and per sale are null rather than zero when there is nothing to
    divide by: zero would be a lie and infinity is not actionable, so the screen
    says "no leads yet" instead.
    """

    campaign_id: uuid.UUID
    code: str
    name: str
    channel: str
    status: str
    budget: Money
    cost: Money
    is_over_budget: bool

    sent: int | None
    opened: int | None
    clicked: int | None
    open_rate: float | None
    click_rate: float | None

    leads: int
    converted: int
    revenue: Money
    cost_per_lead: Money | None
    cost_per_sale: Money | None
    conversion_rate: float | None
    return_on_spend: float | None


class PerformanceReport(BaseModel):
    """Every campaign, ordered by cost per sale rather than by volume.

    The campaign that produced the most enquiries is very often not the one worth
    repeating, and a list sorted by volume quietly argues that it is.
    """

    campaigns: list[CampaignResult]
    total_cost: Money
    total_leads: int
    total_converted: int
    total_revenue: Money
    cost_per_lead: Money | None
    cost_per_sale: Money | None
    return_on_spend: float | None


class MarketingSummary(BaseModel):
    by_status: dict[str, int]
    active: int
    total_spend: Money
    leads_attributed: int
    leads_converted: int
    #: Leads carrying a campaign tag that matches nothing. Money spent somewhere
    #: nobody can name.
    unmatched_tags: int
    cost_per_lead: Money | None
    cost_per_sale: Money | None


class AttachResult(BaseModel):
    attached: int
