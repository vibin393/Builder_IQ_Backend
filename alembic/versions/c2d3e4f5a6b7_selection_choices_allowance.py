"""Selections: choices, allowance and require-selection flag

Matches BuilderTrend's choice-based selections — a selection is a menu of priced
choices measured against an allowance, that the buyer may be required to pick from.
Kept apart from the contractual option/budget cascade.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("selections", sa.Column("allowance_amount", sa.Numeric(14, 2)))
    op.add_column(
        "selections",
        sa.Column("requires_client_selection", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "selection_choices",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("selection_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("selections.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("option_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("options.id", ondelete="SET NULL")),
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("selection_choices")
    op.drop_column("selections", "requires_client_selection")
    op.drop_column("selections", "allowance_amount")
