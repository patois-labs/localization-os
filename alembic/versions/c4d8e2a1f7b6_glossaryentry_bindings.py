"""glossaryentry per-pair bindings (SPEC-terminology-backend T1b, LAW-TERM-18, §A.1/§A.3)

Revision ID: c4d8e2a1f7b6
Revises: c3d5f8a1b6e4
Create Date: 2026-09-04 20:00:00.000000

The AUTHORITY INVERSION substrate. T1-lite made the compat glossaryentry/glossarysense rows
the single authority (LAW-TERM-17); T1b makes the concept store authoritative and the compat
rows a pure PROJECTION of per-pair BINDINGS. This revision adds the two binding-owned columns
the projector reads, plus the homonym-group closure index:

  - glossaryentry.target_term_id (INTEGER, nullable, NAMED FK -> glossaryterm.id): the
    ENFORCED target GlossaryTerm this pair binds. NULL = a DNT-only / empty-target binding
    (target_term == "") or a pre-T1b / not-yet-backfilled row (A.5 self-heals it).
  - glossaryentry.meta_overrides (JSON, nullable): the per-pair meta AUTHORITY, three-valued
    per key (absent = derive from the concept/source term; string = override; JSON null =
    CLEARED tombstone). entry.meta is PROJECTED from it (A.3). NULL = derive everything.
  - ix_glossaryconcept_hgroup on glossaryconcept(glossary_id, homonym_group): the group-closed
    scope query the projector runs (WHERE glossary_id=? AND homonym_group=?). Declared with
    this exact name in models.GlossaryConcept.__table_args__ (create_all) too, and ADDED to
    verify_migrations' index-name list. Deliberately NOT on the db._ADDED_COLUMNS legacy shim
    path (r7-11 — the closure runs unindexed there; accepted, performance-only).

ADDITIVE-IN-EFFECT (LAW-TERM-5): both columns are nullable, so no data rewrite / NOT NULL
narrowing. batch_alter_table rebuilds glossaryentry on SQLite under PRAGMA foreign_keys=ON
(the d7a3c1b9e4f2 precedent on this exact table; a THIRD rebuild of glossaryentry, with
glossarysense FKs into it). The NAMED FK is required by SQLite batch mode (an anonymous inline
FK raises "Constraint must have a name"); it is cosmetic for parity (verify_migrations checks
column-set + nullability, not FK identity) while Postgres carries the enforced FK. The
db._ADDED_COLUMNS legacy-SQLite shim stays FK-less (M6/M19 convention).

The A.5 data-step backfill (populate target_term_id/meta_overrides on existing rows) lands with
the authority-inversion commit that introduces glossary_concepts.backfill_pair_bindings; it is
appended to this revision's upgrade() then (the revision is unmerged on the branch). An install
with zero module entries -- every hermetic verify DB -- has nothing to backfill, so columns +
index alone converge fresh/wipe-rebuild/in-place. Re-parented at the ten-train main merge onto
c3d5f8a1b6e4 (advancedstagedtarget paragraph_index, the Advanced P3 T3 FULL_TEXT head, itself
down_revision b7e3f1a9c2d4 -> steprun_failure_shape -> b9e3d7c1f5a2), which landed on main after
this branch's base; single head restored. Hand-written (no autogenerate), same convention
as b9e3d7c1f5a2_advancedstagedtarget_segment_id.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e2a1f7b6"
down_revision: str | None = "c3d5f8a1b6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("glossaryentry", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "target_term_id",
                sa.Integer(),
                sa.ForeignKey("glossaryterm.id", name="fk_glossaryentry_target_term_id"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("meta_overrides", sa.JSON(), nullable=True))
    op.create_index(
        "ix_glossaryconcept_hgroup", "glossaryconcept", ["glossary_id", "homonym_group"]
    )
    # --- A.5 data step (H-1): pin the per-pair bindings on existing MODULE entries so an
    # `alembic upgrade` (with no app boot) already converges. A FROZEN Core port of
    # glossary_concepts.backfill_pair_bindings (the d7a3c1b9e4f2 precedent avoids the ORM,
    # which drifts ahead of the mid-chain schema once a LATER migration adds a column). It is
    # FIND-ONLY: after the d7a3c1b9e4f2 backfill every non-blank target already exists as a
    # glossaryterm, so no term is created here; a row that would need one is LEFT un-bound for
    # the ORM boot invariant (§A.5, db._assert_glossary_pair_binding_invariant), which can
    # create it. Writes ONLY NULLs, both columns together, so the boot marker's all-or-nothing
    # candidate check (target_term_id IS NULL AND meta_overrides IS NULL) stays correct.
    _backfill_pair_bindings_core(op.get_bind())


def _backfill_pair_bindings_core(bind) -> None:
    _e = sa.table(
        "glossaryentry",
        sa.column("id", sa.Integer),
        sa.column("glossary_id", sa.Integer),
        sa.column("concept_id", sa.Integer),
        sa.column("term_id", sa.Integer),
        sa.column("target_locale", sa.String),
        sa.column("target_term", sa.String),
        sa.column("meta", sa.JSON),
        sa.column("target_term_id", sa.Integer),
        sa.column("meta_overrides", sa.JSON),
    )
    _t = sa.table(
        "glossaryterm",
        sa.column("id", sa.Integer),
        sa.column("concept_id", sa.Integer),
        sa.column("locale", sa.String),
        sa.column("term", sa.String),
        sa.column("part_of_speech", sa.String),
        sa.column("context_example", sa.String),
    )
    _c = sa.table(
        "glossaryconcept",
        sa.column("id", sa.Integer),
        sa.column("definition", sa.String),
        sa.column("domain", sa.String),
        sa.column("notes", sa.String),
    )
    meta_order = ("part_of_speech", "definition", "usage_example", "subject_field", "notes")

    def _lang_base(a: str, b: str) -> bool:
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b:
            return False
        return a.split("-")[0].lower() == b.split("-")[0].lower()

    def _resolve_ttid(cid: int, tloc: str, surface: str, exclude: int | None) -> int | None:
        surface = (surface or "").strip()
        if not surface:
            return None
        cand = [
            r
            for r in bind.execute(
                sa.select(_t.c.id, _t.c.locale, _t.c.term).where(_t.c.concept_id == cid)
            ).fetchall()
            if r[2] == surface and r[0] != exclude
        ]
        tl = (tloc or "").strip()
        for r in cand:
            if (r[1] or "") == tl:
                return r[0]
        for r in cand:
            if (r[1] or "") == "":
                return r[0]
        for r in cand:
            if _lang_base(r[1] or "", tl):
                return r[0]
        return None  # would need a create -> leave for the ORM boot invariant

    def _derive(concept_row, src_row) -> dict:
        out: dict = {}
        if concept_row is not None:
            if concept_row[1]:
                out["definition"] = concept_row[1]
            if concept_row[2]:
                out["subject_field"] = concept_row[2]
            if concept_row[3]:
                out["notes"] = concept_row[3]
        if src_row is not None:
            if src_row[1]:
                out["part_of_speech"] = src_row[1]
            if src_row[2]:
                out["usage_example"] = src_row[2]
        return out

    def _two_way(meta: dict, deriv: dict) -> dict | None:
        meta = meta or {}
        deriv = deriv or {}
        out: dict = {}
        for k in meta_order:
            mv = meta.get(k)
            if mv:
                out[k] = mv
            elif deriv.get(k):
                out[k] = None
        return out or None

    rows = bind.execute(
        sa.select(
            _e.c.id,
            _e.c.concept_id,
            _e.c.term_id,
            _e.c.target_locale,
            _e.c.target_term,
            _e.c.meta,
        ).where(
            _e.c.glossary_id.isnot(None),
            _e.c.concept_id.isnot(None),
            _e.c.term_id.isnot(None),
            _e.c.target_term_id.is_(None),
            _e.c.meta_overrides.is_(None),
        )
    ).fetchall()
    for eid, cid, tid, tloc, tterm, meta in rows:
        ttid = _resolve_ttid(cid, tloc or "", tterm or "", tid)
        if (tterm or "").strip() and ttid is None:
            continue  # needs a term create -> boot invariant heals it
        crow = bind.execute(
            sa.select(_c.c.id, _c.c.definition, _c.c.domain, _c.c.notes).where(_c.c.id == cid)
        ).fetchone()
        srow = bind.execute(
            sa.select(_t.c.id, _t.c.part_of_speech, _t.c.context_example).where(_t.c.id == tid)
        ).fetchone()
        ov = _two_way(meta if isinstance(meta, dict) else {}, _derive(crow, srow))
        vals: dict = {}
        if ttid is not None:
            vals["target_term_id"] = ttid
        if ov is not None:
            vals["meta_overrides"] = ov
        if vals:
            bind.execute(_e.update().where(_e.c.id == eid).values(**vals))


def downgrade() -> None:
    op.drop_index("ix_glossaryconcept_hgroup", table_name="glossaryconcept")
    with op.batch_alter_table("glossaryentry", schema=None) as batch_op:
        batch_op.drop_column("meta_overrides")
        batch_op.drop_column("target_term_id")
