"""Invoices: UK CIS, disputes, late-recorded waivers and recorded overrides

Four gaps in the payables module:

  * A lien waiver could only be recorded when the invoice was received. Payment and
    retention release both refuse without one, so an invoice logged without a waiver
    could never be paid. `lien_waiver_date` records when it arrived.
  * The DISPUTED and VOID statuses had no way in; `disputed_on` / `dispute_reason` keep
    why an invoice was held back.
  * Approving above the PO could be overridden but left no trace; `over_po_override`,
    `override_reason`, `approved_by_id` and (for payment) `paid_by_id` /
    `payment_reference` are the record.
  * UK Construction Industry Scheme: `vendors.cis_status` / `utr`, and on the invoice the
    `labour_amount` CIS applies to plus the `cis_rate` snapshotted at receipt.

Backfill: invoices with no due date take standard 30-day terms from their invoice date,
so overdue-to-pay covers existing invoices too; a waiver already on file is dated from
the invoice date. No trade is given a CIS status — that is a verification with HMRC,
not something to infer.

Revision ID: e5c8f3a26b94
Revises: d4b7e2f15a83
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e5c8f3a26b94"
down_revision = "d4b7e2f15a83"
branch_labels = None
depends_on = None

CIS_STATUS = postgresql.ENUM("gross", "net", "unverified", name="vendor_cis_status")


def upgrade() -> None:
    bind = op.get_bind()
    CIS_STATUS.create(bind, checkfirst=True)

    op.add_column(
        "vendors",
        sa.Column("cis_status", postgresql.ENUM(name="vendor_cis_status", create_type=False),
                  nullable=True),
    )
    op.add_column("vendors", sa.Column("utr", sa.String(20), nullable=True))

    op.add_column("invoices", sa.Column("labour_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "invoices",
        sa.Column("cis_rate", sa.Numeric(5, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "invoices",
        sa.Column("cis_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("invoices", sa.Column("approved_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column(
        "invoices",
        sa.Column("over_po_override", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("invoices", sa.Column("override_reason", sa.String(500), nullable=True))
    op.add_column("invoices", sa.Column("paid_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("invoices", sa.Column("payment_reference", sa.String(120), nullable=True))
    op.add_column("invoices", sa.Column("lien_waiver_date", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("disputed_on", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("dispute_reason", sa.String(1000), nullable=True))
    op.create_foreign_key(
        "fk_invoices_approved_by_id_users", "invoices", "users",
        ["approved_by_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_invoices_paid_by_id_users", "invoices", "users",
        ["paid_by_id"], ["id"], ondelete="SET NULL",
    )

    op.execute(
        "UPDATE invoices SET due_date = invoice_date + INTERVAL '30 days' "
        "WHERE due_date IS NULL AND invoice_date IS NOT NULL"
    )
    op.execute(
        "UPDATE invoices SET lien_waiver_date = invoice_date "
        "WHERE lien_waiver_received = true AND lien_waiver_date IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint("fk_invoices_paid_by_id_users", "invoices", type_="foreignkey")
    op.drop_constraint("fk_invoices_approved_by_id_users", "invoices", type_="foreignkey")
    for col in (
        "dispute_reason", "disputed_on", "lien_waiver_date", "payment_reference",
        "paid_by_id", "override_reason", "over_po_override", "approved_by_id",
        "cis_verified", "cis_rate", "labour_amount",
    ):
        op.drop_column("invoices", col)
    op.drop_column("vendors", "utr")
    op.drop_column("vendors", "cis_status")
    postgresql.ENUM(name="vendor_cis_status", create_type=False).drop(bind, checkfirst=True)
