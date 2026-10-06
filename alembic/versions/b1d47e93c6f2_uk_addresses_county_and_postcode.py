"""UK addresses: county instead of state, and a postcode on the lot

Revision ID: b1d47e93c6f2
Revises: a4c81b2e5d30
Create Date: 2026-09-26

Corrects a term that was wrong for the market. `communities.state` is American;
the UK equivalent is the county, and leaving the column called `state` would have
meant every screen, schema and report carrying the wrong word forever because
nobody wanted to write this migration later.

Renamed rather than dropped and re-added, so existing values survive. They are
currently two-letter US state codes in seed data only, which the seeds replace.

`lots.postcode` is new. A UK house address is not usable without one - it is what
a delivery, a surveyor and a building inspector all navigate by - and the Lead
model already collected one, so the house the lead buys should too.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1d47e93c6f2"
down_revision: str | None = "a4c81b2e5d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("communities", "state", new_column_name="county")
    op.add_column("lots", sa.Column("postcode", sa.String(length=20), nullable=True))
    # Searched when somebody hunts for a plot by address, which is how a site
    # manager looks one up.
    op.create_index(op.f("ix_lots_postcode"), "lots", ["postcode"])


def downgrade() -> None:
    op.drop_index(op.f("ix_lots_postcode"), table_name="lots")
    op.drop_column("lots", "postcode")
    op.alter_column("communities", "county", new_column_name="state")
