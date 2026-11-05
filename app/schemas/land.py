"""Schemas for land development: projects, budget lines, planning obligations."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.land import LandPhaseStatus, ObligationKind, ObligationStatus, PlanningStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LandBudgetLineCreate(BaseModel):
    category: str = Field(min_length=1, max_length=120)
    budget_amount: Money = Field(default=Decimal("0"), ge=0)
    actual_amount: Money = Field(default=Decimal("0"), ge=0)
    vendor_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=500)


class LandBudgetLineUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=120)
    budget_amount: Money | None = Field(default=None, ge=0)
    actual_amount: Money | None = Field(default=None, ge=0)
    vendor_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=500)


class LandBudgetLineRead(ORMModel):
    id: uuid.UUID
    category: str
    vendor_id: uuid.UUID | None
    budget_amount: Money
    actual_amount: Money
    variance: Money
    notes: str | None


class ObligationCreate(BaseModel):
    kind: ObligationKind
    reference: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    trigger: str | None = Field(default=None, max_length=300)
    pre_commencement: bool = False
    due_date: date | None = None
    amount: Money | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class ObligationUpdate(BaseModel):
    kind: ObligationKind | None = None
    reference: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    trigger: str | None = Field(default=None, max_length=300)
    pre_commencement: bool | None = None
    due_date: date | None = None
    amount: Money | None = Field(default=None, ge=0)
    status: ObligationStatus | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ObligationComplete(BaseModel):
    completed_on: date | None = None
    waived: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class ObligationRead(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: ObligationKind
    reference: str | None
    description: str
    trigger: str | None
    pre_commencement: bool
    due_date: date | None
    amount: Money | None
    status: ObligationStatus
    completed_on: date | None
    notes: str | None
    is_overdue: bool


class LandProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    community_id: uuid.UUID | None = None
    acres: Decimal | None = Field(default=None, ge=0)
    site_area_ha: Decimal | None = Field(default=None, ge=0)
    planned_lots: int | None = Field(default=None, ge=0)
    acquisition_cost: Money = Field(default=Decimal("0"), ge=0)
    gdv: Money | None = Field(default=None, ge=0)
    build_cost_estimate: Money | None = Field(default=None, ge=0)
    purchased_on: date | None = None
    start_date: date | None = None
    projected_completion: date | None = None
    local_authority: str | None = Field(default=None, max_length=120)
    planning_ref: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class LandProjectUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    community_id: uuid.UUID | None = None
    status: LandPhaseStatus | None = None
    acres: Decimal | None = Field(default=None, ge=0)
    site_area_ha: Decimal | None = Field(default=None, ge=0)
    planned_lots: int | None = Field(default=None, ge=0)
    delivered_lots: int | None = Field(default=None, ge=0)
    acquisition_cost: Money | None = Field(default=None, ge=0)
    gdv: Money | None = Field(default=None, ge=0)
    build_cost_estimate: Money | None = Field(default=None, ge=0)
    purchased_on: date | None = None
    start_date: date | None = None
    projected_completion: date | None = None
    local_authority: str | None = Field(default=None, max_length=120)
    planning_ref: str | None = Field(default=None, max_length=64)
    planning_status: PlanningStatus | None = None
    planning_approved_on: date | None = None
    planning_expires_on: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class LandProjectRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    community_id: uuid.UUID | None
    status: LandPhaseStatus
    acres: Decimal | None
    site_area_ha: Decimal | None = None
    planned_lots: int | None
    delivered_lots: int
    acquisition_cost: Money
    gdv: Money | None = None
    build_cost_estimate: Money | None = None
    purchased_on: date | None = None
    start_date: date | None
    projected_completion: date | None
    local_authority: str | None = None
    planning_ref: str | None = None
    planning_status: PlanningStatus = PlanningStatus.NOT_SUBMITTED
    planning_approved_on: date | None = None
    planning_expires_on: date | None = None
    notes: str | None = None
    budget_total: Money
    actual_total: Money
    forecast_total: Money = Decimal("0")
    cost_per_lot: Money | None
    profit: Money | None = None
    margin_on_gdv: Decimal | None = None
    is_buildable: bool = False
    days_to_consent_expiry: int | None = None
    budget_lines: list[LandBudgetLineRead] = []
    obligations: list[ObligationRead] = []


class DeliverPlots(BaseModel):
    """Serviced plots handed from the land side to sales and construction."""

    start: int = Field(ge=0, le=99999)
    end: int = Field(ge=0, le=99999)
    prefix: str = Field(default="", max_length=10)
    phase_id: uuid.UUID | None = None
    premium: Money = Field(default=Decimal("0"), ge=0)


class LandSummary(BaseModel):
    projects: int
    by_status: dict[str, int]
    acquisition: Money
    budget: Money
    actual: Money
    forecast: Money
    planned_plots: int
    delivered_plots: int
    consents_expiring: int  # within 180 days, not yet complete
    obligations_open: int
    obligations_overdue: int
    pre_commencement_open: int
    contributions_due: Money  # open S106 / CIL / other sums
