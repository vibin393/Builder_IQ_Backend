"""field ops: verification, severity, re-inspection, one log per day

Revision ID: c3e8b47a1d95
Revises: a91d3f5c72e8
Create Date: 2026-09-26

A punch item went straight from the trade saying it was done to complete, with
nobody signing off - so a home could be handed over on a snag list the builder had
only the trade's word for. `verified_by_id` and `verified_date` record who checked.

`severity` exists so the list is not a flat wall of defects. A scuff and a cracked
soil pipe are both punch items and only one of them should stop a handover.

`reinspects_id` chains a retry to the inspection it is retrying, which is what lets
"how many times did this fail before it passed" be asked at all.

The unique constraint on `daily_logs` is the one that meets existing data: several
logs for the same house on the same day, because the seed never set a date and they
all landed on today. They are merged rather than deleted - the text of every one is
concatenated onto the earliest entry, so a record that substantiates a delay claim
keeps every word of it.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3e8b47a1d95"
down_revision: str | None = "a91d3f5c72e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUNCH_SEVERITY = sa.Enum(
    "cosmetic", "minor", "major", "blocking", name="punch_severity"
)


def upgrade() -> None:
    PUNCH_SEVERITY.create(op.get_bind(), checkfirst=True)

    # NOT NULL on a table that already holds rows. "Minor" is the honest default:
    # existing items were recorded without a severity, and calling them all
    # blocking would stop every handover in the system.
    op.add_column(
        "punch_items",
        sa.Column("severity", PUNCH_SEVERITY, nullable=False, server_default="minor"),
    )
    op.create_index(op.f("ix_punch_items_severity"), "punch_items", ["severity"])

    op.add_column("punch_items", sa.Column("verified_by_id", sa.Uuid(), nullable=True))
    op.add_column("punch_items", sa.Column("verified_date", sa.Date(), nullable=True))
    op.add_column(
        "punch_items", sa.Column("resolution_note", sa.String(length=1000), nullable=True)
    )
    op.add_column("punch_items", sa.Column("inspection_item_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_punch_items_verified_by_id", "punch_items", "users", ["verified_by_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_punch_items_inspection_item_id",
        "punch_items",
        "inspection_items",
        ["inspection_item_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("inspections", sa.Column("reinspects_id", sa.Uuid(), nullable=True))
    op.add_column(
        "inspections",
        sa.Column("fee", sa.Numeric(precision=10, scale=2), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_inspections_reinspects_id",
        "inspections",
        "inspections",
        ["reinspects_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Existing data can hold several logs for one house on one day. They are
    # merged rather than refused or deleted: a daily log is the record that
    # substantiates a delay claim, so every word of it is kept - the text is
    # concatenated onto the earliest entry and the extras go. Nothing is lost,
    # which was the actual concern behind refusing.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, job_id, log_date,
                   row_number() OVER (PARTITION BY job_id, log_date ORDER BY created_at) AS rn
              FROM daily_logs
        ),
        merged AS (
            SELECT r.job_id, r.log_date,
                   string_agg(DISTINCT d.work_performed, E'
') AS work,
                   string_agg(DISTINCT d.delays, E'
')         AS delays,
                   string_agg(DISTINCT d.notes, E'
')          AS notes
              FROM ranked r JOIN daily_logs d ON d.id = r.id
             GROUP BY r.job_id, r.log_date
            HAVING count(*) > 1
        )
        UPDATE daily_logs d
           SET work_performed = left(coalesce(m.work, d.work_performed), 4000),
               delays         = left(coalesce(m.delays, d.delays), 2000),
               notes          = left(coalesce(m.notes, d.notes), 2000)
          FROM merged m, ranked r
         WHERE d.id = r.id AND r.rn = 1
           AND r.job_id = m.job_id AND r.log_date = m.log_date
        """
    )
    op.execute(
        """
        DELETE FROM daily_logs WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY job_id, log_date ORDER BY created_at
                ) AS rn FROM daily_logs
            ) ranked WHERE rn > 1
        )
        """
    )
    op.create_unique_constraint(
        "uq_daily_logs_job_date", "daily_logs", ["job_id", "log_date"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_daily_logs_job_date", "daily_logs", type_="unique")

    op.drop_constraint("fk_inspections_reinspects_id", "inspections", type_="foreignkey")
    op.drop_column("inspections", "fee")
    op.drop_column("inspections", "reinspects_id")

    op.drop_constraint("fk_punch_items_inspection_item_id", "punch_items", type_="foreignkey")
    op.drop_constraint("fk_punch_items_verified_by_id", "punch_items", type_="foreignkey")
    for column in (
        "inspection_item_id",
        "resolution_note",
        "verified_date",
        "verified_by_id",
    ):
        op.drop_column("punch_items", column)
    op.drop_index(op.f("ix_punch_items_severity"), table_name="punch_items")
    op.drop_column("punch_items", "severity")

    PUNCH_SEVERITY.drop(op.get_bind(), checkfirst=True)
