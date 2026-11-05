"""The submittal's revision history and its activity trail.

The submittal itself lives in ``communication.py`` (it shares the ``ball_in_court``
enum with RFIs and links to the buyer's chosen option). What turns it from a single
row that flips status into a real submittal register is here: a record of each review
round it went through, and a log of every move it made.

Like the RFI children, these reference the submittal by string so nothing in
``communication.py`` has to import this file; only this file imports ``BallInCourt``
back, keeping both on one Postgres enum type.
"""

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.communication import Submittal, SubmittalStatus


def _enum(enum_cls, name):
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class SubmittalActivityKind(StrEnum):
    """Every move a submittal can make, so the log reads as a story."""

    CREATED = "created"
    SUBMITTED = "submitted"  # sent in for review
    REVIEW_STARTED = "review_started"
    APPROVED = "approved"
    APPROVED_AS_NOTED = "approved_as_noted"
    REVISE_AND_RESUBMIT = "revise_and_resubmit"
    REJECTED = "rejected"
    RESUBMITTED = "resubmitted"  # trade sent a new revision back
    REASSIGNED = "reassigned"
    EDITED = "edited"


class SubmittalRevision(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One completed review round. Written when a submittal is returned, so the
    history of what was submitted and how it came back is never overwritten.

    The current state stays on the submittal; this is the closed rounds behind it.
    """

    __tablename__ = "submittal_revisions"

    submittal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submittals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # The decision that closed this round.
    outcome: Mapped[SubmittalStatus] = mapped_column(
        _enum(SubmittalStatus, "submittal_status"), nullable=False
    )
    submitted_on: Mapped[date | None] = mapped_column(Date)
    returned_on: Mapped[date | None] = mapped_column(Date)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    review_notes: Mapped[str | None] = mapped_column(String(2000))
    storage_key: Mapped[str | None] = mapped_column(String(500))

    submittal: Mapped[Submittal] = relationship(back_populates="revisions")

    def __repr__(self) -> str:
        return f"<SubmittalRevision r{self.revision} {self.outcome}>"


class SubmittalActivity(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A single line in the submittal's history. Append-only; never edited."""

    __tablename__ = "submittal_activity"

    submittal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submittals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[SubmittalActivityKind] = mapped_column(
        _enum(SubmittalActivityKind, "submittal_activity_kind"), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(String(500))

    submittal: Mapped[Submittal] = relationship(back_populates="activity")

    def __repr__(self) -> str:
        return f"<SubmittalActivity {self.kind}>"
