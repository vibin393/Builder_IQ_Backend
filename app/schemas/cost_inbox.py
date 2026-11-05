"""Request and response shapes for the cost inbox."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.cost_inbox import CostInboxStatus

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CostInboxCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    job_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    tags: list[str] | None = None
    notes: str | None = Field(default=None, max_length=1000)


class CostInboxUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    job_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    tags: list[str] | None = None
    notes: str | None = Field(default=None, max_length=1000)


class CostInboxRead(ORMModel):
    id: uuid.UUID
    title: str
    status: CostInboxStatus
    amount: Money | None
    purchase_date: date | None
    job_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    document_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    tags: list | None
    notes: str | None
    is_coded: bool


class CostInboxSummary(BaseModel):
    count: int
    new: int
    coded: int
    pushed: int
    rejected: int
    open_amount: Money
