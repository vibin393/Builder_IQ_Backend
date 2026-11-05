"""CRM: leads and prospects, before they become buyers.

A lead converts into a Buyer on a Job. Keeping the lead record afterwards is what
makes lead-source reporting possible - which marketing spend actually produced
closings, rather than which produced enquiries.

Three things here are homebuilder-specific and are the reason a generic CRM does
not fit:

  * **the realtor** is a party in their own right, not a note. A large share of
    enquiries arrive through agents, the agent is owed a commission on closing,
    and "which agents actually send us buyers" is a question the business asks
    every quarter.
  * **the contingency**. A buyer with a house to sell is not the same prospect as
    one without, however keen they sound, because their timeline belongs to
    somebody else's sale.
  * **the move-in date**. A family who must be in before the school year has a
    deadline that outranks price.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.mixins import ArchivableMixin, AuditedMixin


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    APPOINTMENT = "appointment"
    CONVERTED = "converted"
    LOST = "lost"


#: Statuses a lead can still be worked from. Converted and lost are terminal.
OPEN_STATUSES = (
    LeadStatus.NEW,
    LeadStatus.CONTACTED,
    LeadStatus.QUALIFIED,
    LeadStatus.APPOINTMENT,
)


class LeadSource(StrEnum):
    WEBSITE = "website"
    WALK_IN = "walk_in"
    REFERRAL = "referral"
    REALTOR = "realtor"
    PORTAL = "portal"
    CAMPAIGN = "campaign"
    SIGNAGE = "signage"
    SOCIAL = "social"
    EVENT = "event"
    OTHER = "other"


class LeadRating(StrEnum):
    """How likely this is to close, as the agent reads it.

    Deliberately three words rather than a score out of a hundred: a salesperson
    will tell you honestly whether someone is hot or cold, and will invent a
    number if you ask for one.
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class FinancingType(StrEnum):
    CASH = "cash"
    CONVENTIONAL = "conventional"
    FHA = "fha"
    VA = "va"
    OTHER = "other"


class ContactMethod(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    TEXT = "text"


class ActivityType(StrEnum):
    CALL = "call"
    EMAIL = "email"
    TEXT = "text"
    VISIT = "visit"
    APPOINTMENT = "appointment"
    NOTE = "note"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT = "assignment"


class ActivityOutcome(StrEnum):
    """What came of the touch. Separate from the type, because a call that went
    to voicemail and a call that booked an appointment are not the same event."""

    REACHED = "reached"
    NO_ANSWER = "no_answer"
    LEFT_MESSAGE = "left_message"
    BOOKED = "booked"
    NOT_INTERESTED = "not_interested"
    NONE = "none"


class Realtor(UUIDMixin, TenantMixin, TimestampMixin, ArchivableMixin, AuditedMixin, Base):
    """An estate agent who sends the builder business.

    A record rather than three text fields on a lead, because the question that
    gets asked is not "who referred this one" but "which agents are worth
    cultivating, and what do we owe them".
    """

    __tablename__ = "realtors"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_realtors_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brokerage: Mapped[str | None] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    licence_number: Mapped[str | None] = mapped_column(String(64))

    #: What this agent earns on a closing, as a percentage of contract value.
    #: Held per agent because it is negotiated per agent.
    commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(2000))

    leads: Mapped[list["Lead"]] = relationship(back_populates="realtor")

    def __repr__(self) -> str:
        return f"<Realtor {self.name} ({self.brokerage})>"


class Lead(UUIDMixin, TenantMixin, TimestampMixin, ArchivableMixin, AuditedMixin, Base):
    __tablename__ = "leads"

    # -- who they are -------------------------------------------------------
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40), index=True)
    mobile: Mapped[str | None] = mapped_column(String(40))
    preferred_contact: Mapped[ContactMethod | None] = mapped_column(
        Enum(ContactMethod, name="contact_method", values_callable=lambda e: [m.value for m in e])
    )
    best_time_to_call: Mapped[str | None] = mapped_column(String(100))

    # Where they live now. Drives catchment analysis - are we drawing from the
    # next town or from across the county?
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120))
    postcode: Mapped[str | None] = mapped_column(String(20))
    #: Geocoded position, for the lead map. Populated from the address when geocoding
    #: is available; a lead with no coordinates simply does not appear on the map.
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    # -- how they found us --------------------------------------------------
    source: Mapped[LeadSource] = mapped_column(
        Enum(LeadSource, name="lead_source", values_callable=lambda e: [m.value for m in e]),
        default=LeadSource.WEBSITE,
        nullable=False,
        index=True,
    )
    #: The free-text campaign tag, kept because inbound channels send whatever
    #: string was configured in them and know nothing about our ids.
    campaign: Mapped[str | None] = mapped_column(String(200), index=True)
    #: The campaign proper, once it has been matched. Attribution used to be a
    #: case-insensitive comparison of the tag above against a campaign code, so
    #: renaming a code silently detached every lead that came from it.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    realtor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("realtors.id", ondelete="SET NULL"), index=True
    )
    referred_by: Mapped[str | None] = mapped_column(String(200))

    # -- what they want -----------------------------------------------------
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL"), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bedrooms_needed: Mapped[int | None] = mapped_column(Integer)
    #: When they need to be in. A school-year deadline outranks price.
    desired_move_in: Mapped[date | None] = mapped_column(Date)
    #: What kind of work this opportunity is — new build, remodel, addition, ADU.
    #: Free text, because every builder's list differs.
    project_type: Mapped[str | None] = mapped_column(String(120))
    #: Free-form labels for filtering the pipeline, as BuilderTrend's opportunity tags.
    tags: Mapped[list | None] = mapped_column(JSON)

    # -- forecast -----------------------------------------------------------
    #: Probability of winning, 0-100. The weight behind a pipeline forecast: a
    #: $500k job at 20% is $100k of expected revenue, not $500k.
    confidence: Mapped[int | None] = mapped_column(Integer)
    #: When the sale is expected to close. Distinct from desired_move_in (the
    #: buyer's deadline) and next_follow_up (the next touch): this is the date the
    #: forecast and the sales calendar hang off.
    projected_sales_date: Mapped[date | None] = mapped_column(Date, index=True)

    # -- can they actually buy ---------------------------------------------
    is_prequalified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lender_name: Mapped[str | None] = mapped_column(String(200))
    financing_type: Mapped[FinancingType | None] = mapped_column(
        Enum(FinancingType, name="financing_type", values_callable=lambda e: [m.value for m in e])
    )
    #: The contingency. A buyer whose deposit depends on their own sale
    #: completing has a timeline that is not theirs to control.
    has_home_to_sell: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -- working the lead ---------------------------------------------------
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status", values_callable=lambda e: [m.value for m in e]),
        default=LeadStatus.NEW,
        nullable=False,
        index=True,
    )
    rating: Mapped[LeadRating | None] = mapped_column(
        Enum(LeadRating, name="lead_rating", values_callable=lambda e: [m.value for m in e]),
        index=True,
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    last_contacted_on: Mapped[date | None] = mapped_column(Date)
    next_follow_up: Mapped[date | None] = mapped_column(Date, index=True)

    # -- endings ------------------------------------------------------------
    # Set when the lead becomes a buyer, so source can be traced to a closing.
    converted_buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )
    converted_on: Mapped[date | None] = mapped_column(Date)
    #: Required when marking a lead lost. "We lost it" is not information;
    #: "they bought from Barratt on price" is.
    lost_reason: Mapped[str | None] = mapped_column(String(500))
    lost_to: Mapped[str | None] = mapped_column(String(200))
    lost_on: Mapped[date | None] = mapped_column(Date)

    notes: Mapped[str | None] = mapped_column(String(2000))

    realtor: Mapped["Realtor | None"] = relationship(back_populates="leads")

    # -- derived ------------------------------------------------------------

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def follow_up_overdue(self) -> bool:
        return bool(self.is_open and self.next_follow_up and self.next_follow_up < date.today())

    @property
    def days_since_contact(self) -> int | None:
        """How long this has been sitting. The number a sales manager scans for."""
        if not self.last_contacted_on:
            return None
        return (date.today() - self.last_contacted_on).days

    @property
    def is_contingent(self) -> bool:
        """They have a house to sell, so their timeline is not their own."""
        return self.has_home_to_sell

    def __repr__(self) -> str:
        return f"<Lead {self.full_name} ({self.status})>"


class LeadActivity(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Every touch on a lead: calls, emails, appointments, notes.

    Status changes and assignments are logged here too, automatically. A history
    that only contains what somebody remembered to type is not a history.
    """

    __tablename__ = "lead_activities"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    activity_type: Mapped[ActivityType] = mapped_column(
        Enum(ActivityType, name="activity_type", values_callable=lambda e: [m.value for m in e]),
        default=ActivityType.NOTE,
        nullable=False,
        index=True,
    )
    outcome: Mapped[ActivityOutcome] = mapped_column(
        Enum(
            ActivityOutcome,
            name="activity_outcome",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ActivityOutcome.NONE,
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    occurred_on: Mapped[date | None] = mapped_column(Date, index=True)
    #: True for entries the system wrote rather than a person, so the UI can
    #: distinguish "Priya called them" from "status moved to qualified".
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
