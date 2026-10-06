"""Working-day arithmetic for the build schedule.

Why this exists
---------------
Every duration in the schedule was calendar days, so a ten-day framing task
starting on a Thursday finished on a Sunday. Nobody frames on a Sunday. Across a
115-day build the error compounds to roughly a third of the programme, which
means every date downstream of the first weekend was wrong and the schedule that
a superintendent and twenty trades work to was fiction.

Construction schedules are in working days. A ten-day task is ten days somebody
turns up, and the calendar stretches around it.

Holidays
--------
Weekends are universal; holidays are not. A builder in Dorset stops for the
August bank holiday and one in Texas does not, so the non-working days beyond
weekends are per builder and configured rather than compiled in. An empty list
means weekends only, which is a reasonable default and an honest one.
"""

from __future__ import annotations

from datetime import date, timedelta

#: Saturday and Sunday. Held as a set rather than a comparison so a builder who
#: works Saturdays can be accommodated later without unpicking the arithmetic.
WEEKEND = frozenset({5, 6})


def is_working_day(day: date, holidays: frozenset[date] = frozenset()) -> bool:
    return day.weekday() not in WEEKEND and day not in holidays


def next_working_day(day: date, holidays: frozenset[date] = frozenset()) -> date:
    """The first working day on or after `day`.

    Used when a task would otherwise be scheduled to begin on a Saturday: work
    starts on the Monday, it does not start on the Saturday and go unattended.
    """
    while not is_working_day(day, holidays):
        day += timedelta(days=1)
    return day


def add_working_days(
    start: date, days: int, holidays: frozenset[date] = frozenset()
) -> date:
    """The date `days` working days after `start`.

    Counted inclusively from the first working day, which is what a builder means
    by a duration: a one-day task starting Monday finishes Monday, and a five-day
    task starting Monday finishes Friday. Counting exclusively would make every
    task finish a day late, which is a subtler and more annoying kind of wrong.
    """
    if days <= 0:
        return next_working_day(start, holidays)

    current = next_working_day(start, holidays)
    remaining = days - 1
    while remaining > 0:
        current += timedelta(days=1)
        current = next_working_day(current, holidays)
        remaining -= 1
    return current


def working_days_between(start: date, finish: date, holidays: frozenset[date] = frozenset()) -> int:
    """How many working days from `start` to `finish`, inclusive.

    The inverse of `add_working_days`, used to express a slip in the units the
    schedule is built in. A task that finishes on Monday having been due Friday is
    one working day late, not three - and telling a superintendent three would be
    counting a weekend nobody was going to work anyway.
    """
    if finish < start:
        return -working_days_between(finish, start, holidays)
    count = 0
    current = start
    while current <= finish:
        if is_working_day(current, holidays):
            count += 1
        current += timedelta(days=1)
    return max(0, count - 1) if count else 0


def shift_working_days(day: date, days: int, holidays: frozenset[date] = frozenset()) -> date:
    """Move a date by a number of working days, forwards or backwards.

    Used to ripple a slip through the tasks behind it. Distinct from
    `add_working_days`, which starts a duration; this one moves an existing date.
    """
    if days == 0:
        return next_working_day(day, holidays)
    step = 1 if days > 0 else -1
    remaining = abs(days)
    current = day
    while remaining > 0:
        current += timedelta(days=step)
        while not is_working_day(current, holidays):
            current += timedelta(days=step)
        remaining -= 1
    return current
