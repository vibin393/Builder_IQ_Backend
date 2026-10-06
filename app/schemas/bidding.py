"""Schemas for bid management."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.bidding import BidPackageStatus, BidStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BidPackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cost_code_id: uuid.UUID
    scope: str | None = None
    plan_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    due_date: date | None = None
    estimate: Money | None = Field(default=None, ge=0)


class BidPackageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    scope: str | None = None
    plan_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    due_date: date | None = None
    estimate: Money | None = Field(default=None, ge=0)


class BidRead(ORMModel):
    id: uuid.UUID
    package_id: uuid.UUID
    vendor_id: uuid.UUID
    status: BidStatus
    amount: Money | None
    submitted_date: date | None
    revision_bid: int
    notes: str | None


class InviteRequest(BaseModel):
    vendor_ids: list[uuid.UUID] = Field(min_length=1)


class BidSubmit(BaseModel):
    amount: Money = Field(ge=0)
    notes: str | None = None


class BidPackageRead(ORMModel):
    id: uuid.UUID
    package_number: str
    name: str
    scope: str | None
    cost_code_id: uuid.UUID
    plan_id: uuid.UUID | None
    community_id: uuid.UUID | None
    status: BidPackageStatus
    due_date: date | None
    estimate: Money | None
    document_revision: int
    # Bid levelling
    low_bid: Money | None
    high_bid: Money | None
    average_bid: Money | None
    spread: Money | None
    awarded_amount: Money | None
    savings_vs_estimate: Money | None
    invited_count: int
    submitted_count: int
    declined_count: int
    response_rate: float | None
    is_overdue: bool
    bids: list[BidRead] = []


class AwardResult(BaseModel):
    """What awarding a bid produced. The PO is the point of the whole exercise."""

    bid_id: uuid.UUID
    vendor_id: uuid.UUID
    amount: Money
    purchase_order_id: uuid.UUID | None
    po_number: str | None
    not_awarded: int
    savings_vs_estimate: Money | None


class BidSummary(BaseModel):
    packages: int
    draft: int
    out_for_bid: int
    awarded: int
    cancelled: int
    overdue: int
    invited: int
    submitted: int             # bids that came back with a price
    response_rate: float | None
    awarded_value: Money       # total value of awarded bids
    savings: Money             # total estimate-minus-awarded across awarded packages
