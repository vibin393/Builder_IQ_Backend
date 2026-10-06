"""Trade compliance: remap US-named certificates to their UK equivalents

The previous revision added the UK document types. Records captured before it used the
US names for the same covers — "general liability" is what a UK insurer calls public
liability, and "workers' comp" is the US counterpart of employers' liability. Left alone,
every existing trade would read as missing both required covers when it actually holds
them. This renames those two in place; the other legacy values have no clean UK
equivalent and are left as they are.

A separate revision because PostgreSQL will not let an enum value be used in the same
transaction that added it.

Revision ID: c39a5b2d0f71
Revises: b28f4a1c9e60
Create Date: 2026-09-28
"""

from alembic import op

revision = "c39a5b2d0f71"
down_revision = "b28f4a1c9e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE compliance_documents SET document_type = 'public_liability' "
        "WHERE document_type = 'general_liability'"
    )
    op.execute(
        "UPDATE compliance_documents SET document_type = 'employers_liability' "
        "WHERE document_type = 'workers_comp'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE compliance_documents SET document_type = 'general_liability' "
        "WHERE document_type = 'public_liability'"
    )
    op.execute(
        "UPDATE compliance_documents SET document_type = 'workers_comp' "
        "WHERE document_type = 'employers_liability'"
    )
