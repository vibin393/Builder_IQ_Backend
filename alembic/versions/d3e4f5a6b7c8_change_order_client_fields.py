"""Change orders: approval deadline + client/internal notes

Matches BuilderTrend's change-order form: a client-response deadline and separate
client-facing vs internal note streams, so a client preview never shows the
builder's internal remarks.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_orders", sa.Column("approval_deadline", sa.Date()))
    op.add_column("change_orders", sa.Column("client_notes", sa.String(2000)))
    op.add_column("change_orders", sa.Column("internal_notes", sa.String(2000)))


def downgrade() -> None:
    op.drop_column("change_orders", "internal_notes")
    op.drop_column("change_orders", "client_notes")
    op.drop_column("change_orders", "approval_deadline")
