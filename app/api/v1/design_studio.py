"""The design studio: appointments, the options allowance, and signatures.

Three things happen in this room and they are not the same thing:

  * a buyer sits with a consultant and spends an allowance
  * structural choices get locked at a cutoff date, after which the answer changes
  * the resulting addendum goes out for signature and comes back, or does not

The API tier and nothing more. Rules live in `app.services.design_studio`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireSales, RequireStaff
from app.models.sales_ops import AppointmentStatus, SignatureStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.design_studio import (
    AllowanceSummary,
    AppointmentCreate,
    AppointmentRead,
    CutoffRequest,
    DeclineRequest,
    OutcomeRequest,
    RescheduleRequest,
    SetAllowance,
    SignatureCreate,
    SignatureRead,
    StudioSummary,
    VoidRequest,
)
from app.services.design_studio import DesignStudioService

router = APIRouter(tags=["design studio"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


# ================================================================ summary =====


@router.get("/studio/summary", response_model=StudioSummary)
async def studio_summary(db: DbSession, user: RequireStaff):
    """The counters above the studio, in one request rather than four."""
    return await DesignStudioService(db, user.tenant_id).summary()


# ============================================================== allowance =====


@router.get("/jobs/{job_id}/allowance", response_model=AllowanceSummary)
async def get_allowance(job_id: uuid.UUID, db: DbSession, user: RequireStaff):
    """What the buyer has to spend on options, and how much of it is gone.

    The number the whole studio conversation turns on. Pending choices count
    against it: a buyer who picked something in the room believes they have
    committed, and a budget that ignores it produces a bill that does not.
    """
    return await DesignStudioService(db, user.tenant_id).allowance(job_id)


@router.post("/jobs/{job_id}/allowance", response_model=AllowanceSummary)
async def set_allowance(
    job_id: uuid.UUID, payload: SetAllowance, db: DbSession, user: RequireSales
):
    """Agree the options allowance. It is part of the contract, so it lives there."""
    return await DesignStudioService(db, user.tenant_id).set_allowance(job_id, payload.amount)


# =========================================================== appointments =====


@router.get("/appointments", response_model=Page[AppointmentRead])
async def list_appointments(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    status_filter: Annotated[
        AppointmentStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
    consultant_id: Annotated[uuid.UUID | None, Query(description="One consultant.")] = None,
):
    filters = {
        k: v
        for k, v in {
            "status": status_filter,
            "job_id": job_id,
            "consultant_id": consultant_id,
        }.items()
        if v is not None
    }
    items, total = await DesignStudioService(db, user.tenant_id).list_appointments(
        params, **filters
    )
    return _page(items, params, total)


@router.post("/appointments", response_model=AppointmentRead, status_code=CREATED)
async def book_appointment(payload: AppointmentCreate, db: DbSession, user: RequireSales):
    """Put a studio session in the diary.

    Refused when the home already has a live booking - two open appointments on
    one house is a diary conflict, not a feature.
    """
    return await DesignStudioService(db, user.tenant_id).book(**payload.model_dump())


@router.get("/appointments/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(appointment_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await DesignStudioService(db, user.tenant_id).get_appointment(appointment_id)


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead)
async def reschedule(
    appointment_id: uuid.UUID, payload: RescheduleRequest, db: DbSession, user: RequireSales
):
    """Move a booking.

    The old one is cancelled and a new one created pointing back at it, rather
    than the date being edited. A buyer who has moved three times is worth being
    able to see.
    """
    return await DesignStudioService(db, user.tenant_id).reschedule(
        appointment_id, payload.scheduled_for, payload.note
    )


@router.post("/appointments/{appointment_id}/complete", response_model=AppointmentRead)
async def complete_appointment(
    appointment_id: uuid.UUID, payload: OutcomeRequest, db: DbSession, user: RequireSales
):
    """The session happened."""
    return await DesignStudioService(db, user.tenant_id).complete_appointment(
        appointment_id, payload.note
    )


@router.post("/appointments/{appointment_id}/no-show", response_model=AppointmentRead)
async def mark_no_show(
    appointment_id: uuid.UUID, payload: OutcomeRequest, db: DbSession, user: RequireSales
):
    """Nobody came.

    Its own ending rather than a cancellation: a cancellation is somebody telling
    you, a no-show is a wasted slot and a consultant sitting alone for three hours.
    """
    return await DesignStudioService(db, user.tenant_id).mark_no_show(
        appointment_id, payload.note
    )


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead)
async def cancel_appointment(
    appointment_id: uuid.UUID, payload: OutcomeRequest, db: DbSession, user: RequireSales
):
    return await DesignStudioService(db, user.tenant_id).cancel_appointment(
        appointment_id, payload.note
    )


@router.post("/appointments/{appointment_id}/cutoff", response_model=AppointmentRead)
async def set_cutoff(
    appointment_id: uuid.UUID, payload: CutoffRequest, db: DbSession, user: RequireSales
):
    """Set the date after which structural options are off the table.

    Enforced, not decorative: a structural option chosen after it is refused with
    an explanation rather than accepted and discovered on site.
    """
    return await DesignStudioService(db, user.tenant_id).set_cutoff(
        appointment_id, payload.structural_cutoff
    )


# ============================================================= signatures =====


@router.get("/signatures", response_model=Page[SignatureRead])
async def list_signatures(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    status_filter: Annotated[
        SignatureStatus | None, Query(alias="status", description="Exact status.")
    ] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    filters = {
        k: v for k, v in {"status": status_filter, "job_id": job_id}.items() if v is not None
    }
    items, total = await DesignStudioService(db, user.tenant_id).list_signatures(
        params, **filters
    )
    return _page(items, params, total)


@router.post("/signatures", response_model=SignatureRead, status_code=CREATED)
async def create_signature_request(
    payload: SignatureCreate, db: DbSession, user: RequireSales
):
    """Prepare a document for signature. Starts as a draft."""
    return await DesignStudioService(db, user.tenant_id).create_signature_request(
        **payload.model_dump()
    )


@router.post("/signatures/{request_id}/send", response_model=SignatureRead)
async def send_for_signature(request_id: uuid.UUID, db: DbSession, user: RequireSales):
    return await DesignStudioService(db, user.tenant_id).send_for_signature(request_id)


@router.post("/signatures/{request_id}/remind", response_model=SignatureRead)
async def remind(request_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Chase an unsigned document, and count the chase.

    Counted so "we only sent it once" cannot be claimed when the record says
    otherwise - which matters when an addendum holding up a frame has been sitting
    for three weeks.
    """
    return await DesignStudioService(db, user.tenant_id).remind(request_id)


@router.post("/signatures/{request_id}/viewed", response_model=SignatureRead)
async def mark_viewed(request_id: uuid.UUID, db: DbSession, user: RequireSales):
    """The signer opened it. Normally written by a provider webhook."""
    return await DesignStudioService(db, user.tenant_id).mark_viewed(request_id)


@router.post("/signatures/{request_id}/mark-signed", response_model=SignatureRead)
async def mark_signed(request_id: uuid.UUID, db: DbSession, user: RequireSales):
    """Record that the document came back signed.

    Worth being plain about: this asserts signing happened, it does not make it
    binding. That takes a provider such as DocuSign, which is what `provider` and
    `external_id` are for.
    """
    return await DesignStudioService(db, user.tenant_id).mark_signed(request_id)


@router.post("/signatures/{request_id}/decline", response_model=SignatureRead)
async def decline(
    request_id: uuid.UUID, payload: DeclineRequest, db: DbSession, user: RequireSales
):
    """The buyer refused to sign, and why.

    The reason is the most valuable field on this record: a declined addendum
    means the selections behind it are wrong, and somebody has to find out how.
    """
    return await DesignStudioService(db, user.tenant_id).decline(request_id, payload.reason)


@router.post("/signatures/{request_id}/void", response_model=SignatureRead)
async def void(request_id: uuid.UUID, payload: VoidRequest, db: DbSession, user: RequireSales):
    """Withdraw a document sent in error or superseded by another."""
    return await DesignStudioService(db, user.tenant_id).void(request_id, payload.reason)
