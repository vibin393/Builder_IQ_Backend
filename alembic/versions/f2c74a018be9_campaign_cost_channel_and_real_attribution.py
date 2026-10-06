"""campaigns: cost, channel, and attribution by foreign key

Revision ID: f2c74a018be9
Revises: e5b9271ca408
Create Date: 2026-09-26

Three changes, and the last one is the one that was quietly wrong.

`cost` and `budget` did not exist, so the campaign screen could report opens and
clicks but not what they cost - and cost per lead is the only number that decides
whether to run something again.

`channel` did not exist, so campaigns were email-only while the Lead model already
recognised leads arriving from signage, social and events.

`leads.campaign_id` replaces attribution by string comparison. Leads were matched
to campaigns with `lower(leads.campaign) = lower(campaigns.code)`, so renaming a
code detached every lead that came from it, silently, with no error anywhere. The
free-text tag stays - inbound channels send whatever string was configured in them
and know nothing about our ids - and the backfill below matches the existing rows
once, by the same rule, so no attribution is lost in the move.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c74a018be9"
down_revision: str | None = "e5b9271ca408"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGN_CHANNEL = sa.Enum(
    "email", "print", "signage", "social", "portal", "event", "radio", "other",
    name="campaign_channel",
)


def upgrade() -> None:
    CAMPAIGN_CHANNEL.create(op.get_bind(), checkfirst=True)

    # `running` sits between scheduled and sent: handed to the provider, or the
    # sign is up. Postgres cannot add an enum value inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaign_status ADD VALUE IF NOT EXISTS 'running'")

    # Existing campaigns were all email, which is what the app could express.
    op.add_column(
        "campaigns",
        sa.Column("channel", CAMPAIGN_CHANNEL, nullable=False, server_default="email"),
    )
    op.create_index(op.f("ix_campaigns_channel"), "campaigns", ["channel"])

    for column in ("budget", "cost"):
        op.add_column(
            "campaigns",
            sa.Column(
                column, sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
            ),
        )

    op.add_column("campaigns", sa.Column("starts_on", sa.Date(), nullable=True))
    op.add_column("campaigns", sa.Column("ends_on", sa.Date(), nullable=True))
    op.add_column(
        "campaigns", sa.Column("cancellation_reason", sa.String(length=500), nullable=True)
    )
    op.add_column("campaigns", sa.Column("notes", sa.String(length=2000), nullable=True))

    # --- attribution ---------------------------------------------------------
    op.add_column("leads", sa.Column("campaign_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_leads_campaign_id", "leads", "campaigns", ["campaign_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_leads_campaign_id"), "leads", ["campaign_id"])

    # Backfill by the rule the report used, so nothing already attributed is lost
    # on the way across. Scoped by tenant, because two builders may both have a
    # campaign called "spring-parade" and matching across them would be worse than
    # not matching at all.
    op.execute(
        """
        UPDATE leads
           SET campaign_id = c.id
          FROM campaigns c
         WHERE c.tenant_id = leads.tenant_id
           AND leads.campaign IS NOT NULL
           AND lower(leads.campaign) = lower(c.code)
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_leads_campaign_id"), table_name="leads")
    op.drop_constraint("fk_leads_campaign_id", "leads", type_="foreignkey")
    op.drop_column("leads", "campaign_id")

    for column in ("notes", "cancellation_reason", "ends_on", "starts_on", "cost", "budget"):
        op.drop_column("campaigns", column)
    op.drop_index(op.f("ix_campaigns_channel"), table_name="campaigns")
    op.drop_column("campaigns", "channel")

    CAMPAIGN_CHANNEL.drop(op.get_bind(), checkfirst=True)
    # `running` stays in campaign_status - PostgreSQL has no DROP VALUE. Any
    # campaign left in that state would be unreadable after a downgrade.
