"""Mixins that give a model a lifecycle beyond "it exists".

Archiving rather than deleting
------------------------------
Almost nothing in this system can be deleted safely. A cost code is referenced
by budget lines, purchase orders, invoices and timecards; a trade is referenced
by every purchase order they ever held. Deleting either would either fail on a
foreign key or, worse, orphan the history that makes last year's job costing
reconcilable.

So the operation a builder actually wants is not "delete" - it is "stop offering
me this in the picker". That is archiving: the row stays, the history stays, and
the record disappears from the places where somebody would pick it by mistake.

Hard deletion is reserved for rows that nothing references and that carry no
history worth keeping.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class ArchivableMixin:
    """Retire a record without destroying what references it."""

    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Six months later, "why did this trade stop appearing" is a real question.
    archive_reason: Mapped[str | None] = mapped_column(String(500))

    def archive(self, user_id: uuid.UUID | None, reason: str | None = None) -> None:
        self.is_archived = True
        self.archived_at = datetime.now()
        self.archived_by_id = user_id
        self.archive_reason = reason

    def restore(self) -> None:
        self.is_archived = False
        self.archived_at = None
        self.archived_by_id = None
        self.archive_reason = None


class AuditedMixin:
    """Who created this, and who last touched it.

    `updated_at` already exists on every row via TimestampMixin. What it does
    not say is *who*, and for anything that moves money that is the first
    question asked when a number looks wrong.
    """

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


__all__ = ["ArchivableMixin", "AuditedMixin", "func"]
