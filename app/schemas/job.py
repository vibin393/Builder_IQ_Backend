"""Schemas for jobs, contracts, selections, budgets, purchase orders and job costing."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.budget import BudgetStatus
from app.models.contract import ContractStatus
from app.models.job import CancellationReason, HoldReason, JobStatus
from app.models.purchase_order import PurchaseOrderStatus
from app.models.selection import SelectionStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Jobs ---------------------------------------------------------------------


class JobCreate(BaseModel):
    community_id: uuid.UUID
    lot_id: uuid.UUID
    plan_id: uuid.UUID
    elevation: str | None = Field(default=None, max_length=32)
    superintendent_id: uuid.UUID | None = None
    start_date: date | None = None
    projected_completion: date | None = None
    notes: str | None = None


class JobUpdate(BaseModel):
    """Correcting a home's details.

    Status is absent on purpose. It moves through the lifecycle routes, which check
    the transition and set the dates that go with it; a PATCH that set status would
    route round every rule in the service.
    """

    plan_id: uuid.UUID | None = None
    elevation: str | None = Field(default=None, max_length=32)
    superintendent_id: uuid.UUID | None = None
    #: Re-baselining. Allowed, and worth doing deliberately - this is the number
    #: slip is measured against.
    projected_completion: date | None = None
    permit_number: str | None = Field(default=None, max_length=64)
    notes: str | None = None


class JobRead(ORMModel):
    id: uuid.UUID
    job_number: str
    community_id: uuid.UUID
    lot_id: uuid.UUID
    plan_id: uuid.UUID
    elevation: str | None
    status: JobStatus
    superintendent_id: uuid.UUID | None

    permit_number: str | None
    permit_applied_on: date | None
    permit_issued_on: date | None

    sold_date: date | None
    start_date: date | None
    projected_completion: date | None
    #: What the schedule currently expects, as opposed to what was promised. The
    #: gap between the two is how far behind the build is.
    forecast_completion: date | None
    actual_completion: date | None
    closing_date: date | None
    warranty_expires_on: date | None

    hold_reason: HoldReason | None
    hold_note: str | None
    on_hold_since: date | None
    days_on_hold: int

    cancelled_on: date | None
    cancellation_reason: CancellationReason | None
    cancellation_note: str | None
    notes: str | None

    # Derived server-side, and cheap - each is a date comparison. A list is read
    # for exactly these: what is late, what has stopped, how long it took.
    is_live: bool
    is_building: bool
    is_late: bool
    days_late: int | None
    forecast_slip: int | None
    cycle_time_days: int | None
    days_building: int | None
    days_since_hold: int | None
    days_waiting_on_permit: int | None


# --- lifecycle requests -------------------------------------------------------


class PermitApplication(BaseModel):
    permit_number: str | None = Field(default=None, max_length=64)
    permit_applied_on: date | None = None


class PermitIssued(BaseModel):
    number: str = Field(min_length=1, max_length=64)
    issued_on: date | None = None


class StartBuild(BaseModel):
    start_date: date | None = None
    projected_completion: date | None = None
    #: Building before the home is sold. Allowed, but said out loud - the
    #: difference between choosing to build on spec and clicking it by accident.
    allow_unsold: bool = False


class HoldRequest(BaseModel):
    reason: HoldReason
    note: str | None = Field(default=None, max_length=500)


class CompleteRequest(BaseModel):
    completed_on: date | None = None


class ReopenBuildRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class CloseRequest(BaseModel):
    closing_date: date | None = None
    warranty_months: int = Field(default=24, ge=0, le=240)


class CancelRequest(BaseModel):
    reason: CancellationReason
    note: str | None = Field(default=None, max_length=500)


class AssignSuperintendent(BaseModel):
    superintendent_id: uuid.UUID | None = None


class JobSummary(BaseModel):
    """The counters above the list. `late` and `on_hold` are where money burns."""

    live: int
    building: int
    on_hold: int
    late: int
    awaiting_permit: int
    by_status: dict[str, int]


class CommunityCycleTime(BaseModel):
    community_id: uuid.UUID
    homes: int
    average_days: int


class CycleTimeReport(BaseModel):
    """Start to completion, over homes that actually finished.

    `average_days` is None when nothing has completed yet - which is different
    from an average of zero, and saying zero would be a lie a report repeats.
    """

    homes_measured: int
    average_days: int | None
    by_community: list[CommunityCycleTime]


# --- Buyers and contracts -----------------------------------------------------


class BuyerCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str | None = None


class BuyerRead(ORMModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str | None
    phone: str | None


class ContractCreate(BaseModel):
    """Selling a home.

    Either name an existing buyer or give their details to create one. `buyer_id`
    is what a lead conversion produces, and using it is what keeps one person from
    becoming two records - which also keeps the chain that campaign revenue is
    traced along intact.
    """

    buyer_id: uuid.UUID | None = None
    buyer: BuyerCreate | None = None
    base_price: Money
    lot_premium: Money = Decimal("0")
    incentives: Money = Decimal("0")
    options_allowance: Money = Decimal("0")

    @model_validator(mode="after")
    def _one_or_the_other(self):
        if not self.buyer_id and not self.buyer:
            raise ValueError("Give either buyer_id or buyer details.")
        return self


class ContractRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    buyer_id: uuid.UUID
    status: ContractStatus
    base_price: Money
    lot_premium: Money
    options_total: Money
    incentives: Money
    total_price: Money
    signed_date: date | None


# --- Selections ---------------------------------------------------------------


class SelectionCreate(BaseModel):
    option_id: uuid.UUID
    quantity: int = Field(default=1, ge=1)
    selected_date: date | None = None


class SelectionRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    option_id: uuid.UUID
    status: SelectionStatus
    quantity: int
    price_at_selection: Money
    cost_at_selection: Money
    schedule_days_at_selection: int
    extended_price: Money
    extended_cost: Money
    selected_date: date | None


class BudgetChange(BaseModel):
    cost_code_id: str
    delta: Money
    revised_amount: Money


class SelectionCascade(BaseModel):
    """What one confirmed buyer decision moved, across every module it touches."""

    selection_id: uuid.UUID
    price_delta: Money
    cost_delta: Money
    schedule_days_delta: int
    contract_options_total: Money
    budget_changes: list[BudgetChange]


# --- Budgets ------------------------------------------------------------------


class BudgetLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None
    original_amount: Money
    revised_amount: Money
    variance: Money
    description: str | None


class BudgetRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    status: BudgetStatus
    original_total: Money
    revised_total: Money
    lines: list[BudgetLineRead]


# --- Purchase orders ----------------------------------------------------------


class PurchaseOrderLineCreate(BaseModel):
    cost_code_id: uuid.UUID
    amount: Money
    description: str | None = None


class PurchaseOrderLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    amount: Money
    description: str | None


class PurchaseOrderCreate(BaseModel):
    job_id: uuid.UUID
    vendor_id: uuid.UUID
    lines: list[PurchaseOrderLineCreate] = Field(min_length=1)
    notes: str | None = None
    required_by: date | None = None
    # Held back from every payment until the scope is complete. 0 for materials.
    retainage_percent: Decimal = Field(default=Decimal("0"), ge=0, le=50)


class PurchaseOrderUpdate(BaseModel):
    """Edit a draft. Once issued a PO is never edited — a change order supersedes it."""

    notes: str | None = None
    required_by: date | None = None
    retainage_percent: Decimal | None = Field(default=None, ge=0, le=50)
    # When given, replaces the lines wholesale.
    lines: list[PurchaseOrderLineCreate] | None = Field(default=None, min_length=1)


class PurchaseOrderRead(ORMModel):
    id: uuid.UUID
    po_number: str
    job_id: uuid.UUID
    vendor_id: uuid.UUID
    status: PurchaseOrderStatus
    issued_date: date | None
    issued_by_id: uuid.UUID | None = None
    required_by: date | None = None
    acknowledged_on: date | None = None
    completed_on: date | None = None
    notes: str | None
    total: Money
    retainage_percent: Decimal
    retainage_on_total: Money
    compliance_override: bool = False
    budget_override: bool = False
    override_reason: str | None = None
    invoiced_total: Money = Decimal("0")
    billed_to_date: Money = Decimal("0")
    remaining_to_bill: Money = Decimal("0")
    percent_billed: float = 0.0
    is_overdue: bool = False
    lines: list[PurchaseOrderLineRead]


class PurchaseOrderSummary(BaseModel):
    committed: Money  # issued, acknowledged or complete
    draft_value: Money
    drafts: int
    open: int  # issued or acknowledged — work still to be done
    awaiting_acknowledgement: int
    overdue: int  # past required-by and still open
    billed_to_date: Money  # approved invoices against committed POs
    remaining_to_bill: Money
    overrides: int  # issued past a gate, deliberately
    total: int
    by_status: dict[str, int]


# --- Job costing --------------------------------------------------------------


class JobCostLine(BaseModel):
    cost_code_id: uuid.UUID
    code: str
    name: str
    original_amount: Money
    revised_amount: Money
    committed_amount: Money
    actual_amount: Money
    uncommitted: Money
    # Exposure is max(committed, actual): an invoice above its PO still counts against
    # the job, so a lagging invoice can never make a job look better than it is.
    exposure: Money
    variance: Money
    # Raised but not yet approved, and where this code lands if it is.
    pending_change: Money
    forecast: Money
    forecast_variance: Money


class JobCostSummary(BaseModel):
    job_id: uuid.UUID
    has_budget: bool
    original_total: Money
    revised_total: Money
    committed_total: Money
    actual_total: Money
    exposure_total: Money
    uncommitted_total: Money
    variance_total: Money
    over_budget: bool
    # Change orders in flight. `forecast_over_budget` is the early warning that
    # `over_budget` cannot give, because it fires before the money is committed.
    pending_change_total: Money
    forecast_total: Money
    forecast_variance_total: Money
    forecast_over_budget: bool
    # Inside budget today, outside it if everything raised gets approved.
    will_go_over: bool
    # Withheld payment. Never nets off actual cost.
    retainage_held: Money
    retainage_released: Money
    lines: list[JobCostLine]
