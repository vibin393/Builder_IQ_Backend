"""schedule: a forecast distinct from the baseline, holidays, blocked reasons

Revision ID: a91d3f5c72e8
Revises: f2c74a018be9
Create Date: 2026-09-26

`jobs.forecast_completion` settles a contradiction between two features. The Job
model documents `projected_completion` as the baseline that slip is measured
against; the schedule service moved it every time a task ran late. Both cannot be
true, and the version that moved it wins by accident - which is how a builder ends
up reporting that nothing is ever late.

Two dates now. The baseline is promised and frozen; the forecast is what the
schedule currently expects, and the gap between them is how far behind the job is.

Existing jobs are backfilled with their baseline as the starting forecast, which
is what the schedule would have computed had nothing slipped yet.

`schedule_holidays` supports working-day durations: which days are not worked is a
property of the builder, not of the software.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a91d3f5c72e8"
down_revision: str | None = "f2c74a018be9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("forecast_completion", sa.Date(), nullable=True))
    # A job that has not slipped forecasts its baseline. Leaving these null would
    # report every existing job as having no forecast, which reads as "unknown"
    # rather than as "on time".
    op.execute(
        "UPDATE jobs SET forecast_completion = projected_completion "
        "WHERE projected_completion IS NOT NULL"
    )

    op.add_column("job_tasks", sa.Column("blocked_reason", sa.String(length=500), nullable=True))

    op.create_table(
        "schedule_holidays",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "on_date", name="uq_schedule_holidays_tenant_date"),
    )
    op.create_index(op.f("ix_schedule_holidays_tenant_id"), "schedule_holidays", ["tenant_id"])
    op.create_index(op.f("ix_schedule_holidays_on_date"), "schedule_holidays", ["on_date"])


def downgrade() -> None:
    op.drop_index(op.f("ix_schedule_holidays_on_date"), table_name="schedule_holidays")
    op.drop_index(op.f("ix_schedule_holidays_tenant_id"), table_name="schedule_holidays")
    op.drop_table("schedule_holidays")
    op.drop_column("job_tasks", "blocked_reason")
    op.drop_column("jobs", "forecast_completion")
