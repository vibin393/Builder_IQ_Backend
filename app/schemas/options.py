"""Schemas for the option catalog."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.option import OptionCategory

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OptionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    category: OptionCategory = OptionCategory.OTHER
    price: Money = Decimal("0")
    schedule_days: int = Field(default=0, ge=-365, le=365)
    is_structural: bool = False
    max_quantity: int = Field(default=1, ge=1, le=999)
    unit: str | None = Field(default=None, max_length=20)
    supplier_ref: str | None = Field(default=None, max_length=64)
    #: House types it is offered on. Empty: every plan.
    plan_ids: list[uuid.UUID] = []


class OptionUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    category: OptionCategory | None = None
    price: Money | None = None
    price_reason: str | None = Field(default=None, max_length=300)
    schedule_days: int | None = Field(default=None, ge=-365, le=365)
    is_structural: bool | None = None
    max_quantity: int | None = Field(default=None, ge=1, le=999)
    unit: str | None = Field(default=None, max_length=20)
    supplier_ref: str | None = Field(default=None, max_length=64)


class OptionCopy(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


class OptionStats(BaseModel):
    cost: Money
    margin: Money
    margin_pct: Decimal | None
    cost_lines: int
    confirmed: int                 # confirmed selections, units summed
    pending: int                   # chosen, not yet confirmed
    revenue: Money                 # confirmed, at the price each was sold at
    #: Confirmed on this share of sold homes it could have gone on.
    attach_rate_pct: Decimal | None
    plans: int                     # 0: offered on every plan


class OptionRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    category: OptionCategory
    price: Money
    schedule_days: int
    is_structural: bool
    is_active: bool
    max_quantity: int = 1
    unit: str | None = None
    supplier_ref: str | None = None
    stats: OptionStats | None = None


class OptionCostLineCreate(BaseModel):
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    #: May be negative: a credit option takes cost out.
    amount: Money
    description: str | None = Field(default=None, max_length=300)


class OptionCostLineUpdate(BaseModel):
    vendor_id: uuid.UUID | None = None
    amount: Money | None = None
    description: str | None = Field(default=None, max_length=300)


class OptionCostLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None
    amount: Money
    description: str | None


class OptionDetail(OptionRead):
    cost_lines: list[OptionCostLineRead] = []
    total_cost: Money = Decimal("0")
    plan_ids: list[uuid.UUID] = []


class OptionPlansSet(BaseModel):
    plan_ids: list[uuid.UUID]


class OptionSelectionRow(BaseModel):
    selection_id: uuid.UUID
    job_id: uuid.UUID
    job_number: str
    status: str
    quantity: int
    price_at_selection: Money
    cost_at_selection: Money
    selected_date: date | None


class OptionPriceChangeRead(ORMModel):
    id: uuid.UUID
    old_price: Money
    new_price: Money
    changed_by_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class PopularOption(BaseModel):
    option_id: uuid.UUID
    code: str
    name: str
    confirmed: int
    revenue: Money


class OptionPortfolio(BaseModel):
    active: int
    archived: int
    by_category: dict[str, int]
    avg_margin_pct: Decimal | None
    low_margin: int
    margin_floor_pct: Decimal
    #: Priced, but no cost lines: selling one moves the contract and not the budget.
    no_costs: int
    confirmed_revenue: Money
    pending_selections: int
    top: list[PopularOption]
