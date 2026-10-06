"""Buyers and the sales contract.

The contract holds the numbers as signed. Option prices are snapshotted onto
Selections, never re-read from the catalog, so a later catalog change cannot silently
alter a signed contract.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ContractStatus(StrEnum):
    DRAFT = "draft"
    OUT_FOR_SIGNATURE = "out_for_signature"
    SIGNED = "signed"
    CANCELLED = "cancelled"


class Buyer(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "buyers"

    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<Buyer {self.full_name}>"


class Contract(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("job_id", name="uq_contracts_job"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    status: Mapped[ContractStatus] = mapped_column(
        Enum(
            ContractStatus,
            name="contract_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ContractStatus.DRAFT,
        nullable=False,
        index=True,
    )

    # Price components, held as agreed. options_total is maintained from Selections.
    base_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    lot_premium: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    options_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    #: What the buyer may spend on options before it costs them extra, included in
    #: the base price. The number the whole design studio revolves around: without
    #: it there is nothing to overspend, and "you are 4,200 over" - the sentence a
    #: design consultant says most - cannot be said at all.
    options_allowance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    incentives: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )

    signed_date: Mapped[date | None] = mapped_column(Date)
    sales_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    buyer: Mapped[Buyer] = relationship()

    @property
    def total_price(self) -> Decimal:
        return self.base_price + self.lot_premium + self.options_total - self.incentives

    @property
    def allowance_remaining(self) -> Decimal:
        """What is left of the allowance. Negative once they are over it."""
        return self.options_allowance - self.options_total

    @property
    def is_over_allowance(self) -> bool:
        return self.options_total > self.options_allowance

    def __repr__(self) -> str:
        return f"<Contract job={self.job_id} {self.status}>"
