"""Data access for leads, realtors and the activity trail.

Three queries here exist only because a sales manager asks for them by name, and
each would be wrong if the caller had to assemble it:

  * **the work queue** - open leads whose follow-up date has passed, oldest
    first. Not "all leads sorted by date": a converted lead with a stale
    follow-up date is not work.
  * **duplicate detection** - somebody who has already enquired, matched on
    email or phone rather than on name, because two Smiths are not a duplicate
    and one Smith who rang twice is.
  * **the funnel count** - how many leads sit at each status, in one round trip
    rather than six.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, or_, select

from app.models.crm import OPEN_STATUSES, Lead, LeadActivity, LeadStatus, Realtor
from app.repositories.base import TenantRepository


class RealtorRepository(TenantRepository[Realtor]):
    model = Realtor
    label = "Realtor"
    search_fields = ("code", "name", "brokerage", "email")

    async def all_ordered(self, *, include_archived: bool = False) -> list[Realtor]:
        return await self.list(order_by=Realtor.name, include_archived=include_archived)

    async def page(self, params, **filters):
        return await self.paginate(params, order_by=Realtor.name, **filters)

    async def code_taken_by_other(self, code: str, exclude_id: uuid.UUID) -> bool:
        found = await self.db.scalar(
            select(Realtor.id).where(
                Realtor.tenant_id == self.tenant_id,
                func.lower(Realtor.code) == code.lower(),
                Realtor.id != exclude_id,
            )
        )
        return found is not None

    async def lead_counts(self) -> dict[uuid.UUID, tuple[int, int]]:
        """Per realtor: how many leads they have sent, and how many converted.

        The pair is the whole point. An agent who sends thirty leads and closes
        none is not the same as one who sends three and closes two, and a raw
        count would rank them the wrong way round.
        """
        rows = await self.db.execute(
            select(
                Lead.realtor_id,
                func.count(Lead.id),
                func.count(Lead.id).filter(Lead.status == LeadStatus.CONVERTED),
            )
            .where(Lead.tenant_id == self.tenant_id, Lead.realtor_id.is_not(None))
            .group_by(Lead.realtor_id)
        )
        return {r[0]: (r[1], r[2]) for r in rows}


class LeadRepository(TenantRepository[Lead]):
    model = Lead
    label = "Lead"
    search_fields = ("first_name", "last_name", "email", "phone", "mobile", "campaign", "city")

    async def all_ordered(self, *, include_archived: bool = False) -> list[Lead]:
        return await self.list(order_by=Lead.created_at.desc(), include_archived=include_archived)

    async def page(self, params, **filters):
        """Newest first. A lead list is read from the top: the one that came in
        this morning is the one somebody needs to ring."""
        return await self.paginate(params, order_by=Lead.created_at.desc(), **filters)

    async def page_open_only(self, params, **filters):
        """A page restricted to leads that are still live.

        Applied as an extra WHERE rather than a status filter, so it composes with
        a caller that also wants, say, only hot leads.
        """
        rows, total = await self._page_with(params, Lead.status.in_(OPEN_STATUSES), **filters)
        return rows, total

    async def page_overdue(self, params, **filters):
        """The work queue: open leads whose follow-up has come and gone.

        Ordered by the follow-up date, oldest first - the most neglected lead is
        the one at the top, which is the one that should be rung.
        """
        return await self._page_with(
            params,
            Lead.status.in_(OPEN_STATUSES),
            Lead.next_follow_up.is_not(None),
            Lead.next_follow_up < date.today(),
            order_by=Lead.next_follow_up.asc(),
            **filters,
        )

    async def page_unassigned(self, params, **filters):
        """Open leads with nobody's name on them. The queue that loses deals."""
        return await self._page_with(
            params,
            Lead.status.in_(OPEN_STATUSES),
            Lead.assigned_to_id.is_(None),
            order_by=Lead.created_at.asc(),
            **filters,
        )

    async def _page_with(self, params, *conditions, order_by=None, **filters):
        """paginate(), plus arbitrary extra conditions.

        The base class deliberately only offers equality filters, because that is
        all most features need. These three queries need ranges and IN clauses, so
        they build on the tenant-scoped statement rather than around it.
        """
        stmt = self._filtered(include_archived=params.include_archived, **filters)
        stmt = self._searched(stmt, params.search)
        for condition in conditions:
            stmt = stmt.where(condition)

        total = await self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(order_by if order_by is not None else Lead.created_at.desc())
        rows = await self.db.scalars(stmt.offset(params.offset).limit(params.size))
        return list(rows), total

    async def find_duplicates(
        self,
        *,
        email: str | None,
        phone: str | None,
        mobile: str | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[Lead]:
        """Existing leads that look like the same person.

        Matched on contact details, never on name: two unrelated J. Smiths are
        not a duplicate, and the same person ringing twice about two plans is.
        Phone numbers are compared with punctuation stripped, because "01202
        555 111" and "01202555111" are the same number to everyone except a
        string comparison.
        """
        clauses = []
        if email:
            clauses.append(func.lower(Lead.email) == email.strip().lower())
        for number in (phone, mobile):
            if number:
                digits = _digits(number)
                if len(digits) >= 7:  # too short to identify anyone
                    clauses.extend(
                        [
                            _digits_of(Lead.phone) == digits,
                            _digits_of(Lead.mobile) == digits,
                        ]
                    )
        if not clauses:
            return []

        stmt = self._base().where(or_(*clauses))
        if exclude_id:
            stmt = stmt.where(Lead.id != exclude_id)
        return list(await self.db.scalars(stmt.order_by(Lead.created_at.desc()).limit(10)))

    async def status_counts(self, *, include_archived: bool = False) -> dict[str, int]:
        """The funnel, in one round trip. Statuses with no leads report zero."""
        stmt = (
            select(Lead.status, func.count(Lead.id))
            .where(Lead.tenant_id == self.tenant_id)
            .group_by(Lead.status)
        )
        if not include_archived:
            stmt = stmt.where(Lead.is_archived.is_(False))
        found = {row[0]: row[1] for row in await self.db.execute(stmt)}
        return {status.value: found.get(status, 0) for status in LeadStatus}

    async def source_counts(self) -> list[tuple[str, int, int]]:
        """Per source: enquiries and conversions.

        This is the report that decides next year's marketing spend, which is why
        conversions are counted alongside enquiries rather than instead of them.
        """
        rows = await self.db.execute(
            select(
                Lead.source,
                func.count(Lead.id),
                func.count(Lead.id).filter(Lead.status == LeadStatus.CONVERTED),
            )
            .where(Lead.tenant_id == self.tenant_id, Lead.is_archived.is_(False))
            .group_by(Lead.source)
            .order_by(func.count(Lead.id).desc())
        )
        return [(r[0].value, r[1], r[2]) for r in rows]

    async def counts_for_dashboard(self) -> dict[str, int]:
        """The four numbers that go above the list."""
        today = date.today()
        open_clause = Lead.status.in_(OPEN_STATUSES)
        base = [Lead.tenant_id == self.tenant_id, Lead.is_archived.is_(False)]

        async def n(*extra) -> int:
            return (
                await self.db.scalar(select(func.count()).select_from(Lead).where(*base, *extra))
                or 0
            )

        return {
            "open": await n(open_clause),
            "unassigned": await n(open_clause, Lead.assigned_to_id.is_(None)),
            "overdue": await n(
                open_clause, Lead.next_follow_up.is_not(None), Lead.next_follow_up < today
            ),
            "due_today": await n(open_clause, Lead.next_follow_up == today),
        }

    async def by_ids(self, ids: list[uuid.UUID]) -> list[Lead]:
        """Several leads at once, for the bulk actions. Silently drops ids that
        are not ours - the caller is told how many were touched, not which
        strangers' records exist."""
        if not ids:
            return []
        return list(await self.db.scalars(self._base().where(Lead.id.in_(ids))))


class LeadActivityRepository(TenantRepository[LeadActivity]):
    model = LeadActivity
    label = "Activity"
    search_fields = ("summary",)

    async def for_lead(self, lead_id: uuid.UUID, limit: int = 200) -> list[LeadActivity]:
        """Newest first. `occurred_on` is nullable, so created_at breaks the tie
        and guarantees a stable order."""
        return list(
            await self.db.scalars(
                self._base()
                .where(LeadActivity.lead_id == lead_id)
                .order_by(
                    LeadActivity.occurred_on.desc().nullslast(),
                    LeadActivity.created_at.desc(),
                )
                .limit(limit)
            )
        )

    async def last_human_touch(self, lead_id: uuid.UUID) -> LeadActivity | None:
        """The last thing a person actually did, ignoring what the system logged.

        "Last contacted" must not be advanced by an automatic status-change entry,
        or a lead nobody has rung for a month will look freshly worked.
        """
        return await self.db.scalar(
            self._base()
            .where(LeadActivity.lead_id == lead_id, LeadActivity.is_system.is_(False))
            .order_by(
                LeadActivity.occurred_on.desc().nullslast(),
                LeadActivity.created_at.desc(),
            )
            .limit(1)
        )


#: The punctuation people actually put in phone numbers.
_PHONE_NOISE = (" ", "-", "(", ")", ".", "+", "/")


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def _digits_of(column):
    r"""SQL-side equivalent of `_digits`, so the comparison happens in the database.

    Nested REPLACE rather than a regex: `regexp_replace` is PostgreSQL-only, and
    the test suite runs against SQLite. Stripping a known list of punctuation is
    less elegant than `\D` but it is portable, and the list is closed - nobody
    writes a phone number with a letter in it.
    """
    expr = func.coalesce(column, "")
    for ch in _PHONE_NOISE:
        expr = func.replace(expr, ch, "")
    return expr
