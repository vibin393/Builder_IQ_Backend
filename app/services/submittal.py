"""Submittal business rules: the review clock and procurement runway.

The one rule worth pulling out of the router is procurement: working backwards from
the date a product is needed on site, through its lead time and the review it still
needs, to whether there is enough runway left. That is the number the register exists
to surface, and the builder will want to tune the allowance, so it lives here.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.communication import (
    SUBMITTAL_REVIEW_ALLOWANCE_DAYS,
    Priority,
    SubmittalStatus,
)
from app.models.submittal import SubmittalActivity, SubmittalActivityKind

# --- Review turnaround target, by priority ------------------------------------
#
# The due date set on a submittal when none is given: how long the reviewer has
# from the day it lands. Urgent items (something already on the schedule's critical
# path) get a tighter promise.
REVIEW_DAYS: dict[Priority, int] = {
    Priority.URGENT: 3,
    Priority.HIGH: 7,
    Priority.NORMAL: 10,
    Priority.LOW: 14,
}


def review_days(priority: Priority) -> int:
    return REVIEW_DAYS.get(priority, REVIEW_DAYS[Priority.NORMAL])


def due_date_for(submitted: date, priority: Priority) -> date:
    """The date the review is expected back by, if none was set explicitly."""
    return submitted + timedelta(days=review_days(priority))


def review_allowance_days() -> int:
    return SUBMITTAL_REVIEW_ALLOWANCE_DAYS


# --- Activity log -------------------------------------------------------------


def log(
    tenant_id: uuid.UUID,
    submittal_id: uuid.UUID,
    kind: SubmittalActivityKind,
    detail: str | None = None,
    author_id: uuid.UUID | None = None,
) -> SubmittalActivity:
    """Build one activity-log row. The caller adds it to the session and commits."""
    return SubmittalActivity(
        tenant_id=tenant_id,
        submittal_id=submittal_id,
        kind=kind,
        detail=detail,
        author_id=author_id,
    )


#: Statuses from which a submittal is out for the reviewer to decide on.
IN_REVIEW = (SubmittalStatus.SUBMITTED, SubmittalStatus.UNDER_REVIEW)
