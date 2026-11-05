"""Safety and quality: observations, incidents and action plans.

Four rules worth knowing before reading the routes:

  * an observation reaches `closed` only through `ready_for_review`, so every
    closed one has been looked at by somebody other than whoever did the work
  * a failed inspection raises one automatically, which is what turns an
    inspection from a record that a problem existed into a record of what was
    done about it
  * an incident carries a RIDDOR category and therefore a deadline, and closing a
    reportable one the HSE has never been told about is refused
  * action plan steps run in order, and a step marked `requires_signoff` must be
    witnessed by somebody other than whoever completed it

The API tier and nothing more. Rules live in `app.services.safety`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireStaff
from app.models.safety import (
    ActionPlanStatus,
    IncidentType,
    ObservationStatus,
    ObservationType,
    RiddorCategory,
    Severity,
)
from app.schemas.common import Page, PageParams, page_params
from app.schemas.safety import (
    ActionPlanCreate,
    ActionPlanItemCreate,
    ActionPlanRead,
    AssignObservation,
    ClassifyIncident,
    CompleteStep,
    HseReport,
    IncidentCreate,
    IncidentRead,
    ObservationCreate,
    ObservationRead,
    ObservationUpdate,
    ReasonBody,
    ResolutionBody,
    RootCause,
    SafetySummary,
    StartFromTemplate,
)
from app.services.safety import SafetyService, suggest_riddor

router = APIRouter(tags=["safety"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/safety/summary", response_model=SafetySummary)
async def safety_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """The numbers above the list.

    Counted over everything rather than over the page on screen, because "two open
    observations" meaning "two on this page" is the kind of number people act on.
    """
    return await SafetyService(db, user.tenant_id).summary(job_id)


# ==================================================== observations =====


@router.get("/observations", response_model=Page[ObservationRead])
async def list_observations(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    status_filter: Annotated[
        ObservationStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    observation_type: Annotated[ObservationType | None, Query()] = None,
    severity: Annotated[Severity | None, Query()] = None,
    assigned_vendor_id: Annotated[uuid.UUID | None, Query(description="One trade.")] = None,
):
    filters = {
        k: v
        for k, v in {
            "job_id": job_id,
            "status": status_filter,
            "observation_type": observation_type,
            "severity": severity,
            "assigned_vendor_id": assigned_vendor_id,
        }.items()
        if v is not None
    }
    items, total = await SafetyService(db, user.tenant_id).list_observations(params, **filters)
    return _page(items, params, total)


@router.get("/observations/open", response_model=list[ObservationRead])
async def open_observations(
    job_id: Annotated[uuid.UUID, Query(description="The home.")],
    db: DbSession,
    user: RequireStaff,
):
    """Everything still owed on one home, unpaged.

    Ready-for-review is included: a claimed fix nobody has looked at is not a
    closed one.
    """
    svc = SafetyService(db, user.tenant_id)
    await svc.jobs.require_exists(job_id)
    return await svc.observations.open_for_job(job_id)


@router.post("/observations", response_model=ObservationRead, status_code=CREATED)
async def raise_observation(payload: ObservationCreate, db: DbSession, user: RequireStaff):
    """Raise a corrective action.

    Passing `inspection_id` links it to the inspection that failed, so the
    inspection record shows what was done about it rather than just that it failed.
    """
    return await SafetyService(db, user.tenant_id).raise_observation(
        actor_id=user.id, **payload.model_dump()
    )


@router.patch("/observations/{obs_id}", response_model=ObservationRead)
async def update_observation(
    obs_id: uuid.UUID, payload: ObservationUpdate, db: DbSession, user: RequireStaff
):
    """Correct the details. Status is not settable here - the routes below move it."""
    return await SafetyService(db, user.tenant_id).update_observation(
        obs_id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/observations/{obs_id}/assign", response_model=ObservationRead)
async def assign_observation(
    obs_id: uuid.UUID, payload: AssignObservation, db: DbSession, user: RequireStaff
):
    """Give it to a trade against a date."""
    return await SafetyService(db, user.tenant_id).assign_observation(
        obs_id, payload.vendor_id, payload.due_on
    )


@router.post("/observations/{obs_id}/submit", response_model=ObservationRead)
async def submit_observation(
    obs_id: uuid.UUID, payload: ResolutionBody, db: DbSession, user: RequireStaff
):
    """The fix has been made - a claim, not a conclusion.

    The resolution is required here rather than at closing, because the person who
    did the work knows what they did. Asking the reviewer produces "fixed".
    """
    return await SafetyService(db, user.tenant_id).submit_observation(obs_id, payload.resolution)


@router.post("/observations/{obs_id}/close", response_model=ObservationRead)
async def close_observation(obs_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Accept the fix. The only way an observation reaches closed."""
    return await SafetyService(db, user.tenant_id).close_observation(obs_id, actor_id=user.id)


@router.post("/observations/{obs_id}/reject", response_model=ObservationRead)
async def reject_observation(
    obs_id: uuid.UUID, payload: ReasonBody, db: DbSession, user: RequireStaff
):
    """The fix was inspected and failed. Back to the trade, with why."""
    return await SafetyService(db, user.tenant_id).reject_observation(obs_id, payload.reason)


@router.post("/observations/{obs_id}/reopen", response_model=ObservationRead)
async def reopen_observation(
    obs_id: uuid.UUID, payload: ReasonBody, db: DbSession, user: RequireStaff
):
    """Put a closed observation back on the list."""
    return await SafetyService(db, user.tenant_id).reopen_observation(obs_id, payload.reason)


# ======================================================= incidents =====


@router.get("/incidents", response_model=Page[IncidentRead])
async def list_incidents(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    incident_type: Annotated[IncidentType | None, Query()] = None,
    riddor_category: Annotated[RiddorCategory | None, Query()] = None,
):
    filters = {
        k: v
        for k, v in {
            "job_id": job_id,
            "incident_type": incident_type,
            "riddor_category": riddor_category,
        }.items()
        if v is not None
    }
    items, total = await SafetyService(db, user.tenant_id).list_incidents(params, **filters)
    return _page(items, params, total)


@router.get("/incidents/awaiting-report", response_model=list[IncidentRead])
async def awaiting_hse_report(db: DbSession, user: RequireStaff):
    """Reportable incidents the HSE has not been told about.

    The list with a legal deadline on it, which is why it is its own route rather
    than a filter somebody has to know to apply.
    """
    return await SafetyService(db, user.tenant_id).incidents.awaiting_report()


@router.post("/incidents", response_model=IncidentRead, status_code=CREATED)
async def report_incident(payload: IncidentCreate, db: DbSession, user: RequireStaff):
    """Record what happened.

    The RIDDOR category is suggested from the facts unless one is stated. Leaving
    the question unasked is how an incident that had to reach the HSE within ten
    days sits in a list for a month.
    """
    return await SafetyService(db, user.tenant_id).report_incident(
        actor_id=user.id, **payload.model_dump()
    )


@router.get("/incidents/riddor-suggestion", response_model=RiddorCategory)
async def riddor_suggestion(
    incident_type: Annotated[IncidentType, Query()],
    severity: Annotated[Severity, Query()] = Severity.MEDIUM,
    lost_time: Annotated[bool, Query()] = False,
    medical_treatment: Annotated[bool, Query()] = False,
):
    """What category this looks like, before it is filed.

    Lets the form say what it is about to decide rather than deciding it silently,
    which matters because the operator can and should override it.
    """
    return suggest_riddor(
        incident_type=incident_type,
        lost_time=lost_time,
        medical_treatment=medical_treatment,
        severity=severity,
    )


@router.post("/incidents/{incident_id}/classify", response_model=IncidentRead)
async def classify_incident(
    incident_id: uuid.UUID, payload: ClassifyIncident, db: DbSession, user: RequireStaff
):
    """Set the RIDDOR category, which sets the deadline with it."""
    return await SafetyService(db, user.tenant_id).classify_incident(
        incident_id, payload.riddor_category
    )


@router.post("/incidents/{incident_id}/hse-report", response_model=IncidentRead)
async def record_hse_report(
    incident_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: HseReport | None = None,
):
    """Record that the report has actually gone in, with its reference."""
    body = payload or HseReport()
    return await SafetyService(db, user.tenant_id).record_hse_report(
        incident_id, reported_on=body.reported_on, reference=body.reference
    )


@router.post("/incidents/{incident_id}/root-cause", response_model=IncidentRead)
async def investigate_incident(
    incident_id: uuid.UUID, payload: RootCause, db: DbSession, user: RequireStaff
):
    """Record what actually caused it."""
    return await SafetyService(db, user.tenant_id).investigate_incident(
        incident_id, payload.root_cause
    )


@router.post("/incidents/{incident_id}/close", response_model=IncidentRead)
async def close_incident(incident_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Close the investigation.

    Refused while a reportable incident has not been reported to the HSE, and
    refused with no root cause - an investigation that concluded nothing leaves
    nothing to act on.
    """
    return await SafetyService(db, user.tenant_id).close_incident(incident_id)


# ==================================================== action plans =====


@router.get("/action-plans", response_model=Page[ActionPlanRead])
async def list_action_plans(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    templates: Annotated[bool, Query(description="Patterns rather than live plans.")] = False,
    status_filter: Annotated[ActionPlanStatus | None, Query(alias="status")] = None,
):
    filters = {"is_template": templates}
    if job_id is not None:
        filters["job_id"] = job_id
    if status_filter is not None:
        filters["status"] = status_filter
    items, total = await SafetyService(db, user.tenant_id).list_plans(params, **filters)
    return _page(items, params, total)


@router.post("/action-plans", response_model=ActionPlanRead, status_code=CREATED)
async def create_action_plan(payload: ActionPlanCreate, db: DbSession, user: RequireStaff):
    """Create a plan, or a template to start plans from."""
    return await SafetyService(db, user.tenant_id).create_plan(
        actor_id=user.id, **payload.model_dump()
    )


@router.post("/action-plans/{plan_id}/items", response_model=ActionPlanRead)
async def add_action_item(
    plan_id: uuid.UUID, payload: ActionPlanItemCreate, db: DbSession, user: RequireStaff
):
    return await SafetyService(db, user.tenant_id).add_step(plan_id, **payload.model_dump())


@router.post("/action-plans/{plan_id}/start", response_model=ActionPlanRead, status_code=CREATED)
async def start_from_template(
    plan_id: uuid.UUID, payload: StartFromTemplate, db: DbSession, user: RequireStaff
):
    """Copy this template onto a home and start it.

    The steps are copied rather than referenced: a template edited next month must
    not rewrite the checklist somebody signed last month.
    """
    return await SafetyService(db, user.tenant_id).start_from_template(
        plan_id, payload.job_id, actor_id=user.id, due_on=payload.due_on
    )


@router.post("/action-plans/{plan_id}/items/{item_id}/complete", response_model=ActionPlanRead)
async def complete_action_item(
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    db: DbSession,
    user: RequireStaff,
    payload: CompleteStep | None = None,
):
    """Mark a step done. These run in order, so it must be the next one."""
    return await SafetyService(db, user.tenant_id).complete_step(
        plan_id, item_id, actor_id=user.id, notes=(payload or CompleteStep()).notes
    )


@router.post("/action-plans/{plan_id}/items/{item_id}/sign-off", response_model=ActionPlanRead)
async def sign_off_action_item(
    plan_id: uuid.UUID, item_id: uuid.UUID, db: DbSession, user: RequireStaff
):
    """Witness a step somebody else completed.

    Refused if you completed it yourself. The point of a sign-off step is a second
    pair of eyes.
    """
    return await SafetyService(db, user.tenant_id).sign_off_step(plan_id, item_id, actor_id=user.id)
