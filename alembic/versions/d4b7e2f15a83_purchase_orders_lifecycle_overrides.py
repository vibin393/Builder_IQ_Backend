"""Purchase orders: required-by date, lifecycle dates and recorded overrides

The PO issue gates (trade compliance, remaining budget) could be overridden, and the
service said an override was "a recorded act" — but nothing recorded it. A PO issued past
a lapsed insurance certificate looked identical to one that passed cleanly. This adds the
record: which gate was overridden, why, and who issued it. It also adds the dates the
lifecycle needs (required-by, acknowledged, completed) so the COMPLETE status is reachable
and a late PO can be seen.

Existing rows keep working: no override is claimed for POs issued before this, and the
new dates are null until the lifecycle sets them.

Revision ID: d4b7e2f15a83
Revises: c39a5b2d0f71
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "d4b7e2f15a83"
down_revision = "c39a5b2d0f71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchase_orders", sa.Column("issued_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("purchase_orders", sa.Column("required_by", sa.Date(), nullable=True))
    op.add_column("purchase_orders", sa.Column("acknowledged_on", sa.Date(), nullable=True))
    op.add_column("purchase_orders", sa.Column("completed_on", sa.Date(), nullable=True))
    op.add_column(
        "purchase_orders",
        sa.Column("compliance_override", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "purchase_orders",
        sa.Column("budget_override", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("purchase_orders", sa.Column("override_reason", sa.String(500), nullable=True))
    op.create_foreign_key(
        "fk_purchase_orders_issued_by_id_users",
        "purchase_orders", "users", ["issued_by_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_purchase_orders_required_by", "purchase_orders", ["required_by"])


def downgrade() -> None:
    op.drop_index("ix_purchase_orders_required_by", table_name="purchase_orders")
    op.drop_constraint("fk_purchase_orders_issued_by_id_users", "purchase_orders", type_="foreignkey")
    for col in (
        "override_reason", "budget_override", "compliance_override",
        "completed_on", "acknowledged_on", "required_by", "issued_by_id",
    ):
        op.drop_column("purchase_orders", col)
