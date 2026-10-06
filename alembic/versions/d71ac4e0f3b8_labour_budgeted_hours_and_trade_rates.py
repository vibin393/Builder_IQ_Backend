"""Labour: budgeted hours, trade rates, timecard approval

The Timecard docstring has always said what this feature is for - "the budget says
a house should take so many framing hours, and this says what it actually took" -
and nothing in the schema could express the first half. A budget line carried an
amount, not hours, so the screen could only show what was spent, which is the half
you already knew.

  * `budget_lines.budgeted_hours` and `plan_cost_lines.labour_hours` make the
    target representable, and make it come from the plan rather than being typed
    in per house.
  * `vendors.labour_rate` stops the rate being retyped on every timecard.
  * `timecards.approved_by_id` gives the approval an author. The date was already
    there with nothing to set it.

All nullable. Hours mean nothing on a materials code, and a zero there would read
as "nobody was supposed to spend any time on this" - a different claim from "we do
not measure this one in hours". The backfill takes the same care: it seeds a
trade's rate from the timecards already written against them, and only where those
timecards agree with each other.

Revision ID: d71ac4e0f3b8
Revises: b62f9d174c03
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "d71ac4e0f3b8"
down_revision = "b62f9d174c03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("labour_rate", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "budget_lines", sa.Column("budgeted_hours", sa.Numeric(10, 2), nullable=True)
    )
    op.add_column(
        "plan_cost_lines", sa.Column("labour_hours", sa.Numeric(10, 2), nullable=True)
    )
    op.add_column("timecards", sa.Column("approved_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_timecards_approved_by_id_users",
        "timecards",
        "users",
        ["approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_timecards_approved_on", "timecards", ["approved_on"])

    # Seed each trade's rate from what has actually been booked against them, but
    # only where every timecard agrees. Where a trade has been booked at three
    # different rates, there is no standard rate to infer and guessing one would
    # put a number on the vendor record that nobody chose - which is worse than
    # leaving it blank, because a blank prompts somebody to fill it in.
    op.execute(
        """
        UPDATE vendors v
           SET labour_rate = r.rate
          FROM (
                SELECT vendor_id,
                       MIN(hourly_rate) AS rate
                  FROM timecards
                 WHERE vendor_id IS NOT NULL AND hourly_rate IS NOT NULL
              GROUP BY vendor_id
                HAVING COUNT(DISTINCT hourly_rate) = 1
               ) r
         WHERE v.id = r.vendor_id
        """
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Refuse rather than silently discard the targets a builder set by hand. The
    # budgeted hours on a live job are not recoverable from anything else here:
    # the plan's standard hours may have been overridden, and the amount does not
    # imply them without a rate.
    rows = bind.execute(
        sa.text(
            """
            SELECT j.job_number, cc.code, bl.budgeted_hours
              FROM budget_lines bl
              JOIN budgets b ON b.id = bl.budget_id
              JOIN jobs j ON j.id = b.job_id
              JOIN cost_codes cc ON cc.id = bl.cost_code_id
             WHERE bl.budgeted_hours IS NOT NULL
             ORDER BY j.job_number, cc.code
            """
        )
    ).fetchall()
    if rows:
        listed = "; ".join(f"{r.job_number} {r.code}: {r.budgeted_hours}h" for r in rows[:10])
        more = f" and {len(rows) - 10} more" if len(rows) > 10 else ""
        raise RuntimeError(
            f"{len(rows)} budget line(s) carry budgeted hours and this downgrade "
            f"would drop them: {listed}{more}. They are the targets every labour "
            "variance on those homes is measured against, and nothing else here "
            "implies them. Export them before downgrading."
        )

    op.drop_index("ix_timecards_approved_on", table_name="timecards")
    op.drop_constraint("fk_timecards_approved_by_id_users", "timecards", type_="foreignkey")
    op.drop_column("timecards", "approved_by_id")
    op.drop_column("plan_cost_lines", "labour_hours")
    op.drop_column("budget_lines", "budgeted_hours")
    op.drop_column("vendors", "labour_rate")
