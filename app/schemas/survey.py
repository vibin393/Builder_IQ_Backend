"""Schemas for the homeowner-satisfaction survey module."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.marketing import SurveyStage, SurveyStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SurveyCreate(BaseModel):
    job_id: uuid.UUID
    stage: SurveyStage = SurveyStage.THIRTY_DAY


class SurveyResponse(BaseModel):
    would_recommend: int = Field(ge=0, le=10)
    rating_sales: int | None = Field(default=None, ge=1, le=5)
    rating_construction: int | None = Field(default=None, ge=1, le=5)
    rating_quality: int | None = Field(default=None, ge=1, le=5)
    rating_warranty: int | None = Field(default=None, ge=1, le=5)
    rating_communication: int | None = Field(default=None, ge=1, le=5)
    rating_value: int | None = Field(default=None, ge=1, le=5)
    overall_satisfaction: int | None = Field(default=None, ge=1, le=5)
    comments: str | None = Field(default=None, max_length=4000)


class SurveyFollowUp(BaseModel):
    follow_up_notes: str | None = Field(default=None, max_length=2000)
    follow_up_done: bool = False
    follow_up_owner_id: uuid.UUID | None = None


class SurveyRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    buyer_id: uuid.UUID | None
    stage: SurveyStage
    status: SurveyStatus
    sent_date: date | None
    due_date: date | None
    responded_date: date | None
    reminder_count: int
    last_reminded_date: date | None
    would_recommend: int | None
    rating_sales: int | None
    rating_construction: int | None
    rating_quality: int | None
    rating_warranty: int | None
    rating_communication: int | None
    rating_value: int | None
    overall_satisfaction: int | None
    comments: str | None
    follow_up_required: bool
    follow_up_done: bool
    follow_up_notes: str | None
    follow_up_owner_id: uuid.UUID | None
    nps_band: str | None
    is_overdue: bool = False
    needs_follow_up: bool = False


# --- dashboard summary --------------------------------------------------------


class TrendPoint(BaseModel):
    label: str          # e.g. "2026-03"
    nps: int | None
    responses: int


class StageBreakdown(BaseModel):
    stage: str
    sent: int
    responses: int
    nps: int | None


class SurveySummary(BaseModel):
    sent: int
    responses: int
    pending: int
    overdue: int
    declined: int
    response_rate: float          # 0-1
    nps: int | None
    star_rating: int | None
    pct_recommend: float | None   # 0-100
    promoters: int
    passives: int
    detractors: int
    detractors_open: int          # detractors still needing follow-up
    avg_overall: float | None
    ratings: dict[str, float | None]
    by_stage: list[StageBreakdown]
    trend: list[TrendPoint]
