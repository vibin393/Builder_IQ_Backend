"""Safety and quality: RIDDOR reporting, observation sign-off, action plan provenance

Three things this adds, and one it backfills.

**Observations** gain `closed_by_id`: who accepted the fix, as distinct from who
did it. Without it an observation closed on the word of the person who owed it.

**Incidents** gain a RIDDOR category, the date the report actually went to the HSE
and its reference. The existing `reportable` boolean said an incident *ought* to be
reported and nothing said whether anybody had - which is the only part the HSE
cares about. It is kept, because it is what somebody ticked at the time and
throwing that away would be discarding a judgement we cannot reconstruct.

The backfill reads the existing rows rather than defaulting them all to
not_reportable: an incident already ticked reportable is categorised from what it
records - a lost-time injury as over-seven-day, a treated injury as a specified
injury, a dangerous occurrence from the near-miss and property-damage types. It is
a best reading of thin data, so it is deliberately conservative: anything ticked
reportable comes out reportable under some category, never as not_reportable.

**Action plans** gain `template_id` and `started_on`, so "copied onto a job when it
is started" becomes a fact that can be queried, and items gain `completed_by_id`
and `signed_off_on` so the two-person rule on a sign-off step can be checked
rather than merely asserted in a comment.

Revision ID: b62f9d174c03
Revises: c3e8b47a1d95
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "b62f9d174c03"
down_revision = "c3e8b47a1d95"
branch_labels = None
depends_on = None


RIDDOR = sa.Enum(
    "not_reportable",
    "fatality",
    "specified_injury",
    "over_7_day",
    "occupational_disease",
    "dangerous_occurrence",
    name="riddor_category",
)


def upgrade() -> None:
    bind = op.get_bind()

    # --- observations ---------------------------------------------------------
    op.add_column(
        "observations",
        sa.Column("closed_by_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_observations_closed_by_id_users",
        "observations",
        "users",
        ["closed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- incidents ------------------------------------------------------------
    RIDDOR.create(bind, checkfirst=True)
    op.add_column(
        "incidents",
        sa.Column(
            "riddor_category",
            RIDDOR,
            nullable=False,
            server_default="not_reportable",
        ),
    )
    op.add_column("incidents", sa.Column("reported_to_hse_on", sa.Date(), nullable=True))
    op.add_column("incidents", sa.Column("hse_reference", sa.String(length=100), nullable=True))
    op.create_index("ix_incidents_riddor_category", "incidents", ["riddor_category"])

    # Read what each row already says rather than flattening everything to
    # not_reportable. Ordered most specific first; the final clause is the safety
    # net so nothing ticked reportable escapes categorisation.
    op.execute(
        """
        UPDATE incidents SET riddor_category = (CASE
            WHEN incident_type = 'near_miss' OR incident_type = 'property_damage'
                THEN 'dangerous_occurrence'
            WHEN incident_type = 'illness' THEN 'occupational_disease'
            WHEN lost_time THEN 'over_7_day'
            WHEN medical_treatment THEN 'specified_injury'
            ELSE 'specified_injury'
        END)::riddor_category
        WHERE reportable
        """
    )

    # --- action plans ---------------------------------------------------------
    op.add_column("action_plans", sa.Column("template_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("action_plans", sa.Column("started_on", sa.Date(), nullable=True))
    op.create_foreign_key(
        "fk_action_plans_template_id_action_plans",
        "action_plans",
        "action_plans",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_action_plans_template_id", "action_plans", ["template_id"])
    # A plan already on a job was started at some point and nobody recorded when.
    # Its creation date is the honest answer available.
    op.execute(
        """
        UPDATE action_plans
           SET started_on = created_at::date
         WHERE job_id IS NOT NULL AND is_template = false AND started_on IS NULL
        """
    )

    op.add_column(
        "action_plan_items", sa.Column("completed_by_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column("action_plan_items", sa.Column("signed_off_on", sa.Date(), nullable=True))
    op.create_foreign_key(
        "fk_action_plan_items_completed_by_id_users",
        "action_plan_items",
        "users",
        ["completed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # An item that was signed off already has the user; the date it happened was
    # never stored, and the day it was completed is the closest true statement.
    op.execute(
        """
        UPDATE action_plan_items
           SET signed_off_on = completed_on
         WHERE signed_off_by_id IS NOT NULL AND completed_on IS NOT NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Refuse rather than silently discard a statutory record. An incident reported
    # to the HSE carries a reference number that exists nowhere else in this
    # system, and dropping the column would lose the only evidence the report was
    # made.
    reported = bind.execute(
        sa.text(
            "SELECT title, occurred_on, hse_reference FROM incidents "
            "WHERE reported_to_hse_on IS NOT NULL ORDER BY occurred_on"
        )
    ).fetchall()
    if reported:
        listed = "; ".join(
            f"{r.title[:40]} ({r.occurred_on}, ref {r.hse_reference or 'none recorded'})"
            for r in reported
        )
        raise RuntimeError(
            f"{len(reported)} incident(s) have been reported to the HSE and this "
            f"downgrade would drop the record of it: {listed}. "
            "Export them before downgrading - the reference number is not "
            "recoverable from anything else here."
        )

    op.drop_constraint(
        "fk_action_plan_items_completed_by_id_users", "action_plan_items", type_="foreignkey"
    )
    op.drop_column("action_plan_items", "signed_off_on")
    op.drop_column("action_plan_items", "completed_by_id")

    op.drop_index("ix_action_plans_template_id", table_name="action_plans")
    op.drop_constraint(
        "fk_action_plans_template_id_action_plans", "action_plans", type_="foreignkey"
    )
    op.drop_column("action_plans", "started_on")
    op.drop_column("action_plans", "template_id")

    op.drop_index("ix_incidents_riddor_category", table_name="incidents")
    op.drop_column("incidents", "hse_reference")
    op.drop_column("incidents", "reported_to_hse_on")
    op.drop_column("incidents", "riddor_category")
    RIDDOR.drop(bind, checkfirst=True)

    op.drop_constraint(
        "fk_observations_closed_by_id_users", "observations", type_="foreignkey"
    )
    op.drop_column("observations", "closed_by_id")
