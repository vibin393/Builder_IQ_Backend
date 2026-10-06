"""Reconcile each plot's sales status with the home on it

Found on the local demo data: a plot still "sold" whose home had been handed over, and
the mirror case is possible too (a home with a buyer on a plot still "available"). Both
come from rows written before the job lifecycle kept the plot in step. Data only.

    home handed over (closed / warranty)  -> plot closed
    home has a buyer, plot not yet sold   -> plot sold (and any stale hold cleared)

Revision ID: b8f2d6e59a37
Revises: a7e1c5d48f26
Create Date: 2026-09-28
"""

from alembic import op

revision = "b8f2d6e59a37"
down_revision = "a7e1c5d48f26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE lots SET status = 'closed', hold_until = NULL, hold_reason = NULL,
               held_on = NULL, held_by_id = NULL, held_for_lead_id = NULL
        WHERE status::text <> 'closed' AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.lot_id = lots.id
              AND j.status::text IN ('closed', 'warranty'))
        """
    )
    op.execute(
        """
        UPDATE lots SET status = 'sold', hold_until = NULL, hold_reason = NULL,
               held_on = NULL, held_by_id = NULL, held_for_lead_id = NULL
        WHERE status::text IN ('available', 'held', 'not_released') AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.lot_id = lots.id
              AND j.status::text NOT IN ('cancelled', 'closed', 'warranty')
              AND j.sold_date IS NOT NULL)
        """
    )


def downgrade() -> None:
    # A correction of bad data; there is nothing meaningful to put back.
    pass
