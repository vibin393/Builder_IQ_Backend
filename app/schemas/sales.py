"""Request and response shapes for lead proposals, the sales calendar and lead map."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.lead_proposal import LeadProposalStatus

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Coord = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.6f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


Num = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


# ---- proposal line items ----
class ProposalItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    group_name: str = Field(default="General", max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal = Field(default=Decimal("1"), ge=0)
    unit_cost: Decimal = Field(default=Decimal("0"), ge=0)
    markup_percent: Decimal = Field(default=Decimal("0"), ge=0)
    sort_order: int = 0


class ProposalItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, min_length=1, max_length=500)
    group_name: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal | None = Field(default=None, ge=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)
    markup_percent: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = None


class ProposalItemRead(ORMModel):
    id: uuid.UUID
    proposal_id: uuid.UUID
    group_name: str
    description: str
    unit: str | None
    quantity: Num
    unit_cost: Money
    markup_percent: Num
    sort_order: int
    unit_price: Money
    extended_cost: Money
    extended_price: Money


# ---- proposal templates ----
class ProposalTemplateItemRead(ORMModel):
    id: uuid.UUID
    group_name: str
    description: str
    unit: str | None
    quantity: Num
    unit_cost: Money
    markup_percent: Num
    sort_order: int
    unit_price: Money
    extended_price: Money


class ProposalTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    items: list[ProposalItemCreate] = Field(default_factory=list)


class SaveAsTemplate(BaseModel):
    """Save an existing proposal's line items as a reusable template."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProposalTemplateRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    total: Money
    items: list[ProposalTemplateItemRead] = []


# ---- lead proposals ----
class LeadProposalCreate(BaseModel):
    lead_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    valid_until: date | None = None
    notes: str | None = None


class LeadProposalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, ge=0)
    valid_until: date | None = None
    notes: str | None = None


class LeadProposalRead(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    lead_name: str
    title: str
    amount: Money
    total: Money
    status: LeadProposalStatus
    sent_on: date | None
    valid_until: date | None
    decided_on: date | None
    notes: str | None
    is_expired: bool
    items: list[ProposalItemRead] = []


# ---- sales calendar ----
class CalendarEvent(BaseModel):
    date: date
    kind: str  # follow_up, appointment, proposal_expiry
    ref_id: uuid.UUID | None
    title: str
    detail: str | None = None


# ---- lead map ----
class LeadMapPoint(BaseModel):
    id: uuid.UUID
    name: str
    lat: Coord
    lng: Coord
    status: str
    city: str | None
