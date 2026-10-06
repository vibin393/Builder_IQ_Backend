"""Plans: UK specification, elevations, price history

Adds to `plans` the house type, storeys, gross internal area in m², en-suites, a
cloakroom flag, garage, parking, target EPC band and a description; and the
`plan_elevations` and `plan_price_changes` tables.

Data:
  * "2.5 bathrooms" is the US half-bath. Here it is two bathrooms and a cloakroom, so a
    fractional count becomes the whole number plus `has_cloakroom`.
  * m² is filled from sq ft where only sq ft was recorded.
  * Every elevation already typed on a home becomes an elevation of its plan (at no
    price difference), so homes keep matching once a plan's elevations are enforced.

Revision ID: c9a3e7f16b48
Revises: b8f2d6e59a37
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c9a3e7f16b48"
down_revision = "b8f2d6e59a37"
branch_labels = None
depends_on = None

HOUSE = postgresql.ENUM(
    "detached", "semi_detached", "terraced", "end_terrace", "bungalow", "apartment",
    "maisonette", "coach_house", name="plan_house_type",
)
GARAGE = postgresql.ENUM(
    "none", "single", "double", "integral", "carport", name="plan_garage_type"
)


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    HOUSE.create(bind, checkfirst=True)
    GARAGE.create(bind, checkfirst=True)

    op.add_column("plans", sa.Column("floor_area_sqm", sa.Numeric(8, 1), nullable=True))
    op.add_column("plans", sa.Column("en_suites", sa.Integer(), nullable=True))
    op.add_column(
        "plans",
        sa.Column("has_cloakroom", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "plans",
        sa.Column("house_type", postgresql.ENUM(name="plan_house_type", create_type=False),
                  nullable=True),
    )
    op.add_column("plans", sa.Column("storeys", sa.Integer(), nullable=True))
    op.add_column(
        "plans",
        sa.Column("garage", postgresql.ENUM(name="plan_garage_type", create_type=False),
                  nullable=True),
    )
    op.add_column("plans", sa.Column("parking_spaces", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("epc_target", sa.String(1), nullable=True))
    op.add_column("plans", sa.Column("description", sa.String(2000), nullable=True))

    op.create_table(
        "plan_elevations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("plan_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_adjustment", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_ts(),
        sa.UniqueConstraint("plan_id", "code", name="uq_plan_elevations_code"),
    )
    op.create_table(
        "plan_price_changes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("plan_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("old_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("new_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("changed_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        *_ts(),
    )

    op.execute(
        "UPDATE plans SET has_cloakroom = true, bathrooms = floor(bathrooms) "
        "WHERE bathrooms IS NOT NULL AND bathrooms <> floor(bathrooms)"
    )
    op.execute(
        "UPDATE plans SET floor_area_sqm = round(square_feet / 10.7639, 1) "
        "WHERE square_feet IS NOT NULL AND floor_area_sqm IS NULL"
    )
    op.execute(
        """
        INSERT INTO plan_elevations (id, tenant_id, plan_id, code, name)
        SELECT gen_random_uuid(), j.tenant_id, j.plan_id, upper(trim(j.elevation)),
               'Elevation ' || upper(trim(j.elevation))
        FROM jobs j
        WHERE j.elevation IS NOT NULL AND trim(j.elevation) <> ''
        GROUP BY j.tenant_id, j.plan_id, upper(trim(j.elevation))
        """
    )
    op.execute("UPDATE jobs SET elevation = upper(trim(elevation)) WHERE elevation IS NOT NULL")


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("plan_price_changes")
    op.drop_table("plan_elevations")
    for col in ("description", "epc_target", "parking_spaces", "garage", "storeys",
                "house_type", "has_cloakroom", "en_suites", "floor_area_sqm"):
        op.drop_column("plans", col)
    GARAGE.drop(bind, checkfirst=True)
    HOUSE.drop(bind, checkfirst=True)
