"""leads: realtors, qualification, rating and activity detail

Revision ID: c7f6da10f708
Revises: 1e8458ecd977
Create Date: 2026-09-26

Hand-written. Autogenerate got three things wrong here, two of them familiar:

  * every new NOT NULL column lands on tables that already hold rows, so each
    needs a server_default
  * foreign keys were emitted unnamed, so the downgrade could not drop them
  * **new**: `activity_type` changes from VARCHAR to an enum. Postgres will not
    cast a text column to an enum implicitly - it needs USING. The existing rows
    all hold 'call', which is a valid member, so the cast is safe.

`lead_source` also gains three members. Postgres cannot add enum values inside a
transaction, hence the autocommit block, and cannot remove them at all, hence the
downgrade leaving them in place.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7f6da10f708"
down_revision: str | None = "1e8458ecd977"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_SOURCES = ("signage", "social", "event")

#: The enum types this migration introduces, created explicitly below. Every
#: column reference uses create_type=False: whether ALTER TABLE ADD COLUMN also
#: emits CREATE TYPE varies between SQLAlchemy versions, and one CREATE TYPE that
#: we control beats two that we do not.
ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "contact_method": ("email", "phone", "text"),
    "financing_type": ("cash", "conventional", "fha", "va", "other"),
    "lead_rating": ("hot", "warm", "cold"),
    "activity_type": (
        "call", "email", "text", "visit", "appointment", "note",
        "status_change", "assignment",
    ),
    "activity_outcome": (
        "reached", "no_answer", "left_message", "booked", "not_interested", "none",
    ),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUM_VALUES[name], name=name, create_type=False)


CONTACT_METHOD = _enum("contact_method")
FINANCING_TYPE = _enum("financing_type")
LEAD_RATING = _enum("lead_rating")
ACTIVITY_OUTCOME = _enum("activity_outcome")

LEAD_USER_FKS = ("archived_by_id", "created_by_id", "updated_by_id")


def upgrade() -> None:
    # --- new enum types -----------------------------------------------------
    for name, values in ENUM_VALUES.items():
        members = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({members})")

    # Extra lead sources. Cannot run inside a transaction.
    with op.get_context().autocommit_block():
        for value in NEW_SOURCES:
            op.execute(f"ALTER TYPE lead_source ADD VALUE IF NOT EXISTS '{value}'")

    # --- realtors -----------------------------------------------------------
    op.create_table(
        "realtors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("brokerage", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("licence_number", sa.String(length=64), nullable=True),
        sa.Column(
            "commission_percent",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("notes", sa.String(length=2000), nullable=True),
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
        sa.ForeignKeyConstraint(["archived_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_realtors_tenant_code"),
    )
    op.create_index(op.f("ix_realtors_tenant_id"), "realtors", ["tenant_id"])
    op.create_index(op.f("ix_realtors_code"), "realtors", ["code"])
    op.create_index(op.f("ix_realtors_brokerage"), "realtors", ["brokerage"])
    op.create_index(op.f("ix_realtors_email"), "realtors", ["email"])
    op.create_index(op.f("ix_realtors_is_archived"), "realtors", ["is_archived"])

    # --- leads --------------------------------------------------------------
    nullable_columns = [
        ("mobile", sa.String(length=40)),
        ("best_time_to_call", sa.String(length=100)),
        ("address", sa.String(length=300)),
        ("city", sa.String(length=120)),
        ("postcode", sa.String(length=20)),
        ("referred_by", sa.String(length=200)),
        ("price_min", sa.Numeric(precision=12, scale=2)),
        ("price_max", sa.Numeric(precision=12, scale=2)),
        ("bedrooms_needed", sa.Integer()),
        ("desired_move_in", sa.Date()),
        ("lender_name", sa.String(length=200)),
        ("last_contacted_on", sa.Date()),
        ("converted_on", sa.Date()),
        ("lost_reason", sa.String(length=500)),
        ("lost_to", sa.String(length=200)),
        ("lost_on", sa.Date()),
        ("archived_at", sa.DateTime(timezone=True)),
        ("archive_reason", sa.String(length=500)),
    ]
    for name, coltype in nullable_columns:
        op.add_column("leads", sa.Column(name, coltype, nullable=True))

    op.add_column("leads", sa.Column("preferred_contact", CONTACT_METHOD, nullable=True))
    op.add_column("leads", sa.Column("financing_type", FINANCING_TYPE, nullable=True))
    op.add_column("leads", sa.Column("rating", LEAD_RATING, nullable=True))

    # Existing leads predate these, and false is the honest default: we do not
    # know that they are prequalified, so they are not.
    for flag in ("is_prequalified", "has_home_to_sell", "is_archived"):
        op.add_column(
            "leads", sa.Column(flag, sa.Boolean(), nullable=False, server_default=sa.false())
        )

    for name in ("realtor_id", "plan_id", *LEAD_USER_FKS):
        op.add_column("leads", sa.Column(name, sa.Uuid(), nullable=True))

    op.create_foreign_key(
        "fk_leads_realtor_id", "leads", "realtors", ["realtor_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_leads_plan_id", "leads", "plans", ["plan_id"], ["id"], ondelete="SET NULL"
    )
    for column in LEAD_USER_FKS:
        op.create_foreign_key(
            f"fk_leads_{column}", "leads", "users", [column], ["id"], ondelete="SET NULL"
        )

    for column in ("phone", "campaign", "realtor_id", "community_id", "rating",
                   "next_follow_up", "is_archived"):
        op.create_index(op.f(f"ix_leads_{column}"), "leads", [column])

    # --- lead activities ----------------------------------------------------
    # VARCHAR -> enum needs an explicit cast; Postgres will not do it for us.
    op.execute(
        "ALTER TABLE lead_activities "
        "ALTER COLUMN activity_type TYPE activity_type "
        "USING activity_type::activity_type"
    )
    op.add_column(
        "lead_activities",
        sa.Column("outcome", ACTIVITY_OUTCOME, nullable=False, server_default="none"),
    )
    op.add_column(
        "lead_activities",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(op.f("ix_lead_activities_activity_type"), "lead_activities", ["activity_type"])
    op.create_index(op.f("ix_lead_activities_occurred_on"), "lead_activities", ["occurred_on"])


def downgrade() -> None:
    op.drop_index(op.f("ix_lead_activities_occurred_on"), table_name="lead_activities")
    op.drop_index(op.f("ix_lead_activities_activity_type"), table_name="lead_activities")
    op.drop_column("lead_activities", "is_system")
    op.drop_column("lead_activities", "outcome")
    op.execute(
        "ALTER TABLE lead_activities "
        "ALTER COLUMN activity_type TYPE VARCHAR(40) USING activity_type::text"
    )

    for column in ("phone", "campaign", "realtor_id", "community_id", "rating",
                   "next_follow_up", "is_archived"):
        op.drop_index(op.f(f"ix_leads_{column}"), table_name="leads")
    for column in LEAD_USER_FKS:
        op.drop_constraint(f"fk_leads_{column}", "leads", type_="foreignkey")
    op.drop_constraint("fk_leads_plan_id", "leads", type_="foreignkey")
    op.drop_constraint("fk_leads_realtor_id", "leads", type_="foreignkey")

    for name in (
        "updated_by_id", "created_by_id", "archived_by_id", "archive_reason", "archived_at",
        "is_archived", "has_home_to_sell", "is_prequalified", "rating", "financing_type",
        "preferred_contact", "plan_id", "realtor_id", "lost_on", "lost_to", "lost_reason",
        "converted_on", "last_contacted_on", "lender_name", "desired_move_in",
        "bedrooms_needed", "price_max", "price_min", "referred_by", "postcode", "city",
        "address", "best_time_to_call", "mobile",
    ):
        op.drop_column("leads", name)

    op.drop_table("realtors")

    # activity_type is dropped last only for readability - by this point the
    # column has already been converted back to VARCHAR, so nothing depends on it.
    for name in ENUM_VALUES:
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # The three added lead_source values stay - Postgres has no DROP VALUE.
