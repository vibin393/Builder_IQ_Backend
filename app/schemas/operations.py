"""Schemas for CRM, change orders, scheduling, invoices, warranty and documents."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.change_order import ChangeOrderStatus, ResponsibleParty
from app.models.invoice import InvoiceStatus
from app.models.schedule import TaskStatus
from app.models.warranty import Priority, ServiceOrderStatus, WarrantyStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- CRM ----------------------------------------------------------------------

# Lead, activity and realtor schemas moved to `app.schemas.crm` when the sales
# feature outgrew five fields. Nothing else in this module referenced them.


# --- Change orders ------------------------------------------------------------


class ChangeOrderLineCreate(BaseModel):
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    amount: Money
    description: str | None = None


class ChangeOrderLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None
    amount: Money
    description: str | None


class ChangeOrderCreate(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    reason: str | None = None
    responsible_party: ResponsibleParty = ResponsibleParty.BUYER
    price_delta: Money = Decimal("0")
    schedule_days: int = 0
    cost_lines: list[ChangeOrderLineCreate] = []


class ChangeOrderRead(ORMModel):
    id: uuid.UUID
    co_number: str
    job_id: uuid.UUID
    status: ChangeOrderStatus
    responsible_party: ResponsibleParty
    description: str
    reason: str | None
    price_delta: Money
    cost_delta: Money
    margin_impact: Money
    schedule_days: int
    approved_date: date | None
    cost_lines: list[ChangeOrderLineRead]


class ChangeOrderCascade(BaseModel):
    change_order_id: uuid.UUID
    responsible_party: str
    price_delta: Money
    cost_delta: Money
    margin_impact: Money
    schedule_days_delta: int
    budget_changes: list[dict]


# --- Scheduling ---------------------------------------------------------------


class TemplateTaskCreate(BaseModel):
    sequence: int
    name: str = Field(min_length=1, max_length=200)
    offset_days: int = 0
    duration_days: int = Field(default=1, ge=0)
    cost_code_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    is_milestone: bool = False


class TemplateTaskRead(ORMModel):
    id: uuid.UUID
    sequence: int
    name: str
    offset_days: int
    duration_days: int
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    is_milestone: bool


class ScheduleTemplateCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    plan_id: uuid.UUID | None = None


class ScheduleTemplateRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    plan_id: uuid.UUID | None
    is_active: bool
    tasks: list[TemplateTaskRead] = []


class GenerateScheduleRequest(BaseModel):
    template_id: uuid.UUID
    start_date: date | None = None


class JobTaskRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    sequence: int
    name: str
    status: TaskStatus
    scheduled_start: date | None
    scheduled_finish: date | None
    actual_start: date | None
    actual_finish: date | None
    duration_days: int
    cost_code_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    is_milestone: bool
    days_late: int
    notes: str | None


class JobTaskUpdate(BaseModel):
    status: TaskStatus | None = None
    vendor_id: uuid.UUID | None = None
    scheduled_start: date | None = None
    actual_start: date | None = None
    notes: str | None = None


class TaskCompletion(BaseModel):
    """What completing one task did to everything behind it."""

    task_id: uuid.UUID
    actual_finish: date
    days_late: int
    tasks_shifted: int
    next_task_id: uuid.UUID | None


# --- Invoices -----------------------------------------------------------------


class InvoiceLineCreate(BaseModel):
    cost_code_id: uuid.UUID
    amount: Money
    description: str | None = None
    # Bills against a specific purchase-order line. The PO lines are the schedule
    # of values, so this is what makes partial progress billing possible.
    purchase_order_line_id: uuid.UUID | None = None


class InvoiceLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    amount: Money
    description: str | None
    purchase_order_line_id: uuid.UUID | None


class InvoiceCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=64)
    purchase_order_id: uuid.UUID
    invoice_date: date | None = None
    due_date: date | None = None
    lien_waiver_received: bool = False
    notes: str | None = None
    lines: list[InvoiceLineCreate] = Field(min_length=1)
    # Left unset, the invoice inherits the purchase order's retainage rate.
    retainage_percent: Decimal | None = Field(default=None, ge=0, le=50)
    # The labour element of the invoice, which CIS is deducted from. Materials are not.
    labour_amount: Decimal | None = Field(default=None, ge=0)


class InvoiceDispute(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class InvoiceSummary(BaseModel):
    actual_cost: Money             # approved + paid
    awaiting_approval: int
    awaiting_value: Money
    disputed: int
    approved_unpaid: int
    approved_unpaid_value: Money   # net payable still to go out
    overdue_to_pay: int
    overdue_value: Money
    waivers_missing: int           # approved, cannot be paid until the waiver is on file
    retention_held: Money
    cis_withheld_this_month: Money  # deducted on payments made this month, owed to HMRC
    cis_unverified: int            # labour billed by a trade with no CIS status on record
    total: int
    by_status: dict[str, int]


class InvoiceRead(ORMModel):
    id: uuid.UUID
    invoice_number: str
    job_id: uuid.UUID
    vendor_id: uuid.UUID
    purchase_order_id: uuid.UUID
    status: InvoiceStatus
    invoice_date: date | None
    due_date: date | None
    approved_date: date | None
    paid_date: date | None
    lien_waiver_received: bool
    total: Money
    # Certified, withheld, and what the trade is actually paid today.
    retainage_percent: Decimal
    retainage_amount: Money
    net_payable: Money
    retainage_released_date: date | None
    notes: str | None
    labour_amount: Money | None = None
    cis_rate: Decimal = Decimal("0")
    cis_verified: bool = False
    cis_deduction: Money = Decimal("0")
    approved_by_id: uuid.UUID | None = None
    over_po_override: bool = False
    override_reason: str | None = None
    paid_by_id: uuid.UUID | None = None
    payment_reference: str | None = None
    lien_waiver_date: date | None = None
    disputed_on: date | None = None
    dispute_reason: str | None = None
    is_overdue: bool = False
    days_overdue: int | None = None
    lines: list[InvoiceLineRead]


# --- Warranty -----------------------------------------------------------------


class WarrantyRequestCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    location: str | None = None
    priority: Priority = Priority.NORMAL


class ServiceOrderCreate(BaseModel):
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    instructions: str | None = None
    scheduled_date: date | None = None


class ServiceOrderRead(ORMModel):
    id: uuid.UUID
    request_id: uuid.UUID
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    status: ServiceOrderStatus
    instructions: str | None
    scheduled_date: date | None
    completed_date: date | None
    resolution: str | None


class WarrantyRequestRead(ORMModel):
    id: uuid.UUID
    request_number: str
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    status: WarrantyStatus
    priority: Priority
    title: str
    description: str | None
    location: str | None
    submitted_date: date | None
    resolved_date: date | None
    service_orders: list[ServiceOrderRead] = []


# NOTE: The document register schemas moved to app/schemas/documents.py when the
# register grew expiry tracking, a supersede lifecycle and analytics.
