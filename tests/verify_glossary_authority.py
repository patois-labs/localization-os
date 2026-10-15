"""LAW-TERM-18 authority inversion: the concept store is authoritative, the compat rows are a
pure projection of the per-pair BINDINGS (SPEC-terminology-backend T1b).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Every assertion is a COUNTERFACTUAL against the
STORE (concept/term rows, ``glossaryentry.target_term_id``/``meta_overrides``, projected compat
columns, APPROVED status), not just a read seam. Covers the round-7 closures T1b was built to fix:

  * A.2 the projector is the SOLE writer of the derived columns (source/target/meta) and never
    deletes; a hand-edited ``meta_overrides`` + a projection changes ``entry.meta``.
  * R7-A a document import touches ONLY the named concepts (an unrelated binding is byte-stable).
  * R7-D/R7-K a concept-level definition edit is visible on an UNPINNED binding, invisible on a
    legacy-PINNED one; the two-fixture rule (meta-less CSV vs definition-bearing CSV).
  * R7-C delete/demote of a bound target re-points to a sibling, or REFUSES the sole target.
  * R7-M turning DNT off on a targetless binding is refused on the service seam.
  * A.4-2 two source synonyms in one pair = two bindings of one concept.
  * A.3 bindings_pinned is reported and an idempotent re-import keeps an APPROVED glossary APPROVED.
  * A.5 projection-equivalence: project(backfill(strip(X))) == X for a store built via the editor.
  * A.4-8 set_pairs widen creates bindings (counted), narrow leaves them, a resubmit is a no-op.
  * A.2-a the empty-target non-DNT binding survives every write; A.4-6 a two-locale homonym sibling
    projects a sense in both pairs.

Final line: "GLOSSARY AUTHORITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_glauth_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db
from app.db import engine, init_db
from app.main import app
from app.models import (
    GlossaryConcept,
    GlossaryEntry,
    GlossarySense,
    GlossaryStatus,
    GlossaryTerm,
    Setting,
)
from app.services import glossary as gloss_engine
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm
from app.services import settings as _settings

init_db()
_settings.set_many({"terminology_concept_editor_enabled": "1", "security_auth_required": "0"})
_settings.invalidate_cache()

SRC, TGT, TGT2 = "en-US", "es-ES", "fr-FR"


def _gl(name, pairs=((SRC, TGT),)):
    with Session(engine) as s:
        gid = gm.create(s, name).id
        gm.set_pairs(s, gid, [f"{a}:{b}" for a, b in pairs])
    return gid


def _entries(s, gid):
    return s.exec(
        select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid).order_by(GlossaryEntry.id)
    ).all()


def _entry(s, gid, src, tl):
    return s.exec(
        select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == gid,
            GlossaryEntry.source_term == src,
            GlossaryEntry.target_locale == tl,
        )
    ).first()


def _fail(msg):
    raise AssertionError(msg)


# --------------------------------------------------------------------------- #
def test_projector_is_sole_writer_of_derived():
    """A.2: the DERIVED ``entry.meta`` is a pure projection of the binding's ``meta_overrides``
    over the concept derivation. Writing an override directly + projecting changes the derived
    column; the projector never deletes the row."""
    gid = _gl("sole-writer")
    with Session(engine) as s:
        e = gm.add_term(
            s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT
        )
        eid = e.id
    with Session(engine) as s:
        e = s.get(GlossaryEntry, eid)
        e.meta_overrides = {"definition": "pinned-by-hand"}
        s.add(e)
        s.flush()
        gc.project(s, gid, entry_ids=[eid], derive_senses=True)
        s.commit()
    with Session(engine) as s:
        e = s.get(GlossaryEntry, eid)
        assert (e.meta or {}).get("definition") == "pinned-by-hand", (
            f"projector did not derive entry.meta from meta_overrides: {e.meta!r}"
        )
        assert e.target_term == "factura", "projector dropped the target"
    print("  A.2: entry.meta is projected from meta_overrides; project never deletes the row OK")


def test_r7a_import_touches_only_named():
    """R7-A: a .glossary.json import upserts ONLY the named concepts; an unrelated pre-existing
    binding is byte-stable (source/target/meta/dnt unchanged)."""
    gid = _gl("r7a")
    with Session(engine) as s:
        gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        before = (
            _entry(s, gid, "bank", TGT).target_term,
            bool(_entry(s, gid, "bank", TGT).dnt),
            _entry(s, gid, "bank", TGT).meta,
        )
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "inv",
                "definition": "a billing document",
                "terms": [
                    {"term": "invoice", "locale": SRC, "status": "preferred"},
                    {"term": "factura", "locale": TGT, "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
    with Session(engine) as s:
        after = (
            _entry(s, gid, "bank", TGT).target_term,
            bool(_entry(s, gid, "bank", TGT).dnt),
            _entry(s, gid, "bank", TGT).meta,
        )
        assert after == before, f"R7-A: import rewrote an UNNAMED binding: {before} -> {after}"
    print("  R7-A: import touches only the named concepts; unrelated binding stable OK")


def test_r7d_concept_edit_visible_on_unpinned_only():
    """R7-D/R7-K + r6-3: a concept-level definition edit is VISIBLE on a binding the legacy path
    left unpinned for that key, and INVISIBLE on a legacy-pinned one. Two fixtures."""
    # (a) meta-LESS CSV over two pairs -> a later concept definition edit shows on BOTH pairs.
    gid = _gl("r7d-a", pairs=((SRC, TGT), (SRC, TGT2)))
    csv = b"source_term,target_term\r\ninvoice,factura\r\n"
    csv2 = b"source_term,target_term\r\ninvoice,facture\r\n"
    with Session(engine) as s:
        gloss_engine.import_bytes(s, None, csv, SRC, TGT, glossary_id=gid, filename="a.csv")
        gloss_engine.import_bytes(s, None, csv2, SRC, TGT2, glossary_id=gid, filename="b.csv")
        cid = _entry(s, gid, "invoice", TGT).concept_id
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, definition="a billing document")
    with Session(engine) as s:
        for tl in (TGT, TGT2):
            m = (_entry(s, gid, "invoice", tl).meta or {}).get("definition")
            assert m == "a billing document", f"R7-D(a): edit not visible on {tl}: {m!r}"
    # (b) a definition-bearing CSV pins it; a later concept edit is INVISIBLE (the pin holds).
    gid2 = _gl("r7d-b")
    csvd = b"source_term,target_term,definition\r\ngizmo,chisme,gadget-def\r\n"
    with Session(engine) as s:
        gloss_engine.import_bytes(s, None, csvd, SRC, TGT, glossary_id=gid2, filename="d.csv")
        cid2 = _entry(s, gid2, "gizmo", TGT).concept_id
    with Session(engine) as s:
        gc.edit_concept(s, gid2, cid2, definition="new-def")
    with Session(engine) as s:
        m = (_entry(s, gid2, "gizmo", TGT).meta or {}).get("definition")
        assert m == "gadget-def", f"R7-D(b): a legacy-pinned definition was overwritten: {m!r}"
    print("  R7-D: a concept edit reaches UNPINNED bindings only; a legacy pin holds OK")


def test_r7c_delete_target_repoints_or_refuses():
    """R7-C/A.4-3: deleting a bound target re-points to a usable sibling; the SOLE target of a
    non-DNT binding REFUSES rather than leaving an empty-target row."""
    gid = _gl("r7c")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "car", "status": "preferred"},
                {"locale": TGT, "term": "coche", "status": "preferred"},
                {"locale": TGT, "term": "auto", "status": "admitted"},
            ],
        )
        cid = c.id
        coche = s.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == cid, GlossaryTerm.term == "coche")
        ).first()
    with Session(engine) as s:
        gc.delete_concept_term(s, gid, cid, coche.id)
    with Session(engine) as s:
        e = _entry(s, gid, "car", TGT)
        assert e.target_term == "auto", f"R7-C: delete did not re-point: {e.target_term!r}"
        auto = s.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == cid, GlossaryTerm.term == "auto")
        ).first()
        try:
            gc.delete_concept_term(s, gid, cid, auto.id)
            _fail("R7-C: deleting the SOLE target of a non-DNT binding was not refused")
        except gc.ConceptError as exc:
            assert "only target" in str(exc), exc
    print("  R7-C: a bound-target delete re-points to a sibling, or refuses the sole target OK")


def test_r7m_dnt_off_refused():
    """R7-M: turning DNT off on a binding with no target term is refused on the service seam."""
    gid = _gl("r7m")
    with Session(engine) as s:
        c = gc.create_concept(
            s, gid, terms=[{"locale": SRC, "term": "Acme", "status": "preferred", "dnt": True}]
        )
        cid = c.id
        aid = s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == cid)).first().id
    with Session(engine) as s:
        try:
            gc.edit_concept_term(s, gid, cid, aid, dnt=False)
            _fail("R7-M: DNT-off on a targetless binding was not refused")
        except gc.ConceptError as exc:
            assert "target before clearing" in str(exc), exc
    print("  R7-M: DNT-off on a targetless binding is refused OK")


def test_two_source_synonyms_one_pair():
    """A.4-2: two usable SOURCE synonyms bound to one target in a pair are TWO bindings of ONE
    concept (the entry key is (term_id, target_locale))."""
    gid = _gl("synonyms")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": SRC, "term": "bill", "status": "admitted"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
            ],
        )
        cid = c.id
    with Session(engine) as s:
        ents = [e for e in _entries(s, gid) if e.concept_id == cid]
        srcs = sorted(e.source_term for e in ents)
        assert srcs == ["bill", "invoice"], f"A.4-2: expected two synonym bindings, got {srcs}"
        assert all(e.target_term == "factura" for e in ents), "synonyms must share the target"
    print("  A.4-2: two source synonyms in one pair are two bindings of one concept OK")


def test_bindings_pinned_and_idempotent_approved():
    """A.3 r6-12: a JSON seed then a meta-less CSV reports bindings_pinned>0; an identical CSV
    re-import reports changed 0 and keeps the glossary APPROVED."""
    gid = _gl("pinned")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "w",
                "definition": "a small gadget",
                "terms": [
                    {"term": "widget", "locale": SRC, "status": "preferred"},
                    {"term": "artilugio", "locale": TGT, "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
    csv = b"source_term,target_term\r\nwidget,artilugio\r\n"
    with Session(engine) as s:
        res = gloss_engine.import_bytes(s, None, csv, SRC, TGT, glossary_id=gid, filename="c.csv")
        assert res.get("bindings_pinned", 0) >= 1, f"bindings_pinned not reported: {res!r}"
    # approve, then an identical re-import must NOT revert to draft.
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="tester")
    with Session(engine) as s:
        gloss_engine.import_bytes(s, None, csv, SRC, TGT, glossary_id=gid, filename="c.csv")
    with Session(engine) as s:
        g = s.get(gm.Glossary, gid)
        assert g.status == GlossaryStatus.APPROVED, f"re-import reverted approval: {g.status}"
    print("  A.3: bindings_pinned reported; an idempotent re-import keeps APPROVED OK")


def test_projection_equivalence_editor_store():
    """A.5/R6-H2: project(backfill(strip(X))) == X for the derived columns on a store built via
    the editor (the override MAPS may differ; the DERIVED columns must not)."""
    from sqlalchemy import update as sa_update

    gid = _gl("equiv", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            definition="a billing document",
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
                {"locale": TGT2, "term": "facture", "status": "preferred"},
            ],
        )
        gm.add_term(s, gid, "cash", target_term="efectivo", source_locale=SRC, target_locale=TGT)

    def snap():
        with Session(engine) as s:
            return {
                (e.source_term, e.target_locale): (e.target_term, bool(e.dnt), e.meta)
                for e in _entries(s, gid)
            }

    before = snap()
    with Session(engine) as s:
        s.execute(
            sa_update(GlossaryEntry)
            .where(GlossaryEntry.glossary_id == gid)
            .values(meta_overrides=None, target_term_id=None)
        )
        s.commit()
        gc.backfill_pair_bindings(s, gid)
        gc.project(s, gid, derive_senses=True)
        s.commit()
    after = snap()
    assert after == before, f"A.5: equivalence broke:\n  before={before}\n  after={after}"
    print("  A.5: project(backfill(strip(X))) == X for the derived columns OK")


def test_set_pairs_widen_narrow_idempotent():
    """A.4-8: adding a pair WIDENS bindings (counted); an idempotent resubmit changes nothing; a
    removed pair leaves its bindings in place."""
    gid = _gl("widen")
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
                {"locale": TGT2, "term": "facture", "status": "preferred"},
            ],
        )
    with Session(engine) as s:
        res = gm.set_pairs(s, gid, [f"{SRC}:{TGT}", f"{SRC}:{TGT2}"])
        assert getattr(res, "bindings_created", 0) >= 1, f"widen created no bindings: {res!r}"
    with Session(engine) as s:
        e = _entry(s, gid, "invoice", TGT2)
        assert e is not None and e.target_term == "facture", "widen did not bind the fr pair"
        n_before = len(_entries(s, gid))
    with Session(engine) as s:
        res2 = gm.set_pairs(s, gid, [f"{SRC}:{TGT2}", f"{SRC}:{TGT}"])  # reordered resubmit
        assert getattr(res2, "bindings_created", 0) == 0, f"resubmit widened again: {res2!r}"
    with Session(engine) as s:
        assert len(_entries(s, gid)) == n_before, "resubmit changed the binding count"
    with Session(engine) as s:
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])  # narrow: drop fr
        assert _entry(s, gid, "invoice", TGT2) is not None, "narrow deleted a binding (must keep)"
    print("  A.4-8: set_pairs widen creates bindings, resubmit is a no-op, narrow keeps them OK")


def test_empty_target_binding_survives():
    """A.2-a: an empty-target non-DNT binding (orphan,,) is first-class and survives a projection
    and an unrelated write; it is never pruned."""
    gid = _gl("empty-target")
    csv = b"source_term,target_term\r\norphan,\r\n"
    with Session(engine) as s:
        gloss_engine.import_bytes(s, None, csv, SRC, TGT, glossary_id=gid, filename="o.csv")
    with Session(engine) as s:
        gm.add_term(s, gid, "other", target_term="otro", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        e = _entry(s, gid, "orphan", TGT)
        assert e is not None and (e.target_term or "") == "" and not e.dnt, (
            f"the empty-target binding was pruned or mutated: {e!r}"
        )
    print("  A.2-a: an empty-target non-DNT binding survives every write OK")


def test_homonym_two_locale_senses():
    """A.4-6/DIFF-1: a homonym sibling holding targets in TWO locales projects a sense on the base
    row of BOTH pairs."""
    gid = _gl("homonym", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        base = gc.create_concept(
            s,
            gid,
            homonym_group="hg-bank",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "banco", "status": "preferred"},
                {"locale": TGT2, "term": "banque", "status": "preferred"},
            ],
        )
        gc.create_concept(
            s,
            gid,
            homonym_group="hg-bank",
            domain="river",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "orilla", "status": "preferred"},
                {"locale": TGT2, "term": "rive", "status": "preferred"},
            ],
        )
        _ = base
    with Session(engine) as s:
        for tl, tgt in ((TGT, "orilla"), (TGT2, "rive")):
            e = _entry(s, gid, "bank", tl)
            senses = s.exec(
                select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
            ).all()
            surfaces = {sn.target_term for sn in senses}
            assert tgt in surfaces, f"A.4-6: no sibling sense {tgt!r} on {tl}: {surfaces}"
    print("  A.4-6/DIFF-1: a two-locale homonym sibling projects a sense in both pairs OK")


def test_meta_overrides_snapshot():
    """A.3 R4-M6: the authority column by writer class -- legacy non-empty -> override; a legacy
    blank cell on a shared concept -> TOMBSTONE; concept-first -> absent (None)."""
    # legacy override: add_term with meta pins the value.
    gid = _gl("ov-legacy")
    with Session(engine) as s:
        e = gm.add_term(
            s,
            gid,
            "note",
            target_term="nota",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "a short note"},
        )
        assert (e.meta_overrides or {}).get("definition") == "a short note", (
            f"legacy writer did not pin the override: {e.meta_overrides!r}"
        )
    # concept-first: create_concept pins NOTHING.
    gid2 = _gl("ov-concept")
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid2,
            definition="a billing document",
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
            ],
        )
        e = _entry(s, gid2, "invoice", TGT)
        assert e.meta_overrides is None, f"concept-first pinned overrides: {e.meta_overrides!r}"
        assert (e.meta or {}).get("definition") == "a billing document", "concept derive failed"
    print("  A.3: meta_overrides by writer class -- legacy override, concept-first absent OK")


# --------------------------------------------------------------------------- #
# Fix-round-1 counterfactuals: one test per HIGH/MED, each failing on 5ac14235.
# --------------------------------------------------------------------------- #
def test_h1_boot_invariant_heals_unbound_rows():
    """H-1: backfill_pair_bindings has a PRODUCTION caller. Strip both binding columns on a
    populated corpus, run the boot invariant, and assert every derived column is byte-identical
    (on 5ac14235 backfill_pair_bindings had zero callers, so meta_overrides stayed NULL forever
    and an unrelated import rewrote the row -- R7-A on installs that carry data)."""
    from sqlalchemy import update as sa_update

    gid = _gl("h1-boot", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        gm.add_term(
            s,
            gid,
            "widget",
            target_term="artilugio",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "a small device"},
        )
        gm.add_term(s, gid, "widget", target_term="", source_locale=SRC, target_locale=TGT2)

    def snap():
        with Session(engine) as s:
            return {
                (e.source_term, e.target_locale): (e.target_term, bool(e.dnt), e.meta)
                for e in _entries(s, gid)
            }

    before = snap()
    # simulate a legacy install: both binding columns NULL (an `alembic upgrade` of main rows).
    with Session(engine) as s:
        s.execute(
            sa_update(GlossaryEntry)
            .where(GlossaryEntry.glossary_id == gid)
            .values(meta_overrides=None, target_term_id=None)
        )
        s.commit()
    # the PRODUCTION boot invariant heals it (never a manual backfill call in the test).
    with Session(engine) as s:
        db._assert_glossary_pair_binding_invariant(s)
        s.commit()
    after = snap()
    assert after == before, (
        f"H-1: boot invariant did not reproduce the stored rows:\n{before}\n{after}"
    )
    print("  H-1: the boot invariant heals stripped bindings byte-identically OK")


def test_h2_propagation_survives_unrelated_pair_edit():
    """H-2: a target-only edit on ONE pair must NOT freeze a sibling pair's meta (the A.3-rejected
    repin-on-clobber did, killing R7-D). meta-less CSVs on two pairs, concept def=D1, a
    target-only update_entry on pair 1, then concept def=D2 -> BOTH pairs show D2."""
    gid = _gl("h2-prop", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        gloss_engine.import_bytes(
            s,
            None,
            b"source_term,target_term\r\ninvoice,factura\r\n",
            SRC,
            TGT,
            glossary_id=gid,
            filename="a.csv",
        )
        gloss_engine.import_bytes(
            s,
            None,
            b"source_term,target_term\r\ninvoice,facture\r\n",
            SRC,
            TGT2,
            glossary_id=gid,
            filename="b.csv",
        )
        cid = _entry(s, gid, "invoice", TGT).concept_id
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, definition="D1")
    with Session(engine) as s:
        e1 = _entry(s, gid, "invoice", TGT)
        gloss_engine.update_entry(s, e1.id, target_term="factura2")  # target-only, no meta
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, definition="D2")
    with Session(engine) as s:
        for tl in (TGT, TGT2):
            d = (_entry(s, gid, "invoice", tl).meta or {}).get("definition")
            assert d == "D2", f"H-2: propagation lost on {tl}: {d!r} (repin froze the sibling)"
    print("  H-2: a target-only edit does not freeze the sibling pair; D2 propagates to both OK")


def test_h2_law_term_1_late_shared_write_pins_sibling():
    """H-2 (LAW-TERM-1): when a legacy write to pair 1 first makes the shared concept definition
    non-empty, the OTHER legacy pair (which authored nothing) must STILL show nothing -- the
    write-time tombstone the removed repin used to (over-)apply."""
    gid = _gl("h2-lt1", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        # pair 2 written FIRST, blank definition -> records no override.
        gm.add_term(s, gid, "bill", target_term="facture", source_locale=SRC, target_locale=TGT2)
        # pair 1 written with a definition -> makes concept.definition non-empty.
        gm.add_term(
            s,
            gid,
            "bill",
            target_term="factura",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "D0"},
        )
    with Session(engine) as s:
        d1 = (_entry(s, gid, "bill", TGT).meta or {}).get("definition")
        d2 = (_entry(s, gid, "bill", TGT2).meta or {}).get("definition")
        assert d1 == "D0", f"H-2: the authoring pair lost its definition: {d1!r}"
        assert d2 is None, f"H-2: LAW-TERM-1 broke -- pair 2 derived a value it never had: {d2!r}"
    print("  H-2: a late shared legacy write tombstones the sibling that authored nothing OK")


def test_h3_concept_null_row_survives_projection():
    """H-3: an UN-BACKFILLED module row the projector cannot derive -- concept_id AND both
    binding columns (target_term_id/meta_overrides) NULL -- keeps its stored target/meta across
    a whole-glossary projection: it is SKIPPED with a WARNING, never blanked to src->'' (A.2-a).

    (Round-2 M-1 rewrite: the round-1 test nulled ONLY concept_id, leaving target_term_id +
    meta_overrides populated -- a shape that survives on BOTH trees, so it was not a
    counterfactual. Nulling the two binding columns too reproduces the exact un-backfilled shape
    5ac14235 blanked to ``keep -> ''`` / meta None; this now FAILS on 5ac14235 and is caught by
    MUT-H3, the mutation that re-blanks a non-derivable row.)"""
    from sqlalchemy import update as sa_update

    gid = _gl("h3-null")
    with Session(engine) as s:
        e = gm.add_term(
            s,
            gid,
            "keep",
            target_term="mantener",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "keep it"},
        )
        eid = e.id
    with Session(engine) as s:
        # the un-backfilled shape: concept link gone AND neither binding column set. On 5ac14235
        # the projector blanked this to source->'' with meta None; the H-3 skip leaves it stored.
        s.execute(
            sa_update(GlossaryEntry)
            .where(GlossaryEntry.id == eid)
            .values(concept_id=None, target_term_id=None, meta_overrides=None)
        )
        s.commit()
    with Session(engine) as s:
        gc.project(s, gid)  # whole-glossary projection
        s.commit()
    with Session(engine) as s:
        e = s.get(GlossaryEntry, eid)
        assert e.target_term == "mantener" and (e.meta or {}).get("definition") == "keep it", (
            f"H-3: an un-backfilled concept-less row was blanked by the projector: "
            f"{e.target_term!r} {e.meta!r}"
        )
    print("  H-3: an un-backfilled concept_id-NULL row survives a projection unblanked OK")


def _homonym_doc():
    return {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "homonym_group": "g",
                "domain": "finance",
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "banco", "status": "preferred"},
                ],
            },
            {
                "homonym_group": "g",
                "domain": "geography",
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "orilla", "status": "preferred"},
                ],
            },
        ],
    }


def test_h4_homonym_siblings_not_merged_on_reimport():
    """H-4: three id-less re-imports of two homonym siblings stay TWO concepts, each intact (on
    5ac14235 the content-identity reconcile merged them onto one stored concept)."""
    gid = _gl("h4-homonym")
    for i in range(3):
        with Session(engine) as s:
            gc.from_v1(s, gid, _homonym_doc())
        with Session(engine) as s:
            cs = s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
            doms = sorted((c.domain or "") for c in cs)
            assert doms == ["finance", "geography"], (
                f"H-4: import {i + 1} merged/stole siblings: {doms}"
            )
    print("  H-4: homonym siblings are never merged across id-less re-imports OK")


def test_h5_bare_base_document_idempotent():
    """H-5: a both-sides-base (bare `en`/`es`) document re-imports idempotently -- no duplicate
    concept, no ConceptError (on 5ac14235 the second import raised a legacy-key collision)."""
    gid = _gl("h5-base")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "terms": [
                    {"locale": "en", "term": "invoice", "status": "preferred"},
                    {"locale": "es", "term": "factura", "status": "preferred"},
                ]
            },
        ],
    }
    for i in range(3):
        with Session(engine) as s:
            gc.from_v1(s, gid, doc)  # must not raise
        with Session(engine) as s:
            n = len(s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all())
            assert n == 1, f"H-5: import {i + 1} minted a duplicate concept ({n})"
    print("  H-5: a bare-base document re-imports idempotently OK")


def test_m1_resolve_meta_uses_enforced_target():
    """M-1/A.2-g: _resolve_concept_meta reads the binding's target_term_id, so a synonym binding
    (`bill` -> `recibo`, admitted) renders recibo's metadata, not the preferred factura's."""
    gid = _gl("m1-enforced")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "inv",
                "terms": [
                    {"locale": SRC, "term": "invoice", "status": "preferred"},
                    {"locale": SRC, "term": "bill", "status": "admitted"},
                    {"locale": TGT, "term": "factura", "status": "preferred"},
                    {"locale": TGT, "term": "recibo", "status": "admitted"},
                ],
                "bindings": [
                    {
                        "source_term": "invoice",
                        "source_locale": SRC,
                        "target_term": "factura",
                        "target_locale": TGT,
                    },
                    {
                        "source_term": "bill",
                        "source_locale": SRC,
                        "target_term": "recibo",
                        "target_locale": TGT,
                    },
                ],
            },
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
    with Session(engine) as s:
        bill = _entry(s, gid, "bill", TGT)
        assert bill is not None and bill.target_term == "recibo", (
            f"M-1: bill did not bind recibo: {bill and bill.target_term!r}"
        )
        meta = gc._resolve_concept_meta(s, bill, None)
        assert meta["status"] == "admitted", (
            f"M-1: resolved the PREFERRED target's metadata, not the enforced: {meta['status']!r}"
        )
    print("  M-1: _resolve_concept_meta renders the ENFORCED target's metadata OK")


def test_m6_locale_edit_refused_on_bound_term():
    """M-6/A.4-4: a locale edit is REFUSED on a term that backs a binding; allowed on an unbound
    term."""
    gid = _gl("m6-locale")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
            ],
        )
        cid = c.id
        src_t = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == cid, GlossaryTerm.term == "invoice"
            )
        ).first()
        src_id = src_t.id
    with Session(engine) as s:
        try:
            gc.edit_concept_term(s, gid, cid, src_id, locale="en-GB")
            _fail("M-6: a locale edit on a bound source term was not refused")
        except gc.ConceptError as exc:
            assert "locale" in str(exc), exc
    print("  M-6: a locale edit on a bound term is refused OK")


def test_m7_both_directions_reimport():
    """M-7/LT3-1: a glossary declaring both directions of a pair exports and re-imports its own
    document (the collision refusal is bindings-aware; on 5ac14235 the restore was refused)."""
    gid = _gl("m7-lt31", pairs=((SRC, TGT), (TGT, SRC)))
    with Session(engine) as s:
        gm.add_term(s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "factura", target_term="bill", source_locale=TGT, target_locale=SRC)
    with Session(engine) as s:
        doc = gc.to_v1(s, gid)
    gid2 = _gl("m7-lt31-restore", pairs=((SRC, TGT), (TGT, SRC)))
    with Session(engine) as s:
        gc.from_v1(s, gid2, doc)  # must not raise
    with Session(engine) as s:
        pairs = {(e.source_term, e.target_term) for e in _entries(s, gid2)}
        assert ("invoice", "factura") in pairs and ("factura", "bill") in pairs, (
            f"M-7: both-directions restore lost an entry: {pairs}"
        )
    print("  M-7: a both-directions export re-imports (LT3-1 closed) OK")


def test_m5_verify_guard_catches_mismatch():
    """M-5/A.2-d (MUT-D): the VERIFY tail lives at its CALL SITE inside ``bulk_bind_touched``
    (the A.2-d bulk-import safety net), not only in ``project(verify=True)`` called directly.

    The round-1 test called ``project(verify=True)`` DIRECTLY, so deleting the CALL from
    ``bulk_bind_touched`` -- the whole bulk-import net -- stayed invisible (MUT-D GREEN) and it
    PASSED on 5ac14235. This drives ``bulk_bind_touched`` and corrupts a DERIVED column
    (``source_term``) the tail must catch, while the binding still re-resolves correctly (so the
    corruption is not masked by the un-backfilled skip). Removing the tail (MUT-D) makes
    ``bulk_bind_touched`` return without raising -> RED; on 5ac14235 the tail is absent -> RED."""
    from sqlalchemy import update as sa_update

    gid = _gl("m5-verify")
    with Session(engine) as s:
        e = gm.add_term(
            s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT
        )
        eid = e.id
    # a clean direct verify still counts the binding and writes nothing (the seam works).
    with Session(engine) as s:
        res = gc.project(s, gid, entry_ids=[eid], verify=True)
        assert res.get("bindings_verified") == 1, f"verify count wrong: {res!r}"
    # corrupt the STORED derived SOURCE column while the binding (concept/term/target) still
    # re-resolves to factura -- so bulk_bind_touched's re-resolution is unaffected and only its
    # VERIFY tail can catch the divergence (a corrupted target_term would break the re-resolution
    # and fall into the un-backfilled skip, masking the mutation).
    with Session(engine) as s:
        s.execute(
            sa_update(GlossaryEntry).where(GlossaryEntry.id == eid).values(source_term="WRONGSRC")
        )
        s.commit()
    # bulk_bind_touched (the CALL SITE) re-binds the touched row, then its project(verify=True)
    # tail must raise, naming the binding + the corrupted column, so the caller's savepoint rolls
    # back. Deleting the tail (MUT-D) makes this return without raising.
    with Session(engine) as s:
        try:
            gc.bulk_bind_touched(s, gid, {eid})
            _fail(
                "M-5: bulk_bind_touched returned without raising -- the A.2-d VERIFY tail is gone"
            )
        except gc.ConceptError as exc:
            assert f"binding {eid}" in str(exc) and "source_term" in str(exc), exc
        # VERIFY wrote nothing: the corrupted value is still stored (the tail asserts, never heals).
        assert s.get(GlossaryEntry, eid).source_term == "WRONGSRC", "VERIFY tail mutated the row"
    print("  M-5/A.2-d: bulk_bind_touched's VERIFY tail catches a mismatch by binding+column OK")


def test_r3m2_noop_row_save_keeps_propagation():
    """R3-M2: opening a row of a two-pair concept-first glossary and saving it UNCHANGED must not
    pin the pair -- a later concept definition edit still shows on it."""
    gid = _gl("r3m2", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            definition="D1",
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
                {"locale": TGT2, "term": "facture", "status": "preferred"},
            ],
        )
        cid = _entry(s, gid, "invoice", TGT).concept_id
        e = _entry(s, gid, "invoice", TGT)
        # a no-op save: target unchanged, meta not submitted (None -> no override change).
        gloss_engine.update_entry(s, e.id, target_term="factura")
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, definition="D2")
    with Session(engine) as s:
        d = (_entry(s, gid, "invoice", TGT).meta or {}).get("definition")
        assert d == "D2", f"R3-M2: a no-op row save froze the pair: {d!r}"
    print("  R3-M2: a no-op row save does not pin the pair; propagation survives OK")


def _clear_pair_binding_marker():
    """Delete the versioned pair-bindings completion marker so the NEXT
    ``db._backfill_glossary_concepts()`` re-runs the install-wide backfill AND commits
    (the commit at db.py's tail fires on ``pb_needs``)."""
    with Session(engine) as s:
        m = s.get(Setting, db._PAIR_BINDINGS_KEY)
        if m is not None:
            s.delete(m)
            s.commit()


def test_h1a_boot_wiring_calls_the_invariant():
    """H-1 (MUT-H1a): the boot seam ``db._backfill_glossary_concepts()`` CALLS the per-boot
    binding invariant. An ORPHAN SENSE (``concept_id`` NULL) is the row ONLY the invariant heals
    -- the versioned backfill (``backfill_pair_bindings``) never touches senses -- so removing
    the invariant CALL from ``_backfill_glossary_concepts`` (MUT-H1a) leaves the sense orphaned.
    Drives the boot seam, NOT the invariant directly (the round-1 test called the invariant)."""
    gid = _gl("h1a-boot", pairs=((SRC, TGT), (SRC, TGT2)))
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            homonym_group="hg",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "banco", "status": "preferred"},
            ],
        )
        gc.create_concept(
            s,
            gid,
            homonym_group="hg",
            domain="river",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "orilla", "status": "preferred"},
            ],
        )
    # find a derived sense on the base "bank"->"banco" row and ORPHAN it (concept_id NULL).
    with Session(engine) as s:
        base = _entry(s, gid, "bank", TGT)
        sense = s.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id == base.id)
        ).first()
        assert sense is not None and sense.concept_id is not None, "precondition: a linked sense"
        sid = sense.id
    from sqlalchemy import update as sa_update

    with Session(engine) as s:
        s.execute(sa_update(GlossarySense).where(GlossarySense.id == sid).values(concept_id=None))
        s.commit()
    _clear_pair_binding_marker()  # pb_needs -> True so the boot seam commits its heal
    db._backfill_glossary_concepts()
    with Session(engine) as s:
        healed = s.get(GlossarySense, sid)
        assert healed is not None and healed.concept_id is not None, (
            "H-1: the boot seam did not heal an orphan sense -- the invariant call is not wired"
        )
    print(
        "  H-1: db._backfill_glossary_concepts() calls the boot invariant (orphan sense healed) OK"
    )


def test_h1b_boot_wiring_runs_the_versioned_backfill():
    """H-1 (MUT-H1b): the boot seam ``db._backfill_glossary_concepts()`` runs the VERSIONED
    install-wide ``backfill_pair_bindings``. An EMPTY-TARGET binding carrying a meta override,
    stripped of both binding columns, is a row the invariant does NOT flag (its SQL predicate is
    ``target_term_id IS NULL AND target_term != ''``, and this row's target is blank) -- only the
    versioned backfill re-pins its ``meta_overrides``. Removing the versioned backfill block
    (MUT-H1b) leaves ``meta_overrides`` NULL."""
    from sqlalchemy import update as sa_update

    gid = _gl("h1b-boot")
    with Session(engine) as s:
        e = gm.add_term(
            s,
            gid,
            "orphan",
            target_term="",  # empty-target binding: target_term_id stays NULL
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "legacy-def"},
        )
        eid = e.id
    with Session(engine) as s:
        e = s.get(GlossaryEntry, eid)
        assert (e.meta_overrides or {}).get("definition") == "legacy-def", (
            f"precondition: the empty-target legacy write pins an override: {e.meta_overrides!r}"
        )
    # strip BOTH binding columns (a pre-T1b row shape); the invariant will NOT flag it because
    # target_term == "" fails its `target_term != ''` predicate.
    with Session(engine) as s:
        s.execute(
            sa_update(GlossaryEntry)
            .where(GlossaryEntry.id == eid)
            .values(target_term_id=None, meta_overrides=None)
        )
        s.commit()
    _clear_pair_binding_marker()  # pb_needs -> True so the versioned backfill re-runs + commits
    db._backfill_glossary_concepts()
    with Session(engine) as s:
        e = s.get(GlossaryEntry, eid)
        assert (e.meta_overrides or {}).get("definition") == "legacy-def", (
            "H-1: the versioned boot backfill did not re-pin an empty-target override "
            f"(meta_overrides={e.meta_overrides!r}) -- the backfill block is not wired"
        )
    print("  H-1: db._backfill_glossary_concepts() runs the versioned pair-bindings backfill OK")


def test_m2_edit_term_sets_case_sensitive():
    """M-2 (MUT-M2): ``glossary_module.edit_term`` carries ``case_sensitive`` through to the
    binding on all four surfaces (the module function they share). Dropping the keyword from
    ``edit_term`` (MUT-M2) leaves the flag unchanged."""
    gid = _gl("m2-cs")
    with Session(engine) as s:
        e = gm.add_term(
            s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT
        )
        eid = e.id
        assert e.case_sensitive is False, "precondition: case_sensitive starts False"
    with Session(engine) as s:
        gm.edit_term(s, gid, eid, case_sensitive=True)
    with Session(engine) as s:
        assert s.get(GlossaryEntry, eid).case_sensitive is True, (
            "M-2: edit_term did not SET case_sensitive on the binding"
        )
    with Session(engine) as s:
        gm.edit_term(s, gid, eid, case_sensitive=False)
    with Session(engine) as s:
        assert s.get(GlossaryEntry, eid).case_sensitive is False, (
            "M-2: edit_term did not CLEAR case_sensitive on the binding"
        )
    print("  M-2: glossary_module.edit_term sets/clears case_sensitive OK")


def test_m3_web_concept_term_edit_form_dnt_tristate():
    """M-3 (MUT-M3) + L-3: the concept editor renders a per-term EDIT form that POSTs to
    ``glossary_edit_concept_term`` with the DNT tri-state marker, so R7-I's DNT-OFF is reachable
    from the web. The form carries a hidden ``dnt_present`` companion; POSTing it with the box
    UNCHECKED turns DNT off on a term whose concept still has a usable target. Reverting the
    route's tri-state (MUT-M3, ``if dnt:`` only) makes the unchecked POST a no-op -> DNT stays ON;
    dropping the form from the template (L-3) removes the ``.../terms/{tid}/edit`` action."""
    gid = _gl("m3-web")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "brand", "status": "preferred", "dnt": True},
                {"locale": TGT, "term": "marca", "status": "preferred"},
            ],
        )
        cid = c.id
        src_t = s.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == cid, GlossaryTerm.term == "brand")
        ).first()
        tid = src_t.id
        assert src_t.dnt is True, "precondition: the source term is DNT"
    client = TestClient(app)
    # L-3: the term-edit form + its dnt_present marker are RENDERED in the concept editor.
    page = client.get(f"/glossaries/{gid}", follow_redirects=False)
    assert page.status_code == 200, f"glossary detail did not render: {page.status_code}"
    action = f'action="/glossaries/{gid}/concepts/{cid}/terms/{tid}/edit"'
    assert action in page.text, f"L-3: no web form posts to glossary_edit_concept_term ({action})"
    assert 'name="dnt_present"' in page.text, "L-3: the DNT tri-state marker is not in the form"
    # M-3: POST with dnt_present=1 and the box UNCHECKED (no `dnt`) turns DNT OFF.
    r = client.post(
        f"/glossaries/{gid}/concepts/{cid}/terms/{tid}/edit",
        data={"dnt_present": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"web edit_concept_term: expected 303, got {r.status_code}"
    with Session(engine) as s:
        assert s.get(GlossaryTerm, tid).dnt is False, (
            "M-3: dnt_present tri-state did not turn DNT OFF from the web (route reverted?)"
        )
    print("  M-3/L-3: the web concept-term edit form + DNT tri-state turn DNT off OK")


def test_l1_wider_restore_reports_bindings_skipped():
    """L-1: a bindings-bearing concept is AUTHORITATIVE and is NOT auto-widened into a configured
    pair its bindings never named (M-7/LT3-1). Restoring a ONE-pair export (bindings name only the
    es-ES pair) into a TWO-pair (es-ES + fr-FR) glossary leaves the fr-FR pair UNBOUND. The import
    now COUNTS it as ``bindings_skipped`` (with a WARNING naming the pair) instead of the round-1
    silent skip; the fr-FR binding is still NOT created (M-7 parity)."""
    gid = _gl("l1-wider", pairs=((SRC, TGT), (SRC, TGT2)))
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "inv",
                "terms": [
                    {"locale": SRC, "term": "invoice", "status": "preferred"},
                    {"locale": TGT, "term": "factura", "status": "preferred"},
                    {"locale": TGT2, "term": "facture", "status": "preferred"},
                ],
                # a one-pair export: the bindings block names ONLY the es-ES pair.
                "bindings": [
                    {
                        "source_term": "invoice",
                        "source_locale": SRC,
                        "target_term": "factura",
                        "target_locale": TGT,
                    }
                ],
            }
        ],
    }
    with Session(engine) as s:
        res = gc.from_v1(s, gid, doc)
    assert res.get("bindings_skipped", 0) >= 1, (
        f"L-1: a wider restore did not count the skipped fr-FR pair: {res!r}"
    )
    with Session(engine) as s:
        assert _entry(s, gid, "invoice", TGT) is not None, "L-1: the es-ES binding is missing"
        assert _entry(s, gid, "invoice", TGT2) is None, (
            "L-1: the fr-FR pair was auto-widened -- the M-7/LT3-1 authority rule regressed"
        )
    print("  L-1: a bindings-bearing restore into a wider glossary counts bindings_skipped OK")


def main():
    test_projector_is_sole_writer_of_derived()
    test_r7a_import_touches_only_named()
    test_r7d_concept_edit_visible_on_unpinned_only()
    test_r7c_delete_target_repoints_or_refuses()
    test_r7m_dnt_off_refused()
    test_two_source_synonyms_one_pair()
    test_bindings_pinned_and_idempotent_approved()
    test_projection_equivalence_editor_store()
    test_set_pairs_widen_narrow_idempotent()
    test_empty_target_binding_survives()
    test_homonym_two_locale_senses()
    test_meta_overrides_snapshot()
    # fix-round-1 counterfactuals (one per HIGH/MED, each failing on 5ac14235)
    test_h1_boot_invariant_heals_unbound_rows()
    test_h2_propagation_survives_unrelated_pair_edit()
    test_h2_law_term_1_late_shared_write_pins_sibling()
    test_h3_concept_null_row_survives_projection()
    test_h4_homonym_siblings_not_merged_on_reimport()
    test_h5_bare_base_document_idempotent()
    test_m1_resolve_meta_uses_enforced_target()
    test_m6_locale_edit_refused_on_bound_term()
    test_m7_both_directions_reimport()
    test_m5_verify_guard_catches_mismatch()
    test_r3m2_noop_row_save_keeps_propagation()
    # round-2 M-1/L-1/L-3 cleanup: real boot-wiring + surface counterfactuals
    test_h1a_boot_wiring_calls_the_invariant()
    test_h1b_boot_wiring_runs_the_versioned_backfill()
    test_m2_edit_term_sets_case_sensitive()
    test_m3_web_concept_term_edit_form_dnt_tristate()
    test_l1_wider_restore_reports_bindings_skipped()
    print("GLOSSARY AUTHORITY OK")


if __name__ == "__main__":
    main()
