"""Communities (developments), their release phases, the plans sold there, and the plots.

A plot's `status` is its **sales** position, and nothing else:

    not_released -> available -> held (a reservation, with an expiry) -> sold -> closed
                        ^              |
                        +--------------+   (hold lapses or is lifted)

Whether a house is being built on it is a separate fact, read from the live job. The
first version set a plot to "held" the moment a home was started on it, which took every
speculative home off the market and made a sales reservation indistinguishable from
"there is a house here".
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


def _values(e):
    return [m.value for m in e]


class LotStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"  # reserved for a prospective buyer, until a date
    SOLD = "sold"  # a contract is signed
    CLOSED = "closed"  # handed over
    NOT_RELEASED = "not_released"


class CommunityStatus(StrEnum):
    PLANNING = "planning"  # land and consents; nothing on sale
    SELLING = "selling"
    SOLD_OUT = "sold_out"  # every plot sold, homes may still be building
    CLOSED = "closed"  # built out and handed over


class Community(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "communities"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_communities_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str | None] = mapped_column(String(120))
    #: County, not state. UK addresses, so the American word would have been
    #: wrong on every screen that showed it.
    county: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[CommunityStatus] = mapped_column(
        Enum(CommunityStatus, name="community_status", values_callable=_values),
        default=CommunityStatus.SELLING,
        nullable=False,
        index=True,
    )
    address: Mapped[str | None] = mapped_column(String(300))  # sales office / site
    postcode: Mapped[str | None] = mapped_column(String(20))
    #: The planning authority. Consents, conditions and S106 obligations all sit with it.
    local_authority: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(2000))
    planned_plots: Mapped[int | None] = mapped_column(Integer)  # the consented number
    opened_on: Mapped[date | None] = mapped_column(Date)  # first plot on sale
    #: Sales per month the land appraisal assumed. Absorption is judged against it.
    target_sales_per_month: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    sales_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    site_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    lots: Mapped[list["Lot"]] = relationship(back_populates="community")

    def __repr__(self) -> str:
        return f"<Community {self.code}>"


class CommunityPhase(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A release of plots. Builders put a development on sale a phase at a time."""

    __tablename__ = "community_phases"
    __table_args__ = (UniqueConstraint("community_id", "name", name="uq_community_phases_name"),)

    community_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("communities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    planned_release: Mapped[date | None] = mapped_column(Date)
    released_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(1000))


class CommunityPlan(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A house type offered on a development, at that development's price.

    The same plan sells for different money in different places; the plan's own base
    price is the fallback, not the answer.
    """

    __tablename__ = "community_plans"
    __table_args__ = (UniqueConstraint("community_id", "plan_id", name="uq_community_plans_pair"),)

    community_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("communities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Lot(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "lots"
    __table_args__ = (
        UniqueConstraint("community_id", "lot_number", name="uq_lots_community_number"),
    )

    community_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("communities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lot_number: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str | None] = mapped_column(String(300))
    #: What a delivery, a surveyor and a building inspector all navigate by.
    postcode: Mapped[str | None] = mapped_column(String(20), index=True)
    #: Geocoded position of the plot, for the homes map. Populated from the address.
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    status: Mapped[LotStatus] = mapped_column(
        Enum(LotStatus, name="lot_status", values_callable=_values),
        default=LotStatus.AVAILABLE,
        nullable=False,
        index=True,
    )
    premium: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)

    phase_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("community_phases.id", ondelete="SET NULL"), index=True
    )
    plot_area_sqm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(String(1000))
    released_on: Mapped[date | None] = mapped_column(Date)

    # --- a reservation: who for, by whom, and until when ------------------------
    # A hold with no end date is how plots quietly stop being sold, so new holds must
    # have one; an expired hold lapses back to available by itself.
    hold_until: Mapped[date | None] = mapped_column(Date)
    hold_reason: Mapped[str | None] = mapped_column(String(300))
    held_on: Mapped[date | None] = mapped_column(Date)
    held_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    held_for_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    community: Mapped[Community] = relationship(back_populates="lots")

    def clear_hold(self) -> None:
        self.hold_until = None
        self.hold_reason = None
        self.held_on = None
        self.held_by_id = None
        self.held_for_lead_id = None

    def __repr__(self) -> str:
        return f"<Lot {self.lot_number} ({self.status})>"
