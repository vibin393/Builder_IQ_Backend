"""Documents: status lifecycle, expiry tracking, references and supersede chain

The document register could record a name, category and storage key, and nothing that
made it more than a spreadsheet: no status (so a mistaken or replaced entry stayed on
the list forever), no expiry (so a permit or insurance certificate could lapse with no
warning — the exact failure the register exists to prevent), no issuing reference to
find it by, and no link from a superseded version to the one that replaced it.

  * `documents` gains `status` (draft/active/superseded/archived), `reference`,
    `issued_on`, `expires_on` and `superseded_by_id`.
  * `document_category` gains an 'insurance' value.

Existing rows keep working: every current document defaults to 'active', and the new
date and reference columns are null until filled.

Revision ID: e3b9c52a7f18
Revises: d2a8b41f6c07
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e3b9c52a7f18"
down_revision = "d2a8b41f6c07"
branch_labels = None
depends_on = None


DOCUMENT_STATUS = postgresql.ENUM(
    "draft", "active", "superseded", "archived", name="document_status",
)


def _enum_ref(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    DOCUMENT_STATUS.create(bind, checkfirst=True)

    # New category value (PG16 allows adding an enum value in a transaction).
    op.execute("ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'insurance'")

    op.add_column(
        "documents",
        sa.Column("status", _enum_ref("document_status"),
                  nullable=False, server_default="active"),
    )
    op.add_column("documents", sa.Column("reference", sa.String(120), nullable=True))
    op.add_column("documents", sa.Column("superseded_by_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("issued_on", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("expires_on", sa.Date(), nullable=True))
    op.create_foreign_key(
        "fk_documents_superseded_by_id",
        "documents", "documents", ["superseded_by_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_expires_on", "documents", ["expires_on"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_documents_expires_on", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_constraint("fk_documents_superseded_by_id", "documents", type_="foreignkey")
    for col in ("expires_on", "issued_on", "superseded_by_id", "reference", "status"):
        op.drop_column("documents", col)

    postgresql.ENUM(name="document_status", create_type=False).drop(bind, checkfirst=True)
    # 'insurance' stays on document_category: PostgreSQL cannot drop a single enum value.
