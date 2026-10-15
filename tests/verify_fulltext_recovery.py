"""FULL_TEXT tag-damage RECOVERY path (GitHub #42) - suite for item C.

Rebuilt on `main` WITHOUT the `advisory` classification (five prior review rounds died
refining a loss-free predicate against docx-filter behaviour the classifier cannot
observe). CONSERVATIVE QUARANTINE SEMANTICS ONLY: any tag divergence is damage,
identical to `quality.tag_damage`. A shape-preserving reorder now counts as damage -
shown red, offered a revert, quarantined by export guard B. The recovery surface is the
mitigation.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network,
MockProvider forced). No provider call is made anywhere in this suite.

The real target is doc 44 (`AcmeGamesMarketing.docx`, `mode=full_text`, `es-419`):
6 dropped paired tokens, all in paragraph 0 of 5. Guard A (paragraph COUNT) passes,
guard B (per-paragraph token set) fires, so `export.build_translated_file` raises
`TagExportQuarantine` forever. The document is BRICKED and the product offers no route
out. That is #42.

What this suite pins:
  1. C-0 STRUCTURED DIAGNOSIS. `TagExportQuarantine` carries `doc_id`/`locale`/`kind`/
     `paragraphs`, guard B scans EVERY paragraph before raising, `str(exc)` stays
     byte-compatible, and the FULL_TEXT `has_tokens` predicate is SEGMENT-keyed (fail-
     CLOSED, not skeleton-keyed / fail-open).
  2. C-1 THE SEAM. `fulltext_paragraphs.split_view` is the one place the per-paragraph
     view is computed; editor, CLI and /api are thin callers.
  3. RECOVERY AT THE BOUNDARY - the load-bearing test. The four non-laundering
     invariants (NEEDS_REVIEW / a durable `source_fallback` qa_flag / an enforced TM
     refusal / an audit row) are asserted on the COMMITTED row re-fetched from a FRESH
     Session. Removing ANY ONE invariant fails a distinct assertion here.
  4. A hand-repaired paragraph CLEARS its stale flags.
  5. web+CLI+api PARITY, field by field.
  6. C-3's per-export override and its `export_source_fallback_max_share` CEILING, plus
     guard A's EXCLUSIVE claim on the global quarantine.
  7. Byte-identity / no-op: SEGMENTED untouched, clean FULL_TEXT untouched, a TAGLESS
     reflowed FULL_TEXT keeps main's graceful positional fallback.
  8. THE BOUNDARY PROBE. Both FULL_TEXT surfaces call `quality.tag_damage` per
     paragraph, not a token-multiset comparison that merely LOOKS equivalent - an
     EMPTIED paired span is caught. And a REORDER is now damage (advisory dropped).
  9. The substitution record names its UNIT and its KIND, and ONE seam classification
     decides how a refusal is reported on web, /api and CLI.
 10. HIGH-1: the revert route enforces the same ceiling the export door does, the
     durable fallback is reported by every later export, and `--all-damaged` is ATOMIC
     (the branch's F-3 bug: a mid-batch ceiling refusal must leave no partial writes).
 11. MEDIUM-1: copy-source + save cannot launder the fallback flag.
 12. MEDIUM-2: the recovery verbs travel over --remote.

Final line: "FULLTEXT RECOVERY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
# Defense in depth: never die on a non-ASCII glyph under a cp1252 pipe. The prints
# below are ALSO written pure-ASCII; this belt covers assertion messages that embed
# the real ⟦⟧ tokens on a failure.
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_ftpara_")

import json as _json
from io import BytesIO

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    WorkflowRun,
)
from app.services import export, orchestrator, quality, segment, settings, tm
from app.services import workflows as wf_svc
from app.services.ingest import ingest_document
from tests._cli_runner import run_cli

init_db()

LOC = "es-419"
_SEQ = [0]

# The doc-44 shape: paragraph 0 carries three run pairs, four plain paragraphs follow.
P0_RUNS = [("The ", "bold"), ("Acme Pro", "italic"), (" connector ships today.", "underline")]
BODY = [
    "Our platform helps studios launch their games worldwide.",
    "Localization is handled by a team of native speaking experts.",
    "Every release ships with reviewed and approved translations.",
    "Contact our sales team to schedule a short product demo now.",
]
# The fluent translation with ALL SIX tokens stripped out of paragraph 0 (what a fix
# step actually did to doc 44) - paragraph COUNT preserved, so guard A passes.
T0_STRIPPED = "El conector Acme Pro se lanza hoy."
TBODY = [
    "Nuestra plataforma ayuda a los estudios a lanzar sus juegos.",
    "La localizacion la maneja un equipo de hablantes nativos.",
    "Cada version se entrega con traducciones revisadas y aprobadas.",
    "Contacte a nuestro equipo de ventas para agendar una demo.",
]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _build_docx(p0_runs, plain_paras):
    d = Docx()
    p = d.add_paragraph()
    for text, fmt in p0_runs:
        r = p.add_run(text)
        setattr(r, fmt, True)
    for t in plain_paras:
        d.add_paragraph(t)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _docx_text(data: bytes) -> str:
    return "\n".join(p.text for p in Docx(BytesIO(data)).paragraphs)


def _formatted_run_count(data: bytes, p_index: int) -> int:
    """Runs carrying BOTH text and an rPr in body paragraph `p_index`.

    Counting rPr-bearing runs alone would not be the invariant: a blanked run keeps its
    rPr element, so the count would read '3' while the formatting was gone."""
    from docx.oxml.ns import qn as _qn

    para = Docx(BytesIO(data)).paragraphs[p_index]
    return sum(1 for r in para.runs if r.text and r._r.find(_qn("w:rPr")) is not None)


def _fixture(p0_runs=None, plain=None, target_paras=None, locale=LOC):
    """A real INGESTED FULL_TEXT DOCX with a doc-44-shaped damaged paragraph 0.

    Returns {project_id, doc_id, seg_id, units, source_text}."""
    p0_runs = p0_runs or P0_RUNS
    plain = BODY if plain is None else plain
    with Session(engine) as s:
        p = Project(
            name="ftpara-%d" % _seq(),  # noqa: UP031
            org_id=1,
            source_locale="en-US",
            default_provider="mock",  # noqa: RUF100, UP031
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=locale))
        s.commit()
        doc = ingest_document(
            s, p, "AcmeGamesMarketing.docx", _build_docx(p0_runs, plain), TransMode.FULL_TEXT
        )
        s.commit()
        s.refresh(doc)
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == locale)
        ).one()
        units = list((doc.skeleton or {})["units"])
        if target_paras is not None:
            seg.target_text = "\n\n".join(target_paras)
            seg.status = SegStatus.MT
            s.add(seg)
            s.commit()
        return {
            "project_id": p.id,
            "doc_id": doc.id,
            "seg_id": seg.id,
            "units": units,
            "source_text": seg.source_text,
        }


def _damaged_fixture(locale=LOC):
    return _fixture(target_paras=[T0_STRIPPED] + TBODY, locale=locale)  # noqa: RUF005


def _docs_key():
    """A docs-scoped lsu_ key (the /api surface refuses anything else). Minted through
    the service; does NOT flip the login wall, so the rest of the suite is unaffected."""
    from app.services import apikeys, security

    u = security.get_by_email("ftpara@example.com")
    if u is None:
        u = security.create_user("ftpara@example.com", "FT Para", "admin", "Pw!12345xyz-ftpara")
    _row, raw = apikeys.create_key(u.id, "ftpara-%d" % _seq(), scopes="api docs")  # noqa: UP031
    return {"X-Loc-Token": raw}


SIX_TOKENS = ["⟦r1⟧", "⟦/r1⟧", "⟦r2⟧", "⟦/r2⟧", "⟦r3⟧", "⟦/r3⟧"]


# ===========================================================================
# 1. C-0 - the structured diagnosis.
# ===========================================================================
def test_structured_quarantine():
    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("a guard-B token-set desync must quarantine")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens", exc.kind
            assert exc.doc_id == d.id and exc.locale == LOC, (exc.doc_id, exc.locale)
            assert [p["index"] for p in exc.paragraphs] == [0], exc.paragraphs
            assert sorted(exc.paragraphs[0]["missing"]) == sorted(SIX_TOKENS), exc.paragraphs[0][
                "missing"
            ]
            assert exc.paragraphs[0]["extra"] == [], exc.paragraphs[0]["extra"]
            assert exc.paragraphs[0]["p_index"] == 0
            assert T0_STRIPPED in exc.paragraphs[0]["target_text"]
            assert "Acme Pro" in exc.paragraphs[0]["source_text"]
            # BYTE-COMPAT: all five handlers do str(exc) ONLY.
            assert str(exc).startswith("FULL_TEXT paragraph 0 inline-tag set desync"), str(exc)
    print("  1a. TagExportQuarantine carries kind/doc_id/locale/paragraphs with the")
    print("      per-paragraph missing/extra delta; str(exc) prefix unchanged OK")

    # EVERY damaged paragraph is collected, not just the first.
    fx2 = _fixture(
        target_paras=[T0_STRIPPED, TBODY[0], TBODY[1], TBODY[2], TBODY[3]],
        plain=[BODY[0], BODY[1], BODY[2], "Tail run pair ⟦r1⟧here⟦/r1⟧ ends it."],
    )
    with Session(engine) as s:
        d = s.get(Document, fx2["doc_id"])
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("must quarantine")
        except export.TagExportQuarantine as exc:
            assert [p["index"] for p in exc.paragraphs] == [0, 4], exc.paragraphs
            assert "2 paragraph" in str(exc), str(exc)
    print("  1b. guard B scans EVERY paragraph and raises ONCE (pre-fix: first only) OK")

    # The FULL_TEXT `has_tokens` predicate must be SEGMENT-keyed like its SEGMENTED
    # sibling. Skeleton-keying is fail-OPEN.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        segs = s.exec(select(Segment).where(Segment.document_id == d.id)).all()
        by_seg = any(segment.find_placeholders(x.source_text or "") for x in segs)
        by_skel = any(
            segment.find_placeholders(u["text"]) for u in (d.skeleton or {}).get("units", [])
        )
        assert by_seg is True and by_skel is True, (by_seg, by_skel)
        # A doc whose SKELETON was lost still gets the guard (fail-CLOSED).
        d.skeleton = {"units": []}
        s.add(d)
        s.commit()
        assert any(segment.find_placeholders(x.source_text or "") for x in segs) is True
        assert (
            any(segment.find_placeholders(u["text"]) for u in (d.skeleton or {}).get("units", []))
            is False
        )
        try:
            export.paragraph_texts_for(s, d, LOC)
            raise AssertionError("a thin skeleton must NOT skip the guard (fail-open)")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "count", exc.kind
    print("  1c. FULL_TEXT has_tokens is SEGMENT-keyed: equivalent on a real doc, and")
    print("      fail-CLOSED when the skeleton is lost OK")


# ===========================================================================
# 2. C-1 - the shared seam.
# ===========================================================================
def test_split_view_seam():
    from app.services import fulltext_paragraphs as ftp

    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        view = ftp.split_view(s, d, LOC)
    assert view["ok"] is True, view
    assert len(view["rows"]) == 5, view["rows"]
    r0 = view["rows"][0]
    assert r0["damaged"] is True and r0["index"] == 0 and r0["p_index"] == 0
    assert sorted(r0["missing"]) == sorted(SIX_TOKENS), r0["missing"]
    assert r0["extra"] == []
    assert r0["source"] == fx["units"][0]["text"]
    assert r0["target"] == T0_STRIPPED
    # advisory classification is GONE: rows carry no advisory key, view no counts.
    assert "advisory" not in r0, r0
    assert "advisory_count" not in view and "advisory_indices" not in view, view
    assert [r["damaged"] for r in view["rows"][1:]] == [False] * 4, view["rows"]
    assert view["damaged_count"] == 1 and view["damaged_indices"] == [0]
    expect = len(fx["units"][0]["text"]) / len(fx["source_text"])
    assert 0.15 <= expect <= 0.30, ("fixture must be doc-44 shaped", expect)
    assert abs(view["damaged_share"] - expect) < 1e-9, view["damaged_share"]
    assert view["reverted_indices"] == [] and view["fallback_share"] == 0.0
    print("  2a. split_view: 5 rows, row 0 damaged with the six missing tokens, rows")
    print(f"      1-4 clean, damaged_share = {expect:.3f}, no advisory fields OK")

    # The count-desync (guard A) case has NO reliable per-paragraph view.
    fxc = _fixture(target_paras=[T0_STRIPPED] + TBODY[:3])  # 4 paragraphs, not 5  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fxc["doc_id"])
        v = ftp.split_view(s, d, LOC)
    assert {k: v[k] for k in ("ok", "reason", "parts", "units")} == {
        "ok": False,
        "reason": "count_desync",
        "parts": 4,
        "units": 5,
    }, v
    assert set(v) == {"ok", "reason", "parts", "units", "message"}, v
    assert "COUNT desync" in v["message"], v
    print("  2b. split_view refuses the count-desync case (guard A keeps its exclusive")
    print("      claim on the GLOBAL quarantine), and the refusal carries a message OK")

    # SEGMENTED is untouched by every part of C.
    with Session(engine) as s:
        p = Project(name="ftpara-seg-%d" % _seq(), org_id=1, source_locale="en-US")  # noqa: UP031
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        sd = ingest_document(s, p, "seg.docx", _build_docx(P0_RUNS, BODY), TransMode.SEGMENTED)
        s.commit()
        s.refresh(sd)
        v = ftp.split_view(s, sd, LOC)
    assert v["ok"] is False and v["reason"] == "not_full_text", v
    print("  2c. split_view refuses a SEGMENTED document OK")


# ===========================================================================
# 3. THE LOAD-BEARING TEST - the four non-laundering invariants on the COMMITTED row.
#    Removing ANY ONE of them fails a distinct assertion in this test.
# ===========================================================================
def test_recovery_at_the_boundary():
    from app.services import fulltext_paragraphs as ftp

    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        res = ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com", ip="127.0.0.1")
    assert res["ok"] is True, res
    assert res["indices"] == [0], res
    expect = len(fx["units"][0]["text"]) / len(fx["source_text"])
    assert abs(res["fallback_share"] - expect) < 1e-9, res

    # A FRESH session: laundering bugs are assertions off the boundary.
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert seg.target_text.split("\n\n")[0] == fx["units"][0]["text"], seg.target_text
        assert seg.target_text.split("\n\n")[1:] == TBODY, seg.target_text
        # INVARIANT 1: NEEDS_REVIEW.
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        # INVARIANT 2: a durable source_fallback qa_flag naming index 0.
        flags = [
            f for f in (seg.qa_flags or []) if f.get("type") == quality.SOURCE_FALLBACK_FLAG_TYPE
        ]
        assert len(flags) == 1, seg.qa_flags
        assert flags[0]["indices"] == [0], flags[0]
        assert flags[0]["severity"] == "major", flags[0]
        assert flags[0].get("message"), flags[0]
        # And the naive-splice symptom is GONE: the row is not silently clean.
        assert quality.tag_damage(seg.source_text, seg.target_text) == "", "tags whole"
    print("  3a. committed row (FRESH session): NEEDS_REVIEW + a durable source_fallback")
    print("      flag naming index 0 (naive splice: status=mt, no flag, TM-eligible) OK")

    # INVARIANT 3: the TM refusal.
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        d = s.get(Document, fx["doc_id"])
        assert (
            tm.write_back(s, d.project_id, "ft fallback src", "cible", "en-US", LOC, segment=seg)
            is None
        ), "write_back must refuse"
        assert (
            tm.ft_store(s, d.project_id, "ft fallback src", "cible", "en-US", LOC, segment=seg)
            is None
        ), "ft_store must refuse"

        # A segment with no source_fallback flag, and the pre-#42 call with no
        # `segment=` at all, are UNCHANGED (byte-identity).
        class _Clean:
            qa_flags = [{"type": "qe", "severity": "minor", "message": "x"}]  # noqa: RUF012

        assert (
            tm.write_back(s, d.project_id, "ft clean src", "cible", "en-US", LOC, segment=_Clean())
            is not None
        )
        assert tm.write_back(s, d.project_id, "ft nosegment src", "cible", "en-US", LOC) is not None
        assert tm.ft_store(s, d.project_id, "ft nosegment body", "cuerpo", "en-US", LOC) is not None
    print("  3b. tm.write_back / tm.ft_store REFUSE a source_fallback segment; a clean")
    print("      segment and the pre-#42 no-segment call are unchanged OK")

    # INVARIANT 4: the audit trail.
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "document.paragraph_revert")).all()
    hit = [r for r in rows if r.target == "doc %d %s p0" % (fx["doc_id"], LOC)]  # noqa: UP031
    assert hit, [r.target for r in rows]
    assert "share=" in hit[-1].detail, hit[-1].detail
    print("  3c. audit: document.paragraph_revert with the fallback share OK")

    # THE POINT: the document exports again, keeps its formatting, leaks no tokens.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        data = export.build_translated_file(s, d, LOC)
    assert len(data) > 0
    assert "⟦" not in _docx_text(data), "no token may leak into the deliverable"
    assert _formatted_run_count(data, 0) == 3, "paragraph 0 formatting must be restored"
    assert "Acme Pro" in _docx_text(data)
    assert TBODY[0] in _docx_text(data), "the other paragraphs stay TRANSLATED"
    print("  3d. export succeeds: 3 formatted runs restored in paragraph 0, no leaked")
    print("      tokens, paragraphs 1-4 still translated OK")

    # The /api view reports the fallback share of the RECOVERED document.
    H = _docs_key()
    client = TestClient(app)
    r = client.get("/api/documents/%d/paragraphs?locale=%s" % (fx["doc_id"], LOC), headers=H)  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text[:300])
    payload = r.json()
    assert payload["ok"] is True
    assert payload["reverted_indices"] == [0], payload
    assert abs(payload["fallback_share"] - expect) < 1e-9, payload
    assert len(payload["rows"]) == 5
    print(f"  3e. GET /api/documents/{{id}}/paragraphs carries fallback_share={expect:.3f} OK")


# ===========================================================================
# 4. A hand repair CLEARS the stale badges.
# ===========================================================================
def test_manual_repair_clears_flags():
    from app.services import fulltext_paragraphs as ftp

    fx = _damaged_fixture()
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        quality.commit_mt_output(s, seg, seg.target_text, source="test")
        s.commit()
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert any(f.get("type") == quality.TAG_FLAG_TYPE for f in (seg.qa_flags or [])), (
            seg.qa_flags
        )

    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")
    fixed = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        res = ftp.save_paragraph(s, d, LOC, 0, fixed, actor="tester@example.com")
    assert res["ok"] is True, res
    assert res["damaged_count"] == 0, res

    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        types = {f.get("type") for f in (seg.qa_flags or [])}
        assert quality.TAG_FLAG_TYPE not in types, seg.qa_flags
        assert quality.SOURCE_FALLBACK_FLAG_TYPE not in types, seg.qa_flags
        assert seg.target_text.split("\n\n")[0] == fixed, seg.target_text
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        data = export.build_translated_file(s, d, LOC)
    assert "⟦" not in _docx_text(data)
    assert "conector se lanza hoy" in _docx_text(data), "paragraph 0 fully TRANSLATED"
    assert _formatted_run_count(data, 0) == 3
    print("  4a. save_paragraph with a correctly re-tagged paragraph clears BOTH the")
    print("      tag_integrity and source_fallback flags and exports translated OK")

    # A save that RE-BREAKS the tags is refused without confirm, and flagged with it.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        bad = ftp.save_paragraph(s, d, LOC, 0, "sin etiquetas", actor="tester@example.com")
    assert bad["ok"] is False and bad["reason"] == "tag_damage", bad
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert seg.target_text.split("\n\n")[0] == fixed, "refused save must not write"
    print("  4b. a save that would RE-BREAK the tags is refused (needs_confirm) and")
    print("      writes nothing OK")

    # ...and with confirm=1 it lands HONESTLY: the write seam's tag_integrity flag +
    # NEEDS_REVIEW survive (downgrading to EDITED would hide a deliberate override).
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        forced = ftp.save_paragraph(
            s, d, LOC, 0, "sin etiquetas", actor="tester@example.com", confirm=True
        )
    assert forced["ok"] is True, forced
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert seg.target_text.split("\n\n")[0] == "sin etiquetas", seg.target_text
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert any(f.get("type") == quality.TAG_FLAG_TYPE for f in (seg.qa_flags or [])), (
            seg.qa_flags
        )
    print("  4c. a CONFIRMED tag-breaking save lands but keeps NEEDS_REVIEW + the")
    print("      tag_integrity flag (never silently downgraded to EDITED) OK")


# ===========================================================================
# 5. PARITY - CLI vs /api, field by field.
# ===========================================================================
def test_parity_cli_and_api():
    fx = _damaged_fixture()
    did = fx["doc_id"]
    H = _docs_key()
    client = TestClient(app)

    api_view = client.get("/api/documents/%d/paragraphs?locale=%s" % (did, LOC), headers=H).json()  # noqa: UP031
    code, out, err = run_cli(["--json", "doc", "paragraphs", "--doc", str(did), "--locale", LOC])
    assert code == 0, (code, out, err)
    cli_view = _json.loads(out.strip().splitlines()[-1])
    for k in (
        "damaged_count",
        "damaged_indices",
        "damaged_share",
        "reverted_indices",
        "fallback_share",
    ):
        assert cli_view[k] == api_view[k], (k, cli_view[k], api_view[k])
    assert cli_view["rows"] == api_view["rows"], "rows must match field for field"
    print("  5a. doc paragraphs --json == GET /api/documents/{id}/paragraphs OK")

    code, out, err = run_cli(
        ["--json", "doc", "revert-paragraph", "--doc", str(did), "--locale", LOC, "--index", "0"]
    )
    assert code == 0, (code, out, err)
    cli_rev = _json.loads(out.strip().splitlines()[-1])
    assert cli_rev["indices"] == [0], cli_rev

    fx2 = _damaged_fixture()
    r = client.post(
        "/api/documents/%d/paragraphs/0/revert?locale=%s" % (fx2["doc_id"], LOC),  # noqa: UP031
        headers=H,  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    api_rev = r.json()
    assert api_rev["indices"] == cli_rev["indices"], (api_rev, cli_rev)
    assert abs(api_rev["fallback_share"] - cli_rev["fallback_share"]) < 1e-9
    print("  5b. doc revert-paragraph --json == POST /api/.../paragraphs/0/revert OK")

    # set-paragraph, and the WEB twin of the view.
    fixed = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    code, out, err = run_cli(
        [
            "--json",
            "doc",
            "set-paragraph",
            "--doc",
            str(did),
            "--locale",
            LOC,
            "--index",
            "0",
            "--text",
            fixed,
        ]
    )
    assert code == 0, (code, out, err)
    cli_set = _json.loads(out.strip().splitlines()[-1])
    assert cli_set["damaged_count"] == 0, cli_set
    # The /api twin of the same write, on the doc the api reverted above.
    ra = client.post(
        "/api/documents/%d/paragraphs/0/save?locale=%s" % (fx2["doc_id"], LOC),  # noqa: UP031
        headers=H,
        json={"text": fixed},
    )
    assert ra.status_code == 200, (ra.status_code, ra.text[:300])
    assert ra.json()["damaged_count"] == cli_set["damaged_count"], ra.json()
    assert ra.json()["indices"] == cli_set["indices"], (ra.json(), cli_set)
    # ...and it is NOT the soft surface: an untagged save is refused, like the CLI's.
    rbad = client.post(
        "/api/documents/%d/paragraphs/0/save?locale=%s" % (fx2["doc_id"], LOC),  # noqa: UP031
        headers=H,
        json={"text": "sin etiquetas"},
    )
    assert rbad.status_code == 409, rbad.status_code
    assert rbad.json()["needs_confirm"] is True, rbad.json()
    w = client.get("/documents/%d/paragraphs?locale=%s" % (did, LOC))  # noqa: UP031
    assert w.status_code == 200, w.status_code
    # THE #42 COMPLAINT: the linguist must be able to SEE and COPY the literal token.
    src0 = fx["units"][0]["text"]
    assert f'id="src-0">{src0}</pre>' in w.text, (
        "the source cell must carry the LITERAL, copyable token text"
    )
    assert "tag-chip" in w.text, "the readable chip view must be there too"

    # The EDITOR must actually link here for a FULL_TEXT doc.
    e = client.get("/documents/%d/editor?locale=%s" % (did, LOC))  # noqa: UP031
    assert e.status_code == 200, e.status_code
    assert "/documents/%d/paragraphs" % did in e.text, "editor must link the paragraph view"  # noqa: UP031
    with Session(engine) as s:
        p = Project(name="ftpara-seglink-%d" % _seq(), org_id=1, source_locale="en-US")  # noqa: UP031
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        sd = ingest_document(s, p, "seg.docx", _build_docx(P0_RUNS, BODY), TransMode.SEGMENTED)
        s.commit()
        s.refresh(sd)
        sid = sd.id
    es = client.get("/documents/%d/editor?locale=%s" % (sid, LOC))  # noqa: UP031
    assert es.status_code == 200, es.status_code
    assert "/paragraphs" not in es.text, "a SEGMENTED doc must NOT offer the link"
    print("  5c. doc set-paragraph --json + GET /documents/{id}/paragraphs, whose page")
    print("      shows the LITERAL token text (not only chips) OK")


# ===========================================================================
# 6. C-3 - the explicit per-export override, its ceiling, and guard A's exclusivity.
# ===========================================================================
def test_source_fallback_override_and_ceiling():
    H = _docs_key()
    client = TestClient(app)
    settings.set_many({"export_source_fallback_max_share": "0.35"})

    # Default is UNCHANGED: no override -> still quarantined.
    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("without the override the document still quarantines")
        except export.TagExportQuarantine:
            pass
        rep: list = []
        data = export.build_translated_file(s, d, LOC, report=rep, allow_source_fallback=True)
    # A self-describing record - a PARAGRAPH index, and an EXPLICIT operator override.
    assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], rep
    assert "⟦" not in _docx_text(data)
    assert _formatted_run_count(data, 0) == 3
    print("  6a. allow_source_fallback=True on the 21-percent doc: exports, reports [0],")
    print("      formatting intact; the DEFAULT still quarantines OK")

    # The CEILING refuses, it does not warn.
    long_p0 = [
        (
            "Acme Pro is the connector platform that localization teams at large "
            "game studios use every single day to ship their titles worldwide "
            "with reviewed translations in more than thirty languages.",
            "bold",
        ),
        (" It also handles glossary and review workflows end to end for them.", "italic"),
        (" Learn more today.", "underline"),
    ]
    fxb = _fixture(
        p0_runs=long_p0,
        plain=["Short tail."],
        target_paras=["Texto en espanol sin etiquetas de formato.", "Cola corta."],
    )
    share = len(fxb["units"][0]["text"]) / len(fxb["source_text"])
    assert share > 0.8, ("ceiling fixture must be dominated by paragraph 0", share)
    with Session(engine) as s:
        d = s.get(Document, fxb["doc_id"])
        try:
            export.build_translated_file(s, d, LOC, allow_source_fallback=True)
            raise AssertionError("above the ceiling the override must be REFUSED")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "share_ceiling", exc.kind
            assert "0.35" in str(exc), str(exc)
    print(f"  6b. share {share:.2f} > ceiling 0.35 -> the override is REFUSED, not warned OK")

    # Guard A keeps its EXCLUSIVE claim on the global quarantine.
    fxc = _fixture(target_paras=[T0_STRIPPED] + TBODY[:3])  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fxc["doc_id"])
        try:
            export.build_translated_file(s, d, LOC, allow_source_fallback=True)
            raise AssertionError("a COUNT desync must still quarantine globally")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "count", exc.kind
            assert exc.paragraphs == [], exc.paragraphs
    print("  6c. guard A (count desync) still quarantines GLOBALLY under the override OK")

    # The surfaces: same override, same #41 header, same CLI flag.
    fx2 = _damaged_fixture()
    r = client.get("/documents/%d/export?locale=%s" % (fx2["doc_id"], LOC))  # noqa: UP031
    assert r.status_code == 409, r.status_code
    # The 409 names a way out (paragraphs page + explicit override).
    assert r.json()["kind"] == "tokens", r.json()
    assert "paragraphs" in r.json().get("paragraphs_url", ""), r.json()
    assert "allow_source_fallback=1" in r.json().get("export_anyway_url", ""), r.json()
    r = client.get("/documents/%d/export?locale=%s&allow_source_fallback=1" % (fx2["doc_id"], LOC))  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.headers.get("x-export-substituted") == "1", dict(r.headers)
    ra = client.get(
        "/api/documents/%d/download?locale=%s&allow_source_fallback=1" % (fx2["doc_id"], LOC),  # noqa: UP031
        headers=H,
    )
    assert ra.status_code == 200, (ra.status_code, ra.text[:300])
    assert ra.headers.get("x-export-substituted") == "1", dict(ra.headers)
    assert _docx_text(r.content) == _docx_text(ra.content), "web/api content differs"
    assert _formatted_run_count(r.content, 0) == _formatted_run_count(ra.content, 0) == 3

    import tempfile

    tmp = tempfile.mkdtemp(prefix="ftpara_")
    code, out, err = run_cli(
        [
            "--json",
            "doc",
            "export",
            str(fx2["doc_id"]),
            "--locale",
            LOC,
            "-o",
            os.path.join(tmp, "x.docx"),
            "--allow-source-fallback",
        ]
    )
    assert code == 0, (code, out, err)
    assert _json.loads(out.strip().splitlines()[-1])["substituted"] == [
        {"unit": "paragraph", "index": 0, "kind": "explicit_override"}
    ]
    code, out, err = run_cli(
        [
            "--json",
            "doc",
            "export",
            str(fx2["doc_id"]),
            "--locale",
            LOC,
            "-o",
            os.path.join(tmp, "y.docx"),
        ]
    )
    assert code != 0, (code, out, err)
    print("  6d. web + /api + CLI all honour the explicit override and report it")
    print("      through the #41 channel; without the flag the CLI still fails OK")


# ===========================================================================
# 7. Byte-identity / no-op cases.
# ===========================================================================
def test_no_op_cases():
    from app.services import fulltext_paragraphs as ftp

    clean = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    fx = _fixture(target_paras=[clean] + TBODY)  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        v = ftp.split_view(s, d, LOC)
        assert v["damaged_count"] == 0 and v["fallback_share"] == 0.0, v
        rep: list = []
        rep2: list = []
        a = export.build_translated_file(s, d, LOC, report=rep)
        b = export.build_translated_file(s, d, LOC, report=rep2, allow_source_fallback=True)
    assert rep == [] and rep2 == [], (rep, rep2)
    assert _docx_text(a) == _docx_text(b), "the override changed a clean document"
    assert _formatted_run_count(a, 0) == _formatted_run_count(b, 0) == 3
    assert "conector se lanza hoy" in _docx_text(a)
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert not [
            f for f in (seg.qa_flags or []) if f.get("type") == quality.SOURCE_FALLBACK_FLAG_TYPE
        ]
    print("  7a. clean FULL_TEXT: no flags, report [], override is a strict no-op OK")

    # HONEST-FAILURE (2026-08-13 big-doc flood): a TAGLESS FULL_TEXT doc whose model
    # emitted FEWER paragraphs than the source no longer silently source-fills the missing
    # positions (the old "graceful positional fallback" that served ENGLISH under a target
    # filename). It now REFUSES loud (kind="untranslated"); split_view still reports the
    # desync. Full coverage of both directions + the ceiling lives in
    # verify_ft_export_untranslated.py.
    with Session(engine) as s:
        p = Project(name="ftpara-tagless-%d" % _seq(), org_id=1, source_locale="en-US")  # noqa: UP031
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        d = ingest_document(
            s,
            p,
            "plain.docx",
            _build_docx([("Title here", "bold")], ["Body here"]),
            TransMode.FULL_TEXT,
        )
        s.commit()
        s.refresh(d)
        seg = s.exec(select(Segment).where(Segment.document_id == d.id)).one()
        assert segment.find_placeholders(seg.source_text) == [], seg.source_text
        seg.target_text = "Titulo y cuerpo juntos"  # reflowed 2 -> 1
        s.add(seg)
        s.commit()
        try:
            export.paragraph_texts_for(s, d, LOC)
            raise AssertionError("a tagless parts<units desync must refuse, not source-fill")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "untranslated", exc.kind
            assert "silently substitute ENGLISH source" in str(exc), str(exc)
            assert str(exc).isascii(), str(exc)
        v = ftp.split_view(s, d, LOC)
        assert v["ok"] is False and v["reason"] == "count_desync", v
    print("  7b. TAGLESS parts<units now REFUSES (honest-failure) instead of source-filling OK")

    # A revert on an UNDAMAGED paragraph is refused.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        r = ftp.revert_paragraph(s, d, LOC, 3, actor="tester@example.com")
    assert r["ok"] is False and r["reason"] == "not_damaged", r
    assert ftp.outcome_class(r) == "conflict", r  # not "invalid"/400
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        r = ftp.revert_paragraph(s, d, LOC, 99, actor="tester@example.com")
    assert r["ok"] is False and r["reason"] == "bad_index", r
    print("  7c. revert refuses an undamaged paragraph and an out-of-range index OK")

    # An UNTRANSLATED FULL_TEXT doc is EMPTY, not damaged.
    fxu = _fixture()  # ingested, never translated
    with Session(engine) as s:
        d = s.get(Document, fxu["doc_id"])
        v = ftp.split_view(s, d, LOC)
        assert v["ok"] is True and v["untranslated"] is True, v
        assert v["damaged_count"] == 0, v
        assert [r["missing"] for r in v["rows"]] == [[]] * 5, v["rows"]
        data = export.build_translated_file(s, d, LOC)  # must NOT quarantine
        r = ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")
    assert r["ok"] is False and r["reason"] == "not_damaged", r
    assert "Acme Pro" in _docx_text(data)
    print("  7d. an UNTRANSLATED FULL_TEXT doc reads as empty, not damaged OK")


# ===========================================================================
# 8. THE BOUNDARY PROBE - the FULL_TEXT path must call the REAL predicate, and a
#    REORDER is now damage (advisory dropped).
# ===========================================================================
# Every token present, IN ORDER, correctly nested - r2's BODY emptied because the model
# moved the words into r3. `quality.tag_damage`'s last rung is written for exactly this
# ("inline tag emptied span r2"). export's guard B and `fulltext_paragraphs._rows` used
# to judge with `Counter(find_placeholders(...))` - a STRICT SUBSET - so the whole class
# was invisible on both surfaces at once.
T0_EMPTIED_SPAN = "⟦r1⟧El ⟦/r1⟧⟦r2⟧⟦/r2⟧⟦r3⟧conector Acme Pro se lanza hoy.⟦/r3⟧"


def test_emptied_span_is_seen_in_fulltext():
    from app.services import fulltext_paragraphs as ftp

    fx = _fixture(target_paras=[T0_EMPTIED_SPAN] + TBODY)  # noqa: RUF005
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        whole = quality.tag_damage(seg.source_text, seg.target_text)
        assert whole == "inline tag emptied span r2", whole
        # The token MULTISET is intact - the comparison the two FULL_TEXT surfaces used
        # to make cannot tell this body from a perfect one.
        src_p0 = fx["units"][0]["text"]
        tgt_p0 = (seg.target_text or "").split("\n\n")[0]
        assert sorted(segment.find_placeholders(src_p0)) == sorted(
            segment.find_placeholders(tgt_p0)
        ), (src_p0, tgt_p0)

        d = s.get(Document, fx["doc_id"])
        try:
            data = export.build_translated_file(s, d, LOC)
            raise AssertionError(
                "an emptied paired span must NOT export silently (delivered %d bytes, "  # noqa: UP031
                "%d formatted run(s) in paragraph 0)" % (len(data), _formatted_run_count(data, 0))
            )
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens", exc.kind
            assert [p["index"] for p in exc.paragraphs] == [0], exc.paragraphs
            # missing/extra are EMPTY here: `reason` is the field that names it.
            assert exc.paragraphs[0]["missing"] == [], exc.paragraphs[0]
            assert exc.paragraphs[0]["extra"] == [], exc.paragraphs[0]
            assert exc.paragraphs[0]["reason"] == "inline tag emptied span r2", exc.paragraphs[0]
            assert str(exc).startswith("FULL_TEXT paragraph 0 inline-tag set desync")
            assert "emptied span r2" in str(exc), str(exc)
            assert str(exc).isascii(), str(exc)  # repo law: ASCII-safe CLI output
    print("  8a. FULL_TEXT export guard B calls quality.tag_damage per paragraph: an")
    print("      emptied paired span quarantines and names r2 OK")

    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        v = ftp.split_view(s, d, LOC)
    assert v["ok"] is True and v["damaged_count"] == 1, v
    assert v["damaged_indices"] == [0], v
    r0 = v["rows"][0]
    assert r0["damaged"] is True and r0["missing"] == [] and r0["extra"] == [], r0
    assert r0["reason"] == "inline tag emptied span r2", r0
    print("  8b. split_view marks it damaged and NAMES r2 in `reason` OK")

    # The CLI and /api render the reason, so an operator is not shown an empty delta.
    code, out, err = run_cli(["doc", "paragraphs", "--doc", str(fx["doc_id"]), "--locale", LOC])
    assert code == 0, (code, out, err)
    assert "paragraph 0: inline tag emptied span r2" in out, out
    assert out.isascii(), out
    H = _docs_key()
    client = TestClient(app)
    ra = client.get("/api/documents/%d/paragraphs?locale=%s" % (fx["doc_id"], LOC), headers=H)  # noqa: UP031
    assert ra.status_code == 200, ra.text[:200]
    assert ra.json()["rows"][0]["reason"] == "inline tag emptied span r2", ra.json()
    rw = client.get("/documents/%d/paragraphs?locale=%s" % (fx["doc_id"], LOC))  # noqa: UP031
    assert rw.status_code == 200 and "emptied span r2" in rw.text, rw.status_code
    print("  8c. CLI + /api + the editor page all NAME the damage OK")

    # The recovery still works on it.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        rep: list = []
        data = export.build_translated_file(s, d, LOC, report=rep, allow_source_fallback=True)
    assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], rep
    assert _formatted_run_count(data, 0) == 3, "source fallback must restore the runs"
    print("  8d. the #42 override rescues it: 3 formatted runs delivered OK")

    # ---- A REORDER IS DAMAGE (advisory dropped) --------------------------------------
    # Under the dropped `advisory` classification a shape-preserving reorder SHIPPED. On
    # main, without advisory, ANY divergence is export-blocking: the reorder is shown red,
    # quarantines, and now HAS a route out (the revert). This is the accepted tradeoff.
    reordered = "⟦r2⟧Acme Pro⟦/r2⟧⟦r1⟧ es el ⟦/r1⟧⟦r3⟧conector que se lanza hoy.⟦/r3⟧"
    fx2 = _fixture(target_paras=[reordered] + TBODY)  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fx2["doc_id"])
        v = ftp.split_view(s, d, LOC)
        r0 = v["rows"][0]
        assert r0["damaged"] is True, r0
        assert r0["reason"].startswith("inline tag reordered"), r0
        assert "advisory" not in r0, "advisory classification must be gone"
        assert v["damaged_count"] == 1 and v["damaged_indices"] == [0], v
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("a reorder must quarantine under conservative semantics")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens", exc.kind
            assert str(exc).isascii(), str(exc)
        # ...and because it is DAMAGED it now HAS a route out: the revert is offered.
        rv = ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")
        assert rv["ok"] is True, rv
    print("  8e. a REORDER is now DAMAGE (advisory dropped): quarantined, shown red, and")
    print("      offered a revert - the conservative-semantics tradeoff OK")

    # Crossed nesting also quarantines (a permutation of the token stream).
    crossed = "⟦r1⟧El ⟦r2⟧Acme⟦/r1⟧ Pro⟦/r2⟧⟦r3⟧ se lanza hoy.⟦/r3⟧"
    fxn = _fixture(target_paras=[crossed] + TBODY)  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fxn["doc_id"])
        v = ftp.split_view(s, d, LOC)
        assert v["damaged_indices"] == [0], v["damaged_indices"]
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("crossed nesting must quarantine")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens", exc.kind
            assert str(exc).isascii(), str(exc)
    print("  8f. crossed nesting quarantines too OK")

    # PER-PARAGRAPH IS STRICTLY SHARPER THAN WHOLE-BODY. Token ids restart at 1 in every
    # paragraph, so on the whole blob `paired_span_bodies` keeps only the LAST body for a
    # repeated name: an emptied r2 in paragraph 0 hides behind a healthy r2 in the tail.
    tail_src = "Tail pair ⟦r2⟧ok⟦/r2⟧ ends it."
    tail_tgt = "Cola ⟦r2⟧bien⟦/r2⟧ termina."
    fx3 = _fixture(
        plain=[BODY[0], BODY[1], BODY[2], tail_src],
        target_paras=[T0_EMPTIED_SPAN, TBODY[0], TBODY[1], TBODY[2], tail_tgt],
    )
    with Session(engine) as s:
        seg = s.get(Segment, fx3["seg_id"])
        assert quality.tag_damage(seg.source_text, seg.target_text) == "", (
            "fixture must be one the WHOLE-BODY verdict cannot see"
        )
        d = s.get(Document, fx3["doc_id"])
        v = ftp.split_view(s, d, LOC)
        assert v["damaged_indices"] == [0], v["damaged_indices"]
        assert v["rows"][0]["reason"] == "inline tag emptied span r2", v["rows"][0]
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("the collision case must still quarantine")
        except export.TagExportQuarantine as exc:
            assert [p["index"] for p in exc.paragraphs] == [0], exc.paragraphs
        # ...and the WRITER agrees with the view: a save that re-creates the paragraph-
        # local break is refused, and a CONFIRMED one lands flagged rather than clearing.
        r = ftp.save_paragraph(s, d, LOC, 0, T0_EMPTIED_SPAN, actor="tester@example.com")
        assert r["ok"] is False and r["reason"] == "tag_damage", r
        assert "emptied span r2" in r["detail"], r
        r = ftp.save_paragraph(
            s, d, LOC, 0, T0_EMPTIED_SPAN, actor="tester@example.com", confirm=True
        )
        assert r["ok"] is True, r
    with Session(engine) as s:
        seg = s.get(Segment, fx3["seg_id"])
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE], (
            seg.qa_flags
        )
    print("  8g. the per-paragraph call is SHARPER than the whole-body one, and")
    print("      save_paragraph judges the paragraph too OK")

    # ---- THE BADGE-CLEARING HALF -----------------------------------------------------
    # The clearing branch judges EVERY paragraph of the candidate body, so saving an
    # UNRELATED clean paragraph does NOT clear a tag_integrity badge the real predicate
    # still calls damaged (the collision above).
    fx4 = _fixture(
        plain=[BODY[0], BODY[1], BODY[2], tail_src],
        target_paras=[T0_EMPTIED_SPAN, TBODY[0], TBODY[1], TBODY[2], tail_tgt],
    )
    with Session(engine) as s:
        seg = s.get(Segment, fx4["seg_id"])
        assert quality.tag_damage(seg.source_text, seg.target_text) == "", (
            "fixture must be one the WHOLE-BODY verdict cannot see"
        )
        seg.status = SegStatus.NEEDS_REVIEW
        seg.qa_flags = quality.merge_tag_flags(None, "inline tag emptied span r2")
        s.add(seg)
        s.commit()
        d = s.get(Document, fx4["doc_id"])
        r = ftp.save_paragraph(s, d, LOC, 1, TBODY[0], actor="tester@example.com")
        assert r["ok"] is True, r
    with Session(engine) as s:
        seg = s.get(Segment, fx4["seg_id"])
        flags = [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]
        assert flags, "the badge must SURVIVE: paragraph 0 is still damaged"
        assert "emptied span r2" in (flags[0].get("reason") or ""), flags
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        d = s.get(Document, fx4["doc_id"])
        assert ftp.split_view(s, d, LOC)["damaged_indices"] == [0]
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("export must still quarantine - the badge was true")
        except export.TagExportQuarantine:
            pass
        # Repairing the REAL defect clears it (the badge must not become permanent).
        repaired = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
        r = ftp.save_paragraph(s, d, LOC, 0, repaired, actor="tester@example.com")
        assert r["ok"] is True, r
    with Session(engine) as s:
        seg = s.get(Segment, fx4["seg_id"])
        assert not [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE], (
            seg.qa_flags
        )
        assert seg.status == SegStatus.EDITED, seg.status
    print("  8h. a save of an UNRELATED clean paragraph does NOT clear a badge the real")
    print("      predicate still calls damaged; repairing the real defect clears it OK")


# ===========================================================================
# 9. The record, the classification, the channel.
# ===========================================================================
def test_report_channel_and_classification():
    from app.services import fulltext_paragraphs as ftp
    from app.web import deps as _deps

    # ONE channel, two units, each record naming its own.
    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        rep: list = []
        export.build_translated_file(s, d, LOC, report=rep, allow_source_fallback=True)
    assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], rep
    assert export.substitution_detail(rep) == (
        "substituted=1 kind=explicit_override unit=paragraph index=0"
    ), export.substitution_detail(rep)
    assert export.substitution_detail([]) == "", "a clean export must audit as before"
    mixed = export.substitution_detail(
        [
            {"unit": "segment", "index": 3, "kind": export.SUB_AUTOMATIC},
            {"unit": "segment", "index": 7, "kind": export.SUB_AUTOMATIC},
        ]
    )
    assert mixed == "substituted=2 kind=automatic unit=segment index=3,7", mixed
    print("  9a. the substitution record names its UNIT (segment order_index vs")
    print("      FULL_TEXT paragraph index) - one channel, no ambiguous ints OK")

    # The audit row distinguishes an EXPLICIT force-export from a routine one.
    client = TestClient(app)
    r = client.get("/documents/%d/export?locale=%s&allow_source_fallback=1" % (fx["doc_id"], LOC))  # noqa: UP031
    assert r.status_code == 200, r.status_code
    with Session(engine) as s:
        row = s.exec(
            select(AuditLog)
            .where(
                AuditLog.action == "document.export",
                AuditLog.target == "doc {} {}".format(fx["doc_id"], LOC),
            )
            .order_by(AuditLog.id.desc())
        ).first()
    assert row is not None and "kind=explicit_override" in (row.detail or ""), row
    assert "unit=paragraph" in row.detail and "index=0" in row.detail, row.detail
    print("  9b. the audit detail says kind=explicit_override unit=paragraph OK")

    # ONE classification of the refusal event; the two web surfaces agree.
    H = _docs_key()
    fx2 = _damaged_fixture()
    breaking = "El conector se lanza hoy."  # drops every token -> tag_damage
    body = {"locale": LOC, "text": breaking}
    rw = client.post("/documents/%d/paragraphs/0/save" % fx2["doc_id"], data=body)  # noqa: UP031
    ra = client.post(
        "/api/documents/%d/paragraphs/0/save?locale=%s" % (fx2["doc_id"], LOC),  # noqa: UP031
        headers=H,
        json={"text": breaking},
    )
    assert rw.json().get("needs_confirm") is True, rw.json()
    assert ra.json().get("needs_confirm") is True, ra.json()
    assert rw.status_code == ra.status_code == 409, (rw.status_code, ra.status_code)
    assert ftp.outcome_class(rw.json()) == "confirmable", rw.json()
    assert _deps.paragraph_status(rw.json()) == 409
    # the classification is exhaustive, and unknown reasons never read as success
    assert ftp.outcome_class({"ok": True}) == "ok"
    assert ftp.outcome_class({"ok": False, "reason": "count_desync"}) == "conflict"
    assert ftp.outcome_class({"ok": False, "reason": "no_segment"}) == "not_found"
    assert ftp.outcome_class({"ok": False, "reason": "blank_line"}) == "invalid"
    assert ftp.outcome_class({"ok": False, "reason": "brand_new"}) == "invalid"
    # EXHAUSTIVE AGAINST THE MODULE, not against a hand-list. Read the reasons out of the
    # source: a reason the module returns but does not classify would answer 400.
    import inspect
    import re as _re

    declared = set(_re.findall(r'"reason":\s*"([a-z_]+)"', inspect.getsource(ftp)))
    assert declared, "the scan must actually find the return sites"
    unclassified = sorted(r for r in declared if r not in ftp._OUTCOME_CLASS)
    assert not unclassified, (
        "reasons this module returns but does not classify (they would answer 400 as "
        f"'invalid'): {unclassified}"
    )
    assert ftp.outcome_class({"ok": False, "reason": "not_damaged"}) == "conflict"
    assert _deps.paragraph_status({"ok": False, "reason": "not_damaged"}) == 409
    # the CLI refusal is a readable, ASCII, --confirm-hinting message.
    code, out, err = run_cli(
        [
            "doc",
            "set-paragraph",
            "--doc",
            str(fx2["doc_id"]),
            "--locale",
            LOC,
            "--index",
            "0",
            "--text",
            breaking,
        ]
    )
    assert code != 0 and "--confirm" in (out + err), (code, out, err)
    _msg = out + err
    assert _msg.isascii(), _msg
    assert "\\u27e6" not in _msg.lower(), _msg
    assert "dropped r1" in _msg or "dropped /r1" in _msg, _msg
    print("  9c. web 409 == /api 409 for ONE refusal; the seam classifies, each")
    print("      transport renders; the CLI message is readable ASCII OK")


# ===========================================================================
# 10. HIGH-1 - the revert ceiling, the durable report, and --all-damaged ATOMICITY.
# ===========================================================================
def test_revert_ceiling_durable_report_and_atomic_batch():
    from app.services import fulltext_paragraphs as ftp

    H = _docs_key()
    client = TestClient(app)
    settings.set_many({"export_source_fallback_max_share": "0.35"})

    # (a) THE BYPASS. The same document the sanctioned door refuses at ~90% share was
    # produced by REVERTING the paragraph instead - revert_paragraph had no ceiling.
    long_p0 = [
        (
            "Acme Pro is the connector platform that localization teams at large "
            "game studios use every single day to ship their titles worldwide "
            "with reviewed translations in more than thirty languages.",
            "bold",
        ),
        (" It also handles glossary and review workflows end to end for them.", "italic"),
        (" Learn more today.", "underline"),
    ]
    fxb = _fixture(
        p0_runs=long_p0,
        plain=["Short tail."],
        target_paras=["Texto en espanol sin etiquetas de formato.", "Cola corta."],
    )
    with Session(engine) as s:
        d = s.get(Document, fxb["doc_id"])
        rv = ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")
    assert rv["ok"] is False and rv["reason"] == "share_ceiling", rv
    assert rv["fallback_share"] > 0.35 and rv["ceiling"] == 0.35, rv
    assert ftp.outcome_class(rv) == "conflict", rv
    # NOTHING was written.
    with Session(engine) as s:
        seg = s.get(Segment, fxb["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], seg.qa_flags
        assert seg.target_text.split("\n\n")[0] == "Texto en espanol sin etiquetas de formato."
    r = client.post(
        "/api/documents/%d/paragraphs/0/revert?locale=%s" % (fxb["doc_id"], LOC),  # noqa: UP031
        headers=H,  # noqa: RUF100, UP031
    )
    assert r.status_code == 409, (r.status_code, r.text[:300])
    code, out, err = run_cli(
        ["doc", "revert-paragraph", "--doc", str(fxb["doc_id"]), "--locale", LOC, "--index", "0"]
    )
    assert code != 0, (code, out, err)
    print("  10a. revert_paragraph enforces the SAME export_source_fallback_max_share")
    print("       ceiling the export door does (service + /api + CLI) OK")

    # (b) --all-damaged IS ATOMIC (the branch's F-3 bug). A doc with TWO damaged
    # paragraphs whose COMBINED share exceeds the ceiling, but each of which is under it,
    # must revert NOTHING - the branch looped and half-wrote before the ceiling tripped.
    fxa = _fixture(
        p0_runs=[
            ("Acme Pro ", "bold"),
            ("is the connector platform for studios ", "italic"),
            ("worldwide every single day here.", "underline"),
        ],
        plain=[
            "Second block ⟦r1⟧with formatting⟦/r1⟧ that must be preserved here too for balance.",
            "Third clean tail paragraph, a reasonable amount of balancing text here.",
        ],
        target_paras=[
            "Plataforma sin etiquetas de formato de ningun tipo aqui.",
            "Segundo bloque sin etiquetas aqui tampoco por desgracia.",
            "Tercer parrafo limpio con bastante texto de equilibrio aqui.",
        ],
    )
    units, src = fxa["units"], fxa["source_text"]
    s0 = len(units[0]["text"]) / len(src)
    s1 = len(units[1]["text"]) / len(src)
    # a ceiling above each individual share but below the combined one.
    ceiling = max(s0, s1) + 0.02
    assert s0 < ceiling and s1 < ceiling and (s0 + s1) > ceiling, (s0, s1, ceiling)
    settings.set_many({"export_source_fallback_max_share": f"{ceiling:.4f}"})
    with Session(engine) as s:
        d = s.get(Document, fxa["doc_id"])
        assert ftp.split_view(s, d, LOC)["damaged_indices"] == [0, 1]
        res = ftp.revert_all_damaged(s, d, LOC, actor="tester@example.com")
    assert res["ok"] is False and res["reason"] == "share_ceiling", res
    # ATOMIC: the WHOLE batch is refused, NOTHING written (no partial p0 revert).
    with Session(engine) as s:
        seg = s.get(Segment, fxa["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], (
            "no partial write on a mid-batch refusal",
            seg.qa_flags,
        )
        parts = seg.target_text.split("\n\n")
        assert parts[0] == "Plataforma sin etiquetas de formato de ningun tipo aqui.", parts
        assert parts[1] == "Segundo bloque sin etiquetas aqui tampoco por desgracia.", parts
    # ...and a SINGLE-paragraph revert (under the ceiling) still works, so the batch
    # refusal is specifically the atomicity, not p0 being individually over ceiling.
    with Session(engine) as s:
        d = s.get(Document, fxa["doc_id"])
        one = ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")
    assert one["ok"] is True, one
    # The CLI arm refuses --all-damaged the same way, atomically.
    fxa2 = _fixture(
        p0_runs=[
            ("Acme Pro ", "bold"),
            ("is the connector platform for studios ", "italic"),
            ("worldwide every single day here.", "underline"),
        ],
        plain=[
            "Second block ⟦r1⟧with formatting⟦/r1⟧ that must be preserved here too for balance.",
            "Third clean tail paragraph, a reasonable amount of balancing text here.",
        ],
        target_paras=[
            "Plataforma sin etiquetas de formato de ningun tipo aqui.",
            "Segundo bloque sin etiquetas aqui tampoco por desgracia.",
            "Tercer parrafo limpio con bastante texto de equilibrio aqui.",
        ],
    )
    code, out, err = run_cli(
        ["doc", "revert-paragraph", "--doc", str(fxa2["doc_id"]), "--locale", LOC, "--all-damaged"]
    )
    assert code != 0, ("--all-damaged over the ceiling must fail atomically", out, err)
    with Session(engine) as s:
        seg = s.get(Segment, fxa2["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], seg.qa_flags
    settings.set_many({"export_source_fallback_max_share": "0.35"})
    print("  10b. --all-damaged is ATOMIC: a batch whose combined share exceeds the")
    print("       ceiling reverts NOTHING (F-3: the branch half-wrote mid-batch) OK")

    # (c) A LEGAL revert (21% doc) is now REPORTED by every later export.
    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        assert ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")["ok"] is True
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        rep: list = []
        export.build_translated_file(s, d, LOC, report=rep)
    assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], rep
    assert "kind=explicit_override" in export.substitution_detail(rep)
    r = client.get("/documents/%d/export?locale=%s" % (fx["doc_id"], LOC))  # noqa: UP031
    assert r.status_code == 200 and r.headers.get("x-export-substituted") == "1", dict(r.headers)
    ra = client.get("/api/documents/%d/download?locale=%s" % (fx["doc_id"], LOC), headers=H)  # noqa: UP031
    assert ra.headers.get("x-export-substituted") == "1", dict(ra.headers)
    assert "kind=explicit_override" in (ra.headers.get("x-export-substituted-detail") or ""), dict(
        ra.headers
    )
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "document.export")).all()
    hit = [x for x in rows if x.target == "doc %d %s" % (fx["doc_id"], LOC)]  # noqa: UP031
    assert hit and "substituted=1" in hit[-1].detail, [x.detail for x in hit]
    # ...and a CLEAN document still reports nothing (byte-identity).
    clean = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    fxc = _fixture(target_paras=[clean] + TBODY)  # noqa: RUF005
    with Session(engine) as s:
        d = s.get(Document, fxc["doc_id"])
        rep2: list = []
        export.build_translated_file(s, d, LOC, report=rep2)
    assert rep2 == [], rep2
    print("  10c. a durable source_fallback is reported by EVERY later export; a clean")
    print("       doc still reports nothing OK")


# ===========================================================================
# 11. MEDIUM-1 - "Copy source" + save was a two-click laundering flow.
# ===========================================================================
def test_copy_source_save_keeps_the_flag():
    from app.services import fulltext_paragraphs as ftp

    settings.set_many({"export_source_fallback_max_share": "0.35"})
    fx = _damaged_fixture()
    src0 = fx["units"][0]["text"]
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        assert ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")["ok"] is True
        # The laundering move: save the SOURCE text back over the reverted paragraph.
        r = ftp.save_paragraph(s, d, LOC, 0, src0, actor="tester@example.com")
        assert r["ok"] is True, r
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [0], seg.qa_flags
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        rep: list = []
        export.build_translated_file(s, d, LOC, report=rep)
    assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], rep
    # A REAL repair still clears it.
    fixed = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        assert ftp.save_paragraph(s, d, LOC, 0, fixed, actor="tester@example.com")["ok"] is True
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], seg.qa_flags
        assert seg.status == SegStatus.EDITED, seg.status
    print("  11. a save whose text IS the source text KEEPS the source_fallback flag;")
    print("      a real repair still clears it OK")


# ===========================================================================
# 12. MEDIUM-2 - the recovery verbs travel over --remote, like the export door.
# ===========================================================================
def _mock_transport_for(token_client):
    """A sync httpx.MockTransport forwarding every request to the in-process
    TestClient, so --remote runs fully offline against the real /api."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = token_client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_remote_recovery_verbs():
    from app.cli import _remote as _rm
    from app.services import apikeys, security

    u = security.get_by_email("ftpara@example.com")
    if u is None:
        u = security.create_user("ftpara@example.com", "FT Para", "admin", "Pw!12345xyz-ftpara")
    _row, tok = apikeys.create_key(u.id, "ftpara-remote-%d" % _seq(), scopes="api docs")  # noqa: UP031
    settings.set_many({"export_source_fallback_max_share": "0.35"})
    fx = _damaged_fixture()
    did = str(fx["doc_id"])
    _rm._set_transport(_mock_transport_for(TestClient(app)))
    try:
        base = ["--json", "--remote", "http://127.0.0.1", "--token", tok]
        code, out, err = run_cli(base + ["doc", "paragraphs", "--doc", did, "--locale", LOC])  # noqa: RUF005
        assert code == 0, (code, out, err)
        remote_view = _json.loads(out.strip().splitlines()[-1])
        code, out, err = run_cli(["--json", "doc", "paragraphs", "--doc", did, "--locale", LOC])
        local_view = _json.loads(out.strip().splitlines()[-1])
        assert remote_view["rows"] == local_view["rows"], "remote != local rows"
        assert remote_view["damaged_indices"] == local_view["damaged_indices"] == [0]

        # A refusal travels as a refusal, and its message stays READABLE ASCII.
        code, out, err = run_cli(
            base  # noqa: RUF005
            + [
                "doc",
                "set-paragraph",
                "--doc",
                did,
                "--locale",
                LOC,
                "--index",
                "0",
                "--text",
                T0_STRIPPED,
            ]
        )
        assert code != 0, (code, out, err)
        msg = out + err
        assert msg.isascii() and "\\u27e6" not in msg.lower(), msg
        assert "--confirm" in msg, msg

        # The hand repair, over HTTP, lands on the committed row.
        fixed = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
        import tempfile

        pth = os.path.join(tempfile.mkdtemp(prefix="ftrem_"), "p.txt")
        with open(pth, "w", encoding="utf-8") as fh:
            fh.write(fixed)
        code, out, err = run_cli(
            base  # noqa: RUF005
            + [
                "doc",
                "set-paragraph",
                "--doc",
                did,
                "--locale",
                LOC,
                "--index",
                "0",
                "--from-file",
                pth,
            ]
        )
        assert code == 0, (code, out, err)
        with Session(engine) as s:
            seg = s.get(Segment, fx["seg_id"])
            assert seg.target_text.split("\n\n")[0] == fixed, seg.target_text

        # A state refusal reads as a SENTENCE remotely, not a bare "HTTP 409".
        code, out, err = run_cli(
            base + ["doc", "revert-paragraph", "--doc", did, "--locale", LOC, "--index", "0"]  # noqa: RUF005
        )
        assert code != 0, (code, out, err)
        assert "not export-blocking damaged" in (out + err), (out, err)
        assert "HTTP 409" not in (out + err), (out, err)

        # revert-paragraph too, including --all-damaged (atomic batch endpoint).
        fx2 = _damaged_fixture()
        code, out, err = run_cli(
            base  # noqa: RUF005
            + [
                "doc",
                "revert-paragraph",
                "--doc",
                str(fx2["doc_id"]),
                "--locale",
                LOC,
                "--all-damaged",
            ]
        )
        assert code == 0, (code, out, err)
        assert _json.loads(out.strip().splitlines()[-1])["reverted"] == [0]
        with Session(engine) as s:
            seg = s.get(Segment, fx2["seg_id"])
            assert quality.source_fallback_indices(seg.qa_flags) == [0], seg.qa_flags
    finally:
        _rm._set_transport(None)
    print("  12. doc paragraphs / revert-paragraph / set-paragraph are --remote-capable")
    print("      and render the SAME rows as the local arm OK")


# ===========================================================================
# 13. Review HIGH-1 (F-3, second door) - --all-damaged is atomic on a mid-batch
#     write_refused, not only on the ceiling.
# ===========================================================================
def test_all_damaged_atomic_on_write_refused():
    from app.services import fulltext_paragraphs as ftp

    # Ceiling OUT OF THE WAY (1.0) so the batch reaches the refusal door, not the ceiling
    # door: this pins the OTHER atomicity path. Two damaged paragraphs; paragraph 1's
    # SOURCE is a curated refusal FULL PHRASE ("provide the actual source text"), so when
    # the batch stages paragraph 1 the whole spliced body trips refusal.is_refusal and
    # commit_mt_output WITHHOLDS the write. Paragraph 0 has already been staged - and on
    # the pre-fix code (a loop of the COMMITTING revert_paragraph) it was already
    # COMMITTED, leaving a durable partial revert. Atomic staging rolls it all back.
    settings.set_many({"export_source_fallback_max_share": "1.0"})
    fx = _fixture(
        p0_runs=P0_RUNS,
        plain=["provide the actual source text ⟦r1⟧here⟦/r1⟧ end.", "Clean tail paragraph here."],
        target_paras=[
            T0_STRIPPED,
            "traduccion sin etiquetas del parrafo uno.",
            "Traduccion del parrafo dos.",
        ],
    )
    # Precondition: the refusal guard is on (else write_refused can never fire and the
    # test is vacuous), and paragraph 1's source really does trip is_refusal.
    from app import config
    from app.services import refusal

    assert config.REFUSAL_GUARD_ENABLED, "refusal guard must be ON for this test"
    assert refusal.is_refusal("provide the actual source text here."), "fixture premise"
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        assert ftp.split_view(s, d, LOC)["damaged_indices"] == [0, 1]
        res = ftp.revert_all_damaged(s, d, LOC, actor="tester@example.com")
    assert res["ok"] is False and res["reason"] == "write_refused", res
    # ATOMIC: a FRESH session must show paragraph 0 UNCHANGED - no partial revert.
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        parts = seg.target_text.split("\n\n")
        assert parts[0] == T0_STRIPPED, (
            "paragraph 0 must NOT be reverted (F-3 via the refusal door)",
            parts[0],
        )
        assert quality.source_fallback_indices(seg.qa_flags) == [], seg.qa_flags
        assert seg.status == SegStatus.MT, seg.status  # not NEEDS_REVIEW
    # No audit row was written for the aborted batch (audit runs only after commit).
    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(
                AuditLog.action == "document.paragraph_revert",
                AuditLog.target == "doc %d %s p0" % (fx["doc_id"], LOC),  # noqa: UP031
            )
        ).all()
    assert rows == [], [r.detail for r in rows]
    settings.set_many({"export_source_fallback_max_share": "0.35"})
    print("  13. --all-damaged is ATOMIC on a mid-batch write_refused too: paragraph 0")
    print("      rolls back, no flag, no audit (pre-fix: p0 durably committed) OK")


# ===========================================================================
# 14. Review MEDIUM - the editor's whole-segment save agrees with export on a
#     FULL_TEXT doc (per-paragraph residual, not the blind whole-body verdict).
# ===========================================================================
def test_segment_save_fulltext_per_paragraph_residual():
    client = TestClient(app)
    # Same-named token across paragraphs (ids restart per paragraph): unit 1 and unit 2
    # each carry r1. The target EMPTIES unit 1's r1 span but leaves unit 2's healthy, so
    # the whole-body verdict (paired_span_bodies keeps only the LAST r1 body) reads CLEAN
    # while export's guard B (per paragraph) quarantines unit 1.
    fx = _fixture(
        p0_runs=[("Document header here.", "bold")],  # unit 0: single run, no tokens
        plain=["⟦r1⟧Bold text⟦/r1⟧ in paragraph one.", "⟦r1⟧Bold text⟦/r1⟧ in paragraph two."],
        target_paras=[
            "Document header here.",
            "⟦r1⟧Bold text⟦/r1⟧ in paragraph one.",
            "⟦r1⟧Bold text⟦/r1⟧ in paragraph two.",
        ],
    )
    emptied_body = (
        "Document header here.\n\n"
        "⟦r1⟧⟦/r1⟧ in paragraph one.\n\n"
        "⟦r1⟧Bold text⟦/r1⟧ in paragraph two."
    )
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        # PREMISE: the WHOLE-BODY verdict is blind here; the per-paragraph one is not.
        assert quality.tag_damage(seg.source_text, emptied_body) == "", (
            "the whole-body verdict must be blind for this test to mean anything"
        )
        d = s.get(Document, fx["doc_id"])
        assert "emptied span r1" in (
            quality.tag_damage_detail(d.skeleton["units"][1]["text"], emptied_body.split("\n\n")[1])
            or {}
        ).get("reason", "")
        sid = seg.id
    # The editor save of the WHOLE full-text unit, CONFIRMED. It must NOT read EDITED and
    # must NOT clear the badge - the export still quarantines paragraph 1.
    r = client.post("/segments/%d/save" % sid, data={"target_text": emptied_body, "confirm": "1"})  # noqa: UP031
    assert r.status_code == 200 and r.json()["ok"] is True, r.text[:300]
    assert r.json()["status"] == SegStatus.NEEDS_REVIEW, (
        "a confirmed export-blocking save must stay in review, not EDITED",
        r.json(),
    )
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        flags = [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]
        assert flags, (
            "the tag_integrity badge must SURVIVE - the export still "
            "quarantines paragraph 1 (pre-fix: cleared + EDITED)"
        )
    # ...and it AGREES with the export, which still quarantines.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("export must still quarantine the emptied span")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "tokens" and [p["index"] for p in exc.paragraphs] == [1], (
                exc.paragraphs
            )
    # A no-confirm save of the same body is BLOCKED (needs_confirm), like the export.
    r = client.post("/segments/%d/save" % sid, data={"target_text": emptied_body})  # noqa: UP031
    assert r.json().get("needs_confirm") is True, r.json()
    print("  14. the editor's whole-segment save uses the per-paragraph residual on a")
    print("      FULL_TEXT doc: an emptied span hidden by a same-named token blocks,")
    print("      keeps the badge and stays in review (pre-fix: EDITED, badge cleared) OK")


# ===========================================================================
# 15. Review MEDIUM/LOW - the copy-source flag-clear guard normalizes zero-width
#     characters, so an invisible edit cannot launder the source_fallback flag.
# ===========================================================================
def test_zero_width_source_save_keeps_flag():
    from app.services import fulltext_paragraphs as ftp

    # A CLASS of invisibles, not just U+200B: the first fix was a 6-char denylist and
    # U+2064 INVISIBLE PLUS / U+FE0F / U+180E / U+3164 laundered right past it (review
    # round 2). Every one of these appended to the source must STILL read as source.
    launder_chars = [
        "​",  # ZWSP (the original)
        "⁤",  # INVISIBLE PLUS (the round-2 repro)
        "️",  # variation selector-16
        "᠎",  # Mongolian vowel separator
        "ㅤ",  # Hangul filler
        "⁠﻿­",
    ]  # a mix
    for zwc in launder_chars:
        settings.set_many({"export_source_fallback_max_share": "0.35"})
        fx = _damaged_fixture()
        src0 = fx["units"][0]["text"]
        with Session(engine) as s:
            d = s.get(Document, fx["doc_id"])
            assert ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")["ok"] is True
            # The laundering move: retype the SOURCE text + an invisible char.
            r = ftp.save_paragraph(s, d, LOC, 0, src0 + zwc, actor="tester@example.com")
            assert r["ok"] is True, r
        with Session(engine) as s:
            seg = s.get(Segment, fx["seg_id"])
            assert quality.source_fallback_indices(seg.qa_flags) == [0], (
                f"source + {zwc!r} must NOT clear the flag",
                seg.qa_flags,
            )
            assert seg.status == SegStatus.NEEDS_REVIEW, (zwc, seg.status)
        # And the export still reports the paragraph as a source substitution.
        with Session(engine) as s:
            d = s.get(Document, fx["doc_id"])
            rep: list = []
            export.build_translated_file(s, d, LOC, report=rep)
        assert rep == [{"unit": "paragraph", "index": 0, "kind": export.SUB_EXPLICIT}], (zwc, rep)

    # NOT over-broad: a GENUINE translation whose text legitimately contains a ZWNJ
    # (Persian/Indic use it meaningfully) differs from the English source by real words,
    # so it is NOT treated as source — the flag DOES clear and it saves as a real edit.
    # A token-clean repair with a ZWNJ inside a real Spanish word.
    settings.set_many({"export_source_fallback_max_share": "0.35"})
    fx = _damaged_fixture()
    real = (
        "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme‌ Pro⟦/r2⟧"
        "⟦r3⟧ conector se lanza hoy.⟦/r3⟧"
    )  # note the ZWNJ inside "Acme‌ Pro"
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        assert ftp.revert_paragraph(s, d, LOC, 0, actor="tester@example.com")["ok"] is True
        r = ftp.save_paragraph(s, d, LOC, 0, real, actor="tester@example.com")
        assert r["ok"] is True and r["damaged_count"] == 0, r
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], (
            "a real translation containing a ZWNJ must clear the flag",
            seg.qa_flags,
        )
    print("  15. a source + ANY invisible-char save KEEPS the source_fallback flag (6")
    print("      laundering chars incl. U+2064); a real ZWNJ translation still clears OK")


# ===========================================================================
# 16. Review MEDIUM - --all-damaged has a WEB equivalent (atomic batch route).
# ===========================================================================
def test_web_revert_all_damaged():
    client = TestClient(app)

    # Under the ceiling: the web batch route reverts BOTH damaged paragraphs atomically.
    settings.set_many({"export_source_fallback_max_share": "1.0"})
    fx = _fixture(
        p0_runs=P0_RUNS,
        plain=["⟦r1⟧tokens⟦/r1⟧ here in paragraph one.", "Clean tail paragraph here."],
        target_paras=[T0_STRIPPED, "sin etiquetas en el parrafo uno.", "cola limpia."],
    )
    r = client.post(
        "/documents/%d/paragraphs/revert-all-damaged" % fx["doc_id"],  # noqa: UP031
        data={"locale": LOC},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert sorted(r.json()["reverted"]) == [0, 1], r.json()
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [0, 1], seg.qa_flags
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status

    # Over the ceiling: the web batch route refuses (409) and writes NOTHING - a linguist
    # cannot leave a half-reverted doc past the ceiling by the web route either.
    settings.set_many({"export_source_fallback_max_share": "0.10"})
    fx2 = _fixture(
        p0_runs=P0_RUNS,
        plain=["⟦r1⟧tokens⟦/r1⟧ here in paragraph one.", "Clean tail paragraph here."],
        target_paras=[T0_STRIPPED, "sin etiquetas en el parrafo uno.", "cola limpia."],
    )
    r = client.post(
        "/documents/%d/paragraphs/revert-all-damaged" % fx2["doc_id"],  # noqa: UP031
        data={"locale": LOC},  # noqa: RUF100, UP031
    )
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert r.json()["reason"] == "share_ceiling", r.json()
    with Session(engine) as s:
        seg = s.get(Segment, fx2["seg_id"])
        assert quality.source_fallback_indices(seg.qa_flags) == [], (
            "nothing may be reverted above the ceiling",
            seg.qa_flags,
        )
        assert seg.target_text.split("\n\n")[0] == T0_STRIPPED, seg.target_text
    settings.set_many({"export_source_fallback_max_share": "0.35"})
    # The button appears on the page only when MORE THAN ONE paragraph is damaged.
    w = client.get("/documents/%d/paragraphs?locale=%s" % (fx2["doc_id"], LOC))  # noqa: UP031
    assert "revert-all-damaged" in w.text or "Revert all" in w.text, "batch button missing"
    print("  16. POST /documents/{id}/paragraphs/revert-all-damaged reverts atomically")
    print("      and refuses (writing nothing) above the ceiling; the button is shown OK")


# ===========================================================================
# 17. Review LOW - blank_line and no_segment reach the REAL path, not only the
#     classification table.
# ===========================================================================
def test_blank_line_and_no_segment_integration():
    from app.services import fulltext_paragraphs as ftp

    fx = _damaged_fixture()
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        r = ftp.save_paragraph(s, d, LOC, 0, "primera\n\nsegunda", actor="tester@example.com")
    assert r["ok"] is False and r["reason"] == "blank_line", r
    assert ftp.outcome_class(r) == "invalid", r
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        v = ftp.split_view(s, d, "zz-ZZ")  # no segment for this locale
    assert v["ok"] is False and v["reason"] == "no_segment", v
    assert ftp.outcome_class(v) == "not_found", v
    print("  17. blank_line (save with an embedded blank line) and no_segment (a locale")
    print("      with no segment) are exercised through the REAL seam OK")


# ===========================================================================
# 18. Reviewer LOW-1 / LOW-2: clearing a full-text body is a CLEAR, not a break.
# ===========================================================================
def test_clear_body_skips_tag_guard_and_stores_empty():
    """LOW-1: the tag-damage confirm gate must NOT block a CLEAR. Clearing a TAGGED
    single-paragraph body succeeds WITHOUT confirm -- the guard is skipped when the candidate
    whole body is whitespace-only, mirroring `documents.segment_save`'s `if target_text.strip():`.
    LOW-2: a fully-cleared body stores "" EXACTLY (not the joiner residue "\\n\\n"), so it is
    falsy for `build_translated_file`'s `target_text or source_text` fallback and matches the
    segmented editor's stored empty target."""
    from app.services import fulltext_paragraphs as ftp

    # LOW-1 -- a single TAGGED paragraph (three run pairs), currently translated with all tokens
    # intact, cleared with NO confirm flag. Pre-fix `_blocking_reason(source, "")` reported the
    # six tokens "dropped" and refused with needs_confirm; the clear was unreachable.
    clean0 = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ se lanza hoy.⟦/r3⟧"
    fx = _fixture(p0_runs=P0_RUNS, plain=[], target_paras=[clean0])
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert segment.find_placeholders(seg.source_text), "fixture must be a TAGGED body"
        d = s.get(Document, fx["doc_id"])
        r = ftp.save_paragraph(s, d, LOC, 0, "", actor="tester@example.com")
    assert r["ok"] is True, ("clearing a tagged body must succeed WITHOUT confirm", r)
    assert "needs_confirm" not in r, r
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        assert seg.target_text == "", (
            "a cleared single-paragraph body stores '' exactly",
            repr(seg.target_text),
        )
        assert seg.status == SegStatus.NEW, seg.status
        # the tag_integrity flag the write seam merged on the empty body is cleared, not stranded
        assert not [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE], (
            seg.qa_flags
        )
    print("  18a. clearing a TAGGED single-paragraph body succeeds WITHOUT confirm -> NEW, ''")
    print("       target, no stale tag badge OK")

    # LOW-2 -- a 2-paragraph body. Clear ONE paragraph: body still non-empty, so it stays EDITED
    # and the positional map is preserved ("\n\nCuerpo"). Then clear the SECOND: the whole body
    # is whitespace-only, so the seam normalizes it to "" (not "\n\n") and status resets to NEW.
    fx2 = _fixture(
        p0_runs=[("Title", "bold")], plain=["Body para."], target_paras=["Titulo", "Cuerpo"]
    )
    with Session(engine) as s:
        d = s.get(Document, fx2["doc_id"])
        r0 = ftp.save_paragraph(s, d, LOC, 0, "", actor="tester@example.com")
    assert r0["ok"] is True, r0
    with Session(engine) as s:
        seg = s.get(Segment, fx2["seg_id"])
        assert seg.target_text == "\n\nCuerpo", (
            "a PARTIAL clear keeps the positional map",
            repr(seg.target_text),
        )
        assert seg.status == SegStatus.EDITED, (
            "one cleared paragraph of two stays EDITED",
            seg.status,
        )
    with Session(engine) as s:
        d = s.get(Document, fx2["doc_id"])
        r1 = ftp.save_paragraph(s, d, LOC, 1, "", actor="tester@example.com")
    assert r1["ok"] is True, r1
    with Session(engine) as s:
        seg = s.get(Segment, fx2["seg_id"])
        assert seg.target_text == "", (
            "a fully-cleared 2-paragraph body must store '' EXACTLY, not joiner residue '\\n\\n'",
            repr(seg.target_text),
        )
        assert seg.status == SegStatus.NEW, seg.status
    print("  18b. partial clear keeps the map (EDITED, '\\n\\nCuerpo'); a full clear stores ''")
    print("       exactly (no joiner residue) -> NEW OK")


# ===========================================================================
# RBAC: the FULL_TEXT paragraph WRITE routes are can_write (>= translator) on /api,
# parity with their web twins (login-wall middleware) + the approve/apply-tm siblings.
# A viewer docs-key was overwriting target text (lane-W probe); now 403 with no write.
# ===========================================================================
_FIXED_P0 = "⟦r1⟧El ⟦/r1⟧⟦r2⟧Acme Pro⟦/r2⟧⟦r3⟧ conector se lanza hoy.⟦/r3⟧"


def test_paragraph_write_rbac():
    """can_write parity for POST /api/.../paragraphs/{i}/save, /{i}/revert,
    /revert-all-damaged: a VIEWER key is 403'd on every write route (uniform role 403
    BEFORE the doc lookup) with NO change to the stored target; translator/reviewer/
    manager reach the route. The web twins are gated by the login-wall middleware at
    can_write (a viewer session 403s, a translator reaches the route) -- asserted here
    too so both surfaces refuse identically. GET paragraphs (read) stays open."""
    from app.models import Role
    from app.services import access, apikeys
    from app.services import security as sec
    from tests._csrf import csrf_for

    PW = "para-rbac-pw-12345"
    roles = [
        (Role.VIEWER, False),
        (Role.TRANSLATOR, True),
        (Role.REVIEWER, True),
        (Role.MANAGER, True),
    ]

    def _mkuser(role):
        email = f"{role}@para.test"
        u = sec.get_by_email(email) or sec.create_user(email, email, role, PW)
        _r, raw = apikeys.create_key(u.id, "para-%d" % _seq(), scopes="api docs")  # noqa: UP031
        return email, raw

    creds = {role: _mkuser(role) for role, _a in roles}

    def _para0(doc_id):
        with Session(engine) as s:
            seg = s.exec(
                select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == LOC)
            ).one()
            return (seg.target_text or "").split("\n\n")[0]

    def _fresh(role_email):
        """A fresh damaged FULL_TEXT doc with `role_email` granted project visibility
        (project_scoping defaults ON, so a translator/reviewer/manager needs membership;
        a viewer is 403'd at the role gate before visibility is ever consulted)."""
        fx = _damaged_fixture()
        with Session(engine) as s:
            access.add_member(s, fx["project_id"], role_email, added_by="test")
        return fx

    def _api_write(route, doc_id, raw):
        h = {"X-Loc-Token": raw}
        if route == "save":
            return client.post(
                "/api/documents/%d/paragraphs/0/save?locale=%s" % (doc_id, LOC),  # noqa: UP031
                headers=h,
                json={"text": _FIXED_P0},
            )
        if route == "revert":
            return client.post(
                "/api/documents/%d/paragraphs/0/revert?locale=%s" % (doc_id, LOC),  # noqa: UP031
                headers=h,  # noqa: RUF100, UP031
            )
        return client.post(
            "/api/documents/%d/paragraphs/revert-all-damaged?locale=%s" % (doc_id, LOC),  # noqa: UP031
            headers=h,  # noqa: RUF100, UP031
        )

    client = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        # --- /api matrix: 4 roles x 3 write routes ---
        for route in ("save", "revert", "revert-all"):
            for role, allowed in roles:
                email, raw = creds[role]
                fx = _fresh(email)
                before = _para0(fx["doc_id"])
                r = _api_write(route, fx["doc_id"], raw)
                if allowed:
                    assert r.status_code != 403, (route, role, r.status_code, r.text[:200])
                else:
                    assert r.status_code == 403 and r.json().get("error"), (
                        route,
                        role,
                        r.status_code,
                        r.text[:200],
                    )
                    assert _para0(fx["doc_id"]) == before, (
                        f"refused {route} by {role} must not change the target"
                    )
        print(
            "  RBAC api: viewer 403 (no write) on save/revert/revert-all; "
            "translator/reviewer/manager reach the route: OK"
        )

        # --- web parity: middleware gates a viewer, a translator reaches the route ---
        def _weblogin(email):
            c = TestClient(app)
            csrf_for(c)
            c.post(
                "/auth/login",
                data={"email": email, "password": PW, "next": "/"},
                follow_redirects=False,
            )
            csrf_for(c)
            return c

        for role, allowed in ((Role.VIEWER, False), (Role.TRANSLATOR, True)):
            email, _raw = creds[role]
            fx = _fresh(email)
            before = _para0(fx["doc_id"])
            c = _weblogin(email)
            r = c.post(
                "/documents/%d/paragraphs/0/save" % fx["doc_id"],  # noqa: UP031
                data={"locale": LOC, "text": _FIXED_P0},
                follow_redirects=False,
            )
            if allowed:
                assert r.status_code != 403, (role, r.status_code)
            else:
                assert r.status_code == 403, (role, r.status_code)
                assert _para0(fx["doc_id"]) == before, "web viewer refusal wrote nothing"
        print(
            "  RBAC web: viewer session 403 (middleware); translator reaches the "
            "route -- both surfaces refuse a viewer identically: OK"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


# Train 1 retires the FULL_TEXT launch-refusal tests; see verify_fulltext_tm_offers.


def _plain_llm_workflow(name="FT-MT"):
    with Session(engine) as s:
        wf = wf_svc.create_workflow(s, name="%s-%d" % (name, _seq()))  # noqa: UP031
        wf_svc.add_step(
            s, wf.id, name="MT", step_type=StepType.LLM_AGENT, prompt_text="Translate: {text}"
        )
        return wf.id


class _FakeEngine:
    """Swap `orchestrator.run_completion` for a fake whose output is `fn(payload['text'])`.

    Returns the FULL result shape the FULL_TEXT loop reads (text / provider / model /
    attempts / usage / finish_reason / error_class), so the run walks the real write-back."""

    def __init__(self, fn):
        self.fn = fn
        self._orig = None

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            return {
                "text": self.fn((payload or {}).get("text", "")),
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
                "cost_usd": 0.0,
                "finish_reason": "stop",
                "error_class": None,
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def _drive_fulltext_run(doc_id, wf_id):
    """Run a workflow over a FULL_TEXT doc through the real stream.
    Returns (run_id, warnings, status)."""
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale=LOC,
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
        list(orchestrator.run_workflow_stream(s, wf_id, doc_id, LOC, run_id=rid, is_resume=False))
        s.refresh(run)
        return rid, list((run.context or {}).get("warnings") or []), run.status


def test_issue107_paragraph_desync_warns_at_writeback():
    """ISSUE #107 (stress-0906, sev:medium): a FULL_TEXT run whose engine SPLIT paragraphs
    (the reporter's 8.2k-word body: 111 source paragraphs -> 120 translated) reported
    `completed` with `warnings: []` and `finish_reason: stop`, the document read translated
    1/1 with 0 empties, and the ONLY surface that knew the body was unusable was the export
    -- 30 minutes later, twice (the desync is deterministic for that model + document).

    The count comparison now ALSO runs at write-back, through the same shared `align_parts`
    predicate the exporter uses, and is recorded as a run-level warning on every surface.
    Counterfactual: without the orchestrator check the run's warnings list is empty while
    `build_translated_file` quarantines that very body."""
    fx = _fixture(p0_runs=[("Intro paragraph one.", "bold")])
    wf = _plain_llm_workflow("FT-Desync")
    # The engine returns the working text PLUS one extra paragraph -- tokens/sentinels
    # untouched, so nothing but the paragraph COUNT differs (an isolated repro of the
    # long-input paragraph splitting the reporter measured).
    with _FakeEngine(lambda t: t + "\n\nUn parrafo extra que el modelo invento."):
        rid, warns, status = _drive_fulltext_run(fx["doc_id"], wf)
    assert status == RunStatus.COMPLETED, status
    hit = [w for w in warns if w.get("code") == "fulltext_paragraph_desync"]
    assert hit, f"the desync must be a run-level warning, got {warns}"
    assert hit[0]["parts"] == len(fx["units"]) + 1, hit[0]
    assert hit[0]["units"] == len(fx["units"]), hit[0]
    assert hit[0]["desync"] == "over", hit[0]
    assert "paragraph count desync" in hit[0]["message"], hit[0]
    # The exporter agrees (the two read ONE predicate): the same body quarantines.
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        try:
            export.build_translated_file(s, d, LOC)
            raise AssertionError("the desynced body must still quarantine at export")
        except export.TagExportQuarantine as exc:
            assert exc.kind == "count", exc.kind
    # FOUR SURFACES read the one seam (the shape Item A's warning already ships on).
    client = TestClient(app)
    r = client.get("/api/runs/%d" % rid, headers=_docs_key())  # noqa: UP031
    assert r.status_code == 200, r.text[:200]
    assert any(
        "paragraph count desync" in (w.get("message") or "")
        for w in (r.json().get("warnings") or [])
    ), r.json()
    rw = client.get("/runs/%d" % rid)  # noqa: UP031
    assert rw.status_code == 200 and 'id="run-warnings"' in rw.text, rw.status_code
    assert "paragraph count desync" in rw.text, rw.text[:400]
    code, out, err = run_cli(["--json", "workflow", "show-run", str(rid)])
    assert code == 0, (code, out, err)
    payload = _json.loads(out.strip().splitlines()[-1])
    assert any(
        "paragraph count desync" in (w.get("message") or "") for w in payload.get("warnings", [])
    ), payload
    code, out, err = run_cli(["workflow", "show-run", str(rid)])
    assert code == 0 and "paragraph count desync" in (out + err), (code, out, err)
    print("  21a. #107: a FULL_TEXT paragraph-count desync is a run-level WARNING at")
    print("       write-back, on all four surfaces, agreeing with the export guard OK")


def test_issue107_clean_run_stays_silent():
    """The other half: a run whose body ALIGNS records NOTHING (a warning nobody can trust is
    worse than none), so a clean run's `context` is byte-identical to before the check."""
    fx = _fixture(p0_runs=[("Intro paragraph one.", "bold")])
    wf = _plain_llm_workflow("FT-Clean")
    with _FakeEngine(lambda t: t):  # echo: same paragraph count
        _rid, warns, status = _drive_fulltext_run(fx["doc_id"], wf)
    assert status == RunStatus.COMPLETED, status
    assert not [w for w in warns if w.get("code") == "fulltext_paragraph_desync"], warns
    with Session(engine) as s:
        d = s.get(Document, fx["doc_id"])
        export.build_translated_file(s, d, LOC)  # exports clean, no quarantine
    # A blank / skeleton-less body is NOT this predicate's business either.
    from app.services import fulltext_paragraphs as _ftp

    assert _ftp.count_desync("", fx["units"]) is None
    assert _ftp.count_desync("a\n\nb", []) is None
    print("  21b. #107: an aligned body records no warning; blank/skeleton-less abstain OK")


def test_issue107_attribution_scoped_to_the_run_that_wrote_the_body():
    """ISSUE #107, round-1 LOW-3: the warning may only blame "the engine" where THIS run
    committed the body.

    The check sits after the vendor-RETURN and status-protected branches, so a run whose
    write-back the seam REFUSED (a human-approved / edited body) still emitted "the engine split
    / merged or dropped paragraphs" about text this run never wrote. The count statement is true
    of the stored body on every branch -- and the export will still quarantine it, which is what
    the operator needs -- but the attribution is not.

    Counterfactual: with the attribution un-scoped this warning reads "the engine split
    paragraphs" for a body the seam withheld the engine's output from."""
    fx = _fixture(p0_runs=[("Intro paragraph one.", "bold")])
    # a HUMAN-APPROVED body that is already desynced (one paragraph too many)
    desynced = "\n\n".join(
        [(u.get("text") or "x") for u in fx["units"] if (u.get("text") or "").strip()]
        + ["Un parrafo de mas que un humano dejo."]
    )
    with Session(engine) as s:
        seg = s.get(Segment, fx["seg_id"])
        seg.target_text = desynced
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    wf = _plain_llm_workflow("FT-Desync-Protected")
    with _FakeEngine(lambda t: t):  # the engine's own output is ALIGNED; the seam refuses it
        _rid, warns, status = _drive_fulltext_run(fx["doc_id"], wf)
    assert status == RunStatus.COMPLETED, status
    hit = [w for w in warns if w.get("code") == "fulltext_paragraph_desync"]
    assert hit, f"the stored desync must still be reported, got {warns}"
    w = hit[0]
    assert w["written_by_this_run"] is False, w
    assert "the engine" not in w["message"], w["message"]
    assert "This run did not write the body" in w["message"], w["message"]
    assert "QUARANTINED" in w["message"], w["message"]
    # the human's body is untouched, and the counts are still the shared predicate's
    with Session(engine) as s:
        assert (s.get(Segment, fx["seg_id"]).target_text or "") == desynced
    from app.services import fulltext_paragraphs as _ftp

    assert (w["parts"], w["units"]) == _ftp.paragraph_counts(desynced, fx["units"])
    print("  21c. #107: the desync warning blames the ENGINE only on the branch that actually")
    print("       committed a body; a protected/withheld body is reported without that claim OK")


def main() -> int:
    print("[1] structured TagExportQuarantine (C-0)")
    test_structured_quarantine()
    print("[2] fulltext_paragraphs.split_view - the shared seam (C-1)")
    test_split_view_seam()
    print("[3] recovery asserted on the COMMITTED row (C-1, load-bearing)")
    test_recovery_at_the_boundary()
    print("[4] a hand repair clears the stale flags")
    test_manual_repair_clears_flags()
    print("[5] web + CLI + api parity")
    test_parity_cli_and_api()
    print("[6] explicit source-fallback override, ceiling, guard-A exclusivity (C-3)")
    test_source_fallback_override_and_ceiling()
    print("[7] byte-identity / no-op cases")
    test_no_op_cases()
    print("[8] BOUNDARY: emptied span seen in FULL_TEXT; a reorder is damage")
    test_emptied_span_is_seen_in_fulltext()
    print("[9] the substitution record, the audit kind, the one classification")
    test_report_channel_and_classification()
    print("[10] HIGH-1: revert ceiling + durable report + --all-damaged ATOMICITY")
    test_revert_ceiling_durable_report_and_atomic_batch()
    print("[11] MEDIUM-1: copy-source + save cannot launder the fallback flag")
    test_copy_source_save_keeps_the_flag()
    print("[12] MEDIUM-2: the recovery verbs are --remote-capable")
    test_remote_recovery_verbs()
    print("[13] HIGH-1 (F-3, refusal door): --all-damaged atomic on write_refused")
    test_all_damaged_atomic_on_write_refused()
    print("[14] MEDIUM: editor whole-segment save uses the per-paragraph residual")
    test_segment_save_fulltext_per_paragraph_residual()
    print("[15] MEDIUM/LOW: zero-width edit cannot launder the source_fallback flag")
    test_zero_width_source_save_keeps_flag()
    print("[16] MEDIUM: --all-damaged has an atomic WEB equivalent")
    test_web_revert_all_damaged()
    print("[17] LOW: blank_line + no_segment reach the real path")
    test_blank_line_and_no_segment_integration()
    print("[18] reviewer LOW-1/LOW-2: clearing a body skips the tag guard + stores ''")
    test_clear_body_skips_tag_guard_and_stores_empty()
    print("[19] paragraph WRITE routes RBAC (can_write parity)")
    test_paragraph_write_rbac()
    print("[21] #107: FULL_TEXT paragraph-count desync surfaced at write-back")
    test_issue107_paragraph_desync_warns_at_writeback()
    test_issue107_clean_run_stays_silent()
    test_issue107_attribution_scoped_to_the_run_that_wrote_the_body()
    print("FULLTEXT RECOVERY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
