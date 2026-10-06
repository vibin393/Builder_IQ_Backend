"""Trades: business profile, payment terms, contacts, specialisms

Adds to `vendors` the address, postcode, Companies House number, VAT number, website,
agreed payment terms (which now set an invoice's due date) and notes; and the
`vendor_contacts` and `vendor_specialisms` tables.

Data: each trade's existing single contact becomes its primary contact, so the contacts
list is not empty on day one.

Revision ID: e2c5a9b38d61
Revises: d1b4f8a27c59
Create Date: 2026-09-28
"""

import sqlalchemy as sa

from alembic import op

revision = "e2c5a9b38d61"
down_revision = "d1b4f8a27c59"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    op.add_column("vendors", sa.Column("address", sa.String(300), nullable=True))
    op.add_column("vendors", sa.Column("postcode", sa.String(20), nullable=True))
    op.add_column("vendors", sa.Column("company_number", sa.String(8), nullable=True))
    op.add_column("vendors", sa.Column("vat_number", sa.String(20), nullable=True))
    op.add_column("vendors", sa.Column("website", sa.String(200), nullable=True))
    op.add_column("vendors", sa.Column("payment_terms_days", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("notes", sa.String(2000), nullable=True))

    op.create_table(
        "vendor_contacts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("vendor_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(100), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_ts(),
    )
    op.create_table(
        "vendor_specialisms",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("vendor_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("cost_code_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("cost_codes.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        *_ts(),
        sa.UniqueConstraint("vendor_id", "cost_code_id", name="uq_vendor_specialisms_pair"),
    )

    op.execute(
        """
        INSERT INTO vendor_contacts (id, tenant_id, vendor_id, name, email, phone, is_primary)
        SELECT gen_random_uuid(), v.tenant_id, v.id,
               COALESCE(NULLIF(trim(v.contact_name), ''), v.name), v.email, v.phone, true
        FROM vendors v
        WHERE v.contact_name IS NOT NULL OR v.email IS NOT NULL OR v.phone IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("vendor_specialisms")
    op.drop_table("vendor_contacts")
    for col in ("notes", "payment_terms_days", "website", "vat_number", "company_number",
                "postcode", "address"):
        op.drop_column("vendors", col)
