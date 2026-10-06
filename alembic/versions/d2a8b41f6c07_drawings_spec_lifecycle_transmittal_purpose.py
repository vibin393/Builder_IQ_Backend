"""Drawings: spec supersede lifecycle, transmittal purpose and spec transmittals

The drawing register could supersede a drawing set but not a specification (a spec's
`is_current` flag was set on create and never changed, and nothing linked a superseded
spec to the one that replaced it), and a transmittal could not say *why* it was sent
(for construction vs for review carry very different obligations) or carry a spec rather
than a drawing set.

  * `specifications` gains `status` (draft/issued/superseded) and `superseded_by_id`, so
    a spec runs the same supersede-not-overwrite lifecycle as a drawing set.
  * `transmittals` gains `purpose` and `specification_id`.

Existing rows keep working: a current spec reads as 'issued' and a superseded one as
'superseded' (from the `is_current` flag it already carried), and every existing
transmittal defaults to 'for_construction'.

Revision ID: d2a8b41f6c07
Revises: c1f7a9d34e52
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d2a8b41f6c07"
down_revision = "c1f7a9d34e52"
branch_labels = None
depends_on = None


SPEC_STATUS = postgresql.ENUM("draft", "issued", "superseded", name="spec_status")
TRANSMITTAL_PURPOSE = postgresql.ENUM(
    "for_construction", "for_review", "for_approval", "for_information", "for_record",
    name="transmittal_purpose",
)


def _enum_ref(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    for enum in (SPEC_STATUS, TRANSMITTAL_PURPOSE):
        enum.create(bind, checkfirst=True)

    # --- specifications -------------------------------------------------------
    op.add_column(
        "specifications",
        sa.Column("status", _enum_ref("spec_status"), nullable=False, server_default="issued"),
    )
    op.add_column(
        "specifications", sa.Column("superseded_by_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_specifications_superseded_by_id",
        "specifications", "specifications", ["superseded_by_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_specifications_status", "specifications", ["status"])
    op.execute("UPDATE specifications SET status = 'superseded' WHERE is_current = false")

    # --- transmittals ---------------------------------------------------------
    op.add_column(
        "transmittals",
        sa.Column("purpose", _enum_ref("transmittal_purpose"),
                  nullable=False, server_default="for_construction"),
    )
    op.add_column(
        "transmittals", sa.Column("specification_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_transmittals_specification_id",
        "transmittals", "specifications", ["specification_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_transmittals_purpose", "transmittals", ["purpose"])
    op.create_index("ix_transmittals_specification_id", "transmittals", ["specification_id"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_transmittals_specification_id", table_name="transmittals")
    op.drop_index("ix_transmittals_purpose", table_name="transmittals")
    op.drop_constraint("fk_transmittals_specification_id", "transmittals", type_="foreignkey")
    op.drop_column("transmittals", "specification_id")
    op.drop_column("transmittals", "purpose")

    op.drop_index("ix_specifications_status", table_name="specifications")
    op.drop_constraint("fk_specifications_superseded_by_id", "specifications", type_="foreignkey")
    op.drop_column("specifications", "superseded_by_id")
    op.drop_column("specifications", "status")

    for name in ("transmittal_purpose", "spec_status"):
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)
