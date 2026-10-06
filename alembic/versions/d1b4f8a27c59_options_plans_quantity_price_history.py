"""Options: plan availability, quantities, price history, selection cost snapshots

  * `option_plans`: the house types an option is offered on (none = every plan).
  * `options.max_quantity` / `unit` / `supplier_ref`.
  * `option_price_changes`: who changed an option's price, and why.
  * `selections.cost_breakdown`: the option's cost per cost code when it was chosen.
    Confirming and cancelling used the catalog's *current* lines, so a catalog change
    in between made the budget and the reversal disagree. Existing selections are
    backfilled from the current lines - exactly what the old code would have used.
  * New categories on `option_category`: kitchen, bathroom, heating, landscaping,
    technology.

Revision ID: d1b4f8a27c59
Revises: c9a3e7f16b48
Create Date: 2026-09-28
"""

import sqlalchemy as sa

from alembic import op

revision = "d1b4f8a27c59"
down_revision = "c9a3e7f16b48"
branch_labels = None
depends_on = None

NEW_CATEGORIES = ("kitchen", "bathroom", "heating", "landscaping", "technology")


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    # New enum values are not used in this migration, so adding them in its
    # transaction is safe.
    for value in NEW_CATEGORIES:
        op.execute(f"ALTER TYPE option_category ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column(
        "options", sa.Column("max_quantity", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("options", sa.Column("unit", sa.String(20), nullable=True))
    op.add_column("options", sa.Column("supplier_ref", sa.String(64), nullable=True))

    op.create_table(
        "option_plans",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("option_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("options.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("plan_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True),
        *_ts(),
        sa.UniqueConstraint("option_id", "plan_id", name="uq_option_plans_pair"),
    )
    op.create_table(
        "option_price_changes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("option_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("options.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("old_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("new_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("changed_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        *_ts(),
    )

    op.add_column("selections", sa.Column("cost_breakdown", sa.JSON(), nullable=True))
    op.execute(
        """
        UPDATE selections s SET cost_breakdown = (
            SELECT COALESCE(json_agg(json_build_object(
                       'cost_code_id', cl.cost_code_id::text,
                       'vendor_id', cl.vendor_id::text,
                       'amount', cl.amount::text)), '[]'::json)
            FROM option_cost_lines cl WHERE cl.option_id = s.option_id)
        WHERE s.cost_breakdown IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("selections", "cost_breakdown")
    op.drop_table("option_price_changes")
    op.drop_table("option_plans")
    op.drop_column("options", "supplier_ref")
    op.drop_column("options", "unit")
    op.drop_column("options", "max_quantity")
    # Enum values cannot be dropped from a Postgres type; the extra categories stay.
