"""LAW-TERM-1 compat byte-identity through T1 (SPEC-terminology-backend).

Hermetic + offline. The dual-write (LAW-TERM-2) keeps the legacy GlossaryEntry the
authoritative write target and adds only inert concept_id/term_id link columns, so every
compat READ seam must be byte-identical to a glossary that has NO concept store at all.

This suite proves it by BUILDING THE SAME LOGICAL GLOSSARY TWO WAYS and asserting the read
seams agree:

  * glossary A — through the dual-write seams (gm.add_term / gm.add_sense), which write the
    concept/term store alongside every entry;
  * glossary B — by inserting GlossaryEntry/GlossarySense rows DIRECTLY (no concepts).

Then across a battery of segments it asserts match_terms(A) == match_terms(B) EXACTLY (the
five-key dict, in order), prompts.glossary_line(A) == glossary_line(B), and the deterministic
gates return identical verdicts. It also pins:

  * match_terms emits EXACTLY the five keys source_term/target_term/sense_label/dnt/
    case_sensitive — no `exact`, nothing added (E5).
  * the CSV export header is the AMENDED header (LAW-TERM-1 CSV carve-out H15): the five meta
    columns PLUS the six concept-model term columns.
  * M9 (post round 6 / LAW-TERM-17): the compat ENTRY is authoritative for every field it
    carries. Two SEPARATELY-added synonyms get DISTINCT concepts, so a concept-level meta edit
    on one stays local; and the reconciler (rematerialize pass 1) is LINK-ONLY, so it never
    rewrites an existing entry's fields (source/target surface, dnt, case_sensitive, meta). A
    concept-editor edit that means to change an entry-carried field (edit_concept_term dnt /
    edit_concept definition) is written THROUGH to the entry at its own seam, not derived by a
    reconcile — so no shared concept can stamp one pair's value onto another.
  * R6-A/R6-B (both-mode differential): building the same fixture via the dual-write seams,
    reading it, then forcing a rematerialize, must leave every read seam BYTE-IDENTICAL — a
    reconcile can neither blank a wildcard DNT target nor multiply a case variant into a pair.

Final line: "GLOSSARY COMPAT OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_glcompat_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryEntry, GlossarySense
from app.services import glossary as gloss_engine
from app.services import glossary_module as gm
from app.services import prompts, quality

init_db()

# T1-lite: this suite exercises the concept-editor WRITE seams (and the .glossary.json
# import into a populated glossary), which are gated behind the DEFAULT-OFF
# terminology_concept_editor flag; enable it for the suite's fixtures.
from app.services import settings as _settings

_settings.set_many({"terminology_concept_editor_enabled": "1"})
_settings.invalidate_cache()

SRC, TGT = "en-US", "es-ES"

_SEGMENTS = [
    "Please login to the widget dashboard.",
    "Acme is a brand; the widget and gadget ship today.",
    "bank the money in your account",
    "walk along the river bank at dawn",
    "nothing relevant here",
    "ID and id are different",
]


def _build_dualwrite():
    with Session(engine) as s:
        g = gm.create(s, "Dualwrite GL")
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
    with Session(engine) as s:
        gm.add_term(
            s,
            gid,
            "login",
            target_term="acceso",
            source_locale=SRC,
            target_locale=TGT,
            meta={"part_of_speech": "noun", "definition": "sign in"},
        )
        gm.add_term(s, gid, "widget", target_term="artilugio", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "gadget", target_term="artilugio", source_locale=SRC, target_locale=TGT)
        gm.add_term(s, gid, "Acme", target_term="", source_locale=SRC, target_locale=TGT, dnt=True)
        gm.add_term(s, gid, "ID", target_term="identificador", source_locale=SRC, target_locale=TGT)
        e = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "river", "orilla", context_cue="river water shore")
    return gid


def _build_direct():
    """The SAME logical glossary as _build_dualwrite, inserted directly (no concepts)."""
    with Session(engine) as s:
        g = gm.create(s, "Direct GL")
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
        rows = [
            ("login", "acceso", False, {"part_of_speech": "noun", "definition": "sign in"}),
            ("widget", "artilugio", False, None),
            ("gadget", "artilugio", False, None),
            ("Acme", "", True, None),
            ("ID", "identificador", False, None),
            ("bank", "banco", False, None),
        ]
        for src, tgt, dnt, meta in rows:
            s.add(
                GlossaryEntry(
                    glossary_id=gid,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_term=src,
                    target_term=tgt,
                    dnt=dnt,
                    meta=meta,
                )
            )
        s.commit()
        bank = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
            )
        ).first()
        s.add(
            GlossarySense(
                glossary_entry_id=bank.id,
                sense_label="river",
                context_cue="river water shore",
                target_term="orilla",
            )
        )
        s.commit()
    return gid


def _match(gid, text):
    with Session(engine) as s:
        # deterministic offline provider (no key) -> MockProvider; sense picks are stable
        return gloss_engine.match_terms(s, None, text, SRC, TGT, glossary_id=gid)


def test_byte_identity_reads():
    a = _build_dualwrite()
    b = _build_direct()
    five = {"source_term", "target_term", "sense_label", "dnt", "case_sensitive"}
    for text in _SEGMENTS:
        ma = _match(a, text)
        mb = _match(b, text)
        assert ma == mb, f"match_terms diverged (dual vs direct) on {text!r}:\n  A={ma}\n  B={mb}"
        for d in ma:
            assert set(d) == five, f"match_terms emitted non-five-key dict (E5): {set(d)}"
        la = prompts.glossary_line(ma)
        lb = prompts.glossary_line(mb)
        assert la == lb, f"glossary_line diverged on {text!r}: {la!r} vs {lb!r}"
        # the deterministic gates read the same dicts -> identical verdicts
        for target in ("acceso artilugio banco", ""):
            assert quality.dnt_violations(text, target, ma) == quality.dnt_violations(
                text, target, mb
            ), "dnt_violations diverged"
    print("  reads: match_terms + glossary_line + dnt gate byte-identical (dual-write == direct)")


def test_csv_amended_header():
    gid = _build_dualwrite()
    with Session(engine) as s:
        data = gloss_engine.export_csv(s, None, glossary_id=gid)
    header = data.decode("utf-8").splitlines()[0].split(",")
    for col in ("part_of_speech", "definition", "usage_example", "subject_field", "notes"):
        assert col in header, f"CSV lost a legacy meta column: {col}"
    for col in (
        "status",
        "term_type",
        "audience",
        "grammatical_gender",
        "grammatical_number",
        "geographical_usage",
    ):
        assert col in header, f"CSV missing the amended concept column: {col}"
    print("  csv: export header carries the five meta + six concept-model columns (H15)")


def test_synonym_concept_meta_is_per_concept():
    """M9 (dual-write path, §4.6-4): two synonyms ADDED SEPARATELY get DISTINCT concepts, so
    a concept-level meta edit on one stays local — and match_terms is unperturbed. (The
    §4.4-2 shared-concept-across-synonyms projection is the multi-term-concept import path
    owned by T2 — see the T1 errata.)"""
    with Session(engine) as s:
        g = gm.create(s, "Synonym GL")
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
    with Session(engine) as s:
        e1 = gm.add_term(s, gid, "car", target_term="coche", source_locale=SRC, target_locale=TGT)
        _ = gm.add_term(s, gid, "auto", target_term="coche", source_locale=SRC, target_locale=TGT)
        # a concept-level definition edit on car's entry
        gm.edit_term(s, gid, e1.id, meta={"definition": "a motor vehicle"})
    with Session(engine) as s:
        car = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "car")).first()
        auto = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == "auto")).first()
        # distinct concepts (distinct source terms) -> auto's meta is NOT rewritten by car's
        assert car.concept_id != auto.concept_id, "distinct synonyms share a concept unexpectedly"
        assert (car.meta or {}).get("definition") == "a motor vehicle", "concept-meta edit lost"
        # match_terms still byte-identical five-key dicts
        m = gloss_engine.match_terms(s, None, "the car and the auto", SRC, TGT, glossary_id=gid)
        assert {d["source_term"] for d in m} == {"car", "auto"}
        for d in m:
            assert set(d) == {"source_term", "target_term", "sense_label", "dnt", "case_sensitive"}
    print("  meta: concept-level derivation is per-concept; match_terms stays five-key identical")


def test_reconcile_does_not_move_the_read_path():
    """R6-A/R6-B (gate round 6): the LAW-TERM-1 read path must be BYTE-IDENTICAL before and
    after a forced `rematerialize` on a fixture carrying every shape that used to diverge — a
    multi-pair shared surface with DIFFERENT per-pair meta and dnt, a case variant, a
    full-wildcard DNT `X->X` row, a wildcard-SOURCE row and a sense. Pass 1 is link-only, so a
    reconcile can neither blank a DNT target (R6-A) nor multiply a case variant into another
    pair (R6-B)."""
    from app.services import glossary_concepts as gc

    fr = "fr-FR"
    with Session(engine) as s:
        g = gm.create(s, "BothMode GL")
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}", f"{SRC}:{fr}"])
    with Session(engine) as s:
        # multi-pair shared surface, DIFFERENT per-pair meta
        gm.add_term(
            s,
            gid,
            "invoice",
            target_term="factura",
            source_locale=SRC,
            target_locale=TGT,
            meta={"definition": "a bill (es)"},
        )
        gm.add_term(
            s,
            gid,
            "invoice",
            target_term="facture",
            source_locale=SRC,
            target_locale=fr,
            meta={"definition": "une facture (fr)"},
        )
        # a case variant on the shared surface (es only)
        gm.add_term(s, gid, "Invoice", target_term="Factura", source_locale=SRC, target_locale=TGT)
        # per-pair dnt: DNT in es, translatable in fr
        gm.add_term(s, gid, "Acme", target_term="", source_locale=SRC, target_locale=TGT, dnt=True)
        gm.add_term(s, gid, "Acme", target_term="Acme SA", source_locale=SRC, target_locale=fr)
        # a FULL-wildcard DNT row whose target equals its source
        gm.add_term(
            s, gid, "Widget", target_term="Widget", source_locale="", target_locale="", dnt=True
        )
        # a sense on a shared surface
        e = gm.add_term(s, gid, "bank", target_term="banco", source_locale=SRC, target_locale=TGT)
        gm.add_sense(s, gid, e.id, "river", "orilla", context_cue="water shore")

    segs = [
        "the invoice and the Invoice are due",
        "Acme and the Widget ship to the bank today",
        "walk along the river bank",
    ]

    def _reads():
        out = []
        with Session(engine) as s:
            for seg in segs:
                for tl in (TGT, fr):
                    m = gloss_engine.match_terms(s, None, seg, SRC, tl, glossary_id=gid)
                    out.append(("MT", tl, seg, json.dumps(m, sort_keys=True)))
                    out.append(("GL", tl, seg, prompts.glossary_line(m)))
                    out.append(
                        (
                            "DV",
                            tl,
                            seg,
                            json.dumps(
                                quality.dnt_violations(seg, "una traduccion sin marcas", m),
                                sort_keys=True,
                                default=str,
                            ),
                        )
                    )
                    out.append(
                        (
                            "GV",
                            tl,
                            seg,
                            json.dumps(
                                quality.glossary_term_violations(
                                    seg, "una traduccion sin marcas", m, target_locale=tl
                                ),
                                sort_keys=True,
                                default=str,
                            ),
                        )
                    )
            ents = sorted(
                (e.source_term, e.target_term, e.source_locale, e.target_locale, bool(e.dnt))
                for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
            )
            out.append(("E", ents))
        return out

    before = _reads()
    with Session(engine) as s:
        gc.project(s, gid, derive_senses=True)
        s.commit()
    after = _reads()
    assert before == after, (
        "R6-A/R6-B: a reconcile moved the read path:\n  before-only="
        f"{[x for x in before if x not in after]}\n  after-only="
        f"{[x for x in after if x not in before]}"
    )
    print("  R6-A/R6-B: the read path is byte-identical before and after a forced reconcile")


def main():
    test_byte_identity_reads()
    test_csv_amended_header()
    test_synonym_concept_meta_is_per_concept()
    test_reconcile_does_not_move_the_read_path()
    print("GLOSSARY COMPAT OK")


if __name__ == "__main__":
    main()
