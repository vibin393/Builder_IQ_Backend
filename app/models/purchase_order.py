"""Purchase orders: the builder's commitment of money to a vendor on a job.

A PO moves budget from "planned" to "committed". Committed cost is the number that
tells a builder what a house will actually cost before the invoices arrive, so POs are
never edited once issued - they are superseded by a change order.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.invoice import Invoice


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"  # sent to the vendor; counts as committed cost
    ACKNOWLEDGED = "acknowledged"
    COMPLETE = "complete"
    VOID = "void"  # no longer committed


COMMITTED_STATUSES = (
    PurchaseOrderStatus.ISSUED,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.COMPLETE,
)


class PurchaseOrder(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "po_number", name="uq_pos_tenant_number"),)

    po_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(
            PurchaseOrderStatus,
            name="purchase_order_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PurchaseOrderStatus.DRAFT,
        nullable=False,
        index=True,
    )
    issued_date: Mapped[date | None] = mapped_column(Date)
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # When the materials or the work are needed on site. Past it and still open, the
    # PO is late — the question a site manager asks before any other.
    required_by: Mapped[date | None] = mapped_column(Date, index=True)
    acknowledged_on: Mapped[date | None] = mapped_column(Date)
    completed_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(1000))

    # Both issue gates can be overridden deliberately. These record that it happened,
    # and why — an override that leaves no trace is indistinguishable from a bug.
    compliance_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    budget_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(String(500))

    # Percentage held back from every payment against this commitment until the
    # work is complete and signed off. 0 means pay in full, which is the default
    # for material supply; trade labour is usually 5-10%.
    retainage_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), nullable=False
    )

    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan", lazy="selectin"
    )
    # Read-only view of what has been billed against this commitment.
    invoices: Mapped[list["Invoice"]] = relationship(viewonly=True, lazy="selectin")

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def invoiced_total(self) -> Decimal:
        """Everything the trade has invoiced, whatever its approval state."""
        from app.models.invoice import InvoiceStatus

        return sum(
            (i.total for i in self.invoices if i.status is not InvoiceStatus.VOID),
            Decimal("0"),
        )

    @property
    def billed_to_date(self) -> Decimal:
        """Invoiced and approved — the part of the commitment that is now actual cost."""
        from app.models.invoice import ACTUAL_STATUSES

        return sum((i.total for i in self.invoices if i.status in ACTUAL_STATUSES), Decimal("0"))

    @property
    def remaining_to_bill(self) -> Decimal:
        return self.total - self.invoiced_total

    @property
    def percent_billed(self) -> float:
        if not self.total:
            return 0.0
        return round(float(self.billed_to_date / self.total * 100), 1)

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.required_by
            and self.required_by < date.today()
            and self.status in (PurchaseOrderStatus.ISSUED, PurchaseOrderStatus.ACKNOWLEDGED)
        )

    @property
    def is_committed(self) -> bool:
        return self.status in COMMITTED_STATUSES

    @property
    def retainage_on_total(self) -> Decimal:
        """What would be withheld if the whole commitment were billed today."""
        return (self.total * self.retainage_percent / Decimal("100")).quantize(Decimal("0.01"))

    def __repr__(self) -> str:
        return f"<PurchaseOrder {self.po_number} {self.status}>"


class PurchaseOrderLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "purchase_order_lines"

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
