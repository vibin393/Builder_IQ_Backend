"""Bid management: getting priced work from trades before committing to it.

A bid package bundles a scope (a plan, a cost code, a set of documents) and goes out
to several trades. Their bids come back, get compared, and one is awarded - which is
what should produce the purchase order, so the price nobody negotiated twice.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BidPackageStatus(StrEnum):
    DRAFT = "draft"
    OUT_FOR_BID = "out_for_bid"
    AWARDED = "awarded"
    CANCELLED = "cancelled"


class BidStatus(StrEnum):
    INVITED = "invited"
    SUBMITTED = "submitted"
    AWARDED = "awarded"
    DECLINED = "declined"
    NOT_AWARDED = "not_awarded"


class BidPackage(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "bid_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_number", name="uq_bid_packages_tenant_number"),
    )

    package_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(4000))

    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # A package is priced against a plan (production) or a specific job (one-off).
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), index=True
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[BidPackageStatus] = mapped_column(
        Enum(
            BidPackageStatus,
            name="bid_package_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=BidPackageStatus.DRAFT,
        nullable=False,
        index=True,
    )
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    # The builder's own target for the work — the number the bids are levelled against,
    # so a package can show whether it came in under or over what was expected.
    estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Trades bid against a specific plan revision; bumping it invalidates open bids.
    document_revision: Mapped[int] = mapped_column(default=1, nullable=False)

    bids: Mapped[list["Bid"]] = relationship(
        back_populates="package", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def _priced(self) -> list[Decimal]:
        return [b.amount for b in self.bids if b.amount is not None]

    @property
    def awarded_bid_id(self) -> uuid.UUID | None:
        for b in self.bids:
            if b.status is BidStatus.AWARDED:
                return b.id
        return None

    @property
    def low_bid(self) -> Decimal | None:
        amounts = self._priced
        return min(amounts) if amounts else None

    @property
    def high_bid(self) -> Decimal | None:
        amounts = self._priced
        return max(amounts) if amounts else None

    @property
    def average_bid(self) -> Decimal | None:
        amounts = self._priced
        if not amounts:
            return None
        return (sum(amounts) / len(amounts)).quantize(Decimal("0.01"))

    @property
    def spread(self) -> Decimal | None:
        """Distance between the lowest and highest price — how much a good comparison
        is worth on this package."""
        if len(self._priced) < 2:
            return None
        return max(self._priced) - min(self._priced)

    @property
    def invited_count(self) -> int:
        return len(self.bids)

    @property
    def submitted_count(self) -> int:
        return len(self._priced)

    @property
    def declined_count(self) -> int:
        return sum(1 for b in self.bids if b.status is BidStatus.DECLINED)

    @property
    def response_rate(self) -> float | None:
        """Fraction of invited trades that have responded — priced or declined. The
        number that says whether the package went to enough trades to trust the low bid."""
        if not self.bids:
            return None
        responded = self.submitted_count + self.declined_count
        return round(responded / len(self.bids), 2)

    @property
    def awarded_amount(self) -> Decimal | None:
        for b in self.bids:
            if b.status is BidStatus.AWARDED:
                return b.amount
        return None

    @property
    def savings_vs_estimate(self) -> Decimal | None:
        """Estimate minus the awarded (or, before award, the low) price. Positive means
        it came in under what the builder expected."""
        if self.estimate is None:
            return None
        benchmark = self.awarded_amount or self.low_bid
        return self.estimate - benchmark if benchmark is not None else None

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.status is BidPackageStatus.OUT_FOR_BID
            and self.due_date
            and self.due_date < date.today()
        )

    def __repr__(self) -> str:
        return f"<BidPackage {self.package_number} {self.status}>"


class Bid(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One trade's response to a bid package."""

    __tablename__ = "bids"
    __table_args__ = (UniqueConstraint("package_id", "vendor_id", name="uq_bids_package_vendor"),)

    package_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bid_packages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[BidStatus] = mapped_column(
        Enum(BidStatus, name="bid_status", values_callable=lambda e: [m.value for m in e]),
        default=BidStatus.INVITED,
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    submitted_date: Mapped[date | None] = mapped_column(Date)
    # The revision this price was quoted against. If the package moves past it, the
    # bid is stale and must be re-solicited rather than silently used.
    revision_bid: Mapped[int] = mapped_column(default=1, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))

    package: Mapped[BidPackage] = relationship(back_populates="bids")

    def __repr__(self) -> str:
        return f"<Bid {self.vendor_id} {self.amount}>"
