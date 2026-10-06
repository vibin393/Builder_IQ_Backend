"""home lifecycle: permits, holds, cancellation, and freeing a cancelled lot

Revision ID: d8a35c1f9b74
Revises: b1d47e93c6f2
Create Date: 2026-09-26

The important change here is not a new column - it is the constraint that comes
off.

`uq_jobs_lot` was a plain UNIQUE(lot_id), so once a job existed on a lot no other
job could ever be created there. That is wrong in exactly the case it matters: a
buyer withdraws, the job is cancelled, and the builder wants to rebuild on the
same plot. The old model comment claimed cancelled jobs freed the lot up; nothing
implemented it. A partial unique index does, by excluding cancelled rows.

`job_status` also gains `on_hold`, which Postgres cannot add inside a transaction,
hence the autocommit block - and cannot remove, hence the downgrade leaving it.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8a35c1f9b74"
down_revision: str | None = "b1d47e93c6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOLD_REASON = sa.Enum(
    "weather", "inspection", "buyer_decision", "trade_unavailable",
    "material_shortage", "permit", "finance", "other",
    name="hold_reason",
)
CANCELLATION_REASON = sa.Enum(
    "buyer_withdrew", "finance_fell_through", "builder_withdrew",
    "lot_problem", "plan_changed", "other",
    name="cancellation_reason",
)


def upgrade() -> None:
    bind = op.get_bind()
    HOLD_REASON.create(bind, checkfirst=True)
    CANCELLATION_REASON.create(bind, checkfirst=True)

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'on_hold'")

    op.add_column("jobs", sa.Column("elevation", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("permit_number", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("permit_applied_on", sa.Date(), nullable=True))
    op.add_column("jobs", sa.Column("permit_issued_on", sa.Date(), nullable=True))
    op.add_column("jobs", sa.Column("sold_date", sa.Date(), nullable=True))
    op.add_column("jobs", sa.Column("warranty_expires_on", sa.Date(), nullable=True))

    op.add_column("jobs", sa.Column("hold_reason", HOLD_REASON, nullable=True))
    op.add_column("jobs", sa.Column("hold_note", sa.String(length=500), nullable=True))
    op.add_column("jobs", sa.Column("on_hold_since", sa.Date(), nullable=True))
    # NOT NULL on a table that already holds rows, so it needs a default.
    op.add_column(
        "jobs", sa.Column("days_on_hold", sa.Integer(), nullable=False, server_default="0")
    )

    op.add_column("jobs", sa.Column("cancelled_on", sa.Date(), nullable=True))
    op.add_column("jobs", sa.Column("cancellation_reason", CANCELLATION_REASON, nullable=True))
    op.add_column("jobs", sa.Column("cancellation_note", sa.String(length=500), nullable=True))
    op.add_column("jobs", sa.Column("notes", sa.Text(), nullable=True))

    op.create_index(op.f("ix_jobs_permit_number"), "jobs", ["permit_number"])
    op.create_index(op.f("ix_jobs_superintendent_id"), "jobs", ["superintendent_id"])

    # The fix. Drop the absolute uniqueness, add it back excluding cancelled rows.
    op.drop_constraint("uq_jobs_lot", "jobs", type_="unique")
    op.create_index(
        "uq_jobs_live_lot",
        "jobs",
        ["lot_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
        sqlite_where=sa.text("status <> 'cancelled'"),
    )


def downgrade() -> None:
    # The plain UNIQUE(lot_id) cannot represent what this migration made possible:
    # a cancelled home and its replacement on the same plot. Rather than let
    # PostgreSQL refuse with "duplicate key value violates unique constraint" -
    # which tells an operator nothing at three in the morning - check first and
    # say exactly what is in the way.
    #
    # Nothing is deleted to make room. A downgrade that silently destroyed a home
    # would be far worse than one that stops and explains itself.
    conflicts = op.get_bind().execute(
        sa.text(
            "SELECT lot_id, count(*) AS n FROM jobs GROUP BY lot_id HAVING count(*) > 1"
        )
    ).fetchall()
    if conflicts:
        raise RuntimeError(
            f"{len(conflicts)} plot(s) hold more than one home - a cancelled build and "
            "its replacement. The constraint this downgrade restores cannot represent "
            "that, and no home will be deleted to make it fit. Move or remove the "
            "surplus jobs deliberately first: "
            "SELECT lot_id, array_agg(job_number || ':' || status) FROM jobs "
            "GROUP BY lot_id HAVING count(*) > 1;"
        )

    op.drop_index("uq_jobs_live_lot", table_name="jobs")
    op.create_unique_constraint("uq_jobs_lot", "jobs", ["lot_id"])

    op.drop_index(op.f("ix_jobs_superintendent_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_permit_number"), table_name="jobs")

    for column in (
        "notes", "cancellation_note", "cancellation_reason", "cancelled_on",
        "days_on_hold", "on_hold_since", "hold_note", "hold_reason",
        "warranty_expires_on", "sold_date", "permit_issued_on", "permit_applied_on",
        "permit_number", "elevation",
    ):
        op.drop_column("jobs", column)

    CANCELLATION_REASON.drop(op.get_bind(), checkfirst=True)
    HOLD_REASON.drop(op.get_bind(), checkfirst=True)
    # `on_hold` stays in job_status - PostgreSQL has no DROP VALUE. Any job left
    # in that state would be unreadable after a downgrade, which is one more
    # reason not to downgrade past this in anger.
