"""Who gets the next lead that arrives with nobody at the keyboard.

Round-robin, derived rather than stored
--------------------------------------
The obvious way to do round-robin is a pointer: a row saying "last we gave one to
Priya, so next is Marcus". That is also the way that breaks. The pointer needs
locking, because two enquiries arriving in the same second must not both read the
same value; it drifts whenever somebody joins or leaves; and when it is wrong
there is no way to work out what it should have been.

So there is no pointer. The next salesperson is whoever currently holds the
**fewest** automatically captured leads, and among equals whoever received one
least recently. Both facts are already in the data.

Counting rather than only timing is the part that matters. Ordering on "last
assigned" alone looks like the natural round-robin and is quietly broken: six
enquiries arriving inside one clock tick all read the same timestamp, so they all
choose the same person and one salesperson takes the lot. On PostgreSQL's
microsecond clock that is rare; it is still a rotation whose fairness depends on
how precisely the machine can tell the time, which is not a property worth
relying on. A count cannot tie in that way.

What this buys over a pointer:

  * **self-correcting.** Add a salesperson and they hold nothing, so they are next
    until they have caught up. Remove one and the rotation closes over them with no
    cleanup.
  * **concurrency-safe without a lock**, because it is a query rather than a
    read-modify-write. Two simultaneous enquiries can in principle both pick the
    same person; the cost is one salesperson being one lead ahead, which the count
    corrects on the next arrival. Locking to prevent it would be paying interest on
    a debt that settles itself.
  * **auditable.** "Why did Marcus get this one" is answerable from the same
    records the salesperson already reads.

Who is eligible
---------------
Active users who sell. Owner and admin are included only when nobody holds the
sales role - a one-person builder should not have leads pile up unowned because
they never created a separate sales account.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import Lead
from app.models.intake import IntakeEvent
from app.models.user import User, UserRole

#: Roles that are given leads. Order matters: sales first, and the fallbacks are
#: only consulted when nobody holds the role above them.
SELLING_ROLES = (UserRole.SALES,)
FALLBACK_ROLES = (UserRole.OWNER, UserRole.ADMIN)


class RoundRobin:
    """Picks the next owner for an automatically captured lead."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def eligible(self) -> list[User]:
        """The salespeople in rotation, or the fallback when there are none."""
        for roles in (SELLING_ROLES, FALLBACK_ROLES):
            rows = await self.db.scalars(
                select(User)
                .where(
                    User.tenant_id == self.tenant_id,
                    User.is_active.is_(True),
                    User.role.in_(roles),
                )
                .order_by(User.created_at)
            )
            people = list(rows)
            if people:
                return people
        return []

    async def next_owner(self) -> uuid.UUID | None:
        """Whoever is due the next lead, or None when there is nobody to give it to.

        None is a legitimate answer, not a failure: a builder with no active staff
        still wants the enquiry captured. It lands in the unassigned queue, which
        is precisely what that queue is for.
        """
        people = await self.eligible()
        if not people:
            return None
        if len(people) == 1:
            return people[0].id

        load = await self._workload([p.id for p in people])

        # Fewest first, then longest since. Ordering on recency alone looks like
        # the obvious round-robin and is quietly broken: several leads arriving in
        # the same clock tick all read the same "last assigned" time, so they all
        # pick the same person and one salesperson takes the lot. Counting is
        # immune to clock resolution, and in steady state produces exactly the
        # rotation that recency was meant to.
        #
        # `people` is ordered by join date, so the final tiebreak - two newcomers
        # who have both received nothing - is the order they joined rather than
        # whatever the database returned.
        return min(
            people,
            key=lambda p: (load.get(p.id, (0, None))[0], _epoch(load.get(p.id, (0, None))[1])),
        ).id

    async def _workload(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, object]]:
        """Per person: how many automated leads they hold, and when the last arrived.

        Read from the intake events rather than from the activity trail. The trail
        would have been the obvious source, but an activity's `user_id` means "who
        did this", and a round-robin handover has no actor - writing the recipient
        there would overload the column for every other reader of it.

        The intake event needs no such reinterpretation: it is an automated arrival
        by definition, and the lead it produced carries the owner. Joining the two
        asks exactly the question, with nothing implied.

        A lead later reassigned by hand counts against its new owner, which nudges
        the rotation slightly. That is acceptable - it settles within one cycle,
        and the alternative is a column recording who a lead was *originally* given
        to, which nothing else would ever read.
        """
        rows = await self.db.execute(
            select(
                Lead.assigned_to_id,
                func.count(IntakeEvent.id),
                func.max(IntakeEvent.created_at),
            )
            .join(IntakeEvent, IntakeEvent.lead_id == Lead.id)
            .where(
                IntakeEvent.tenant_id == self.tenant_id,
                Lead.assigned_to_id.in_(user_ids),
            )
            .group_by(Lead.assigned_to_id)
        )
        return {r[0]: (r[1], r[2]) for r in rows if r[0] is not None}


def _epoch(value: object) -> float:
    """Sortable timestamp, with "never" sorting first."""
    return value.timestamp() if hasattr(value, "timestamp") else 0.0
