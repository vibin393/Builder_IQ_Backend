"""design studio: options allowance, appointment and signature lifecycles

Revision ID: e5b9271ca408
Revises: d8a35c1f9b74
Create Date: 2026-09-26

`contracts.options_allowance` is the important one. A design studio is a buyer
spending a budget, and there was no budget - so the screen could list appointments
but could not answer the question the whole room is there to answer, which is
whether the selections so far are inside what the buyer has to spend.

Everything else fills in endings that were missing: an appointment that was
rescheduled or nobody turned up to, and a signature request that was declined,
voided or chased.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5b9271ca408"
down_revision: str | None = "d8a35c1f9b74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL on tables that already hold rows, so each needs a server_default.
    # Zero is the honest starting value: existing contracts were written without
    # an allowance, and inventing one would be worse than showing none.
    op.add_column(
        "contracts",
        sa.Column(
            "options_allowance",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column(
        "design_appointments",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "design_appointments", sa.Column("outcome_note", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "design_appointments", sa.Column("rescheduled_from_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_design_appointments_rescheduled_from_id",
        "design_appointments",
        "design_appointments",
        ["rescheduled_from_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "signature_requests", sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "signature_requests", sa.Column("decline_reason", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "signature_requests", sa.Column("void_reason", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "signature_requests",
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "signature_requests",
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("selections", sa.Column("appointment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_selections_appointment_id",
        "selections",
        "design_appointments",
        ["appointment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_selections_appointment_id"), "selections", ["appointment_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_selections_appointment_id"), table_name="selections")
    op.drop_constraint("fk_selections_appointment_id", "selections", type_="foreignkey")
    op.drop_column("selections", "appointment_id")

    for column in (
        "last_reminder_at",
        "reminders_sent",
        "void_reason",
        "decline_reason",
        "declined_at",
    ):
        op.drop_column("signature_requests", column)

    op.drop_constraint(
        "fk_design_appointments_rescheduled_from_id", "design_appointments", type_="foreignkey"
    )
    for column in ("rescheduled_from_id", "outcome_note", "completed_at"):
        op.drop_column("design_appointments", column)

    op.drop_column("contracts", "options_allowance")
