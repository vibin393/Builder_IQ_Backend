"""The data-access tier.

Every SQL statement in the application belongs below this line. Nothing above it
imports `select`, touches a `Session`, or knows that PostgreSQL exists.

Why it is worth the layer
-------------------------
Before this existed there were 191 database calls sitting directly in the HTTP
routers. That works, and it is how the application was built - but it means the
same three things are re-typed on every endpoint: scope the query to the tenant,
404 rather than 403 when a row belongs to somebody else, and raise a conflict
when a code is already taken. Re-typed rules get forgotten, and the one that
gets forgotten will be the tenant filter.

Here the tenant filter is not something a caller remembers to apply. It is a
constructor argument, and every method applies it. A repository cannot read
another builder's data even if the caller asks it to.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import Base
from app.schemas.common import PageParams


class NotFound(Exception):
    """No such row, or it belongs to another tenant.

    Deliberately one exception for both. The API turns it into a 404 - a 403
    would confirm the record exists, which is exactly what a caller probing
    another builder's data wants to learn.
    """

    def __init__(self, entity: str, obj_id: uuid.UUID | None = None):
        self.entity = entity
        self.obj_id = obj_id
        super().__init__(f"{entity} not found")


class Conflict(Exception):
    """The write would break a uniqueness rule the business cares about."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class TenantRepository[ModelT: Base]:
    """Data access for one model, permanently scoped to one tenant.

    Construct it with the tenant from the verified token and it cannot reach
    outside that tenant. There is no method that takes a tenant_id, so there is
    no method a caller can pass the wrong one to.
    """

    model: type[ModelT]
    #: Human name used in error messages, e.g. "Cost code".
    label: str = "Record"
    #: Relationships to eager-load by default, so callers never trip a lazy load
    #: on a detached object.
    default_loads: tuple[str, ...] = ()
    #: Columns a free-text search looks at. Empty means this feature has nothing
    #: worth searching, and `search` is ignored rather than silently matching all.
    search_fields: tuple[str, ...] = ()

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    # -- query construction -------------------------------------------------

    def _base(self, *loads: str) -> Select:
        """A SELECT already filtered to this tenant. The only way in."""
        stmt = select(self.model).where(self.model.tenant_id == self.tenant_id)
        for name in loads or self.default_loads:
            stmt = stmt.options(selectinload(getattr(self.model, name)))
        return stmt

    # -- reads ---------------------------------------------------------------

    async def list(self, *, order_by: Any = None, **filters: Any) -> list[ModelT]:
        stmt = self._filtered(**filters)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        return list(await self.db.scalars(stmt))

    def _filtered(self, **filters: Any) -> Select:
        """Tenant scope, equality filters, and the archived rule."""
        stmt = self._base()
        include_archived = filters.pop("include_archived", False)
        for field, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, field) == value)
        # Archived rows are hidden unless asked for: they are archived precisely
        # so nobody picks them by accident.
        if hasattr(self.model, "is_archived") and not include_archived:
            stmt = stmt.where(self.model.is_archived.is_(False))
        return stmt

    def _searched(self, stmt: Select, term: str | None) -> Select:
        """Case-insensitive match across whatever this repository calls searchable."""
        if not term or not self.search_fields:
            return stmt
        like = f"%{term.strip().lower()}%"
        clauses = [
            func.lower(getattr(self.model, f)).like(like)
            for f in self.search_fields
            if hasattr(self.model, f)
        ]
        return stmt.where(or_(*clauses)) if clauses else stmt

    async def paginate(
        self,
        params: PageParams,
        *,
        order_by: Any = None,
        **filters: Any,
    ) -> tuple[list[ModelT], int]:
        """One page of rows, and how many matched in total.

        The total is counted after filtering and searching, so a caller can say
        "12 of 340 match" without either number being a lie.
        """
        stmt = self._filtered(include_archived=params.include_archived, **filters)
        stmt = self._searched(stmt, params.search)

        # Count over the same conditions, without the eager loads or ordering.
        total = await self.db.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0

        if order_by is not None:
            stmt = stmt.order_by(order_by)
        # The primary key as a final tiebreaker, always. Paging is only
        # well-defined over a *total* order: if two rows tie on the sort column -
        # two enquiries captured in the same second, two cost codes with the same
        # name - the database may order them differently between the query for
        # page 1 and the query for page 2, and a row then appears twice or not at
        # all. This makes that impossible rather than unlikely.
        stmt = stmt.order_by(self.model.id)
        rows = await self.db.scalars(stmt.offset(params.offset).limit(params.size))
        return list(rows), total

    async def get(self, obj_id: uuid.UUID, *loads: str) -> ModelT:
        """One row, or NotFound. Never another tenant's row."""
        obj = await self.db.scalar(self._base(*loads).where(self.model.id == obj_id))
        if obj is None:
            raise NotFound(self.label, obj_id)
        return obj

    async def find(self, obj_id: uuid.UUID, *loads: str) -> ModelT | None:
        """Like `get`, but None rather than raising, for callers that expect a miss."""
        return await self.db.scalar(self._base(*loads).where(self.model.id == obj_id))

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model).where(
            self.model.tenant_id == self.tenant_id
        )
        for field, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, field) == value)
        return (await self.db.scalar(stmt)) or 0

    async def exists_with_code(self, code: str, field: str = "code") -> bool:
        """Case-insensitive uniqueness check, scoped to the tenant.

        Two builders may both have a cost code "03-100"; one builder may not.
        """
        column = getattr(self.model, field)
        found = await self.db.scalar(
            select(self.model.id).where(
                self.model.tenant_id == self.tenant_id,
                func.lower(column) == code.lower(),
            )
        )
        return found is not None

    # -- writes --------------------------------------------------------------

    def stage(self, **values: Any) -> ModelT:
        """Add a new row to the session without committing.

        Staging and committing are separate on purpose: a service that writes
        three related rows wants one transaction, not three.
        """
        obj = self.model(tenant_id=self.tenant_id, **values)
        self.db.add(obj)
        return obj

    async def create(self, **values: Any) -> ModelT:
        """Stage, commit and return a fully loaded row."""
        obj = self.stage(**values)
        await self.db.commit()
        return await self.get(obj.id)

    async def update(self, obj: ModelT, **values: Any) -> ModelT:
        """Apply only the fields supplied. `None` means "not supplied", not "clear it"."""
        for field, value in values.items():
            if value is not None:
                setattr(obj, field, value)
        await self.db.commit()
        return await self.get(obj.id)

    async def flush(self) -> None:
        """Make staged rows visible to later queries in the same transaction."""
        await self.db.flush()

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, obj: ModelT, *attrs: str) -> ModelT:
        await self.db.refresh(obj, list(attrs) or None)
        return obj

    # -- guards --------------------------------------------------------------

    async def archive(
        self, obj: ModelT, user_id: uuid.UUID | None, reason: str | None = None
    ) -> ModelT:
        """Retire a record without destroying what references it."""
        if not hasattr(obj, "archive"):
            raise Conflict(f"{self.label} records cannot be archived.")
        if getattr(obj, "is_archived", False):
            raise Conflict(f"{self.label} is already archived.")
        obj.archive(user_id, reason)
        await self.db.commit()
        return await self.get(obj.id)

    async def restore(self, obj: ModelT) -> ModelT:
        if not getattr(obj, "is_archived", False):
            raise Conflict(f"{self.label} is not archived.")
        obj.restore()
        await self.db.commit()
        return await self.get(obj.id)

    async def require_unique_code(self, code: str, field: str = "code") -> None:
        if await self.exists_with_code(code, field):
            raise Conflict(f"{self.label} {code} already exists.")

    async def require_exists(self, obj_id: uuid.UUID) -> ModelT:
        """Assert a foreign key points at something in this tenant.

        Called before writing a row that references another table, so a request
        cannot attach its record to a different builder's community.
        """
        return await self.get(obj_id)
