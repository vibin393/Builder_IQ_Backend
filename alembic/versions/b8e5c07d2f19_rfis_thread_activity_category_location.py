"""RFIs: response thread, activity log, category, location and spec reference

The RFI module could raise a question and record a single answer string, and nothing
between the two. It had no thread (so several parties could not weigh in and no answer
could be marked official over the others), no audit trail (so who moved the ball, and
when, was lost), and no classification beyond priority (so the log could not be read by
discipline, by location, or against the drawing detail it hung off).

  * `rfis` gains `category`, `location`, `spec_reference` and `closed_on`.
  * `rfi_responses` is new: the thread, with one row per response, a `court` naming who
    it speaks for, and `is_official` marking the one that answered the question.
  * `rfi_activity` is new: the append-only history (raised / responded / answered /
    reassigned / reopened / closed / voided / change_order).

Existing rows are read honestly rather than flattened: a closed RFI takes its
`closed_on` from when it was answered, every already-answered RFI has its `answer`
mirrored into an official response row so its thread is not empty, and each answered
RFI gets a single 'answered' activity entry so its history does not start blank.

Revision ID: b8e5c07d2f19
Revises: a7d3f1c92b64
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8e5c07d2f19"
down_revision = "a7d3f1c92b64"
branch_labels = None
depends_on = None


RFI_CATEGORY = postgresql.ENUM(
    "design", "structural", "architectural", "mep", "civils", "coordination",
    "site_condition", "materials", "building_control", "other",
    name="rfi_category",
)
RFI_ACTIVITY_KIND = postgresql.ENUM(
    "raised", "responded", "answered", "reassigned", "edited", "reopened",
    "closed", "voided", "change_order",
    name="rfi_activity_kind",
)


def _enum_ref(name: str) -> postgresql.ENUM:
    """Reference an already-created type without trying to create it again."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    for enum in (RFI_CATEGORY, RFI_ACTIVITY_KIND):
        enum.create(bind, checkfirst=True)

    # --- rfis -----------------------------------------------------------------
    op.add_column(
        "rfis",
        sa.Column("category", _enum_ref("rfi_category"),
                  nullable=False, server_default="other"),
    )
    op.add_column("rfis", sa.Column("location", sa.String(200), nullable=True))
    op.add_column("rfis", sa.Column("spec_reference", sa.String(120), nullable=True))
    op.add_column("rfis", sa.Column("closed_on", sa.Date(), nullable=True))
    op.create_index("ix_rfis_category", "rfis", ["category"])

    # A closed RFI's close date is, at worst, the day it was answered - honest enough
    # for rows that predate the column.
    op.execute(
        "UPDATE rfis SET closed_on = answered_on "
        "WHERE status = 'closed' AND closed_on IS NULL AND answered_on IS NOT NULL"
    )

    # --- rfi_responses --------------------------------------------------------
    op.create_table(
        "rfi_responses",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rfi_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("court", _enum_ref("ball_in_court"),
                  nullable=False, server_default="builder"),
        sa.Column("body", sa.String(4000), nullable=False),
        sa.Column("is_official", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost_impact", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("schedule_impact_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rfi_id"], ["rfis.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_rfi_responses_tenant_id", "rfi_responses", ["tenant_id"])
    op.create_index("ix_rfi_responses_rfi_id", "rfi_responses", ["rfi_id"])

    # --- rfi_activity ---------------------------------------------------------
    op.create_table(
        "rfi_activity",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rfi_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("kind", _enum_ref("rfi_activity_kind"), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rfi_id"], ["rfis.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_rfi_activity_tenant_id", "rfi_activity", ["tenant_id"])
    op.create_index("ix_rfi_activity_rfi_id", "rfi_activity", ["rfi_id"])

    # Backfill the thread and the history from what the rows already say, so an RFI
    # answered before this migration does not read as one nobody ever replied to.
    op.execute(
        """
        INSERT INTO rfi_responses
            (id, tenant_id, rfi_id, author_id, court, body, is_official,
             cost_impact, schedule_impact_days, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, id, answered_by_id, 'architect', answer, true,
               cost_impact, schedule_impact_days,
               COALESCE(answered_on::timestamptz, now()), now()
          FROM rfis
         WHERE answer IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO rfi_activity
            (id, tenant_id, rfi_id, author_id, kind, detail, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, id, answered_by_id, 'answered',
               'Answer recorded (backfilled from history).',
               COALESCE(answered_on::timestamptz, now()), now()
          FROM rfis
         WHERE answer IS NOT NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("rfi_activity")
    op.drop_table("rfi_responses")

    op.drop_index("ix_rfis_category", table_name="rfis")
    for col in ("closed_on", "spec_reference", "location", "category"):
        op.drop_column("rfis", col)

    for name in ("rfi_activity_kind", "rfi_category"):
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)
