"""Users: last sign-in, so dormant accounts (likely leavers) can be found

Revision ID: c6a9e4f72b05
Revises: b5f8d3e61a94
Create Date: 2026-09-28
"""

import sqlalchemy as sa

from alembic import op

revision = "c6a9e4f72b05"
down_revision = "b5f8d3e61a94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
