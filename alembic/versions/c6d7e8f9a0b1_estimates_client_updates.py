"""Project management: estimates + line items, and client updates

Estimates are the customer-facing, priced version of a job's work (grouped line
items, cost marked up to price) that become a proposal. Client updates are the
builder's broadcast channel to the homeowner.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-11-02
"""

import sqlalchemy as sa

from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "estimates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.Enum("draft", "sent", "accepted", "declined", name="estimate_status"),
                  nullable=False, server_default="draft", index=True),
        sa.Column("default_markup_percent", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("tax_percent", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("issued_on", sa.Date()),
        sa.Column("notes", sa.Text()),
        *_ts(),
    )
    op.create_table(
        "estimate_items",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("estimate_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("estimates.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("group_name", sa.String(120), nullable=False, server_default="General"),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("cost_code_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("cost_codes.id", ondelete="SET NULL")),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("markup_percent", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_ts(),
    )
    op.create_table(
        "client_updates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("posted_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("posted_on", sa.Date(), nullable=False, server_default=sa.func.current_date(), index=True),
        *_ts(),
    )


def downgrade() -> None:
    op.drop_table("client_updates")
    op.drop_table("estimate_items")
    op.drop_table("estimates")
    sa.Enum(name="estimate_status").drop(op.get_bind(), checkfirst=True)
