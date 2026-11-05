"""Cost inbox: the staging queue for incoming costs before they become bills."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequirePurchasing, RequireStaff
from app.models.cost_inbox import CostInboxStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.cost_inbox import (
    CostInboxCreate,
    CostInboxRead,
    CostInboxSummary,
    CostInboxUpdate,
)
from app.services.cost_inbox import CostInboxService

router = APIRouter(prefix="/cost-inbox", tags=["cost inbox"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


@router.get("/summary", response_model=CostInboxSummary)
async def cost_inbox_summary(db: DbSession, user: RequireStaff):
    data = await CostInboxService(db, user.tenant_id).summary()
    return CostInboxSummary(**data)


@router.get("", response_model=Page[CostInboxRead])
async def list_cost_inbox(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    item_status: Annotated[CostInboxStatus | None, Query()] = None,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
):
    items, total = await CostInboxService(db, user.tenant_id).list(
        params, status=item_status, job_id=job_id
    )
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.post("", response_model=CostInboxRead, status_code=CREATED)
async def capture_cost(payload: CostInboxCreate, db: DbSession, user: RequirePurchasing):
    return await CostInboxService(db, user.tenant_id).create(**payload.model_dump())


@router.patch("/{item_id}", response_model=CostInboxRead)
async def code_cost(
    item_id: uuid.UUID, payload: CostInboxUpdate, db: DbSession, user: RequirePurchasing
):
    """Code an item — assign a job, vendor and cost code, or edit its details."""
    return await CostInboxService(db, user.tenant_id).update(
        item_id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/{item_id}/push", response_model=CostInboxRead)
async def push_cost(item_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    """Push a coded item out of the inbox to a bill."""
    return await CostInboxService(db, user.tenant_id).push(item_id)


@router.post("/{item_id}/reject", response_model=CostInboxRead)
async def reject_cost(item_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    return await CostInboxService(db, user.tenant_id).reject(item_id)


@router.post("/{item_id}/reopen", response_model=CostInboxRead)
async def reopen_cost(item_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    return await CostInboxService(db, user.tenant_id).reopen(item_id)


@router.delete("/{item_id}", status_code=NO_CONTENT)
async def delete_cost(item_id: uuid.UUID, db: DbSession, user: RequirePurchasing):
    await CostInboxService(db, user.tenant_id).delete(item_id)
