"""Warranty business rules: the SLA clock and the coverage engine.

These live in a service, not in the router, because two of them are policy the builder
will want to change (how fast we promise to respond, how long cover lasts) and one of
them - the coverage decision - is the whole reason the module earns its keep: it answers
"is this our bill or the homeowner's?" without a human guessing.
"""

from __future__ import annotations

from datetime import date

from app.models.warranty import (
    STRUCTURAL_CATEGORIES,
    CoverageStatus,
    Priority,
    WarrantyCategory,
)

# --- SLA: how quickly we promise to respond, by priority ----------------------
#
# An emergency (no heat, no water, an active leak) is a same-day promise. The rest
# step down from there. These are response targets against the submitted date, the
# figure a housebuilder is judged on far more than the eventual fix date.
SLA_DAYS: dict[Priority, int] = {
    Priority.EMERGENCY: 1,
    Priority.HIGH: 3,
    Priority.NORMAL: 14,
    Priority.LOW: 30,
}

# --- Coverage: NHBC-style Buildmark windows, measured from legal completion ----
#
# The UK new-build standard: the builder carries a 2-year defects period for
# workmanship, and structural cover runs to 10 years. Both start at the closing
# (completion) date on the job.
DEFECTS_PERIOD_YEARS = 2
STRUCTURAL_PERIOD_YEARS = 10


def sla_days(priority: Priority) -> int:
    return SLA_DAYS.get(priority, SLA_DAYS[Priority.NORMAL])


def due_date_for(submitted: date, priority: Priority) -> date:
    """The date by which customer care has promised to have responded."""
    from datetime import timedelta

    return submitted + timedelta(days=sla_days(priority))


def _add_years(start: date, years: int) -> date:
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        # 29 Feb -> 28 Feb in a non-leap year.
        return start.replace(year=start.year + years, day=28)


def coverage_windows(closing_date: date | None) -> dict[str, date | None]:
    """When each cover expires for a home that closed on ``closing_date``."""
    if closing_date is None:
        return {"closing_date": None, "defects_until": None, "structural_until": None}
    return {
        "closing_date": closing_date,
        "defects_until": _add_years(closing_date, DEFECTS_PERIOD_YEARS),
        "structural_until": _add_years(closing_date, STRUCTURAL_PERIOD_YEARS),
    }


def suggest_coverage(
    category: WarrantyCategory,
    closing_date: date | None,
    on_date: date | None = None,
) -> CoverageStatus:
    """Best guess at whether a claim of this category is still in cover today.

    A suggestion, not a verdict: the assessor can always override it, and where we
    have no completion date to measure from we say PENDING rather than guess.
    """
    if closing_date is None:
        return CoverageStatus.PENDING

    on_date = on_date or date.today()
    windows = coverage_windows(closing_date)
    limit = (
        windows["structural_until"]
        if category in STRUCTURAL_CATEGORIES
        else windows["defects_until"]
    )
    if limit is None:
        return CoverageStatus.PENDING
    return CoverageStatus.IN_WARRANTY if on_date <= limit else CoverageStatus.OUT_OF_WARRANTY
