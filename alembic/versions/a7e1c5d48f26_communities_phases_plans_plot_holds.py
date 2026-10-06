"""Communities: status and details, release phases, plans offered, plot holds

Adds the development details (status, address, planning authority, planned plots,
target sales rate, sales and site managers), `community_phases`, `community_plans`, and
on `lots` the phase, area, notes, release date and a real hold (until, reason, by whom,
for which lead).

Data fix: starting a home used to set its plot to "held". A held plot now means a
sales reservation, so plots held only because a home is on them go back to available
(or sold, where the home has a buyer). Holds that are real but predate expiry dates are
given 14 days, so none sits on hold indefinitely. Jobs sold through a contract but with
no sale date get the contract's date, so absorption counts them.

Revision ID: a7e1c5d48f26
Revises: f6d9a4b37c15
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a7e1c5d48f26"
down_revision = "f6d9a4b37c15"
branch_labels = None
depends_on = None

STATUS = postgresql.ENUM("planning", "selling", "sold_out", "closed", name="community_status")


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    STATUS.create(bind, checkfirst=True)

    op.add_column(
        "communities",
        sa.Column("status", postgresql.ENUM(name="community_status", create_type=False),
                  nullable=False, server_default="selling"),
    )
    op.create_index("ix_communities_status", "communities", ["status"])
    op.add_column("communities", sa.Column("address", sa.String(300), nullable=True))
    op.add_column("communities", sa.Column("postcode", sa.String(20), nullable=True))
    op.add_column("communities", sa.Column("local_authority", sa.String(120), nullable=True))
    op.add_column("communities", sa.Column("description", sa.String(2000), nullable=True))
    op.add_column("communities", sa.Column("planned_plots", sa.Integer(), nullable=True))
    op.add_column("communities", sa.Column("opened_on", sa.Date(), nullable=True))
    op.add_column(
        "communities", sa.Column("target_sales_per_month", sa.Numeric(5, 1), nullable=True)
    )
    for col in ("sales_manager_id", "site_manager_id"):
        op.add_column("communities", sa.Column(col, sa.Uuid(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_communities_{col}_users", "communities", "users", [col], ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "community_phases",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("community_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("communities.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("planned_release", sa.Date(), nullable=True),
        sa.Column("released_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        *_ts(),
        sa.UniqueConstraint("community_id", "name", name="uq_community_phases_name"),
    )

    op.create_table(
        "community_plans",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("community_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("communities.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("plan_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("base_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_ts(),
        sa.UniqueConstraint("community_id", "plan_id", name="uq_community_plans_pair"),
    )

    op.add_column("lots", sa.Column("phase_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_lots_phase_id_community_phases", "lots", "community_phases", ["phase_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_lots_phase_id", "lots", ["phase_id"])
    op.add_column("lots", sa.Column("plot_area_sqm", sa.Numeric(10, 2), nullable=True))
    op.add_column("lots", sa.Column("notes", sa.String(1000), nullable=True))
    op.add_column("lots", sa.Column("released_on", sa.Date(), nullable=True))
    op.add_column("lots", sa.Column("hold_until", sa.Date(), nullable=True))
    op.add_column("lots", sa.Column("hold_reason", sa.String(300), nullable=True))
    op.add_column("lots", sa.Column("held_on", sa.Date(), nullable=True))
    op.add_column("lots", sa.Column("held_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("lots", sa.Column("held_for_lead_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_lots_held_by_id_users", "lots", "users", ["held_by_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_lots_held_for_lead_id_leads", "lots", "leads", ["held_for_lead_id"], ["id"],
        ondelete="SET NULL",
    )

    # --- data -------------------------------------------------------------------------
    # A sale recorded by contract but without a date on the job.
    op.execute(
        """
        UPDATE jobs SET sold_date = c.created_at::date
        FROM contracts c
        WHERE c.job_id = jobs.id AND jobs.sold_date IS NULL
          AND jobs.status::text <> 'cancelled'
        """
    )
    # Held only because a home was started on it: sold if the home has a buyer...
    op.execute(
        """
        UPDATE lots SET status = 'sold'
        WHERE status::text = 'held' AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.lot_id = lots.id
              AND j.status::text <> 'cancelled' AND j.sold_date IS NOT NULL)
        """
    )
    # ...otherwise back on sale, as the speculative home it is.
    op.execute(
        """
        UPDATE lots SET status = 'available'
        WHERE status::text = 'held' AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.lot_id = lots.id AND j.status::text <> 'cancelled')
        """
    )
    # A genuine hold from before holds had dates: two weeks, then it lapses.
    op.execute(
        """
        UPDATE lots SET hold_until = CURRENT_DATE + 14, held_on = CURRENT_DATE,
               hold_reason = 'Carried over - give it an end date or lift it'
        WHERE status::text = 'held' AND hold_until IS NULL
        """
    )
    op.execute(
        "UPDATE lots SET released_on = created_at::date "
        "WHERE status::text <> 'not_released' AND released_on IS NULL"
    )
    op.execute(
        "UPDATE communities SET opened_on = ("
        "  SELECT MIN(l.created_at)::date FROM lots l WHERE l.community_id = communities.id)"
        " WHERE opened_on IS NULL"
    )
    op.execute("UPDATE communities SET status = 'closed' WHERE is_active = false")


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint("fk_lots_held_for_lead_id_leads", "lots", type_="foreignkey")
    op.drop_constraint("fk_lots_held_by_id_users", "lots", type_="foreignkey")
    op.drop_constraint("fk_lots_phase_id_community_phases", "lots", type_="foreignkey")
    op.drop_index("ix_lots_phase_id", table_name="lots")
    for col in ("held_for_lead_id", "held_by_id", "held_on", "hold_reason", "hold_until",
                "released_on", "notes", "plot_area_sqm", "phase_id"):
        op.drop_column("lots", col)
    op.drop_table("community_plans")
    op.drop_table("community_phases")
    for col in ("site_manager_id", "sales_manager_id"):
        op.drop_constraint(f"fk_communities_{col}_users", "communities", type_="foreignkey")
        op.drop_column("communities", col)
    for col in ("target_sales_per_month", "opened_on", "planned_plots", "description",
                "local_authority", "postcode", "address"):
        op.drop_column("communities", col)
    op.drop_index("ix_communities_status", table_name="communities")
    op.drop_column("communities", "status")
    STATUS.drop(bind, checkfirst=True)
