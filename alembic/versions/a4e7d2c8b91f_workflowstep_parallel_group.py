"""workflowstep.parallel_group (SPEC-parallel-routing Phase 1 - parallel step routing)

Revision ID: a4e7d2c8b91f
Revises: c5a1f8e3b724
Create Date: 2026-08-01 15:00:00.000000

Adds ONE additive, NULLABLE column to `workflowstep`:

  workflowstep.parallel_group - nullable VARCHAR. The PARALLEL GROUP slug
                                (SPEC-parallel-routing section 5.6). Steps that are
                                CONSECUTIVE in order_index and share a non-NULL slug
                                run CONCURRENTLY inside one run, against the same
                                frozen snapshot of the working text, and JOIN before
                                the next step starts. NULL = not in a group.

WHY AN ANNOTATION AND NOT A DAG (the decision worth defending in a migration file).
The engine's spine is `order_index` + a single cursor, and EVERY structural predicate
in the codebase is prefix-shaped: `_graph_edit_errors` defines "prior" as a prefix of
sorted(order_index); `_ref_resolves`, `_is_correction_loop_step`, `_references_prev_lqa`,
`_is_lqa_onfail_target` and `workflow_shapes.detect_shapes` all scan the ordered list.
A real fan-out/fan-in EDGE model would turn "an earlier step of the right type" into a
reachability/dominator question and force a graph-theoretic rewrite of all of them. A
CONSECUTIVE-RUN annotation keeps every one of those predicates correct as written, adds
no new edge type (so `_next_cursor`'s resolution table is unchanged), and costs exactly
this one nullable column. A reader who greps for "why is parallelism a string column"
should find this paragraph, not add an edge table.

WHY NULLABLE, NO SERVER DEFAULT AND NO BACKFILL: "not in a group" is the absence of a
value, not a value. Every existing row backfills to NULL, and the orchestrator's
`_has_parallel(steps)` gate is False for a workflow whose steps are all NULL - the
fork/join seam is never entered, no extra query is issued, and the run is byte-identical
to pre-parallel (the sacred byte-stable-when-off gate; proven by
tests/verify_parallel_routing.py's ungrouped-inertness group).

NOT UNIQUE, ON PURPOSE. Unlike `key` (unique per workflow), this column is SHARED by
definition - two rows carrying the same slug is precisely what makes them one group.
Membership rules (consecutive; check-only `lqa` members in Phase 1; at most one routing
member; guards resolved against PRE-group steps) are save-time validations in
app/services/workflows.py, fail-closed, not DDL constraints: they are relations between
ROWS in an ordered list, which no column constraint can express.

Hand-written (no autogenerate) off the head c5a1f8e3b724 (the Evaluations Lab tables),
same convention as the sibling additive-column revisions. Runs clean on SQLite
(batch_alter_table) and Postgres (native ALTER); the SQLite _ADDED_COLUMNS["workflowstep"]
shim in app/db.py carries the SAME column so a non-alembic SQLite boot adds it
identically (the nullability-parity CI guard compares the two).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4e7d2c8b91f'
down_revision: Union[str, None] = 'c5a1f8e3b724'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parallel_group', sa.String(), nullable=True))
    # No backfill: NULL already reads as "not in a parallel group" everywhere.


def downgrade() -> None:
    with op.batch_alter_table('workflowstep', schema=None) as batch_op:
        batch_op.drop_column('parallel_group')
