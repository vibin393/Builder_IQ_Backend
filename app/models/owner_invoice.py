"""Owner (client) invoices: billing the homeowner — the receivables side.

The existing `Invoice` model is a *vendor* bill: money the builder owes a trade.
This is its mirror image — money the *owner* owes the builder: deposits, progress
draws and the final payment that make up the contract's payment schedule.

Totals are derived from the recorded payments, never stored, so "amount paid" and
"balance due" cannot drift away from the payments that produced them — the same
discipline the job-cost summary uses.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

ZERO = Decimal("0")


class OwnerInvoiceKind(StrEnum):
    DEPOSIT = "deposit"
    PROGRESS = "progress"
    FINAL = "final"
    STANDARD = "standard"


class OwnerInvoiceStatus(StrEnum):
    DRAFT = "draft"  # being prepared, not yet sent
    SENT = "sent"  # issued to the owner
    VIEWED = "viewed"  # owner has opened it
    PARTIAL = "partial"  # part-paid
    PAID = "paid"  # settled in full
    VOID = "void"  # cancelled


# Statuses where the money is still expected — what "outstanding" sums over.
OPEN_STATUSES = (
    OwnerInvoiceStatus.SENT,
    OwnerInvoiceStatus.VIEWED,
    OwnerInvoiceStatus.PARTIAL,
)


class OwnerInvoice(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "owner_invoices"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[OwnerInvoiceKind] = mapped_column(
        Enum(
            OwnerInvoiceKind,
            name="owner_invoice_kind",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OwnerInvoiceKind.PROGRESS,
        nullable=False,
    )
    status: Mapped[OwnerInvoiceStatus] = mapped_column(
        Enum(
            OwnerInvoiceStatus,
            name="owner_invoice_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OwnerInvoiceStatus.DRAFT,
        nullable=False,
        index=True,
    )

    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    viewed_by_owner_on: Mapped[date | None] = mapped_column(Date)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    #: Withheld from what is due now, not from what the work was worth.
    retainage_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
    #: Shown to the owner on the invoice, above and below the figures. BuilderTrend
    #: splits these as the invoice description and closing text.
    invoice_description: Mapped[str | None] = mapped_column(Text)
    closing_text: Mapped[str | None] = mapped_column(Text)
    #: e.g. "Net 30". Free text because every builder words terms their own way.
    payment_terms: Mapped[str | None] = mapped_column(String(100))

    payments: Mapped[list["OwnerPayment"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    lines: Mapped[list["OwnerInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    @property
    def line_subtotal(self) -> Decimal:
        return sum((ln.extended_price for ln in self.lines), start=ZERO)

    @property
    def effective_subtotal(self) -> Decimal:
        """Priced from line items when there are any; otherwise the flat `subtotal`.
        A line-item invoice and a one-figure invoice are both valid — BuilderTrend
        offers the same choice (line items vs flat fee)."""
        return self.line_subtotal if self.lines else self.subtotal

    @property
    def total_price(self) -> Decimal:
        return self.effective_subtotal + self.tax_amount

    @property
    def amount_paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), start=ZERO)

    @property
    def balance_due(self) -> Decimal:
        """What is owed now: the total, less retainage held back, less what has been
        paid. Never below zero — an overpayment is a credit, not a negative bill."""
        return max(ZERO, self.total_price - self.retainage_amount - self.amount_paid)

    @property
    def is_paid(self) -> bool:
        return self.balance_due <= ZERO and self.status is not OwnerInvoiceStatus.DRAFT

    def __repr__(self) -> str:
        return f"<OwnerInvoice {self.invoice_number} {self.status} job={self.job_id}>"


class OwnerPayment(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "owner_payments"

    owner_invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("owner_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: check, card, ach, cash, other
    method: Mapped[str] = mapped_column(String(20), default="other", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(String(500))

    invoice: Mapped["OwnerInvoice"] = relationship(back_populates="payments")


class OwnerInvoiceLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A priced line on a client invoice — the same cost→markup→price shape as an
    estimate line, so the invoice can itemise the work rather than state one figure."""

    __tablename__ = "owner_invoice_lines"

    owner_invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("owner_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=ZERO, nullable=False)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=ZERO, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    invoice: Mapped["OwnerInvoice"] = relationship(back_populates="lines")

    @property
    def unit_price(self) -> Decimal:
        return (self.unit_cost * (Decimal(1) + self.markup_percent / Decimal(100))).quantize(
            Decimal("0.01")
        )

    @property
    def extended_cost(self) -> Decimal:
        return (self.unit_cost * self.quantity).quantize(Decimal("0.01"))

    @property
    def extended_price(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))
