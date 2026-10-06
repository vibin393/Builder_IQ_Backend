"""Configuration catalog: cost codes, trades, communities, lots, plans, options.

This is what a builder sets up before any home can be sold. It is per-tenant by
definition, since every builder's cost codes and option catalog are their own.

The API tier and nothing more. Each handler does four things - read the request,
check the role, call the service, shape the response - and no more than that.
There is no SQL in this file, and no business rule: those live in
`app.services.catalog` and `app.repositories.catalog`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import DbSession, RequireAdmin, RequireStaff
from app.schemas.catalog import (
    CostCodeCreate,
    CostCodeRead,
    CostCodeUpdate,
    VendorCreate,
    VendorRead,
    VendorUpdate,
)
from app.schemas.common import ArchiveRequest, Page, PageParams, page_params
from app.services.catalog import CatalogService

router = APIRouter(tags=["catalog"])

#: Paging and free-text search, declared once and reused by every list.
PageQuery = Annotated[PageParams, Depends(page_params)]

CREATED = status.HTTP_201_CREATED


# --- Cost codes ---------------------------------------------------------------


@router.get("/cost-codes", response_model=list[CostCodeRead])
async def list_cost_codes(db: DbSession, user: RequireStaff):
    """Every cost code, unpaged.

    Kept unpaged because callers use it to populate pickers, and a picker
    missing half its options is worse than a slow one. Use /cost-codes/search
    when you want paging and free text.
    """
    return await CatalogService(db, user.tenant_id).list_cost_codes()


@router.get("/cost-codes/search", response_model=Page[CostCodeRead])
async def search_cost_codes(db: DbSession, user: RequireStaff, params: PageQuery):
    items, total = await CatalogService(db, user.tenant_id).list_cost_codes(params)
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.post("/cost-codes", response_model=CostCodeRead, status_code=CREATED)
async def create_cost_code(payload: CostCodeCreate, db: DbSession, admin: RequireAdmin):
    return await CatalogService(db, admin.tenant_id).create_cost_code(
        actor_id=admin.id, **payload.model_dump()
    )


@router.get("/cost-codes/{code_id}", response_model=CostCodeRead)
async def get_cost_code(code_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await CatalogService(db, user.tenant_id).get_cost_code(code_id)


@router.patch("/cost-codes/{code_id}", response_model=CostCodeRead)
async def update_cost_code(
    code_id: uuid.UUID, payload: CostCodeUpdate, db: DbSession, admin: RequireAdmin
):
    """Correct a cost code. Only the fields supplied are changed."""
    return await CatalogService(db, admin.tenant_id).update_cost_code(
        code_id, actor_id=admin.id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/cost-codes/{code_id}/archive", response_model=CostCodeRead)
async def archive_cost_code(
    code_id: uuid.UUID, payload: ArchiveRequest, db: DbSession, admin: RequireAdmin
):
    """Retire a cost code so it stops appearing in pickers.

    Refused while budget lines or purchase order lines still point at it -
    hiding a code that money is still landing on is the worst of both.
    """
    return await CatalogService(db, admin.tenant_id).archive_cost_code(
        code_id, admin.id, payload.reason
    )


@router.post("/cost-codes/{code_id}/restore", response_model=CostCodeRead)
async def restore_cost_code(code_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    return await CatalogService(db, admin.tenant_id).restore_cost_code(code_id)


# --- Trades -------------------------------------------------------------------


@router.get("/vendors", response_model=list[VendorRead])
async def list_vendors(db: DbSession, user: RequireStaff):
    """Every active trade, unpaged, for pickers."""
    return await CatalogService(db, user.tenant_id).list_vendors()


# The paged /vendors/search was never called; the trades directory in vendors.py
# searches, filters by specialism and carries compliance and money.


@router.post("/vendors", response_model=VendorRead, status_code=CREATED)
async def create_vendor(payload: VendorCreate, db: DbSession, admin: RequireAdmin):
    return await CatalogService(db, admin.tenant_id).create_vendor(
        actor_id=admin.id, **payload.model_dump()
    )


@router.get("/vendors/{vendor_id}", response_model=VendorRead)
async def get_vendor(vendor_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await CatalogService(db, user.tenant_id).get_vendor(vendor_id)


@router.patch("/vendors/{vendor_id}", response_model=VendorRead)
async def update_vendor(
    vendor_id: uuid.UUID, payload: VendorUpdate, db: DbSession, admin: RequireAdmin
):
    """Correct a trade's details. Contact people change; the record should too."""
    return await CatalogService(db, admin.tenant_id).update_vendor(
        vendor_id, actor_id=admin.id, **payload.model_dump(exclude_unset=True)
    )


@router.post("/vendors/{vendor_id}/archive", response_model=VendorRead)
async def archive_vendor(
    vendor_id: uuid.UUID, payload: ArchiveRequest, db: DbSession, admin: RequireAdmin
):
    """Stop offering this trade in pickers, keeping every record they hold.

    Refused while they still hold open purchase orders - archiving a trade
    mid-job hides them from the people who need to chase them.
    """
    return await CatalogService(db, admin.tenant_id).archive_vendor(
        vendor_id, admin.id, payload.reason
    )


@router.post("/vendors/{vendor_id}/restore", response_model=VendorRead)
async def restore_vendor(vendor_id: uuid.UUID, db: DbSession, admin: RequireAdmin):
    return await CatalogService(db, admin.tenant_id).restore_vendor(vendor_id)


# Communities, phases and plots moved to app/api/v1/communities.py.


# Plans, elevations and standard costs moved to app/api/v1/plans.py.


# The option catalog moved to app/api/v1/options.py.
