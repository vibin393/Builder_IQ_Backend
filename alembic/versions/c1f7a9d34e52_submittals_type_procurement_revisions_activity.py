"""Submittals: type, priority, procurement runway, revision history and activity log

The submittal register could raise an item, flip its status once, and bump a revision
counter — but not say what kind of submittal it was, how urgent, when the product was
needed on site (so no procurement warning), or what happened in any earlier review
round (the revision number went up and the previous decision was overwritten). This adds
all of that.

  * `submittals` gains `submittal_type`, `priority`, `approved_on`, `submitted_by_id`,
    and the procurement pair `required_on_site` / `lead_time_days`.
  * `submittal_revisions` is new: one row per completed review round, so the history of
    what was proposed and how it came back survives a revise-and-resubmit loop.
  * `submittal_activity` is new: the append-only move-by-move log.

Existing rows keep working: the new columns default (`submittal_type` 'product_data',
`priority` 'normal', `lead_time_days` 0), an already-approved submittal takes its
`approved_on` from when it was returned, and each already-returned submittal gets one
revision row and a matching activity entry so its history is not blank.

Revision ID: c1f7a9d34e52
Revises: b8e5c07d2f19
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1f7a9d34e52"
down_revision = "b8e5c07d2f19"
branch_labels = None
depends_on = None


SUBMITTAL_TYPE = postgresql.ENUM(
    "product_data", "shop_drawing", "sample", "mock_up", "certificate",
    "method_statement", "o_and_m", "warranty_doc", "other",
    name="submittal_type",
)
SUBMITTAL_ACTIVITY_KIND = postgresql.ENUM(
    "created", "submitted", "review_started", "approved", "approved_as_noted",
    "revise_and_resubmit", "rejected", "resubmitted", "reassigned", "edited",
    name="submittal_activity_kind",
)


def _enum_ref(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    for enum in (SUBMITTAL_TYPE, SUBMITTAL_ACTIVITY_KIND):
        enum.create(bind, checkfirst=True)

    # --- submittals -----------------------------------------------------------
    op.add_column(
        "submittals",
        sa.Column("submittal_type", _enum_ref("submittal_type"),
                  nullable=False, server_default="product_data"),
    )
    # priority reuses the existing comm_priority enum shared with RFIs.
    op.add_column(
        "submittals",
        sa.Column("priority", _enum_ref("comm_priority"),
                  nullable=False, server_default="normal"),
    )
    op.add_column("submittals", sa.Column("approved_on", sa.Date(), nullable=True))
    op.add_column("submittals", sa.Column("submitted_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("submittals", sa.Column("required_on_site", sa.Date(), nullable=True))
    op.add_column(
        "submittals",
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_submittals_submitted_by_id_users",
        "submittals", "users", ["submitted_by_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_submittals_submittal_type", "submittals", ["submittal_type"])
    op.create_index("ix_submittals_required_on_site", "submittals", ["required_on_site"])

    op.execute(
        "UPDATE submittals SET approved_on = returned_on "
        "WHERE status = 'approved' AND approved_on IS NULL AND returned_on IS NOT NULL"
    )

    # --- submittal_revisions --------------------------------------------------
    op.create_table(
        "submittal_revisions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("submittal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", _enum_ref("submittal_status"), nullable=False),
        sa.Column("submitted_on", sa.Date(), nullable=True),
        sa.Column("returned_on", sa.Date(), nullable=True),
        sa.Column("reviewer_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("review_notes", sa.String(2000), nullable=True),
        sa.Column("storage_key", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submittal_id"], ["submittals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_submittal_revisions_tenant_id", "submittal_revisions", ["tenant_id"])
    op.create_index("ix_submittal_revisions_submittal_id", "submittal_revisions", ["submittal_id"])

    # --- submittal_activity ---------------------------------------------------
    op.create_table(
        "submittal_activity",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("submittal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("kind", _enum_ref("submittal_activity_kind"), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submittal_id"], ["submittals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_submittal_activity_tenant_id", "submittal_activity", ["tenant_id"])
    op.create_index("ix_submittal_activity_submittal_id", "submittal_activity", ["submittal_id"])

    # Backfill the history from the one round each existing submittal already recorded,
    # so a returned submittal does not read as one that was never reviewed.
    op.execute(
        """
        INSERT INTO submittal_revisions
            (id, tenant_id, submittal_id, revision, outcome, submitted_on, returned_on,
             reviewer_id, review_notes, storage_key, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, id, revision, status, submitted_on, returned_on,
               reviewer_id, review_notes, storage_key,
               COALESCE(returned_on::timestamptz, now()), now()
          FROM submittals
         WHERE returned_on IS NOT NULL
           AND status IN ('approved', 'approved_as_noted', 'rejected', 'revise_and_resubmit')
        """
    )
    op.execute(
        """
        INSERT INTO submittal_activity
            (id, tenant_id, submittal_id, author_id, kind, detail, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, id, reviewer_id,
               status::text::submittal_activity_kind,
               'Decision recorded (backfilled from history).',
               COALESCE(returned_on::timestamptz, now()), now()
          FROM submittals
         WHERE returned_on IS NOT NULL
           AND status IN ('approved', 'approved_as_noted', 'rejected', 'revise_and_resubmit')
        """
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("submittal_activity")
    op.drop_table("submittal_revisions")

    op.drop_index("ix_submittals_required_on_site", table_name="submittals")
    op.drop_index("ix_submittals_submittal_type", table_name="submittals")
    op.drop_constraint("fk_submittals_submitted_by_id_users", "submittals", type_="foreignkey")
    for col in (
        "lead_time_days", "required_on_site", "submitted_by_id", "approved_on",
        "priority", "submittal_type",
    ):
        op.drop_column("submittals", col)

    for name in ("submittal_activity_kind", "submittal_type"):
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)
