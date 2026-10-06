"""Schemas for house plans: the specification, elevations, standard costs, price history."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.plan import GarageType, HouseType

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


_EPC = r"^[A-Ga-g]$"


class PlanCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    square_feet: int | None = Field(default=None, ge=0, le=100000)
    floor_area_sqm: Decimal | None = Field(default=None, ge=0, le=10000)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=20)
    en_suites: int | None = Field(default=None, ge=0, le=20)
    has_cloakroom: bool = False
    house_type: HouseType | None = None
    storeys: int | None = Field(default=None, ge=1, le=10)
    garage: GarageType | None = None
    parking_spaces: int | None = Field(default=None, ge=0, le=20)
    epc_target: str | None = Field(default=None, pattern=_EPC)
    description: str | None = Field(default=None, max_length=2000)
    base_price: Money = Field(default=Decimal("0"), ge=0)


class PlanUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    square_feet: int | None = Field(default=None, ge=0, le=100000)
    floor_area_sqm: Decimal | None = Field(default=None, ge=0, le=10000)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=20)
    en_suites: int | None = Field(default=None, ge=0, le=20)
    has_cloakroom: bool | None = None
    house_type: HouseType | None = None
    storeys: int | None = Field(default=None, ge=1, le=10)
    garage: GarageType | None = None
    parking_spaces: int | None = Field(default=None, ge=0, le=20)
    epc_target: str | None = Field(default=None, pattern=_EPC)
    description: str | None = Field(default=None, max_length=2000)
    base_price: Money | None = Field(default=None, ge=0)
    #: Why the price moved. Kept on the price history.
    price_reason: str | None = Field(default=None, max_length=300)


class PlanCopy(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


class PlanStats(BaseModel):
    standard_cost: Money
    cost_lines: int
    labour_hours: Decimal
    margin: Money                     # base price less standard cost
    margin_pct: Decimal | None
    elevations: int
    communities: int                  # developments it is offered on
    live_homes: int
    finished_homes: int
    #: Average actual cost (approved invoices) of finished homes of this plan, and how
    #: far it ran from the standard. The estimator's feedback loop.
    avg_actual_cost: Money | None
    cost_variance_pct: Decimal | None


class PlanRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    square_feet: int | None
    floor_area_sqm: Decimal | None = None
    bedrooms: int | None
    bathrooms: Decimal | None
    en_suites: int | None = None
    has_cloakroom: bool = False
    house_type: HouseType | None = None
    storeys: int | None = None
    garage: GarageType | None = None
    parking_spaces: int | None = None
    epc_target: str | None = None
    description: str | None = None
    base_price: Money
    is_active: bool
    stats: PlanStats | None = None


class PlanPortfolio(BaseModel):
    active: int
    archived: int
    avg_margin_pct: Decimal | None
    #: Active plans below the margin floor.
    low_margin: int
    margin_floor_pct: Decimal
    #: Active plans with no standard costs: a home on one of these cannot be budgeted.
    no_costs: int
    live_homes: int
    price_changes_90_days: int


class PlanCostLineCreate(BaseModel):
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    amount: Money = Field(ge=0)
    labour_hours: Decimal | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=300)


class PlanCostLineUpdate(BaseModel):
    vendor_id: uuid.UUID | None = None
    amount: Money | None = Field(default=None, ge=0)
    labour_hours: Decimal | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=300)


class PlanCostLineRead(ORMModel):
    id: uuid.UUID
    cost_code_id: uuid.UUID
    vendor_id: uuid.UUID | None
    amount: Money
    labour_hours: Decimal | None = None
    description: str | None


class ElevationCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    price_adjustment: Money = Decimal("0")
    description: str | None = Field(default=None, max_length=500)


class ElevationUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    price_adjustment: Money | None = None
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class ElevationRead(ORMModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    code: str
    name: str
    price_adjustment: Money
    description: str | None
    is_active: bool
    homes: int = 0


class PriceChangeRead(ORMModel):
    id: uuid.UUID
    old_price: Money
    new_price: Money
    changed_by_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class PricingGuide(BaseModel):
    """What a home of this plan should sell for, built up the way a price list is."""

    plan_price: Money
    community_price: Money | None      # this development's price for the plan, if set
    house_price: Money                 # the one that applies
    elevation_code: str | None
    elevation_adjustment: Money
    plot_premium: Money
    base_price: Money                  # house price + elevation: the contract's base
    total: Money                       # base + plot premium
