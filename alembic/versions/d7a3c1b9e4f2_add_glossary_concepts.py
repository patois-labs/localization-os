"""add glossary concept/term store (SPEC-terminology-backend T1, D1)

Revision ID: d7a3c1b9e4f2
Revises: e4b1c7d92a6f
Create Date: 2026-09-01 22:30:00.000000

ADDITIVE-IN-EFFECT (LAW-TERM-5): two new tables (glossaryconcept, glossaryterm) + two
nullable FK columns on glossaryentry AND glossarysense, then an idempotent data backfill.
Mirrors models.GlossaryConcept / models.GlossaryTerm + the two link columns on
GlossaryEntry / GlossarySense and the create_all()/`_ensure_columns` path in app/db.py, so
the nullability-parity guard (tests/verify_migrations.py) holds on BOTH SQLite and Postgres.
The glossaryentry/glossarysense ADD COLUMNs go through op.batch_alter_table so SQLite's
table rebuild is handled under PRAGMA foreign_keys=ON (the d2e6b8c4a1f7 precedent on this
exact table). Hand-written off the worktree head b4e1d7c2a9f3.

Step 4 is the DATA backfill (one concept + source/target terms per existing MODULE entry,
senses -> homonym-sibling concepts): create_all cannot reach it, so it is ALSO exposed as
glossary_concepts.backfill_glossary(session) and called from the db seed path, so a fresh
install, a wipe-and-rebuild, and an in-place upgrade all converge on the same concept/term
set (LAW-TERM-6). backfill_glossary FLUSHes (never commits) so it is safe inside this
migration's transaction.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a3c1b9e4f2"
# Re-parented onto the quality-lenses migration at the main merge (both originally branched off
# b4e1d7c2a9f3, which would leave two alembic heads) so the chain is linear and `alembic heads`
# prints exactly one head (d7a3c1b9e4f2).
down_revision: str | None = "e4b1c7d92a6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- step 1: glossaryconcept ---
    op.create_table(
        "glossaryconcept",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("glossary_id", sa.Integer(), sa.ForeignKey("glossary.id"), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("subdomain", sa.String(), nullable=False),
        sa.Column("definition", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("cross_refs", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("homonym_group", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_glossaryconcept_glossary_id", "glossaryconcept", ["glossary_id"])
    # NON-UNIQUE composite (§4.1/M5): a blank external_id is legal + repeatable.
    op.create_index("ix_glossaryconcept_extid", "glossaryconcept", ["glossary_id", "external_id"])

    # --- step 2: glossaryterm ---
    op.create_table(
        "glossaryterm",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("concept_id", sa.Integer(), sa.ForeignKey("glossaryconcept.id"), nullable=False),
        sa.Column("locale", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("term_type", sa.String(), nullable=False),
        sa.Column("part_of_speech", sa.String(), nullable=False),
        sa.Column("grammatical_gender", sa.String(), nullable=False),
        sa.Column("grammatical_number", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("usage_note", sa.String(), nullable=False),
        sa.Column("context_example", sa.String(), nullable=False),
        sa.Column("geographical_usage", sa.String(), nullable=False),
        sa.Column("case_sensitive", sa.Boolean(), nullable=False),
        sa.Column("exact", sa.Boolean(), nullable=False),
        sa.Column("dnt", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "deprecated_replacement_term_id",
            sa.Integer(),
            sa.ForeignKey("glossaryterm.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_glossaryterm_concept", "glossaryterm", ["concept_id"])

    # --- step 3: nullable FK link columns on glossaryentry AND glossarysense ---
    # batch_alter_table rebuilds the table on SQLite (which cannot bare-ADD a REFERENCES
    # column); native ADD on Postgres. Nullable -> no data rewrite, no NOT NULL narrowing.
    # NAMED FK constraints: alembic's SQLite batch mode requires every constraint to
    # carry a name (an anonymous inline FK raises "Constraint must have a name"). The
    # names are cosmetic for parity (verify_migrations checks column-set + nullability,
    # not FK identity); Postgres carries the real enforced FK the pg_smoke delete-order
    # test relies on. The db._ADDED_COLUMNS legacy-SQLite shim stays FK-less (M6/M19).
    with op.batch_alter_table("glossaryentry", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "concept_id",
                sa.Integer(),
                sa.ForeignKey("glossaryconcept.id", name="fk_glossaryentry_concept_id"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "term_id",
                sa.Integer(),
                sa.ForeignKey("glossaryterm.id", name="fk_glossaryentry_term_id"),
                nullable=True,
            )
        )
    with op.batch_alter_table("glossarysense", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "concept_id",
                sa.Integer(),
                sa.ForeignKey("glossaryconcept.id", name="fk_glossarysense_concept_id"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "term_id",
                sa.Integer(),
                sa.ForeignKey("glossaryterm.id", name="fk_glossarysense_term_id"),
                nullable=True,
            )
        )

    # --- step 4: idempotent data backfill (LAW-TERM-6) ---
    # M9 (gate round-1): expressed in FROZEN SQLAlchemy Core against the column set THIS
    # revision creates — it does NOT import the live ORM models, so a future revision that
    # adds a column to any of these five tables can never break `alembic upgrade head` from
    # an empty DB (the ORM would otherwise emit a column that does not yet exist at this
    # revision). The create_all seed path keeps using the ORM `backfill_glossary`, which is
    # covered by tests/verify_glossary_backfill.py; this Core mirror is covered by that
    # suite's migration-backfill test.
    _backfill_core(op.get_bind())


def _frozen_tables():
    """The four tables at THIS revision's column set (frozen; never the live models). Real
    ``sa.Table`` objects on a private MetaData so ``inserted_primary_key`` returns the new
    row id cross-DB (the id column is declared primary_key)."""
    md = sa.MetaData()

    def _tbl(name, *names):
        cols = [
            sa.Column("id", sa.Integer(), primary_key=True)
            if n == "id"
            else sa.Column(n, sa.JSON() if n in ("meta", "cross_refs") else sa.String())
            for n in names
        ]
        return sa.Table(name, md, *cols)

    e = _tbl(
        "glossaryentry",
        "id",
        "glossary_id",
        "source_locale",
        "target_locale",
        "source_term",
        "target_term",
        "dnt",
        "case_sensitive",
        "meta",
        "concept_id",
        "term_id",
    )
    s = _tbl(
        "glossarysense",
        "id",
        "glossary_entry_id",
        "sense_label",
        "context_cue",
        "target_term",
        "concept_id",
        "term_id",
    )
    c = _tbl(
        "glossaryconcept",
        "id",
        "glossary_id",
        "external_id",
        "domain",
        "subdomain",
        "definition",
        "notes",
        "cross_refs",
        "source",
        "homonym_group",
        "created_by",
        "created_at",
        "updated_at",
    )
    t = _tbl(
        "glossaryterm",
        "id",
        "concept_id",
        "locale",
        "term",
        "status",
        "term_type",
        "part_of_speech",
        "grammatical_gender",
        "grammatical_number",
        "audience",
        "usage_note",
        "context_example",
        "geographical_usage",
        "case_sensitive",
        "exact",
        "dnt",
        "source",
        "deprecated_replacement_term_id",
        "created_at",
        "updated_at",
    )
    return e, s, c, t


def _backfill_core(bind) -> None:
    """Port of glossary_concepts.backfill_glossary in frozen Core (M9). One concept +
    source/target terms per MODULE entry; senses -> homonym-sibling concepts sharing the
    pair-free homonym_group; legacy per-project entries (glossary_id NULL) untouched;
    per-glossary guarded + idempotent."""
    import json
    import uuid

    _e, _s, _c, _t = _frozen_tables()
    now = datetime.utcnow()
    _AUD = "human,mt,llm"

    def _flat(v) -> str:
        return " ".join(str(v or "").split())[:500]

    def _meta(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if raw:
            try:
                d = json.loads(raw)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
        return {}

    # (concept_id, locale, term) EXACT -> term id. Mirrors the ORM `_upsert_term` (keyed on
    # the EXACT surface, no-clobber on update): so a case-variant source surface (`Invoice`
    # vs `invoice`) gets its OWN term row and its own `term_id` (R5-2 — no later reconcile
    # rewrites `source_term`/`target_term`), and a wildcard row whose target equals its source
    # surface reuses the one term instead of creating a phantom non-DNT twin (R5-5).
    term_by_key: dict = {}

    def _upsert_term_core(cid, locale, term, *, dnt=None, case_sensitive=None, pos=None, ctx=None):
        k = (cid, locale or "", term)
        tid = term_by_key.get(k)
        if tid is not None:
            vals = {}
            if dnt is not None:
                vals["dnt"] = bool(dnt)
            if case_sensitive is not None:
                vals["case_sensitive"] = bool(case_sensitive)
            if pos is not None:
                vals["part_of_speech"] = _flat(pos)
            if ctx is not None:
                vals["context_example"] = _flat(ctx)
            if vals:
                bind.execute(_t.update().where(_t.c.id == tid).values(**vals))
            return tid
        tid = bind.execute(
            _t.insert().values(
                concept_id=cid,
                locale=locale or "",
                term=term,
                status="preferred",
                term_type="",
                part_of_speech=_flat(pos) if pos is not None else "",
                grammatical_gender="",
                grammatical_number="",
                audience=_AUD,
                usage_note="",
                context_example=_flat(ctx) if ctx is not None else "",
                geographical_usage="",
                case_sensitive=bool(case_sensitive),
                exact=False,
                dnt=bool(dnt),
                source="",
                deprecated_replacement_term_id=None,
                created_at=now,
                updated_at=None,
            )
        ).inserted_primary_key[0]
        term_by_key[k] = tid
        return tid

    # glossaries that already carry a concept are SKIPPED (per-glossary guard, idempotent).
    have = {row[0] for row in bind.execute(sa.select(_c.c.glossary_id).distinct()).fetchall()}
    entries = bind.execute(
        sa.select(
            _e.c.id,
            _e.c.glossary_id,
            _e.c.source_locale,
            _e.c.target_locale,
            _e.c.source_term,
            _e.c.target_term,
            _e.c.dnt,
            _e.c.case_sensitive,
            _e.c.meta,
        )
        .where(_e.c.glossary_id.isnot(None))
        .order_by(_e.c.id)
    ).fetchall()
    # R4 shared identity: (glossary_id, source_locale, casefold source_term) -> concept_id, so
    # a multi-language term is ONE concept (same grouping as the ORM backfill, so
    # upgrade == wipe-and-rebuild — LAW-TERM-6). The concept is cached; the SOURCE TERM is
    # resolved per EXACT surface via _upsert_term_core (R5-2), and concept-level meta is
    # no-clobber last-non-blank-wins, exactly as sync_entry_concept does.
    concept_by_source: dict = {}
    for en in entries:
        if en.glossary_id in have:
            continue
        meta = _meta(en.meta)
        skey = (en.glossary_id, en.source_locale or "", (en.source_term or "").casefold())
        cid = concept_by_source.get(skey)
        if cid is None:
            cid = bind.execute(
                _c.insert().values(
                    glossary_id=en.glossary_id,
                    external_id="",
                    domain=_flat(meta.get("subject_field")),
                    subdomain="",
                    definition=_flat(meta.get("definition")),
                    notes=_flat(meta.get("notes")),
                    cross_refs=None,
                    source="",
                    homonym_group="",
                    created_by="",
                    created_at=now,
                    updated_at=None,
                )
            ).inserted_primary_key[0]
            bind.execute(_c.update().where(_c.c.id == cid).values(external_id=str(uuid.uuid4())))
            concept_by_source[skey] = cid
        else:
            # no-clobber last-non-blank-wins on the shared concept's meta (mirror the ORM).
            cvals = {}
            if meta.get("subject_field"):
                cvals["domain"] = _flat(meta.get("subject_field"))
            if meta.get("definition"):
                cvals["definition"] = _flat(meta.get("definition"))
            if meta.get("notes"):
                cvals["notes"] = _flat(meta.get("notes"))
            if cvals:
                bind.execute(_c.update().where(_c.c.id == cid).values(**cvals))
        src_id = _upsert_term_core(
            cid,
            en.source_locale,
            en.source_term,
            dnt=en.dnt,
            case_sensitive=en.case_sensitive,
            pos=(meta.get("part_of_speech") or None),
            ctx=(meta.get("usage_example") or None),
        )
        bind.execute(_e.update().where(_e.c.id == en.id).values(concept_id=cid, term_id=src_id))
        if (en.target_term or "").strip():
            _upsert_term_core(cid, en.target_locale, en.target_term)
        senses = bind.execute(
            sa.select(_s.c.id, _s.c.sense_label, _s.c.context_cue, _s.c.target_term).where(
                _s.c.glossary_entry_id == en.id
            )
        ).fetchall()
        if senses:
            hg = f"g{en.glossary_id}:{(en.source_term or '').strip().casefold()}"
            bind.execute(_c.update().where(_c.c.id == cid).values(homonym_group=hg))
            for se in senses:
                sib = bind.execute(
                    _c.insert().values(
                        glossary_id=en.glossary_id,
                        external_id="",
                        domain=_flat(se.sense_label),
                        subdomain="",
                        definition=_flat(se.context_cue),
                        notes="",
                        cross_refs=None,
                        source="",
                        homonym_group=hg,
                        created_by="",
                        created_at=now,
                        updated_at=None,
                    )
                ).inserted_primary_key[0]
                bind.execute(
                    _c.update().where(_c.c.id == sib).values(external_id=str(uuid.uuid4()))
                )
                _upsert_term_core(
                    sib,
                    en.source_locale,
                    en.source_term,
                    dnt=en.dnt,
                    case_sensitive=en.case_sensitive,
                )
                sib_tgt = None
                if (se.target_term or "").strip():
                    sib_tgt = _upsert_term_core(sib, en.target_locale, se.target_term)
                bind.execute(
                    _s.update().where(_s.c.id == se.id).values(concept_id=sib, term_id=sib_tgt)
                )


def downgrade() -> None:
    # Drop the link columns FIRST (they reference the two tables), THEN the child table
    # glossaryterm (self-FK + concept FK), THEN glossaryconcept (M7 order).
    with op.batch_alter_table("glossarysense", schema=None) as batch_op:
        batch_op.drop_column("term_id")
        batch_op.drop_column("concept_id")
    with op.batch_alter_table("glossaryentry", schema=None) as batch_op:
        batch_op.drop_column("term_id")
        batch_op.drop_column("concept_id")
    op.drop_index("ix_glossaryterm_concept", table_name="glossaryterm")
    op.drop_table("glossaryterm")
    op.drop_index("ix_glossaryconcept_extid", table_name="glossaryconcept")
    op.drop_index("ix_glossaryconcept_glossary_id", table_name="glossaryconcept")
    op.drop_table("glossaryconcept")
