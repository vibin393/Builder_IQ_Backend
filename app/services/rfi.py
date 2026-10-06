"""RFI business rules: the response clock and the aging buckets.

These are policy the builder will want to tune — how quickly a question should be
answered, and where the line falls between "getting old" and "late" — so they live
here rather than being scattered as literals through the router.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.communication import Priority, RFIStatus
from app.models.rfi import RFIActivity, RFIActivityKind

# --- SLA: how quickly a question should be answered, by priority ---------------
#
# An urgent RFI usually stops work, so it is a two-day promise. The rest step down.
# These set the default due date when the person raising it does not pick one.
RESPONSE_DAYS: dict[Priority, int] = {
    Priority.URGENT: 2,
    Priority.HIGH: 5,
    Priority.NORMAL: 10,
    Priority.LOW: 15,
}


def response_days(priority: Priority) -> int:
    return RESPONSE_DAYS.get(priority, RESPONSE_DAYS[Priority.NORMAL])


def due_date_for(raised: date, priority: Priority) -> date:
    """The date the answer is expected by, if none was set explicitly."""
    return raised + timedelta(days=response_days(priority))


# --- Aging: how the open queue is bucketed for the summary ---------------------
#
# The point of the buckets is triage: an RFI at 20 days open is a different problem
# from one at 2, even if neither is technically overdue against its due date.
AGING_BUCKETS = ("0-3", "4-7", "8-14", "15+")


def aging_bucket(days_open: int | None) -> str:
    if days_open is None:
        return "0-3"
    if days_open <= 3:
        return "0-3"
    if days_open <= 7:
        return "4-7"
    if days_open <= 14:
        return "8-14"
    return "15+"


def log(
    tenant_id: uuid.UUID,
    rfi_id: uuid.UUID,
    kind: RFIActivityKind,
    detail: str | None = None,
    author_id: uuid.UUID | None = None,
) -> RFIActivity:
    """Build one activity-log row. The caller adds it to the session.

    Kept as a plain factory rather than a session-touching helper so the router
    controls exactly one commit per request.
    """
    return RFIActivity(
        tenant_id=tenant_id,
        rfi_id=rfi_id,
        kind=kind,
        detail=detail,
        author_id=author_id,
    )


#: Statuses from which an RFI can still be answered or worked.
ANSWERABLE = (RFIStatus.OPEN, RFIStatus.DRAFT)
