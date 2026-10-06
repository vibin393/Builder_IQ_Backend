"""Schemas for the accounting hand-off: queue, journals, batches, settings, CIS return."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.accounting import ExportFormat, ExportKind, ExportStatus

Money = Decimal


class JournalLine(BaseModel):
    """One side of a double entry. Exactly one of debit and credit is non-zero."""

    posting_date: date
    # Set when the item is dated inside a locked period and was moved to the first open day.
    original_date: date | None = None
    account: str
    debit: Money = Decimal("0")
    credit: Money = Decimal("0")
    job_number: str | None = None
    reference: str
    details: str
    supplier_code: str | None = None


class QueueItem(BaseModel):
    """An invoice event waiting to go to the ledger, or already carried by a batch."""

    invoice_id: uuid.UUID
    document: str
    event_date: date | None
    supplier_code: str | None
    supplier_name: str | None
    job_number: str | None
    amount: Money
    # Advisory: still exported, but worth a controller's glance.
    warnings: list[str] = []


class ExportQueue(BaseModel):
    kind: ExportKind
    through_date: date
    items: list[QueueItem]
    journal: list[JournalLine]
    debits: Money
    credits: Money
    balanced: bool
    # Payments and releases whose invoice has not gone to the ledger yet. Exporting
    # them first would clear a creditor balance the ledger has never seen.
    waiting: int = 0


class BatchCreate(BaseModel):
    kind: ExportKind
    through_date: date | None = None   # blank: everything ready up to today
    notes: str | None = Field(default=None, max_length=500)


class BatchPost(BaseModel):
    ledger_reference: str | None = Field(default=None, max_length=64)


class BatchVoid(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_number: str
    kind: ExportKind
    status: ExportStatus
    through_date: date
    item_count: int
    total: Money
    notes: str | None
    created_at: datetime
    created_by_id: uuid.UUID | None
    posted_at: datetime | None
    posted_by_id: uuid.UUID | None
    ledger_reference: str | None
    voided_at: datetime | None
    void_reason: str | None
    download_count: int
    last_downloaded_at: datetime | None


class BatchDetail(BatchRead):
    items: list[QueueItem] = []
    journal: list[JournalLine] = []
    debits: Money = Decimal("0")
    credits: Money = Decimal("0")


class ReadyCount(BaseModel):
    count: int
    value: Money


class AccountingSummary(BaseModel):
    ready: dict[str, ReadyCount]          # by export kind
    waiting: int                          # payments / releases held behind their invoice
    unposted_batches: int                 # exported, not yet confirmed in the ledger
    last_export_on: date | None
    exported_this_month: Money
    unmapped_cost_codes: int
    open_commitments: Money               # committed on POs, not yet invoiced
    cis_this_period: Money                # deducted in the current CIS tax month
    locked_through: date | None


class AccountingSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    default_format: ExportFormat
    cost_account: str
    creditors_account: str
    retention_account: str
    cis_account: str
    bank_account: str
    locked_through: date | None


class AccountingSettingsUpdate(BaseModel):
    default_format: ExportFormat | None = None
    cost_account: str | None = Field(default=None, min_length=1, max_length=20)
    creditors_account: str | None = Field(default=None, min_length=1, max_length=20)
    retention_account: str | None = Field(default=None, min_length=1, max_length=20)
    cis_account: str | None = Field(default=None, min_length=1, max_length=20)
    bank_account: str | None = Field(default=None, min_length=1, max_length=20)
    locked_through: date | None = None


class MappingRow(BaseModel):
    cost_code_id: uuid.UUID
    # Blank clears the mapping, and the code falls back to the default cost account.
    gl_account: str | None = Field(default=None, max_length=20)


class MappingRead(BaseModel):
    cost_code_id: uuid.UUID
    code: str
    name: str
    division: str | None
    gl_account: str | None
    is_active: bool


class CISReturnRow(BaseModel):
    vendor_id: uuid.UUID
    supplier_code: str
    supplier_name: str
    utr: str | None
    cis_status: str | None
    payments: int
    gross_paid: Money        # paid before the deduction, retention excluded
    materials: Money
    labour: Money
    deducted: Money
    # Why this row cannot go on the return as it stands (no UTR, never verified).
    problems: list[str] = []


class CISReturn(BaseModel):
    period_start: date       # the 6th
    period_end: date         # the 5th of the next month
    due_date: date           # the 19th after the period ends
    rows: list[CISReturnRow]
    gross_paid: Money
    materials: Money
    deducted: Money
