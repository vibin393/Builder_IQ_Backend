"""expand user roles: executive, accounting, warranty, architect

Revision ID: e9e97e97e9ad
Revises: 133ec5bd35ae
Create Date: 2026-09-24

Hand-written. Alembic's autogenerate does not detect new values on an existing
Postgres enum, so adding them to the Python StrEnum alone leaves the database
rejecting every insert that uses one.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on older
Postgres and cannot be reversed at all, so:

  * each value is added with IF NOT EXISTS, making the upgrade re-runnable
  * the downgrade deliberately does nothing but say so, rather than pretending
    to reverse something Postgres will not reverse

Removing an enum value properly means recreating the type and rewriting every
column that uses it. That is a real migration, not a rollback, and it belongs in
its own revision written when somebody actually wants it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e9e97e97e9ad"
down_revision: str | None = "133ec5bd35ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ROLES = ("executive", "accounting", "warranty", "architect")


def upgrade() -> None:
    # Autocommit: ALTER TYPE ... ADD VALUE cannot participate in a transaction.
    with op.get_context().autocommit_block():
        for role in NEW_ROLES:
            op.execute(f"ALTER TYPE user_role ADD VALUE IF NOT EXISTS '{role}'")


def downgrade() -> None:
    # Postgres has no DROP VALUE. Leaving the values in place is harmless: no row
    # uses them unless somebody created such a user, and in that case dropping
    # the value would orphan them.
    pass
