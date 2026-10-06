"""automated lead intake: sources and raw event log

Revision ID: a4c81b2e5d30
Revises: c7f6da10f708
Create Date: 2026-09-26

Hand-written, though this one is mostly new tables so there was less to correct.
Two choices worth recording:

  * `kind` and `status` are VARCHAR, not database enums, unlike the rest of the
    schema. The set of connectors grows every time one is added, and a Postgres
    enum cannot lose a value and needs an out-of-transaction migration to gain
    one. Lead statuses are a business rule and earn an enum; a connector list is
    a backlog and does not.
  * `payload` is JSONB on PostgreSQL and JSON on SQLite, matching the model's
    variant, so the test suite can run against SQLite unchanged.

The index on `key_hash` is the one that matters for the public endpoint: every
inbound webhook resolves its channel through it, unauthenticated, so it must not
be a table scan.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a4c81b2e5d30"
down_revision: str | None = "c7f6da10f708"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_COLUMN = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "intake_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_hint", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("signing_secret", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_community_id", sa.Uuid(), nullable=True),
        sa.Column("default_campaign", sa.String(length=200), nullable=True),
        sa.Column("default_realtor_id", sa.Uuid(), nullable=True),
        sa.Column("lead_source", sa.String(length=32), nullable=False, server_default="website"),
        sa.Column("auto_assign", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("field_map", JSON_COLUMN, nullable=True),
        sa.Column("honeypot_field", sa.String(length=64), nullable=True),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_id", sa.Uuid(), nullable=True),
        sa.Column("archive_reason", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["default_community_id"], ["communities.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["default_realtor_id"], ["realtors.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["archived_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_intake_sources_tenant_code"),
    )
    op.create_index(op.f("ix_intake_sources_tenant_id"), "intake_sources", ["tenant_id"])
    op.create_index(op.f("ix_intake_sources_code"), "intake_sources", ["code"])
    op.create_index(op.f("ix_intake_sources_kind"), "intake_sources", ["kind"])
    op.create_index(op.f("ix_intake_sources_is_active"), "intake_sources", ["is_active"])
    op.create_index(op.f("ix_intake_sources_is_archived"), "intake_sources", ["is_archived"])
    # The public endpoint's only lookup, on an unauthenticated route. Not optional.
    op.create_index(op.f("ix_intake_sources_key_hash"), "intake_sources", ["key_hash"])

    op.create_table(
        "intake_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="accepted"),
        sa.Column("payload", JSON_COLUMN, nullable=False),
        sa.Column("remote_ip", sa.String(length=64), nullable=True),
        sa.Column("external_ref", sa.String(length=200), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["intake_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_intake_events_tenant_id"), "intake_events", ["tenant_id"])
    op.create_index(op.f("ix_intake_events_source_id"), "intake_events", ["source_id"])
    op.create_index(op.f("ix_intake_events_status"), "intake_events", ["status"])
    op.create_index(op.f("ix_intake_events_lead_id"), "intake_events", ["lead_id"])
    op.create_index(op.f("ix_intake_events_remote_ip"), "intake_events", ["remote_ip"])
    # The idempotency check: "have we already had this reference from this
    # channel". Portals retry, so this runs on a large share of requests.
    op.create_index(
        "ix_intake_events_source_ref", "intake_events", ["source_id", "external_ref"]
    )


def downgrade() -> None:
    op.drop_index("ix_intake_events_source_ref", table_name="intake_events")
    for col in ("remote_ip", "lead_id", "status", "source_id", "tenant_id"):
        op.drop_index(op.f(f"ix_intake_events_{col}"), table_name="intake_events")
    op.drop_table("intake_events")

    for col in ("key_hash", "is_archived", "is_active", "kind", "code", "tenant_id"):
        op.drop_index(op.f(f"ix_intake_sources_{col}"), table_name="intake_sources")
    op.drop_table("intake_sources")
