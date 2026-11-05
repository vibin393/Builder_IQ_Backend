"""Estimates (proposal builder), client updates, and the per-job activity feed."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, RequireStaff
from app.models.estimate import EstimateStatus
from app.schemas.estimate import (
    ActivityEvent,
    ClientUpdateCreate,
    ClientUpdateEdit,
    ClientUpdateRead,
    EstimateCreate,
    EstimateItemCreate,
    EstimateItemUpdate,
    EstimateListRow,
    EstimateRead,
    EstimateUpdate,
)
from app.services.estimate import EstimateService

router = APIRouter(tags=["estimates"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


# ---- estimates ----
@router.get("/estimates", response_model=list[EstimateListRow])
async def list_estimates(
    db: DbSession, user: RequireStaff, job_id: Annotated[uuid.UUID | None, Query()] = None
):
    return await EstimateService(db, user.tenant_id).list(job_id)


@router.get("/estimates/{estimate_id}", response_model=EstimateRead)
async def get_estimate(estimate_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).get(estimate_id)


@router.post("/estimates", response_model=EstimateRead, status_code=CREATED)
async def create_estimate(payload: EstimateCreate, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).create(**payload.model_dump())


@router.patch("/estimates/{estimate_id}", response_model=EstimateRead)
async def update_estimate(
    estimate_id: uuid.UUID, payload: EstimateUpdate, db: DbSession, user: RequireStaff
):
    return await EstimateService(db, user.tenant_id).update(
        estimate_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/estimates/{estimate_id}", status_code=NO_CONTENT)
async def delete_estimate(estimate_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await EstimateService(db, user.tenant_id).delete(estimate_id)


@router.post("/estimates/{estimate_id}/send", response_model=EstimateRead)
async def create_proposal(estimate_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """Issue the estimate to the owner as a proposal."""
    return await EstimateService(db, user.tenant_id).set_status(estimate_id, EstimateStatus.SENT)


@router.post("/estimates/{estimate_id}/accept", response_model=EstimateRead)
async def accept_estimate(estimate_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).set_status(
        estimate_id, EstimateStatus.ACCEPTED
    )


@router.post("/estimates/{estimate_id}/decline", response_model=EstimateRead)
async def decline_estimate(estimate_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).set_status(
        estimate_id, EstimateStatus.DECLINED
    )


@router.post("/estimates/{estimate_id}/items", response_model=EstimateRead, status_code=CREATED)
async def add_item(
    estimate_id: uuid.UUID, payload: EstimateItemCreate, db: DbSession, user: RequireStaff
):
    return await EstimateService(db, user.tenant_id).add_item(estimate_id, **payload.model_dump())


@router.patch("/estimate-items/{item_id}", response_model=EstimateRead)
async def update_item(
    item_id: uuid.UUID, payload: EstimateItemUpdate, db: DbSession, user: RequireStaff
):
    return await EstimateService(db, user.tenant_id).update_item(
        item_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/estimate-items/{item_id}", response_model=EstimateRead)
async def delete_item(item_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).delete_item(item_id)


# ---- client updates ----
@router.get("/client-updates", response_model=list[ClientUpdateRead])
async def list_client_updates(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID, Query(description="One home (required).")],
    published_only: Annotated[bool, Query()] = False,
):
    return await EstimateService(db, user.tenant_id).list_updates(
        job_id, published_only=published_only
    )


@router.post("/client-updates", response_model=ClientUpdateRead, status_code=CREATED)
async def post_client_update(payload: ClientUpdateCreate, db: DbSession, user: RequireStaff):
    return await EstimateService(db, user.tenant_id).post_update(
        posted_by_id=user.id, **payload.model_dump()
    )


@router.patch("/client-updates/{update_id}", response_model=ClientUpdateRead)
async def edit_client_update(
    update_id: uuid.UUID, payload: ClientUpdateEdit, db: DbSession, user: RequireStaff
):
    return await EstimateService(db, user.tenant_id).edit_update(
        update_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/client-updates/{update_id}", status_code=NO_CONTENT)
async def delete_client_update(update_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await EstimateService(db, user.tenant_id).delete_update(update_id)


# ---- activity feed ----
@router.get("/activity", response_model=list[ActivityEvent])
async def activity_feed(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID, Query(description="One home (required).")],
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
):
    """A merged timeline of what has happened on the job, newest first."""
    return await EstimateService(db, user.tenant_id).activity(job_id, limit=limit)
