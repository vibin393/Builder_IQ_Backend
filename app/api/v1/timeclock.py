"""Time clock: employee shifts, the live board, and the locator map.

Clock in opens a shift, clock out closes it, and a worker may have only one open at
a time. The map and the "on the clock" board read the same shifts from the presence
side; timecards stay the money side.

The API tier only. Rules live in `app.services.timeclock`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, RequireStaff
from app.models.timeclock import ShiftStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.timeclock import (
    ClockIn,
    ClockOut,
    ShiftCreate,
    ShiftMapPoint,
    ShiftRead,
    ShiftUpdate,
    TimeClockSummary,
)
from app.services.timeclock import TimeClockService

router = APIRouter(prefix="/shifts", tags=["time clock"])

PageQuery = Annotated[PageParams, Depends(page_params)]
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


def _page(items, params: PageParams, total: int) -> Page:
    return Page(items=items, total=total, page=params.page, size=params.size)


@router.get("/summary", response_model=TimeClockSummary)
async def timeclock_summary(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query(description="One home.")] = None,
):
    """On the clock now, workers and hours today, pending approvals, out of bounds."""
    data = await TimeClockService(db, user.tenant_id).summary(job_id)
    return TimeClockSummary(**data)


@router.get("/active", response_model=list[ShiftRead])
async def on_the_clock(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """Who is clocked in right now."""
    return await TimeClockService(db, user.tenant_id).active(job_id)


@router.get("/map", response_model=list[ShiftMapPoint])
async def locator_map(
    db: DbSession,
    user: RequireStaff,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """Pins for the employee locator — active shifts carrying a clock-in location."""
    shifts = await TimeClockService(db, user.tenant_id).map_points(job_id)
    return [
        ShiftMapPoint(
            id=s.id,
            job_id=s.job_id,
            worker_name=s.worker_name,
            status=s.status,
            lat=s.clock_in_lat,
            lng=s.clock_in_lng,
            clock_in_at=s.clock_in_at,
            out_of_bounds=s.out_of_bounds,
        )
        for s in shifts
    ]


@router.get("", response_model=Page[ShiftRead])
async def list_shifts(
    db: DbSession,
    user: RequireStaff,
    params: PageQuery,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    shift_status: Annotated[ShiftStatus | None, Query()] = None,
):
    filters = {k: v for k, v in {"job_id": job_id, "status": shift_status}.items() if v is not None}
    items, total = await TimeClockService(db, user.tenant_id).list_shifts(params, **filters)
    return _page(items, params, total)


@router.post("/clock-in", response_model=ShiftRead, status_code=CREATED)
async def clock_in(payload: ClockIn, db: DbSession, user: RequireStaff):
    """Start a shift now. Refused if the worker already has one open."""
    return await TimeClockService(db, user.tenant_id).clock_in(
        actor_id=user.id, **payload.model_dump()
    )


@router.post("/{shift_id}/clock-out", response_model=ShiftRead)
async def clock_out(shift_id: uuid.UUID, payload: ClockOut, db: DbSession, user: RequireStaff):
    """Close an open shift now."""
    return await TimeClockService(db, user.tenant_id).clock_out(
        shift_id, lat=payload.lat, lng=payload.lng
    )


@router.post("", response_model=ShiftRead, status_code=CREATED)
async def create_shift(payload: ShiftCreate, db: DbSession, user: RequireStaff):
    """Enter a shift by hand, both ends known."""
    return await TimeClockService(db, user.tenant_id).create_manual(
        actor_id=user.id, **payload.model_dump()
    )


@router.patch("/{shift_id}", response_model=ShiftRead)
async def amend_shift(shift_id: uuid.UUID, payload: ShiftUpdate, db: DbSession, user: RequireStaff):
    """Correct a shift. Refused once approved."""
    return await TimeClockService(db, user.tenant_id).amend(
        shift_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/{shift_id}", status_code=NO_CONTENT)
async def delete_shift(shift_id: uuid.UUID, db: DbSession, user: RequireStaff):
    await TimeClockService(db, user.tenant_id).delete(shift_id)


@router.post("/{shift_id}/approve", response_model=ShiftRead)
async def approve_shift(shift_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await TimeClockService(db, user.tenant_id).approve(shift_id, actor_id=user.id)


@router.post("/{shift_id}/unapprove", response_model=ShiftRead)
async def unapprove_shift(shift_id: uuid.UUID, db: DbSession, user: RequireStaff):
    return await TimeClockService(db, user.tenant_id).unapprove(shift_id)
