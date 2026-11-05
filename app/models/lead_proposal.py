"""Lead proposals: the priced offer sent to a prospect before they are a buyer.

A proposal sits on a lead the way an estimate sits on a job — it is the number the
salesperson puts in front of someone to win the work. Accepting one is the moment a
lead has committed to a price, which is exactly what should trigger converting the
lead into a job.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

ZERO = Decimal("0")


class LeadProposalStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class LeadProposal(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "lead_proposals"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    status: Mapped[LeadProposalStatus] = mapped_column(
        Enum(
            LeadProposalStatus,
            name="lead_proposal_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=LeadProposalStatus.DRAFT,
        nullable=False,
        index=True,
    )
    sent_on: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date, index=True)
    decided_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    items: Mapped[list["ProposalItem"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )

    @property
    def line_total(self) -> Decimal:
        return sum((i.extended_price for i in self.items), start=ZERO)

    @property
    def total(self) -> Decimal:
        """Priced from line items when there are any; otherwise the flat `amount`.
        BuilderTrend proposals are estimate-like — a total built from marked-up line
        items — but a quick one-number proposal is still valid, so the flat amount
        remains the fallback rather than being forced to zero."""
        return self.line_total if self.items else self.amount

    @property
    def is_expired(self) -> bool:
        return bool(
            self.status is LeadProposalStatus.SENT
            and self.valid_until
            and self.valid_until < date.today()
        )


class ProposalItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A priced line on a lead proposal — the same cost→markup→price shape as an
    estimate item, so a proposal reads as the offer an estimate becomes."""

    __tablename__ = "proposal_items"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("lead_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_name: Mapped[str] = mapped_column(String(120), default="General", nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Unit of measure — each, sqft, ft, hour — as BuilderTrend's line Unit.
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=ZERO, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    proposal: Mapped["LeadProposal"] = relationship(back_populates="items")

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


class ProposalTemplate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A reusable proposal — a named set of line items the salesperson applies to a
    lead instead of retyping the same scope every time (BuilderTrend's Proposal
    templates)."""

    __tablename__ = "proposal_templates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["ProposalTemplateItem"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )

    @property
    def total(self) -> Decimal:
        return sum((i.extended_price for i in self.items), start=ZERO)


class ProposalTemplateItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "proposal_template_items"

    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("proposal_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_name: Mapped[str] = mapped_column(String(120), default="General", nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=ZERO, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    template: Mapped["ProposalTemplate"] = relationship(back_populates="items")

    @property
    def unit_price(self) -> Decimal:
        return (self.unit_cost * (Decimal(1) + self.markup_percent / Decimal(100))).quantize(
            Decimal("0.01")
        )

    @property
    def extended_price(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))
