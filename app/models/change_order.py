"""Change orders: scope changes after the contract is signed.

A selection is the buyer choosing from the catalog. A change order is anything else that
moves money after the fact - a field condition, a plan revision, a trade error. The
critical field is `responsible_party`: who pays. Builders lose margin precisely where
that question goes unanswered.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ChangeOrderStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"  # cascaded into contract, budget and schedule
    REJECTED = "rejected"
    VOID = "void"


class ResponsibleParty(StrEnum):
    """Who absorbs the cost. This is the margin question."""

    BUYER = "buyer"  # billed on: price and cost both move
    BUILDER = "builder"  # absorbed: cost moves, price does not
    TRADE = "trade"  # backcharged: neither price nor budget should rise


class ChangeOrder(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "change_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "co_number", name="uq_cos_tenant_number"),)

    co_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[ChangeOrderStatus] = mapped_column(
        Enum(
            ChangeOrderStatus,
            name="change_order_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ChangeOrderStatus.DRAFT,
        nullable=False,
        index=True,
    )
    responsible_party: Mapped[ResponsibleParty] = mapped_column(
        Enum(
            ResponsibleParty,
            name="responsible_party",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ResponsibleParty.BUYER,
        nullable=False,
    )

    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))

    #: When the client is expected to respond. Used to flag a pending CO as overdue.
    approval_deadline: Mapped[date | None] = mapped_column(Date)
    #: Notes meant for the client — safe to show on a client-facing preview.
    client_notes: Mapped[str | None] = mapped_column(String(2000))
    #: Notes for the builder's own team. Never shown to the client.
    internal_notes: Mapped[str | None] = mapped_column(String(2000))

    # What the buyer is charged. Zero for builder-absorbed and trade-backcharged work.
    price_delta: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    schedule_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_date: Mapped[date | None] = mapped_column(Date)

    cost_lines: Mapped[list["ChangeOrderLine"]] = relationship(
        back_populates="change_order", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def cost_delta(self) -> Decimal:
        return sum((line.amount for line in self.cost_lines), Decimal("0"))

    @property
    def margin_impact(self) -> Decimal:
        """Positive means the change makes the builder money, negative means it costs."""
        return self.price_delta - self.cost_delta

    @property
    def approval_overdue(self) -> bool:
        """A pending change order whose client-response deadline has passed."""
        return bool(
            self.status is ChangeOrderStatus.PENDING_APPROVAL
            and self.approval_deadline is not None
            and self.approval_deadline < date.today()
        )

    def __repr__(self) -> str:
        return f"<ChangeOrder {self.co_number} {self.status}>"


class ChangeOrderLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Budget effect of the change order, per cost code. May be negative (a credit)."""

    __tablename__ = "change_order_lines"

    change_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("change_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))

    change_order: Mapped[ChangeOrder] = relationship(back_populates="cost_lines")
