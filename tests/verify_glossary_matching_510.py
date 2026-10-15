"""Glossary matching and import fidelity (issue #510).

  1. _term_spans: a non-ASCII term no longer matches INSIDE a word ("uber" in "Darueber"
     style cases with real umlauts), terms ending in an apostrophe match before a space,
     CJK keeps substring matching, and a character whose lower() changes length does not
     shift the spans.
  2. Imported locales are canonicalized (`es_ES`, `es-es` -> `es-ES`) and the TBX locale
     match understands the underscore form.
  3. A wildcard (blank-locale) TARGET survives the default TBX-Basic export and re-import.
  4. A `.glossary.json` with a byte-order mark imports.
  5. A minted concept id never equals another concept's stored external id.
  6. Promoting a deprecated term to a usable status runs the source-surface collision rule.

Hermetic + offline. Final line: "GLOSSARY MATCHING 510 OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gloss510_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryEntry
from app.services import glossary_common as gc
from app.services import glossary_module as gm
from app.services import glossary_tbx

init_db()

from app.models import GlossaryTerm
from app.services import glossary_concepts as gcn
from app.services import settings as _settings

_settings.set_many({"terminology_concept_editor_enabled": "1"})
_settings.invalidate_cache()


def test_term_spans_respect_word_boundaries_for_non_ascii():
    uber = "über"
    assert gc._term_spans(uber, "Dar" + uber + " sprechen", True) == [], "inside Darueber"
    assert gc._term_spans(uber, uber + " alles", True) == [(0, 4)]
    assert gc._term_spans("Bär", "Bärenstark", True) == []
    assert gc._term_spans("Bär", "Der Bär, ja", True) == [(4, 7)]
    # an apostrophe edge matches before a space and at the end
    assert gc._term_spans("users'", "all users' data", True) == [(4, 10)]
    assert gc._term_spans("users'", "all users'", True) == [(4, 10)]
    assert gc._term_spans("user", "users data", True) == []
    # CJK has no word boundaries: substring stays
    assert gc._term_spans("数据", "大数据库", True) == [(1, 3)]
    # U+0130 lowercases to TWO code points: spans must still index the original text
    text = "İ cloud and cloud"
    spans = gc._term_spans("cloud", text, False)
    assert [text[a:b] for a, b in spans] == ["cloud", "cloud"], spans
    # case-insensitive non-ASCII
    assert gc._term_spans("Über", "ein über alles", False) == [(4, 8)]
    print("  [510-1] non-ASCII, apostrophe, CJK and length-changing lower() spans OK")


def test_inflected_languages_match_whole_words_like_ascii_terms():
    """Gate decision (MEDIUM b): before #510 a term matched INSIDE a longer word only
    when it happened to contain a non-ASCII letter (an `isascii()` test meant for CJK).
    ASCII terms in the same languages were always whole-word, so inflected matching was
    never a feature: Finnish "talo" never matched "talossa" while "äiti" matched
    "äitini". #510 makes every spaced script whole-word, consistently."""
    cases = [
        # (term, text that only holds an inflected/longer form, text with the term)
        ("книга", "Много книгами здесь", "Это книга, да"),  # Russian
        ("książka", "Nowa książkami tu", "To jest książka."),  # Polish
        ("äiti", "Hän on äitini", "Äiti tuli kotiin"),  # Finnish, non-ASCII
        ("talo", "Olen talossa", "Iso talo on tuossa"),  # Finnish, ASCII (as always)
        ("İstanbul", "İstanbul'dan geldim", "İstanbul büyük"),  # Turkish: apostrophe ends it
    ]
    for term, inflected, plain in cases:
        if term == "İstanbul":
            # Turkish writes the suffix after an apostrophe, so the word itself still
            # matches: whole-word is not a loss there.
            assert gc._term_spans(term, inflected, True) == [(0, 8)], term
        else:
            assert gc._term_spans(term, inflected, False) == [], (term, inflected)
        assert gc._term_spans(term, plain, False), (term, plain)
    # Cyrillic case-insensitivity still works
    assert gc._term_spans("Книга", "это книга", False) == [(4, 9)]
    print("  [510-1b] Russian/Polish/Finnish match whole words, same as ASCII terms OK")


def test_tbx_lang_match_accepts_underscore_locales():
    assert glossary_tbx._lang_match("es_ES", "es-ES")
    assert glossary_tbx._lang_match("es_MX", "es-ES")
    assert not glossary_tbx._lang_match("es_ES", "fr-FR")
    print("  [510-2a] TBX locale match handles es_ES OK")


def _glossary(name):
    with Session(engine) as s:
        g = gm.create(s, name)
        return g.id


def test_import_canonicalizes_locales():
    gid = _glossary("canon 510")
    with Session(engine) as s:
        gm.import_csv(s, gid, b"source_term,target_term\nhello,hola\n", "en_us", "es_ES")
        rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        assert rows, "the CSV must import"
        assert {(r.source_locale, r.target_locale) for r in rows} == {("en-US", "es-ES")}, [
            (r.source_locale, r.target_locale) for r in rows
        ]
    print("  [510-2b] imported locales are stored canonical OK")


def test_wildcard_target_round_trips_through_default_tbx():
    gid = _glossary("wild 510")
    with Session(engine) as s:
        gm.add_term(s, gid, "gizmo", "artilugio", "en-US", "")  # blank TARGET locale
        data = gm.export_bytes(s, gid, fmt="tbx")
    assert b"und-x-anytgt" in data, data.decode("utf-8")[:2000]
    gid2 = _glossary("wild 510 again")
    with Session(engine) as s:
        res = gm.import_bytes(s, gid2, data, "en-US", "es-ES", filename="w.tbx")
        rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid2)).all()
        got = {(r.source_term, r.target_term) for r in rows}
        assert ("gizmo", "artilugio") in got, (res, got)
    print("  [510-3] a wildcard target survives export and re-import OK")


def test_json_with_bom_imports():
    gid = _glossary("bom 510")
    with Session(engine) as s:
        gm.add_term(s, gid, "widget", "artilugio", "en-US", "es-ES")
        data = gm.export_bytes(s, gid, fmt="json")
    assert json.loads(data.decode("utf-8"))  # sanity
    gid2 = _glossary("bom 510 again")
    with Session(engine) as s:
        gm.import_bytes(
            s, gid2, b"\xef\xbb\xbf" + data, "en-US", "es-ES", filename="x.glossary.json"
        )
        rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid2)).all()
        assert any(r.source_term == "widget" for r in rows), rows
    print("  [510-5] a .glossary.json with a BOM imports OK")


def test_minted_concept_id_avoids_stored_external_ids():
    from app.services import glossary_concepts as gcn

    class _C:
        def __init__(self, cid, ext):
            self.id, self.external_id = cid, ext

    used: set[str] = set()
    a, b = _C(7, ""), _C(9, "c7")
    reserved = frozenset(["c7"])
    ids = [gcn._doc_concept_id(b, used, reserved), gcn._doc_concept_id(a, used, reserved)]
    assert ids[0] == "c7" and ids[1] != "c7" and len(set(ids)) == 2, ids
    print("  [510-6] a minted concept id never equals a stored external id OK")


def test_promoting_a_deprecated_term_runs_the_collision_check():
    gid = _glossary("promote 510")
    with Session(engine) as s:
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gcn.create_concept(
            s,
            gid,
            terms=[
                {"locale": "en-US", "term": "bank", "status": "preferred"},
                {"locale": "es-ES", "term": "banco", "status": "preferred"},
            ],
        )
        c2 = gcn.create_concept(
            s, gid, terms=[{"locale": "en-US", "term": "bank", "status": "deprecated"}]
        )
        term = s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == c2.id)).first()
        try:
            gcn.edit_concept_term(s, gid, c2.id, term.id, status="preferred")
        except gcn.ConceptError:
            pass
        else:
            raise AssertionError("promoting onto a live source surface must be refused")
    print("  [510-4] promoting a deprecated term runs the collision rule OK")


def main():
    test_term_spans_respect_word_boundaries_for_non_ascii()
    test_inflected_languages_match_whole_words_like_ascii_terms()
    test_tbx_lang_match_accepts_underscore_locales()
    test_import_canonicalizes_locales()
    test_wildcard_target_round_trips_through_default_tbx()
    test_json_with_bom_imports()
    test_minted_concept_id_avoids_stored_external_ids()
    test_promoting_a_deprecated_term_runs_the_collision_check()
    print("GLOSSARY MATCHING 510 OK")


if __name__ == "__main__":
    main()
