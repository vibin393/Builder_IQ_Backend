"""Homeowner satisfaction surveys.

Sending, chasing and reading the surveys, plus the analytics that make them worth
sending: NPS, the UK star rating, response rate, the trend over time, and the list of
detractors still owed a follow-up. The dashboard and the queue are computed from the same
rows, so the headline can never disagree with the work list.
"""

import uuid
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DbSession, RequireStaff, get_tenant_scoped
from app.models.contract import Contract
from app.models.job import Job
from app.models.marketing import Survey, SurveyStage, SurveyStatus
from app.models.user import User
from app.schemas.survey import (
    StageBreakdown,
    SurveyCreate,
    SurveyFollowUp,
    SurveyRead,
    SurveyResponse,
    SurveySummary,
    TrendPoint,
)
from app.services import survey as survey_service

router = APIRouter(prefix="/surveys", tags=["surveys"])


def _nps(promoters: int, detractors: int, n: int) -> int | None:
    return round(((promoters - detractors) / n) * 100) if n else None


# --- summary (must precede /{survey_id} sub-routes only conceptually) ----------


@router.get("/summary", response_model=SurveySummary)
async def summary(db: DbSession, user: RequireStaff):
    """Everything the dashboard needs, from one pass over the tenant's surveys."""
    rows = list(await db.scalars(select(Survey).where(Survey.tenant_id == user.tenant_id)))

    sent = len(rows)
    responded = [r for r in rows if r.responded_date is not None]
    pending = [r for r in rows if r.status is SurveyStatus.PENDING]
    overdue = sum(1 for r in pending if r.is_overdue)
    declined = sum(1 for r in rows if r.status is SurveyStatus.DECLINED)

    scored = [r for r in responded if r.would_recommend is not None]
    n = len(scored)
    promoters = sum(1 for r in scored if r.nps_band == "promoter")
    passives = sum(1 for r in scored if r.nps_band == "passive")
    detractors = sum(1 for r in scored if r.nps_band == "detractor")
    detractors_open = sum(1 for r in rows if r.needs_follow_up)

    pct_recommend = round(promoters / n * 100, 1) if n else None

    def avg(attr: str) -> float | None:
        vals = [getattr(r, attr) for r in responded if getattr(r, attr) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # By stage: sent, responses and NPS per lifecycle stage.
    by_stage: list[StageBreakdown] = []
    for st in SurveyStage:
        stage_rows = [r for r in rows if r.stage is st]
        stage_scored = [r for r in stage_rows if r.would_recommend is not None]
        sp = sum(1 for r in stage_scored if r.nps_band == "promoter")
        sd = sum(1 for r in stage_scored if r.nps_band == "detractor")
        by_stage.append(
            StageBreakdown(
                stage=st.value,
                sent=len(stage_rows),
                responses=len(stage_scored),
                nps=_nps(sp, sd, len(stage_scored)),
            )
        )

    # Trend: NPS by the month the response came in, oldest first, last 12 months present.
    buckets: dict[str, list[Survey]] = defaultdict(list)
    for r in scored:
        buckets[r.responded_date.strftime("%Y-%m")].append(r)
    trend: list[TrendPoint] = []
    for label in sorted(buckets)[-12:]:
        group = buckets[label]
        gp = sum(1 for r in group if r.nps_band == "promoter")
        gd = sum(1 for r in group if r.nps_band == "detractor")
        trend.append(TrendPoint(label=label, nps=_nps(gp, gd, len(group)), responses=len(group)))

    return SurveySummary(
        sent=sent,
        responses=n,
        pending=len(pending),
        overdue=overdue,
        declined=declined,
        response_rate=round(len(responded) / sent, 3) if sent else 0.0,
        nps=_nps(promoters, detractors, n),
        star_rating=survey_service.star_rating(pct_recommend),
        pct_recommend=pct_recommend,
        promoters=promoters,
        passives=passives,
        detractors=detractors,
        detractors_open=detractors_open,
        avg_overall=avg("overall_satisfaction"),
        ratings={
            "sales": avg("rating_sales"),
            "construction": avg("rating_construction"),
            "quality": avg("rating_quality"),
            "warranty": avg("rating_warranty"),
            "communication": avg("rating_communication"),
            "value": avg("rating_value"),
        },
        by_stage=by_stage,
        trend=trend,
    )


# --- list / send --------------------------------------------------------------


@router.get("", response_model=list[SurveyRead])
async def list_surveys(
    db: DbSession,
    user: RequireStaff,
    job_id: uuid.UUID | None = None,
    stage: SurveyStage | None = None,
    status_filter: SurveyStatus | None = Query(default=None, alias="status"),
    band: str | None = Query(default=None, pattern="^(promoter|passive|detractor)$"),
    needs_follow_up: bool | None = None,
):
    stmt = select(Survey).where(Survey.tenant_id == user.tenant_id)
    if job_id is not None:
        stmt = stmt.where(Survey.job_id == job_id)
    if stage is not None:
        stmt = stmt.where(Survey.stage == stage)
    if status_filter is not None:
        stmt = stmt.where(Survey.status == status_filter)
    if band == "promoter":
        stmt = stmt.where(Survey.would_recommend >= 9)
    elif band == "passive":
        stmt = stmt.where(Survey.would_recommend >= 7, Survey.would_recommend < 9)
    elif band == "detractor":
        stmt = stmt.where(Survey.would_recommend.is_not(None), Survey.would_recommend < 7)
    if needs_follow_up:
        stmt = stmt.where(Survey.follow_up_required.is_(True), Survey.follow_up_done.is_(False))

    rows = await db.scalars(stmt.order_by(Survey.created_at.desc()))
    return list(rows)


@router.post("", response_model=SurveyRead, status_code=status.HTTP_201_CREATED)
async def send_survey(payload: SurveyCreate, db: DbSession, user: RequireStaff):
    """Send a survey to the homeowner. The response window opens today."""
    job = await get_tenant_scoped(db, Job, user.tenant_id, payload.job_id)
    contract = await db.scalar(select(Contract).where(Contract.job_id == job.id))
    today = date.today()
    obj = Survey(
        tenant_id=user.tenant_id,
        job_id=job.id,
        buyer_id=contract.buyer_id if contract else None,
        stage=payload.stage,
        status=SurveyStatus.PENDING,
        sent_date=today,
        due_date=survey_service.due_date_for(today),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


# --- respond / chase / act ----------------------------------------------------


@router.post("/{survey_id}/respond", response_model=SurveyRead)
async def respond_to_survey(
    survey_id: uuid.UUID, payload: SurveyResponse, db: DbSession, user: RequireStaff
):
    """Record the homeowner's answers. A detractor is flagged for follow-up automatically."""
    s = await get_tenant_scoped(db, Survey, user.tenant_id, survey_id)
    for field_, value in payload.model_dump().items():
        setattr(s, field_, value)
    s.responded_date = date.today()
    s.status = SurveyStatus.RESPONDED
    if s.nps_band == "detractor":
        s.follow_up_required = True
    await db.commit()
    await db.refresh(s)
    return s


@router.post("/{survey_id}/remind", response_model=SurveyRead)
async def remind_survey(survey_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Chase an unanswered survey. Only pending surveys can be reminded."""
    s = await get_tenant_scoped(db, Survey, user.tenant_id, survey_id)
    if s.status is not SurveyStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a pending survey can be reminded.",
        )
    s.reminder_count += 1
    s.last_reminded_date = date.today()
    await db.commit()
    await db.refresh(s)
    return s


@router.post("/{survey_id}/decline", response_model=SurveyRead)
async def decline_survey(survey_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """The homeowner declined to take part."""
    s = await get_tenant_scoped(db, Survey, user.tenant_id, survey_id)
    if s.status is SurveyStatus.RESPONDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This survey has already been answered.",
        )
    s.status = SurveyStatus.DECLINED
    await db.commit()
    await db.refresh(s)
    return s


@router.post("/{survey_id}/follow-up", response_model=SurveyRead)
async def follow_up_survey(
    survey_id: uuid.UUID, payload: SurveyFollowUp, db: DbSession, user: RequireStaff
):
    """Log the detractor follow-up: what was done, and who owns it."""
    s = await get_tenant_scoped(db, Survey, user.tenant_id, survey_id)
    if payload.follow_up_owner_id:
        await get_tenant_scoped(db, User, user.tenant_id, payload.follow_up_owner_id)
    s.follow_up_required = True
    s.follow_up_notes = payload.follow_up_notes
    s.follow_up_done = payload.follow_up_done
    s.follow_up_owner_id = payload.follow_up_owner_id
    await db.commit()
    await db.refresh(s)
    return s
