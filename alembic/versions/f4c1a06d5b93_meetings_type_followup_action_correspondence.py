"""Meetings: type, follow-up series, carried actions and correspondence follow-up

The meetings tool could record an agenda, mark a meeting held, and count open actions —
but not close one (there was no complete endpoint), not carry an open action into the
next meeting, not say what kind of meeting it was, and not track whether a piece of
correspondence that demanded a reply ever got one.

  * `meetings` gains `meeting_type` and `previous_meeting_id` (the link that makes a
    follow-up part of a series).
  * `meeting_items` gains `carried_from_id` (an action rolled forward from a prior
    meeting points back at where it was first raised).
  * `correspondence` gains `requires_response`, `response_due` and `responded_on`, so
    the log becomes a chase list for the letters still awaiting a reply.

Existing rows keep working: every meeting defaults to type 'coordination', and no
existing correspondence requires a response until it is marked as such.

Revision ID: f4c1a06d5b93
Revises: e3b9c52a7f18
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f4c1a06d5b93"
down_revision = "e3b9c52a7f18"
branch_labels = None
depends_on = None


MEETING_TYPE = postgresql.ENUM(
    "pre_construction", "coordination", "oac", "progress", "pre_install",
    "safety", "handover", "other",
    name="meeting_type",
)


def _enum_ref(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    MEETING_TYPE.create(bind, checkfirst=True)

    # --- meetings -------------------------------------------------------------
    op.add_column(
        "meetings",
        sa.Column("meeting_type", _enum_ref("meeting_type"),
                  nullable=False, server_default="coordination"),
    )
    op.add_column(
        "meetings", sa.Column("previous_meeting_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_meetings_previous_meeting_id",
        "meetings", "meetings", ["previous_meeting_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_meetings_meeting_type", "meetings", ["meeting_type"])

    # --- meeting_items --------------------------------------------------------
    op.add_column(
        "meeting_items", sa.Column("carried_from_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_meeting_items_carried_from_id",
        "meeting_items", "meeting_items", ["carried_from_id"], ["id"], ondelete="SET NULL",
    )

    # --- correspondence -------------------------------------------------------
    op.add_column(
        "correspondence",
        sa.Column("requires_response", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("correspondence", sa.Column("response_due", sa.Date(), nullable=True))
    op.add_column("correspondence", sa.Column("responded_on", sa.Date(), nullable=True))
    op.create_index("ix_correspondence_response_due", "correspondence", ["response_due"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_correspondence_response_due", table_name="correspondence")
    op.drop_column("correspondence", "responded_on")
    op.drop_column("correspondence", "response_due")
    op.drop_column("correspondence", "requires_response")

    op.drop_constraint("fk_meeting_items_carried_from_id", "meeting_items", type_="foreignkey")
    op.drop_column("meeting_items", "carried_from_id")

    op.drop_index("ix_meetings_meeting_type", table_name="meetings")
    op.drop_constraint("fk_meetings_previous_meeting_id", "meetings", type_="foreignkey")
    op.drop_column("meetings", "previous_meeting_id")
    op.drop_column("meetings", "meeting_type")

    postgresql.ENUM(name="meeting_type", create_type=False).drop(bind, checkfirst=True)
