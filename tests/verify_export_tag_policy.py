"""BUG-04 - the export tag-damage policy must be consulted on EVERY document.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider,
no network). The bug, measured on production (corpus-data/qa-campaign/CAMPAIGN.md):

    doc 22 (fr-FR) segment 181  qa_flags: tag_integrity MAJOR "inline tag added
    [/g1], [g1]"   status: needs_review
    `doc export 22 --locale fr-FR`  ->  "Cliquez sur [g1]Enregistrer[/g1] pour
    continuer."   shipped in the delivered .md

    doc 23 (es-ES) segment 196  ->  "Se subieron  [r1]%s de [/r1]%d archivos;"

Default `export_tag_damage_policy` is `source`, so both segments should have exported
as their ENGLISH SOURCE. They did not, because `export.paragraph_texts_for` gated the
whole SEGMENTED guard behind a DOCUMENT-level `doc_has_tokens` precondition -- true only
when the SOURCE already carried an inline-formatting token. Only docx/html/xliff sources
ever do, so on markdown/text/po/srt/json/xlsx the policy was never consulted at all, and
the one damage class that leaks INTERNAL NOTATION to a customer -- a model INVENTING
mask tokens on a tagless source -- was the exact case the precondition suppressed.

Groups:
  1. the two production repros, end to end on real .md bytes: source substituted,
     counted, clean units untouched, not one bracket in the deliverable.
  2. FAIL-PRE-FIX: the pinned pre-fix module ships the damaged text and reports nothing.
  3. policy is honoured: source (default) / quarantine / ship.
  4. the DURABLE-flag rung: a structurally clean but tag_integrity-flagged segment in
     needs_review is substituted; the same flag on a reviewed segment is NOT.
  5. no-regression: a clean tagless document is byte-identical under every policy and
     byte-identical to the pre-fix module.
  6. FULL_TEXT guard regression: tagged desync still quarantines globally; a TAGLESS
     parts<units desync now REFUSES loud (honest-failure, 2026-08-13).
  7. bundle honesty: the substituted document ships (it is not quarantined) and says so
     -- `_SUBSTITUTED.txt` + the report records.
  8. surface parity: web + CLI, per-document and per-project, report the same fact.
  9. the rung-2 STALENESS FLOOR: a tag_integrity flag that names a STRUCTURAL break on
     text rung 1 has just re-derived as clean is describing a PREVIOUS version of the
     segment, and honouring it would replace a correct translation with English.

Final line: "EXPORT TAG POLICY OK".
"""

import io
import json
import os
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

_TMP = init_env("loctms_exporttag_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    TargetLocale,
    TransMode,
)
from app.services import delivery, export, quality, segment, settings
from app.services.ingest import ingest_document

init_db()

# main this work was cut from -- the export seam there is the PRE-FIX code, so the
# fail-pre-fix proof stays valid after this branch merges (a bare HEAD would silently
# become the FIXED file and the proof would invert).
_BASE_COMMIT = "a95a402"

G1, G1C = segment.ph_open("g", 1), segment.ph_close("g", 1)
R1, R1C = segment.ph_open("r", 1), segment.ph_close("r", 1)
X1, X2 = segment.ph_point("x", 1), segment.ph_point("x", 2)

# The two production sources, verbatim in shape: plain markdown prose, NO tags anywhere.
MD = "# Getting started with Acme Cloud\n\nClick Save to continue.\n\nUploaded %s of %s files.\n"
MD_BYTES = (MD % ("5", "10")).encode()

# ...and the two delivered translations that shipped internal notation to a customer.
FR_INVENTED = "Cliquez sur " + G1 + "Enregistrer" + G1C + " pour continuer."
ES_INVENTED = "Se subieron " + R1 + "5 de " + R1C + "10 archivos."

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# --- fixtures ---------------------------------------------------------------
def _mk_project(s, locale="fr-FR"):
    p = Project(name="exptag-%d" % _seq(), org_id=1, source_locale="en-US", default_provider="mock")  # noqa: UP031
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale=locale))
    s.commit()
    return p


def _mk_md_doc(
    s, p, targets, *, name="guide.md", data=MD_BYTES, locale="fr-FR", flags=None, statuses=None
):
    """A REAL ingested markdown document (skeleton, units, upload blob, segments) with
    `targets` written onto its segments in order. `None` leaves a segment untranslated."""
    d = ingest_document(s, p, name, data, TransMode.SEGMENTED)
    s.commit()
    s.refresh(d)
    segs = s.exec(
        select(Segment)
        .where(Segment.document_id == d.id, Segment.target_locale == locale)
        .order_by(Segment.order_index)
    ).all()
    assert len(segs) == len(targets), (len(segs), len(targets))
    for i, (seg, tgt) in enumerate(zip(segs, targets, strict=False)):
        if tgt is None:
            continue
        seg.target_text = tgt
        seg.status = (statuses or {}).get(i, SegStatus.MT)
        if (flags or {}).get(i):
            seg.qa_flags = quality.merge_tag_flags(seg.qa_flags, (flags or {})[i])
        s.add(seg)
    s.commit()
    s.refresh(d)
    return d


def _prod_fixture(s, invented=FR_INVENTED, locale="fr-FR"):
    """The BUG-04 repro exactly as production produced it: the tag-damaged unit carries
    the tag_integrity MAJOR flag AND needs_review, its neighbours are clean MT."""
    p = _mk_project(s, locale)
    return _mk_md_doc(
        s,
        p,
        ["Prise en main d'Acme Cloud", invented, "5 fichiers sur 10 ont ete televerses."],
        locale=locale,
        flags={1: "added " + G1C + ", " + G1},
        statuses={1: SegStatus.NEEDS_REVIEW},
    )


def _prefix_module():
    """`app/services/export.py` AS OF `_BASE_COMMIT`, importable under its own name.

    File-copy, never `git stash` (the stash stack is shared across worktrees)."""
    try:
        out = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:app/services/export.py"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0 or "def paragraph_texts_for" not in out.stdout:
            return None
    except Exception:
        return None
    import importlib.util

    path = os.path.join(_TMP, "export_prefix.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out.stdout)
    spec = importlib.util.spec_from_file_location("export_prefix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_cli(argv):
    from app.cli.__main__ import main as _cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = _cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _policy(value):
    settings.set_many({"export_tag_damage_policy": value})


# ===========================================================================
# 1. the two production repros, end to end
# ===========================================================================
def test_production_repros():
    for invented, locale, token in ((FR_INVENTED, "fr-FR", G1), (ES_INVENTED, "es-ES", R1)):
        _policy("source")
        with Session(engine) as s:
            d = _prod_fixture(s, invented, locale)
            rep = []
            data = export.build_translated_file(s, d, locale, report=rep)
        text = data.decode()
        assert "⟦" not in text and "⟧" not in text, (
            "internal mask notation reached the deliverable",
            text,
        )
        assert token not in text, text
        # the damaged unit exported as its ENGLISH SOURCE (policy `source`)...
        assert "Click Save to continue." in text, text
        # ...and ONLY that unit: the clean neighbours keep their translations, and the
        # markdown structure (the heading prefix) survives untouched.
        assert "# Prise en main d'Acme Cloud" in text, text
        assert "5 fichiers sur 10 ont ete televerses." in text, text
        # counted, and named as a SEGMENT order_index, on the seam's own channel
        assert rep == [{"unit": "segment", "index": 1, "kind": export.SUB_AUTOMATIC}], rep
    print("  1. production repro (both cases): invented mask tokens NEVER reach the .md;")
    print("     the damaged unit exports as SOURCE, is counted, neighbours untouched OK")


# ===========================================================================
# 2. FAIL-PRE-FIX
# ===========================================================================
def test_fail_pre_fix():
    mod = _prefix_module()
    if mod is None:
        print("  2. (pre-fix module unavailable - the behavioural delta above stands) SKIP")
        return
    _policy("source")
    with Session(engine) as s:
        d = _prod_fixture(s)
        rep_old = []
        old = mod.paragraph_texts_for(s, d, "fr-FR", report=rep_old)
        rep_new = []
        new = export.paragraph_texts_for(s, d, "fr-FR", report=rep_new)
    joined_old = " ".join(old.values())
    assert G1 in joined_old, ("pre-fix export must SHIP the damaged text (the bug)", joined_old)
    assert rep_old == [], ("pre-fix export reported nothing either", rep_old)
    assert G1 not in " ".join(new.values()) and len(rep_new) == 1
    print(f"  2. fail-pre-fix: export.py@{_BASE_COMMIT} ships the invented tokens and reports [];")
    print("     the fixed seam substitutes the source and reports 1 OK")


# ===========================================================================
# 3. the policy is honoured on a TAGLESS-source document
# ===========================================================================
def test_policy_respected():
    with Session(engine) as s:
        d = _prod_fixture(s)
        d_id = d.id

        _policy("ship")
        joined = " ".join(export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR").values())
        assert G1 in joined, ("`ship` is the explicit opt-out and must ship as-is", joined)

        _policy("quarantine")
        try:
            export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR")
            raise AssertionError("`quarantine` must refuse the whole export")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "segments" and exc.doc_id == d_id, (exc.kind, exc.doc_id)
            assert "order_index 1" in str(exc), str(exc)
            assert str(exc).isascii(), ("CLI-bound message must be ASCII", str(exc))

        _policy("source")
        rep = []
        joined = " ".join(
            export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR", report=rep).values()
        )
        assert G1 not in joined and len(rep) == 1

        # An unreadable/junk policy row must not fail OPEN into `ship`.
        _policy("  SOURCE  ")
        rep = []
        export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR", report=rep)
        assert len(rep) == 1, rep
        _policy("source")
    print("  3. policy honoured on a tagless-source doc: source substitutes /")
    print("     quarantine refuses (kind=segments, ASCII) / ship opts out OK")


# ===========================================================================
# 4. the DURABLE tag_integrity flag rung
# ===========================================================================
def test_durable_flag_rung():
    """`quality.commit_mt_output` can commit text that is structurally CLEAN and still
    flagged: a deterministic point-order restore is a BEST GUESS, so the tokens are all
    back, in order, and only the PLACEMENT is unconfirmed. Recomputing `tag_damage` at
    export cannot see that -- the durable flag is the only record of it."""
    _policy("source")
    src = "Press " + X1 + " then " + X2 + " to save."
    ok = "Appuyez sur " + X1 + " puis " + X2 + " pour enregistrer."
    assert quality.tag_damage(src, ok) == "", "fixture must be structurally CLEAN"
    with Session(engine) as s:
        p = _mk_project(s)
        d = ingest_document(s, p, "pt.md", (src + "\n").encode(), TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        seg = s.exec(select(Segment).where(Segment.document_id == d.id)).first()
        seg.target_text = ok
        seg.status = SegStatus.NEEDS_REVIEW
        seg.qa_flags = quality.merge_tag_flags(
            seg.qa_flags, "point-token order restored (best-guess placement - verify)"
        )
        s.add(seg)
        s.commit()
        seg_id, d_id = seg.id, d.id

        rep = []
        m = export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR", report=rep)
        assert " ".join(m.values()) == src, ("a flagged, unreviewed segment follows the policy", m)
        assert len(rep) == 1, rep

        # A REVIEWED segment is trusted: the human is the authority, and the editor save
        # re-derives or clears the flag from the text actually written. A stale flag must
        # never turn an approved translation back into English.
        seg = s.get(Segment, seg_id)
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
        rep = []
        m = export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR", report=rep)
        assert " ".join(m.values()) == ok and rep == [], (m, rep)

        # A needs_review segment flagged for something ELSE (glossary/refusal) is NOT a
        # tag substitution -- only tag_integrity gates the deliverable.
        seg = s.get(Segment, seg_id)
        seg.status = SegStatus.NEEDS_REVIEW
        seg.qa_flags = [{"type": "glossary", "severity": "major", "message": "x"}]
        s.add(seg)
        s.commit()
        rep = []
        m = export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR", report=rep)
        assert " ".join(m.values()) == ok and rep == [], (m, rep)
    print("  4. durable rung: tag_integrity + needs_review -> source; reviewed -> ships;")
    print("     a non-tag flag never substitutes OK")


# ===========================================================================
# 5. no-regression on clean documents
# ===========================================================================
def test_clean_documents_untouched():
    mod = _prefix_module()
    with Session(engine) as s:
        p = _mk_project(s)
        d = _mk_md_doc(
            s,
            p,
            [
                "Prise en main d'Acme Cloud",
                "Cliquez sur Enregistrer pour continuer.",
                "5 fichiers sur 10 ont ete televerses.",
            ],
            name="clean.md",
        )
        d_id = d.id
        base = None
        for pol in ("source", "quarantine", "ship"):
            _policy(pol)
            rep = []
            data = export.build_translated_file(s, s.get(Document, d_id), "fr-FR", report=rep)
            assert rep == [], (pol, rep)
            if base is None:
                base = data
            assert data == base, (
                "a clean deliverable must be byte-identical under every policy",
                pol,
            )
        _policy("source")
        assert "Cliquez sur Enregistrer" in base.decode()
        if mod is not None:
            assert mod.paragraph_texts_for(
                s, s.get(Document, d_id), "fr-FR"
            ) == export.paragraph_texts_for(s, s.get(Document, d_id), "fr-FR"), (
                "the fix must not change a clean document by one byte"
            )

        # ...and an UNTRANSLATED segment still falls back to source with no report.
        p2 = _mk_project(s)
        d2 = _mk_md_doc(s, p2, [None, None, None], name="new.md")
        rep = []
        out = export.build_translated_file(s, d2, "fr-FR", report=rep).decode()
        assert rep == [] and "Click Save to continue." in out, (rep, out)

        # ...and a NULL source_text (connector-created rows carry one) must SUBSTITUTE,
        # not crash: the guard now runs on formats where that row shape occurs, and a
        # None in `targets` would raise inside the paragraph join.
        p3 = _mk_project(s)
        d3 = Document(
            project_id=p3.id,
            filename="null.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"p_index": 0, "text": "Click Save."}], "seg_locale": "en-US"},
        )
        s.add(d3)
        s.commit()
        s.refresh(d3)
        s.add(
            Segment(
                document_id=d3.id,
                target_locale="fr-FR",
                order_index=0,
                source_text=None,
                target_text=FR_INVENTED,
            )
        )
        s.commit()
        rep = []
        m = export.paragraph_texts_for(s, d3, "fr-FR", report=rep)
        assert G1 not in "".join(m.values()) and len(rep) == 1, (m, rep)
        # ...and it substitutes the SKELETON's unit text, never "". An empty paragraph
        # reported as "substituted" is CONTENT DELETION dressed as a source fallback --
        # the policy promises the unit ships its SOURCE and is visibly untranslated.
        assert m["0"] == "Click Save.", (
            "a null-source substitution must fall back to the document's own unit text",
            m,
        )
    print("  5. clean/untranslated documents: byte-identical under every policy and")
    print("     byte-identical to the pre-fix seam, nothing reported OK")


# ===========================================================================
# 6. FULL_TEXT guard regression
# ===========================================================================
def test_full_text_guard_regression():
    """The FULL_TEXT tagged branch is deliberately untouched: there a per-segment source
    fallback would export the WHOLE document in English (the 1.6 quarantine is the
    proportionate answer). The TAGLESS branch's silent positional fallback was the
    honest-failure bug (2026-08-13) and now REFUSES a parts<units desync loud."""
    _policy("source")
    units = [{"p_index": 0, "text": R1 + "Title" + R1C}, {"p_index": 1, "text": R1 + "Body" + R1C}]
    with Session(engine) as s:
        p = _mk_project(s)
        d = Document(
            project_id=p.id,
            filename="ft.docx",
            fmt="docx",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": units, "seg_locale": "en-US"},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale="fr-FR",
                order_index=0,
                source_text=R1 + "Title" + R1C + "\n\n" + R1 + "Body" + R1C,
                target_text=R1 + "Titre" + R1C + " " + R1 + "Corps" + R1C,
            )
        )
        s.commit()
        try:
            export.paragraph_texts_for(s, d, "fr-FR")
            raise AssertionError("a tagged FULL_TEXT count desync must quarantine")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "count", exc.kind

        seg = s.exec(select(Segment).where(Segment.document_id == d.id)).first()
        seg.target_text = "Titre\n\n" + R1 + R1 + "Corps" + R1C
        s.add(seg)
        s.commit()
        try:
            export.paragraph_texts_for(s, d, "fr-FR")
            raise AssertionError("a tagged FULL_TEXT token desync must quarantine")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens", exc.kind

        # TAGLESS full-text parts<units: honest-failure (2026-08-13) — no longer a silent
        # source-fill; REFUSED loud (kind="untranslated") so export never serves ENGLISH
        # source under a target-locale filename. Full coverage: verify_ft_export_untranslated.py.
        p2 = _mk_project(s)
        d2 = Document(
            project_id=p2.id,
            filename="tl.docx",
            fmt="docx",
            mode=TransMode.FULL_TEXT,
            skeleton={
                "units": [
                    {"p_index": 0, "text": "Title here"},
                    {"p_index": 1, "text": "Body here"},
                ],
                "seg_locale": "en-US",
            },
        )
        s.add(d2)
        s.commit()
        s.refresh(d2)
        s.add(
            Segment(
                document_id=d2.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Title here\n\nBody here",
                target_text="Titre et corps ensemble",
            )
        )
        s.commit()
        try:
            export.paragraph_texts_for(s, d2, "fr-FR")
            raise AssertionError("a tagless parts<units desync must refuse, not source-fill")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "untranslated", exc.kind

        # A FULL_TEXT body whose model INVENTED a token still quarantines globally --
        # it must never take the per-segment source fallback (that would ship the whole
        # document untranslated).
        seg2 = s.exec(select(Segment).where(Segment.document_id == d2.id)).first()
        seg2.target_text = "Titre " + G1 + "ici" + G1C + "\n\nCorps ici"
        seg2.status = SegStatus.NEEDS_REVIEW
        seg2.qa_flags = quality.merge_tag_flags(seg2.qa_flags, "added " + G1)
        s.add(seg2)
        s.commit()
        rep = []
        m = export.paragraph_texts_for(s, d2, "fr-FR", report=rep)
        assert m["0"].startswith("Titre "), m
        assert "Title here" not in m["0"], ("FULL_TEXT must NOT per-segment-substitute", m)
    print("  6. FULL_TEXT: count/token desync still quarantine globally; a tagless")
    print("     parts<units desync now REFUSES loud; no per-segment substitution OK")


# ===========================================================================
# 7. bundle honesty
# ===========================================================================
def test_bundle_reports_substitutions():
    _policy("source")
    with Session(engine) as s:
        p = _mk_project(s)
        _mk_md_doc(
            s,
            p,
            ["Prise en main d'Acme Cloud", FR_INVENTED, "5 fichiers sur 10 ont ete televerses."],
            name="damaged.md",
            flags={1: "added " + G1},
            statuses={1: SegStatus.NEEDS_REVIEW},
        )
        _mk_md_doc(
            s,
            p,
            [
                "Prise en main d'Acme Cloud",
                "Cliquez sur Enregistrer pour continuer.",
                "5 fichiers sur 10 ont ete televerses.",
            ],
            name="clean.md",
        )
        pid = p.id
        rep = []
        data, _name, _ctype, skipped = delivery.export_project(s, pid, zip=True, report=rep)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert skipped == [], ("a substitution is NOT a quarantine - the document ships", skipped)
    assert any("damaged" in n for n in names) and any("clean" in n for n in names), names
    assert "_SUBSTITUTED.txt" in names, names
    assert G1 not in zf.read([n for n in names if "damaged" in n][0]).decode()  # noqa: RUF015
    assert len(rep) == 1 and rep[0]["count"] == 1, rep
    assert rep[0]["filename"] == "damaged.md" and rep[0]["locale"] == "fr-FR", rep
    assert "unit=segment" in rep[0]["detail"] and "index=1" in rep[0]["detail"], rep
    manifest = zf.read("_SUBSTITUTED.txt").decode()
    assert "damaged.md" in manifest and "UNTRANSLATED" in manifest, manifest
    assert "clean.md" not in manifest, manifest

    # A clean bundle carries NO manifest and reports nothing (byte-identical to before).
    with Session(engine) as s:
        p2 = _mk_project(s)
        _mk_md_doc(
            s,
            p2,
            [
                "Prise en main d'Acme Cloud",
                "Cliquez sur Enregistrer pour continuer.",
                "5 fichiers sur 10 ont ete televerses.",
            ],
            name="ok.md",
        )
        rep2 = []
        data2, _n, _c, _s2 = delivery.export_project(s, p2.id, zip=True, report=rep2)
    assert rep2 == [], rep2
    assert "_SUBSTITUTED.txt" not in zipfile.ZipFile(io.BytesIO(data2)).namelist()

    # ...and the single-file fast path reports through the same channel.
    with Session(engine) as s:
        p3 = _mk_project(s)
        _mk_md_doc(
            s,
            p3,
            ["Prise en main d'Acme Cloud", FR_INVENTED, "5 fichiers sur 10 ont ete televerses."],
            name="one.md",
            flags={1: "added " + G1},
            statuses={1: SegStatus.NEEDS_REVIEW},
        )
        rep3 = []
        data3, _n3, _c3, _s3 = delivery.export_project(
            s, p3.id, locales=["fr-FR"], zip=False, report=rep3
        )
    assert G1 not in data3.decode() and len(rep3) == 1 and rep3[0]["count"] == 1, rep3
    print("  7. bundle: the substituted doc SHIPS and is declared (_SUBSTITUTED.txt +")
    print("     per-document records); a clean bundle is unchanged OK")


# ===========================================================================
# 8. surface parity (web + CLI, document + project)
# ===========================================================================
def test_surface_parity():
    _policy("source")
    client = TestClient(app)
    with Session(engine) as s:
        d = _prod_fixture(s)
        doc_id, pid = d.id, d.project_id

    r = client.get(f"/documents/{doc_id}/export?locale=fr-FR")
    assert r.status_code == 200, r.status_code
    assert r.headers.get("X-Export-Substituted") == "1", dict(r.headers)
    detail = r.headers.get("X-Export-Substituted-Detail") or ""
    assert "unit=segment" in detail and "index=1" in detail, detail
    assert G1 not in r.text and "Click Save to continue." in r.text

    r = client.get(f"/projects/{pid}/export")
    assert r.status_code == 200, r.status_code
    assert r.headers.get("X-Export-Substituted") == "1", dict(r.headers)
    pdetail = r.headers.get("X-Export-Substituted-Detail") or ""
    assert pdetail.isascii() and ("doc %d" % doc_id) in pdetail, pdetail  # noqa: UP031

    out_doc = os.path.join(_TMP, "cli_doc_%d.md" % doc_id)  # noqa: UP031
    code, out, err = _run_cli(
        ["--json", "doc", "export", str(doc_id), "--locale", "fr-FR", "--out", out_doc, "--force"]
    )
    assert code == 0, (code, out, err)
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["substituted"] == [
        {"unit": "segment", "index": 1, "kind": export.SUB_AUTOMATIC}
    ], payload
    with open(out_doc, encoding="utf-8") as fh:
        body = fh.read()
    assert G1 not in body and "Click Save to continue." in body

    out_zip = os.path.join(_TMP, "cli_proj_%d.zip" % pid)  # noqa: UP031
    code, out, err = _run_cli(
        ["--json", "project", "export", str(pid), "--out", out_zip, "--force"]
    )
    assert code == 0, (code, out, err)
    payload = json.loads(out.strip().splitlines()[-1])
    assert len(payload["substituted"]) == 1, payload
    assert payload["substituted"][0]["count"] == 1, payload
    assert payload["substituted"][0]["filename"] == "guide.md", payload

    # the human arm says it too (a --json-only report is invisible to an operator)
    code, out, err = _run_cli(["project", "export", str(pid), "--out", out_zip, "--force"])
    assert code == 0, (code, out, err)
    assert "WARNING" in out and "UNTRANSLATED" in out, out
    # The AUDIT row is the fourth honesty surface and the only DURABLE one -- headers and
    # stdout are gone the moment the operator closes the terminal. Both writers (web route
    # + CLI) must name the substitution in `detail`, and a CLEAN export's row must stay
    # byte-identical to pre-#41 (no trailing "substituted=0").
    from app.models import AuditLog

    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(AuditLog.action == "project.export").order_by(AuditLog.id)
        ).all()
        details = [r.detail for r in rows]
    assert any("substituted=1 docs=1" in d for d in details), details
    assert all("substituted=0" not in d for d in details), details

    # The project header's length cap must DECLARE ITSELF. A silent slice can cut a
    # record mid-token, leaving a short, well-formed-LOOKING list that hides whole
    # documents -- the silent-omission failure this channel exists to close.
    with Session(engine) as s:
        big = _mk_project(s)
        for _ in range(18):
            _mk_md_doc(
                s,
                big,
                [
                    "Prise en main d'Acme Cloud",
                    FR_INVENTED,
                    "5 fichiers sur 10 ont ete televerses.",
                ],
                name="big-%d.md" % _seq(),  # noqa: UP031
                flags={1: "added " + G1},
                statuses={1: SegStatus.NEEDS_REVIEW},
            )
        big_id = big.id
    r = client.get(f"/projects/{big_id}/export")
    assert r.status_code == 200, r.status_code
    assert r.headers.get("X-Export-Substituted") == "18", dict(r.headers)
    big_detail = r.headers.get("X-Export-Substituted-Detail") or ""
    assert big_detail.isascii() and len(big_detail) <= 950, (len(big_detail), big_detail)
    assert "TRUNCATED docs=18" in big_detail, ("a capped header must say so", big_detail)
    print("  8. parity: web doc + web project headers, CLI --json + CLI human output")
    print("     all name the SAME substitution; a capped header declares its cap OK")


# ===========================================================================
# 9. rung-2 STALENESS FLOOR (adversarial review of this fix)
# ===========================================================================
def _docx_shaped(s, target, *, flag=None, status=None, src=None):
    """A SEGMENTED, TOKEN-BEARING document (the docx/xliff/html shape) with one segment."""
    p = _mk_project(s)
    src = src if src is not None else "Click " + R1 + "Save" + R1C + " to continue."
    d = Document(
        project_id=p.id,
        filename="stale-%d.docx" % _seq(),  # noqa: UP031
        fmt="docx",
        mode=TransMode.SEGMENTED,
        skeleton={"units": [{"p_index": 0, "text": src}], "seg_locale": "en-US"},
    )
    s.add(d)
    s.commit()
    s.refresh(d)
    seg = Segment(
        document_id=d.id,
        target_locale="fr-FR",
        order_index=0,
        source_text=src,
        target_text=target,
        status=status or SegStatus.MT,
    )
    if flag:
        seg.qa_flags = quality.merge_tag_flags(None, flag)
    s.add(seg)
    s.commit()
    s.refresh(d)
    return d, src


def test_rung2_staleness_floor():
    """`quality.commit_mt_output` merges a tag_integrity flag on damage and NEVER clears
    it on a clean commit. So a RE-TRANSLATION that lands perfect tokens and then trips any
    OTHER soft gate (QE fail / DNT / glossary / number / nontranslation / LQA fix-revert)
    re-asserts NEEDS_REVIEW with the OLD tag flag still in qa_flags. Rung 2 must not
    substitute ENGLISH SOURCE over that correct translation -- the FALSE-SUBSTITUTION
    direction, and a regression against pre-BUG-04 behaviour (rung 1 alone shipped it).

    Reproduced pre-fix on BOTH shapes: a tagless .md segment and a token-bearing .docx
    segment. Rung 2 is admissible only for a flag CLAIMING UNCONFIRMED PLACEMENT, on a
    segment that actually carries tokens."""
    _policy("source")
    good_fr = "Cliquez sur Enregistrer pour continuer."
    with Session(engine) as s:
        # (a) TAGLESS .md, clean text, STALE STRUCTURAL flag + a QE fail -> must SHIP.
        p = _mk_project(s)
        d = _mk_md_doc(
            s,
            p,
            ["Prise en main d'Acme Cloud", good_fr, "5 fichiers sur 10 ont ete televerses."],
            name="stale.md",
            flags={1: "inline tag added " + G1},  # a PREVIOUS version's break
            statuses={1: SegStatus.NEEDS_REVIEW},
        )
        seg = s.exec(
            select(Segment).where(Segment.document_id == d.id, Segment.order_index == 1)
        ).first()
        assert quality.tag_damage(seg.source_text, seg.target_text) == "", "clean now"
        seg.qa_flags = list(seg.qa_flags or []) + [  # noqa: RUF005
            {"type": "qe", "severity": "major", "message": "score 70 below threshold"}
        ]
        s.add(seg)
        s.commit()
        rep = []
        m = export.paragraph_texts_for(s, s.get(Document, d.id), "fr-FR", report=rep)
        joined = " ".join(m.values())
        assert good_fr in joined and "Click Save to continue." not in joined, (
            "a stale STRUCTURAL flag must never substitute clean text",
            m,
        )
        assert rep == [], rep

        # (b) the same class on the TOKEN-BEARING (docx) shape rung 1 always covered.
        d2, src2 = _docx_shaped(
            s,
            "Cliquez sur " + R1 + "Enregistrer" + R1C + " pour continuer.",
            flag="inline tag dropped " + R1C,
            status=SegStatus.NEEDS_REVIEW,
        )
        rep = []
        m = export.paragraph_texts_for(s, d2, "fr-FR", report=rep)
        assert m["0"] != src2 and rep == [], ("stale structural flag, docx shape", m, rep)

        # (c) ...and the LIVE verdict rung 1 cannot re-derive still holds on that shape.
        d3, src3 = _docx_shaped(
            s,
            "Cliquez sur " + R1 + "Enregistrer" + R1C + " pour continuer.",
            flag="AI tag repair applied, placement unconfirmed (moved span)",
            status=SegStatus.NEEDS_REVIEW,
        )
        rep = []
        m = export.paragraph_texts_for(s, d3, "fr-FR", report=rep)
        assert m["0"] == src3 and len(rep) == 1, ("a PLACEMENT flag is rung-2 admissible", m, rep)

        # (d) a placement-worded flag on a TAGLESS segment has no placement to be
        #     unconfirmed about -- the structural floor holds even past the reason check.
        p4 = _mk_project(s)
        d4 = _mk_md_doc(
            s,
            p4,
            ["Prise en main d'Acme Cloud", good_fr, "5 fichiers sur 10 ont ete televerses."],
            name="tagless-placement.md",
            flags={1: "point-token order restored (best-guess placement - verify)"},
            statuses={1: SegStatus.NEEDS_REVIEW},
        )
        rep = []
        m = export.paragraph_texts_for(s, d4, "fr-FR", report=rep)
        assert good_fr in " ".join(m.values()) and rep == [], (m, rep)
    print("  9. rung-2 staleness floor: a stale STRUCTURAL flag never substitutes clean")
    print("     text (md + docx shapes); a PLACEMENT flag on a tagged segment still does")


def main():
    test_production_repros()
    test_fail_pre_fix()
    test_policy_respected()
    test_durable_flag_rung()
    test_clean_documents_untouched()
    test_full_text_guard_regression()
    test_bundle_reports_substitutions()
    test_surface_parity()
    test_rung2_staleness_floor()
    print("EXPORT TAG POLICY OK")


if __name__ == "__main__":
    main()
