"""Concept/term store CRUD + the LAW-TERM-2b delete units (SPEC-terminology-backend T1).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Covers:

  * concept/term CRUD through the service seams; enum validation + audience canonicalize.
  * homonym create + sense identity: editing a homonym concept twice leaves EXACTLY ONE
    sense row (H2); a 2-sense term is 1 entry + 2 senses and reconcile is a fixed point.
  * H3 ingestion cap: a definition + domain carrying newlines + a structure token are
    flatten+500-capped and the derived meta still satisfies the legacy cap; a truncated
    field is COUNTED (truncated_fields, M7).
  * the LAW-TERM-2b delete UNIT (anti-resurrection):
      (i)     delete a single-pair term -> no concept, no source/target term; reconcile;
              a JSON export omits it.
      (i-bis) SHARED SOURCE TERM (H1): a three-language concept (en->es/en->fr/en->de
              sharing the en source), delete en->es -> the shared source term + the other
              two pairs SURVIVE with live term_ids and NO IntegrityError, reconcile is a
              no-op; only after the LAST pair is deleted does the source term go.
      (ii)    delete a multi-sense term through the grid -> reconcile -> neither base nor
              any sense row returns.
      (iii)   delete a whole glossary holding concepts/terms.
      (iv)    delete a project with legacy entries -> NOTHING concept-side moves (H7).
      (v)     delete a term that is ANOTHER glossary's deprecated_replacement -> inbound
              ref cleared first, no IntegrityError (M11).

Final line: "GLOSSARY CONCEPTS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_glcon_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    GlossaryConcept,
    GlossaryEntry,
    GlossarySense,
    GlossaryTerm,
    Project,
    TermStatus,
)
from app.services import glossary as gloss_engine
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm
from app.services import prompts, quality

init_db()

# T1-lite: this suite exercises the concept-editor WRITE seams, which are gated behind the
# DEFAULT-OFF terminology_concept_editor flag; enable it for the suite's fixtures. (The
# flag-OFF refusals are pinned separately in verify_glossary_concept_flag.py.)
from app.services import settings as _settings

_settings.set_many({"terminology_concept_editor_enabled": "1"})
_settings.invalidate_cache()

SRC, TGT = "en-US", "es-ES"


def _gl(name, pairs=((SRC, TGT),)):
    with Session(engine) as s:
        g = gm.create(s, name)
        gid = g.id
        gm.set_pairs(s, gid, [f"{a}:{b}" for a, b in pairs])
    return gid


def _concepts_of(s, gid):
    return s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()


def _terms_of(s, gid):
    """All GlossaryTerm rows whose concept belongs to this glossary (the shared-DB verify
    script holds several glossaries at once, so global selects must be gid-scoped)."""
    cids = [c.id for c in _concepts_of(s, gid)]
    if not cids:
        return []
    return s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(cids))).all()


def _entries_of(s, gid):
    return s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()


def test_enum_validation():
    assert gc.canon_audience(["llm", "human", "bogus"]) == "human,llm"
    assert gc.canon_audience("") == "human,mt,llm"
    assert gc.canon_audience("mt|junk") == "human,mt,llm"  # junk-only string -> default
    assert gc.canon_audience("mt") == "mt"
    assert gc._valid_status("forbidden") == "forbidden"
    assert gc._valid_status("garbage") == TermStatus.PREFERRED
    print("  enums: audience canonicalized (sorted subset), status validated")


def test_crud_and_dual_write():
    gid = _gl("CRUD GL")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            definition="a login action",
            terms=[
                {"locale": SRC, "term": "login", "status": "preferred", "audience": ["llm"]},
                {"locale": TGT, "term": "acceso", "status": "preferred"},
            ],
        )
        cid = c.id
    with Session(engine) as s:
        # rematerialize projected a compat entry (concept editor is the primary write)
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "login"
            )
        ).first()
        assert e is not None and e.target_term == "acceso", "concept create did not project entry"
        assert e.concept_id == cid, "projected entry not linked to the concept"
    print("  crud: create_concept projects a linked compat entry (dual-write)")


def test_homonym_sense_identity():
    """Editing a homonym concept's target twice UPDATES the one sense row (H2)."""
    gid = _gl("Homonym GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "river", "orilla", context_cue="river water")
    with Session(engine) as s:
        e = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "bank")).first()
        # editing the entry (dual-write re-sync) twice must not multiply sense siblings
        gm.edit_term(s, gid, e.id, target_term="banco2")
        gm.edit_term(s, gid, e.id, target_term="banco3")
    with Session(engine) as s:
        e = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "bank")).first()
        senses = s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)).all()
        assert len(senses) == 1, f"expected EXACTLY 1 sense after edits, got {len(senses)}"
        base = s.get(GlossaryConcept, e.concept_id)
        sib = s.get(GlossaryConcept, senses[0].concept_id)
        assert sib is not None and sib.homonym_group == base.homonym_group, "sibling group drift"
    print("  homonym: edit x2 -> exactly 1 sense row; base + sibling share homonym_group")


def test_ingestion_cap_counted():
    """H3/M7: a definition + domain with newlines + a structure token are flatten+capped,
    the derived meta stays within the legacy cap, and truncation is COUNTED."""
    gid = _gl("Cap GL")
    long_def = "line one\nline two {inject}" + ("x" * 700)
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "c1",
                "definition": long_def,
                "domain": "dom\nain {tok}",
                "terms": [{"locale": SRC, "term": "widget", "status": "preferred"}],
            }
        ],
    }
    with Session(engine) as s:
        counters = gc.from_v1(s, gid, doc)
    with Session(engine) as s:
        c = s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).first()
        assert "\n" not in c.definition and "\n" not in c.domain, "newline survived sanitization"
        assert len(c.definition) <= gloss_engine._META_VALUE_CAP, "definition exceeds store cap"
    assert counters["truncated_fields"] >= 1, f"truncation not counted: {counters}"
    print("  ingestion: definition/domain flatten+capped; truncated_fields counted (M7)")


def _three_language_concept(gid):
    """A concept with an en source term + es/fr/de targets -> 3 entries sharing the source."""
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "c1",
                "terms": [
                    {"locale": "en-US", "term": "bank", "status": "preferred"},
                    {"locale": "es-ES", "term": "banco", "status": "preferred"},
                    {"locale": "fr-FR", "term": "banque", "status": "preferred"},
                    {"locale": "de-DE", "term": "Bank", "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)


def test_delete_unit_single_pair():
    """(i) delete a single-pair term -> no concept, no source/target term; export omits it."""
    gid = _gl("Del1 GL")
    with Session(engine) as s:
        gm.add_term(s, gid, "solo", target_term="unico", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        e = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "solo")).first()
        cid = e.concept_id
        gm.remove_term(s, gid, e.id)
    with Session(engine) as s:
        assert s.get(GlossaryConcept, cid) is None, "base concept survived a single-pair delete"
        assert not _terms_of(s, gid), "orphan term(s) survived the delete"
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        assert not _entries_of(s, gid), "reconcile resurrected the deleted entry"
        doc = gc.to_v1(s, gid)
        assert not doc["concepts"], "JSON export still carries the deleted concept"
    print("  delete(i): single-pair -> no concept/term, reconcile no-op, export omits it")


def test_delete_unit_shared_source_h1():
    """(i-bis) H1: delete one pair of a 3-language concept -> shared source + others survive."""
    gid = _gl("DelH1 GL", pairs=[("en-US", "es-ES"), ("en-US", "fr-FR"), ("en-US", "de-DE")])
    _three_language_concept(gid)
    with Session(engine) as s:
        entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        assert len(entries) == 3, f"expected 3 projected pairs, got {len(entries)}"
        src_ids = {e.term_id for e in entries}
        assert len(src_ids) == 1, "the three pairs must SHARE one source term (H1 premise)"
        shared_src_id = next(iter(src_ids))
        es_entry = next(e for e in entries if e.target_locale == "es-ES")
        gm.remove_term(s, gid, es_entry.id)
    with Session(engine) as s:
        assert s.get(GlossaryTerm, shared_src_id) is not None, "shared source term dropped (H1)"
        survivors = _entries_of(s, gid)
        assert len(survivors) == 2, f"expected 2 survivors, got {len(survivors)}"
        assert all(e.term_id == shared_src_id for e in survivors), "survivor term_id dangled"
        # the es target term is gone; fr/de remain
        surfaces = {t.term for t in _terms_of(s, gid)}
        assert "banco" not in surfaces, "orphaned es target survived"
        assert {"banque", "Bank", "bank"} <= surfaces, "a surviving term was wrongly dropped"
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        again = _entries_of(s, gid)
        assert len(again) == 2, "reconcile after H1 delete was not a no-op"
        # now delete the LAST two pairs -> source term finally goes
        for e in again:
            gm.remove_term(s, gid, e.id)
    with Session(engine) as s:
        assert s.get(GlossaryTerm, shared_src_id) is None, "source term survived after last pair"
    print("  delete(i-bis): shared source term + other pairs survive; source goes with last pair")


def test_delete_unit_multi_sense():
    """(ii) delete a multi-sense term through the grid -> reconcile -> nothing returns."""
    gid = _gl("Del2 GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "term", target_term="base", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "s1", "sense1", context_cue="one")
        gm.add_sense(s, gid, e.id, "s2", "sense2", context_cue="two")
    with Session(engine) as s:
        e = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "term")).first()
        gm.remove_term(s, gid, e.id)
    with Session(engine) as s:
        assert not _concepts_of(s, gid), "concept/sibling survived multi-sense delete"
        assert not _terms_of(s, gid), "orphan term survived"
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        assert not _entries_of(s, gid), "reconcile resurrected the deleted term/senses"
    print("  delete(ii): multi-sense term + siblings gone, reconcile resurrects nothing")


def test_delete_unit_whole_glossary():
    """(iii) delete a glossary holding concepts/terms -> everything gone, no FK error."""
    gid = _gl("Del3 GL")
    with Session(engine) as s:
        gm.add_term(s, gid, "a", target_term="aa", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "b", target_term="bb", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        term_ids = [t.id for t in _terms_of(s, gid)]
        gm.delete(s, gid)
    with Session(engine) as s:
        assert not _concepts_of(s, gid), "concepts survived a whole-glossary delete"
        assert all(s.get(GlossaryTerm, tid) is None for tid in term_ids), (
            "terms survived a whole-glossary delete"
        )
    print("  delete(iii): whole-glossary delete drops concepts/terms, no FK error")


def test_delete_project_legacy_no_op():
    """(iv/H7) deleting a project with legacy entries moves NOTHING concept-side."""
    from app.services import content

    gid = _gl("Del4 GL")
    with Session(engine) as s:
        gm.add_term(s, gid, "keep", target_term="mantener", source_locale=SRC, target_locale=TGT)
        p = Project(name="ToDelete", source_locale=SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_term="legacy",
                target_term="heredado",
            )
        )
        s.commit()
        before = len(s.exec(select(GlossaryConcept)).all())
    with Session(engine) as s:
        content.delete_project(s, pid)
    with Session(engine) as s:
        after = len(s.exec(select(GlossaryConcept)).all())
        assert after == before, "delete_project touched the concept store (LAW-TERM-2a violated)"
        assert (
            s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "keep")).first()
            is not None
        ), "an unrelated module entry was harmed"
    print("  delete(iv): delete_project is a concept-store NO-OP for legacy entries")


def test_delete_cross_glossary_replacement():
    """(v/M11) deleting a term another glossary points at via deprecated_replacement
    clears the inbound ref first -> no IntegrityError."""
    g1 = _gl("Repl A")
    g2 = _gl("Repl B")
    with Session(engine) as s:
        # a preferred term in g1, and a deprecated term in g2 whose replacement is g1's term
        c1 = gc.create_concept(
            s, g1, terms=[{"locale": SRC, "term": "good", "status": "preferred"}]
        )
        c2 = gc.create_concept(
            s, g2, terms=[{"locale": SRC, "term": "bad", "status": "deprecated"}]
        )
        t1 = s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == c1.id)).first()
        t2 = s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == c2.id)).first()
        t2.deprecated_replacement_term_id = t1.id
        s.add(t2)
        s.commit()
        t1_id = t1.id
    with Session(engine) as s:
        gm.delete(s, g1)  # drops t1 -> must clear g2's inbound ref first
    with Session(engine) as s:
        t2b = s.exec(select(GlossaryTerm).where(GlossaryTerm.term == "bad")).first()
        assert t2b is not None, "the pointing term was wrongly deleted"
        assert t2b.deprecated_replacement_term_id is None, "inbound replacement ref not cleared"
        assert s.get(GlossaryTerm, t1_id) is None, "target term not deleted"
    print("  delete(v): cross-glossary deprecated_replacement cleared first, no FK error")


def test_delete_concept_term_unit():
    """H1/M10 (gate round-1): delete a concept's SOURCE term through the editor seam every
    surface calls → no FK IntegrityError escapes; the linked entry (+ senses) is gone; the
    concept goes when termless."""
    gid = _gl("DelTerm GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "solo", target_term="unico", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        e = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "solo")).first()
        src_tid = e.term_id  # the source GlossaryTerm the entry references
        cid = e.concept_id
        ok = gc.delete_concept_term(s, gid, cid, src_tid)  # would 500 on FK error pre-fix
        assert ok is True
    with Session(engine) as s:
        # H1 core: the FK-referencing entry is deleted FIRST, so the source-term delete
        # raises no IntegrityError (pre-fix it 500'd every surface with a poisoned session).
        assert s.get(GlossaryTerm, src_tid) is None, "source term survived"
        assert not _entries_of(s, gid), "the linked entry was not deleted (H1)"
    # deleting the SOLE remaining (target) term then empties the concept (LAW-TERM-2b
    # "concept iff zero terms").
    with Session(engine) as s:
        leftover = _terms_of(s, gid)
        for t in leftover:
            gc.delete_concept_term(s, gid, t.concept_id, t.id)
    with Session(engine) as s:
        assert not _terms_of(s, gid) and not _concepts_of(s, gid), "termless concept survived"
    print("  delete-TERM unit (H1): source-term delete removes the entry, no FK error")


def test_delete_concept_sibling_unit():
    """H2/M10: delete a homonym SIBLING concept (it projects a GlossarySense, not an entry)
    → its sense + terms go, the BASE entry/concept survive, no FK IntegrityError."""
    gid = _gl("DelSib GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "river", "orilla", context_cue="river water")
    with Session(engine) as s:
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
            )
        ).first()
        base_cid = e.concept_id
        sense = s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)).first()
        sib_cid = sense.concept_id
        assert sib_cid and sib_cid != base_cid, "no distinct sibling concept to delete"
        ok = gc.delete_concept(s, gid, sib_cid)  # would 500 on FK error pre-fix
        assert ok is True
    with Session(engine) as s:
        assert s.get(GlossaryConcept, sib_cid) is None, "sibling concept survived"
        assert s.get(GlossaryConcept, base_cid) is not None, "base concept wrongly removed (H2)"
        base_e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
            )
        ).first()
        assert base_e is not None and base_e.target_term == "banco", "base entry harmed"
        assert not s.exec(select(GlossarySense).where(GlossarySense.concept_id == sib_cid)).all(), (
            "the sibling's sense survived"
        )
    print("  delete-SENSE unit (H2): sibling concept + sense gone, base survives, no FK error")


def test_rematerialize_never_empty_target():
    """H3: rematerialize must never PROJECT or LEAVE a compat entry with an empty
    target_term and dnt=False (which renders `src→` into the prompt)."""
    gid = _gl("H3 GL", pairs=[("en-US", "es-ES"), ("en-US", "fr-FR")])
    with Session(engine) as s:
        # a concept with an en source + an es target only (no fr target) — pass 2 must NOT
        # manufacture an en→fr empty-target entry.
        gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": "en-US", "term": "alpha", "status": "preferred"},
                {"locale": "es-ES", "term": "alfa", "status": "preferred"},
            ],
        )
    with Session(engine) as s:
        for e in _entries_of(s, gid):
            assert e.target_term.strip() or e.dnt, (
                f"empty-target non-DNT entry manufactured: {e.source_term!r}→{e.target_term!r}"
            )
        # the prompt line for the fr pair must not be `alpha→` — match_terms drops it.
        for tgt in ("es-ES", "fr-FR"):
            m = gloss_engine.match_terms(s, None, "use the alpha value", SRC, tgt, glossary_id=gid)
            line = prompts.glossary_line(m)
            assert not any(part.endswith("→") for part in line.split(";")), (
                f"glossary_line emitted an empty target (x→) for {tgt}: {line!r}"
            )
    # pass 1 must not blank a LIVE entry when its sole target is demoted to deprecated.
    with Session(engine) as s:
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.target_locale == "es-ES"
            )
        ).first()
        concept_id = e.concept_id
        alfa = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == concept_id, GlossaryTerm.term == "alfa"
            )
        ).first()
        # LAW-TERM-18 A.4-3: demoting the LAST usable bound target of a non-DNT binding is
        # REFUSED (T1b replaces T1-lite's silent prune) -- the row keeps its enforced target.
        try:
            gc.edit_concept_term(s, gid, concept_id, alfa.id, status="deprecated")
            raise AssertionError("expected A.4-3 refusal demoting the sole target")
        except gc.ConceptError as exc:
            assert "only target" in str(exc), exc
    with Session(engine) as s:
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.target_locale == "es-ES"
            )
        ).first()
        assert e is None or e.target_term.strip() or e.dnt, (
            "a live non-DNT entry's target was blanked (A.4-3 should have refused)"
        )
    print("  A.4-3: demoting the sole non-DNT target is refused; no empty-target 'x->' row")


def test_dualwrite_no_clobber_status_audience():
    """M1 + R2-4 (gate round 2): a concept-editor-set field on the SOURCE and TARGET terms is
    NOT reset by a later legacy-grid edit of the same entry — including the target term's
    part_of_speech / context_example / case_sensitive (the R2-4 defect)."""
    gid = _gl("M1 GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "login", target_term="acceso", source_locale=SRC, target_locale=TGT)
        cid, src_tid = e.concept_id, e.term_id
        gc.edit_concept_term(s, gid, cid, src_tid, audience=["human"], term_type="ui")
        # R2-4: rich fields on the TARGET term
        tgt = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == cid, GlossaryTerm.term == "acceso"
            )
        ).first()
        tgt_tid = tgt.id
        gc.edit_concept_term(
            s,
            gid,
            cid,
            tgt_tid,
            part_of_speech="noun",
            context_example="el acceso",
            case_sensitive=True,
            audience=["human"],
        )
    with Session(engine) as s:
        # a legacy-grid edit of the SAME entry must leave the concept-set fields intact
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "login"
            )
        ).first()
        gm.edit_term(s, gid, e.id, target_term="acceso")  # re-write the same target
    with Session(engine) as s:
        src = s.get(GlossaryTerm, src_tid)
        assert src.audience == "human", f"legacy edit clobbered source audience: {src.audience!r}"
        assert src.term_type == "ui", f"legacy edit clobbered source term_type: {src.term_type!r}"
        tgt = s.get(GlossaryTerm, tgt_tid)
        assert tgt.part_of_speech == "noun", f"target pos clobbered: {tgt.part_of_speech!r}"
        assert tgt.context_example == "el acceso", f"target ctx clobbered: {tgt.context_example!r}"
        assert tgt.case_sensitive is True, "target case_sensitive clobbered (R2-4)"
        assert tgt.audience == "human", f"target audience clobbered: {tgt.audience!r}"
    print("  M1/R2-4: a legacy edit does not reset source OR target concept-editor fields")


def test_csv_audience_filters_llm_channel():
    """M2: a CSV-set `audience=human` on a term actually drops it from the llm channel
    (the SOURCE term is the single audience owner; the filter reads it)."""
    gid = _gl("M2 GL")
    csv = (
        "source_term,target_term,dnt,sense_label,context_cue,part_of_speech,definition,"
        "usage_example,subject_field,notes,status,term_type,audience,grammatical_gender,"
        "grammatical_number,geographical_usage\n"
        "gizmo,chisme,,,,,,,,,preferred,,human,,,\n"
        "widget,artilugio,,,,,,,,,preferred,,human|mt|llm,,,\n"
    )
    with Session(engine) as s:
        gm.import_bytes(s, gid, csv.encode("utf-8"), SRC, TGT, filename="a.csv")
    with Session(engine) as s:
        mc = gc.match_concepts(s, None, "the gizmo and the widget", SRC, TGT, glossary_id=gid)
        srcs = {m["source_term"] for m in mc}
        assert {"gizmo", "widget"} <= srcs, f"terms not matched: {srcs}"
        llm = {m["source_term"] for m in gc.filter_by_audience(mc, "llm")}
        assert "gizmo" not in llm, "a CSV audience=human term still reached the llm channel (M2)"
        assert "widget" in llm, "the all-audience term was wrongly dropped"
    print("  M2: CSV-set audience=human drops the term from the llm channel; source-owned")


def _entry_pairs(s, gid):
    return {(e.source_term, e.target_term) for e in _entries_of(s, gid)}


def test_json_roundtrip_sense_and_synonym():
    """R2-3 (gate round 2): a .glossary.json carrying a HOMONYM GROUP (base + sibling ->
    a sense) and a SYNONYM concept (2 source terms) IMPORTS and projects into the compat
    store, and export -> import -> export is a fixed point (the product's own export is
    re-importable)."""
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "c1",
                "homonym_group": "g1",
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "banco", "status": "preferred"},
                ],
            },
            {
                "external_id": "c2",
                "homonym_group": "g1",
                "domain": "river",
                "definition": "the shore",
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "orilla", "status": "preferred"},
                ],
            },
            {
                "external_id": "c3",
                "terms": [
                    {"locale": SRC, "term": "car", "status": "preferred"},
                    {"locale": SRC, "term": "auto", "status": "preferred"},
                    {"locale": TGT, "term": "coche", "status": "preferred"},
                ],
            },
        ],
    }
    gid1 = _gl("RT1 GL")
    with Session(engine) as s:
        gc.from_v1(s, gid1, doc)
    with Session(engine) as s:
        pairs = _entry_pairs(s, gid1)
        assert pairs == {("bank", "banco"), ("car", "coche"), ("auto", "coche")}, pairs
        eids = [e.id for e in _entries_of(s, gid1)]
        senses = s.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))
        ).all()
        got = [(x.sense_label, x.target_term) for x in senses]
        assert len(senses) == 1 and senses[0].target_term == "orilla", (
            f"homonym sibling did not project a sense: {got}"
        )
        doc1 = gc.to_v1(s, gid1)
    # re-import the export into a FRESH glossary — the round-trip must reproduce it
    gid2 = _gl("RT2 GL")
    with Session(engine) as s:
        gc.from_v1(s, gid2, doc1)
    with Session(engine) as s:
        assert _entry_pairs(s, gid2) == pairs, "export -> import lost/added a compat entry"
        eids2 = [e.id for e in _entries_of(s, gid2)]
        senses2 = s.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids2))
        ).all()
        assert len(senses2) == 1 and senses2[0].target_term == "orilla", "sense lost on round-trip"
    print("  R2-3: a sense + synonym .glossary.json imports and export->import->export round-trips")


def test_json_self_restore_is_noop():
    """R2-2 (gate round 2): to_v1 -> from_v1 into the SAME glossary is a no-op for concepts,
    terms AND entries (the synthesized external_id is persisted, so the re-import upserts
    instead of doubling the store + duplicating every constraint in the prompt)."""
    gid = _gl("Self GL")
    with Session(engine) as s:
        gm.add_term(s, gid, "alpha", target_term="alfa", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "beta", target_term="beta-es", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        before = (len(_concepts_of(s, gid)), len(_terms_of(s, gid)), len(_entries_of(s, gid)))
        doc = gc.to_v1(s, gid)
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)  # self-restore of the product's own export
    with Session(engine) as s:
        after = (len(_concepts_of(s, gid)), len(_terms_of(s, gid)), len(_entries_of(s, gid)))
        assert after == before, f"self-restore doubled the store: {before} -> {after}"
        m = gc.match_concepts(s, None, "alpha and beta", SRC, TGT, glossary_id=gid)
        assert len({x["source_term"] for x in m}) == len(m), (
            f"self-restore duplicated a compat match: {[x['source_term'] for x in m]}"
        )
    print("  R2-2: to_v1 -> from_v1 into the same glossary is a no-op (concepts/terms/entries)")


def test_delete_target_term_removes_entry():
    """LAW-TERM-18 A.4-3: deleting the SOLE bound target of a non-DNT binding is REFUSED (the
    row must never be left enforcing an empty target); the operator deletes the glossary row or
    adds another target first. The binding keeps enforcing its target until then."""
    gid = _gl("TgtDel GL")
    with Session(engine) as s:
        e = gm.add_term(
            s, gid, "gadget", target_term="aparato", source_locale=SRC, target_locale=TGT
        )
        cid = e.concept_id
    with Session(engine) as s:
        tgt = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == cid, GlossaryTerm.term == "aparato"
            )
        ).first()
        try:
            gc.delete_concept_term(s, gid, cid, tgt.id)
            raise AssertionError("expected A.4-3 refusal deleting the sole target")
        except gc.ConceptError as exc:
            assert "only target" in str(exc), exc
    with Session(engine) as s:
        m = gloss_engine.match_terms(s, None, "the gadget here", SRC, TGT, glossary_id=gid)
        assert m and m[0]["target_term"] == "aparato", f"binding lost its target after refusal: {m}"
    print("  A.4-3: deleting the sole non-DNT target is refused; the binding keeps enforcing")


def test_create_concept_rejects_shared_source_surface():
    """R2-5 (gate round 2): a second UN-GROUPED concept sharing a source surface is refused
    (it would project two conflicting compat entries for one source term)."""
    gid = _gl("R5 GL")
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "banco", "status": "preferred"},
            ],
        )
        try:
            gc.create_concept(
                s,
                gid,
                terms=[
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "orilla", "status": "preferred"},
                ],
            )
            raise AssertionError("a second ungrouped concept sharing 'bank' was not refused (R2-5)")
        except gc.ConceptError:
            pass
    with Session(engine) as s:
        assert _entry_pairs(s, gid) == {("bank", "banco")}, "the store holds a conflicting entry"
    # R3-4: the SAME collision through the RENAME and ADD-TERM seams is also refused.
    with Session(engine) as s:
        c2 = gc.create_concept(
            s,
            gid,
            terms=[
                {"locale": SRC, "term": "shore", "status": "preferred"},
                {"locale": TGT, "term": "orilla", "status": "preferred"},
            ],
        )
        shore = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == c2.id, GlossaryTerm.term == "shore"
            )
        ).first()
        try:
            gc.edit_concept_term(s, gid, c2.id, shore.id, term="bank")  # rename onto 'bank'
            raise AssertionError("edit_concept_term rename onto a rival surface not refused (R3-4)")
        except gc.ConceptError:
            pass
        # add_concept_term of a colliding surface (a DIFFERENT concept owns 'bank')
        try:
            gc.add_concept_term(s, gid, c2.id, locale=SRC, term="bank", status="preferred")
            raise AssertionError("add_concept_term of a rival surface not refused (R3-4)")
        except gc.ConceptError:
            pass
    print("  R2-5/R3-4: a shared source surface is refused on create, rename AND add-term")


def test_synonym_added_in_place_equals_fresh_import():
    """R3-2 (gate round 3): adding a synonym IN PLACE to an already-projected concept
    produces the SAME compat projection as importing the identical exported document into a
    fresh glossary (the projection must run for linked concepts, not only fresh imports)."""
    gid_a = _gl("R3-2A GL")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid_a,
            terms=[
                {"locale": SRC, "term": "car", "status": "preferred"},
                {"locale": TGT, "term": "coche", "status": "preferred"},
            ],
        )
        cid = c.id
    with Session(engine) as s:
        gc.add_concept_term(s, gid_a, cid, locale=SRC, term="automobile", status="preferred")
    with Session(engine) as s:
        pairs_a = _entry_pairs(s, gid_a)
        doc_a = gc.to_v1(s, gid_a)
    gid_b = _gl("R3-2B GL")
    with Session(engine) as s:
        gc.from_v1(s, gid_b, doc_a)
    with Session(engine) as s:
        pairs_b = _entry_pairs(s, gid_b)
    want = {("car", "coche"), ("automobile", "coche")}
    assert pairs_a == want, f"add-in-place synonym not projected (R3-2): {pairs_a}"
    assert pairs_a == pairs_b, f"add-in-place != fresh-import: {pairs_a} vs {pairs_b}"
    print("  R3-2: a synonym added in place projects the same as a fresh import")


def test_foreign_doc_id_collision_does_not_hijack():
    """R3-3 (gate round 3): a foreign .glossary.json whose concept external_id collides with
    a local concept's (but shares no term) imports as a NEW concept and never mutates the
    local one."""
    gid = _gl("R3-3 GL")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            definition="an invoice",
            terms=[
                {"locale": SRC, "term": "invoice", "status": "preferred"},
                {"locale": TGT, "term": "factura", "status": "preferred"},
            ],
        )
        local_ext, local_cid = c.external_id, c.id
    foreign = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": local_ext,  # a COLLIDING id, but a totally different concept
                "definition": "a noble gas",
                "terms": [
                    {"locale": SRC, "term": "argon", "status": "preferred"},
                    {"locale": TGT, "term": "argon-es", "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        counters = gc.from_v1(s, gid, foreign)
    with Session(engine) as s:
        local = s.get(GlossaryConcept, local_cid)
        assert local.definition == "an invoice", "R3-3: foreign doc hijacked the local concept"
        assert counters.get("id_conflicts", 0) >= 1, f"id conflict not counted: {counters}"
        argon = s.exec(select(GlossaryTerm).where(GlossaryTerm.term == "argon")).first()
        assert argon is not None and argon.concept_id != local_cid, (
            "argon merged into local concept"
        )
    print("  R3-3: a colliding-id foreign doc imports as a new concept, never hijacks a local one")


def test_legacy_empty_target_survives_editor_edit():
    """R3-1 (gate round 3): a legacy empty-target term (a supported `orphan,` / no-target
    row) must survive an unrelated concept-editor metadata edit — the prune only collects an
    entry whose target was PREVIOUSLY non-blank."""
    gid = _gl("R3-1 GL")
    with Session(engine) as s:
        gm.add_term(s, gid, "orphan", target_term="", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "keep", target_term="mantener", source_locale=SRC, target_locale=TGT)
    with Session(engine) as s:
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "orphan"
            )
        ).first()
        cid, src_tid = e.concept_id, e.term_id
        # an unrelated metadata edit on the orphan's source term must NOT destroy the row
        gc.edit_concept_term(s, gid, cid, src_tid, part_of_speech="noun")
    with Session(engine) as s:
        srcs = {e.source_term for e in _entries_of(s, gid)}
        assert "orphan" in srcs, "R3-1: an editor edit destroyed a legacy empty-target term"
        assert "keep" in srcs, "the unrelated entry was harmed"
    print("  R3-1: a legacy empty-target term survives an unrelated editor edit")


def test_r4_multilang_json_roundtrip():
    """R4-1 (gate round 4): a MULTI-LANGUAGE termbase — one source surface translated into
    two languages — is ONE concept (R4 shared identity), so its `.glossary.json` export
    round-trips through the REAL surface seam (`gm.export_bytes`/`gm.import_bytes`):
    self-restore is a no-op AND restoring into a FRESH glossary reproduces both compat
    entries. Proven for a glossary built with `gm.add_term` AND one built from two CSV
    imports (the two shapes the legacy dual-write used to split one-per-entry)."""
    JSON = gloss_engine.JSON_FORMAT
    want = {("invoice", "factura"), ("invoice", "facture")}
    # (a) built with gm.add_term across two pairs -> ONE concept, not two.
    gid = _gl("R4-1 add_term", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.add_term(s, gid, "invoice", target_term="factura", source_locale=SRC, target_locale=TGT)
        gm.add_term(
            s, gid, "invoice", target_term="facture", source_locale=SRC, target_locale="fr-FR"
        )
    with Session(engine) as s:
        assert len(_concepts_of(s, gid)) == 1, "a two-language source surface must be ONE concept"
        assert _entry_pairs(s, gid) == want, f"two-pair store wrong: {_entry_pairs(s, gid)}"
        blob = gm.export_bytes(s, gid, fmt=JSON)
    with Session(engine) as s:  # self-restore must NOT raise and must not change the store
        gm.import_bytes(s, gid, blob, SRC, TGT, filename="RealWorld.glossary.json")
    with Session(engine) as s:
        assert _entry_pairs(s, gid) == want, "R4-1: self-restore changed the compat store"
        assert len(_concepts_of(s, gid)) == 1, "R4-1: self-restore split the concept"
    gid2 = _gl("R4-1 fresh", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.import_bytes(s, gid2, blob, SRC, TGT, filename="RealWorld.glossary.json")
    with Session(engine) as s:
        assert _entry_pairs(s, gid2) == want, "R4-1: fresh restore lost/added a compat entry"
    # (b) built from two CSV imports (en-es then en-fr, same source term).
    gid3 = _gl("R4-1 csv", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.import_bytes(
            s, gid3, b"source_term,target_term\ncar,coche\n", SRC, TGT, filename="a.csv"
        )
        gm.import_bytes(
            s, gid3, b"source_term,target_term\ncar,voiture\n", SRC, "fr-FR", filename="b.csv"
        )
    with Session(engine) as s:
        assert len(_concepts_of(s, gid3)) == 1, "R4-1: two CSV imports of one source term split"
        blob3 = gm.export_bytes(s, gid3, fmt=JSON)
    gid4 = _gl("R4-1 csv fresh", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.import_bytes(s, gid4, blob3, SRC, TGT, filename="c.glossary.json")
    with Session(engine) as s:
        assert _entry_pairs(s, gid4) == {("car", "coche"), ("car", "voiture")}, (
            f"R4-1: CSV-built export did not round-trip: {_entry_pairs(s, gid4)}"
        )
    print("  R4-1: a two-language termbase round-trips through gm.export_bytes/import_bytes")


def test_r4_disjoint_targets_import_vs_overlap_refused():
    """R4-1 (narrowing): the doc-collision refusal fires ONLY for two un-grouped concepts
    that would project CONFLICTING entries for the SAME (source_locale, target_locale) pair.
    Two concepts sharing a source surface with DISJOINT target locales are the normal
    multi-language shape and import fine; two with an OVERLAPPING target locale are refused."""
    # disjoint: bank->banco (es) and bank->banque (fr) — NOT refused.
    gid = _gl("R4-1 disjoint", pairs=((SRC, TGT), (SRC, "fr-FR")))
    doc_ok = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "banco", "status": "preferred"},
                ]
            },
            {
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": "fr-FR", "term": "banque", "status": "preferred"},
                ]
            },
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc_ok)  # must not raise
    with Session(engine) as s:
        assert _entry_pairs(s, gid) == {("bank", "banco"), ("bank", "banque")}, (
            f"R4-1: disjoint-target multi-language doc did not import both pairs: "
            f"{_entry_pairs(s, gid)}"
        )
    # overlap: bank->banco (es) and bank->orilla (es) — a real conflict, refused.
    gid2 = _gl("R4-1 overlap", pairs=((SRC, TGT),))
    doc_bad = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "banco", "status": "preferred"},
                ]
            },
            {
                "terms": [
                    {"locale": SRC, "term": "bank", "status": "preferred"},
                    {"locale": TGT, "term": "orilla", "status": "preferred"},
                ]
            },
        ],
    }
    with Session(engine) as s:
        try:
            gc.from_v1(s, gid2, doc_bad)
            raise AssertionError("R4-1: two same-pair concepts were not refused")
        except gc.ConceptError:
            pass
    print("  R4-1: disjoint-target concepts import; same-pair conflicting concepts refused")


def test_r4_consolidate_split_concepts():
    """R4 one-time repair: a store already SPLIT by the earlier (buggy) one-concept-per-entry
    logic — two un-grouped concepts sharing (source_locale, source_term) — is merged by
    `consolidate_split_concepts` into ONE concept carrying both target terms, with every
    compat entry re-pointed; a second call is a NO-OP (idempotent)."""
    # Manufacture the split store directly (the shape the old dual-write produced): two
    # un-grouped concepts, each with the SAME en source surface but a different-language
    # target, each with its own compat entry.
    gid = _gl("R4 consolidate", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        for tgt_loc, tgt_term in ((TGT, "coche"), ("fr-FR", "voiture")):
            c = GlossaryConcept(glossary_id=gid, external_id=f"x-{tgt_loc}")
            s.add(c)
            s.flush()
            src = GlossaryTerm(concept_id=c.id, locale=SRC, term="car", status="preferred")
            tgt = GlossaryTerm(concept_id=c.id, locale=tgt_loc, term=tgt_term, status="preferred")
            s.add(src)
            s.add(tgt)
            s.flush()
            e = GlossaryEntry(
                glossary_id=gid,
                project_id=None,
                source_locale=SRC,
                target_locale=tgt_loc,
                source_term="car",
                target_term=tgt_term,
                concept_id=c.id,
                term_id=src.id,
            )
            s.add(e)
        s.commit()
    with Session(engine) as s:
        assert len(_concepts_of(s, gid)) == 2, "setup: a split (two-concept) store expected"
        merged = gc.consolidate_split_concepts(s)
        s.commit()
    assert merged >= 1, "R4: consolidation reported no merge on a split store"
    with Session(engine) as s:
        assert len(_concepts_of(s, gid)) == 1, "R4: split concepts were not merged into one"
        assert _entry_pairs(s, gid) == {("car", "coche"), ("car", "voiture")}, (
            f"R4: consolidation lost a compat entry: {_entry_pairs(s, gid)}"
        )
        cid = _concepts_of(s, gid)[0].id
        locs = {t.locale for t in _terms_of(s, gid)}
        assert {SRC, TGT, "fr-FR"} <= locs, f"R4: merged concept missing a target term: {locs}"
        # every entry now points at the survivor and has a live term_id
        for e in _entries_of(s, gid):
            assert e.concept_id == cid and e.term_id is not None, "R4: an entry was not re-pointed"
    with Session(engine) as s:  # idempotent second pass
        assert gc.consolidate_split_concepts(s) == 0, "R4: consolidation is not idempotent"
    print("  R4: consolidate_split_concepts merges an already-split store; idempotent")


def test_r5_consolidate_merges_sense_bearing_split():
    """R5-4 (gate round 5): a split whose BASE carries a homonym_group (any source term that
    also has a SENSE) must still be merged — the round-4 consolidation filtered to un-grouped
    concepts and left it split forever. The sense-only sibling is never touched."""
    gid = _gl("R5-4 split", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        hg = f"g{gid}:invoice"
        # base concept A (es) — GROUPED because it carries a sense
        cA = GlossaryConcept(glossary_id=gid, external_id="ra", homonym_group=hg)
        s.add(cA)
        s.flush()
        srcA = GlossaryTerm(concept_id=cA.id, locale=SRC, term="invoice", status="preferred")
        tgtA = GlossaryTerm(concept_id=cA.id, locale=TGT, term="factura", status="preferred")
        s.add(srcA)
        s.add(tgtA)
        s.flush()
        eA = GlossaryEntry(
            glossary_id=gid,
            project_id=None,
            source_locale=SRC,
            target_locale=TGT,
            source_term="invoice",
            target_term="factura",
            concept_id=cA.id,
            term_id=srcA.id,
        )
        s.add(eA)
        s.flush()
        # sense-only SIBLING concept C — referenced by a sense, never an entry
        cC = GlossaryConcept(glossary_id=gid, external_id="rc", homonym_group=hg, domain="river")
        s.add(cC)
        s.flush()
        srcC = GlossaryTerm(concept_id=cC.id, locale=SRC, term="invoice", status="preferred")
        tgtC = GlossaryTerm(concept_id=cC.id, locale=TGT, term="recibo", status="preferred")
        s.add(srcC)
        s.add(tgtC)
        s.flush()
        s.add(
            GlossarySense(
                glossary_entry_id=eA.id,
                sense_label="river",
                context_cue="water",
                target_term="recibo",
                concept_id=cC.id,
                term_id=tgtC.id,
            )
        )
        # the split TWIN base concept B (fr) — un-grouped
        cB = GlossaryConcept(glossary_id=gid, external_id="rb")
        s.add(cB)
        s.flush()
        srcB = GlossaryTerm(concept_id=cB.id, locale=SRC, term="invoice", status="preferred")
        tgtB = GlossaryTerm(concept_id=cB.id, locale="fr-FR", term="facture", status="preferred")
        s.add(srcB)
        s.add(tgtB)
        s.flush()
        s.add(
            GlossaryEntry(
                glossary_id=gid,
                project_id=None,
                source_locale=SRC,
                target_locale="fr-FR",
                source_term="invoice",
                target_term="facture",
                concept_id=cB.id,
                term_id=srcB.id,
            )
        )
        s.commit()
    with Session(engine) as s:
        merged = gc.consolidate_split_concepts(s)
        s.commit()
    assert merged >= 1, "R5-4: a sense-bearing split was left unmerged (merged == 0)"
    with Session(engine) as s:
        assert _entry_pairs(s, gid) == {("invoice", "factura"), ("invoice", "facture")}, (
            f"R5-4: lost/added a compat entry: {_entry_pairs(s, gid)}"
        )
        base_ids = {e.concept_id for e in _entries_of(s, gid)}
        assert len(base_ids) == 1, f"R5-4: the two base entries did not converge: {base_ids}"
        assert len(_concepts_of(s, gid)) == 2, (
            f"R5-4: expected survivor + sibling = 2 concepts: {len(_concepts_of(s, gid))}"
        )
        eids = [e.id for e in _entries_of(s, gid)]
        senses = s.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))
        ).all()
        assert len(senses) == 1 and senses[0].target_term == "recibo", (
            "R5-4: the sense sibling was harmed by the merge: "
            f"{[(x.sense_label, x.target_term) for x in senses]}"
        )
    with Session(engine) as s:  # idempotent
        assert gc.consolidate_split_concepts(s) == 0, "R5-4: consolidation not idempotent"
    print("  R5-4: a sense-bearing (grouped-base) split is merged; the sibling is preserved")


def test_r4_second_language_in_place_equals_fresh():
    """R4-3 (gate round 4): adding a SECOND-LANGUAGE target IN PLACE (the concept gains an
    fr term through the editor) projects the compat entry for that pair — exactly as a fresh
    import of the identical exported document would. add-in-place == fresh-import for a
    second pair, not only the first."""
    gid_a = _gl("R4-3A", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        e = gm.add_term(s, gid_a, "car", target_term="coche", source_locale=SRC, target_locale=TGT)
        cid = e.concept_id
    with Session(engine) as s:
        gc.add_concept_term(s, gid_a, cid, locale="fr-FR", term="voiture", status="preferred")
    with Session(engine) as s:
        pairs_a = _entry_pairs(s, gid_a)
        m_fr = gloss_engine.match_terms(s, None, "the car", SRC, "fr-FR", glossary_id=gid_a)
        doc_a = gc.to_v1(s, gid_a)
    want = {("car", "coche"), ("car", "voiture")}
    assert pairs_a == want, f"R4-3: second-language target not projected in place: {pairs_a}"
    assert [(x["source_term"], x["target_term"]) for x in m_fr] == [("car", "voiture")], (
        f"R4-3: match_terms(fr) empty after add-in-place: {m_fr}"
    )
    gid_b = _gl("R4-3B", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gc.from_v1(s, gid_b, doc_a)
    with Session(engine) as s:
        assert _entry_pairs(s, gid_b) == want, "R4-3: add-in-place != fresh-import for a 2nd pair"
    print("  R4-3: a second-language target added in place equals a fresh import")


def test_r4_grouped_linked_caller_refused():
    """R4-2 (gate round 4): the collision resolver never auto-groups. An ALREADY-LINKED,
    ALREADY-GROUPED caller renaming (or adding) onto a rival's source surface is REFUSED and
    the rival's `homonym_group` is left untouched — no silent cross-concept write, no second
    compat entry for one source surface."""
    gid = _gl("R4-2 GL")
    with Session(engine) as s:
        a = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        a_cid = a.concept_id
        b = gc.create_concept(
            s,
            gid,
            domain="coastline",
            homonym_group="g-x",
            terms=[
                {"locale": SRC, "term": "shore", "status": "preferred"},
                {"locale": TGT, "term": "orilla", "status": "preferred"},
            ],
        )
        b_cid = b.id
        shore = s.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == b_cid, GlossaryTerm.term == "shore"
            )
        ).first()
        shore_id = shore.id
    # rename seam: B (grouped g-x) renaming shore -> bank collides with A (ungrouped) -> refused
    with Session(engine) as s:
        try:
            gc.edit_concept_term(s, gid, b_cid, shore_id, term="bank")
            raise AssertionError("R4-2: a grouped caller's rename onto a rival was not refused")
        except gc.ConceptError:
            pass
    # add-term seam: B adding a 'bank' source term collides with A -> refused
    with Session(engine) as s:
        try:
            gc.add_concept_term(s, gid, b_cid, locale=SRC, term="bank", status="preferred")
            raise AssertionError("R4-2: a grouped caller's add-term onto a rival was not refused")
        except gc.ConceptError:
            pass
    with Session(engine) as s:
        a2 = s.get(GlossaryConcept, a_cid)
        assert (a2.homonym_group or "") == "", (
            "R4-2: the rival's homonym_group was silently mutated"
        )
        assert _entry_pairs(s, gid) == {("bank", "banco"), ("shore", "orilla")}, (
            f"R4-2: the store gained a conflicting entry: {_entry_pairs(s, gid)}"
        )
    print("  R4-2: a grouped, linked caller colliding with a rival is refused; no silent regroup")


def test_r4_pairless_collision_refused():
    """R4-4 (gate round 4): the two-same-surface refusal is NOT bypassable by importing into
    a glossary that declares no pairs — the source locales are inferred from the document's
    own term locales."""
    with Session(engine) as s:
        g = gm.create(s, "R4-4 pairless")
        gid = g.id  # NO set_pairs
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "terms": [
                    {"locale": SRC, "term": "dup", "status": "preferred"},
                    {"locale": TGT, "term": "uno", "status": "preferred"},
                ]
            },
            {
                "terms": [
                    {"locale": SRC, "term": "dup", "status": "preferred"},
                    {"locale": TGT, "term": "dos", "status": "preferred"},
                ]
            },
        ],
    }
    with Session(engine) as s:
        try:
            gc.from_v1(s, gid, doc)
            raise AssertionError("R4-4: a pair-less glossary bypassed the same-surface refusal")
        except gc.ConceptError:
            pass
    print("  R4-4: the same-surface refusal is enforced even on a pair-less glossary")


def test_r4_delete_base_keeps_sibling():
    """R4-5 (gate round 4): deleting the BASE of a homonym group deletes exactly ONE concept
    and PROMOTES the surviving sibling to the new base (its own compat entry) — it does not
    cascade into the sibling and wipe the whole group."""
    gid = _gl("R4-5 GL")
    with Session(engine) as s:
        e = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "river", "orilla", context_cue="river water")
        base_cid = e.concept_id
    with Session(engine) as s:
        assert len(_concepts_of(s, gid)) == 2, "setup: base + sibling expected"
        gc.delete_concept(s, gid, base_cid)
    with Session(engine) as s:
        cs = _concepts_of(s, gid)
        assert len(cs) == 1, f"R4-5: deleting the base did not leave exactly one concept: {len(cs)}"
        assert _entry_pairs(s, gid) == {("bank", "orilla")}, (
            f"R4-5: the surviving sibling was not promoted to a base entry: {_entry_pairs(s, gid)}"
        )
    print("  R4-5: deleting a homonym base keeps + promotes the sibling (one concept deleted)")


def test_r4_edit_concept_homonym_group_and_remedy():
    """R4-6 (gate round 4): `edit_concept` APPLIES `homonym_group` (it is advertised on the
    API edit route and is the remedy the collision refusals prescribe), and that remedy is
    actually reachable — grouping two rival concepts lets both coexist on one source surface."""
    gid = _gl("R4-6 GL")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            domain="x",
            terms=[
                {"locale": SRC, "term": "foo", "status": "preferred"},
                {"locale": TGT, "term": "bar", "status": "preferred"},
            ],
        )
        cid = c.id
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, domain="finance", homonym_group="g-new")
    with Session(engine) as s:
        c2 = s.get(GlossaryConcept, cid)
        assert c2.domain == "finance", "R4-6: edit_concept dropped domain"
        assert c2.homonym_group == "g-new", f"R4-6: homonym_group not applied: {c2.homonym_group!r}"
    # the prescribed remedy is reachable: group a rival, then a second same-surface concept
    # in the SAME group is accepted (both coexist as homonyms).
    gid2 = _gl("R4-6 remedy")
    with Session(engine) as s:
        a = gc.create_concept(
            s,
            gid2,
            domain="finance",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "banco", "status": "preferred"},
            ],
        )
        gc.edit_concept(s, gid2, a.id, homonym_group="grp")
    with Session(engine) as s:
        b = gc.create_concept(
            s,
            gid2,
            domain="river",
            homonym_group="grp",
            terms=[
                {"locale": SRC, "term": "bank", "status": "preferred"},
                {"locale": TGT, "term": "orilla", "status": "preferred"},
            ],
        )
        assert b.id is not None, "R4-6: grouping the rivals did not let the second concept in"
    print("  R4-6: edit_concept applies homonym_group; grouping is a reachable remedy")


def _entry_at(s, gid, source_term, target_locale):
    return s.exec(
        select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == gid,
            GlossaryEntry.source_term == source_term,
            GlossaryEntry.target_locale == target_locale,
        )
    ).first()


def test_r5_per_pair_meta_and_dnt_survive_reconcile():
    """R5-0 + R5-1 (gate round 5): one source surface now shares ONE concept across its
    locale pairs, but `dnt`, `case_sensitive` and legacy `meta` are PER-PAIR and
    legacy-authoritative (LAW-TERM-2). A reconcile must NOT stamp the last-written pair's
    values onto every pair (the shared concept can hold only one)."""
    # R5-1: two pairs of one surface carrying DIFFERENT legacy meta both survive.
    gid = _gl("R5-1 meta", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.add_term(
            s,
            gid,
            "invoice",
            target_term="factura",
            source_locale=SRC,
            target_locale=TGT,
            meta={
                "definition": "a bill (es)",
                "subject_field": "finance",
                "part_of_speech": "noun",
            },
        )
        gm.add_term(
            s,
            gid,
            "invoice",
            target_term="facture",
            source_locale=SRC,
            target_locale="fr-FR",
            meta={
                "definition": "une facture (fr)",
                "subject_field": "commerce",
                "part_of_speech": "nom",
            },
        )
    with Session(engine) as s:
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        es = _entry_at(s, gid, "invoice", TGT).meta or {}
        fr = _entry_at(s, gid, "invoice", "fr-FR").meta or {}
        assert es.get("definition") == "a bill (es)", (
            f"R5-1: es meta clobbered by a reconcile: {es}"
        )
        assert fr.get("definition") == "une facture (fr)", f"R5-1: fr meta clobbered: {fr}"
        assert es.get("subject_field") == "finance" and fr.get("subject_field") == "commerce", (
            f"R5-1: subject_field crossed pairs: {es} / {fr}"
        )
    # R5-0: a surface DNT in one pair and translatable in another keeps its per-pair dnt.
    gid2 = _gl("R5-0 dnt", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gm.add_term(s, gid2, "Acme", target_term="", source_locale=SRC, target_locale=TGT, dnt=True)
        gm.add_term(
            s, gid2, "Acme", target_term="Acme SA", source_locale=SRC, target_locale="fr-FR"
        )
    with Session(engine) as s:
        gc.project(s, gid2, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        es = _entry_at(s, gid2, "Acme", TGT)
        fr = _entry_at(s, gid2, "Acme", "fr-FR")
        assert es.dnt is True and (es.target_term or "") == "", (
            f"R5-0: es DNT lost by a reconcile: dnt={es.dnt} target={es.target_term!r}"
        )
        assert fr.dnt is False and fr.target_term == "Acme SA", (
            f"R5-0: fr pair gained the es pair's DNT: dnt={fr.dnt} target={fr.target_term!r}"
        )
        m_es = gloss_engine.match_terms(s, None, "Acme ships today", SRC, TGT, glossary_id=gid2)
        assert any(x["source_term"] == "Acme" and x["dnt"] for x in m_es), (
            f"R5-0: the DNT gate stopped seeing the es pair: {m_es}"
        )
        # the exact round-5 probe: quality.dnt_violations still flags the es pair (a DNT term
        # translated in the target) — before the fix the reconcile flipped its dnt to False and
        # dnt_violations returned []; the prompt line marked it (do not translate), not `Acme->`.
        dv = quality.dnt_violations("Acme ships today", "Cumbre envia hoy", m_es)
        dv_terms = [(d["source_term"] if isinstance(d, dict) else d) for d in dv]
        assert dv_terms == ["Acme"], (
            f"R5-0: dnt_violations no longer flags the es DNT pair after a reconcile: {dv}"
        )
    print("  R5-0/R5-1: per-pair dnt + legacy meta survive a reconcile on a shared concept")


def test_r5_legacy_meta_clear_survives_reconcile():
    """R5-6 (gate round 5): clearing a definition/domain/notes through the LEGACY row editor
    (`edit_term(meta={})`) must not be undone by the next reconcile."""
    gid = _gl("R5-6 GL")
    with Session(engine) as s:
        e = gm.add_term(
            s,
            gid,
            "bank",
            target_term="banco",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "a money house", "subject_field": "finance", "notes": "n1"},
        )
        eid = e.id
    with Session(engine) as s:
        gm.edit_term(s, gid, eid, meta={})  # explicit clear
    with Session(engine) as s:
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        meta = _entry_at(s, gid, "bank", TGT).meta or {}
        assert (
            not meta.get("definition") and not meta.get("subject_field") and not meta.get("notes")
        ), f"R5-6: a reconcile resurrected a cleared legacy meta: {meta}"
    print("  R5-6: clearing legacy meta via the row editor survives a reconcile")


def test_r5_dnt_only_concept_is_projected_and_flagged():
    """R5-3 (gate round 5): a DNT-only concept (a brand/product name with no target) created
    through `create_concept` AND through a `.glossary.json` import is projected to a compat
    entry on every pair, matched by `match_terms`, and flagged by `quality.dnt_violations`."""
    gid = _gl("R5-3 editor", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gc.create_concept(
            s,
            gid,
            domain="brand",
            terms=[{"locale": SRC, "term": "Acme", "status": "preferred", "dnt": True}],
        )
    with Session(engine) as s:
        got = {(e.source_term, e.target_locale, bool(e.dnt)) for e in _entries_of(s, gid)}
        assert ("Acme", TGT, True) in got and ("Acme", "fr-FR", True) in got, (
            f"R5-3: DNT-only concept not projected on both pairs: {got}"
        )
        m = gloss_engine.match_terms(s, None, "Acme ships today", SRC, TGT, glossary_id=gid)
        assert any(x["source_term"] == "Acme" and x["dnt"] for x in m), (
            f"R5-3: DNT not matched: {m}"
        )
        assert quality.dnt_violations("Acme ships today", "Cumbre envia hoy", m), (
            "R5-3: dnt_violations did not fire for the editor-created DNT concept"
        )
        doc = gc.to_v1(s, gid)
    gid2 = _gl("R5-3 json", pairs=((SRC, TGT), (SRC, "fr-FR")))
    with Session(engine) as s:
        gc.from_v1(s, gid2, doc)
    with Session(engine) as s:
        m = gloss_engine.match_terms(s, None, "Acme ships today", SRC, TGT, glossary_id=gid2)
        assert any(x["source_term"] == "Acme" and x["dnt"] for x in m), (
            f"R5-3: JSON DNT not matched: {m}"
        )
        assert quality.dnt_violations("Acme ships today", "Cumbre envia hoy", m), (
            "R5-3: dnt_violations did not fire for the imported DNT-only concept"
        )
    print("  R5-3: a DNT-only concept (editor + .glossary.json) projects + dnt_violations fires")


def test_r6_wildcard_dnt_target_survives_reconcile():
    """R6-A (gate round 6): a FULL-wildcard DNT row whose target equals its source
    (`Widget->Widget`, source_locale == target_locale == "" — the DEFAULT web/CLI add-term
    submission for a brand that is the same in every language) keeps its stored target through a
    reconcile; pass 1 is link-only so it can never blank a DNT target the concept still
    expresses."""
    gid = _gl("R6-A GL")
    with Session(engine) as s:
        gm.add_term(
            s, gid, "Widget", target_term="Widget", source_locale="", target_locale="", dnt=True
        )
    with Session(engine) as s:
        gc.project(s, gid, derive_senses=True)
        s.commit()
    with Session(engine) as s:
        e = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "Widget"
            )
        ).first()
        assert e.target_term == "Widget" and bool(e.dnt) is True, (
            "R6-A: a reconcile blanked the wildcard DNT target: "
            f"{e.source_term!r}->{e.target_term!r}"
        )
        m = gloss_engine.match_terms(s, None, "the Widget ships", SRC, TGT, glossary_id=gid)
        assert any(x["source_term"] == "Widget" and x["target_term"] == "Widget" for x in m), (
            f"R6-A: match_terms lost the wildcard DNT target after a reconcile: {m}"
        )
    print("  R6-A: a full-wildcard DNT X->X row keeps its target through a reconcile")


def test_r6_concept_term_dnt_reaches_entry_all_surfaces():
    """R6-C (gate round 6): `dnt` (and `case_sensitive`) set on the concept-term edit surfaces
    reaches the compat entry — in BOTH directions — for an entry that already exists. Covered on
    the service seam `edit_concept_term` (which the web route, /api and the CLI all delegate to)
    AND on the `.glossary.json` import surface. Accepting-and-discarding is the failure mode."""
    # DNT-OFF: a concept-first DNT entry, turned off via edit_concept_term, reaches the entry.
    gid = _gl("R6-C off")
    with Session(engine) as s:
        c = gc.create_concept(
            s, gid, terms=[{"locale": SRC, "term": "Acme", "status": "preferred", "dnt": True}]
        )
        cid = c.id
        aid = (
            s.exec(
                select(GlossaryTerm).where(
                    GlossaryTerm.concept_id == cid, GlossaryTerm.term == "Acme"
                )
            )
            .first()
            .id
        )
    with Session(engine) as s:
        # LAW-TERM-18 R7-M: turning DNT OFF on a binding with no target term is REFUSED.
        try:
            gc.edit_concept_term(s, gid, cid, aid, dnt=False)
            raise AssertionError("expected R7-M refusal turning DNT off with no target")
        except gc.ConceptError as exc:
            assert "target before clearing" in str(exc), exc
    with Session(engine) as s:
        # give the term a target, THEN DNT-off reaches the entry (R6-C intent preserved).
        gc.add_concept_term(s, gid, cid, locale=TGT, term="acme-es", status="preferred")
    with Session(engine) as s:
        gc.edit_concept_term(s, gid, cid, aid, dnt=False)
    with Session(engine) as s:
        e = _entry_at(s, gid, "Acme", TGT)
        assert bool(e.dnt) is False, "R6-C: edit_concept_term(dnt=False) did not reach the entry"
        m = gloss_engine.match_terms(s, None, "Acme ships", SRC, TGT, glossary_id=gid)
        assert not any(x["source_term"] == "Acme" and x["dnt"] for x in m), (
            f"R6-C: match_terms still reports the entry as DNT after DNT-off: {m}"
        )
    # DNT-ON: a legacy non-DNT entry, turned on via edit_concept_term, reaches the entry.
    gid2 = _gl("R6-C on")
    with Session(engine) as s:
        e = gm.add_term(
            s, gid2, "Zeta", target_term="zeta-es", source_locale=SRC, target_locale=TGT
        )
        cid2, tid2 = e.concept_id, e.term_id
    with Session(engine) as s:
        gc.edit_concept_term(s, gid2, cid2, tid2, dnt=True)
    with Session(engine) as s:
        e = _entry_at(s, gid2, "Zeta", TGT)
        assert bool(e.dnt) is True, "R6-C: edit_concept_term(dnt=True) did not reach the entry"
        m = gloss_engine.match_terms(s, None, "Use Zeta here", SRC, TGT, glossary_id=gid2)
        dv = quality.dnt_violations("Use Zeta here", "Usa esto aqui", m)
        assert dv, "R6-C: dnt_violations still empty after edit_concept_term(dnt=True)"
    # JSON-import surface: a doc that flips a source term's dnt reaches the entry.
    gid3 = _gl("R6-C json")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid3,
            terms=[
                {"locale": SRC, "term": "Yankee", "status": "preferred"},
                {"locale": TGT, "term": "yan-es", "status": "preferred"},
            ],
        )
        doc = gc.to_v1(s, gid3)
    # LAW-TERM-18: dnt is a PER-BINDING fact -- flip it on the concept's `bindings` block (the
    # term's dnt is only the default for NEW bindings), which from_v1 applies verbatim (A.6).
    for cdoc in doc["concepts"]:
        for tdoc in cdoc["terms"]:
            if tdoc["term"] == "Yankee":
                tdoc["dnt"] = True
        for b in cdoc.get("bindings", []):
            if b["source_term"] == "Yankee":
                b["dnt"] = True
    with Session(engine) as s:
        gc.from_v1(s, gid3, doc)
    with Session(engine) as s:
        e = _entry_at(s, gid3, "Yankee", TGT)
        assert bool(e.dnt) is True, (
            "R6-C: a .glossary.json import did not propagate dnt to the entry"
        )
    print("  R6-C: dnt reaches the compat entry both directions (edit_concept_term + JSON)")


def test_r6_concept_meta_edit_reaches_entry():
    """R6-D (gate round 6): a concept-editor `definition`/`domain`/`notes` edit reaches the
    compat entry's `meta` (the CSV/TBX/grid do not go stale), MERGING — the per-source
    `part_of_speech`/`usage_example` keys are not disturbed. Pass 1 stays link-only so an
    automatic reconcile never clobbers per-pair meta."""
    gid = _gl("R6-D GL")
    with Session(engine) as s:
        c = gc.create_concept(
            s,
            gid,
            definition="v1",
            domain="finance",
            terms=[
                {"locale": SRC, "term": "ledger", "status": "preferred", "part_of_speech": "noun"},
                {"locale": TGT, "term": "libro", "status": "preferred"},
            ],
        )
        cid = c.id
    with Session(engine) as s:
        gc.edit_concept(s, gid, cid, definition="v2", notes="added later")
    with Session(engine) as s:
        meta = _entry_at(s, gid, "ledger", TGT).meta or {}
        assert meta.get("definition") == "v2", (
            f"R6-D: concept definition edit did not reach entry: {meta}"
        )
        assert meta.get("notes") == "added later", (
            f"R6-D: concept notes edit did not reach entry: {meta}"
        )
        assert meta.get("subject_field") == "finance", f"R6-D: subject_field lost: {meta}"
        assert meta.get("part_of_speech") == "noun", f"R6-D: per-source pos was clobbered: {meta}"
    print("  R6-D: a concept-editor definition/notes edit reaches the compat entry meta (merged)")


def test_r2m1_base_fallback_is_bounded_and_exact_path_unchanged():
    """round-2 M1: the base-language fallback must apply ONLY across the base/region
    boundary of ONE language section (`en` <-> `en-US`), NEVER across two REGION variants of
    the same base language, and the EXACT-locale match path must stay byte-identical to main.

    Two halves:
      * EXACT (byte-identical to main): a plain en-US/es-ES concept into an en-US:es-ES
        glossary projects exactly one entry and matches -- the fallback tier is never reached.
      * SAME-SECTION guard (the S3 phantom): a concept holding two en-US surfaces `color` and
        `colour` in an en-US:en-GB glossary must project NOTHING and match NOTHING, exactly
        as `main` does. Counterfactual: on the pre-fix tree the unguarded base fallback bound
        the en-US `colour` as the en-GB target of `color` (and vice versa), so both entries
        were projected and `color` matched -- a source-language synonym rendered as its own
        translation, which the entry path's parse_rows same-section rule forbids."""
    # --- EXACT path (must equal main) ---
    gid = _gl("R2M1 exact GL")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "e1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    {"locale": "es-ES", "term": "artilugio", "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
    with Session(engine) as s:
        ents = {
            (e.source_term, e.source_locale, e.target_locale, e.target_term)
            for e in _entries_of(s, gid)
        }
        assert ents == {("widget", "en-US", "es-ES", "artilugio")}, ents
        m = gloss_engine.match_terms(s, None, "open the widget", "en-US", "es-ES", glossary_id=gid)
        assert [x["target_term"] for x in m] == ["artilugio"], m

    # --- SAME-SECTION guard: two regions of one base language never cross-bind ---
    gid2 = _gl("R2M1 same-section GL", pairs=(("en-US", "en-GB"),))
    doc2 = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "s3",
                "terms": [
                    {"locale": "en-US", "term": "color", "status": "preferred"},
                    {"locale": "en-US", "term": "colour", "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid2, doc2)
    with Session(engine) as s:
        _ents2 = [
            (e.source_term, e.source_locale, e.target_locale, e.target_term)
            for e in _entries_of(s, gid2)
        ]
        assert _ents2 == [], (
            "an en-US source term must never be bound as an en-GB target of its sibling "
            "(the S3 color->colour phantom); the base fallback is same-section only",
            _ents2,
        )
        assert (
            gloss_engine.match_terms(s, None, "the color here", "en-US", "en-GB", glossary_id=gid2)
            == []
        ), "a same-section-only glossary must match nothing (byte-identical to main)"
    print("  R2M1: base fallback is same-section only; the exact-locale match path is unchanged")


def test_r2m1_base_fallback_prefers_generic_base_tag():
    """round-2 M1: among base-language fallback candidates the GENERIC base tag (`es` for an
    `es-ES` pair) is preferred over a differently-regioned sibling (`es-419`), so the choice
    is deterministic, not document order. Counterfactual: on the pre-fix tree the fallback
    returned the first base match in document order, so listing `es-419` `ventanita` before
    the generic `es` `ventana` bound `widget -> ventanita`."""
    gid = _gl("R2M1 generic base GL")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "g1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    # es-419 listed FIRST on purpose: document order must NOT decide.
                    {"locale": "es-419", "term": "ventanita", "status": "preferred"},
                    {"locale": "es", "term": "ventana", "status": "preferred"},
                ],
            }
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
    with Session(engine) as s:
        e = _entry_at(s, gid, "widget", "es-ES")
        assert e is not None and e.target_term == "ventana", (
            "the generic base tag `es` must win over the regioned `es-419`",
            None if e is None else e.target_term,
        )
        m = gloss_engine.match_terms(s, None, "the widget", "en-US", "es-ES", glossary_id=gid)
        assert [x["target_term"] for x in m] == ["ventana"], m
    print("  R2M1: the generic base tag beats a differently-regioned sibling (deterministic)")


def test_r3m2_fallback_tiebreak_is_deterministic_without_generic_base():
    """round-3 M2: when NO base-fallback candidate carries the bare generic base tag, the
    target pick must STILL be deterministic (the lexicographically-first locale tag), not
    document order. Two REGION siblings of the target base language (`es-419` + `es-ES`) with
    no generic `es` are projected onto an `es-MX` pair: whichever order the langSecs arrive in,
    the SAME regional target must be chosen.

    Counterfactual: round 2's two-value stable sort key (`0 if locale == base else 1`) kept
    DOCUMENT ORDER whenever no candidate carried the bare base tag, so listing `es-419` first
    bound `ventanita` and listing `es-ES` first bound `ventana` -- a langSec reorder in the
    vendor's next export silently changed the enforced translation. On the pre-fix tree the two
    orders below disagree and the equality assertion fails."""
    doc_a = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "d1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    {"locale": "es-419", "term": "ventanita", "status": "preferred"},
                    {"locale": "es-ES", "term": "ventana", "status": "preferred"},
                ],
            }
        ],
    }
    doc_b = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "d1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    # reversed order of the two region siblings -- must NOT decide the pick.
                    {"locale": "es-ES", "term": "ventana", "status": "preferred"},
                    {"locale": "es-419", "term": "ventanita", "status": "preferred"},
                ],
            }
        ],
    }
    gid_a = _gl("R3M2 order A", pairs=(("en-US", "es-MX"),))
    gid_b = _gl("R3M2 order B", pairs=(("en-US", "es-MX"),))
    with Session(engine) as s:
        gc.from_v1(s, gid_a, doc_a)
        gc.from_v1(s, gid_b, doc_b)
    with Session(engine) as s:
        ea = _entry_at(s, gid_a, "widget", "es-MX")
        eb = _entry_at(s, gid_b, "widget", "es-MX")
        assert ea is not None and eb is not None, (ea, eb)
        assert ea.target_term == eb.target_term, (
            "the base-fallback tie-break must be independent of document order even when no "
            "generic base tag is present",
            ea.target_term,
            eb.target_term,
        )
        # and it is the deterministic lexicographically-first locale tag (es-419 < es-ES).
        assert ea.target_term == "ventanita", ea.target_term
    print("  R3M2: the fallback tie-break is total-ordered (deterministic without a base tag)")


def main():
    test_enum_validation()
    test_crud_and_dual_write()
    test_homonym_sense_identity()
    test_ingestion_cap_counted()
    test_delete_unit_single_pair()
    test_delete_unit_shared_source_h1()
    test_delete_unit_multi_sense()
    test_delete_unit_whole_glossary()
    test_delete_project_legacy_no_op()
    test_delete_cross_glossary_replacement()
    test_delete_concept_term_unit()
    test_delete_concept_sibling_unit()
    test_rematerialize_never_empty_target()
    test_dualwrite_no_clobber_status_audience()
    test_csv_audience_filters_llm_channel()
    test_json_roundtrip_sense_and_synonym()
    test_json_self_restore_is_noop()
    test_delete_target_term_removes_entry()
    test_create_concept_rejects_shared_source_surface()
    test_synonym_added_in_place_equals_fresh_import()
    test_foreign_doc_id_collision_does_not_hijack()
    test_legacy_empty_target_survives_editor_edit()
    test_r4_multilang_json_roundtrip()
    test_r4_disjoint_targets_import_vs_overlap_refused()
    test_r4_consolidate_split_concepts()
    test_r4_second_language_in_place_equals_fresh()
    test_r4_grouped_linked_caller_refused()
    test_r4_pairless_collision_refused()
    test_r4_delete_base_keeps_sibling()
    test_r4_edit_concept_homonym_group_and_remedy()
    test_r5_per_pair_meta_and_dnt_survive_reconcile()
    test_r5_legacy_meta_clear_survives_reconcile()
    test_r5_dnt_only_concept_is_projected_and_flagged()
    test_r5_consolidate_merges_sense_bearing_split()
    test_r6_wildcard_dnt_target_survives_reconcile()
    test_r6_concept_term_dnt_reaches_entry_all_surfaces()
    test_r6_concept_meta_edit_reaches_entry()
    test_r2m1_base_fallback_is_bounded_and_exact_path_unchanged()
    test_r2m1_base_fallback_prefers_generic_base_tag()
    test_r3m2_fallback_tiebreak_is_deterministic_without_generic_base()
    print("GLOSSARY CONCEPTS OK")


if __name__ == "__main__":
    main()
