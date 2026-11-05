"""Sales: proposal templates (reusable priced proposals)

A named set of line items a salesperson applies to a lead instead of retyping the
same scope, and can be saved from an existing proposal — BuilderTrend's Proposal
templates.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "proposal_templates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        *_ts(),
    )
    op.create_table(
        "proposal_template_items",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("template_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("proposal_templates.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("group_name", sa.String(120), nullable=False, server_default="General"),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("unit", sa.String(40)),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("markup_percent", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_ts(),
    )


def downgrade() -> None:
    op.drop_table("proposal_template_items")
    op.drop_table("proposal_templates")
