"""workflowstep per-step concept-glossary renderer toggle (Terminology backend T3, SPEC §9-T3)

Revision ID: a3f7c1e9d2b8
Revises: f2c8a4e6b1d9
Create Date: 2026-09-04 12:00:00.000000

Adds three per-`WorkflowStep` columns for the concept-glossary renderer swap behind a
per-step toggle (SPEC-terminology-backend §9-T3 / LAW-TERM-14), all DEFAULT so the swap
is inert until an author turns it on:
  - concept_glossary_render (bool, default 0/OFF): OFF -> the {glossary} prompt block is
    prompts.glossary_line verbatim and the gates enforce the full match set (byte-identical
    to main, LAW-TERM-1). ON -> glossary_concepts.render_for_llm's audience-gated, ordered,
    capped Markdown, and the enforced set IS the shown set (LAW-TERM-15).
  - glossary_concept_cap (int, default 10): at most N concepts in the rendered block.
  - glossary_token_cap (int, default 0): the block's token ceiling (0 = fall back to the
    install setting glossary_prompt_token_cap="1500").

DEFAULT-OFF `server_default` backfills every existing row to the toggle-OFF default, so a
live upgrade preserves today's exact prompt/enforcement behavior. Chained off the current
single head (f2c8a4e6b1d9, quality_lens_mission_gate). Mirrors the SQLite _ensure_columns()
additive shim in app/db.py + the model (nullability-parity CI guard). Hand-written (no
autogenerate) to avoid spurious index noise, same convention as
b4d9f1a7c2e6_workflowstep_use_image_context.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c1e9d2b8'
down_revision: Union[str, None] = 'f2c8a4e6b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('concept_glossary_render', sa.Boolean(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('glossary_concept_cap', sa.Integer(),
                                      nullable=False, server_default='10'))
        batch_op.add_column(sa.Column('glossary_token_cap', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('glossary_token_cap')
        batch_op.drop_column('glossary_concept_cap')
        batch_op.drop_column('concept_glossary_render')
