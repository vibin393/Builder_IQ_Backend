"""Vendor invoices: actual cost, and the third leg of the job-cost triangle.

    budget  ->  committed (PO)  ->  actual (invoice)

An invoice is matched to a purchase order. Approving it is what turns committed cost into
actual cost, and the difference between the two is what a builder chases at month end.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class InvoiceStatus(StrEnum):
    RECEIVED = "received"
    APPROVED = "approved"   # counts as actual cost
    PAID = "paid"
    DISPUTED = "disputed"
    VOID = "void"


ACTUAL_STATUSES = (InvoiceStatus.APPROVED, InvoiceStatus.PAID)


class Invoice(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_number", name="uq_invoices_tenant_number"),
    )

    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    # Matched against a PO. Unmatched invoices are the classic leak, so this is required.
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status", values_callable=lambda e: [m.value for m in e]),
        default=InvoiceStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    approved_date: Mapped[date | None] = mapped_column(Date)
    paid_date: Mapped[date | None] = mapped_column(Date)

    # A lien waiver is what protects the builder's title at closing.
    lien_waiver_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1000))

    # Retainage is withheld *payment*, not reduced *cost*. The work was done and
    # the cost is real, so `total` and the actual-cost rollup stay whole; what
    # changes is how much is payable now. Snapshotted from the purchase order at
    # invoice time so a later change to the PO cannot silently restate history.
    retainage_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), nullable=False
    )
    retainage_released_date: Mapped[date | None] = mapped_column(Date)

    # --- CIS (Construction Industry Scheme) ------------------------------------
    # Tax is deducted from the labour element only, never from materials, at the rate
    # the trade's HMRC status gives. Both are snapshotted at receipt so a later change
    # to the trade's status cannot restate an invoice already paid.
    labour_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    cis_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), nullable=False)
    cis_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- decisions, and who made them -----------------------------------------
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Approved above its PO, deliberately. Recorded only when it actually was over.
    over_po_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(String(500))
    paid_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    payment_reference: Mapped[str | None] = mapped_column(String(120))   # BACS ref, cheque no.
    lien_waiver_date: Mapped[date | None] = mapped_column(Date)
    disputed_on: Mapped[date | None] = mapped_column(Date)
    dispute_reason: Mapped[str | None] = mapped_column(String(1000))

    # --- the accounting hand-off: which export batch carried each event -------
    # Set when the batch is built, cleared if it is voided. Never set twice, so no
    # invoice, payment or retention release reaches the ledger more than once.
    export_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounting_export_batches.id", ondelete="SET NULL"),
        index=True,
    )
    payment_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounting_export_batches.id", ondelete="SET NULL"),
        index=True,
    )
    retention_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounting_export_batches.id", ondelete="SET NULL"),
        index=True,
    )

    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def retainage_amount(self) -> Decimal:
        """Money certified but withheld. Zero once released."""
        if self.retainage_released_date is not None:
            return Decimal("0")
        return (self.total * self.retainage_percent / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def retention_withheld(self) -> Decimal:
        """Retention taken when the invoice was certified, whether or not released since.

        The ledger needs the original figure: the invoice journal credited it to the
        retention account, and the release journal has to take out exactly that.
        """
        return (self.total * self.retainage_percent / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def cis_deduction(self) -> Decimal:
        """Tax withheld for HMRC — the labour element at the trade's CIS rate."""
        if not self.labour_amount or not self.cis_rate:
            return Decimal("0")
        return (self.labour_amount * self.cis_rate / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def net_payable(self) -> Decimal:
        """What the trade actually gets paid on this invoice today: the total, less the
        retention still held, less the CIS deduction paid to HMRC instead."""
        return self.total - self.retainage_amount - self.cis_deduction

    @property
    def is_overdue(self) -> bool:
        """Approved, past its due date, and still not paid — owed and late."""
        return bool(
            self.status is InvoiceStatus.APPROVED
            and self.due_date
            and self.due_date < date.today()
        )

    @property
    def days_overdue(self) -> int | None:
        if not self.is_overdue or self.due_date is None:
            return None
        return (date.today() - self.due_date).days

    @property
    def is_actual(self) -> bool:
        return self.status in ACTUAL_STATUSES

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} {self.status}>"


class InvoiceLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    # The purchase-order line being billed against. The PO lines are the schedule
    # of values: a trade on a long scope bills a portion of a line at a time, and
    # this is what makes "previously billed" and "remaining" derivable rather than
    # something a clerk has to keep in a spreadsheet.
    purchase_order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("purchase_order_lines.id", ondelete="SET NULL"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
