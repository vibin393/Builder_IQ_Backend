"""retainage, progress billing, inspection checklists, punch ball-in-court, daily log photos

Revision ID: 133ec5bd35ae
Revises: 444a5b40e966
Create Date: 2026-09-24 11:38:17.676600

Hand-adjusted after autogenerate in two places:

  * the three new NOT NULL columns land on tables that already hold rows, so each
    one carries a server_default; without it the upgrade fails on any live database
  * autogenerate emitted `drop_constraint(None, ...)` in the downgrade, which cannot
    execute, so both foreign keys are named explicitly
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '133ec5bd35ae'
down_revision: str | None = '444a5b40e966'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- inspection checklists -------------------------------------------------
    op.create_table(
        'inspection_items',
        sa.Column('inspection_id', sa.Uuid(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=False),
        sa.Column(
            'result',
            sa.Enum('pending', 'pass', 'fail', 'not_applicable', name='inspection_item_result'),
            nullable=False,
        ),
        sa.Column('notes', sa.String(length=1000), nullable=True),
        sa.Column('vendor_id', sa.Uuid(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['inspection_id'], ['inspections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_inspection_items_inspection_id'), 'inspection_items', ['inspection_id'], unique=False
    )
    op.create_index(op.f('ix_inspection_items_result'), 'inspection_items', ['result'], unique=False)
    op.create_index(
        op.f('ix_inspection_items_tenant_id'), 'inspection_items', ['tenant_id'], unique=False
    )

    # --- daily log photos ------------------------------------------------------
    op.add_column('documents', sa.Column('daily_log_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_documents_daily_log_id'), 'documents', ['daily_log_id'], unique=False)
    op.create_foreign_key(
        'fk_documents_daily_log_id',
        'documents',
        'daily_logs',
        ['daily_log_id'],
        ['id'],
        ondelete='CASCADE',
    )

    # --- progress billing: invoice line -> purchase order line -----------------
    op.add_column('invoice_lines', sa.Column('purchase_order_line_id', sa.Uuid(), nullable=True))
    op.create_index(
        op.f('ix_invoice_lines_purchase_order_line_id'),
        'invoice_lines',
        ['purchase_order_line_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_invoice_lines_purchase_order_line_id',
        'invoice_lines',
        'purchase_order_lines',
        ['purchase_order_line_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # --- retainage -------------------------------------------------------------
    # Existing rows predate retainage, so they hold nothing back: 0 is both the
    # correct historical value and the correct default for new material POs.
    op.add_column(
        'invoices',
        sa.Column(
            'retainage_percent', sa.Numeric(precision=5, scale=2), nullable=False, server_default='0'
        ),
    )
    op.add_column('invoices', sa.Column('retainage_released_date', sa.Date(), nullable=True))
    op.add_column(
        'purchase_orders',
        sa.Column(
            'retainage_percent', sa.Numeric(precision=5, scale=2), nullable=False, server_default='0'
        ),
    )

    # --- punch list ball in court ---------------------------------------------
    # Existing punch items are defects owed by a trade, which is the default.
    ball_in_court = sa.Enum(
        'builder', 'trade', 'architect', 'engineer', 'buyer', 'closed', name='ball_in_court'
    )
    ball_in_court.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'punch_items',
        sa.Column('ball_in_court', ball_in_court, nullable=False, server_default='trade'),
    )
    op.create_index(
        op.f('ix_punch_items_ball_in_court'), 'punch_items', ['ball_in_court'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_punch_items_ball_in_court'), table_name='punch_items')
    op.drop_column('punch_items', 'ball_in_court')

    op.drop_column('purchase_orders', 'retainage_percent')
    op.drop_column('invoices', 'retainage_released_date')
    op.drop_column('invoices', 'retainage_percent')

    op.drop_constraint('fk_invoice_lines_purchase_order_line_id', 'invoice_lines', type_='foreignkey')
    op.drop_index(op.f('ix_invoice_lines_purchase_order_line_id'), table_name='invoice_lines')
    op.drop_column('invoice_lines', 'purchase_order_line_id')

    op.drop_constraint('fk_documents_daily_log_id', 'documents', type_='foreignkey')
    op.drop_index(op.f('ix_documents_daily_log_id'), table_name='documents')
    op.drop_column('documents', 'daily_log_id')

    op.drop_index(op.f('ix_inspection_items_tenant_id'), table_name='inspection_items')
    op.drop_index(op.f('ix_inspection_items_result'), table_name='inspection_items')
    op.drop_index(op.f('ix_inspection_items_inspection_id'), table_name='inspection_items')
    op.drop_table('inspection_items')
    sa.Enum(name='inspection_item_result').drop(op.get_bind(), checkfirst=True)
