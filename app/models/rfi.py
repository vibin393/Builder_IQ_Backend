"""The RFI conversation and its audit trail.

The RFI itself lives in ``communication.py`` (it shares the ``ball_in_court`` enum
with submittals and is quoted by the change-order flow). What turns it from a single
question-and-answer field into a real RFI is here: a thread of responses from named
parties, one of which is marked official, and a log of every move the record made.

The models reference the RFI by string so nothing in ``communication.py`` has to import
this file — only this file imports ``BallInCourt`` back, which keeps the two on one
Postgres enum type rather than two that drift apart.
"""

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.communication import RFI, BallInCourt


def _enum(enum_cls, name):
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class RFIActivityKind(StrEnum):
    """Every kind of move an RFI can make, so the log reads as a story, not a diff."""

    RAISED = "raised"
    RESPONDED = "responded"
    ANSWERED = "answered"          # an official response was recorded
    REASSIGNED = "reassigned"      # the ball moved to another court
    EDITED = "edited"
    REOPENED = "reopened"
    CLOSED = "closed"
    VOIDED = "voided"
    CHANGE_ORDER = "change_order"  # a priced change was raised from the answer


class RFIResponse(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One entry in an RFI's thread. Exactly one per RFI carries ``is_official``.

    A response names the court it came from, so an architect's answer and a trade's
    clarification read differently even when the same person typed both.
    """

    __tablename__ = "rfi_responses"

    rfi_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rfis.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Which court this response speaks for. Reuses the shared ball_in_court enum.
    court: Mapped[BallInCourt] = mapped_column(
        _enum(BallInCourt, "ball_in_court"),
        default=BallInCourt.BUILDER,
        nullable=False,
    )
    body: Mapped[str] = mapped_column(String(4000), nullable=False)

    # The one response that closes the question. Its cost/schedule impact is what the
    # change-order flow prices, so it is captured here and mirrored onto the RFI.
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_impact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_impact_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    rfi: Mapped[RFI] = relationship(back_populates="responses")

    def __repr__(self) -> str:
        mark = " (official)" if self.is_official else ""
        return f"<RFIResponse {self.court}{mark}>"


class RFIActivity(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A single line in the RFI's history. Append-only; never edited."""

    __tablename__ = "rfi_activity"

    rfi_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rfis.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[RFIActivityKind] = mapped_column(
        _enum(RFIActivityKind, "rfi_activity_kind"),
        nullable=False,
    )
    detail: Mapped[str | None] = mapped_column(String(500))

    rfi: Mapped[RFI] = relationship(back_populates="activity")

    def __repr__(self) -> str:
        return f"<RFIActivity {self.kind}>"
