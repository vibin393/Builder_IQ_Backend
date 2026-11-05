"""Schemas for developments: communities, phases, plans offered, plots and their holds."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.community import CommunityStatus, LotStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- communities -----------------------------------------------------------------


class CommunityCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    county: str | None = Field(default=None, max_length=80)
    status: CommunityStatus = CommunityStatus.SELLING
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    local_authority: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    planned_plots: int | None = Field(default=None, ge=0, le=100000)
    opened_on: date | None = None
    target_sales_per_month: Decimal | None = Field(default=None, ge=0, le=1000)
    sales_manager_id: uuid.UUID | None = None
    site_manager_id: uuid.UUID | None = None


class CommunityUpdate(BaseModel):
    """Every field optional: a PATCH says what changed."""

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    county: str | None = Field(default=None, max_length=80)
    status: CommunityStatus | None = None
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    local_authority: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    planned_plots: int | None = Field(default=None, ge=0, le=100000)
    opened_on: date | None = None
    target_sales_per_month: Decimal | None = Field(default=None, ge=0, le=1000)
    sales_manager_id: uuid.UUID | None = None
    site_manager_id: uuid.UUID | None = None


class PlotCounts(BaseModel):
    total: int = 0
    not_released: int = 0
    available: int = 0
    held: int = 0
    sold: int = 0
    closed: int = 0


class CommunityStats(BaseModel):
    plots: PlotCounts
    live_homes: int  # a job not cancelled and not yet handed over
    spec_homes: int  # being built with no buyer: stock to sell
    sales_90_days: int
    #: Sales per month over the last 90 days.
    absorption: Decimal
    #: Unsold plots (available + held) at that rate. None when nothing is selling.
    months_of_supply: Decimal | None
    holds_expiring_soon: int  # within 7 days


class CommunityRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    city: str | None
    county: str | None
    is_active: bool
    status: CommunityStatus = CommunityStatus.SELLING
    address: str | None = None
    postcode: str | None = None
    local_authority: str | None = None
    description: str | None = None
    planned_plots: int | None = None
    opened_on: date | None = None
    target_sales_per_month: Decimal | None = None
    sales_manager_id: uuid.UUID | None = None
    site_manager_id: uuid.UUID | None = None
    stats: CommunityStats | None = None


class CommunityPortfolio(BaseModel):
    communities: int
    by_status: dict[str, int]
    plots: PlotCounts
    live_homes: int
    spec_homes: int
    sales_90_days: int
    holds_expiring_soon: int
    #: Unsold plots in a community that is not yet on sale — land waiting on release.
    unreleased_plots: int


# --- phases ------------------------------------------------------------------------


class PhaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sequence: int | None = Field(default=None, ge=1, le=999)
    planned_release: date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class PhaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sequence: int | None = Field(default=None, ge=1, le=999)
    planned_release: date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class PhaseRead(ORMModel):
    id: uuid.UUID
    community_id: uuid.UUID
    name: str
    sequence: int
    planned_release: date | None
    released_on: date | None
    notes: str | None
    plots: PlotCounts = PlotCounts()


# --- plans offered -------------------------------------------------------------------


class CommunityPlanSet(BaseModel):
    plan_id: uuid.UUID
    #: Blank: the plan's own base price applies here.
    base_price: Money | None = Field(default=None, ge=0)
    is_active: bool = True


class CommunityPlanRead(BaseModel):
    plan_id: uuid.UUID
    code: str
    name: str
    bedrooms: int | None
    plan_base_price: Money
    base_price: Money | None
    effective_price: Money
    is_active: bool
    homes: int  # live homes of this plan on this development


# --- plots ----------------------------------------------------------------------------


class LotCreate(BaseModel):
    lot_number: str = Field(min_length=1, max_length=32)
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    premium: Money = Field(default=Decimal("0"), ge=0)
    #: Only a starting position: a plot is created unreleased or on sale. Held, sold
    #: and closed are reached through the hold and the contract, never typed in.
    status: LotStatus = LotStatus.AVAILABLE
    phase_id: uuid.UUID | None = None
    plot_area_sqm: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class LotBulkCreate(BaseModel):
    """Plots 1–40 in one go, the way a site layout numbers them."""

    start: int = Field(ge=0, le=99999)
    end: int = Field(ge=0, le=99999)
    prefix: str = Field(default="", max_length=10)
    phase_id: uuid.UUID | None = None
    premium: Money = Field(default=Decimal("0"), ge=0)
    status: LotStatus = LotStatus.NOT_RELEASED
    postcode: str | None = Field(default=None, max_length=20)


class LotBulkResult(BaseModel):
    created: int
    skipped: list[str]  # numbers that already existed


class LotUpdate(BaseModel):
    lot_number: str | None = Field(default=None, min_length=1, max_length=32)
    address: str | None = Field(default=None, max_length=300)
    postcode: str | None = Field(default=None, max_length=20)
    premium: Money | None = Field(default=None, ge=0)
    phase_id: uuid.UUID | None = None
    plot_area_sqm: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class LotHold(BaseModel):
    until: date
    reason: str | None = Field(default=None, max_length=300)
    lead_id: uuid.UUID | None = None


class LotRead(ORMModel):
    id: uuid.UUID
    community_id: uuid.UUID
    lot_number: str
    address: str | None
    postcode: str | None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    status: LotStatus
    premium: Money
    phase_id: uuid.UUID | None = None
    plot_area_sqm: Decimal | None = None
    notes: str | None = None
    released_on: date | None = None
    hold_until: date | None = None
    hold_reason: str | None = None
    held_on: date | None = None
    held_by_id: uuid.UUID | None = None
    held_for_lead_id: uuid.UUID | None = None
    # Filled from the live job on the plot, when there is one.
    job_id: uuid.UUID | None = None
    job_number: str | None = None
    job_status: str | None = None
    plan_id: uuid.UUID | None = None
    #: Being built with no buyer: a speculative home, and the first thing to sell.
    is_spec: bool = False
    held_for_name: str | None = None
    hold_days_left: int | None = None
