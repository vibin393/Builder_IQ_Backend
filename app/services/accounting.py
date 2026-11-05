"""Building the journals the ledger gets, and the files they travel in.

Three events reach the ledger from an invoice, each exactly once:

    certified (approved)   Dr job cost (by cost code's nominal)   total
                           Cr retention payable                   retention withheld
                           Cr trade creditors                     total - retention

    paid                   Dr trade creditors                     total - retention
                           Cr CIS liability (HMRC)                CIS deducted
                           Cr bank                                what the trade received

    retention released     Dr retention payable                   retention withheld
                           Cr bank

Every journal balances by construction. A payment or release is only offered once its
invoice has gone to the ledger, or the creditor it clears would not exist there yet.
"""

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.accounting import AccountingSettings, ExportFormat, ExportKind
from app.models.cost_code import CostCode
from app.models.invoice import ACTUAL_STATUSES, Invoice, InvoiceStatus
from app.models.job import Job
from app.models.vendor import Vendor
from app.schemas.accounting import JournalLine, QueueItem

ZERO = Decimal("0")


async def settings_for(db: AsyncSession, tenant_id: uuid.UUID) -> AccountingSettings:
    """The tenant's settings, created with the standard defaults on first use."""
    row = await db.scalar(
        select(AccountingSettings).where(AccountingSettings.tenant_id == tenant_id)
    )
    if row is None:
        row = AccountingSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
    return row


def event_date(inv: Invoice, kind: ExportKind) -> date | None:
    if kind is ExportKind.INVOICES:
        return inv.approved_date or inv.invoice_date
    if kind is ExportKind.PAYMENTS:
        return inv.paid_date
    return inv.retainage_released_date


def batch_column(kind: ExportKind):
    return {
        ExportKind.INVOICES: Invoice.export_batch_id,
        ExportKind.PAYMENTS: Invoice.payment_batch_id,
        ExportKind.RETENTION: Invoice.retention_batch_id,
    }[kind]


def item_amount(inv: Invoice, kind: ExportKind) -> Decimal:
    """The size of the event: what the journal moves through the control account."""
    if kind is ExportKind.INVOICES:
        return inv.total
    if kind is ExportKind.PAYMENTS:
        return inv.total - inv.retention_withheld
    return inv.retention_withheld


@dataclass
class Lookups:
    codes: dict[uuid.UUID, CostCode]
    vendors: dict[uuid.UUID, Vendor]
    jobs: dict[uuid.UUID, Job]


async def lookups(db: AsyncSession, tenant_id: uuid.UUID) -> Lookups:
    return Lookups(
        codes={
            c.id: c
            for c in await db.scalars(select(CostCode).where(CostCode.tenant_id == tenant_id))
        },
        vendors={
            v.id: v for v in await db.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id))
        },
        jobs={j.id: j for j in await db.scalars(select(Job).where(Job.tenant_id == tenant_id))},
    )


async def candidates(
    db: AsyncSession, tenant_id: uuid.UUID, kind: ExportKind, through: date
) -> tuple[list[Invoice], int]:
    """Items ready for a batch of this kind, and how many are held behind their invoice."""
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(Invoice.tenant_id == tenant_id, batch_column(kind).is_(None))
    )
    if kind is ExportKind.INVOICES:
        stmt = stmt.where(Invoice.status.in_(ACTUAL_STATUSES))
    elif kind is ExportKind.PAYMENTS:
        stmt = stmt.where(Invoice.status == InvoiceStatus.PAID)
    else:
        stmt = stmt.where(
            Invoice.retainage_released_date.isnot(None), Invoice.retainage_percent > 0
        )

    ready: list[Invoice] = []
    waiting = 0
    for inv in await db.scalars(stmt.order_by(Invoice.invoice_number)):
        when = event_date(inv, kind)
        if when is None or when > through:
            continue
        if kind is not ExportKind.INVOICES and inv.export_batch_id is None:
            waiting += 1
            continue
        if kind is ExportKind.RETENTION and inv.retention_withheld <= 0:
            continue
        ready.append(inv)
    return ready, waiting


async def items_in_batch(
    db: AsyncSession, tenant_id: uuid.UUID, kind: ExportKind, batch_id: uuid.UUID
) -> list[Invoice]:
    return list(
        await db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.tenant_id == tenant_id, batch_column(kind) == batch_id)
            .order_by(Invoice.invoice_number)
        )
    )


def build(
    invoices: Iterable[Invoice], kind: ExportKind, cfg: AccountingSettings, lk: Lookups
) -> tuple[list[QueueItem], list[JournalLine]]:
    """The queue rows and the balanced journal for a set of items."""
    items: list[QueueItem] = []
    journal: list[JournalLine] = []
    first_open = cfg.locked_through + timedelta(days=1) if cfg.locked_through else None

    for inv in invoices:
        vendor = lk.vendors.get(inv.vendor_id)
        job = lk.jobs.get(inv.job_id)
        supplier = vendor.code if vendor else None
        who = vendor.name if vendor else "Unknown supplier"
        job_no = job.job_number if job else None
        when = event_date(inv, kind) or date.today()
        warnings: list[str] = []

        moved_from = None
        if first_open and when < first_open:
            moved_from, when = when, first_open
            warnings.append(
                f"Dated {moved_from:%d/%m/%Y}, inside the locked period; posts {when:%d/%m/%Y}."
            )

        stamp = {
            "posting_date": when,
            "original_date": moved_from,
            "job_number": job_no,
            "reference": inv.invoice_number,
            "supplier_code": supplier,
        }

        def line(account, *, dr=ZERO, cr=ZERO, details, _stamp=stamp):
            journal.append(
                JournalLine(account=account, debit=dr, credit=cr, details=details[:120], **_stamp)
            )

        retention = inv.retention_withheld
        if kind is ExportKind.INVOICES:
            for ln in inv.lines:
                code = lk.codes.get(ln.cost_code_id)
                account = (code.gl_account if code else None) or cfg.cost_account
                if code is None or not code.gl_account:
                    label = code.code if code else "an unknown cost code"
                    warnings.append(f"{label} has no nominal code; posts to {cfg.cost_account}.")
                name = f"{code.code} {code.name}" if code else "Cost"
                line(account, dr=ln.amount, details=f"{who} · {name}")
            if retention > 0:
                line(
                    cfg.retention_account,
                    cr=retention,
                    details=f"{who} · retention {inv.retainage_percent.normalize()}%",
                )
            line(cfg.creditors_account, cr=inv.total - retention, details=f"{who} · invoice")

        elif kind is ExportKind.PAYMENTS:
            cleared = inv.total - retention
            cis = inv.cis_deduction
            if inv.labour_amount and not inv.cis_verified:
                warnings.append("Trade's CIS status was not verified; nothing was deducted.")
            ref = f" · {inv.payment_reference}" if inv.payment_reference else ""
            line(cfg.creditors_account, dr=cleared, details=f"{who} · payment{ref}")
            if cis > 0:
                line(
                    cfg.cis_account,
                    cr=cis,
                    details=f"{who} · CIS {inv.cis_rate.normalize()}% of labour",
                )
            line(cfg.bank_account, cr=cleared - cis, details=f"{who} · payment{ref}")

        else:
            line(cfg.retention_account, dr=retention, details=f"{who} · retention released")
            line(cfg.bank_account, cr=retention, details=f"{who} · retention released")

        items.append(
            QueueItem(
                invoice_id=inv.id,
                document=inv.invoice_number,
                event_date=event_date(inv, kind),
                supplier_code=supplier,
                supplier_name=vendor.name if vendor else None,
                job_number=job_no,
                amount=item_amount(inv, kind),
                warnings=list(dict.fromkeys(warnings)),
            )
        )
    return items, journal


def totals(journal: list[JournalLine]) -> tuple[Decimal, Decimal]:
    return (
        sum((j.debit for j in journal), ZERO),
        sum((j.credit for j in journal), ZERO),
    )


# --- files -----------------------------------------------------------------------


def render(journal: list[JournalLine], fmt: ExportFormat, batch_number: str) -> str:
    """The journal as the file a given package imports."""
    buf = io.StringIO()
    w = csv.writer(buf)

    if fmt is ExportFormat.SAGE50:
        # Sage 50 Accounts audit-trail import: JD / JC journal rows, one per side.
        w.writerow(
            [
                "Type",
                "Account Reference",
                "Nominal A/C Ref",
                "Department Code",
                "Date",
                "Reference",
                "Details",
                "Net Amount",
                "Tax Code",
                "Tax Amount",
            ]
        )
        for j in journal:
            is_dr = j.debit > 0
            w.writerow(
                [
                    "JD" if is_dr else "JC",
                    j.account,
                    j.account,
                    "",
                    j.posting_date.strftime("%d/%m/%Y"),
                    j.reference[:30],
                    f"{j.job_number or ''} {j.details}".strip()[:60],
                    j.debit if is_dr else j.credit,
                    "T9",
                    "0.00",
                ]
            )
    elif fmt is ExportFormat.XERO:
        # Xero manual journal import: signed amounts, the home as a tracking category.
        w.writerow(
            [
                "*Narration",
                "*Date",
                "Description",
                "*AccountCode",
                "*TaxRate",
                "*Amount",
                "TrackingName1",
                "TrackingOption1",
            ]
        )
        for j in journal:
            amount = j.debit if j.debit > 0 else -j.credit
            w.writerow(
                [
                    f"{batch_number} {j.reference}",
                    j.posting_date.strftime("%d/%m/%Y"),
                    j.details,
                    j.account,
                    "No VAT",
                    amount,
                    "Job" if j.job_number else "",
                    j.job_number or "",
                ]
            )
    else:
        w.writerow(
            [
                "batch",
                "date",
                "account",
                "debit",
                "credit",
                "job",
                "reference",
                "details",
                "supplier",
            ]
        )
        for j in journal:
            w.writerow(
                [
                    batch_number,
                    j.posting_date.isoformat(),
                    j.account,
                    j.debit or "",
                    j.credit or "",
                    j.job_number or "",
                    j.reference,
                    j.details,
                    j.supplier_code or "",
                ]
            )
    return buf.getvalue()


# --- CIS tax months ---------------------------------------------------------------


def tax_month(containing: date) -> tuple[date, date]:
    """The CIS tax month (6th to the 5th) a date falls in."""
    if containing.day >= 6:
        start = containing.replace(day=6)
    else:
        prev = containing.replace(day=1) - timedelta(days=1)
        start = prev.replace(day=6)
    nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=5)
    return start, nxt


def return_due(period_end: date) -> date:
    """A monthly CIS return is due by the 19th after the tax month ends."""
    return period_end.replace(day=19)
