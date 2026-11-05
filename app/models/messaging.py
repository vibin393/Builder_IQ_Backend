"""In-app messaging: threaded comments and an internal message inbox.

Two different conversations, kept apart on purpose.

A **Comment** is talk *about a thing* — a note on a job, pinned to the record it is
about, that anyone looking at that record sees. It threads (a reply has a parent)
so a discussion reads as a discussion.

A **Message** is talk *between people* — an internal note sent to named recipients,
with its own inbox and read state, grouped into a thread. It is email-shaped, not
record-shaped.

The existing `Correspondence` model is neither: it is a one-way audit log of things
said to outside parties. This is the two-way, in-house layer it never tried to be.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Comment(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "comments"

    #: Comments are scoped to a job — the record they hang off always lives under one.
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Optional finer target, so a comment can hang off a specific record within the
    #: job (an RFI, a selection) rather than the job as a whole.
    entity_type: Mapped[str | None] = mapped_column(String(40), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)

    #: A reply points at the comment it answers. Null means a top-level comment.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<Comment job={self.job_id} by={self.author_id}>"


class Message(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """An internal message. The first message of a conversation is its own thread
    root; replies reuse its `thread_id`."""

    __tablename__ = "messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Optionally about a job, so a message can be filed against the work it concerns.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )

    recipients: Mapped[list["MessageRecipient"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class MessageRecipient(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One addressee of a message, and whether they have read it. Read state is
    per-person, which is the whole reason the inbox is a table and not a column."""

    __tablename__ = "message_recipients"

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    message: Mapped["Message"] = relationship(back_populates="recipients")

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
