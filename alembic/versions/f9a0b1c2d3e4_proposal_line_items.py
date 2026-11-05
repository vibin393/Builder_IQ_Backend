"""Sales: lead proposal line items (estimate-like proposals)

Gives a lead proposal priced line items (cost → markup → price), matching
BuilderTrend, where a proposal is the estimate a lead is offered. The flat `amount`
remains as a fallback for quick one-number proposals.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposal_items",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("proposal_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("lead_proposals.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("group_name", sa.String(120), nullable=False, server_default="General"),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("unit", sa.String(40)),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("markup_percent", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("proposal_items")
