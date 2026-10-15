"""Language-matrix run monitor verification (SPEC-workflows-redesign Phase 5b).

Hermetic + offline: throwaway LOC_TMS_DATA_DIR + provider keys blanked, so every member
run resolves through MockProvider (or an injected instrumenting provider) and ZERO live
calls are made. Covers the whole 5b surface ON TOP of 5a's dispatch (verify_batch_runs):

  detail payload shape      batch_as_dict(detail=steps) attaches per-member step_runs
                            [step_id/order_index/name/status/score/gate/outcome_index/visits]
                            + top-level requested_steps; the LIGHT shape (no flag) carries
                            NEITHER (regression — the light poll stays light).
  build_matrix (pure)       clean grid when variants are identical; an honest UNION column
                            axis under structural divergence with NO misaligned cell (every
                            cell sits under the column whose (order,type,name) it equals);
                            score / loop-visit (x2) / absent-step (None) cells; unlaunched row
                            with a reason.
  matrix page               renders a row for EVERY requested locale incl. an un-launched one;
                            the static (no-JS) page is a complete matrix; the live poll script
                            is present only while RUNNING and is bounded.
  re-run one locale         a TERMINAL member -> a NEW member run in the SAME batch (batch_id
                            stamped), the prior run kept in history, the matrix shows the LATEST
                            with an attempt count; REFUSED on an ACTIVE member.
  reopen / re-finalize      a re-run on a FINISHED batch clears finished_at for the duration
                            then re-finalizes; a healed FAILED locale re-rolls the batch to
                            COMPLETED (latest attempt per locale, no double-count).
  retry-failed              re-dispatches EXACTLY the failed locales (the non-failed untouched).
  web + /api + CLI parity   the web rerun/retry routes; the /api rerun + retry-failed shapes;
                            the CLI `workflow batch show/rerun/retry-failed` round-trip + the
                            three new verify_cli_parity leaf entries.
  RBAC                      a viewer key is 403 on a write; a foreign-org key is 404 on rerun.

Final line: "BATCH MATRIX OK".
"""

import io
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bmatrix_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Organization,
    Project,
    RunStatus,
    Segment,
    TargetLocale,
    TransMode,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import apikeys, batch_runs, security, settings
from app.services import workflows as W
from app.services.providers import Provider, register_provider

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0


def _uniq(prefix):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


# --- a provider that fails a configurable set of source texts (heal by clearing it) ----
class HealProvider(Provider):
    """Fails any segment whose source text is in `fail_texts` (all attempts); succeeds
    otherwise. Clearing `fail_texts` between a batch and a re-run turns a FAILED locale
    COMPLETED — the deterministic hook for the reopen / re-finalize / retry-heal tests."""

    name = "healmock"
    fail_texts: set = set()  # noqa: RUF012

    @classmethod
    def reset(cls, fail_texts=None):
        cls.fail_texts = set(fail_texts or [])

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        text = (payload or {}).get("text", "") or ""
        if text in HealProvider.fail_texts:
            raise RuntimeError("configured failure -> member must FAIL")
        return "ok-" + text


register_provider("healmock", HealProvider())


# --- fixtures -----------------------------------------------------------------
def _mk_project_doc(locales, source="en-US", fail_loc=None, org_id=None):
    """A project (+ TargetLocale rows) + a SEGMENTED doc with one segment per locale. The
    fail_loc's segment source is the HealProvider FAIL marker; every other segment is plain."""
    with Session(engine) as s:
        p = Project(name=_uniq("BMProj"), source_locale=source)
        if org_id is not None:
            p.org_id = org_id
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        doc = Document(
            project_id=p.id,
            filename="bm.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for loc in locales:
            src = "FAILME" if loc == fail_loc else "Hello world."
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=0,
                    source_text=src,
                    target_text="",
                )
            )
        s.commit()
        return p.id, doc.id


def _mk_wf(name, steps, provider="mock", src=None, tgt=None, retry_max=0):
    """A workflow with `steps` = [(name, step_type, prompt), ...]. retry_max defaults 0 so a
    failed step FAILS the member immediately (no long retry loop)."""
    with Session(engine) as s:
        w = W.create_workflow(s, name)
        if src is not None or tgt is not None:
            w.source_locale, w.target_locale = src, tgt
            s.add(w)
            s.commit()
        for sname, stype, prompt in steps:
            st = W.add_step(
                s,
                w.id,
                name=sname,
                step_type=stype,
                provider=provider,
                model="m1",
                prompt_text=prompt,
                chunk_policy="none",
            )
            st.retry_max = retry_max
            st.backoff_base = 0.0
            s.add(st)
            s.commit()
        return w.id


_T = [("Translator", "llm_agent", "Translate {text}")]
_TF = [
    ("Translator", "llm_agent", "Translate {text}"),
    ("Finalizer", "llm_agent", "Finalize {text}"),
]
_TXF = [
    ("Translator", "llm_agent", "Translate {text}"),
    ("Xtra", "llm_agent", "Extra {text}"),
    ("Finalizer", "llm_agent", "Finalize {text}"),
]


def _members(batch_id):
    with Session(engine) as s:
        return list(
            s.exec(
                select(WorkflowRun).where(WorkflowRun.batch_id == batch_id).order_by(WorkflowRun.id)
            ).all()
        )


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _ck(s):
    """Mirror of batch_runs._col_key — a column's identity, for the no-misalignment proof."""
    return f"{s.get('order_index')}|{s.get('step_type')}|{s.get('name', '')}"


# ============================================================================
# 1. detail payload shape — step_runs + requested_steps behind the flag; light unchanged
# ============================================================================
def test_detail_payload_shape():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("DetailWf"), _TF, provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        light = batch_runs.batch_as_dict(s, batch)
        heavy = batch_runs.batch_as_dict(s, batch, detail=True)
    # LIGHT shape: no per-member step_runs, no requested_steps (regression — stays light).
    assert "requested_steps" not in light, "light shape must not carry requested_steps"
    assert all("step_runs" not in m for m in light["members"]), "light members must be step-free"
    # every member still carries the 5a keys + the new attempt/is_latest markers
    for m in light["members"]:
        assert {
            "locale",
            "run_id",
            "status",
            "current_step_index",
            "resolved_workflow_id",
            "attempt",
            "is_latest",
        } <= set(m), m
    # HEAVY shape: requested_steps baseline + per-member step_runs with the required fields
    assert len(heavy["requested_steps"]) == 2, heavy["requested_steps"]
    for m in heavy["members"]:
        srs = m["step_runs"]
        assert len(srs) == 2, ("one cell per plan step", srs)
        need = {
            "step_id",
            "order_index",
            "name",
            "step_type",
            "status",
            "score",
            "gate",
            "outcome_index",
            "visits",
        }
        assert all(need <= set(sr) for sr in srs), srs
        assert [sr["name"] for sr in srs] == ["Translator", "Finalizer"], srs
        assert all(sr["status"] == RunStatus.COMPLETED for sr in srs), srs
    print(
        "  detail: heavy shape adds requested_steps + per-member step_runs; light shape "
        "carries neither (regression) OK"
    )


# ============================================================================
# 2. build_matrix — clean grid when every variant is identical
# ============================================================================
def test_build_matrix_clean_grid():
    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("GridWf"), _TF, provider="mock")  # ungrouped -> every locale same plan
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        view = batch_runs.batch_as_dict(s, batch, detail=True)
    matrix = batch_runs.build_matrix(view)
    assert not matrix["diverged"], "identical variants must not read as diverged"
    assert [c["name"] for c in matrix["columns"]] == ["Translator", "Finalizer"], matrix["columns"]
    assert len(matrix["rows"]) == 3, "one row per requested locale"
    for row in matrix["rows"]:
        assert not row["diverges"] and not row["unlaunched"]
        assert row["attempt"] == 1 and not row["rerun"]
        # every column has a real cell (a clean grid) — no None
        assert all(row["cells"][c["key"]] is not None for c in matrix["columns"]), row
        assert all(
            row["cells"][c["key"]]["status"] == RunStatus.COMPLETED for c in matrix["columns"]
        ), row
    print(
        "  build_matrix: identical variants -> a clean 3x2 grid, no divergence, every cell "
        "filled OK"
    )


# ============================================================================
# 3. build_matrix — honest UNION axis under structural divergence, NO misaligned cell
# ============================================================================
def test_build_matrix_divergent_axis():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    with Session(engine) as s:
        grp = W.create_group(s, _uniq("DivGrp"))
        gid = grp.id
    es = _mk_wf("Div es", _TF, src="en-US", tgt="es-ES")  # T, F  (the baseline)
    fr = _mk_wf("Div fr", _TXF, src="en-US", tgt="fr-FR")  # T, X, F (an inserted step)
    with Session(engine) as s:
        W.add_to_group(s, es, gid, source_locale="en-US", target_locale="es-ES", make_default=True)
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR")
        batch = batch_runs.start_batch(
            s, workflow_id=es, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        view = batch_runs.batch_as_dict(s, batch, detail=True)
    matrix = batch_runs.build_matrix(view)
    assert matrix["diverged"], "an inserted variant step must read as diverged"
    # The union splits the two Finalizers (order 1 vs order 2) into DISTINCT columns, so a
    # cell is never placed under a differently-positioned step of the same name.
    keys = [c["key"] for c in matrix["columns"]]
    assert keys == [
        "0|llm_agent|Translator",
        "1|llm_agent|Finalizer",
        "1|llm_agent|Xtra",
        "2|llm_agent|Finalizer",
    ], keys
    rows = {r["locale"]: r for r in matrix["rows"]}
    es_row, fr_row = rows["es-ES"], rows["fr-FR"]
    assert not es_row["diverges"], "the baseline variant does not diverge from itself"
    assert fr_row["diverges"], "the fr variant (extra step) diverges"
    # es's Finalizer sits under (1|Finalizer), NOT under fr's (2|Finalizer); es lacks Xtra + (2|F)
    assert es_row["cells"]["1|llm_agent|Finalizer"] is not None
    assert es_row["cells"]["2|llm_agent|Finalizer"] is None
    assert es_row["cells"]["1|llm_agent|Xtra"] is None
    # fr's Finalizer sits under (2|Finalizer); fr lacks (1|Finalizer)
    assert fr_row["cells"]["2|llm_agent|Finalizer"] is not None
    assert fr_row["cells"]["1|llm_agent|Finalizer"] is None
    assert fr_row["cells"]["1|llm_agent|Xtra"] is not None
    # THE NO-MISALIGN INVARIANT: every non-None cell's underlying step key EQUALS its column key.
    by_run_step = {}
    for m in view["members"]:
        for sr in m["step_runs"]:
            by_run_step[(m["run_id"], sr["step_id"])] = _ck(sr)
    for r in matrix["rows"]:
        for colkey, cell in r["cells"].items():
            if cell is not None:
                assert by_run_step[(r["run_id"], cell["step_id"])] == colkey, (
                    "a cell is under the WRONG column",
                    r["locale"],
                    colkey,
                )
    print(
        "  build_matrix: divergent variants -> honest union axis (split columns), each cell "
        "under its own step's column, no misalignment OK"
    )


# ============================================================================
# 4. build_matrix (pure) — score / loop-visit / absent-step / unlaunched-reason cells
# ============================================================================
def test_build_matrix_cell_adornments():
    # A hand-built view (build_matrix is pure) exercises score, x2 visits, a None cell, and
    # an un-launched row's reason without needing a live LQA run.
    view = {
        "id": 1,
        "status": "partial",
        "notes": "Not launched: de-DE.",
        "spend_stopped": False,
        "counts": {
            "requested": 3,
            "completed": 1,
            "failed": 0,
            "paused": 0,
            "running": 0,
            "unlaunched": 1,
        },
        "requested_locales": ["es-ES", "fr-FR", "de-DE"],
        "requested_steps": [
            {"step_id": 10, "order_index": 0, "name": "Translate", "step_type": "llm_agent"},
            {"step_id": 11, "order_index": 1, "name": "Check", "step_type": "lqa"},
        ],
        "members": [
            {
                "locale": "es-ES",
                "run_id": 100,
                "status": "completed",
                "attempt": 1,
                "is_latest": True,
                "step_runs": [
                    {
                        "step_id": 10,
                        "order_index": 0,
                        "name": "Translate",
                        "step_type": "llm_agent",
                        "status": "completed",
                        "score": None,
                        "gate": None,
                        "outcome_index": 0,
                        "visits": 1,
                    },
                    {
                        "step_id": 11,
                        "order_index": 1,
                        "name": "Check",
                        "step_type": "lqa",
                        "status": "completed",
                        "score": 92.0,
                        "gate": "passed",
                        "outcome_index": 1,
                        "visits": 2,
                    },
                ],
            },
            {
                "locale": "fr-FR",
                "run_id": 101,
                "status": "completed",
                "attempt": 2,
                "is_latest": True,
                "step_runs": [
                    {
                        "step_id": 20,
                        "order_index": 0,
                        "name": "Translate",
                        "step_type": "llm_agent",
                        "status": "completed",
                        "score": None,
                        "gate": None,
                        "outcome_index": 0,
                        "visits": 1,
                    }
                ],
            },
        ],
    }
    matrix = batch_runs.build_matrix(view)
    rows = {r["locale"]: r for r in matrix["rows"]}
    # es Check cell carries the score + the x2 visit count
    check = rows["es-ES"]["cells"]["1|lqa|Check"]
    assert check["score"] == 92.0 and check["gate"] == "passed" and check["visits"] == 2, check
    # fr (attempt 2) is a re-run row; it LACKS the Check column -> None (no misalignment)
    assert rows["fr-FR"]["rerun"] and rows["fr-FR"]["attempt"] == 2
    assert rows["fr-FR"]["cells"]["1|lqa|Check"] is None
    # de-DE is un-launched with an honest reason drawn from the notes
    de = rows["de-DE"]
    assert de["unlaunched"] and de["run_id"] is None and de["reason"] == "not launched", de
    print(
        "  build_matrix: score + x2 visits on a cell, None where a variant lacks the step, "
        "un-launched row carries a reason OK"
    )


# ============================================================================
# 5. matrix page — a row for EVERY requested locale incl. a skipped/un-launched one
# ============================================================================
def test_matrix_page_all_locales():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("PageWf"), _TF, provider="mock")
    # a pre-existing ACTIVE run for fr-FR NOT in the batch -> fr-FR is SKIPPED (un-launched)
    with Session(engine) as s:
        phantom = WorkflowRun(
            workflow_id=wf, document_id=did, target_locale="fr-FR", status=RunStatus.RUNNING
        )
        s.add(phantom)
        s.commit()
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    client = TestClient(app)
    page = client.get(f"/batches/{bid}")
    assert page.status_code == 200, page.text[:200]
    html = page.text

    class MatrixTables(HTMLParser):
        count = 0

        def handle_starttag(self, tag, attrs):
            classes = set(dict(attrs).get("class", "").split())
            if tag == "table" and {"table", "bm-matrix", "data-table"} <= classes:
                self.count += 1

    matrix_dom = MatrixTables()
    matrix_dom.feed(html)
    assert matrix_dom.count == 1, "the matrix table is missing or duplicated"
    assert "es-ES" in html and "fr-FR" in html, "a requested locale row is missing"
    assert "Translator" in html and "Finalizer" in html, "a step column header is missing"
    # the skipped locale renders an honest 'not launched' row
    assert "not launched" in html, "the un-launched (skipped) locale row is missing its status"
    print(
        "  page: renders a row per requested locale incl. the skipped/un-launched one, "
        "with step column headers OK"
    )


# ============================================================================
# 5b. heatmap tint — populated cells carry their status-family class (viz quick-win)
# ============================================================================
def test_matrix_heatmap_tint():
    """Heatmap tint (VIZ-RESEARCH-2026-07-21 quick-win): each populated cell's
    <td class="bm-cell"> ALSO carries the SAME status-family class the chip inside
    it already uses (stclass), so a light alpha-wash CSS rule can key off it.
    Redundant reinforcement only — the chip + label stay the actual signal; a cell
    with no chip (not in this variant) carries no status class and stays untinted.
    Since the td's class comes from the exact same stclass(c.status) call the chip
    itself renders, the two can never disagree — no new misalignment risk is
    introduced (the no-misalign invariant lives in build_matrix's column keying,
    untouched here)."""
    locales = ["es-ES", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("TintWf"), _T, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    client = TestClient(app)
    html = client.get(f"/batches/{bid}").text
    # a completed cell (es-ES) and a failed cell (de-DE) both carry their
    # status-family tint class on the <td>, matching the chip's own class.
    assert '<td class="bm-cell bm-ok"' in html, "completed cell missing its tint class"
    assert '<td class="bm-cell bm-fail"' in html, "failed cell missing its tint class"
    # style.css carries the alpha-wash rules (a wash, not a solid fill — color-mix
    # against `transparent`, reusing the SAME reserved tokens the chip already uses).
    css_path = os.path.join(_ROOT, "app", "static", "style.css")
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()
    assert "td.bm-cell.bm-ok" in css and "color-mix(in srgb, var(--success)" in css, (
        "the bm-ok heatmap tint rule is missing from style.css"
    )
    assert "td.bm-cell.bm-fail" in css and "color-mix(in srgb, var(--danger)" in css, (
        "the bm-fail heatmap tint rule is missing from style.css"
    )
    print(
        "  heatmap: populated cells carry their status-family tint class on the "
        "<td> (matching the chip's own class); style.css carries the alpha-wash "
        "rules OK"
    )


def _extract_js_function(js_src, name):
    """Brace-balanced extraction of `function name(...) { ... }` from a JS source
    blob — a plain regex can't safely match nested braces, and cellHtml()/apply()
    both nest them."""
    start = js_src.index("function " + name + "(")
    brace = js_src.index("{", start)
    depth, i = 0, brace
    while True:
        if js_src[i] == "{":
            depth += 1
        elif js_src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return js_src[start : i + 1]


# ============================================================================
# 5c. heatmap tint — the LIVE-poll JS keeps className paired with innerHTML
# (reviewer finding: a transitioning cell showed the new chip over a STALE tint)
# ============================================================================
def test_matrix_heatmap_tint_live_js():
    """REGRESSION GUARD (reviewer finding): batch_view.html's live apply() used to
    write td.innerHTML on a step-run update WITHOUT ever touching td.className, so
    a cell transitioning status (e.g. running -> completed) kept its OLD heatmap
    tint class under the NEW chip — test_matrix_heatmap_tint above only proves the
    synchronous server render, never a live in-place update. The fix pairs every
    innerHTML write with `td.className = 'bm-cell ' + stClass(sr.status)`, reusing
    the SAME status->class mapping (stClass) the chip text itself is built from,
    so the two can never drift apart.

    This test EXECUTES the real stClass()/esc()/cellHtml()/apply() functions
    (brace-extracted from batch_view.html's live-poll <script>, exactly as
    shipped) under Node against a hand-stubbed `document` — no jsdom dependency
    in this repo, matching the existing node-harness pattern in verify_ui.py's
    doc-upload size-guard check. If node isn't on PATH, it falls back to a static
    source assertion that the innerHTML/className writes are paired under the
    SAME `if (td)` guard (the reviewer's stated fallback)."""
    html_path = os.path.join(_ROOT, "app", "templates", "batch_view.html")
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    # Expand the shared escaper include before extracting, the same way
    # verify_run_recovery_progress.py:1303 does. batch_view.html used to define its own
    # quote-blind esc(); it now includes app/templates/_html_escape.js so quotes are
    # escaped too, and a raw-source read would otherwise see no `function esc(` at all.
    # The WHOLE include has to land in `html`, not just the function: the Node harness
    # below calls cellHtml() -> esc(), and esc() closes over `const ESCAPES`, so dropping
    # the constant gives a ReferenceError instead of a clean assertion.
    with open(os.path.join(_ROOT, "app", "templates", "_html_escape.js"), encoding="utf-8") as fh:
        html = html.replace("{% include '_html_escape.js' %}", fh.read())
    live_start = html.index("(function () {")
    live_end = html.index("})();", live_start) + len("})();")
    js = html[live_start:live_end]
    for fn in ("stClass", "esc", "cellHtml", "setText", "apply"):
        assert "function " + fn + "(" in js, f"{fn}() missing from the live-poll script"
    apply_src = _extract_js_function(js, "apply")
    cell_block = apply_src[apply_src.index("(m.step_runs") :]
    assert "td.innerHTML = html;" in cell_block, "innerHTML write missing/renamed"
    assert "td.className = 'bm-cell ' + stClass(sr.status);" in cell_block, (
        "className write missing — the exact regression this test guards"
    )
    idx_html, idx_class = cell_block.index("td.innerHTML"), cell_block.index("td.className")
    assert re.search(
        r"if \(td\) \{\s*var html = cellHtml\(sr\);\s*"
        r"if \(td.bmHtml !== html\) \{ td.bmHtml = html; td.innerHTML = html; \}\s*"
        r"td.className = 'bm-cell ' \+ stClass\(sr.status\);\s*\}",
        cell_block,
    ), "HTML is conditional; className must stay paired under the outer td guard"
    assert idx_html < idx_class, (
        "innerHTML should be written before className (order is cosmetic, but pin it)"
    )

    node = shutil.which("node")
    if not node:
        print(
            "  heatmap(live-js): node not on PATH - static pairing check only "
            "(innerHTML+className share one `if (td)` guard) OK"
        )
        return

    with tempfile.TemporaryDirectory(prefix="loctms_bmtint_js_") as tmp:
        extracted_path = os.path.join(tmp, "extracted.js")
        harness_path = os.path.join(tmp, "harness.js")
        # Prepend the WHOLE shared escaper, not just its function. `_extract_js_function`
        # returns one function slice, and `esc()` closes over `const ESCAPES`, which lives
        # a line above it, so a function-only extraction gives
        # `ReferenceError: ESCAPES is not defined` the moment cellHtml() escapes anything.
        # That stayed green purely because the fixture payload had no escapable character.
        # verify_sec_xss.esc_source() already takes this approach.
        with open(
            os.path.join(_ROOT, "app", "templates", "_html_escape.js"), encoding="utf-8"
        ) as fh:
            shared_escaper = fh.read()
        extracted = shared_escaper + "\n"
        extracted += "\n".join(
            _extract_js_function(js, fn) for fn in ("stClass", "cellHtml", "setText", "apply")
        )
        extracted += (
            "\nmodule.exports = { apply: apply, stClass: stClass, "
            "cellHtml: cellHtml, esc: esc, setText: setText };\n"
        )
        with open(extracted_path, "w", encoding="utf-8") as f:
            f.write(extracted)
        rc = subprocess.run([node, "--check", extracted_path], capture_output=True, text=True)
        assert rc.returncode == 0, ("extracted apply()/stClass() failed node --check", rc.stderr)
        harness = r"""
const path = require('path');
const mod = require(path.resolve(process.argv[2]));
function makeTd(initialClass) {
  return { className: initialClass, htmlWrites: 0, html: '',
    get innerHTML() { return this.html; },
    set innerHTML(value) { this.htmlWrites++; this.html = value; }
  };
}
// Cell A: bm-run (running) -> completes. Cell B: NO tint class at all today (the
// server only tints a POPULATED `c`) -> also completes, proving the fix covers a
// cell that started with zero tint, not just a stale one.
const tdA = makeTd('bm-cell bm-run');
const tdB = makeTd('bm-cell');
const chipStub = { className: '', textContent: '' };
const trStub = {
  getAttribute: function (name) {
    if (name === 'data-locale') return 'es-ES';
    if (name === 'data-run-id') return '100';
    return null;
  },
  querySelector: function (sel) {
    if (sel === '.bm-statuscell .chip') return chipStub;
    if (sel.indexOf('data-step-id="10"') !== -1) return tdA;
    if (sel.indexOf('data-step-id="11"') !== -1) return tdB;
    return null;
  },
};
global.document = {
  querySelectorAll: function (sel) { return sel === 'tr[data-locale]' ? [trStub] : []; },
  getElementById: function () { return null; },
};
const payload = {
  counts: {}, status: 'running',
  members: [{
    locale: 'es-ES', run_id: '100', is_latest: true, status: 'running',
    step_runs: [
      { step_id: 10, status: 'completed', score: null, visits: 1 },
      { step_id: 11, status: 'completed', score: 92.0, visits: 1 },
    ],
  }],
};
mod.apply(payload);
tdA.className = 'stale';
mod.apply(payload);
if (tdA.htmlWrites !== 1 || tdB.htmlWrites !== 1) throw Error('unchanged cell rewritten');
if (tdA.className !== 'bm-cell bm-ok') throw Error('unchanged HTML skipped className');
payload.members[0].step_runs[0].status = 'running';
mod.apply(payload);
mod.apply(payload);
if (tdA.htmlWrites !== 2 || tdB.htmlWrites !== 1) throw Error('running cell rewritten');
payload.members[0].step_runs[0].status = 'completed';
mod.apply(payload);
if (tdA.htmlWrites !== 3) throw Error('changed cell not rewritten');
console.log(JSON.stringify({
  aClass: tdA.className, aHtml: tdA.innerHTML,
  bClass: tdB.className, bHtml: tdB.innerHTML,
  // Exercise the escaper directly. esc() closes over `const ESCAPES`, which lives in the
  // shared include rather than inside the function, so a function-only extraction throws
  // ReferenceError here. The tint fixture alone cannot catch that: its status strings
  // contain nothing escapable, so esc() never touches the map.
  escaped: mod.esc('<img src=x>"\'&'),
}));
"""
        with open(harness_path, "w", encoding="utf-8") as f:
            f.write(harness)
        run = subprocess.run([node, harness_path, extracted_path], capture_output=True, text=True)
        assert run.returncode == 0, ("live-js heatmap harness crashed", run.stderr)
        out = _json.loads(run.stdout.strip().splitlines()[-1])
    # Cell A: STALE bm-run tint must be replaced by bm-ok on the running->completed
    # transition — the exact bug the reviewer flagged.
    assert out["aClass"] == "bm-cell bm-ok", ("stale tint survived a status change", out)
    assert "completed" in out["aHtml"], out
    # Cell B: started with NO tint class at all; apply() must still give it one.
    assert out["bClass"] == "bm-cell bm-ok", ("a never-tinted cell stayed untinted", out)
    assert "completed" in out["bHtml"], out
    # The shared escaper is reachable AND quote-aware inside the extracted bundle.
    assert out["escaped"] == "&lt;img src=x&gt;&quot;&#39;&amp;", ("escaper not wired", out)
    print(
        "  heatmap(live-js): apply() executed under node - a status transition "
        "replaces a STALE tint class, and a never-tinted cell gets one, on "
        "every live-poll cell update OK"
    )


# ============================================================================
# 6. no-JS static correctness — the matrix is complete without JS; live poll bounded
# ============================================================================
def test_no_js_static_correctness():
    locales = ["es-ES", "fr-FR", "de-DE"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("NoJsWf"), _TF, provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    client = TestClient(app)
    html = client.get(f"/batches/{bid}").text
    # A finished batch: the FULL matrix is present statically (every locale + a completed
    # cell chip) and there is NO live poll script (terminal -> static snapshot only).
    for loc in locales:
        assert loc in html, f"{loc} row absent from the static page"
    assert "bm-ok" in html, "completed cell chips absent from the static matrix"
    assert "status.json" not in html, "a finished batch must not carry the live poll"
    # A RUNNING batch (finished_at NULL) DOES carry the bounded poll (the tokens 5a asserts).
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf, document_id=did, requested_locales=locales, status="running"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        rid = b.id
    live = client.get(f"/batches/{rid}").text
    assert "status.json" in live and "BATCH_RELOAD_MAX" in live, "live poll/cap missing"
    assert "window.location.reload" in live and "batch-reloads-" in live, "bounded reload missing"
    print(
        "  no-JS: a finished batch renders a complete static matrix (no poll); a running one "
        "carries the bounded poll (reload cap present) OK"
    )


# ============================================================================
# 7. re-run one locale — TERMINAL member -> new run in the SAME batch, attempt count
# ============================================================================
def test_rerun_one_locale():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("RerunWf"), _TF, provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        assert batch.status == "completed" and batch.finished_at is not None
        first_es = {m.id for m in _members(bid) if m.target_locale == "es-ES"}
        batch = batch_runs.rerun_batch(
            s, batch=batch, locales=["es-ES"], created_by="rerunner", synchronous=True
        )
    members = _members(bid)
    es_members = [m for m in members if m.target_locale == "es-ES"]
    fr_members = [m for m in members if m.target_locale == "fr-FR"]
    assert len(es_members) == 2, ("es-ES must have a fresh SECOND member run", es_members)
    assert len(fr_members) == 1, ("fr-FR must be untouched by an es-ES re-run", fr_members)
    assert all(m.batch_id == bid for m in es_members), "the new run must carry the batch id"
    new_es = [m for m in es_members if m.id not in first_es]
    assert len(new_es) == 1 and new_es[0].status == RunStatus.COMPLETED
    # the matrix shows the LATEST es-ES run with attempt 2 (a re-run marker)
    with Session(engine) as s:
        view = batch_runs.batch_as_dict(s, batch, detail=True)
    matrix = batch_runs.build_matrix(view)
    es_row = {r["locale"]: r for r in matrix["rows"]}["es-ES"]
    assert es_row["run_id"] == max(m.id for m in es_members), "row must show the LATEST run"
    assert es_row["attempt"] == 2 and es_row["rerun"], es_row
    # the batch re-finalized (finished_at re-stamped) and stayed completed
    assert view["status"] == "completed" and view["finished_at"] is not None
    print(
        "  rerun: a terminal locale -> a fresh member run in the SAME batch, prior run kept, "
        "matrix shows the latest with attempt 2, batch re-finalized OK"
    )


# ============================================================================
# 8. re-run refused on an ACTIVE member (never re-run an in-flight locale)
# ============================================================================
def test_rerun_refused_on_active():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("ActiveWf"), _T, provider="mock")
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf, document_id=did, requested_locales=locales, status="running"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        # a genuinely in-flight member for es-ES
        s.add(
            WorkflowRun(
                workflow_id=wf,
                document_id=did,
                target_locale="es-ES",
                status=RunStatus.RUNNING,
                batch_id=b.id,
            )
        )
        s.commit()
        try:
            batch_runs.rerun_batch(s, batch=b, locales=["es-ES"], synchronous=True)
            raise AssertionError("re-running an ACTIVE member must raise BatchError")
        except batch_runs.BatchError as exc:
            assert str(exc) == (
                "Re-run is available once the batch has finished; 2 locale(s) still in flight"
            )
        # After batch finalization, the per-member guard still applies (for example,
        # a preserved review member), as does request-locale validation.
        from app.models import _utcnow

        b.finished_at = _utcnow()
        s.add(b)
        s.commit()
        try:
            batch_runs.rerun_batch(s, batch=b, locales=["es-ES"], synchronous=True)
            raise AssertionError("re-running an ACTIVE member must raise BatchError")
        except batch_runs.BatchError as exc:
            assert "es-ES" in str(exc) and "running" in str(exc).lower(), str(exc)
        # a locale NOT in the batch request is also refused
        try:
            batch_runs.rerun_batch(s, batch=b, locales=["zz-ZZ"], synchronous=True)
            raise AssertionError("a locale outside the request must raise")
        except batch_runs.BatchError as exc:
            assert "zz-ZZ" in str(exc), str(exc)
    print("  rerun-guard: an ACTIVE member is refused; a non-request locale is refused OK")


# ============================================================================
# 9. reopen / re-finalize — a healed FAILED locale re-rolls the batch to COMPLETED
# ============================================================================
def test_rerun_reopen_finalize():
    locales = ["es-ES", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("HealWf"), _T, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})  # de-DE (source 'FAILME') fails; es-ES completes
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        assert batch.status == "partial", (batch.status, batch.notes)  # a mix (1 done, 1 failed)
        first_finished = batch.finished_at
        assert first_finished is not None
    # HEAL: clear the failure, then re-run de-DE. The batch reopens (finished_at cleared for the
    # duration) and re-finalizes COMPLETED from the LATEST attempt per locale.
    HealProvider.reset(set())
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, bid)
        batch = batch_runs.rerun_batch(
            s, batch=batch, locales=["de-DE"], created_by="healer", synchronous=True
        )
        assert batch.status == "completed", (batch.status, batch.notes)
        assert batch.finished_at is not None and batch.finished_at != first_finished, (
            "the batch must be RE-finalized (a fresh finished_at)"
        )
        de_members = [m for m in _members(bid) if m.target_locale == "de-DE"]
        assert len(de_members) == 2, de_members
        assert de_members[0].status == RunStatus.FAILED, "the original de-DE attempt stays FAILED"
        assert de_members[-1].status == RunStatus.COMPLETED, "the re-run de-DE attempt COMPLETED"
        view = batch_runs.batch_as_dict(s, batch)
    # counts read the LATEST attempt only — de-DE counts once (COMPLETED), never double.
    assert view["counts"]["completed"] == 2 and view["counts"]["failed"] == 0, view["counts"]
    print(
        "  reopen: a healed FAILED locale re-run reopens + re-finalizes the batch to "
        "COMPLETED; counts read the latest attempt (no double-count) OK"
    )


# ============================================================================
# 10. retry-failed — re-dispatches EXACTLY the failed set; non-failed untouched
# ============================================================================
def test_retry_failed_targets_failed_set():
    locales = ["es-ES", "fr-FR", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("RetryWf"), _T, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})  # only de-DE fails
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        assert batch_runs.failed_locales(s, batch) == ["de-DE"], "only de-DE should be failed"
        # retry-failed (still failing) re-dispatches ONLY de-DE
        batch = batch_runs.rerun_batch(
            s,
            batch=batch,
            locales=batch_runs.failed_locales(s, batch),
            created_by="retry",
            synchronous=True,
        )
    members = _members(bid)
    by_loc = {}
    for m in members:
        by_loc.setdefault(m.target_locale, []).append(m)
    assert len(by_loc["de-DE"]) == 2, ("de-DE re-dispatched a 2nd attempt", by_loc["de-DE"])
    assert len(by_loc["es-ES"]) == 1 and len(by_loc["fr-FR"]) == 1, (
        "the non-failed locales must be untouched by retry-failed",
        by_loc,
    )
    print(
        "  retry-failed: re-dispatches EXACTLY the failed set (de-DE), leaving the completed "
        "locales untouched OK"
    )


# ============================================================================
# 11. web routes — rerun + retry-failed POSTs (303 -> the live matrix), async daemon
# ============================================================================
def test_web_rerun_and_retry_routes():
    locales = ["es-ES", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("WebRerunWf"), _T, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    client = TestClient(app)  # auth off -> no CSRF
    # retry-failed heals nothing yet (still failing) but must 303 back to the matrix
    HealProvider.reset(set())  # now heal so the async re-run completes de-DE
    r = client.post(f"/batches/{bid}/retry-failed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/batches/{bid}", r.headers
    # wait for the async daemon re-dispatch to re-finalize
    for _ in range(80):
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            if b.finished_at is not None and b.status == "completed":
                break
        time.sleep(0.1)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        assert b.status == "completed", (b.status, b.notes)
        de = [m for m in _members(bid) if m.target_locale == "de-DE"]
        assert len(de) == 2 and de[-1].status == RunStatus.COMPLETED, de
    # a single-locale rerun route works too (re-run the now-completed es-ES)
    r2 = client.post(f"/batches/{bid}/rerun", data={"locale": "es-ES"}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == f"/batches/{bid}", r2.headers
    for _ in range(80):
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            if (
                b.finished_at is not None
                and len([m for m in _members(bid) if m.target_locale == "es-ES"]) == 2
            ):
                break
        time.sleep(0.1)
    with Session(engine) as s:
        es = [m for m in _members(bid) if m.target_locale == "es-ES"]
        assert len(es) == 2, ("the web rerun added a 2nd es-ES attempt", es)
    print(
        "  web: POST /batches/{id}/retry-failed + /rerun -> 303 back to the matrix; the async "
        "daemon re-dispatches + re-finalizes OK"
    )


# ============================================================================
# 12. /api — rerun + retry-failed shapes + GET ?detail=steps
# ============================================================================
def test_api_rerun_retry_shapes():
    locales = ["es-ES", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("ApiRerunWf"), _TF, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})
    admin = security.create_user(_uniq("apibm") + "@x.io", "apibm", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "bm-key", scopes="api docs")
    h = {"X-Loc-Token": raw}
    client = TestClient(app)
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    # GET ?detail=steps returns the matrix payload
    g = client.get(f"/api/batches/{bid}?detail=steps", headers=h)
    assert g.status_code == 200, g.text
    gb = g.json()
    assert gb["ok"] and "requested_steps" in gb and gb["members"][0].get("step_runs"), gb
    # retry-failed heals de-DE (clear the failure first) and reports the retried set. The
    # routes now LAUNCH async by default (AUDIT Q2 follow-up); wait=true restores the old
    # one-shot finalized-matrix shape this shape-assertion needs.
    HealProvider.reset(set())
    rf = client.post(f"/api/batches/{bid}/retry-failed", headers=h, json={"wait": True})
    assert rf.status_code == 200, rf.text
    rb = rf.json()
    assert rb["retried"] == ["de-DE"] and rb["status"] == "completed", rb
    assert "requested_steps" in rb, "the write endpoints return the detail (matrix) shape"
    # a single-locale rerun over /api (wait=true -> the finalized matrix shape)
    rr = client.post(f"/api/batches/{bid}/rerun", headers=h, json={"locale": "es-ES", "wait": True})
    assert rr.status_code == 200, rr.text
    assert rr.json()["kind"] == "batch", rr.json()
    # a bad rerun body is a clean 400; a missing batch is 404
    assert client.post(f"/api/batches/{bid}/rerun", headers=h, json={}).status_code == 400
    assert (
        client.post("/api/batches/999999/rerun", headers=h, json={"locale": "es-ES"}).status_code
        == 404
    )
    print(
        "  /api: GET ?detail=steps + POST rerun/retry-failed return the matrix shape; bad "
        "body 400, missing batch 404 OK"
    )


# ============================================================================
# 13. RBAC — a viewer key is 403 on a write; a foreign-org key is 404 on rerun
# ============================================================================
def test_rbac_write_and_foreign_org():
    locales = ["es-ES"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("RbacWf"), _T, provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
    client = TestClient(app)
    viewer = security.create_user(
        _uniq("bmview") + "@x.io", "bmview", "viewer", "fixture-pass-1234!!"
    )
    _r, vraw = apikeys.create_key(viewer.id, "bm-view-key", scopes="api docs")
    vh = {"X-Loc-Token": vraw}
    # Both users are created with the gate OFF (org NULL). The role/org rings are no-ops with
    # the login wall off (single-tenant); turn them ON per-phase (restored in finally) so the
    # write ring + org isolation actually enforce.
    admin_b = security.create_user(_uniq("orgb") + "@x.io", "orgb", "admin", "fixture-pass-1234!!")
    _r2, braw = apikeys.create_key(admin_b.id, "bm-orgb-key", scopes="api docs")
    bh = {"X-Loc-Token": braw}
    try:
        # --- write ring: a VIEWER key cannot write -> 403 (auth ON, multi-org still OFF so a
        # NULL-org viewer key clears the gate and the write-block is what fires) ---
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
        assert (
            client.post(
                f"/api/batches/{bid}/rerun", headers=vh, json={"locale": "es-ES"}
            ).status_code
            == 403
        )
        assert (
            client.post(f"/api/batches/{bid}/retry-failed", headers=vh, json={}).status_code == 403
        )
        # --- org isolation: a key whose HOME org != the batch's project org 404s (not
        # enumerable). Enable the multi-org gate and put the batch's project in org A, the key
        # in org B. ---
        with Session(engine) as s:
            org_a = Organization(name=_uniq("OrgA"))
            org_b = Organization(name=_uniq("OrgB"))
            s.add(org_a)
            s.add(org_b)
            s.commit()
            s.refresh(org_a)
            s.refresh(org_b)
            b = s.get(WorkflowBatch, bid)
            proj = s.get(Project, s.get(Document, b.document_id).project_id)
            proj.org_id = org_a.id
            b.org_id = org_a.id
            s.add(proj)
            s.add(b)
            u = s.get(type(admin_b), admin_b.id)
            u.org_id = org_b.id
            s.add(u)
            s.commit()
        settings.set_many({"multi_org_enabled": "1"})
        settings.invalidate_cache()
        assert (
            client.post(
                f"/api/batches/{bid}/rerun", headers=bh, json={"locale": "es-ES"}
            ).status_code
            == 404
        ), "a foreign-org key must 404 on rerun"
        assert client.get(f"/api/batches/{bid}?detail=steps", headers=bh).status_code == 404, (
            "foreign-org read must 404 too"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  RBAC: a viewer key is 403 on rerun/retry-failed; a foreign-org key is 404 on "
        "rerun + read OK"
    )


# ============================================================================
# 14. CLI — `workflow batch show / rerun / retry-failed` round-trip
# ============================================================================
def test_cli_batch_roundtrip():
    locales = ["es-ES", "de-DE"]
    _pid, did = _mk_project_doc(locales, fail_loc="de-DE")
    wf = _mk_wf(_uniq("CliBmWf"), _TF, provider="healmock", retry_max=0)
    HealProvider.reset({"FAILME"})
    # dispatch a batch via the CLI run --all-locales
    code, out, err = _run_cli(["--json", "workflow", "run", str(did), str(wf), "--all-locales"])
    payload = _json.loads(out.strip().splitlines()[-1])
    bid = payload["id"]
    assert payload["status"] == "partial", payload  # de-DE failed, es-ES done
    # `workflow batch show` (human) prints an ASCII matrix; --json emits the detail shape
    code, out, err = _run_cli(["workflow", "batch", "show", str(bid)])
    assert code == 0, (code, err)
    assert "Locale" in out and "Translator" in out and "es-ES" in out and "de-DE" in out, out
    assert "codes:" in out, "the ASCII legend is missing"
    code, out, err = _run_cli(["--json", "workflow", "batch", "show", str(bid)])
    jb = _json.loads(out.strip().splitlines()[-1])
    assert jb["ok"] and "requested_steps" in jb and jb["members"][0].get("step_runs"), jb
    # `workflow batch retry-failed` after healing -> de-DE completes; batch COMPLETED
    HealProvider.reset(set())
    code, out, err = _run_cli(["--json", "workflow", "batch", "retry-failed", str(bid)])
    assert code == 0, (code, err)
    rb = _json.loads(out.strip().splitlines()[-1])
    assert rb["retried"] == ["de-DE"] and rb["status"] == "completed", rb
    # `workflow batch rerun --locale` re-runs one locale
    code, out, err = _run_cli(
        ["--json", "workflow", "batch", "rerun", str(bid), "--locale", "es-ES"]
    )
    assert code == 0, (code, err)
    rr = _json.loads(out.strip().splitlines()[-1])
    assert rr["kind"] == "batch", rr
    es = [m for m in _members(bid) if m.target_locale == "es-ES"]
    assert len(es) == 2, ("the CLI rerun added a 2nd es-ES attempt", es)
    # a bad locale is a clean usage error (exit 2), never a traceback
    code, _out, _err = _run_cli(["workflow", "batch", "rerun", str(bid), "--locale", "zz-ZZ"])
    assert code == 2, code
    print(
        "  CLI: batch show (ASCII + --json) + retry-failed + rerun round-trip; bad locale is "
        "a clean usage error OK"
    )


# ============================================================================
# 15. parity — the three new leaf commands are registered + carry an entry
# ============================================================================
def test_parity_entries():
    import importlib

    parity = importlib.import_module("tests.verify_cli_parity")
    for cmd in ("workflow batch show", "workflow batch rerun", "workflow batch retry-failed"):
        assert cmd in parity.EXPECTED_COMMANDS, f"{cmd} missing from EXPECTED_COMMANDS"
    # the leaf commands actually register under the parser (help resolves)
    for sub in ("show", "rerun", "retry-failed"):
        code, _out, _err = _run_cli(["workflow", "batch", sub, "--help"])
        assert code == 0, (sub, code)
    print("  parity: the 3 new `workflow batch` leaf commands register + carry parity entries OK")


# ============================================================================
# 16. cancelled is terminal -- build_matrix marks it terminal; rerun_batch accepts it
# ============================================================================
def test_cancelled_is_terminal():
    """Issue #453: A CANCELLED member must read as terminal in build_matrix so the
    matrix shows a Re-run button instead of 'running...'. rerun_batch already accepts
    a cancelled locale (CANCELLED is not in _ACTIVE)."""
    # --- Pure build_matrix: a cancelled row gets terminal=True, resume=False ---
    view = {
        "id": 1,
        "status": "partial",
        "notes": "",
        "spend_stopped": False,
        "requested_locales": ["es-ES", "de-DE"],
        "requested_steps": [
            {"step_id": 10, "order_index": 0, "name": "Translate", "step_type": "llm_agent"},
        ],
        "members": [
            {
                "locale": "es-ES",
                "run_id": 100,
                "status": "completed",
                "attempt": 1,
                "is_latest": True,
                "step_runs": [
                    {
                        "step_id": 10,
                        "order_index": 0,
                        "name": "Translate",
                        "step_type": "llm_agent",
                        "status": "completed",
                        "score": None,
                        "gate": None,
                        "outcome_index": 0,
                        "visits": 1,
                    },
                ],
            },
            {
                "locale": "de-DE",
                "run_id": 101,
                "status": "cancelled",
                "attempt": 1,
                "is_latest": True,
                "step_runs": [
                    {
                        "step_id": 10,
                        "order_index": 0,
                        "name": "Translate",
                        "step_type": "llm_agent",
                        "status": "cancelled",
                        "score": None,
                        "gate": None,
                        "outcome_index": 0,
                        "visits": 0,
                    },
                ],
            },
        ],
    }
    matrix = batch_runs.build_matrix(view)
    rows = {r["locale"]: r for r in matrix["rows"]}
    de = rows["de-DE"]
    assert de["terminal"], "a CANCELLED member must read as terminal in build_matrix"
    assert not de["resume"], "a CANCELLED member must NOT read as resumable"
    assert not de["unlaunched"], "a CANCELLED member is launched, not unlaunched"
    assert rows["es-ES"]["terminal"], "COMPLETED must still be terminal"

    # --- rerun_batch accepts a cancelled locale ---
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("CancelTermWf"), _T, provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        assert batch.finished_at is not None
        # Set the fr-FR member to CANCELLED
        fr_member = next(m for m in _members(bid) if m.target_locale == "fr-FR")
        fr_member.status = RunStatus.CANCELLED
        s.add(fr_member)
        s.commit()
        # rerun_batch must accept the cancelled locale without raising
        batch = batch_runs.rerun_batch(
            s, batch=batch, locales=["fr-FR"], created_by="canceller", synchronous=True
        )
        assert batch.status == "completed", (batch.status, batch.notes)
    print(
        "  cancelled-terminal: build_matrix reads CANCELLED as terminal (not running); "
        "rerun_batch accepts a cancelled locale OK"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_batch_matrix FAILED ({failures})")
        raise SystemExit(1)
    print("BATCH MATRIX OK")
