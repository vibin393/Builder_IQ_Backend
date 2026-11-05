"""Owner (client) invoicing — the receivables side.

Deposits, progress draws and the final payment that make up a job's payment
schedule, plus the payments recorded against them.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireAccounting, RequireStaff
from app.models.owner_invoice import OwnerInvoiceStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.owner_invoice import (
    GenerateSchedule,
    InvoiceLineCreate,
    InvoiceLineUpdate,
    OwnerInvoiceCreate,
    OwnerInvoiceRead,
    OwnerInvoiceSummary,
    OwnerInvoiceUpdate,
    PaymentCreate,
)
from app.services.owner_invoice import OwnerInvoiceService

router = APIRouter(prefix="/client-invoices", tags=["owner invoices"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


@router.get("/summary", response_model=OwnerInvoiceSummary)
async def owner_invoice_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """Invoiced, paid and outstanding totals, plus draft and overdue counts."""
    data = await OwnerInvoiceService(db, user.tenant_id).summary(job_id)
    return OwnerInvoiceSummary(**data)


@router.get("", response_model=Page[OwnerInvoiceRead])
async def list_owner_invoices(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    invoice_status: Annotated[OwnerInvoiceStatus | None, Query()] = None,
):
    items, total = await OwnerInvoiceService(db, user.tenant_id).list(
        params, job_id=job_id, status=invoice_status
    )
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/{invoice_id}", response_model=OwnerInvoiceRead)
async def get_owner_invoice(invoice_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await OwnerInvoiceService(db, user.tenant_id).get(invoice_id)


@router.post("", response_model=OwnerInvoiceRead, status_code=CREATED)
async def create_owner_invoice(payload: OwnerInvoiceCreate, db: DbSession, user: RequireAccounting):
    return await OwnerInvoiceService(db, user.tenant_id).create(**payload.model_dump())


@router.post("/generate-schedule", response_model=list[OwnerInvoiceRead], status_code=CREATED)
async def generate_payment_schedule(
    payload: GenerateSchedule, db: DbSession, user: RequireAccounting
):
    """Create a draft deposit → progress draws → final payment schedule in one step."""
    return await OwnerInvoiceService(db, user.tenant_id).generate_schedule(**payload.model_dump())


@router.patch("/{invoice_id}", response_model=OwnerInvoiceRead)
async def update_owner_invoice(
    invoice_id: uuid.UUID, payload: OwnerInvoiceUpdate, db: DbSession, user: RequireAccounting
):
    return await OwnerInvoiceService(db, user.tenant_id).update(
        invoice_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/{invoice_id}", status_code=NO_CONTENT)
async def delete_owner_invoice(invoice_id: uuid.UUID, db: DbSession, user: RequireAccounting):
    await OwnerInvoiceService(db, user.tenant_id).delete(invoice_id)


@router.post("/{invoice_id}/send", response_model=OwnerInvoiceRead)
async def send_owner_invoice(invoice_id: uuid.UUID, db: DbSession, user: RequireAccounting):
    return await OwnerInvoiceService(db, user.tenant_id).send(invoice_id)


@router.post("/{invoice_id}/viewed", response_model=OwnerInvoiceRead)
async def mark_viewed(invoice_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await OwnerInvoiceService(db, user.tenant_id).mark_viewed(invoice_id)


@router.post("/{invoice_id}/void", response_model=OwnerInvoiceRead)
async def void_owner_invoice(invoice_id: uuid.UUID, db: DbSession, user: RequireAccounting):
    return await OwnerInvoiceService(db, user.tenant_id).void(invoice_id)


@router.post("/{invoice_id}/payments", response_model=OwnerInvoiceRead, status_code=CREATED)
async def record_payment(
    invoice_id: uuid.UUID, payload: PaymentCreate, db: DbSession, user: RequireAccounting
):
    """Record a payment and move the balance. Repeats until the invoice is paid."""
    return await OwnerInvoiceService(db, user.tenant_id).record_payment(
        invoice_id, **payload.model_dump()
    )


# ---- line items ----
@router.post("/{invoice_id}/lines", response_model=OwnerInvoiceRead, status_code=CREATED)
async def add_invoice_line(
    invoice_id: uuid.UUID, payload: InvoiceLineCreate, db: DbSession, user: RequireAccounting
):
    return await OwnerInvoiceService(db, user.tenant_id).add_line(
        invoice_id, **payload.model_dump()
    )


@router.patch("/lines/{line_id}", response_model=OwnerInvoiceRead)
async def update_invoice_line(
    line_id: uuid.UUID, payload: InvoiceLineUpdate, db: DbSession, user: RequireAccounting
):
    return await OwnerInvoiceService(db, user.tenant_id).update_line(
        line_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/lines/{line_id}", response_model=OwnerInvoiceRead)
async def delete_invoice_line(line_id: uuid.UUID, db: DbSession, user: RequireAccounting):
    return await OwnerInvoiceService(db, user.tenant_id).delete_line(line_id)
