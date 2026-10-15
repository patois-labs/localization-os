"""Longest match wins in the glossary and DNT gates (issue #663).

A shorter glossary term that only occurs INSIDE a longer matched term ("trail" inside the
DNT "Colorado Trail") must not demand its own target ("sentier"); an occurrence OUTSIDE
the longer term still does. Also pins the prompt side across glossaries: terms merged from
several glossaries drop the shadowed shorter term, as one glossary always did.

Hermetic + offline. Final line: "GLOSSARY LONGEST MATCH 663 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gloss663_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Glossary, Project
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm
from app.services.glossary_module import GlossaryPlan
from app.services.quality_common import dnt_violations
from app.services.quality_part2 import glossary_term_violations

SRC, TGT = "en-US", "fr-FR"
TERMS = [
    {"source_term": "trail", "target_term": "sentier", "dnt": False},
    {"source_term": "Colorado Trail", "target_term": "Colorado Trail", "dnt": True},
]
SRC_TEXT = "We packed our gear into one bundle, tested on the Colorado Trail in October frost."
TGT_TEXT = "Nous avons tout mis dans un pack, teste sur le Colorado Trail sous le givre."


def test_checker():
    assert glossary_term_violations(SRC_TEXT, TGT_TEXT, TERMS, target_locale="fr") == [], (
        "inner 'trail' inside the DNT 'Colorado Trail' still demands 'sentier'"
    )
    # a free occurrence outside the long term is still a constraint
    src2 = SRC_TEXT + " The trail was icy."
    tgt2 = TGT_TEXT + " Le chemin etait glace."
    got = glossary_term_violations(src2, tgt2, TERMS, target_locale="fr")
    assert got == [{"source_term": "trail", "target_term": "sentier"}], got
    # ... and satisfied when its target is present
    ok = glossary_term_violations(src2, tgt2 + " sentier", TERMS, target_locale="fr")
    assert ok == [], ok
    # a longer NON-dnt term shadows too
    terms = [
        {"source_term": "cloud", "target_term": "nube", "dnt": False},
        {"source_term": "cloud storage", "target_term": "almacenamiento", "dnt": False},
    ]
    assert (
        glossary_term_violations(
            "Use cloud storage.", "Use almacenamiento.", terms, target_locale="es"
        )
        == []
    )
    # a plain miss with no longer term is unchanged
    assert glossary_term_violations(
        "The trail is long.", "Le chemin est long.", TERMS[:1], target_locale="fr"
    ) == [{"source_term": "trail", "target_term": "sentier"}]
    # equal-length case variants never shadow each other
    pair = [
        {"source_term": "trail", "target_term": "sentier", "dnt": False},
        {"source_term": "Trail", "target_term": "Sentier", "dnt": False, "case_sensitive": True},
    ]
    assert glossary_term_violations("A Trail.", "Un chemin.", pair, target_locale="fr"), pair
    # an informational longer entry (no target, not DNT) is never checked, so it must not
    # shadow a shorter required term (review fix)
    info = [
        {"source_term": "trail", "target_term": "sentier", "dnt": False},
        {"source_term": "Colorado Trail", "target_term": "", "dnt": False},
    ]
    assert glossary_term_violations(SRC_TEXT, TGT_TEXT, info, target_locale="fr") == [
        {"source_term": "trail", "target_term": "sentier"}
    ]
    # CJK: inner term inside a longer CJK term is shadowed, a free occurrence is not
    cjk = [
        {"source_term": "云", "target_term": "nuage", "dnt": False},
        {"source_term": "云存储", "target_term": "stockage", "dnt": False},
    ]
    assert (
        glossary_term_violations("使用云存储。", "Utiliser stockage.", cjk, target_locale="fr")
        == []
    )
    assert glossary_term_violations("云和云存储", "stockage", cjk, target_locale="fr") == [
        {"source_term": "云", "target_term": "nuage"}
    ]
    print("  checker: inner span shadowed, free occurrence still enforced")


def test_dnt_sibling():
    terms = [
        {"source_term": "Cloud", "dnt": True},
        {"source_term": "Acme Cloud", "dnt": True},
    ]
    # 'Acme Cloud' kept verbatim, the shadowed inner 'Cloud' must not be flagged on its own
    assert dnt_violations("Welcome to Acme Cloud", "Bienvenue a Acme Cloud", terms) == []
    # dropped entirely: only the longer term is reported
    assert dnt_violations("Welcome to Acme Cloud", "Bienvenue a nuage", terms) == ["Acme Cloud"]
    # a free inner occurrence is still its own constraint (reported alongside)
    got = dnt_violations("Cloud and Acme Cloud", "nuage et nuage", terms)
    assert got == ["Cloud", "Acme Cloud"], got
    print("  dnt: longest match wins")


def test_merged_prompt_terms():
    init_db()
    with Session(engine) as s:
        p = Project(name="P663", source_locale=SRC)
        s.add(p)
        g1 = Glossary(name="Names 663")
        g2 = Glossary(name="Generic 663")
        s.add(g1)
        s.add(g2)
        s.commit()
        s.refresh(p)
        s.refresh(g1)
        s.refresh(g2)
        pid, g1id, g2id = p.id, g1.id, g2.id
    with Session(engine) as s:
        gm.add_term(s, g1id, "Colorado Trail", "", source_locale=SRC, target_locale=TGT, dnt=True)
        gm.add_term(s, g2id, "trail", "sentier", source_locale=SRC, target_locale=TGT)
    plan = GlossaryPlan(glossary_ids=(g1id, g2id), include_legacy=False)
    with Session(engine) as s:
        for fn in (gm.match_terms_merged, gc.match_concepts_merged):
            got = [t["source_term"] for t in fn(s, pid, SRC_TEXT, SRC, TGT, plan)]
            assert got == ["Colorado Trail"], (fn.__name__, got)
            got = [
                t["source_term"]
                for t in fn(s, pid, SRC_TEXT + " The trail is icy.", SRC, TGT, plan)
            ]
            assert sorted(got) == ["Colorado Trail", "trail"], (fn.__name__, got)
    print("  prompt side: cross-glossary shadowing, both merged matchers agree")


def main():
    test_checker()
    test_dnt_sibling()
    test_merged_prompt_terms()
    print("GLOSSARY LONGEST MATCH 663 OK")


if __name__ == "__main__":
    main()
