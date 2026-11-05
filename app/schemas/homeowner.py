"""Buyer-facing response shapes for the homeowner portal.

These are deliberately *not* the staff schemas. A homeowner may see the price they
are asked to pay, but never the builder's cost, markup or margin. An invoice line
here carries `unit_price` and `extended_price` only — `unit_cost` and
`markup_percent` are dropped on the way out, so a serialisation mistake cannot leak
what a staff schema would have shown.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.models.estimate import ClientUpdate
from app.models.owner_invoice import OwnerInvoice, OwnerInvoiceKind, OwnerInvoiceStatus

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Num = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class BuyerInvoiceLine(BaseModel):
    """One priced line, cost and markup removed — the client price only."""

    description: str
    unit: str | None
    quantity: Num
    unit_price: Money
    extended_price: Money


class BuyerPayment(BaseModel):
    amount: Money
    paid_on: date
    method: str
    reference: str | None


class BuyerInvoice(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    invoice_number: str
    title: str
    kind: OwnerInvoiceKind
    status: OwnerInvoiceStatus
    issue_date: date | None
    due_date: date | None
    total_price: Money
    tax_amount: Money
    retainage_amount: Money
    amount_paid: Money
    balance_due: Money
    is_paid: bool
    payment_terms: str | None
    invoice_description: str | None
    closing_text: str | None
    lines: list[BuyerInvoiceLine] = []
    payments: list[BuyerPayment] = []

    @classmethod
    def of(cls, inv: OwnerInvoice) -> "BuyerInvoice":
        return cls(
            id=inv.id,
            job_id=inv.job_id,
            invoice_number=inv.invoice_number,
            title=inv.title,
            kind=inv.kind,
            status=inv.status,
            issue_date=inv.issue_date,
            due_date=inv.due_date,
            total_price=inv.total_price,
            tax_amount=inv.tax_amount,
            retainage_amount=inv.retainage_amount,
            amount_paid=inv.amount_paid,
            balance_due=inv.balance_due,
            is_paid=inv.is_paid,
            payment_terms=inv.payment_terms,
            invoice_description=inv.invoice_description,
            closing_text=inv.closing_text,
            lines=[
                BuyerInvoiceLine(
                    description=ln.description,
                    unit=ln.unit,
                    quantity=ln.quantity,
                    unit_price=ln.unit_price,
                    extended_price=ln.extended_price,
                )
                for ln in sorted(inv.lines, key=lambda ln: ln.sort_order)
            ],
            payments=[
                BuyerPayment(
                    amount=p.amount, paid_on=p.paid_on, method=p.method, reference=p.reference
                )
                for p in sorted(inv.payments, key=lambda p: p.paid_on)
            ],
        )


class BuyerUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    body: str
    posted_on: date

    @classmethod
    def of(cls, u: ClientUpdate) -> "BuyerUpdate":
        return cls(id=u.id, job_id=u.job_id, title=u.title, body=u.body, posted_on=u.posted_on)
