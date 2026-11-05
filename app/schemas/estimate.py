"""Request and response shapes for estimates, client updates and the activity feed."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.estimate import EstimateStatus

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Num = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- estimate items ----
class EstimateItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    group_name: str = Field(default="General", max_length=120)
    cost_code_id: uuid.UUID | None = None
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal = Field(default=Decimal("1"), ge=0)
    unit_cost: Decimal = Field(default=Decimal("0"), ge=0)
    markup_percent: Decimal = Field(default=Decimal("0"), ge=0)
    sort_order: int = 0


class EstimateItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, min_length=1, max_length=500)
    group_name: str | None = Field(default=None, max_length=120)
    cost_code_id: uuid.UUID | None = None
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal | None = Field(default=None, ge=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)
    markup_percent: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = None


class EstimateItemRead(ORMModel):
    id: uuid.UUID
    estimate_id: uuid.UUID
    group_name: str
    description: str
    cost_code_id: uuid.UUID | None
    unit: str | None
    quantity: Num
    unit_cost: Money
    markup_percent: Num
    sort_order: int
    unit_price: Money
    extended_cost: Money
    extended_price: Money


# ---- estimates ----
class EstimateCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    default_markup_percent: Decimal = Field(default=Decimal("0"), ge=0)
    tax_percent: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = None


class EstimateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    default_markup_percent: Decimal | None = Field(default=None, ge=0)
    tax_percent: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None


class EstimateRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    status: EstimateStatus
    default_markup_percent: Num
    tax_percent: Num
    issued_on: date | None
    notes: str | None
    total_cost: Money
    subtotal: Money
    tax_amount: Money
    total_price: Money
    margin: Money
    items: list[EstimateItemRead] = []


class EstimateListRow(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    status: EstimateStatus
    issued_on: date | None
    subtotal: Money
    total_price: Money


# ---- client updates ----
class ClientUpdateCreate(BaseModel):
    job_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    is_published: bool = True


class ClientUpdateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=8000)
    is_published: bool | None = None


class ClientUpdateRead(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    body: str
    posted_by_id: uuid.UUID | None
    posted_by_name: str
    is_published: bool
    posted_on: date


# ---- activity feed ----
class ActivityEvent(BaseModel):
    kind: str  # comment, daily_log, client_update, invoice, shift, selection, change_order
    at: datetime
    actor: str
    title: str
    detail: str | None = None
