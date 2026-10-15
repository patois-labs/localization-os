"""vendors + external dispatch-and-wait (PROPOSAL-external-processing-step Wave A)

Revision ID: e1f4c7a9d2b6
Revises: c7e1a9f4b2d6
Create Date: 2026-08-08 12:00:00.000000

The combined vendor-review + external-processing foundation (PROPOSAL-external-
processing-step §9 decision record; PROPOSAL-vendor-human-review-handoff). Creates
THREE new tables and adds TWO nullable columns to `workflowstep`:

  vendor            — a first-class external translation/review VENDOR: connection
                      (endpoint_url + Fernet secret_enc + non-secret config), the MSA
                      rate_card (per-TM-match-band cost-per-word), status, archive.
  vendorcustomer    — the many-to-many Vendor<->Customer link (a vendor is offered on a
                      step only within a customer it is linked to). UNIQUE (vendor_id,
                      customer_id) so a double link is a no-op, never a duplicate.
  vendordispatch    — one outbound dispatch-and-wait leg: the claim_token Wave B matches
                      a return to, the persisted scoping estimate Wave B reconciles the
                      vendor's actual cost against, host-only endpoint (never the secret).

  workflowstep.vendor_id           — nullable FK: the Vendor a StepType.EXTERNAL step
                                     dispatches to. NULL on every legacy row + every non-
                                     external step -> byte-identical.
  workflowstep.external_wait_hours — nullable INT: per-step bounded-wait override before
                                     the leg escalates to a human_review pause (decision
                                     3). NULL inherits the install TTL setting.

All additive: every existing WorkflowStep backfills both columns NULL, and a run with no
external step never reads them. Hand-written (no autogenerate) off the head c7e1a9f4b2d6,
same convention as the sibling additive revisions; the workflowstep columns are added as
plain nullable Integers (the SQLite-batch idiom the glossary_id / key_id revisions use --
SQLite cannot add a REFERENCES clause to an existing table without a rebuild, and the
nullability-parity guard compares column set + nullability, not FK constraints). The
app/db.py _ADDED_COLUMNS shim carries the SAME two columns and _UNIQUE_INDEXES carries
ux_vendorcustomer_vendor_customer so a non-alembic SQLite boot builds them identically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f4c7a9d2b6'
down_revision: Union[str, None] = 'c7e1a9f4b2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'vendor',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('endpoint_url', sa.String(), nullable=False, server_default=''),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('secret_enc', sa.String(), nullable=False, server_default=''),
        sa.Column('rate_card', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='new'),
        sa.Column('last_message', sa.String(), nullable=False, server_default=''),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
    )

    op.create_table(
        'vendorcustomer',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendor.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
    )
    op.create_index('ix_vendorcustomer_vendor_id', 'vendorcustomer', ['vendor_id'])
    op.create_index('ix_vendorcustomer_customer_id', 'vendorcustomer', ['customer_id'])
    # At most one link per (vendor, customer) — a double link is a no-op, never a dup.
    op.create_index('ux_vendorcustomer_vendor_customer', 'vendorcustomer',
                    ['vendor_id', 'customer_id'], unique=True)

    op.create_table(
        'vendordispatch',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('workflow_run_id', sa.Integer(), nullable=False),
        sa.Column('step_id', sa.Integer(), nullable=True),
        sa.Column('step_key', sa.String(), nullable=False, server_default=''),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('target_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('claim_token', sa.String(), nullable=False, server_default=''),
        sa.Column('contract_version', sa.String(), nullable=False, server_default=''),
        sa.Column('endpoint_host', sa.String(), nullable=False, server_default=''),
        sa.Column('status', sa.String(), nullable=False, server_default='dispatched'),
        sa.Column('segment_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('word_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimate', sa.JSON(), nullable=True),
        sa.Column('estimate_cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('estimate_currency', sa.String(), nullable=False, server_default='USD'),
        sa.Column('actual_cost_usd', sa.Float(), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('returned_at', sa.DateTime(), nullable=True),
        sa.Column('escalated_at', sa.DateTime(), nullable=True),
        sa.Column('org_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendor.id']),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflowrun.id']),
        sa.ForeignKeyConstraint(['step_id'], ['workflowstep.id']),
        sa.ForeignKeyConstraint(['document_id'], ['document.id']),
        sa.ForeignKeyConstraint(['org_id'], ['organization.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
    )
    op.create_index('ix_vendordispatch_vendor_id', 'vendordispatch', ['vendor_id'])
    op.create_index('ix_vendordispatch_workflow_run_id', 'vendordispatch',
                    ['workflow_run_id'])
    op.create_index('ix_vendordispatch_claim_token', 'vendordispatch', ['claim_token'])

    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vendor_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('external_wait_hours', sa.Integer(), nullable=True))
    # No backfill: NULL already reads as "no vendor / inherit the install TTL" for every
    # existing (non-external) step.


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('external_wait_hours')
        batch_op.drop_column('vendor_id')
    op.drop_index('ix_vendordispatch_claim_token', table_name='vendordispatch')
    op.drop_index('ix_vendordispatch_workflow_run_id', table_name='vendordispatch')
    op.drop_index('ix_vendordispatch_vendor_id', table_name='vendordispatch')
    op.drop_table('vendordispatch')
    op.drop_index('ux_vendorcustomer_vendor_customer', table_name='vendorcustomer')
    op.drop_index('ix_vendorcustomer_customer_id', table_name='vendorcustomer')
    op.drop_index('ix_vendorcustomer_vendor_id', table_name='vendorcustomer')
    op.drop_table('vendorcustomer')
    op.drop_table('vendor')
