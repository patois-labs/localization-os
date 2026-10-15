"""A Markdown link / image the engine damaged goes to review and never ships (#736).

Format soak round 11 (side instance on next/connectors 60ac0d7b0, local engine): Markdown link
syntax is unit TEXT, and de-DE wrote `Lesen Sie zuerst die [Einrichtungsschritte](#install-tools].`
for `Read the [setup steps](#install-tools) first.` (`)` became `]`: not a link any more). No QA
check looked at Markdown links, so it was stored as clean MT and exported. Round 10 had the same
class (`[**Haupt-**]Dashboard](...)`).

  comparator  quality.placeholder_violation also names (quality_part2.md_link_violation) a
              Markdown inline link / image, full or collapsed reference link, or URI autolink of
              the source that the target drops, adds, re-points (destination or reference label)
              or breaks (a `](` that no longer closes). Link text, alt text and titles may
              change. Silent: no link in the source, code spans, bracket text that is not a link
              (`[Enter]`, task boxes), escaped brackets, `a[i][j]`, `<url>` vs a bare url.
  seam        quality.commit_mt_output routes the live de-DE target to NEEDS_REVIEW under one
              placeholder_integrity flag (TM-ineligible); the correct target is clean MT.
  export      export.segment_tag_damage (rung 3, #711) reports the held target, and the .md
              export writes the SOURCE sentence for it (policy `source`), neighbours translated.
  linear      hostile shapes (backtick runs, bracket floods, unclosed titles) stay fast.

Hermetic (throwaway LOC_TMS_DATA_DIR, no engine). ASCII output; final line "MD LINK QA 736 OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mdlink736_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus, TargetLocale, TransMode
from app.services import export, quality, settings
from app.services.ingest import ingest_document

LIVE_SRC = "Read the [setup steps](#install-tools) first."
LIVE_BAD = "Lesen Sie zuerst die [Einrichtungsschritte](#install-tools]."  # live de-DE, round 11
LIVE_GOOD = "Lesen Sie zuerst die [Einrichtungsschritte](#install-tools)."

FLAGGED = [
    (LIVE_SRC, LIVE_BAD, "broken ](#install-tools"),
    # round 10 de-DE: the link text's bracket moved, the link is gone
    (
        "The [**main** dashboard](https://example.com/dash) shows *every* project.",
        "Das [**Haupt-**]Dashboard](https://example.com/dash) zeigt *jedes* Projekt.",
        "dropped or changed [...](https://example.com/dash)",
    ),
    (LIVE_SRC, "Lesen Sie zuerst die Einrichtungsschritte.", "dropped or changed"),  # dropped
    (LIVE_SRC, "Lesen Sie [die](#a) [Schritte](#install-tools).", "added [...](#a)"),  # added
    (LIVE_SRC, "Lesen Sie [Schritte](#installieren).", "added [...](#installieren)"),  # changed
    (LIVE_SRC, "Lesen Sie [Schritte] (#install-tools).", "dropped or changed"),  # `] (`: text
    (
        '![Trail map](images/map.png "Map of all trails")',
        '![Wanderkarte](images/karte.png "Karte aller Wege")',
        "![...](images/map.png)",
    ),
    ("![Logo](logo.svg)", "Logo", "![...](logo.svg)"),
    ("See [the docs][docs] for more.", "Siehe [die Doku][doku].", "[...][docs]"),
    ("Read the [FAQ][] first.", "Lies zuerst die [Haeufigen Fragen][].", "[...][faq]"),
    ("Visit <https://example.com/a> now.", "Besuchen Sie <https://example.com/b>.", "<https"),
    (
        "[![build](https://ci.example/b.svg)](https://ci.example/run)",
        "[![Build](https://ci.example/b.svg)] (https://ci.example/run)",
        "[...](https://ci.example/run)",
    ),
    ("Use `code` and [docs](/d).", "Nutze `code` und [Doku](/d", "broken ](/d"),
]
SILENT = [
    (LIVE_SRC, LIVE_GOOD),  # link text translated
    (
        '![Trail map](images/map.png "Map of all trails")',
        '![Wanderkarte](images/map.png "Karte aller Wege")',  # alt + title translated
    ),
    ("See [the docs][docs] and [setup](#s).", "Siehe [Einrichtung](#s) und [die Doku][docs]."),
    ("Visit <https://example.com/a> now.", "Besuchen Sie https://example.com/a jetzt."),
    ("Visit https://example.com/a now.", "Besuchen Sie <https://example.com/a> jetzt."),
    ("Use `[x](y)` and [docs](/d).", "Nutze `[x](z)` und [Doku](/d)."),  # code span
    ("Press [Enter] to go (fast).", "Druecken Sie [Eingabe] (schnell)."),  # not a link
    ("- [ ] Open the [dashboard](/dash)", "- [ ] Oeffne das [Dashboard](/dash)"),  # task box
    ("Item a[i][j] and [docs](/d)", "Element a[k][j] und [Doku](/d)"),  # index, not a ref
    ("Hello \\[not](a link) and [d](/d)", "Hallo \\[nicht](ein Link) und [D](/d)"),  # escaped
    (
        "A [page](https://en.wikipedia.org/wiki/Foo_(bar)) here",
        "Eine [Seite](https://en.wikipedia.org/wiki/Foo_(bar)) hier",
    ),
    ("See [docs](</my docs/a b>) now", "Siehe [Doku](</my docs/a b>) jetzt"),
    ("See [docs](/d) now", "詳細は[こちら](/d)"),  # ja, glued to CJK
    ("Open ⟦c1⟧ then [docs](/d).", "Oeffne ⟦c1⟧, dann [Doku](/d)."),
    ("No link [here] at all.", "Kein Link [hier] (gar nicht)."),  # no link in the source
    ("Click <b>Save</b>.", "Klicken Sie auf <b>Speichern</b>."),
]


def _fail(msg):
    print("FAIL:", msg.encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def test_comparator():
    for src, tgt, want in FLAGGED:
        r = quality.placeholder_violation(src, tgt)
        if "Markdown link" not in r or want not in r:
            _fail(f"[comparator] {tgt!r} must be flagged naming {want!r}, got {r!r}")
    for src, tgt in SILENT:
        r = quality.placeholder_violation(src, tgt)
        if "Markdown" in r:
            _fail(f"[comparator] must stay silent: {tgt!r}: {r!r}")
    if quality.placeholder_violation(LIVE_SRC, "   "):
        _fail("[comparator] an empty target abstains")
    print(f"  [comparator] {len(FLAGGED)} flagged, {len(SILENT)} silent OK")


def test_seam():
    with Session(engine) as session:
        p = Project(
            name="M736", source_locale="en-US", target_locales=["de-DE"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="ids.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale="de-DE",
            order_index=0,
            source_text=LIVE_SRC,
            status=SegStatus.NEW,
        )
        session.add(seg)
        session.commit()
        session.refresh(seg)
        out = quality.commit_mt_output(session, seg, LIVE_BAD, source="mt")
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
            _fail(f"[seam] the live target must go to review, TM-ineligible: {seg.status}")
        flags = [f for f in (seg.qa_flags or []) if f.get("type") == quality.PLACEHOLDER_FLAG_TYPE]
        if len(flags) != 1 or "#install-tools" not in flags[0].get("reason", ""):
            _fail(f"[seam] want one placeholder_integrity flag naming the link: {seg.qa_flags!r}")
        quality.commit_mt_output(session, seg, LIVE_GOOD, source="mt")
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.MT:
            _fail(f"[seam] the correct target must be clean MT: {seg.status} {seg.qa_flags!r}")
    print("  [seam] live de-DE target held for review; the correct one is clean OK")


MD = b"# Install the tools\n\nRead the [setup steps](#install-tools) first.\n\nClick Save.\n"


def test_export():
    settings.set_many({"export_tag_damage_policy": "source"})
    with Session(engine) as s:
        p = Project(name="M736x", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        d = ingest_document(s, p, "ids.md", MD, TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == d.id, Segment.target_locale == "de-DE")
            .order_by(Segment.order_index)
        ).all()
        srcs = [x.source_text for x in segs]
        if len(segs) != 3 or srcs[1] != LIVE_SRC:
            _fail(f"[export] unexpected segmentation: {srcs!r}")
        for seg, tgt in zip(
            segs, ["Werkzeuge installieren", LIVE_BAD, "Klicken Sie auf Speichern."], strict=True
        ):
            quality.commit_mt_output(s, seg, tgt, source="mt")
        s.commit()
        for seg in segs:
            s.refresh(seg)
        if segs[1].status != SegStatus.NEEDS_REVIEW:
            _fail(f"[export] the live target must be held: {segs[1].status}")
        if not export.segment_tag_damage(segs[1]) or export.segment_tag_damage(segs[2]):
            _fail("[export] rung 3 must report the held link damage and only it")
        rep = []
        text = export.build_translated_file(s, d, "de-DE", report=rep).decode("utf-8")
        if LIVE_BAD in text or LIVE_SRC not in text:
            _fail(f"[export] the broken link shipped (or the source was not written): {text!r}")
        if "Werkzeuge installieren" not in text or "Klicken Sie auf Speichern." not in text:
            _fail(f"[export] neighbours must stay translated: {text!r}")
        if rep != [{"unit": "segment", "index": 1, "kind": export.SUB_AUTOMATIC}]:
            _fail(f"[export] the substitution must be reported: {rep!r}")
        # a reviewer who approves the segment is trusted (rung 3 is NEEDS_REVIEW only)
        segs[1].status = SegStatus.APPROVED
        s.add(segs[1])
        s.commit()
        if export.segment_tag_damage(segs[1]):
            _fail("[export] an approved segment must not be substituted")
    print("  [export] held link damage exports the source, reported; approved ships OK")


def test_linear():
    from app.services.quality_part2 import md_link_violation

    t0 = time.perf_counter()
    for x in (
        "[](" * 66000,
        "[" * 200000,
        '[a](b "' * 25000,
        "`" * 100000,
        "`a``" * 40000,
        "<http:" * 30000,
        "[x][" * 50000,
        ("[a](" + "b" * 1999) * 90,
        "](" * 100000,
        "\\[" * 100000,
        "[a](b" + " " * 150000,
    ):
        md_link_violation("[a](b) " + x, x + "z")
    src = "".join(f"[t{i}](/p{i}) " for i in range(8000))
    reason = md_link_violation(src, src.replace("](", "] ("))
    if not reason or len(reason) > 400:
        _fail(f"[linear] many dropped links: reason {len(reason)} chars")
    took = time.perf_counter() - t0
    if took > 3.0:
        _fail(f"[linear] hostile shapes took {took:.1f} s")
    # gate: 20k changed autolinks (one substring search of the whole target per lost autolink
    # was quadratic: about 4 s at this size); still flagged, reason short
    src = "".join(f"<xx:{i}>" for i in range(20000))
    t1 = time.perf_counter()
    reason = md_link_violation(src, src.replace("<xx:", "<yy:"))
    took = time.perf_counter() - t1
    if not reason or len(reason) > 400 or took > 1.0:
        _fail(f"[linear] many changed autolinks: {took:.1f} s, reason {len(reason)} chars")
    print("  [linear] hostile shapes stay fast OK")


if __name__ == "__main__":
    init_db()
    test_comparator()
    test_seam()
    test_export()
    test_linear()
    print("MD LINK QA 736 OK")
