"""Bidding: an estimate to level bids against

The bid board could show the low bid but had nothing to judge it by — no sense of
whether the low bid was good value or just the least-bad of a thin field. A package now
carries the builder's own `estimate`, so bids can be levelled against it and awarding
shows the saving (or overrun) against what the work was expected to cost.

Existing packages keep working: `estimate` is null until filled, and the levelling
figures that need it simply read as unavailable.

Revision ID: a17e9c3b8d24
Revises: f4c1a06d5b93
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "a17e9c3b8d24"
down_revision = "f4c1a06d5b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bid_packages", sa.Column("estimate", sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("bid_packages", "estimate")
