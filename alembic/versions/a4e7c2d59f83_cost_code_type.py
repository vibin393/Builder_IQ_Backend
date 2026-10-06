"""Cost codes: cost type (labour, materials, subcontract, plant, preliminaries, fees)

No backfill: what kind of cost a code carries is the commercial manager's call, not
something to infer from its name.

Revision ID: a4e7c2d59f83
Revises: f3d6b1c49e72
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a4e7c2d59f83"
down_revision = "f3d6b1c49e72"
branch_labels = None
depends_on = None

TYPE = postgresql.ENUM(
    "labour", "materials", "subcontract", "plant", "preliminaries", "fees", "other",
    name="cost_code_type",
)


def upgrade() -> None:
    TYPE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "cost_codes",
        sa.Column("cost_type", postgresql.ENUM(name="cost_code_type", create_type=False),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cost_codes", "cost_type")
    TYPE.drop(op.get_bind(), checkfirst=True)
