"""Messaging: threaded comments and an internal message inbox

Comments hang off a job (and optionally a record within it) and thread via parent.
Messages are internal, sent to named recipients with per-person read state, grouped
into threads.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-11-02
"""

import sqlalchemy as sa

from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("entity_type", sa.String(40), index=True),
        sa.Column("entity_id", sa.Uuid(as_uuid=True), index=True),
        sa.Column("parent_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("comments.id", ondelete="CASCADE"), index=True),
        sa.Column("author_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("body", sa.Text(), nullable=False),
        *_ts(),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("thread_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="SET NULL"), index=True),
        *_ts(),
    )
    op.create_table(
        "message_recipients",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("message_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        *_ts(),
    )


def downgrade() -> None:
    op.drop_table("message_recipients")
    op.drop_table("messages")
    op.drop_table("comments")
