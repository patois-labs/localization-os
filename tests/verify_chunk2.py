"""Chunk 2 verification — dual translation memory, offline.

- import a TMX into segment TM
- exact (100%) and fuzzy (75-99%) lookup; below-threshold returns None
- export round-trips back to valid TMX
- full-text TM: store then retrieve by content hash; different text misses
- end-to-end: upload a docx, "Apply 100% TM matches" fills exact targets
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from lxml import etree
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Segment
from app.services import tm

TMX = """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="en-US"/><body>
  <tu><tuv xml:lang="en-US"><seg>Adventure awaits.</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>L'aventure vous attend.</seg></tuv></tu>
  <tu><tuv xml:lang="en-US"><seg>Save up to 50% this weekend only.</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>Economisez jusqu'a 50% ce week-end seulement.</seg></tuv></tu>
</body></tmx>"""


def make_docx() -> bytes:
    d = Docx()
    d.add_paragraph("Adventure awaits.")
    d.add_paragraph("A completely unrelated sentence about penguins.")
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def main() -> None:
    init_db()
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "TM Test",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # --- import TMX ---
    r = client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": "en-US", "target_locale": "fr-FR"},
        files={"file": ("mem.tmx", TMX.encode("utf-8"), "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "tm_imported=2" in r.headers["location"], r.headers.get(
        "location"
    )
    print("  imported 2 TMX units")

    with Session(engine) as s:
        # exact
        m = tm.lookup(s, pid, "Adventure awaits.", "en-US", "fr-FR")
        assert m and m["match_pct"] == 100, f"expected 100% exact, got {m}"
        assert m["target_text"] == "L'aventure vous attend."
        # fuzzy (punctuation change)
        m2 = tm.lookup(s, pid, "Adventure awaits!", "en-US", "fr-FR")
        assert m2 and 75 <= m2["match_pct"] < 100, f"expected fuzzy, got {m2}"
        # miss
        m3 = tm.lookup(s, pid, "Totally different content here.", "en-US", "fr-FR")
        assert m3 is None, f"expected no match, got {m3}"
        print(f"  exact=100%, fuzzy={m2['match_pct']}%, miss=None")

        # inline-fidelity TM exclusion (Phase 5 chunk 6, revised contract): a
        # source carrying inline placeholder tokens is EXCLUDED from TM entirely —
        # it must NOT match a tagless TM twin. The old "strip tokens then match"
        # canonicalization poisoned the TM (a token-bearing target got served to a
        # tagless segment / different inline_map, leaking literal tokens), so the
        # contract is now: token-bearing segments skip TM lookup AND write-back.
        # normalize() is whitespace-only again and does NOT strip tokens.
        assert tm.normalize("  a   b  ") == "a b", tm.normalize("  a   b  ")
        assert tm.normalize("⟦g1⟧Adventure⟦/g1⟧ awaits.") == "⟦g1⟧Adventure⟦/g1⟧ awaits.", (
            "normalize must NOT strip inline tokens"
        )
        assert tm.is_tm_eligible("Adventure awaits.") is True
        assert tm.is_tm_eligible("⟦g1⟧Adventure⟦/g1⟧ awaits.⟦x2⟧") is False
        m4 = tm.lookup(s, pid, "⟦g1⟧Adventure⟦/g1⟧ awaits.⟦x2⟧", "en-US", "fr-FR")
        assert m4 is None, f"tokenized source must be excluded from TM, got {m4}"
        # a plain (tagless) source still exact-matches exactly as before
        m5 = tm.lookup(s, pid, "Adventure awaits.", "en-US", "fr-FR")
        assert m5 and m5["match_pct"] == 100 and m5["target_text"] == "L'aventure vous attend."
        # write-back of a token-bearing segment is a no-op (does not poison TM)
        assert tm.write_back(s, pid, "⟦g1⟧New source⟦/g1⟧", "cible", "en-US", "fr-FR") is None
        assert tm.lookup(s, pid, "New source", "en-US", "fr-FR") is None, (
            "token write-back must not create a tagless-matchable entry"
        )
        print("  token-bearing sources excluded from TM; plain TM unchanged")

    # --- export TMX ---
    r = client.get(f"/projects/{pid}/tm/export?source=en-US&target=fr-FR")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    tus = root.find("body").findall("tu")
    assert len(tus) == 2, f"export should have 2 TUs, got {len(tus)}"
    assert b"L'aventure vous attend." in r.content
    print(f"  exported {len(tus)} TUs to valid TMX")

    # --- full-text TM ---
    with Session(engine) as s:
        assert tm.ft_lookup(s, pid, "Whole doc body.", "en-US", "fr-FR") is None
        tm.ft_store(s, pid, "Whole doc body.", "Corps entier du document.", "en-US", "fr-FR")
        hit = tm.ft_lookup(s, pid, "Whole  doc   body.", "en-US", "fr-FR")  # whitespace-normalized
        assert hit == "Corps entier du document.", f"ft hit failed: {hit}"
        miss = tm.ft_lookup(s, pid, "A different document.", "en-US", "fr-FR")
        assert miss is None
        print("  full-text TM: store + normalized retrieval + miss OK")

    # --- end-to-end apply-tm ---
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    # the grid should show a 100% badge for the matching segment
    page = client.get(f"/documents/{doc_id}?locale=fr-FR").text
    assert "100%" in page, "TM match badge not shown in grid"

    r = client.post(
        f"/documents/{doc_id}/apply-tm",
        data={"locale": "fr-FR"},
        follow_redirects=False,
    )
    assert "tm_applied=1" in r.headers["location"], r.headers.get("location")

    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
        ).all()
        filled = [x for x in segs if x.target_text]
        assert len(filled) == 1 and filled[0].target_text == "L'aventure vous attend."
        assert filled[0].tm_match_pct == 100
        print(f"  apply-tm filled {len(filled)} exact match")

    # --- Chunk 1 scale-fix guards (SQL prefilter must not drop real matches) ---
    with Session(engine) as s:
        # count()/ft_count() return plain ints (SQL func.count, no row load).
        c = tm.count(s, pid)
        fc = tm.ft_count(s, pid)
        assert isinstance(c, int) and c >= 2, f"count int expected, got {c!r}"
        assert isinstance(fc, int) and fc >= 1, f"ft_count int expected, got {fc!r}"
        print(f"  count={c} ft_count={fc} (ints via SQL func.count)")

        # concordance is filtered + limited in SQL, case-insensitively.
        conc = tm.concordance(s, pid, "ADVENTURE", "en-US", "fr-FR")
        assert any(r["source"] == "Adventure awaits." for r in conc), (
            f"concordance should find 'Adventure' case-insensitively, got {conc}"
        )
        conc_t = tm.concordance(s, pid, "attend", "en-US", "fr-FR")  # target-side hit
        assert any(r["target"] == "L'aventure vous attend." for r in conc_t), (
            f"concordance should match on target text too, got {conc_t}"
        )
        conc_miss = tm.concordance(s, pid, "nonsense-zzz", "en-US", "fr-FR")
        assert conc_miss == [], f"concordance miss should be empty, got {conc_miss}"
        conc_lim = tm.concordance(s, pid, "a", "en-US", "fr-FR", limit=1)
        assert len(conc_lim) <= 1, f"concordance limit not honored: {conc_lim}"
        print("  concordance: SQL-filtered, case-insensitive, source+target, limited")

        # length-band miss: a candidate far outside the min_score-aware length
        # band is provably below threshold and must NOT be returned as a match.
        # "Adventure awaits." (len 17) vs a very long query -> band excludes it.
        long_q = "Adventure awaits " + ("x " * 60)
        assert tm.lookup(s, pid, long_q, "en-US", "fr-FR") is None, (
            "a query far outside the length band must miss"
        )
        # low min_score WIDENS the band: the same long query, at min_score=1,
        # now considers the short row (still scored honestly by fuzz.ratio).
        low = tm.lookup(s, pid, long_q, "en-US", "fr-FR", min_score=1)
        assert low is not None and low["match_pct"] >= 1, (
            f"low min_score should widen the band to find something, got {low}"
        )
        print(f"  length-band: high-min miss / low-min widens (match={low['match_pct']}%)")

    # --- Finding 1 regression: the removed upper length-band bound (adversarial
    #     review). The band ran on func.length(STORED source_text) but scoring on
    #     normalize()d text, and stored length >= normalized length. A row whose
    #     NORMALIZED length is in-band can have a padded STORED length above hi, so
    #     the old `stored_len <= hi` clause wrongly EXCLUDED a legitimate match.
    #     Reviewer's exact repro: stored source is a whitespace-run-padded string
    #     (normalizes to "the quick brown fax", 19 chars) vs query "xhe quick brown
    #     fox" (also 19). fuzz.ratio ~= 94.7%, so lookup MUST return it (>=75). ---
    with Session(engine) as s:
        padded = "the" + (" " * 30) + "quick brown fax"  # stored length ~48
        tm.write_back(s, pid, padded, "le renard brun rapide", "en-US", "fr-FR")
        assert tm.normalize(padded) == "the quick brown fax"
        hit = tm.lookup(s, pid, "xhe quick brown fox", "en-US", "fr-FR")
        assert hit is not None, (
            "whitespace-run-padded stored source must NOT be dropped by the length band"
        )
        assert hit["match_pct"] >= 75, ("expected >=75% match, got", hit)
        assert hit["target_text"] == "le renard brun rapide", hit
        print(f"  finding-1: whitespace-padded stored source matches ({hit['match_pct']}%)")

    # --- big-TM: exact match still found past the candidate cap ---
    with Session(engine) as s:
        # Seed ~200 filler rows that all share the "Adventure" prefix + length so
        # they crowd the length band + prefix LIKE, then confirm an EXACT source
        # still returns 100% (exact-hash short-circuit bypasses the cap entirely).
        for i in range(200):
            tm.write_back(
                s,
                pid,
                f"Adventure number {i:04d} of the filler batch here.",
                f"Aventure numero {i:04d} du lot.",
                "en-US",
                "fr-FR",
            )
        big = tm.lookup(s, pid, "Adventure awaits.", "en-US", "fr-FR")
        assert big and big["match_pct"] == 100, (
            f"exact match must survive a big TM (>{tm.TM_FUZZY_CANDIDATE_CAP} rows), got {big}"
        )
        assert big["target_text"] == "L'aventure vous attend."
        assert tm.count(s, pid) >= 200
        print(f"  big-TM ({tm.count(s, pid)} rows): exact still found at 100%")

    print("CHUNK 2 OK")


if __name__ == "__main__":
    main()
