"""CRM: lead opportunity fields — confidence, projected sales date, project type, tags

Matches BuilderTrend's Lead Opportunity: a win-probability, an expected close date,
the kind of work, and free-form tags — the fields a weighted pipeline forecast and
the sales calendar hang off.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-03
"""

import sqlalchemy as sa

from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("project_type", sa.String(120)))
    op.add_column("leads", sa.Column("tags", sa.JSON()))
    op.add_column("leads", sa.Column("confidence", sa.Integer()))
    op.add_column("leads", sa.Column("projected_sales_date", sa.Date()))
    op.create_index("ix_leads_projected_sales_date", "leads", ["projected_sales_date"])


def downgrade() -> None:
    op.drop_index("ix_leads_projected_sales_date", table_name="leads")
    for col in ("projected_sales_date", "confidence", "tags", "project_type"):
        op.drop_column("leads", col)
