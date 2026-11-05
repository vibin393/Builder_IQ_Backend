"""The option catalog - the pivot of the whole system.

A single option is simultaneously:
  * a price      -> it changes the contract amount
  * a cost       -> it changes the job budget, per cost code
  * a commitment -> it changes purchase orders to trades
  * a duration   -> it can shift the construction schedule

Price and cost are held here as the *current catalog* values. When a buyer selects an
option the values are snapshotted onto the Selection, so a signed contract is never
altered by a later catalog price change.
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OptionCategory(StrEnum):
    STRUCTURAL = "structural"
    INTERIOR = "interior"
    EXTERIOR = "exterior"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    FLOORING = "flooring"
    APPLIANCE = "appliance"
    KITCHEN = "kitchen"
    BATHROOM = "bathroom"
    HEATING = "heating"  # heat pumps, underfloor heating, solar PV
    LANDSCAPING = "landscaping"
    TECHNOLOGY = "technology"  # data cabling, EV charger, smart home
    OTHER = "other"


class Option(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "options"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_options_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    category: Mapped[OptionCategory] = mapped_column(
        Enum(
            OptionCategory,
            name="option_category",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OptionCategory.OTHER,
        nullable=False,
        index=True,
    )
    # What the buyer pays.
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    # Days added to (or removed from) the construction schedule.
    schedule_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Structural options must be chosen before construction reaches a cut-off stage.
    is_structural: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: How many a buyer can take. 1 for a kitchen upgrade; 20 for extra sockets.
    max_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: What one of them is: "each", "m²", "room". Shown with the quantity.
    unit: Mapped[str | None] = mapped_column(String(20))
    #: The supplier's product reference, for purchasing.
    supplier_ref: Mapped[str | None] = mapped_column(String(64))

    cost_lines: Mapped[list["OptionCostLine"]] = relationship(
        back_populates="option", cascade="all, delete-orphan"
    )

    @property
    def total_cost(self) -> Decimal:
        return sum((line.amount for line in self.cost_lines), Decimal("0"))

    def __repr__(self) -> str:
        return f"<Option {self.code} {self.name}>"


class OptionCostLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """What this option does to the budget, per cost code. May be negative (a credit)."""

    __tablename__ = "option_cost_lines"
    __table_args__ = (
        UniqueConstraint("option_id", "cost_code_id", name="uq_option_cost_lines_option_code"),
    )

    option_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("options.id", ondelete="CASCADE"),
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

    option: Mapped[Option] = relationship(back_populates="cost_lines")


class OptionPlan(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """The house types an option is offered on. None at all means every plan.

    A loft conversion is only buildable on the plans with the roof pitch for it; a
    double-garage upgrade only on the plots with the width. Offering it everywhere is
    how a buyer is sold something the site manager then cannot build.
    """

    __tablename__ = "option_plans"
    __table_args__ = (UniqueConstraint("option_id", "plan_id", name="uq_option_plans_pair"),)

    option_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class OptionPriceChange(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Every change to an option's price. Selections already made keep theirs."""

    __tablename__ = "option_price_changes"

    option_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    old_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    new_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(300))
