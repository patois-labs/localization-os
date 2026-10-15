"""Three review holds on machine output (the maintainer's decisions 11, 14 and 16 of 2026-10-11).

The format soak found three kinds of output every existing check passed:
  16  dates   en-US 10/11/2026 (October 11) came back as de-DE 10.11.2026 and ar 2026/11/10,
              and Hebrew / Thai kept 10/11/2026: every one of those readers takes November 10.
              The number check sees the same numbers and passes.
  11  breaks  a PowerPoint table cell "Target URI" came back with a line break and an invented
              "Source:" label; nothing noticed the added line.
  14  glue    German / Dutch / Finnish / Korean / Turkish / Arabic / Hebrew: the engine joined two
              words across a formatting change ("bevor Sie|das Fenster"); #721b leaves those
              languages as written because a real compound looks the same.

  comparator  quality.review_hold_violation (app/services/quality_part3.py) names each case;
              the false-positive guards (correct conversions, same-convention locales,
              unambiguous dates, compounds, particles, suffixes, prefix clitics, splits the
              source also writes without a space, kept line structure) stay silent.
  seam        quality.commit_mt_output on a real-MT write (lang_guard) routes the de-DE swapped
              date and the added line break to NEEDS_REVIEW under ONE review_hold flag,
              TM-ineligible; the correct target is clean MT and the stale flag is cleared; the
              offline mock path (no lang_guard) is never held.
  surfaces    the editor badge (documents.editor_qa_flags, label "check"), the headless
              detail summary (api_cli.review_summary_line "review-hold") and the surfaced set.
  linear      hostile shapes stay fast.

Hermetic (throwaway LOC_TMS_DATA_DIR, no engine). ASCII output; final line "REVIEW HOLDS QA OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_reviewholds_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import quality

US = "en-US"
S1, S2, S3 = "⟦/r1⟧⟦r2⟧", "⟦r1⟧", "⟦/r2⟧"  # run split, open, close

# (source, target, target locale, source locale, text the reason must contain)
DATES_FLAGGED = [
    ("Due on 10/11/2026.", "Fällig am 10.11.2026.", "de-DE", US, "reads as 2026-11-10"),
    ("Due on 10/11/2026.", "Fällig am 10. 11. 2026.", "de-DE", US, "reads as 2026-11-10"),
    ("Due on 10/11/2026.", "مستحق في 2026/11/10.", "ar-SA", US, "reads as 2026-11-10"),
    ("Due on 10/11/2026.", "לתשלום ב-10/11/2026.", "he-IL", US, "reads as 2026-11-10"),
    ("Due on 10/11/2026.", "ครบกำหนด 10/11/2026", "th-TH", US, "reads as 2026-11-10"),
    # a year-first language reading a day/month/year layout: unknown, held
    ("Due on 10/11/2026.", "期限は 10/11/2026 です。", "ja-JP", US, "day and month swapped"),
    # en-GB means 10 November; kept as written, a US reader takes 11 October
    ("Due on 10/11/2026.", "Due on 10/11/2026.", US, "en-GB", "reads as 2026-10-11"),
    # a written-out source date the engine wrote in numbers the wrong way round for fr-FR
    ("Due on October 11, 2026.", "Échéance le 10/11/2026.", "fr-FR", US, "2026-11-10"),
    # numbers in no source date at all
    ("Due soon.", "Fällig am 10.11.2026.", "de-DE", US, "not a date the source writes"),
]
DATES_SILENT = [
    ("Due on 10/11/2026.", "Fällig am 11.10.2026.", "de-DE", US),  # correct conversion
    ("Due on 10/11/2026.", "期限は 2026/10/11 です。", "ja-JP", US),  # year first, right
    ("Due on 10/11/2026.", "Fällig am 10.11.2026.", "de-DE", "en-GB"),  # both day first
    ("Due on 10/11/2026.", "Due on 11/10/2026.", US, "en-GB"),  # correct conversion to US
    ("Due on 10/11/2026.", "Due on 10/11/2026.", "fil", US),  # both MDY
    ("Due on 25/12/2026.", "Fällig am 25.12.2026.", "de-DE", "en-GB"),  # unambiguous
    ("Due on 12/25/2026.", "Fällig am 25.12.2026.", "de-DE", US),  # unambiguous
    ("Due on 10/10/2026.", "Fällig am 10.10.2026.", "de-DE", US),  # day == month
    ("Due on 2026-11-10.", "Fällig am 2026-11-10.", "de-DE", US),  # ISO both sides
    ("Due on October 11, 2026.", "Échéance le 11/10/2026.", "fr-FR", US),  # written, right
    ("Due on 11 October 2026.", "Fällig am 11.10.2026.", "de-DE", US),
    ("Version 1.10.11.2026 ships.", "Version 1.10.11.2026 erscheint.", "de-DE", US),
    ("Host 10.11.2026.4 is up.", "Host 10.11.2026.4 läuft.", "de-DE", US),
    ("Due on 10/11/26.", "Fällig am 10.11.26.", "de-DE", US),  # two-digit year: not judged
    ("Due on 10/11/2026.", "Fällig am 11. Oktober 2026.", "de-DE", US),  # written target
    # gate: IDs and codes that look like dates
    ("Order 10/11/2026-A7", "Bestellung 10/11/2026-A7", "de-DE", US),
    ("Ticket ABC-10-11-2026", "Ticket ABC-10-11-2026", "de-DE", US),
    ("Part 10-11-4455", "Teil 10-11-4455", "de-DE", US),
    ("Meet at 10:11 on 2026-10-11.", "Treffen um 10:11 am 2026-10-11.", "de-DE", US),
]

BREAKS_FLAGGED = [
    ("Target URI", "Quelle:\nZiel-URI", "the source has 0, the target has 1"),  # live PPTX cell
    ("First line\nSecond line", "Erste Zeile Zweite Zeile", "the source has 1, the target has 0"),
    ("Line one<br>Line two", "Zeile eins Zeile zwei", "the source has 1, the target has 0"),
    ("One\nTwo", "Eins\nZwei\nDrei", "the source has 1, the target has 2"),
]
BREAKS_SILENT = [
    ("First line\nSecond line", "Erste Zeile\nZweite Zeile"),
    ("First line\\nSecond line", "Erste Zeile\nZweite Zeile"),  # literal escape vs real (#706)
    ("First line\r\nSecond line", "Erste Zeile\nZweite Zeile"),  # CRLF vs LF
    ("Line one<br/>Line two", "Zeile eins\nZeile zwei"),  # a <br> written as a newline
    ("Target URI", "Ziel-URI\n"),  # a trailing break is the whitespace check's (and #712's)
    ("Target URI", "\nZiel-URI"),
    ("Target URI", "Ziel-URI"),
]

GLUE_FLAGGED = [
    (
        f"Save your changes before you{S1} close the window?",
        f"Speichern Sie Ihre Änderungen, bevor Sie{S1}das Fenster schließen?",
        "de-DE",
        f"Sie{S1}das",
    ),
    (f"Click{S1} Save now.", f"Klicken{S1}Sie jetzt auf Speichern.", "de-DE", f"Klicken{S1}Sie"),
    (
        f"Save before{S1} closing the window.",
        f"Opslaan voordat{S1}het venster sluit.",
        "nl-NL",
        f"voordat{S1}het",
    ),
    (f"Open{S1} the file.", f"פתח את{S1}הקובץ.", "he-IL", f"את{S1}הקובץ"),
    (f"Copy from{S1} the file.", f"انسخ من{S1}الملف.", "ar-SA", f"من{S1}الملف"),
    (f"Open the file{S1} now.", f"파일을{S1}지금 여세요.", "ko-KR", f"파일을{S1}지금"),
    (f"The file and{S1} folder.", f"Dosya ve{S1}klasör.", "tr-TR", f"ve{S1}klasör"),
    (f"Files and{S1} folders.", f"Tiedostot ja{S1}kansiot.", "fi-FI", f"ja{S1}kansiot"),
]
GLUE_SILENT = [
    (f"{S2}User{S1} name{S3}", f"{S2}Benutzer{S1}name{S3}", "de-DE"),  # compound
    (f"{S2}User{S1} name{S3}", f"{S2}gebruikers{S1}naam{S3}", "nl-NL"),
    (f"{S2}Network{S1} connection{S3}", f"{S2}verkko{S1}yhteys{S3}", "fi-FI"),
    # a Korean particle after the split
    (f"Save the {S2}document{S1} now{S3}.", f"{S2}문서{S1}를 지금{S3} 저장하세요.", "ko-KR"),
    (f"Open the {S2}file{S1} now{S3}.", f"{S2}Dosya{S1}yı şimdi{S3} aç.", "tr-TR"),  # suffix
    (f"Open {S2}and{S1} the file{S3}.", f"{S2}ו{S1}הקובץ{S3}", "he-IL"),  # prefix clitic
    (f"Open {S2}and{S1} the file{S3}.", f"{S2}و{S1}الملف{S3}", "ar-SA"),
    (f"{S2}village{S1} hall{S3}", f"{S2}마을{S1}회관{S3}", "ko-KR"),  # noun ending like a particle
    # the source writes this split without a space too
    (f"{S2}Power{S1}Point{S3} is open.", f"{S2}Power{S1}Point{S3} ist offen.", "de-DE"),
    (
        f"Save your changes before you{S1} close the window?",
        f"Speichern Sie Ihre Änderungen, bevor Sie {S1}das Fenster schließen?",  # space kept
        "de-DE",
    ),
    (
        f"Save your changes before you{S1} close the window?",
        f"Guarde los cambios antes de{S1}cerrar la ventana?",  # es: #721 repairs, not judged
        "es-ES",
    ),
]


def _fail(msg):
    print("FAIL:", msg.encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def test_comparator():
    hold = quality.review_hold_violation
    for src, tgt, loc, sloc, want in DATES_FLAGGED:
        r = hold(src, tgt, loc, source_locale=sloc)
        if "ambiguous date" not in r or want not in r:
            _fail(f"[dates] {tgt!r} ({sloc} -> {loc}) must be held naming {want!r}, got {r!r}")
    for src, tgt, loc, sloc in DATES_SILENT:
        r = hold(src, tgt, loc, source_locale=sloc)
        if r:
            _fail(f"[dates] must stay silent: {tgt!r} ({sloc} -> {loc}): {r!r}")
    # the source locale is resolved lazily, only when the target has an ambiguous date
    calls = []
    hold("Hello", "Hallo", "de-DE", source_locale=lambda: calls.append(1) or US)
    if calls:
        _fail("[dates] the source locale must not be resolved without an ambiguous date")
    for src, tgt, want in BREAKS_FLAGGED:
        r = hold(src, tgt, "de-DE", source_locale=US)
        if "line breaks differ" not in r or want not in r:
            _fail(f"[breaks] {tgt!r} must be held naming {want!r}, got {r!r}")
    for src, tgt in BREAKS_SILENT:
        r = hold(src, tgt, "de-DE", source_locale=US)
        if r:
            _fail(f"[breaks] must stay silent: {tgt!r}: {r!r}")
    # an HTML paragraph keeps its source wrapping; a newline there is a space and a real break
    # is a tag, so HTML / Markdown are not judged
    wrapped = "This is a long\n   wrapped paragraph\n   in HTML."
    for fmt in ("html", "md", "xml", "epub", "mdx"):
        r = hold(wrapped, "Dies ist ein langer umbrochener Absatz in HTML.", "de-DE", fmt=fmt)
        if r:
            _fail(f"[breaks] {fmt}: a newline is not a break there: {r!r}")
    for fmt in ("pptx", "json", "po", "srt", "xlsx", "txt", None):
        r = hold("Target URI", "Quelle:\nZiel-URI", "de-DE", fmt=fmt)
        if "line breaks differ" not in r:
            _fail(f"[breaks] {fmt}: an added newline must be held: {r!r}")
    # a subtitle cue merged into one line is reflow; an added line in a cue is still held
    cue = "The meeting is on Friday,\nat 3:30 PM in room 204."
    if hold(cue, "会議は金曜日の午後3時30分、204号室で行われます。", "ja-JP", fmt="srt"):
        _fail("[breaks] a merged subtitle cue is reflow, not damage")
    three = "Das Treffen ist\nam Freitag,\num 15:30."
    if "line breaks differ" not in hold(cue, three, "de-DE", fmt="vtt"):
        _fail("[breaks] an added line in a subtitle cue must be held")
    calls = []
    hold("A\nB", "X\nY", "de-DE", fmt=lambda: calls.append(1) or "html")
    if calls:
        _fail("[breaks] the format must not be resolved when the counts match")
    for src, tgt, loc, want in GLUE_FLAGGED:
        r = hold(src, tgt, loc, source_locale=US)
        if "words joined at a formatting change" not in r or want not in r:
            _fail(f"[glue] {tgt!r} ({loc}) must be held naming {want!r}, got {r!r}")
    for src, tgt, loc in GLUE_SILENT:
        r = hold(src, tgt, loc, source_locale=US)
        if r:
            _fail(f"[glue] must stay silent: {tgt!r} ({loc}): {r!r}")
    if hold("Due on 10/11/2026.", "   ", "de-DE", source_locale=US):
        _fail("[comparator] an empty target abstains")
    n_flag = len(DATES_FLAGGED) + len(BREAKS_FLAGGED) + len(GLUE_FLAGGED)
    n_silent = len(DATES_SILENT) + len(BREAKS_SILENT) + len(GLUE_SILENT)
    print(f"  [comparator] {n_flag} held, {n_silent} silent OK")


def _segment(session, source, locale="de-DE", fmt="pptx"):
    p = Project(name="RH", source_locale=US, target_locales=[locale], default_provider="mock")
    session.add(p)
    session.commit()
    session.refresh(p)
    d = Document(project_id=p.id, filename="cells." + fmt, fmt=fmt, mode="segmented")
    session.add(d)
    session.commit()
    session.refresh(d)
    seg = Segment(
        document_id=d.id,
        target_locale=locale,
        order_index=0,
        source_text=source,
        status=SegStatus.NEW,
    )
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def _holds(seg):
    return [f for f in (seg.qa_flags or []) if f.get("type") == quality.REVIEW_HOLD_FLAG_TYPE]


def test_seam():
    with Session(engine) as session:
        seg = _segment(session, "Due on 10/11/2026.")
        out = quality.commit_mt_output(
            session, seg, "Fällig am 10.11.2026.", source="mt", lang_guard=True
        )
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible or not out.review_hold:
            _fail(f"[seam] swapped date must be held, TM-ineligible: {seg.status} {out!r}")
        if len(_holds(seg)) != 1 or "2026-11-10" not in _holds(seg)[0]["reason"]:
            _fail(f"[seam] want one review_hold flag naming the date: {seg.qa_flags!r}")
        out = quality.commit_mt_output(
            session, seg, "Fällig am 11.10.2026.", source="mt", lang_guard=True
        )
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.MT or _holds(seg) or not out.tm_eligible:
            _fail(f"[seam] the correct date must be clean MT: {seg.status} {seg.qa_flags!r}")
        # the offline mock path (no lang_guard) is never held
        out = quality.commit_mt_output(session, seg, "[de-DE] Due on 10/11/2026.", source="mt")
        session.commit()
        session.refresh(seg)
        if out.review_hold or _holds(seg):
            _fail(f"[seam] the mock echo must not be held: {seg.qa_flags!r}")

        cell = _segment(session, "Target URI")
        out = quality.commit_mt_output(
            session, cell, "Quelle:\nZiel-URI", source="mt", lang_guard=True
        )
        session.commit()
        session.refresh(cell)
        if cell.status != SegStatus.NEEDS_REVIEW or "line breaks differ" not in out.review_hold:
            _fail(f"[seam] added line break must be held: {cell.status} {cell.qa_flags!r}")
        if quality.clear_review_hold_flags(cell.qa_flags) and any(
            f.get("type") == quality.REVIEW_HOLD_FLAG_TYPE
            for f in quality.clear_review_hold_flags(cell.qa_flags)
        ):
            _fail("[seam] clear_review_hold_flags must drop the family")
        # the same wrapped text in an HTML document is not held (fmt resolved from the document)
        para = _segment(session, "This is a long\n   wrapped paragraph.", fmt="html")
        out = quality.commit_mt_output(
            session, para, "Dies ist ein langer umbrochener Absatz.", source="mt", lang_guard=True
        )
        if out.review_hold:
            _fail(f"[seam] an HTML paragraph's wrapping is not a line break: {out.review_hold!r}")
        # gate: approving the held segment is the human look; the badge must not outlive it
        from app.services import segment_review

        doc = session.get(Document, cell.document_id)
        proj = session.get(Project, doc.project_id)
        segment_review.approve_and_write_tm(session, cell, proj, doc, propagate=False)
        session.refresh(cell)
        if cell.status != SegStatus.APPROVED or _holds(cell):
            _fail(f"[seam] approve must clear the review hold: {cell.status} {cell.qa_flags!r}")
    print("  [seam] swapped date and added line break held (TM-ineligible); fixes clean OK")


def test_surfaces():
    from app.web import api_cli, api_cli_misc_1
    from app.web.documents import editor_qa_flags

    flag = quality.review_hold_flag("line breaks differ: the source has 0, the target has 1")
    badges = editor_qa_flags("Target URI", "Quelle:\nZiel-URI", [flag], "de-DE")
    mine = [b for b in badges if b.get("type") == quality.REVIEW_HOLD_FLAG_TYPE]
    if len(mine) != 1 or mine[0].get("label") != "check":
        _fail(f"[surfaces] the editor must show one 'check' badge: {badges!r}")
    if quality.REVIEW_HOLD_FLAG_TYPE not in api_cli_misc_1._SEGMENT_SURFACED:
        _fail("[surfaces] the headless detail must surface review_hold")
    line = api_cli.review_summary_line(
        [{"status": "needs_review", "qa_flags": [flag], "target_present": True}]
    )
    if not line or "review-hold 1" not in line:
        _fail(f"[surfaces] the summary line must name review-hold: {line!r}")
    print("  [surfaces] editor badge, headless detail and summary line OK")


def test_linear():
    hold = quality.review_hold_violation
    t0 = time.perf_counter()
    for src, tgt in (
        ("1/1/" * 50000, "1.1." * 50000),
        ("10/11/2026 " * 15000, "10.11.2026 " * 15000),
        ("2026/1" * 30000, "2026-1-" * 30000),
        ("October 1" * 20000, "1/2/" * 40000),
        ("\n" * 90000 + "x", "x" + "\r" * 90000),
        ("<br" * 50000, "<br " * 40000 + ">"),
        ("\\" * 100000, "\\n" * 50000),
        (f"a{S1} b " * 8000, f"Sie{S1}das " * 8000),
        (f"x{S1} y", "a" * 99000 + S1 + "b" * 900),
        ("⟦" * 100000, "⟦/r1" * 25000),
    ):
        r = hold(src, tgt, "de-DE", source_locale=US)
        if len(r) > 2000:
            _fail(f"[linear] reason must stay short, got {len(r)} chars")
    took = time.perf_counter() - t0
    if took > 3.0:
        _fail(f"[linear] hostile shapes took {took:.1f} s")
    print(f"  [linear] hostile shapes stay fast ({took:.2f} s) OK")


def main():
    init_db()
    test_comparator()
    test_seam()
    test_surfaces()
    test_linear()
    print("REVIEW HOLDS QA OK")


if __name__ == "__main__":
    main()
