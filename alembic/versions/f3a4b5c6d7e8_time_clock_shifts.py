"""Time clock: employee shifts with clock-in/out and location

The presence layer, distinct from timecards (the money layer). A shift records when
a worker clocked in, where they were, and when they clocked out — the data behind
"who is on site now" and the employee locator map.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None

_STATUS = ("active", "completed", "cancelled")
_TYPE = ("regular", "overtime", "break", "pto")
_OOB = ("unknown", "in_bounds", "out_of_bounds")


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    # Let create_table create each enum once (one column per enum here), the
    # standard Alembic pattern. Mixing an explicit ENUM.create() with the table's
    # own enum columns double-creates the type.
    op.create_table(
        "shifts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("worker_name", sa.String(200), nullable=False),
        sa.Column("cost_code_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("cost_codes.id", ondelete="SET NULL"), index=True),
        sa.Column("status", sa.Enum(*_STATUS, name="shift_status"),
                  nullable=False, server_default="active", index=True),
        sa.Column("shift_type", sa.Enum(*_TYPE, name="shift_type"),
                  nullable=False, server_default="regular"),
        sa.Column("clock_in_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("clock_out_at", sa.DateTime(timezone=True)),
        sa.Column("clock_in_lat", sa.Numeric(9, 6)),
        sa.Column("clock_in_lng", sa.Numeric(9, 6)),
        sa.Column("clock_out_lat", sa.Numeric(9, 6)),
        sa.Column("clock_out_lng", sa.Numeric(9, 6)),
        sa.Column("out_of_bounds", sa.Enum(*_OOB, name="out_of_bounds_status"),
                  nullable=False, server_default="unknown"),
        sa.Column("tags", sa.JSON()),
        sa.Column("notes", sa.String(1000)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("invoiced_to_client", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("entered_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_ts(),
    )


def downgrade() -> None:
    op.drop_table("shifts")
    for name in ("shift_status", "shift_type", "out_of_bounds_status"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
