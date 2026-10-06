"""House plans (house types), their elevations, standard costs and price history.

A Plan is a template. Its cost lines are the estimator's standard cost per cost code,
and they are what a job budget is generated from. Changing a plan changes what the
*next* home is budgeted and priced at - a budget and a contract are snapshots.
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
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

SQFT_PER_SQM = Decimal("10.7639")


def _values(e):
    return [m.value for m in e]


class HouseType(StrEnum):
    DETACHED = "detached"
    SEMI_DETACHED = "semi_detached"
    TERRACED = "terraced"
    END_TERRACE = "end_terrace"
    BUNGALOW = "bungalow"
    APARTMENT = "apartment"
    MAISONETTE = "maisonette"
    COACH_HOUSE = "coach_house"


class GarageType(StrEnum):
    NONE = "none"
    SINGLE = "single"
    DOUBLE = "double"
    INTEGRAL = "integral"
    CARPORT = "carport"


class Plan(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_plans_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    square_feet: Mapped[int | None] = mapped_column(Integer)
    #: Gross internal area. UK buyers are quoted both; this one is what is measured.
    floor_area_sqm: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    #: Full bathrooms, en-suites included. The US "2.5" (a half bath) is a cloakroom here.
    bathrooms: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    en_suites: Mapped[int | None] = mapped_column(Integer)
    has_cloakroom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    house_type: Mapped[HouseType | None] = mapped_column(
        Enum(HouseType, name="plan_house_type", values_callable=_values)
    )
    storeys: Mapped[int | None] = mapped_column(Integer)
    garage: Mapped[GarageType | None] = mapped_column(
        Enum(GarageType, name="plan_garage_type", values_callable=_values)
    )
    parking_spaces: Mapped[int | None] = mapped_column(Integer)
    #: The EPC band the specification is designed to achieve (A-G).
    epc_target: Mapped[str | None] = mapped_column(String(1))
    description: Mapped[str | None] = mapped_column(String(2000))
    base_price: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    #: Archived plans are kept for the homes already built, but no new home can use one.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    cost_lines: Mapped[list["PlanCostLine"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Plan {self.code}>"


class PlanCostLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Standard cost for one cost code on this plan."""

    __tablename__ = "plan_cost_lines"
    __table_args__ = (
        UniqueConstraint("plan_id", "cost_code_id", name="uq_plan_cost_lines_plan_code"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # The standard hours for this code on this plan, carried onto the budget when
    # one is generated. Typing them per house is how two identical homes end up
    # measured against different targets.
    labour_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    description: Mapped[str | None] = mapped_column(String(300))

    plan: Mapped[Plan] = relationship(back_populates="cost_lines")


class PlanElevation(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A front the plan can be built with - brick, render, stone - at its own price."""

    __tablename__ = "plan_elevations"
    __table_args__ = (UniqueConstraint("plan_id", "code", name="uq_plan_elevations_code"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Added to the house price when a home is built with this front. Can be negative.
    price_adjustment: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PlanPriceChange(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Every change to a plan's base price, with who and why. Price reviews are a board
    decision; "who put the Maple up by £5k, and when" has to have an answer."""

    __tablename__ = "plan_price_changes"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    old_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    new_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(300))
