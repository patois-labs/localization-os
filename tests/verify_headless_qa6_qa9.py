"""Headless-surface regressions for QA-6 (#36) and QA-9 (#39) — offline, hermetic.

Both findings are web+CLI+/api PARITY gaps: state the web editor / builder shows a
linguist, but the headless surfaces (`doc show`, `workflow show --json`, the mirroring
/api endpoints) dropped. This suite pins the fix on all three surfaces so it can't
regress, and each core assertion FAILS on the pre-fix tree (proven out-of-band by
restoring the HEAD versions of the four owned files and re-running — see the fixer's
report).

QA-6 — per-segment status + qa_flags are invisible headless. `GET
/api/documents/{id}/segments` and `doc show` returned only `{segments, translated}`
counts, so an operator saw "N translated" while segments sat quarantined in
NEEDS_REVIEW with a dnt / glossary / refusal qa_flag. The fix adds an OPT-IN
`?detail=segments` (`doc show --detail`) per-segment payload (mirroring the batch
`?detail=steps` precedent — the light counts shape stays byte-identical without it).
Asserted here: the flag reveals the per-segment rows + a calm human summary line, the
light shape is unchanged without the flag, and the local / --remote / /api arms agree.

QA-9 — `workflow show --json` omitted `needs_review` + `adapted_from_id`. The fix adds
both to the CLI step dict AND the /api step payload (row-shape parity, the P6 guard).
Asserted here: both fields round-trip on both surfaces with an identical key set.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). The
--remote arm is driven over an httpx.MockTransport forwarding to the in-process
TestClient (no live network). ASCII-only prints. Final line: "HEADLESS QA6/QA9 OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_headless_qa69_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Segment,
    SegStatus,
    StepType,
    User,
    Workflow,
    WorkflowStep,
)
from app.services import apikeys, qe, quality, refusal
from app.web.api_cli import review_summary_line

_ROW_KEYS = {"id", "order", "status", "target_present", "qa_flags"}
_FLAG_KEYS = {"type", "severity", "message"}


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- in-process CLI runner ---------------------------------------------------
def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


# --- web seed helpers (identical posture to verify_workflow_segments) --------
def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def new_project(client, name, targets="es-ES"):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": targets,
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def upload(client, pid, paragraphs):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _mock_transport_for(token_client):
    """httpx.MockTransport forwarding the CLI --remote arm to the in-process
    TestClient (mirrors verify_due_dates). Preserves the X-Loc-Token header the
    RemoteClient sets, so the /api gate authenticates the same as a real call."""
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


def main() -> None:
    init_db()
    client = TestClient(app)

    # docs-scoped per-user key for the /api + --remote arms (auth off -> the role
    # gate no-ops; the key just resolves the actor).
    with Session(engine) as s:
        u = User(email="headless@x.com", role="admin", active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "headless", scopes="api docs")
    hdr = {"X-Loc-Token": raw}

    # =====================================================================
    # SEED (QA-6): a doc whose 3 es-ES segments are all quarantined -> the
    # counts alone read "translated" while the review state is hidden.
    #   seg0: dnt_violation, target present
    #   seg1: glossary_violation, target present
    #   seg2: refusal, NO target (suspected non-translation)
    # =====================================================================
    pid = new_project(client, "Headless QA6")
    doc_id = upload(client, pid, ["Alpha source.", "Beta source.", "Gamma source."])
    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "es-ES"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        assert len(segs) == 3, ("expected 3 segments", len(segs))
        segs[0].target_text = "Alfa"
        segs[0].status = SegStatus.NEEDS_REVIEW
        segs[0].qa_flags = quality.merge_dnt_flags(None, ["BrandX"])
        segs[1].target_text = "Beta"
        segs[1].status = SegStatus.NEEDS_REVIEW
        segs[1].qa_flags = quality.merge_glossary_flags(
            None, [{"source_term": "invoice", "target_term": "factura"}]
        )
        segs[2].target_text = ""
        segs[2].status = SegStatus.NEEDS_REVIEW
        segs[2].qa_flags = refusal.merge_flag(None, refusal.refusal_flag(source="mt"))
        for sg in segs:
            s.add(sg)
        s.commit()
        seg_ids = [sg.id for sg in segs]

    EXPECTED_LINE = "3 flagged for review (dnt 1, glossary 1, refusal 1)"

    # ---------------------------------------------------------------------
    # (1) QA-6 core: `doc show --detail --json` surfaces the per-segment rows.
    #     (pre-fix: --detail is an unknown arg -> exit 2, no segment_details)
    # ---------------------------------------------------------------------
    code, out, err = run(["--json", "doc", "show", "--detail", str(doc_id)])
    assert code == 0, ("doc show --detail must succeed", code, err)
    local = _json.loads(out)
    rows = local["locales"]["es-ES"].get("segment_details")
    assert isinstance(rows, list) and len(rows) == 3, (
        "QA-6 REGRESSION: --detail did not surface per-segment rows",
        local,
    )
    for r in rows:
        assert set(r.keys()) == _ROW_KEYS, ("segment row shape", r.keys())
        for f in r["qa_flags"]:
            assert set(f.keys()) == _FLAG_KEYS, ("qa_flag shape", f)
    assert rows[0]["id"] == seg_ids[0] and rows[0]["order"] == 0
    assert rows[0]["status"] == SegStatus.NEEDS_REVIEW
    assert rows[0]["target_present"] is True
    assert [f["type"] for f in rows[0]["qa_flags"]] == [quality.DNT_FLAG_TYPE]
    assert rows[1]["qa_flags"][0]["type"] == quality.GLOSSARY_FLAG_TYPE
    # the refusal-quarantined segment: flagged AND target-empty (invisible in counts)
    assert rows[2]["target_present"] is False, rows[2]
    assert rows[2]["qa_flags"][0]["type"] == refusal.FLAG_TYPE, rows[2]
    print("  (1) doc show --detail --json surfaces per-segment status + qa_flags")

    # ---------------------------------------------------------------------
    # (2) Light shape unchanged WITHOUT the flag (the pre-fix consumer contract).
    # ---------------------------------------------------------------------
    code, out, _ = run(["--json", "doc", "show", str(doc_id)])
    assert code == 0, out
    light = _json.loads(out)
    assert light["locales"]["es-ES"] == {"segments": 3, "translated": 2}, (
        "QA-6: light `doc show --json` must stay counts-only",
        light["locales"],
    )
    # ... and the counts a flagged operator would (mis)read: "2/3 translated".
    print("  (2) doc show --json without --detail: byte-identical counts-only shape")

    # ---------------------------------------------------------------------
    # (3) Calm human summary line on `doc show --detail` (matches the report wording).
    # ---------------------------------------------------------------------
    code, human, _ = run(["doc", "show", "--detail", str(doc_id)])
    assert code == 0, human
    assert EXPECTED_LINE in human, ("QA-6: missing/incorrect summary line", human)
    # clean-locale calm: the plain (no-flag) human output prints no review line.
    _c, plain, _ = run(["doc", "show", str(doc_id)])
    assert "flagged for review" not in plain, plain
    print("  (3) doc show --detail prints the calm '" + EXPECTED_LINE + "' line")

    # ---------------------------------------------------------------------
    # (4) /api parity: ?detail=segments returns the SAME rows; light stays light.
    # ---------------------------------------------------------------------
    r_light = client.get(f"/api/documents/{doc_id}/segments", headers=hdr).json()
    assert r_light["locales"]["es-ES"] == {"segments": 3, "translated": 2}, (
        "QA-6: /api light shape must stay counts-only",
        r_light["locales"],
    )
    r_det = client.get(f"/api/documents/{doc_id}/segments?detail=segments", headers=hdr).json()
    api_rows = r_det["locales"]["es-ES"]["segment_details"]
    assert api_rows == rows, (
        "QA-6: /api detail rows must equal the local `doc show --detail` rows",
        api_rows,
        rows,
    )
    print("  (4) /api ?detail=segments mirrors the CLI rows; light shape unchanged")

    # ---------------------------------------------------------------------
    # (5) --remote parity: the /api-backed CLI arm shows the SAME rows + line.
    # ---------------------------------------------------------------------
    _remote._set_transport(_mock_transport_for(client))
    try:
        code, out, err = run(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "--json",
                "doc",
                "show",
                "--detail",
                str(doc_id),
            ]
        )
        assert code == 0, ("remote doc show --detail --json", code, err)
        remote = _json.loads(out)
        assert remote["locales"]["es-ES"]["segment_details"] == rows, (
            "QA-6: --remote rows must equal the in-process rows",
            remote,
        )
        code, rhuman, err = run(
            ["--remote", "http://127.0.0.1", "--token", raw, "doc", "show", "--detail", str(doc_id)]
        )
        assert code == 0, ("remote doc show --detail (human)", code, err)
        assert EXPECTED_LINE in rhuman, ("QA-6: --remote summary line", rhuman)
        # and the remote light shape stays counts-only without the flag
        code, rlight, _ = run(
            ["--remote", "http://127.0.0.1", "--token", raw, "--json", "doc", "show", str(doc_id)]
        )
        rl = _json.loads(rlight)
        assert "segment_details" not in rl["locales"]["es-ES"], rl
    finally:
        _remote._set_transport(None)
    print("  (5) --remote arm shows the identical rows + summary line (parity)")

    # ---------------------------------------------------------------------
    # (5b) FIX (review LOW): the summary-line header must be fully accounted for
    #      by its parenthetical breakdown. The header counts segments carrying ANY
    #      surfaced flag, but the pre-fix breakdown itemized only dnt/glossary/refusal
    #      — so a segment quarantined solely for a `qe` gate or a `nontranslation`
    #      language-mismatch inflated the header while landing in no bucket, printing
    #      "1 flagged for review (dnt 0, glossary 0, refusal 0)" (a header naming no
    #      reason). First pin the exact per-family strings by calling the shared fn
    #      directly (the reviewer's probe), then prove it end-to-end via doc show.
    # ---------------------------------------------------------------------
    # direct-fn: a qe-only and a nontranslation-only segment must each NAME their
    # own family (pre-fix both rendered "(dnt 0, glossary 0, refusal 0)").
    qe_row = {"qa_flags": [{"type": qe.QE_FLAG_TYPE}]}
    nt_row = {"qa_flags": [{"type": refusal.NONTRANSLATION_FLAG_TYPE}]}
    assert review_summary_line([qe_row]) == "1 flagged for review (qe 1)", (
        "FIX: qe-only segment must name qe",
        review_summary_line([qe_row]),
    )
    assert review_summary_line([nt_row]) == "1 flagged for review (nontranslation 1)", (
        "FIX: nontranslation-only segment must name nontranslation",
        review_summary_line([nt_row]),
    )
    # a clean set still returns None; the original dnt/glossary/refusal wording is
    # unchanged (zero-count qe/nontranslation dropped -> no churn for the calm case).
    assert review_summary_line([]) is None
    assert (
        review_summary_line([{"qa_flags": [{"type": quality.DNT_FLAG_TYPE}]}])
        == "1 flagged for review (dnt 1)"
    )

    # end-to-end: a fresh doc whose es-ES segments are quarantined by qe / language-
    # mismatch only. Pre-fix the header (3) was itemized as "(dnt 1, glossary 0,
    # refusal 0)" — 2 of the 3 flagged segments unaccounted for.
    pid2 = new_project(client, "Headless QA6 summary")
    doc2 = upload(client, pid2, ["One src.", "Two src.", "Three src."])
    with Session(engine) as s:
        g2 = sorted(
            s.exec(
                select(Segment).where(Segment.document_id == doc2, Segment.target_locale == "es-ES")
            ).all(),
            key=lambda x: x.order_index,
        )
        assert len(g2) == 3, ("expected 3 segments", len(g2))
        g2[0].target_text = "Uno"
        g2[0].status = SegStatus.NEEDS_REVIEW
        g2[0].qa_flags = qe.merge_qe_flag(
            None, qe.qe_flag(40, "low adequacy", 60, passed=False, skipped=False)
        )
        g2[1].target_text = "source stayed english"
        g2[1].status = SegStatus.NEEDS_REVIEW
        g2[1].qa_flags = refusal.merge_nontranslation_flag(
            None, refusal.nontranslation_flag(source="mt")
        )
        # a mixed segment: dnt + nontranslation -> both buckets, header still 1 for it
        g2[2].target_text = "Tres BrandX"
        g2[2].status = SegStatus.NEEDS_REVIEW
        g2[2].qa_flags = refusal.merge_nontranslation_flag(
            quality.merge_dnt_flags(None, ["BrandX"]), refusal.nontranslation_flag(source="mt")
        )
        for sg in g2:
            s.add(sg)
        s.commit()

    SUMMARY_LINE = "3 flagged for review (dnt 1, nontranslation 2, qe 1)"
    code, human2, _ = run(["doc", "show", "--detail", str(doc2)])
    assert code == 0, human2
    assert SUMMARY_LINE in human2, (
        "FIX: header must be fully accounted for by the breakdown",
        human2,
    )
    # guard against the pre-fix wording explicitly (glossary/refusal are zero -> dropped;
    # the misleading "(dnt 0, glossary 0, refusal 0)"-style all-zero tail must be gone).
    assert "glossary 0" not in human2 and "refusal 0" not in human2, human2
    print("  (5b) summary-line header is fully accounted for by its breakdown")

    # =====================================================================
    # SEED (QA-9): a 2-step workflow with an LLM-adapted variant step.
    # =====================================================================
    with Session(engine) as s:
        wf = Workflow(name="QA9 wf", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        base = WorkflowStep(
            workflow_id=wf_id,
            order_index=0,
            name="Base",
            step_type=StepType.LLM_AGENT,
            provider="claude",
            prompt_text="Translate: {text}",
        )
        s.add(base)
        s.commit()
        s.refresh(base)
        base_id = base.id
        variant = WorkflowStep(
            workflow_id=wf_id,
            order_index=1,
            name="Variant",
            step_type=StepType.LLM_AGENT,
            provider="claude",
            prompt_text="Adapted: {text}",
            needs_review=True,
            adapted_from_id=base_id,
        )
        s.add(variant)
        s.commit()

    # ---------------------------------------------------------------------
    # (6) QA-9 core: `workflow show --json` round-trips both fields.
    #     (pre-fix: neither key is present on the step dict)
    # ---------------------------------------------------------------------
    code, out, err = run(["--json", "workflow", "show", str(wf_id)])
    assert code == 0, ("workflow show --json", code, err)
    wdoc = _json.loads(out)
    steps = wdoc["steps"]
    assert len(steps) == 2, steps
    for st in steps:
        assert "needs_review" in st and "adapted_from_id" in st, (
            "QA-9 REGRESSION: fields missing from the step dict",
            sorted(st.keys()),
        )
    assert steps[0]["needs_review"] is False and steps[0]["adapted_from_id"] is None, (
        "QA-9: base step provenance",
        steps[0],
    )
    assert steps[1]["needs_review"] is True and steps[1]["adapted_from_id"] == base_id, (
        "QA-9: variant step provenance round-trip",
        steps[1],
    )
    print("  (6) workflow show --json round-trips needs_review + adapted_from_id")

    # ---------------------------------------------------------------------
    # (7) /api workflow show carries the same fields AND the identical key set
    #     (the P6 row-shape guard: CLI --json and /api step rows must match).
    # ---------------------------------------------------------------------
    api = client.get(f"/api/workflows/{wf_id}", headers=hdr).json()
    assert len(api["steps"]) == 2, api
    for ls, as_ in zip(steps, api["steps"], strict=False):
        assert set(ls.keys()) == set(as_.keys()), (
            "QA-9: CLI/api step-row key mismatch",
            set(ls.keys()) ^ set(as_.keys()),
        )
    av = api["steps"][1]
    assert av["needs_review"] is True and av["adapted_from_id"] == base_id, (
        "QA-9: /api variant provenance",
        av,
    )
    print("  (7) /api workflow show mirrors the fields + step-row key set (P6 guard)")

    print("HEADLESS QA6/QA9 OK")


if __name__ == "__main__":
    main()
