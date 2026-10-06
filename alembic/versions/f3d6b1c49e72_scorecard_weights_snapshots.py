"""Trade scorecard: the builder's weights, and monthly snapshots for the trend

Revision ID: f3d6b1c49e72
Revises: e2c5a9b38d61
Create Date: 2026-09-28
"""

import sqlalchemy as sa

from alembic import op

revision = "f3d6b1c49e72"
down_revision = "e2c5a9b38d61"
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
    op.create_table(
        "scorecard_settings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("weights", sa.JSON(), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", name="uq_scorecard_settings_tenant"),
    )
    op.create_table(
        "scorecard_snapshots",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("vendor_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("overall", sa.Integer(), nullable=True),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        *_ts(),
        sa.UniqueConstraint("vendor_id", "period", name="uq_scorecard_snapshots_period"),
    )


def downgrade() -> None:
    op.drop_table("scorecard_snapshots")
    op.drop_table("scorecard_settings")
