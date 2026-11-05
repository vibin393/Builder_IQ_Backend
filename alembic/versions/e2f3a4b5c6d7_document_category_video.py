"""Files: add a 'video' document category

BuilderTrend's Files area separates Documents, Photos and Videos. The register
already had 'photo'; this adds 'video' so a walkthrough or a defect clip is a first
-class file rather than an 'other'.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-11-02
"""

from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE cannot run inside the migration's transaction on PostgreSQL.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'video'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum label. Removing it would mean recreating
    # the type and rewriting every column that uses it; the stray label is harmless,
    # so this is deliberately a no-op.
    pass
