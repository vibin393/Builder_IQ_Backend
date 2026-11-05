"""Marketing campaigns and homeowner satisfaction surveys.

Campaigns exist here to close the attribution loop: a campaign produces leads, leads
convert to buyers, buyers close homes. Without the campaign record the lead's `campaign`
string is just text nobody can total.

Sending real email is out of scope - this models the campaign and its results, and a
delivery provider plugs in behind `send`.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    #: Handed to the provider, or the sign is up and the ad is running. Delivery
    #: numbers arrive while a campaign is in this state.
    RUNNING = "running"
    SENT = "sent"
    CANCELLED = "cancelled"


#: A campaign that has not finished. What "what are we running" means.
ACTIVE_CAMPAIGNS = (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING)


class CampaignChannel(StrEnum):
    """How the campaign reaches people.

    Email is the one with delivery numbers; the rest are money spent where the
    only measurable outcome is the leads that arrive afterwards - which is exactly
    why cost per lead matters more here than open rate.
    """

    EMAIL = "email"
    PRINT = "print"  # local paper, brochure drop
    SIGNAGE = "signage"  # boards on the road, site hoarding
    SOCIAL = "social"
    PORTAL = "portal"  # featured listing on Rightmove and similar
    EVENT = "event"  # parade of homes, open weekend
    RADIO = "radio"
    OTHER = "other"


#: Channels where delivery counts mean something. Reporting an open rate on a
#: road sign would be inventing a number.
MEASURABLE_CHANNELS = (CampaignChannel.EMAIL,)


class SurveyStage(StrEnum):
    """When in the lifecycle the homeowner is asked."""

    CONTRACT = "contract"
    PRE_CLOSING = "pre_closing"
    THIRTY_DAY = "thirty_day"
    ELEVEN_MONTH = "eleven_month"
    WARRANTY = "warranty"


class SurveyStatus(StrEnum):
    """Where a sent survey is in its own small lifecycle."""

    PENDING = "pending"  # sent, awaiting a response
    RESPONDED = "responded"  # the homeowner answered
    DECLINED = "declined"  # the homeowner said no thanks
    EXPIRED = "expired"  # the response window closed with no answer


class Campaign(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_campaigns_tenant_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[CampaignChannel] = mapped_column(
        Enum(
            CampaignChannel,
            name="campaign_channel",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=CampaignChannel.EMAIL,
        nullable=False,
        index=True,
    )
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(String(8000))

    # -- what it cost ---------------------------------------------------------
    #: What was set aside, and what was actually spent. Both, because "we
    #: overspent on the parade again" is a sentence a marketing budget exists to
    #: make sayable.
    budget: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), nullable=False)

    status: Mapped[CampaignStatus] = mapped_column(
        Enum(
            CampaignStatus,
            name="campaign_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=CampaignStatus.DRAFT,
        nullable=False,
        index=True,
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL")
    )
    scheduled_for: Mapped[date | None] = mapped_column(Date)
    sent_date: Mapped[date | None] = mapped_column(Date)
    #: A sign is up for three months and an email lands in one morning. Non-email
    #: channels run over a period, and attributing leads to them needs to know it.
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(String(2000))

    # Delivery counters. Kept on the campaign rather than derived, because the
    # provider reports them in aggregate and there is no per-recipient row to sum.
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bounced_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsubscribed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def open_rate(self) -> float:
        return round(self.opened_count / self.sent_count, 4) if self.sent_count else 0.0

    @property
    def click_rate(self) -> float:
        return round(self.clicked_count / self.sent_count, 4) if self.sent_count else 0.0

    @property
    def has_delivery_numbers(self) -> bool:
        """Whether an open rate means anything for this channel.

        A road sign has no open rate. Showing 0% against one is not a neutral
        default - it reads as a failure rather than as a question that does not
        apply.
        """
        return self.channel in MEASURABLE_CHANNELS

    @property
    def is_over_budget(self) -> bool:
        return self.budget > 0 and self.cost > self.budget

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_CAMPAIGNS

    def __repr__(self) -> str:
        return f"<Campaign {self.code} {self.status}>"


class Survey(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A satisfaction survey sent to one homeowner at one lifecycle stage."""

    __tablename__ = "surveys"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )
    stage: Mapped[SurveyStage] = mapped_column(
        Enum(SurveyStage, name="survey_stage", values_callable=lambda e: [m.value for m in e]),
        default=SurveyStage.THIRTY_DAY,
        nullable=False,
        index=True,
    )
    status: Mapped[SurveyStatus] = mapped_column(
        Enum(SurveyStatus, name="survey_status", values_callable=lambda e: [m.value for m in e]),
        default=SurveyStatus.PENDING,
        nullable=False,
        index=True,
    )
    sent_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)  # response window closes
    responded_date: Mapped[date | None] = mapped_column(Date)

    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminded_date: Mapped[date | None] = mapped_column(Date)

    # 0-10. The industry standard question, so results benchmark against others.
    would_recommend: Mapped[int | None] = mapped_column(Integer)
    # 1-5 department ratings, plus an overall figure distinct from the recommend score.
    rating_sales: Mapped[int | None] = mapped_column(Integer)
    rating_construction: Mapped[int | None] = mapped_column(Integer)
    rating_quality: Mapped[int | None] = mapped_column(Integer)
    rating_warranty: Mapped[int | None] = mapped_column(Integer)
    rating_communication: Mapped[int | None] = mapped_column(Integer)
    rating_value: Mapped[int | None] = mapped_column(Integer)
    overall_satisfaction: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[str | None] = mapped_column(String(4000))

    # Detractor follow-up: a low score is a callback waiting to happen. Tracking the
    # follow-up is what turns "detractor comments are worth reading" into action.
    follow_up_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up_notes: Mapped[str | None] = mapped_column(String(2000))
    follow_up_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def nps_band(self) -> str | None:
        """Net Promoter bands. Detractors are the ones worth reading the comments of."""
        if self.would_recommend is None:
            return None
        if self.would_recommend >= 9:
            return "promoter"
        if self.would_recommend >= 7:
            return "passive"
        return "detractor"

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.status is SurveyStatus.PENDING and self.due_date and self.due_date < date.today()
        )

    @property
    def needs_follow_up(self) -> bool:
        return self.follow_up_required and not self.follow_up_done

    def __repr__(self) -> str:
        return f"<Survey {self.job_id} {self.stage} ({self.status})>"
