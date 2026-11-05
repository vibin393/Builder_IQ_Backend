"""Materials selection signoff: the homeowner's approval of a chosen material

Adds a customer-facing approval track to `selections`, kept separate from the
contractual `status`: a choice can be confirmed into the budget yet still be
awaiting the buyer's approval, and an approval carries a signature and a moment.

Revision ID: d1e2f3a4b5c6
Revises: c6a9e4f72b05
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c6a9e4f72b05"
branch_labels = None
depends_on = None

_VALUES = ("not_submitted", "awaiting", "approved", "declined")


def upgrade() -> None:
    approval = postgresql.ENUM(*_VALUES, name="selection_approval_status")
    approval.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "selections",
        sa.Column(
            "approval_status",
            sa.Enum(*_VALUES, name="selection_approval_status", create_type=False),
            nullable=False,
            server_default="not_submitted",
        ),
    )
    op.add_column("selections", sa.Column("approval_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("selections", sa.Column("approval_deadline", sa.Date(), nullable=True))
    op.add_column("selections", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("selections", sa.Column("buyer_signature", sa.String(length=200), nullable=True))
    op.add_column(
        "selections",
        sa.Column(
            "approved_by_buyer_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("buyers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("selections", sa.Column("decline_reason", sa.String(length=1000), nullable=True))
    op.add_column("selections", sa.Column("buyer_comment", sa.String(length=1000), nullable=True))
    op.create_index(
        "ix_selections_approval_status", "selections", ["approval_status"]
    )


def downgrade() -> None:
    op.drop_index("ix_selections_approval_status", table_name="selections")
    for column in (
        "buyer_comment",
        "decline_reason",
        "approved_by_buyer_id",
        "buyer_signature",
        "decided_at",
        "approval_deadline",
        "approval_requested_at",
        "approval_status",
    ):
        op.drop_column("selections", column)
    postgresql.ENUM(name="selection_approval_status").drop(op.get_bind(), checkfirst=True)
