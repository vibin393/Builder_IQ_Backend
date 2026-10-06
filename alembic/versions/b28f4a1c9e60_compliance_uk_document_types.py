"""Trade compliance: UK document types

The prequalification document types were US-shaped (general liability, workers' comp,
W-9, auto liability) — the wrong paperwork to ask a British subcontractor for, and the
kind of detail a UK client notices in a demo. This adds the covers and registrations a
UK homebuilder actually collects: public and employers' liability, professional
indemnity, CIS and VAT registration, trade accreditation (Gas Safe, NICEIC), a health &
safety policy and a DBS check.

The old values stay on the enum so records created before this rework still read; the
frontend leads with the UK types and treats the US ones as legacy.

Revision ID: b28f4a1c9e60
Revises: a17e9c3b8d24
Create Date: 2026-09-28
"""

from alembic import op

revision = "b28f4a1c9e60"
down_revision = "a17e9c3b8d24"
branch_labels = None
depends_on = None

_NEW_VALUES = (
    "public_liability",
    "employers_liability",
    "professional_indemnity",
    "cis_registration",
    "vat_registration",
    "trade_accreditation",
    "health_safety_policy",
    "dbs_check",
)


def upgrade() -> None:
    # PG16 allows ADD VALUE inside a transaction; IF NOT EXISTS keeps it idempotent.
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE compliance_document_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value, and rebuilding the type would risk the
    # rows already using it. The added values are harmless if left in place.
    pass
