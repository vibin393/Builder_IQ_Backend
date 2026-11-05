"""Cost inbox: the staging queue for incoming costs before they become bills.

A receipt photographed on site, a supplier statement forwarded by email, a card
charge — a cost arrives before anyone has decided which job and cost code it belongs
to. Dropping it straight into the budget would mean coding it in a hurry or losing
it; leaving it in an inbox means it is captured now and coded deliberately later.

An item leaves the inbox when it is pushed to a bill (a vendor `Invoice`) or
rejected. Until then it is a cost the business can see coming but has not committed.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, Date, Enum, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class CostInboxStatus(StrEnum):
    NEW = "new"  # just captured, uncoded
    CODED = "coded"  # job + cost code assigned, ready to push
    PUSHED = "pushed"  # turned into a bill; leaves the inbox
    REJECTED = "rejected"  # not a real cost / duplicate


class CostInboxItem(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "cost_inbox_items"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[CostInboxStatus] = mapped_column(
        Enum(
            CostInboxStatus,
            name="cost_inbox_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=CostInboxStatus.NEW,
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    purchase_date: Mapped[date | None] = mapped_column(Date, index=True)

    # All nullable: the whole point is that a new item may not know these yet.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    cost_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="SET NULL"), index=True
    )
    #: The uploaded receipt/document, where one is attached.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    #: The bill it became, once pushed. Lets the inbox link back to the real cost.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL")
    )

    tags: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(String(1000))

    @property
    def is_coded(self) -> bool:
        """Has enough to become a bill: a job and a cost code."""
        return self.job_id is not None and self.cost_code_id is not None

    def __repr__(self) -> str:
        return f"<CostInboxItem {self.title} {self.status}>"
