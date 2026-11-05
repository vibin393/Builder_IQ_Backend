"""Estimates: priced line items that become a proposal, and then a budget.

The budget already existed, but it is the builder's internal number — cost by cost
code. An estimate is the *customer-facing* version of the same work: grouped line
items, each with a cost marked up to a price, that add up to what the owner is asked
to sign. "Create proposal" is the moment the internal estimate becomes the external
offer.

Price is derived from cost and markup, never typed alongside it, so a margin cannot
be quoted that the line items do not actually support.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

ZERO = Decimal("0")


class EstimateStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"  # issued to the owner as a proposal
    ACCEPTED = "accepted"
    DECLINED = "declined"


class Estimate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "estimates"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[EstimateStatus] = mapped_column(
        Enum(
            EstimateStatus, name="estimate_status", values_callable=lambda e: [m.value for m in e]
        ),
        default=EstimateStatus.DRAFT,
        nullable=False,
        index=True,
    )
    #: A default markup offered when a line does not set its own.
    default_markup_percent: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=ZERO, nullable=False
    )
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=ZERO, nullable=False)
    issued_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["EstimateItem"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan"
    )

    @property
    def total_cost(self) -> Decimal:
        return sum((i.extended_cost for i in self.items), start=ZERO)

    @property
    def subtotal(self) -> Decimal:
        """The priced subtotal, before tax."""
        return sum((i.extended_price for i in self.items), start=ZERO)

    @property
    def tax_amount(self) -> Decimal:
        return (self.subtotal * self.tax_percent / Decimal(100)).quantize(Decimal("0.01"))

    @property
    def total_price(self) -> Decimal:
        return self.subtotal + self.tax_amount

    @property
    def margin(self) -> Decimal:
        return self.subtotal - self.total_cost


class EstimateItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "estimate_items"

    estimate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("estimates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Line items are grouped into sections — "Sitework", "Framing" — that print as
    #: subtotalled headings on the proposal.
    group_name: Mapped[str] = mapped_column(String(120), default="General", nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL")
    )
    #: Unit of measure — each, sqft, ft, hour — as BuilderTrend's line Unit.
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=ZERO, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    estimate: Mapped["Estimate"] = relationship(back_populates="items")

    @property
    def unit_price(self) -> Decimal:
        return (self.unit_cost * (Decimal(1) + self.markup_percent / Decimal(100))).quantize(
            Decimal("0.01")
        )

    @property
    def extended_cost(self) -> Decimal:
        return (self.unit_cost * self.quantity).quantize(Decimal("0.01"))

    @property
    def extended_price(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class ClientUpdate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A posted update the homeowner sees — the builder's broadcast channel.

    Not a message (no recipients, no replies) and not a comment (not internal). It is
    an announcement: "foundation poured", "roofing next week", pinned to the job and
    shown in the owner's portal when published.
    """

    __tablename__ = "client_updates"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Draft until published; only published updates reach the owner's portal.
    is_published: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    posted_on: Mapped[date] = mapped_column(Date, default=date.today, nullable=False, index=True)
