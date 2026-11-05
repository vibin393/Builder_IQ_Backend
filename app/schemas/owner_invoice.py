"""Request and response shapes for owner (client) invoicing."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.owner_invoice import OwnerInvoiceKind, OwnerInvoiceStatus

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Num = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InvoiceLineCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal = Field(default=Decimal("1"), ge=0)
    unit_cost: Decimal = Field(default=Decimal("0"), ge=0)
    markup_percent: Decimal = Field(default=Decimal("0"), ge=0)
    sort_order: int = 0


class InvoiceLineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, min_length=1, max_length=500)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal | None = Field(default=None, ge=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)
    markup_percent: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = None


class InvoiceLineRead(ORMModel):
    id: uuid.UUID
    owner_invoice_id: uuid.UUID
    description: str
    unit: str | None
    quantity: Num
    unit_cost: Money
    markup_percent: Num
    sort_order: int
    unit_price: Money
    extended_cost: Money
    extended_price: Money


class OwnerInvoiceCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    kind: OwnerInvoiceKind = OwnerInvoiceKind.PROGRESS
    invoice_number: str | None = Field(default=None, max_length=32)
    issue_date: date | None = None
    due_date: date | None = None
    subtotal: Decimal = Field(default=Decimal("0"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0)
    retainage_amount: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    invoice_description: str | None = None
    closing_text: str | None = None
    payment_terms: str | None = Field(default=None, max_length=100)


class OwnerInvoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    kind: OwnerInvoiceKind | None = None
    issue_date: date | None = None
    due_date: date | None = None
    subtotal: Decimal | None = Field(default=None, ge=0)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    retainage_amount: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    invoice_description: str | None = None
    closing_text: str | None = None
    payment_terms: str | None = Field(default=None, max_length=100)


class GenerateSchedule(BaseModel):
    """Generate a draft payment schedule from a contract total."""

    job_id: uuid.UUID
    #: Defaults to the job's contract total when left off.
    contract_amount: Decimal | None = Field(default=None, gt=0)
    deposit_percent: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    progress_count: int = Field(default=3, ge=0, le=24)
    final_percent: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    first_due: date | None = None
    interval_days: int = Field(default=30, ge=0, le=365)
    retainage_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class PaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    paid_on: date | None = None
    method: str = Field(default="other", max_length=20)
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=500)


class OwnerPaymentRead(ORMModel):
    id: uuid.UUID
    owner_invoice_id: uuid.UUID
    amount: Money
    paid_on: date
    method: str
    reference: str | None
    notes: str | None


class OwnerInvoiceRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    invoice_number: str
    title: str
    kind: OwnerInvoiceKind
    status: OwnerInvoiceStatus
    issue_date: date | None
    due_date: date | None
    viewed_by_owner_on: date | None
    subtotal: Money
    effective_subtotal: Money
    tax_amount: Money
    retainage_amount: Money
    notes: str | None
    invoice_description: str | None
    closing_text: str | None
    payment_terms: str | None

    total_price: Money
    amount_paid: Money
    balance_due: Money
    is_paid: bool
    payments: list[OwnerPaymentRead] = []
    lines: list[InvoiceLineRead] = []


class OwnerInvoiceSummary(BaseModel):
    job_id: uuid.UUID | None
    count: int
    draft: int
    outstanding_count: int
    overdue: int
    total_invoiced: Money
    total_paid: Money
    outstanding: Money
