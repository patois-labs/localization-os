"""workflowstep.doc_agent_mode / doc_agent_chunk (add-on step fields)

Revision ID: a6d2e9b4c1f7
Revises: e5a1c9d3b7f2
Create Date: 2026-11-11 21:00:00.000000

Adds TWO additive columns to `workflowstep` for an add-on that is not part of this edition
(the columns stay in the core schema so a database moves between editions unchanged):

  workflowstep.doc_agent_mode   VARCHAR NOT NULL, server_default "". "" (off, the
                                default and every legacy row), "decision_log" or
                                "conversation". Read only by the add-on, on an
                                llm_agent step; this edition never sets it.
  workflowstep.doc_agent_chunk  INTEGER NULL. Segments per call, 2..40. NULL = the
                                built-in default (10).

Back-compat: every pre-existing row reads doc_agent_mode = "", so no existing step ever
changes its requests. The tables stay in the core schema on purpose (open-core summary:
tables stay, code moves).

Hand-written off the head e5a1c9d3b7f2. The SQLite non-alembic boot path carries the same
columns in db._ADDED_COLUMNS["workflowstep"] (the nullability-parity CI guard compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6d2e9b4c1f7'
down_revision: Union[str, None] = 'e5a1c9d3b7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('doc_agent_mode', sa.String(),
                                      nullable=False, server_default=''))
        batch_op.add_column(sa.Column('doc_agent_chunk', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('doc_agent_chunk')
        batch_op.drop_column('doc_agent_mode')
