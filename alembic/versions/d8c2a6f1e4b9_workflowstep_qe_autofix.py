"""workflowstep.qe_autofix* (Train B: opt-in QE auto-fix in workflows)

Revision ID: d8c2a6f1e4b9
Revises: b4e1c7f29a06
Create Date: 2026-09-22 12:00:00.000000

Adds FOUR additive columns to `workflowstep`, all default OFF / neutral:

  workflowstep.qe_autofix             BOOLEAN NOT NULL, server_default "0". The per-step
                                      opt-in: a segment that fails a Jev QE gate is handed
                                      to an LLM post-editor and re-scored.
  workflowstep.qe_autofix_provider    VARCHAR NULL. The fixer LLM (a built-in name or
                                      engine:<id>). NULL = the step's own provider.
  workflowstep.qe_autofix_max_rounds  INTEGER NOT NULL, server_default "3". Fixer rounds,
                                      clamped 1..5 at write time.
  workflowstep.qe_autofix_cross_check BOOLEAN NOT NULL, server_default "0". A second
                                      opinion from the fixer LLM shown to the fixer.

Back-compat: every pre-existing row reads qe_autofix = 0, so no existing step ever
enters the fix loop and an upgraded install is byte-identical until someone opts in.

Hand-written (no autogenerate) off the head b4e1c7f29a06, same convention as the
sibling additive-column revisions. Runs clean on SQLite (batch_alter_table) and
Postgres (native ALTER). The SQLite non-alembic boot path carries the same columns in
db._ADDED_COLUMNS["workflowstep"] (the nullability-parity CI guard compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8c2a6f1e4b9'
down_revision: Union[str, None] = 'b4e1c7f29a06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qe_autofix', sa.Boolean(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('qe_autofix_provider', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('qe_autofix_max_rounds', sa.Integer(),
                                      nullable=False, server_default='3'))
        batch_op.add_column(sa.Column('qe_autofix_cross_check', sa.Boolean(),
                                      nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('qe_autofix_cross_check')
        batch_op.drop_column('qe_autofix_max_rounds')
        batch_op.drop_column('qe_autofix_provider')
        batch_op.drop_column('qe_autofix')
