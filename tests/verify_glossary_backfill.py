"""Glossary concept backfill parity (SPEC-terminology-backend T1, LAW-TERM-6).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Builds SYNTHETIC pre-migration corpora
DIRECTLY as GlossaryEntry/GlossarySense rows in the temp DB (never the real loc_tms.db,
H19) — module entries, legacy per-project entries, synonyms, senses, wildcard-locale
rows, all-five-meta rows, and DNT-without-target — then asserts:

  1. backfill_glossary(session) is per-glossary guarded + idempotent: a SECOND run is a
     no-op (returns 0), and every module entry gains a concept + source term link while
     LEGACY per-project entries stay unlinked (concept_id/term_id NULL — LAW-TERM-2a).
  2. rematerialize(backfill(X)) == X for the compat rows: the exact GlossaryEntry set
     (source/target/dnt/case_sensitive/locales/meta) AND the GlossarySense set are
     reproduced, and a reconcile is a no-op.
  3. sense identity: each GlossarySense gains a concept_id/term_id link to its sibling
     homonym concept, and the base concept + every sibling share the SAME homonym_group
     (H5); a 2-sense term backfills to EXACTLY 1 entry + 2 senses.

Final line: "GLOSSARY BACKFILL OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_glbf_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryTerm,
    Project,
)
from app.services import glossary_concepts as gc

init_db()

SRC, TGT = "en-US", "es-ES"


def _snapshot_entries(s, glossary_id):
    rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary_id)).all()
    out = {}
    for e in rows:
        out[(e.source_term, e.source_locale, e.target_locale)] = (
            e.target_term,
            bool(e.dnt),
            bool(e.case_sensitive),
            dict(e.meta) if e.meta else None,
        )
    return out


def _snapshot_senses(s, glossary_id):
    eids = list(
        s.exec(select(GlossaryEntry.id).where(GlossaryEntry.glossary_id == glossary_id)).all()
    )
    out = {}
    for se in s.exec(
        select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids or [-1]))
    ).all():
        out[(se.glossary_entry_id, se.sense_label)] = (se.context_cue, se.target_term)
    return out


def _build_corpus():
    """Insert entries/senses DIRECTLY (no dual-write) to simulate a pre-migration DB."""
    with Session(engine) as s:
        g = Glossary(name="Backfill GL")
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id
        s.add(GlossaryPair(glossary_id=gid, source_locale=SRC, target_locale=TGT))
        # a wildcard-locale pair so a blank-locale DNT row projects
        s.add(GlossaryPair(glossary_id=gid, source_locale="", target_locale=""))

        # (a) plain module entry with all five meta keys
        e1 = GlossaryEntry(
            glossary_id=gid,
            source_locale=SRC,
            target_locale=TGT,
            source_term="login",
            target_term="acceso",
            meta={
                "part_of_speech": "noun",
                "definition": "entry to a system",
                "usage_example": "please log in",
                "subject_field": "software",
                "notes": "UI term",
            },
        )
        # (b) synonym of the same concept target (separate entry, no senses)
        e2 = GlossaryEntry(
            glossary_id=gid,
            source_locale=SRC,
            target_locale=TGT,
            source_term="sign in",
            target_term="acceso",
        )
        # (c) DNT-without-target, wildcard locale (blank source/target locale)
        e3 = GlossaryEntry(
            glossary_id=gid,
            source_locale="",
            target_locale="",
            source_term="Acme",
            target_term="",
            dnt=True,
        )
        # (d) case-sensitive entry
        e4 = GlossaryEntry(
            glossary_id=gid,
            source_locale=SRC,
            target_locale=TGT,
            source_term="ID",
            target_term="identificador",
            case_sensitive=True,
        )
        # (e) a MULTI-sense term (2 senses -> 2 homonym siblings)
        e5 = GlossaryEntry(
            glossary_id=gid,
            source_locale=SRC,
            target_locale=TGT,
            source_term="bank",
            target_term="banco",
        )
        s.add_all([e1, e2, e3, e4, e5])
        s.commit()
        s.refresh(e5)
        s.add(
            GlossarySense(
                glossary_entry_id=e5.id,
                sense_label="finance",
                context_cue="money account deposit",
                target_term="banco",
            )
        )
        s.add(
            GlossarySense(
                glossary_entry_id=e5.id,
                sense_label="river",
                context_cue="river water shore",
                target_term="orilla",
            )
        )
        s.commit()

        # legacy per-project entries (glossary_id NULL) — must NEVER gain a concept
        p = Project(name="Legacy P", source_locale=SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(
            GlossaryEntry(
                project_id=p.id,
                source_locale=SRC,
                target_locale=TGT,
                source_term="legacy",
                target_term="heredado",
            )
        )
        s.commit()
        return gid, p.id


def test_backfill_and_parity():
    gid, pid = _build_corpus()
    with Session(engine) as s:
        before_entries = _snapshot_entries(s, gid)
        before_senses = _snapshot_senses(s, gid)

    # 1. backfill
    with Session(engine) as s:
        done = gc.backfill_glossary(s)
        s.commit()
        assert done == 1, f"expected 1 glossary backfilled, got {done}"

    with Session(engine) as s:
        # every module entry linked
        entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        assert all(e.concept_id and e.term_id for e in entries), "a module entry is unlinked"
        # legacy entry stays unlinked (LAW-TERM-2a)
        legacy = s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).first()
        assert legacy.concept_id is None and legacy.term_id is None, "legacy entry got a concept"
        assert s.exec(select(GlossaryConcept)).first() is not None, "no concepts created"
        # sense identity: 2 senses on the bank entry, both linked to sibling concepts
        bank = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
            )
        ).first()
        senses = s.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id == bank.id)
        ).all()
        assert len(senses) == 2, f"expected 2 senses, got {len(senses)}"
        assert all(se.concept_id and se.term_id for se in senses), "a sense is unlinked (H2)"
        base_concept = s.get(GlossaryConcept, bank.concept_id)
        assert base_concept.homonym_group, "base concept has no homonym_group"
        for se in senses:
            sib = s.get(GlossaryConcept, se.concept_id)
            assert sib.homonym_group == base_concept.homonym_group, "sibling group mismatch (H5)"
            assert sib.id != base_concept.id, "sibling is the base concept"

    # 2. idempotent — a second backfill is a no-op (per-glossary guard)
    with Session(engine) as s:
        done2 = gc.backfill_glossary(s)
        s.commit()
        assert done2 == 0, f"second backfill should be a no-op, got {done2}"

    # 3. rematerialize(backfill(X)) == X for the compat rows
    with Session(engine) as s:
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        after_entries = _snapshot_entries(s, gid)
        after_senses = _snapshot_senses(s, gid)
    assert after_entries == before_entries, (
        "rematerialize(backfill(X)) diverged for entries:\n"
        f"  before={before_entries}\n  after={after_entries}"
    )
    assert after_senses == before_senses, (
        "rematerialize(backfill(X)) diverged for senses:\n"
        f"  before={before_senses}\n  after={after_senses}"
    )
    # a second reconcile is still a no-op (never appends a duplicate)
    with Session(engine) as s:
        n_terms_before = len(s.exec(select(GlossaryTerm)).all())
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        n_terms_after = len(s.exec(select(GlossaryTerm)).all())
    assert n_terms_after == n_terms_before, "a repeat reconcile appended terms (not a fixed point)"
    print("  backfill: module entries linked, legacy unlinked, 2-sense -> 1 entry + 2 senses")
    print("  parity: rematerialize(backfill(X)) == X for entries AND senses; reconcile is stable")


# The R5-7 / R6-E corpus: (source_locale, target_locale, source_term, target_term, dnt,
# case_sensitive, meta). Exercises a MULTI-PAIR shared surface (invoice es+fr), a CASE
# VARIANT (Invoice, case_sensitive), a WILDCARD-SOURCE DNT row with a real target locale
# (Widget), a FULL-WILDCARD DNT row whose target equals its source AND whose source==target
# locale (Gizmo->Gizmo, "" / "" — the R6-A shape a reconcile used to blank), a
# DNT-WITHOUT-TARGET row (Acme), and a base for a SENSE (bank).
_CORPUS = [
    (SRC, TGT, "invoice", "factura", 0, 0, {"definition": "a bill", "subject_field": "finance"}),
    (SRC, "fr-FR", "invoice", "facture", 0, 0, {"definition": "une facture"}),
    (SRC, TGT, "Invoice", "Factura", 0, 1, None),
    ("", TGT, "Widget", "Widget", 1, 0, None),
    ("", "", "Gizmo", "Gizmo", 1, 0, None),
    (SRC, TGT, "Acme", "", 1, 0, {"subject_field": "brand"}),
    (SRC, TGT, "bank", "banco", 0, 0, {"definition": "money", "part_of_speech": "noun"}),
]
_SENSES = {"bank": ("river", "water shore", "orilla")}


def _seed_corpus(s, glossary_id):
    for sl, tl, st, tt, dnt, cs, meta in _CORPUS:
        e = GlossaryEntry(
            glossary_id=glossary_id,
            source_locale=sl,
            target_locale=tl,
            source_term=st,
            target_term=tt,
            dnt=bool(dnt),
            case_sensitive=bool(cs),
            meta=meta,
        )
        s.add(e)
        s.flush()
        if st in _SENSES and tl == TGT:
            lbl, cue, tgt = _SENSES[st]
            s.add(
                GlossarySense(
                    glossary_entry_id=e.id, sense_label=lbl, context_cue=cue, target_term=tgt
                )
            )
    s.commit()


def _norm_hg(hg):
    """Strip the glossary id out of a homonym_group (`g<gid>:surface` -> `g*:surface`) so two
    glossaries built from the same corpus compare equal."""
    import re

    return re.sub(r"^g\d+:", "g*:", hg or "")


def _norm_shapes(s, glossary_id):
    """Id- and timestamp-independent (concepts, terms-per-concept, entries-with-linked-term,
    senses) shapes for a glossary — the differential's comparison surface."""
    concepts = s.exec(
        select(GlossaryConcept).where(GlossaryConcept.glossary_id == glossary_id)
    ).all()
    cids = [c.id for c in concepts]
    terms = (
        s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(cids))).all() if cids else []
    )
    terms_by_c = {}
    for t in terms:
        terms_by_c.setdefault(t.concept_id, []).append(
            (
                t.locale,
                t.term,
                t.status,
                t.term_type,
                t.part_of_speech,
                t.audience,
                bool(t.dnt),
                bool(t.case_sensitive),
                bool(t.exact),
            )
        )
    by_id = {t.id: t for t in terms}
    concept_tuples = sorted(
        (
            c.domain,
            c.subdomain,
            c.definition,
            c.notes,
            _norm_hg(c.homonym_group),
            c.source,
            tuple(sorted(terms_by_c.get(c.id, []))),
        )
        for c in concepts
    )
    entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary_id)).all()
    entry_tuples = sorted(
        (
            e.source_locale,
            e.target_locale,
            e.source_term,
            e.target_term,
            bool(e.dnt),
            bool(e.case_sensitive),
            tuple(sorted((e.meta or {}).items())),
            # R5-2: the LINKED source term must be the entry's OWN surface, not a case-variant
            (by_id[e.term_id].locale, by_id[e.term_id].term) if e.term_id in by_id else None,
        )
        for e in entries
    )
    eids = [e.id for e in entries]
    senses = (
        s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))).all()
        if eids
        else []
    )
    sense_tuples = sorted(
        (x.sense_label, x.context_cue, x.target_term, x.concept_id in cids) for x in senses
    )
    return concept_tuples, entry_tuples, sense_tuples


def test_migration_core_backfill():
    """M9 + R5-7 (gate rounds 1/5): a REAL side-by-side differential — the migration's frozen
    `_backfill_core` vs the ORM `_sync_glossary_from_entries`, on ONE identical corpus seeded
    into two glossaries, compared field-for-field on the normalized (id/timestamp-independent)
    concept/term/entry/sense shapes. The corpus contains a multi-pair shared surface, a case
    variant, a wildcard-locale DNT row, a DNT-without-target row and a sense — the shapes R5-2
    and R5-5 diverge on. Also seeds a `glossary_id IS NULL` legacy row and asserts BOTH
    backfills leave it unlinked (LAW-TERM-2a)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mig_add_glossary_concepts",
        os.path.join(_ROOT, "alembic", "versions", "d7a3c1b9e4f2_add_glossary_concepts.py"),
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    with Session(engine) as s:
        gA = Glossary(name="Core Diff A")
        gB = Glossary(name="Core Diff B")
        s.add(gA)
        s.add(gB)
        s.commit()
        s.refresh(gA)
        s.refresh(gB)
        gida, gidb = gA.id, gB.id
        # a legacy per-project row (glossary_id NULL) — must stay unlinked on BOTH paths
        s.add(
            GlossaryEntry(
                project_id=None,
                glossary_id=None,
                source_locale=SRC,
                target_locale=TGT,
                source_term="legacyonly",
                target_term="heredado",
            )
        )
        s.commit()
    with Session(engine) as s:
        _seed_corpus(s, gida)
        _seed_corpus(s, gidb)

    # ORM backfill on gB, THEN the Core backfill (its per-glossary guard skips gB, does gA).
    with Session(engine) as s:
        gc._sync_glossary_from_entries(s, gidb)
        s.commit()
    with Session(engine) as s:
        mig._backfill_core(s.connection())
        s.commit()

    with Session(engine) as s:
        a = _norm_shapes(s, gida)
        b = _norm_shapes(s, gidb)
        assert a[0] == b[0], (
            "R5-7 concepts differ (core vs ORM):\n  core-only="
            f"{[x for x in a[0] if x not in b[0]]}\n  orm-only={[x for x in b[0] if x not in a[0]]}"
        )
        assert a[1] == b[1], (
            "R5-7 entries differ (core vs ORM):\n  core-only="
            f"{[x for x in a[1] if x not in b[1]]}\n  orm-only={[x for x in b[1] if x not in a[1]]}"
        )
        assert a[2] == b[2], f"R5-7 senses differ: core={a[2]} orm={b[2]}"
        # R5-2: every entry's linked source term is its OWN surface (no case-variant aliasing).
        for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gida)).all():
            assert s.get(GlossaryTerm, e.term_id).term == e.source_term, (
                f"R5-2: entry {e.source_term!r} linked to a different term surface"
            )
        # the legacy NULL row stays unlinked on both paths (LAW-TERM-2a).
        leg = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "legacyonly")).first()
        assert leg.concept_id is None and leg.term_id is None, "legacy row linked (LAW-TERM-2a)"
    print("  migration: _backfill_core == ORM _sync_glossary_from_entries field-for-field (R5-7)")


def test_migration_case_variant_survives_reconcile():
    """R5-2 + R6-B (gate rounds 5/6): a case-variant surface on a MULTI-PAIR shared surface,
    backfilled by the migration's `_backfill_core`, gets its OWN term row (R5-2, no rewrite of
    `source_term`/`target_term`), AND a following reconcile does NOT MULTIPLY the case variant
    into the other locale pair (R6-B, no phantom `Invoice->facture` fr row). Asserts the EXACT
    entry SET, on two locale pairs."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mig_add_glossary_concepts2",
        os.path.join(_ROOT, "alembic", "versions", "d7a3c1b9e4f2_add_glossary_concepts.py"),
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    # invoice->factura (es) + invoice->facture (fr) + a case variant Invoice->Factura (es only).
    rows = [
        (SRC, TGT, "invoice", "factura", 0),
        (SRC, "fr-FR", "invoice", "facture", 0),
        (SRC, TGT, "Invoice", "Factura", 1),
    ]
    with Session(engine) as s:
        g = Glossary(name="Case Variant Multipair GL")
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id
        for sl, tl, st, tt, cs in rows:
            s.add(
                GlossaryEntry(
                    glossary_id=gid,
                    source_locale=sl,
                    target_locale=tl,
                    source_term=st,
                    target_term=tt,
                    case_sensitive=bool(cs),
                )
            )
        s.commit()
    with Session(engine) as s:
        mig._backfill_core(s.connection())
        s.commit()
    with Session(engine) as s:
        gc.project(
            s, gid, derive_senses=True
        )  # the step that used to rewrite AND multiply the surface
        s.commit()
    with Session(engine) as s:
        got = {
            (e.source_term, e.target_term, e.source_locale, e.target_locale)
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        }
        want = {
            ("invoice", "factura", SRC, TGT),
            ("invoice", "facture", SRC, "fr-FR"),
            ("Invoice", "Factura", SRC, TGT),
        }
        assert got == want, (
            f"R5-2/R6-B: the entry SET changed after a reconcile (a case variant was rewritten "
            f"or multiplied into the fr pair): {got}"
        )
    print("  R5-2/R6-B: a multi-pair case variant survives a reconcile (no rewrite, no phantom)")


def main():
    test_backfill_and_parity()
    test_migration_core_backfill()
    test_migration_case_variant_survives_reconcile()
    print("GLOSSARY BACKFILL OK")


if __name__ == "__main__":
    main()
