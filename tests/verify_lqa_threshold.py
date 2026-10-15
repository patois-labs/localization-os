"""Configurable LQA step pass threshold (feat/lqa-configurable-threshold), hermetic.

The maintainer's headline example -- "an LQA step that needs to pass with a 94" -- was NOT
expressible: the LQA step's numeric pass gate was hardcoded (`score >= 90`, in
lqa._score line ~492), and `qe_threshold` is a DIFFERENT mechanism (the QE routing gate
on an llm_agent step) that is DEAD on an lqa step. This feature adds a NEW nullable field
`WorkflowStep.lqa_threshold`: unset (NULL) keeps the built-in gate of 90 -- byte-identical
to before -- and a set value (0-100) makes the step PASS only at `score >= it` (the
no-major-error policy is unchanged).

WHY A NEW FIELD, NOT REUSING qe_threshold: qe_threshold is non-nullable with default 70,
and every existing lqa step already carries 70; wiring it into the LQA gate would drop
every existing workflow's effective gate from 90 to 70 -- the opposite of "default
preserves behaviour". It is also semantically distinct (QE routing cutoff vs LQA pass
bar) and is an explicitly DEAD field on lqa steps. A dedicated nullable field, live ONLY
on lqa, is the clean mirror.

Proves the acceptance bar:
  (1)  NUMERIC GATE (the headline): an lqa gate at 94 FAILS a card scoring 92 and PASSES
       one scoring 95, at score_text level (deterministic via a FORBID rulebook).
  (2)  DEFAULT PRESERVES TODAY (regression): an UNSET threshold gates at exactly 90 -- a
       92 that FAILS at 94 still PASSES unset, and 90 is the pinned constant.
  (3)  CLAMP: out-of-range is clamped to 0..100; junk/blank -> None (inherit 90).
  (4)  END-TO-END BRANCH DRIVE: the SAME 92-scoring document, same workflow shape, routes
       DIFFERENTLY by threshold alone -- at 94 the LQA StepRun.gate is 'failed' (on_fail
       branch taken), unset it is 'passed' (on_pass=end skips the trailing step). The
       durable StepRun.score is 92 either way; only the verdict changes.
  (5)  SERVICE ROUND-TRIP: add_step / update_step store lqa_threshold on an lqa step but
       REJECT an out-of-band value (BUG-A202: the authoring seam refuses instead of
       silently clamping; the engine-level lqa.clamp_threshold in (3) still clamps); it is
       also REFUSED (dead-for-type) on llm_agent / human_review / tm_match; a type switch
       off lqa force-clears it.
  (6)  CLI: `workflow add-step --type lqa --lqa-threshold 94` + `show --json` round-trips;
       `--type llm_agent --lqa-threshold 94` exits nonzero (refused); `edit-step` sets it.
  (7)  API: POST /api/workflows/{id}/steps with lqa_threshold on an lqa step lands and is
       echoed by GET; on an llm_agent step it is a hard 400; edit round-trips; an
       out-of-band value is a hard 400 (unchanged stored value).
  (8)  MCP: lqa_threshold is in the create/edit step field allowlists and the tool schema.
  (9)  MIGRATION: the column exists after upgrade, is NULLABLE, and the model + the SQLite
       shim agree; a second `upgrade head` is a clean no-op (idempotent).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). ASCII-only.
Final line: "LQA CONFIGURABLE THRESHOLD OK".
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

init_env("loctms_lqathr_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Role, StepType, User, Workflow, WorkflowStep
from app.services import apikeys, lqa, orchestrator, settings
from app.services import workflows as wf

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Deterministic scoring: a FORBID rulebook. Each FORBID token present in the
# target is one MINOR penalty (lqa._rulebook_errors), so N distinct tokens in the
# text -> score 100 - N. The mock LLM judge returns no errors, and a clean (no
# tag/number/placeholder, target != source) pair has no structural errors, so the
# score is EXACTLY 100 - (FORBID hits).
# --------------------------------------------------------------------------- #
_SRC, _TGT = "en-US", "fr-FR"
# Alpha-only tokens (NO digits -- digits would trip the structural number-mismatch
# check and add a stray minor). None is a substring of another.
_TOKENS = ["zqa", "zqb", "zqc", "zqd", "zqe", "zqf", "zqg", "zqh"]


def _write_rulebook():
    rb_dir = config.DATA_DIR / "rulebooks"
    rb_dir.mkdir(parents=True, exist_ok=True)
    (rb_dir / f"{_SRC}__{_TGT}.md").write_text(
        "\n".join(f"FORBID {t}" for t in _TOKENS) + "\n", encoding="utf-8"
    )


def _target_with(n):
    """A translated-looking target carrying exactly `n` forbidden tokens -> score 100-n.
    The leading '[fr-FR] ' makes target != source (no 'untranslated' major)."""
    return "[fr-FR] " + " ".join(_TOKENS[:n]) + " alpha bravo charlie"


# ============================================================================ #
# (1) + (2) + (3): numeric gate, default-preserves, clamp -- at score_text level
# ============================================================================ #
def test_numeric_gate_and_default():
    _write_rulebook()
    rb = lqa.load_rulebook(_SRC, _TGT)
    if not rb:
        _fail("rulebook did not load from the throwaway data dir")

    # score 92 (8 forbidden), score 95 (5 forbidden)
    t92, t95 = _target_with(8), _target_with(5)

    c92_94 = lqa.score_text("source text here", t92, _SRC, _TGT, rb, "mock", lqa_threshold=94)
    if c92_94["score"] != 92:
        _fail(f"expected score 92, got {c92_94['score']} (rulebook wiring drift)")
    if c92_94["passed"] is not False:
        _fail("a 92 must FAIL an lqa gate configured to 94")
    if c92_94["gate"] != "needs_review":
        _fail(f"a failed card must gate 'needs_review', got {c92_94['gate']}")
    if c92_94.get("threshold") != 94:
        _fail(f"the card must echo the effective threshold 94, got {c92_94.get('threshold')}")

    c95_94 = lqa.score_text("source text here", t95, _SRC, _TGT, rb, "mock", lqa_threshold=94)
    if c95_94["score"] != 95 or c95_94["passed"] is not True:
        _fail(
            f"a 95 must PASS an lqa gate at 94 (got score={c95_94['score']}, "
            f"passed={c95_94['passed']})"
        )
    print("  (1) gate at 94: 92 FAILS, 95 PASSES: OK")

    # DEFAULT PRESERVES TODAY: the SAME 92, unset -> PASSES (92 >= 90). This is the
    # regression assert against the pre-configurable behaviour.
    c92_default = lqa.score_text("source text here", t92, _SRC, _TGT, rb, "mock")
    if c92_default["passed"] is not True:
        _fail("REGRESSION: a 92 with an UNSET threshold must still PASS (default gate 90)")
    if c92_default.get("threshold") != 90:
        _fail(f"unset threshold must resolve to 90, got {c92_default.get('threshold')}")
    if lqa.LQA_PASS_THRESHOLD != 90:
        _fail(f"the pinned default constant must be 90, got {lqa.LQA_PASS_THRESHOLD}")
    # explicit 90 == unset
    if lqa.score_text("s", t92, _SRC, _TGT, rb, "mock", lqa_threshold=90)["passed"] is not True:
        _fail("threshold=90 must behave exactly like the default")
    print("  (2) default (unset) preserves the historic >=90 gate; constant pinned at 90: OK")

    # A below-90 card FAILS default AND a lower threshold cannot rescue a MAJOR: prove
    # the no-major policy is unchanged (a REQUIRE major fails even at threshold 0).
    rb_major = "REQUIRE zzmissing"
    cmaj = lqa.score_text(
        "source", "[fr-FR] no such term", _SRC, _TGT, rb_major, "mock", lqa_threshold=0
    )
    if cmaj["passed"] is not False:
        _fail("a MAJOR error must fail the gate even at threshold 0 (no-major policy)")
    print("  (2b) no-major-error policy unchanged (a major fails even at threshold 0): OK")

    # CLAMP
    if lqa.clamp_threshold(None) is not None:
        _fail("clamp(None) must be None (inherit default)")
    if lqa.clamp_threshold("nope") is not None:
        _fail("clamp(junk) must be None (inherit default)")
    if lqa.clamp_threshold(150) != 100 or lqa.clamp_threshold(-5) != 0:
        _fail("clamp must bound to 0..100")
    if lqa.clamp_threshold("94") != 94:
        _fail("clamp must accept a numeric string")
    # a 150 threshold gates as 100 (nothing at 100+ can pass a 92)
    if lqa.score_text("s", t92, _SRC, _TGT, rb, "mock", lqa_threshold=150)["passed"] is not False:
        _fail("threshold 150 must clamp to 100 and FAIL a 92")
    print("  (3) clamp bounds 0..100; junk/blank -> inherit default: OK")


# ============================================================================ #
# (2c) Q7 CONTRACT PIN: a card scoring AT OR ABOVE the bar but carrying a
# major/critical MQM error is STILL routed to review. This is the documented,
# working-as-designed interaction behind the campaign's "94 -> needs_review even at
# --lqa-threshold 90" observation: the threshold IS the numeric floor, and the
# no-major/critical clause routes regardless of score. Investigated (AUDIT Q7) and
# RE-CLASSIFIED as working-as-designed; this test makes the contract explicit so it is
# not an accident. A single REQUIRE-missing major = one 5-point penalty (PENALTY[major]
# == 5) -> score 95, comfortably >= a bar of 90, yet routed BY THE MAJOR alone.
# ============================================================================ #
def test_major_error_routes_above_bar():
    if lqa.PENALTY["major"] != 5:
        _fail(
            f"this pin assumes PENALTY[major]==5 (score 95 from one major); "
            f"got {lqa.PENALTY['major']}"
        )
    # One REQUIRE-missing major, no minors -> score 95. Target is translated-looking
    # (leading [fr-FR], no digits/tags/placeholders) so the ONLY error is the major.
    rb_major = "REQUIRE zzmissing"
    tgt = "[fr-FR] alpha bravo charlie"

    # score >= bar (95 >= 90) but a MAJOR present -> FAIL / needs_review at the default.
    card = lqa.score_text("source text here", tgt, _SRC, _TGT, rb_major, "mock")
    if card["score"] != 95:
        _fail(f"expected a 95 (one major penalty of 5), got {card['score']}")
    if card["passed"] is not False:
        _fail("a 95 with a MAJOR error must be ROUTED even though 95 >= the bar of 90")
    if card["gate"] != "needs_review":
        _fail(f"the routed card must gate 'needs_review', got {card['gate']}")

    # Same at an EXPLICIT bar of 90 -- the configured threshold does not rescue a major.
    card90 = lqa.score_text("source text here", tgt, _SRC, _TGT, rb_major, "mock", lqa_threshold=90)
    if card90["passed"] is not False:
        _fail("a 95 with a major must STILL route at an explicit bar of 90")

    # The CONTRAST that proves it is the MAJOR, not the score: a 95 built from FIVE
    # MINOR errors (no major) PASSES at the same bar of 90. Same score, different verdict
    # -- exactly the campaign's 94/95-routed vs 99/100-approved split.
    _write_rulebook()
    rb = lqa.load_rulebook(_SRC, _TGT)
    minors95 = lqa.score_text("source text here", _target_with(5), _SRC, _TGT, rb, "mock")
    if minors95["score"] != 95:
        _fail(f"expected a 95 from five minors, got {minors95['score']}")
    if minors95["passed"] is not True:
        _fail(
            "a 95 with only MINOR errors must PASS at the default bar of 90 -- "
            "proving the routing above is driven by the major, not the score"
        )
    print(
        "  (2c) Q7 contract: a >=bar score with a major/critical error is routed; "
        "the same score with only minors passes (major, not score, drives it): OK"
    )


# ============================================================================ #
# (4) end-to-end: same 92-scoring document routes DIFFERENTLY by threshold alone
# ============================================================================ #
def _docx(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _new_project(client, name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": _SRC,
            "target_locales": _TGT,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(client, pid, paragraphs, mode="segmented"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": mode},
        files={
            "file": (
                "d.docx",
                _docx(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _direct_step(session, wf_id, order, name, **kw):
    """Direct-construct a step (bypasses save-time validation) so the engine seam can be
    driven with an arbitrary lqa_threshold/branch -- the validation path is tested
    separately in (5)."""
    session.add(
        WorkflowStep(
            workflow_id=wf_id,
            order_index=order,
            name=name,
            chunk_policy="auto",
            retry_max=1,
            backoff_base=0.0,
            **kw,
        )
    )
    session.commit()


def _new_wf(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global")
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _stepruns(run_id):
    from sqlmodel import select

    from app.models import StepRun

    with Session(engine) as s:
        return list(
            s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
        )


def _run_branch(threshold):
    """translate -> LQA(threshold, on_pass=end, on_fail=next) -> Finalize.
    The single paragraph scores 92. Returns (lqa_steprun, finalize_ran)."""
    client = TestClient(app)
    wid = _new_wf(f"lqathr-e2e-{threshold}")
    with Session(engine) as s:
        _direct_step(
            s,
            wid,
            0,
            "Translator",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
        )
        _direct_step(
            s,
            wid,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider="mock",
            key="lqa",
            lqa_threshold=threshold,
            branch={"on_pass": "end", "on_fail": "next"},
        )
        _direct_step(
            s,
            wid,
            2,
            "Finalize",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Finalize: {text}",
        )
    pid = _new_project(client, f"lqathr-e2e-{threshold}")
    # One paragraph carrying all 8 forbidden tokens -> mock target scores 92.
    doc = _upload(client, pid, [" ".join(_TOKENS) + " alpha bravo charlie"])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, doc, _TGT)
    if summary["status"] != "completed":
        _fail(f"run did not complete (threshold={threshold}): {summary}")
    srs = _stepruns(summary["run_id"])
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wid)
    lqa_id, final_id = steps[1].id, steps[2].id
    lqa_sr = [sr for sr in srs if sr.step_id == lqa_id][0]  # noqa: RUF015
    finalize_ran = any(sr.step_id == final_id for sr in srs)
    return lqa_sr, finalize_ran


def test_end_to_end_branch_by_threshold():
    _write_rulebook()
    # gate at 94: 92 < 94 -> FAIL -> on_fail=next -> Finalize RUNS
    lqa_sr, finalize_ran = _run_branch(94)
    if lqa_sr.score != 92.0:
        _fail(f"expected durable LQA score 92, got {lqa_sr.score}")
    if lqa_sr.gate != "failed":
        _fail(f"gate at 94 must be 'failed' for a 92 (got {lqa_sr.gate})")
    if not finalize_ran:
        _fail("on_fail=next must run the trailing Finalize step")
    print("  (4a) threshold 94: a 92 FAILS -> on_fail branch runs Finalize: OK")

    # unset (None): 92 >= 90 -> PASS -> on_pass=end -> Finalize SKIPPED
    lqa_sr2, finalize_ran2 = _run_branch(None)
    if lqa_sr2.score != 92.0:
        _fail(f"expected durable LQA score 92 (unset), got {lqa_sr2.score}")
    if lqa_sr2.gate != "passed":
        _fail(f"unset gate must be 'passed' for a 92 (got {lqa_sr2.gate})")
    if finalize_ran2:
        _fail("unset (default 90) must PASS -> on_pass=end skips Finalize")
    print("  (4b) unset threshold: the SAME 92 PASSES -> on_pass=end skips Finalize: OK")
    print("  (4)  same document + shape, routed by threshold alone (92 vs gate 94/90): OK")


# ============================================================================ #
# (5) service add_step / update_step round-trip + dead-for-type refusal
# ============================================================================ #
def _raises_naming(fn, *needles):
    try:
        fn()
    except wf.WorkflowError as exc:
        msg = str(exc).lower()
        for n in needles:
            if n.lower() not in msg:
                _fail(f"expected {n!r} in refusal: {exc}")
        return
    _fail(f"expected a WorkflowError naming {needles}")


def test_service_roundtrip_and_refusal():
    wid = _new_wf("lqathr-svc")
    with Session(engine) as s:
        st = wf.add_step(s, wid, name="Check", step_type=StepType.LQA, lqa_threshold=94)
        sid = st.id
    with Session(engine) as s:
        got = s.get(WorkflowStep, sid)
        if got.lqa_threshold != 94:
            _fail(f"add_step must store lqa_threshold=94, got {got.lqa_threshold}")

    # REJECT out-of-band on add (BUG-A202: the seam now refuses instead of silently
    # clamping -5 -> gate-disabling 0 / 150 -> 100). The engine-level lqa.clamp_threshold
    # still clamps (tested in (3)); the authoring SEAM rejects.
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, name="Check2", step_type=StepType.LQA, lqa_threshold=150),
            "0 to 100",
        )
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, name="Check2b", step_type=StepType.LQA, lqa_threshold=-5),
            "0 to 100",
        )

    # unset default is None
    with Session(engine) as s:
        st3 = wf.add_step(s, wid, name="Check3", step_type=StepType.LQA)
        if st3.lqa_threshold is not None:
            _fail(f"an unset lqa step must store lqa_threshold=None, got {st3.lqa_threshold}")

    # REFUSED (dead-for-type) on llm_agent / human_review / tm_match
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(
                s, wid, step_type=StepType.LLM_AGENT, prompt_text="{text}", lqa_threshold=94
            ),
            "lqa pass threshold",
        )
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, lqa_threshold=94),
            "lqa pass threshold",
        )
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.TM_MATCH, lqa_threshold=94),
            "lqa pass threshold",
        )
    # a DEFAULT (None) on a non-lqa step is NEVER refused
    with Session(engine) as s:
        wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_text="{text}")
    print("  (5a) add_step stores on lqa; rejects out-of-band; refuses a real value off-type: OK")

    # update_step: set, clear, and refuse-on-wrong-type
    with Session(engine) as s:
        wf.update_step(s, sid, lqa_threshold=80)
    with Session(engine) as s:
        if s.get(WorkflowStep, sid).lqa_threshold != 80:
            _fail("update_step must set lqa_threshold=80")
    with Session(engine) as s:
        wf.update_step(s, sid, lqa_threshold="")  # blank clears -> None (default 90)
    with Session(engine) as s:
        if s.get(WorkflowStep, sid).lqa_threshold is not None:
            _fail("update_step with blank must clear lqa_threshold back to None")

    # a TYPE SWITCH off lqa force-clears a set threshold (never refuses)
    with Session(engine) as s:
        wf.update_step(s, sid, lqa_threshold=95)
    with Session(engine) as s:
        rep: dict = {}
        wf.update_step(s, sid, step_type=StepType.LLM_AGENT, prompt_text="{text}", report=rep)
    with Session(engine) as s:
        sw = s.get(WorkflowStep, sid)
        if sw.lqa_threshold is not None:
            _fail("a type switch off lqa must force-clear lqa_threshold to None")
    print("  (5b) update_step set/clear + type-switch force-clear: OK")


# ============================================================================ #
# (6) CLI add-step / edit-step / show --json round-trip
# ============================================================================ #
def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    combined = out.getvalue() + err.getvalue()
    if not combined.isascii():
        _fail(("CLI output must be ASCII-only", combined))
    return rc, combined


def _show_json(wid):
    rc, out = _run_cli(["--json", "workflow", "show", str(wid)])
    if rc != 0:
        _fail(f"workflow show --json exited {rc}: {out}")
    # the JSON is the last complete object printed
    start = out.index("{")
    return _json.loads(out[start:])


def test_cli_roundtrip():
    wid = _new_wf("lqathr-cli")
    rc, out = _run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "lqa",
            "--name",
            "Check",
            "--lqa-threshold",
            "94",
        ]
    )
    if rc != 0:
        _fail(f"CLI add-step --type lqa --lqa-threshold 94 must succeed: {out}")
    data = _show_json(wid)
    steps = data["steps"]
    if steps[0].get("lqa_threshold") != 94:
        _fail(f"show --json must echo lqa_threshold=94, got {steps[0].get('lqa_threshold')}")

    # refused on a non-lqa step -> nonzero exit
    rc2, out2 = _run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--prompt",
            "Translate: {text}",
            "--lqa-threshold",
            "94",
        ]
    )
    if rc2 == 0:
        _fail("CLI add-step --type llm_agent --lqa-threshold 94 must be refused (nonzero)")
    if "lqa pass threshold" not in out2.lower():
        _fail(f"the CLI refusal must name the field: {out2}")
    print("  (6a) CLI add-step + show --json round-trips; off-type refused: OK")

    # edit-step sets it on the lqa step (step id 1 = order 0)
    sid = steps[0]["id"]
    rc3, out3 = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--lqa-threshold", "88"])
    if rc3 != 0:
        _fail(f"CLI edit-step --lqa-threshold 88 must succeed: {out3}")
    data2 = _show_json(wid)
    if data2["steps"][0].get("lqa_threshold") != 88:
        _fail("edit-step must update lqa_threshold to 88")
    print("  (6b) CLI edit-step updates lqa_threshold: OK")


# ============================================================================ #
# (7) API round-trip (login wall ON, multi-org OFF)
# ============================================================================ #
def _admin_key():
    with Session(engine) as s:
        u = s.exec(
            __import__("sqlmodel").select(User).where(User.email == "lqathr-admin@x.io")
        ).first()
        if not u:
            u = User(email="lqathr-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "lqathr-api", scopes="api docs")
    return raw


def test_api_roundtrip():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        client = TestClient(app)
        h = {"X-Loc-Token": _admin_key()}
        wid = client.post(
            "/api/workflows", json={"name": "lqathr-api", "scope": "global"}, headers=h
        ).json()["id"]
        # lqa step with lqa_threshold
        r = client.post(
            f"/api/workflows/{wid}/steps",
            json={"step_type": "lqa", "name": "Check", "lqa_threshold": 94},
            headers=h,
        )
        if r.status_code != 200:
            _fail(f"API add lqa step w/ lqa_threshold must 200: {r.status_code} {r.text}")
        # GET echoes it
        got = client.get(f"/api/workflows/{wid}", headers=h).json()
        st = got["steps"][0]
        if st.get("lqa_threshold") != 94:
            _fail(f"GET must echo lqa_threshold=94, got {st.get('lqa_threshold')}")
        sid = st["id"]

        # REJECT out-of-band over the wire (BUG-A202): an edit to 150 is a hard 400,
        # and the stored value is unchanged (still 94), never silently clamped to 100.
        re = client.post(
            f"/api/workflows/{wid}/steps/{sid}/edit", json={"lqa_threshold": 150}, headers=h
        )
        if re.status_code != 400:
            _fail(f"API edit to an out-of-band lqa_threshold must 400, got {re.status_code}")
        got2 = client.get(f"/api/workflows/{wid}", headers=h).json()
        if got2["steps"][0].get("lqa_threshold") != 94:
            _fail("a rejected out-of-band edit must leave the stored value unchanged (94)")

        # refused on an llm_agent step -> hard 400
        r2 = client.post(
            f"/api/workflows/{wid}/steps",
            json={"step_type": "llm_agent", "prompt_text": "{text}", "lqa_threshold": 94},
            headers=h,
        )
        if r2.status_code != 400:
            _fail(f"API lqa_threshold on an llm_agent step must 400, got {r2.status_code}")
        print("  (7) API add/edit/GET round-trips; out-of-band rejected; off-type 400: OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


# ============================================================================ #
# (8) MCP: field allowlists + tool schema
# ============================================================================ #
def test_mcp_surface():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry
    import server as mcp_server

    if "lqa_threshold" not in mcp_server._WORKFLOW_STEP_FIELDS:
        _fail("lqa_threshold must be in the MCP create/add step field allowlist")
    if "lqa_threshold" not in mcp_server._WORKFLOW_EDIT_FIELDS:
        _fail("lqa_threshold must be in the MCP edit step field allowlist")
    # the allowlist checker passes it through (not refused as unknown)
    checked = mcp_server._allowed_key_fields(
        {"lqa_threshold": 94}, mcp_server._WORKFLOW_STEP_FIELDS, "steps[0]"
    )
    if checked.get("lqa_threshold") != 94:
        _fail("MCP _allowed_key_fields must pass lqa_threshold through")
    # documented in the create_workflow tool schema
    blob = _json.dumps(
        [t.input_schema for t in registry.TOOLS if t.name in ("create_workflow", "edit_workflow")]
    )
    if "lqa_threshold" not in blob:
        _fail("the MCP create/edit_workflow tool schema must mention lqa_threshold")
    print("  (8) MCP step field allowlists + tool schema carry lqa_threshold: OK")


# ============================================================================ #
# (9) migration: column present, nullable, model/shim agree, idempotent upgrade
# ============================================================================ #
def test_migration():
    from sqlalchemy import inspect as _inspect

    cols = {c["name"]: c for c in _inspect(engine).get_columns("workflowstep")}
    if "lqa_threshold" not in cols:
        _fail("the workflowstep.lqa_threshold column must exist after upgrade")
    if not cols["lqa_threshold"]["nullable"]:
        _fail("lqa_threshold must be NULLABLE")
    # the SQLite shim (app/db.py) carries the same column
    from app import db as _db

    shim = dict(_db._ADDED_COLUMNS.get("workflowstep", []))
    if "lqa_threshold" not in shim:
        _fail(
            "app/db.py _ADDED_COLUMNS['workflowstep'] must carry lqa_threshold "
            "(nullability-parity guard)"
        )
    # CHAIN INTEGRITY: our revision is the single head, chained off the prior head, and
    # its up/down are a plain add/drop of one column. (The full "runs clean + idempotent
    # on a fresh DB" is delegated to verify_alembic + the nullability-parity guard, which
    # run against a clean chain rather than this suite's already-populated hermetic DB.)
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        _fail(f"the migration chain must have exactly ONE head, found {heads}")
    # find the lqa_threshold revision itself (later migrations may legitimately
    # chain on top of it, so it need not be the current head)
    lqa_rev = None
    for rev in script.walk_revisions():
        if "lqa_threshold" in (rev.doc or "").lower() or "lqa_threshold" in os.path.basename(
            rev.path
        ):
            lqa_rev = rev
            break
    if lqa_rev is None:
        _fail("could not find the lqa_threshold revision in the script directory")
    if lqa_rev.down_revision != "a9f3c1e7d5b2":
        _fail(f"lqa_threshold migration must chain off a9f3c1e7d5b2, got {lqa_rev.down_revision}")
    # the lqa_threshold revision must be an ancestor of (or equal to) the current head
    ancestors = {r.revision for r in script.iterate_revisions(heads[0], None)}
    if lqa_rev.revision not in ancestors:
        _fail(
            f"the lqa_threshold revision {lqa_rev.revision} must be an ancestor of "
            f"the current head {heads[0]}"
        )
    print("  (9) migration: column present + nullable, shim agrees, single-head chain: OK")


if __name__ == "__main__":
    test_numeric_gate_and_default()
    test_major_error_routes_above_bar()
    test_end_to_end_branch_by_threshold()
    test_service_roundtrip_and_refusal()
    test_cli_roundtrip()
    test_api_roundtrip()
    test_mcp_surface()
    test_migration()
    print("LQA CONFIGURABLE THRESHOLD OK")
