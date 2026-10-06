"""Land: UK planning, obligations (conditions, S106, CIL, S38/S278/S104), appraisal

Adds to `land_projects` the site area in hectares, GDV and build-cost estimate (the
appraisal), purchase date, local authority, planning reference / status / approval and
expiry dates, and notes; and the `land_obligations` table.

Data: hectares are filled from acres where only acres were recorded.

Revision ID: b5f8d3e61a94
Revises: a4e7c2d59f83
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b5f8d3e61a94"
down_revision = "a4e7c2d59f83"
branch_labels = None
depends_on = None

PLANNING = postgresql.ENUM(
    "not_submitted", "submitted", "outline", "reserved_matters", "full", "refused", "appeal",
    name="land_planning_status",
)
KIND = postgresql.ENUM(
    "condition", "s106", "cil", "s38", "s278", "s104", "other", name="land_obligation_kind"
)
STATUS = postgresql.ENUM(
    "outstanding", "submitted", "discharged", "paid", "waived", name="land_obligation_status"
)


def _enum(name):
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for e in (PLANNING, KIND, STATUS):
        e.create(bind, checkfirst=True)

    op.add_column("land_projects", sa.Column("site_area_ha", sa.Numeric(10, 2), nullable=True))
    op.add_column("land_projects", sa.Column("gdv", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "land_projects", sa.Column("build_cost_estimate", sa.Numeric(14, 2), nullable=True)
    )
    op.add_column("land_projects", sa.Column("purchased_on", sa.Date(), nullable=True))
    op.add_column("land_projects", sa.Column("local_authority", sa.String(120), nullable=True))
    op.add_column("land_projects", sa.Column("planning_ref", sa.String(64), nullable=True))
    op.add_column(
        "land_projects",
        sa.Column("planning_status", _enum("land_planning_status"), nullable=False,
                  server_default="not_submitted"),
    )
    op.add_column("land_projects", sa.Column("planning_approved_on", sa.Date(), nullable=True))
    op.add_column("land_projects", sa.Column("planning_expires_on", sa.Date(), nullable=True))
    op.add_column("land_projects", sa.Column("notes", sa.String(2000), nullable=True))

    op.create_table(
        "land_obligations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("project_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("land_projects.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("kind", _enum("land_obligation_kind"), nullable=False),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("trigger", sa.String(300), nullable=True),
        sa.Column("pre_commencement", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("status", _enum("land_obligation_status"), nullable=False,
                  server_default="outstanding", index=True),
        sa.Column("completed_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )

    op.execute(
        "UPDATE land_projects SET site_area_ha = round(acres * 0.404686, 2) "
        "WHERE acres IS NOT NULL AND site_area_ha IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("land_obligations")
    for col in ("notes", "planning_expires_on", "planning_approved_on", "planning_status",
                "planning_ref", "local_authority", "purchased_on", "build_cost_estimate", "gdv",
                "site_area_ha"):
        op.drop_column("land_projects", col)
    for e in (STATUS, KIND, PLANNING):
        e.drop(bind, checkfirst=True)
