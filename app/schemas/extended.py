"""Schemas for bidding, field ops, marketing, design studio, e-signature and land."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.communication import BallInCourt
from app.models.field import InspectionResult, InspectionType, ItemResult, PunchStatus
from app.models.marketing import CampaignStatus, SurveyStage
from app.models.sales_ops import AppointmentStatus, DocumentKind, SignatureStatus

Money = Decimal


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# NOTE: The bidding schemas moved to app/schemas/bidding.py when bidding grew levelling,
# an estimate/savings benchmark, coverage stats, decline and analytics.


# --- Field: daily logs, inspections, punch list -------------------------------


class DailyLogCreate(BaseModel):
    job_id: uuid.UUID
    log_date: date | None = None
    weather: str | None = None
    crew_count: int | None = None
    trades_on_site: str | None = None
    work_performed: str | None = None
    delays: str | None = None
    notes: str | None = None


class DailyLogRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    log_date: date
    author_id: uuid.UUID | None
    weather: str | None
    crew_count: int | None
    trades_on_site: str | None
    work_performed: str | None
    delays: str | None
    notes: str | None


class InspectionCreate(BaseModel):
    job_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    inspection_type: InspectionType = InspectionType.MUNICIPAL
    scheduled_date: date | None = None
    inspector: str | None = None


class InspectionResultUpdate(BaseModel):
    result: InspectionResult
    completed_date: date | None = None
    failure_reason: str | None = None


class InspectionItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    vendor_id: uuid.UUID | None = None


class InspectionItemResultUpdate(BaseModel):
    result: ItemResult
    notes: str | None = None


class InspectionItemRead(ORMModel):
    id: uuid.UUID
    sequence: int
    description: str
    result: ItemResult
    notes: str | None
    vendor_id: uuid.UUID | None


class InspectionRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    inspection_type: InspectionType
    name: str
    result: InspectionResult
    scheduled_date: date | None
    completed_date: date | None
    inspector: str | None
    failure_reason: str | None
    checked_items: int
    items: list[InspectionItemRead] = []


class PunchItemCreate(BaseModel):
    job_id: uuid.UUID
    description: str = Field(min_length=1, max_length=2000)
    location: str | None = None
    vendor_id: uuid.UUID | None = None
    cost_code_id: uuid.UUID | None = None
    from_walkthrough: bool = False
    due_date: date | None = None
    ball_in_court: BallInCourt = BallInCourt.TRADE


class PunchItemUpdate(BaseModel):
    status: PunchStatus | None = None
    vendor_id: uuid.UUID | None = None
    due_date: date | None = None
    ball_in_court: BallInCourt | None = None


class PunchItemRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    description: str
    location: str | None
    status: PunchStatus
    vendor_id: uuid.UUID | None
    cost_code_id: uuid.UUID | None
    from_walkthrough: bool
    ball_in_court: BallInCourt
    due_date: date | None
    completed_date: date | None


# --- Marketing and surveys ----------------------------------------------------


class CampaignCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    subject: str | None = None
    body: str | None = None
    community_id: uuid.UUID | None = None
    scheduled_for: date | None = None


class CampaignMetrics(BaseModel):
    sent_count: int = 0
    opened_count: int = 0
    clicked_count: int = 0
    bounced_count: int = 0
    unsubscribed_count: int = 0


class CampaignRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    subject: str | None
    body: str | None
    status: CampaignStatus
    community_id: uuid.UUID | None
    scheduled_for: date | None
    sent_date: date | None
    sent_count: int
    opened_count: int
    clicked_count: int
    bounced_count: int
    unsubscribed_count: int
    open_rate: float
    click_rate: float


class SurveyCreate(BaseModel):
    job_id: uuid.UUID
    stage: SurveyStage = SurveyStage.THIRTY_DAY


class SurveyResponse(BaseModel):
    would_recommend: int = Field(ge=0, le=10)
    rating_sales: int | None = Field(default=None, ge=1, le=5)
    rating_construction: int | None = Field(default=None, ge=1, le=5)
    rating_quality: int | None = Field(default=None, ge=1, le=5)
    rating_warranty: int | None = Field(default=None, ge=1, le=5)
    comments: str | None = None


class SurveyRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    stage: SurveyStage
    sent_date: date | None
    responded_date: date | None
    would_recommend: int | None
    rating_sales: int | None
    rating_construction: int | None
    rating_quality: int | None
    rating_warranty: int | None
    comments: str | None
    nps_band: str | None


# --- Design studio and e-signature --------------------------------------------


class AppointmentCreate(BaseModel):
    job_id: uuid.UUID
    scheduled_for: datetime | None = None
    duration_minutes: int = 180
    consultant_id: uuid.UUID | None = None
    location: str | None = None
    structural_cutoff: date | None = None
    notes: str | None = None


class AppointmentRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    consultant_id: uuid.UUID | None
    scheduled_for: datetime | None
    duration_minutes: int
    status: AppointmentStatus
    location: str | None
    structural_cutoff: date | None
    notes: str | None


class SignatureCreate(BaseModel):
    job_id: uuid.UUID
    document_kind: DocumentKind = DocumentKind.CONTRACT
    document_name: str = Field(min_length=1, max_length=300)
    signer_name: str = Field(min_length=1, max_length=200)
    signer_email: EmailStr
    expires_on: date | None = None


class SignatureRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    document_kind: DocumentKind
    document_name: str
    signer_name: str
    signer_email: str
    status: SignatureStatus
    sent_at: datetime | None
    signed_at: datetime | None
    expires_on: date | None
    provider: str | None


# --- Land development ---------------------------------------------------------


# Land development schemas moved to app/schemas/land.py.


# Accounting export schemas moved to app/schemas/accounting.py when the flat GL dump
# became batched, double-entry journals.
