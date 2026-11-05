"""Financial: owner (client) invoices + payments, and the cost inbox

The receivables side (money the owner owes the builder) and the staging queue for
incoming costs before they become bills.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-11-02
"""

import sqlalchemy as sa

from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "owner_invoices",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_number", sa.String(32), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.Enum("deposit", "progress", "final", "standard", name="owner_invoice_kind"),
                  nullable=False, server_default="progress"),
        sa.Column("status", sa.Enum("draft", "sent", "viewed", "partial", "paid", "void", name="owner_invoice_status"),
                  nullable=False, server_default="draft", index=True),
        sa.Column("issue_date", sa.Date()),
        sa.Column("due_date", sa.Date(), index=True),
        sa.Column("viewed_by_owner_on", sa.Date()),
        sa.Column("subtotal", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("retainage_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(2000)),
        *_ts(),
    )
    op.create_table(
        "owner_payments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("owner_invoice_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("owner_invoices.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False, index=True),
        sa.Column("method", sa.String(20), nullable=False, server_default="other"),
        sa.Column("reference", sa.String(100)),
        sa.Column("notes", sa.String(500)),
        *_ts(),
    )
    op.create_table(
        "cost_inbox_items",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.Enum("new", "coded", "pushed", "rejected", name="cost_inbox_status"),
                  nullable=False, server_default="new", index=True),
        sa.Column("amount", sa.Numeric(14, 2)),
        sa.Column("purchase_date", sa.Date(), index=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="SET NULL"), index=True),
        sa.Column("vendor_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="SET NULL"), index=True),
        sa.Column("cost_code_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("cost_codes.id", ondelete="SET NULL"), index=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("invoice_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("invoices.id", ondelete="SET NULL")),
        sa.Column("tags", sa.JSON()),
        sa.Column("notes", sa.String(1000)),
        *_ts(),
    )


def downgrade() -> None:
    op.drop_table("cost_inbox_items")
    op.drop_table("owner_payments")
    op.drop_table("owner_invoices")
    for name in ("cost_inbox_status", "owner_invoice_status", "owner_invoice_kind"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
