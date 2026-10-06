"""A buyer's chosen option on a specific job.

Price and cost are snapshotted at selection time. This is the effective-dating that
keeps a signed contract stable while the catalog moves on underneath it.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class SelectionStatus(StrEnum):
    PENDING = "pending"      # chosen, not yet on a signed addendum
    CONFIRMED = "confirmed"  # signed by the buyer, cascaded into budget
    CANCELLED = "cancelled"


class Selection(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "selections"
    __table_args__ = (
        UniqueConstraint("job_id", "option_id", name="uq_selections_job_option"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    option_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("options.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    status: Mapped[SelectionStatus] = mapped_column(
        Enum(
            SelectionStatus,
            name="selection_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SelectionStatus.PENDING,
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Snapshots taken from the catalog when the buyer selected. Never re-read.
    price_at_selection: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cost_at_selection: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    schedule_days_at_selection: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The option's cost per cost code at the moment it was chosen, one unit's worth:
    #: [{"cost_code_id", "vendor_id", "amount"}]. Confirming applies exactly this to the
    #: budget and cancelling takes exactly this back out. Using the catalog's *current*
    #: lines made the two disagree whenever the catalog changed in between.
    cost_breakdown: Mapped[list | None] = mapped_column(JSON)

    selected_date: Mapped[date | None] = mapped_column(Date)
    #: The studio session this was chosen at, where there was one. Without it the
    #: appointment is a diary entry rather than a record of what came out of it.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("design_appointments.id", ondelete="SET NULL"),
        index=True,
    )

    job: Mapped["Job"] = relationship(back_populates="selections")  # noqa: F821
    option: Mapped["Option"] = relationship()  # noqa: F821

    @property
    def extended_price(self) -> Decimal:
        return self.price_at_selection * self.quantity

    @property
    def extended_cost(self) -> Decimal:
        return self.cost_at_selection * self.quantity

    def __repr__(self) -> str:
        return f"<Selection job={self.job_id} option={self.option_id} {self.status}>"
