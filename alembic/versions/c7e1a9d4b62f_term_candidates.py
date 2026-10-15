"""term_candidate table (term-extraction review grid)

Revision ID: c7e1a9d4b62f
Revises: fa3d9c1e7b52
Create Date: 2026-07-21 12:00:00.000000

Adds the `termcandidate` table (feat/term-extraction) — the upstream half of the
term-extraction loop. An extraction run over a document proposes candidate terms
(LLM extract -> LLM precision-prune -> deterministic sanity filters) that land
PENDING in a per-glossary review grid; a reviewer accepts one into the owning
Glossary as a real GlossaryEntry, or drops it. Accepted/dropped decisions PERSIST so
re-extraction never re-proposes an already-decided term (the per-glossary
noise-control backbone).

  termcandidate (new table)
    glossary_id         FK glossary.id (NOT NULL, indexed) — the owning glossary
    source_term         the proposed source term (NOT NULL)
    proposed_target     the LLM-proposed translation for the target locale (NULLABLE —
                        NULL when the offline mock / a spend-degraded run produced no
                        translation)
    source_locale       run source locale (server_default '')
    target_locale       run target locale (server_default '')
    frequency           occurrences in the source document (server_default '0')
    context_snippet     a short in-context concordance snippet (server_default '')
    status              pending|accepted|dropped (server_default 'pending')
    source_document_id  FK document.id (NULLABLE) — extraction provenance
    created_at          NOT NULL
    decided_at          NULLABLE — when accepted/dropped
    decided_by          email of the deciding reviewer (server_default '')

A candidate is DATA about a proposed term; like GlossaryEntry.meta it never reaches a
prompt. Hand-written (no autogenerate) off the head fa3d9c1e7b52, same convention as
the sibling create-table revisions (workflow_groups). New table only — no existing
row changes, so every existing install is byte-identical until the feature is used.
The SQLite hermetic/legacy path builds this table via create_all() (a NEW table, not
an altered one — no app/db.py _ADDED_COLUMNS shim needed, mirroring workflowgroup).
The nullability of every column matches models.TermCandidate exactly (the
verify_migrations nullability-parity guard enforces create_all == alembic).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e1a9d4b62f'
down_revision: Union[str, None] = 'fa3d9c1e7b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'termcandidate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('glossary_id', sa.Integer(), nullable=False),
        sa.Column('source_term', sa.String(), nullable=False),
        sa.Column('proposed_target', sa.String(), nullable=True),
        sa.Column('source_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('target_locale', sa.String(), nullable=False, server_default=''),
        sa.Column('frequency', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('context_snippet', sa.String(), nullable=False, server_default=''),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('source_document_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by', sa.String(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['glossary_id'], ['glossary.id']),
        sa.ForeignKeyConstraint(['source_document_id'], ['document.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_termcandidate_glossary_id', 'termcandidate',
                    ['glossary_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_termcandidate_glossary_id', table_name='termcandidate')
    op.drop_table('termcandidate')
