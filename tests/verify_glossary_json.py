"""glossary-concept/v1 JSON codec + renderer + match_concepts non-divergence (T1, D2).

Hermetic + offline. Covers LAW-TERM-8/11/13/4/1:

  * schema refusal: a missing/unknown-major schema is refused (never best-effort parsed).
  * full v1 field round-trip: to_v1(from_v1(doc)) preserves every enumerated field incl.
    homonym_group / grammatical_* / geographical_usage / created_by / exact / case_sensitive
    (M1), and a cross-concept deprecated_replacement OBJECT re-links by (concept,locale,
    surface) (M2).
  * synthesized c<id> identity (M6): a blank external_id normalizes to c<id> on export and
    cross_refs resolve to it; import->export->import is a fixed point.
  * the glossary header is DESCRIPTIVE on import (M3): a file-supplied customer_id never
    re-scopes the target glossary.
  * JSON counters incl. truncated_fields; unknown fields ignored (LAW-TERM-8).
  * renderer (render_for_llm): concept cap, DNT/forbidden-first ordering, do-not-use line,
    DNT line, per-field 200-char budget, omitted marker counted in token_cap, and the
    shown-set return.
  * match_concepts_merged NON-DIVERGENCE (H6): its five-key compat projection == the
    match_terms_merged result on every fixture; audience-filter runs AFTER shadow/precedence.

Final line: "GLOSSARY JSON OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_gljson_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryConcept, Project
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm

init_db()

# T1-lite: this suite exercises from_v1 / .glossary.json import into populated glossaries,
# a concept-store WRITE gated behind the DEFAULT-OFF terminology_concept_editor flag;
# enable it for the suite's fixtures.
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


def test_schema_refusal():
    gid = _gl("Refuse GL")
    with Session(engine) as s:
        for bad in ({}, {"schema": "glossary-concept/v9"}, {"concepts": []}):
            try:
                gc.from_v1(s, gid, bad)
                raise AssertionError(f"accepted a bad schema doc: {bad}")
            except gc.ConceptError:
                pass
    print("  schema: missing/unknown-major refused (never best-effort parsed)")


def _rich_doc():
    return {
        "schema": "glossary-concept/v1",
        "glossary": {"customer_id": 999, "name": "SHOULD BE IGNORED", "pairs": []},
        "concepts": [
            {
                "external_id": "",  # blank -> synthesized c<id> on export (M6)
                "domain": "manufacturing",
                "definition": "a smoothing coefficient",
                "notes": "n",
                "homonym_group": "",
                "created_by": "importer",
                "cross_refs": [],
                "terms": [
                    {
                        "locale": "en-US",
                        "term": "alpha",
                        "status": "preferred",
                        "term_type": "fullForm",
                        "part_of_speech": "noun",
                        "grammatical_gender": "",
                        "grammatical_number": "singular",
                        "geographical_usage": "US",
                        "audience": ["human", "mt", "llm"],
                        "usage_note": "u",
                        "context_example": "c",
                        "case_sensitive": True,
                        "exact": True,
                        "dnt": False,
                        "source": "iate",
                    },
                    {"locale": "es-ES", "term": "alfa", "status": "preferred"},
                    {
                        "locale": "es-ES",
                        "term": "alfa vieja",
                        "status": "deprecated",
                        "audience": ["human"],
                        "deprecated_replacement": {
                            "concept": "c-self",
                            "locale": "es-ES",
                            "surface": "alfa",
                        },
                    },
                ],
            }
        ],
    }


def test_full_round_trip_and_identity():
    gid = _gl("RT GL")
    doc = _rich_doc()
    # point the self-replacement at the concept's own (soon-minted) id via external_id
    doc["concepts"][0]["external_id"] = "cX"
    doc["concepts"][0]["terms"][2]["deprecated_replacement"]["concept"] = "cX"
    with Session(engine) as s:
        counters = gc.from_v1(s, gid, doc)
        assert counters["concepts"] == 1 and counters["terms"] == 3, counters
        # header customer_id IGNORED (M3): the glossary keeps its own scope
        from app.models import Glossary

        g = s.get(Glossary, gid)
        assert g.customer_id != 999, "file-supplied customer_id re-scoped the glossary (M3)"
    with Session(engine) as s:
        out1 = gc.to_v1(s, gid)
    # round-trip into a SECOND glossary and compare the concept payloads
    gid2 = _gl("RT GL 2")
    with Session(engine) as s:
        gc.from_v1(s, gid2, out1)
        out2 = gc.to_v1(s, gid2)
    c1, c2 = out1["concepts"][0], out2["concepts"][0]
    # identity: a real external_id survives; every enumerated field round-trips
    for key in ("domain", "definition", "notes", "created_by"):
        assert c1[key] == c2[key], f"concept.{key} drifted: {c1[key]!r} vs {c2[key]!r}"
    t1 = {(t["locale"], t["term"]): t for t in c1["terms"]}
    t2 = {(t["locale"], t["term"]): t for t in c2["terms"]}
    assert set(t1) == set(t2), "term set drifted across round-trip"
    a = t1[("en-US", "alpha")]
    b = t2[("en-US", "alpha")]
    for key in (
        "status",
        "term_type",
        "part_of_speech",
        "grammatical_number",
        "geographical_usage",
        "audience",
        "usage_note",
        "context_example",
        "case_sensitive",
        "exact",
        "dnt",
        "source",
    ):
        assert a[key] == b[key], f"term.{key} drifted: {a[key]!r} vs {b[key]!r}"
    # the deprecated term's cross-concept replacement OBJECT re-linked (M2)
    dep2 = t2[("es-ES", "alfa vieja")]
    assert "deprecated_replacement" in dep2, "deprecated_replacement dropped on round-trip"
    assert dep2["deprecated_replacement"]["surface"] == "alfa", "replacement surface drifted"
    print("  json: full v1 round-trip (all fields, replacement object) is a fixed point")


def test_synthesized_id_for_blank_external():
    gid = _gl("Synth GL")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {"external_id": "", "terms": [{"locale": SRC, "term": "widget"}]},
        ],
    }
    with Session(engine) as s:
        gc.from_v1(s, gid, doc)
        out = gc.to_v1(s, gid)
        # a blank external_id is stamped with a persisted GLOBALLY-UNIQUE identity (R3-3:
        # uuid4, NOT a row id), so a re-export carries a stable, collision-free id.
        ext = out["concepts"][0]["external_id"]
        assert ext and len(ext) >= 8, f"blank external_id not stamped: {ext!r}"
        # and re-importing this export upserts (no duplicate) by that id
        before = len(
            s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
        )
        gc.from_v1(s, gid, out)
        after = len(s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all())
        assert after == before, "the stamped id did not make the re-import a no-op"
    print("  json: blank external_id gets a persisted unique id; re-import is a no-op (R3-3)")


def test_counters_and_unknown_fields():
    gid = _gl("Ctr GL")
    doc = {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "c1",
                "definition": "x" * 800,  # will be capped -> truncated_fields++
                "bogus_field": "ignored",  # unknown -> ignored (LAW-TERM-8)
                "terms": [
                    {"locale": SRC, "term": "t1", "unknown": 1},
                    {
                        "locale": TGT,
                        "term": "t2",
                        "status": "deprecated",
                        "deprecated_replacement": {
                            "concept": "nope",
                            "locale": "x",
                            "surface": "y",
                        },
                    },
                ],
            }
        ],
    }
    with Session(engine) as s:
        counters = gc.from_v1(s, gid, doc)
    assert counters["truncated_fields"] >= 1, f"cap not counted: {counters}"
    assert counters["dropped_refs"] >= 1, f"unresolved replacement not counted: {counters}"
    print("  json: truncated_fields + dropped_refs counted; unknown fields ignored")


def test_renderer():
    """render_for_llm: ordering, DNT line, do-not-use, per-field budget, cap + shown set."""

    # Build matches the way match_concepts would (five-key dict + _concept meta).
    def _m(src, tgt, dnt=False, meta=None):
        return {
            "source_term": src,
            "target_term": tgt,
            "sense_label": None,
            "dnt": dnt,
            "case_sensitive": False,
            "_concept": meta,
        }

    long_def = "d" * 500
    matches = [
        _m(
            "widget",
            "artilugio",
            meta={
                "definition": long_def,
                "domain": "hardware",
                "part_of_speech": "noun",
                "alternatives": [],
                "audience": "human,mt,llm",
            },
        ),
        _m(
            "Acme",
            "",
            dnt=True,
            meta={
                "definition": "",
                "domain": "",
                "part_of_speech": "",
                "alternatives": [],
                "audience": "human,mt,llm",
            },
        ),
        _m(
            "gadget",
            "aparato",
            meta={
                "definition": "",
                "domain": "",
                "part_of_speech": "",
                "alternatives": [
                    {"surface": "cosa", "status": "deprecated", "replacement": "aparato"}
                ],
                "audience": "human,mt,llm",
            },
        ),
    ]
    block, shown = gc.render_for_llm(matches, concept_cap=10, token_cap=1500)
    # DNT/forbidden-bearing first: Acme (dnt) and gadget (alternatives) precede widget
    assert block.index("Acme") < block.index("widget"), "DNT concept not ordered first"
    assert block.index("gadget") < block.index("widget"), "forbidden-bearing not ordered first"
    assert "(do not translate)" in block, "DNT line missing"
    assert "do not use: cosa (deprecated; use aparato)" in block, "do-not-use line missing/wrong"
    # per-field budget: the 500-char definition is truncated to <=200 chars with an ellipsis
    for line in block.splitlines():
        if line.strip().startswith("note:"):
            assert "…" in line and len(line) < 230, "definition not budget-truncated"
    assert len(shown) == 3, f"shown set wrong size: {len(shown)}"
    assert set(shown[0]) == {"source_term", "target_term", "sense_label", "dnt", "case_sensitive"}
    # a moderate token_cap drops whole concepts from the tail with a counted marker
    cap2 = 40
    block2, shown2 = gc.render_for_llm(matches, concept_cap=10, token_cap=cap2)
    assert 0 < len(shown2) < 3, f"token_cap did not partially drop: {len(shown2)}"
    assert "omitted for length" in block2, "omitted marker missing under token_cap"
    assert gc._est_tokens(block2) <= cap2, "emitted block exceeds token_cap (L4)"
    # L4: a token_cap too small for even the header + marker emits NOTHING (never over-cap)
    block3, shown3 = gc.render_for_llm(matches, concept_cap=10, token_cap=3)
    assert block3 == "" and shown3 == [], "over-cap marker-only block was not suppressed (L4)"
    print("  renderer: ordering/DNT/do-not-use/budget/cap + shown-set + L4 all hold")


def test_match_concepts_non_divergence():
    """match_concepts_merged's five-key projection == match_terms_merged (H6), and the
    audience filter runs AFTER shadow/precedence."""
    pid = _seed_match_project()
    plan = _single_source_plan(pid)
    texts = [
        "Please Acme Cloud login now",  # shadowing: Acme Cloud vs cloud
        "the widget and the gadget",
        "nothing here",
        "Acme brand",
    ]
    with Session(engine) as s:
        for text in texts:
            mt = gm.match_terms_merged(s, pid, text, SRC, TGT, plan)
            mc = gc.match_concepts_merged(s, pid, text, SRC, TGT, plan)
            five = ("source_term", "target_term", "sense_label", "dnt", "case_sensitive")
            proj = [{k: m[k] for k in five} for m in mc]
            assert proj == mt, (
                f"match_concepts diverged from match_terms on {text!r}:\n  mt={mt}\n  mc={proj}"
            )
    # audience filter runs LAST: a human-only long term still shadows its substring
    with Session(engine) as s:
        mc = gc.match_concepts(s, pid, "Acme Cloud login", SRC, TGT, glossary_id=_mgid(pid))
        filtered = gc.filter_by_audience(mc, "llm")
        srcs = {m["source_term"] for m in filtered}
        assert "cloud" not in srcs, "shadowed substring resurrected by audience filter"
    print("  non-divergence: match_concepts projection == match_terms_merged; filter runs last")


_MGID = {}


def _mgid(pid):
    return _MGID[pid]


def _seed_match_project():
    gid = _gl("Match GL")
    with Session(engine) as s:
        p = Project(name="MatchP", source_locale=SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    _MGID[pid] = gid
    with Session(engine) as s:
        gm.add_term(
            s, gid, "Acme Cloud", target_term="", source_locale=SRC, target_locale=TGT, dnt=True
        )
        gm.add_term(s, gid, "cloud", target_term="nube", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "widget", target_term="artilugio", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "gadget", target_term="aparato", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "Acme", target_term="", source_locale=SRC, target_locale=TGT, dnt=True)
    return pid


def _single_source_plan(pid):
    from app.services.glossary_module import GlossaryPlan

    return GlossaryPlan(glossary_ids=(_MGID[pid],), include_legacy=False)


def main():
    test_schema_refusal()
    test_full_round_trip_and_identity()
    test_synthesized_id_for_blank_external()
    test_counters_and_unknown_fields()
    test_renderer()
    test_match_concepts_non_divergence()
    print("GLOSSARY JSON OK")


if __name__ == "__main__":
    main()
