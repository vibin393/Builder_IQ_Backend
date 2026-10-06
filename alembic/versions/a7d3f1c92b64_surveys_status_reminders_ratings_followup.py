"""Surveys: status, response window, reminders, extra ratings and detractor follow-up

The surveys module could send a survey and record a response, and nothing in between or
after. It had no notion of a response window (so no overdue queue and no reminders), no
status beyond "has a responded_date", only four department ratings, and - despite the
screen saying "detractor comments are the ones worth reading" - no way to record that
anyone had read one, let alone acted on it.

  * `surveys.status` gives the survey its own small lifecycle (pending/responded/
    declined/expired), and `due_date`/`reminder_count`/`last_reminded_date` make the
    chase real.
  * `rating_communication`, `rating_value` and `overall_satisfaction` broaden what is
    measured beyond the original four departments.
  * `follow_up_*` turn a detractor from a number into a task with an owner.

Existing rows are read honestly, not flattened: a survey with a `responded_date` becomes
'responded', an unanswered one keeps its window from its sent date, and a past detractor
is flagged for the follow-up it never had.

Revision ID: a7d3f1c92b64
Revises: f4a2c9d1e880
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7d3f1c92b64"
down_revision = "f4a2c9d1e880"
branch_labels = None
depends_on = None


SURVEY_STATUS = postgresql.ENUM(
    "pending", "responded", "declined", "expired", name="survey_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    SURVEY_STATUS.create(bind, checkfirst=True)

    op.add_column(
        "surveys",
        sa.Column("status", postgresql.ENUM(name="survey_status", create_type=False),
                  nullable=False, server_default="pending"),
    )
    op.add_column("surveys", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column(
        "surveys",
        sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("surveys", sa.Column("last_reminded_date", sa.Date(), nullable=True))
    op.add_column("surveys", sa.Column("rating_communication", sa.Integer(), nullable=True))
    op.add_column("surveys", sa.Column("rating_value", sa.Integer(), nullable=True))
    op.add_column("surveys", sa.Column("overall_satisfaction", sa.Integer(), nullable=True))
    op.add_column(
        "surveys",
        sa.Column("follow_up_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "surveys",
        sa.Column("follow_up_done", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("surveys", sa.Column("follow_up_notes", sa.String(2000), nullable=True))
    op.add_column("surveys", sa.Column("follow_up_owner_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_surveys_follow_up_owner_id_users",
        "surveys", "users", ["follow_up_owner_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_surveys_status", "surveys", ["status"])
    op.create_index("ix_surveys_due_date", "surveys", ["due_date"])

    # Backfill from what the rows already say.
    op.execute("UPDATE surveys SET status = 'responded' WHERE responded_date IS NOT NULL")
    op.execute(
        "UPDATE surveys SET due_date = sent_date + INTERVAL '21 days' "
        "WHERE sent_date IS NOT NULL AND due_date IS NULL"
    )
    op.execute(
        "UPDATE surveys SET follow_up_required = true "
        "WHERE responded_date IS NOT NULL AND would_recommend IS NOT NULL AND would_recommend <= 6"
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_surveys_due_date", table_name="surveys")
    op.drop_index("ix_surveys_status", table_name="surveys")
    op.drop_constraint("fk_surveys_follow_up_owner_id_users", "surveys", type_="foreignkey")
    for col in (
        "follow_up_owner_id", "follow_up_notes", "follow_up_done", "follow_up_required",
        "overall_satisfaction", "rating_value", "rating_communication",
        "last_reminded_date", "reminder_count", "due_date", "status",
    ):
        op.drop_column("surveys", col)

    postgresql.ENUM(name="survey_status", create_type=False).drop(bind, checkfirst=True)
