"""Estimate line unit, and owner-invoice line items

Adds a unit of measure to estimate lines, and gives client invoices optional line
items (cost → markup → price) plus description/closing/terms fields — matching
BuilderTrend's line-item invoice mode.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "a0b1c2d3e4f5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estimate_items", sa.Column("unit", sa.String(40)))

    op.add_column("owner_invoices", sa.Column("invoice_description", sa.Text()))
    op.add_column("owner_invoices", sa.Column("closing_text", sa.Text()))
    op.add_column("owner_invoices", sa.Column("payment_terms", sa.String(100)))

    op.create_table(
        "owner_invoice_lines",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("owner_invoice_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("owner_invoices.id", ondelete="CASCADE"), nullable=False, index=True),
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
    op.drop_table("owner_invoice_lines")
    op.drop_column("owner_invoices", "payment_terms")
    op.drop_column("owner_invoices", "closing_text")
    op.drop_column("owner_invoices", "invoice_description")
    op.drop_column("estimate_items", "unit")
