"""The accounting hand-off: export batches, the nominal mapping, and the period lock.

The accounts live in the builder's accounting package (Sage, Xero). This system is the
source of job cost, so what it owes the ledger is a set of balanced journals, each sent
exactly once. An export batch is that "exactly once": every invoice, payment and
retention release belongs to at most one batch of each kind, so re-downloading a batch
is safe and building a new one never repeats what has already gone.

    exported  ->  posted      (the controller confirms it imported cleanly)
        \\->  void            (never imported: its items go back in the queue)

A posted batch cannot be voided here. It is in the ledger, and the correction is a
reversing journal there, not a quiet delete here.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


def _values(e):
    return [m.value for m in e]


class ExportKind(StrEnum):
    INVOICES = "invoices"     # purchase invoices: cost in, creditor and retention up
    PAYMENTS = "payments"     # payments out: creditor down, bank and CIS liability
    RETENTION = "retention"   # retention released and paid to the trade


class ExportStatus(StrEnum):
    EXPORTED = "exported"
    POSTED = "posted"
    VOID = "void"


class ExportFormat(StrEnum):
    JOURNAL = "journal"   # generic double-entry CSV: any package, or a spreadsheet
    SAGE50 = "sage50"     # Sage 50 Accounts audit-trail import, JD/JC journal rows
    XERO = "xero"         # Xero manual journal import, job as a tracking category


class AccountingSettings(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One row per tenant. Defaults follow the Sage 50 UK standard chart of accounts."""

    __tablename__ = "accounting_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_accounting_settings_tenant"),)

    default_format: Mapped[ExportFormat] = mapped_column(
        Enum(ExportFormat, name="accounting_export_format", values_callable=_values),
        default=ExportFormat.SAGE50,
        nullable=False,
    )
    # Cost codes without their own nominal code post here, and are flagged as they do.
    cost_account: Mapped[str] = mapped_column(String(20), default="5000", nullable=False)
    creditors_account: Mapped[str] = mapped_column(String(20), default="2100", nullable=False)
    retention_account: Mapped[str] = mapped_column(String(20), default="2150", nullable=False)
    cis_account: Mapped[str] = mapped_column(String(20), default="2211", nullable=False)
    bank_account: Mapped[str] = mapped_column(String(20), default="1200", nullable=False)
    # Periods on or before this date are closed in the ledger. Anything dated inside a
    # closed period posts on the first open day instead, and says so.
    locked_through: Mapped[date | None] = mapped_column(Date)


class ExportBatch(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "accounting_export_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "batch_number", name="uq_accounting_batches_tenant_number"),
    )

    batch_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    kind: Mapped[ExportKind] = mapped_column(
        Enum(ExportKind, name="accounting_export_kind", values_callable=_values),
        nullable=False,
        index=True,
    )
    status: Mapped[ExportStatus] = mapped_column(
        Enum(ExportStatus, name="accounting_export_status", values_callable=_values),
        default=ExportStatus.EXPORTED,
        nullable=False,
        index=True,
    )
    # Items dated up to and including this day were swept in.
    through_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Snapshotted so the batch's totals do not drift if an item is later edited.
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # The journal or batch number the accounting package gave it on import.
    ledger_reference: Mapped[str | None] = mapped_column(String(64))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(500))
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ExportBatch {self.batch_number} {self.kind} {self.status}>"
