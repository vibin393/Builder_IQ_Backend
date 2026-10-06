"""add homeowner user role

Revision ID: 2eec410a2373
Revises: 2b01c55e0769
Create Date: 2026-09-22 13:18:58.095308
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '2eec410a2373'
down_revision: str | None = '2b01c55e0769'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the homeowner role.

    Alembic's autogenerate does not detect new values on an existing PostgreSQL enum,
    so this has to be written by hand. Adding a value cannot run inside a transaction
    on older servers, hence the explicit COMMIT.
    """
    op.execute("COMMIT")
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'homeowner'")


def downgrade() -> None:
    """PostgreSQL cannot drop a single enum value; reversing means rebuilding the type.

    Left as a no-op deliberately rather than silently rewriting the column.
    """
    pass
