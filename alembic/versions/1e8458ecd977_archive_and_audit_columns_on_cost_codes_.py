"""archive and audit columns on cost codes and vendors

Revision ID: 1e8458ecd977
Revises: e9e97e97e9ad
Create Date: 2026-09-25

Hand-adjusted after autogenerate, for the same two reasons as the last one:

  * `is_archived` is NOT NULL and these tables already hold rows, so it needs a
    server_default - existing records are not archived
  * autogenerate emits `create_foreign_key(None, ...)`, which cannot be dropped
    in the downgrade because it has no name
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1e8458ecd977"
down_revision: str | None = "e9e97e97e9ad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("cost_codes", "vendors")
USER_FKS = ("archived_by_id", "created_by_id", "updated_by_id")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.add_column(table, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("archived_by_id", sa.Uuid(), nullable=True))
        op.add_column(table, sa.Column("archive_reason", sa.String(length=500), nullable=True))
        op.add_column(table, sa.Column("created_by_id", sa.Uuid(), nullable=True))
        op.add_column(table, sa.Column("updated_by_id", sa.Uuid(), nullable=True))

        # Archived rows are filtered out of nearly every read, so the flag earns
        # its index.
        op.create_index(
            op.f(f"ix_{table}_is_archived"), table, ["is_archived"], unique=False
        )

        for column in USER_FKS:
            op.create_foreign_key(
                f"fk_{table}_{column}", table, "users", [column], ["id"], ondelete="SET NULL"
            )


def downgrade() -> None:
    for table in TABLES:
        for column in USER_FKS:
            op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_is_archived"), table_name=table)
        for column in (
            "updated_by_id",
            "created_by_id",
            "archive_reason",
            "archived_by_id",
            "archived_at",
            "is_archived",
        ):
            op.drop_column(table, column)
