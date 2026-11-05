"""Sales: lead proposals, and lead map coordinates

A priced offer sent to a prospect, and optional geocoded coordinates on a lead so
it can be plotted on the lead map.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-11-02
"""

import sqlalchemy as sa

from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_proposals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("lead_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.Enum("draft", "sent", "accepted", "declined", "expired", name="lead_proposal_status"),
                  nullable=False, server_default="draft", index=True),
        sa.Column("sent_on", sa.Date()),
        sa.Column("valid_until", sa.Date(), index=True),
        sa.Column("decided_on", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("leads", sa.Column("latitude", sa.Numeric(9, 6)))
    op.add_column("leads", sa.Column("longitude", sa.Numeric(9, 6)))


def downgrade() -> None:
    op.drop_column("leads", "longitude")
    op.drop_column("leads", "latitude")
    op.drop_table("lead_proposals")
    sa.Enum(name="lead_proposal_status").drop(op.get_bind(), checkfirst=True)
