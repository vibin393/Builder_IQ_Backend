"""Warranty: coverage, SLA, categories, appointments, notes and back-charges

The warranty module could log a claim and dispatch a trade, and nothing else. It could
not say whether the claim was even the builder's to fix (coverage), how quickly it had to
be answered (the SLA), what part of the house was at fault (category), when someone was
coming (appointments), what had been said (notes), or how much the callback cost and who
should pay it (back-charge). This adds all of that.

  * `warranty_requests` gains category and coverage (the NHBC-style decision), the SLA
    dates (acknowledged, due, closed) and the assessment/decline narrative.
  * `service_orders` gains not-to-exceed, actual cost and the back-charge flag - the
    figures that turn "which trade calls us back" into "how much that trade owes us".
  * `warranty_appointments` and `warranty_notes` are new: the visit calendar and the
    activity log, the latter split into internal and homeowner-visible entries.
  * `warranty_status` gains 'acknowledged' and 'closed' so the queue reflects the real
    lifecycle rather than jumping from triaged to resolved.

Everything on the existing rows keeps working: the new columns default (category 'other',
coverage 'pending', back_charge false) so a claim logged before this migration reads as
un-triaged rather than mis-triaged.

Revision ID: f4a2c9d1e880
Revises: d71ac4e0f3b8
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f4a2c9d1e880"
down_revision = "d71ac4e0f3b8"
branch_labels = None
depends_on = None


WARRANTY_CATEGORY = postgresql.ENUM(
    "structural", "roofing", "plumbing", "electrical", "heating", "exterior",
    "interior", "joinery", "appliance", "damp", "drainage", "landscaping", "other",
    name="warranty_category",
)
WARRANTY_COVERAGE = postgresql.ENUM(
    "pending", "in_warranty", "out_of_warranty", "goodwill", name="warranty_coverage",
)
APPOINTMENT_KIND = postgresql.ENUM(
    "assessment", "service_visit", "inspection", "follow_up",
    name="warranty_appointment_kind",
)
APPOINTMENT_STATUS = postgresql.ENUM(
    "scheduled", "completed", "cancelled", "no_show",
    name="warranty_appointment_status",
)
NOTE_VISIBILITY = postgresql.ENUM("internal", "homeowner", name="note_visibility")


def _enum_ref(name: str) -> postgresql.ENUM:
    """Reference an already-created type without trying to create it again."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    for enum in (
        WARRANTY_CATEGORY, WARRANTY_COVERAGE, APPOINTMENT_KIND,
        APPOINTMENT_STATUS, NOTE_VISIBILITY,
    ):
        enum.create(bind, checkfirst=True)

    # New lifecycle statuses on the existing enum (PG16 allows this in a transaction;
    # the values are not used in this migration, only made available).
    op.execute("ALTER TYPE warranty_status ADD VALUE IF NOT EXISTS 'acknowledged'")
    op.execute("ALTER TYPE warranty_status ADD VALUE IF NOT EXISTS 'closed'")

    # --- warranty_requests ----------------------------------------------------
    op.add_column(
        "warranty_requests",
        sa.Column("category", _enum_ref("warranty_category"),
                  nullable=False, server_default="other"),
    )
    op.add_column(
        "warranty_requests",
        sa.Column("coverage", _enum_ref("warranty_coverage"),
                  nullable=False, server_default="pending"),
    )
    op.add_column("warranty_requests", sa.Column("reported_via", sa.String(20), nullable=True))
    op.add_column("warranty_requests", sa.Column("acknowledged_date", sa.Date(), nullable=True))
    op.add_column("warranty_requests", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("warranty_requests", sa.Column("closed_date", sa.Date(), nullable=True))
    op.add_column("warranty_requests", sa.Column("assessment_notes", sa.String(2000), nullable=True))
    op.add_column("warranty_requests", sa.Column("decline_reason", sa.String(500), nullable=True))
    op.create_index("ix_warranty_requests_category", "warranty_requests", ["category"])
    op.create_index("ix_warranty_requests_coverage", "warranty_requests", ["coverage"])
    op.create_index("ix_warranty_requests_due_date", "warranty_requests", ["due_date"])

    # Backfill the SLA due date for open claims so the overdue queue is meaningful from
    # day one. Same policy the service applies: emergency 1 day, high 3, normal 14, low 30.
    op.execute(
        """
        UPDATE warranty_requests
           SET due_date = submitted_date + (CASE priority
                   WHEN 'emergency' THEN INTERVAL '1 day'
                   WHEN 'high'      THEN INTERVAL '3 days'
                   WHEN 'low'       THEN INTERVAL '30 days'
                   ELSE                  INTERVAL '14 days'
               END)
         WHERE submitted_date IS NOT NULL AND due_date IS NULL
        """
    )

    # --- service_orders -------------------------------------------------------
    op.add_column("service_orders", sa.Column("nte_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("service_orders", sa.Column("cost_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "service_orders",
        sa.Column("back_charge", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # --- warranty_appointments ------------------------------------------------
    op.create_table(
        "warranty_appointments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", _enum_ref("warranty_appointment_kind"),
                  nullable=False, server_default="service_visit"),
        sa.Column("status", _enum_ref("warranty_appointment_status"),
                  nullable=False, server_default="scheduled"),
        sa.Column("scheduled_for", sa.Date(), nullable=False),
        sa.Column("slot", sa.String(40), nullable=True),
        sa.Column("vendor_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("assignee_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("outcome", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["warranty_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_warranty_appointments_tenant_id", "warranty_appointments", ["tenant_id"])
    op.create_index("ix_warranty_appointments_request_id", "warranty_appointments", ["request_id"])
    op.create_index("ix_warranty_appointments_status", "warranty_appointments", ["status"])
    op.create_index("ix_warranty_appointments_scheduled_for", "warranty_appointments", ["scheduled_for"])

    # --- warranty_notes -------------------------------------------------------
    op.create_table(
        "warranty_notes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("visibility", _enum_ref("note_visibility"),
                  nullable=False, server_default="internal"),
        sa.Column("body", sa.String(4000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["warranty_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_warranty_notes_tenant_id", "warranty_notes", ["tenant_id"])
    op.create_index("ix_warranty_notes_request_id", "warranty_notes", ["request_id"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("warranty_notes")
    op.drop_table("warranty_appointments")

    op.drop_column("service_orders", "back_charge")
    op.drop_column("service_orders", "cost_amount")
    op.drop_column("service_orders", "nte_amount")

    op.drop_index("ix_warranty_requests_due_date", table_name="warranty_requests")
    op.drop_index("ix_warranty_requests_coverage", table_name="warranty_requests")
    op.drop_index("ix_warranty_requests_category", table_name="warranty_requests")
    for col in (
        "decline_reason", "assessment_notes", "closed_date", "due_date",
        "acknowledged_date", "reported_via", "coverage", "category",
    ):
        op.drop_column("warranty_requests", col)

    for name in (
        "note_visibility", "warranty_appointment_status", "warranty_appointment_kind",
        "warranty_coverage", "warranty_category",
    ):
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)

    # 'acknowledged' and 'closed' remain on warranty_status: PostgreSQL cannot drop a
    # single enum value, and rebuilding the type would risk the live rows using it.
