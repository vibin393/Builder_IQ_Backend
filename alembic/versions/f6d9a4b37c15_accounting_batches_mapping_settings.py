"""Accounting: export batches, nominal mapping, settings and the period lock

The old export re-sent every approved invoice on every download, so importing it twice
posted every cost twice. Now each invoice, payment and retention release is stamped with
the batch that carried it (`invoices.export_batch_id` / `payment_batch_id` /
`retention_batch_id`), and a batch is exported, then posted or voided.

`cost_codes.gl_account` maps job cost to the ledger's nominal codes; `accounting_settings`
holds the control accounts, the default file format and the locked-through date.

No backfill: nothing has been through a batch yet, so everything already approved is
in the first queue. That is the honest starting point — the controller decides what
the first batch sweeps in by its through-date.

Revision ID: f6d9a4b37c15
Revises: e5c8f3a26b94
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f6d9a4b37c15"
down_revision = "e5c8f3a26b94"
branch_labels = None
depends_on = None

KIND = postgresql.ENUM("invoices", "payments", "retention", name="accounting_export_kind")
STATUS = postgresql.ENUM("exported", "posted", "void", name="accounting_export_status")
FORMAT = postgresql.ENUM("journal", "sage50", "xero", name="accounting_export_format")


def _enum(name: str):
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for e in (KIND, STATUS, FORMAT):
        e.create(bind, checkfirst=True)

    op.create_table(
        "accounting_settings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("default_format", _enum("accounting_export_format"), nullable=False,
                  server_default="sage50"),
        sa.Column("cost_account", sa.String(20), nullable=False, server_default="5000"),
        sa.Column("creditors_account", sa.String(20), nullable=False, server_default="2100"),
        sa.Column("retention_account", sa.String(20), nullable=False, server_default="2150"),
        sa.Column("cis_account", sa.String(20), nullable=False, server_default="2211"),
        sa.Column("bank_account", sa.String(20), nullable=False, server_default="1200"),
        sa.Column("locked_through", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_accounting_settings_tenant"),
    )

    op.create_table(
        "accounting_export_batches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("batch_number", sa.String(20), nullable=False, index=True),
        sa.Column("kind", _enum("accounting_export_kind"), nullable=False, index=True),
        sa.Column("status", _enum("accounting_export_status"), nullable=False,
                  server_default="exported", index=True),
        sa.Column("through_date", sa.Date(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ledger_reference", sa.String(64), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(500), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("tenant_id", "batch_number",
                            name="uq_accounting_batches_tenant_number"),
    )

    op.add_column("cost_codes", sa.Column("gl_account", sa.String(20), nullable=True))

    for col in ("export_batch_id", "payment_batch_id", "retention_batch_id"):
        op.add_column("invoices", sa.Column(col, sa.Uuid(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_invoices_{col}_batches", "invoices", "accounting_export_batches",
            [col], ["id"], ondelete="SET NULL",
        )
        op.create_index(f"ix_invoices_{col}", "invoices", [col])


def downgrade() -> None:
    bind = op.get_bind()
    for col in ("retention_batch_id", "payment_batch_id", "export_batch_id"):
        op.drop_index(f"ix_invoices_{col}", table_name="invoices")
        op.drop_constraint(f"fk_invoices_{col}_batches", "invoices", type_="foreignkey")
        op.drop_column("invoices", col)
    op.drop_column("cost_codes", "gl_account")
    op.drop_table("accounting_export_batches")
    op.drop_table("accounting_settings")
    for e in (FORMAT, STATUS, KIND):
        e.drop(bind, checkfirst=True)
