"""#664 regression lock: a model that echoes the prompt's own "Source:" label.

Live WordPress demo run (gemma-4-26b-a4b on the LLM server): the heading "Layers that earn their
weight" was stored as the target "Source:\\n<ja>" with status mt and shipped. The translation
prompt ends with "Source:\\n<text>"; the model repeated the section label and nothing on the
write path noticed.

THE FIX (app/services/scaffold.py + quality.commit_mt_output, the sole MT write seam):
  * scaffold.STRUCTURE_TOKENS is the ONE definition of the prompt's section labels (adaptive
    and context_pkg import it; prompts.translation_prompt builds its "Source:" line from it).
  * scaffold.label_echo(): output that STARTS with a label the source does not start with is
    a prompt echo. Only the label (plus following whitespace) before the translation -> it is
    stripped and a minor `label_echo_stripped` qa_flag is added (status stays mt). Anything
    more than the label (the source echoed back, a second label) -> the text is left alone,
    the segment goes to NEEDS_REVIEW under a `nontranslation` flag, TM-ineligible.
  * a source that genuinely starts with the label keeps it.

Because the fix lives at the seam it covers every provider path; this suite proves it on the
plain MT path (translate._apply_result, a local engine's answer) and on the workflow
orchestrator path (an LLM step), plus the seam itself.

FAIL-BEFORE: the pre-fix quality.py is loaded from git (file copy, never git stash) and the
issue's exact shape is driven through it: it commits "Source:\\n..." as clean mt.

Hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys). ASCII-only failure messages;
final line 'verify_label_echo OK'.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_i664_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import adaptive, context_pkg, orchestrator, prompts, quality, refusal, scaffold
from app.services import translate as translate_svc

SRC = "Layers that earn their weight"
JA = "重みに見合うレイヤリング"
LOCALE = "ja-JP"
FLAG = "label_echo_stripped"


def _fail(msg):
    print("FAIL: " + msg)
    sys.exit(1)


def _ok(label):
    print("  ok - " + label)


def _seg(session, doc_id, source_text, order, locale=LOCALE):
    s = Segment(
        document_id=doc_id,
        target_locale=locale,
        order_index=order,
        source_text=source_text,
        status=SegStatus.NEW,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _flags(seg, kind):
    return [f for f in (seg.qa_flags or []) if isinstance(f, dict) and f.get("type") == kind]


# ---------------------------------------------------------------------------
def check_fail_before(session, doc_id):
    print("A. FAIL-BEFORE: the pre-fix seam commits the echoed label as clean mt")
    base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    ).stdout.strip()
    if not base:
        print("  skip - no origin/main merge-base (shallow checkout)")
        return
    shown = subprocess.run(
        ["git", "show", base + ":app/services/quality.py"],
        capture_output=True,
        cwd=_ROOT,
    )
    if shown.returncode != 0 or b"label_echo" in shown.stdout:
        print("  skip - base already carries the fix")
        return
    tmp = tempfile.mkdtemp(prefix="i664_prefix_")
    path = os.path.join(tmp, "quality_prefix.py")
    with open(path, "wb") as fh:
        fh.write(shown.stdout)
    spec = importlib.util.spec_from_file_location("quality_prefix_i664", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seg = _seg(session, doc_id, SRC, 0)
    out = mod.commit_mt_output(session, seg, "Source:\n" + JA, source="mt")
    session.commit()
    if not (out.committed and seg.target_text == "Source:\n" + JA and seg.status == SegStatus.MT):
        _fail("pre-fix seam did not reproduce the shipped bug")
    _ok("pre-fix seam stores 'Source:' + translation as status mt (the shipped bug)")


# ---------------------------------------------------------------------------
def check_single_definition():
    print("B. one shared definition of the structure tokens")
    if adaptive._STRUCTURE_TOKENS != scaffold.STRUCTURE_TOKENS:
        _fail("adaptive._STRUCTURE_TOKENS drifted from scaffold.STRUCTURE_TOKENS")
    if context_pkg._STRUCTURE_TOKENS != scaffold.STRUCTURE_TOKENS:
        _fail("context_pkg._STRUCTURE_TOKENS drifted from scaffold.STRUCTURE_TOKENS")
    _sys, prompt, _payload = prompts.translation_prompt("Hello", "en-US", LOCALE, [])
    if not prompt.endswith(scaffold.SOURCE_LABEL + "\nHello"):
        _fail("translation_prompt must end with the shared Source label")
    _ok("adaptive, context_pkg and prompts all use scaffold's tokens")


# ---------------------------------------------------------------------------
def check_detector():
    print("C. scaffold.label_echo")
    le = scaffold.label_echo
    cases = [
        # (output, source, expected cleaned, expected verdict)
        ("Source:\n" + JA, SRC, JA, "stripped"),
        ("Source: " + JA, SRC, JA, "stripped"),
        ("  Source:\r\n\r\n" + JA, SRC, JA, "stripped"),
        ("Target:\n" + JA, SRC, JA, "stripped"),
        (JA, SRC, JA, ""),
        # the source genuinely starts with the label: keep it
        ("Source: " + JA, "Source: layers", "Source: " + JA, ""),
        ("Target: " + JA, "Target: layers", "Target: " + JA, ""),
        # more than the label before the translation: review
        (
            "Source:\n" + SRC + "\n\nTarget:\n" + JA,
            SRC,
            "Source:\n" + SRC + "\n\nTarget:\n" + JA,
            "review",
        ),
        ("Source:\n" + SRC, SRC, "Source:\n" + SRC, "review"),
        ("Source:\nTarget:\n" + JA, SRC, "Source:\nTarget:\n" + JA, "review"),
        (
            "Source:\n" + JA + "\nTarget:\n" + JA,
            SRC,
            "Source:\n" + JA + "\nTarget:\n" + JA,
            "review",
        ),
        ("Source:", SRC, "Source:", "review"),
        # a label that is not at the start is content
        (JA + "\nSource: x", SRC, JA + "\nSource: x", ""),
        # a word that merely begins like a label is content
        ("Sources: " + JA, SRC, "Sources: " + JA, ""),
        ("", SRC, "", ""),
        (None, SRC, None, ""),
        # reviewer: case / spacing / fullwidth-colon variants of the same echo
        ("source:\n" + JA, SRC, JA, "stripped"),
        ("SOURCE: " + JA, SRC, JA, "stripped"),
        ("Source :\n" + JA, SRC, JA, "stripped"),
        ("Source：" + JA, SRC, JA, "stripped"),
        ("target :  " + JA, SRC, JA, "stripped"),
        # a source that opens with the label in another case still keeps it
        ("source: " + JA, "SOURCE: layers", "source: " + JA, ""),
        # a labelled foreign source ("Quelle: ...") translated to "Source: ..." on ONE line is
        # a faithful citation, never silently stripped; the newline echo shape still strips
        ("Source: Wikipedia", "Quelle: Wikipedia", "Source: Wikipedia", "review"),
        # Review gate: no shape where the label can be a faithful translation is ever stripped
        ("Source:\nWikipedia", "Quelle: Wikipedia", "Source:\nWikipedia", "review"),
        ("Source:\nWikipedia", "Quelle:\nWikipedia", "Source:\nWikipedia", "review", "en-US"),
        ("Target:\n100", "目標\n100", "Target:\n100", "review", "en-US"),
        ("Source: Wikipedia", "出典 Wikipedia", "Source: Wikipedia", "review", "en-US"),
        ("Source: Wikipedia", "【出典】Wikipedia", "Source: Wikipedia", "review", "en"),
        ("Source: " + JA, "Note: layers", "Source: " + JA, "review"),
        ("Source: " + JA, "See below - here: layers", "Source: " + JA, "review"),
        ("Source: " + JA, SRC, "Source: " + JA, "review", None),
        ("Source: The weights", SRC, "Source: The weights", "review", "en_GB"),
        # ordinary text that merely begins with the word is untouched in any locale
        ("Source code is open", "ソース", "Source code is open", "", "en-US"),
        ("Target audience: kids", "対象: 子供", "Target audience: kids", "", "en-US"),
        # the prompt's own newline shape into English is still repaired
        ("Source:\nThe weights", "重み", "The weights", "stripped", "en-US"),
        ("Source:\nL1\n\nL2", "S1\n\nS2", "L1\n\nL2", "stripped", "en-US"),
    ]
    for case in cases:
        out, src, want_text, want_verdict = case[:4]
        loc = case[4] if len(case) > 4 else LOCALE
        got_text, got_verdict = le(out, src, loc)
        if got_text != want_text or got_verdict != want_verdict:
            _fail(
                f"label_echo({out!r}, {src!r}, {loc!r}) -> {(got_text, got_verdict)!r}, "
                f"wanted {(want_text, want_verdict)!r}"
            )
    _ok("strip / keep / review decisions")


# ---------------------------------------------------------------------------
def check_seam(session, doc_id):
    print("D. commit_mt_output (the sole write seam)")
    seg = _seg(session, doc_id, SRC, 1)
    out = quality.commit_mt_output(session, seg, "Source:\n" + JA, source="mt")
    session.commit()
    if not out.committed or seg.target_text != JA:
        _fail(f"label must be stripped, got {seg.target_text!r}")
    if seg.status != SegStatus.MT:
        _fail(f"a stripped label stays mt, got {seg.status!r}")
    fl = _flags(seg, FLAG)
    if len(fl) != 1 or fl[0].get("severity") != "minor":
        _fail(f"expected one minor {FLAG} flag, got {seg.qa_flags!r}")
    if not out.tm_eligible or not out.label_echo_stripped:
        _fail("stripped text is clean: TM-eligible, outcome.label_echo_stripped True")
    _ok("stripped, status mt, one minor label_echo_stripped flag, outcome carries it")

    # a clean re-commit clears the stale flag
    quality.commit_mt_output(session, seg, JA, source="mt")
    session.commit()
    if _flags(seg, FLAG):
        _fail("a clean re-commit must clear the stale label_echo_stripped flag")
    _ok("stale flag cleared by a clean re-commit")

    # source that starts with the label keeps it, no flag
    seg2 = _seg(session, doc_id, "Source: layers", 2)
    quality.commit_mt_output(session, seg2, "Source: " + JA, source="mt")
    session.commit()
    if seg2.target_text != "Source: " + JA or _flags(seg2, FLAG):
        _fail(f"a genuine leading 'Source:' must be kept untouched, got {seg2.target_text!r}")
    _ok("source that starts with 'Source:' keeps it")

    # whole source echoed back: review, TM-ineligible, text not guessed at
    seg3 = _seg(session, doc_id, SRC, 3)
    echoed = "Source:\n" + SRC + "\n\nTarget:\n" + JA
    out3 = quality.commit_mt_output(session, seg3, echoed, source="mt")
    session.commit()
    if seg3.status != SegStatus.NEEDS_REVIEW or out3.tm_eligible:
        _fail(f"echoed source must route to review and stay out of TM, got {seg3.status!r}")
    if seg3.target_text != echoed:
        _fail("the review path must not guess: text left as the model wrote it")
    nt = _flags(seg3, refusal.NONTRANSLATION_FLAG_TYPE)
    if len(nt) != 1 or "label" not in str(nt[0].get("reason")).lower():
        _fail(f"expected a nontranslation flag naming the label echo, got {seg3.qa_flags!r}")
    if out3.scaffold_echo != scaffold.LABEL_ECHO_REVIEW_REASON:
        _fail("outcome.scaffold_echo must carry the label-echo review reason")
    _ok("source echoed back -> NEEDS_REVIEW, TM-ineligible, flagged, text untouched")

    # Review gate: an English target whose real translation opens with "Source:" is never cut;
    # it is held for a reviewer with the text exactly as the model wrote it
    seg5 = _seg(session, doc_id, "出典 Wikipedia", 7, locale="en-US")
    out5 = quality.commit_mt_output(session, seg5, "Source: Wikipedia", source="mt")
    session.commit()
    if seg5.target_text != "Source: Wikipedia" or _flags(seg5, FLAG):
        _fail(f"a plausible translated label must never be stripped, got {seg5.target_text!r}")
    if seg5.status != SegStatus.NEEDS_REVIEW or out5.tm_eligible:
        _fail(f"an ambiguous label goes to review, got {seg5.status!r}")
    _ok("en target 'Source: Wikipedia' from an unlabelled source -> review, text intact")

    # a refusal hidden behind a label is still a refusal
    seg4 = _seg(session, doc_id, SRC, 4)
    out4 = quality.commit_mt_output(
        session,
        seg4,
        "Source:\nI'm ready to translate, however I don't see any source text. "
        "Please provide the text.",
        source="mt",
    )
    session.commit()
    if not out4.flagged or seg4.status != SegStatus.NEEDS_REVIEW or seg4.target_text:
        _fail("a refusal behind a stripped label must still be withheld")
    _ok("refusal behind a label is still withheld")


def check_kill_switch(session, doc_id):
    print("E. REFUSAL_GUARD_ENABLED off restores pre-guard behaviour")
    prev = config.REFUSAL_GUARD_ENABLED
    config.REFUSAL_GUARD_ENABLED = False
    try:
        seg = _seg(session, doc_id, SRC, 5)
        quality.commit_mt_output(session, seg, "Source:\n" + JA, source="mt")
        session.commit()
        if seg.target_text != "Source:\n" + JA:
            _fail("guard off must leave the text byte-identical")
    finally:
        config.REFUSAL_GUARD_ENABLED = prev
    _ok("guard off: text untouched")


# ---------------------------------------------------------------------------
def check_translate_path(session, doc_id):
    print("F. path 1: plain MT (translate._apply_result, a local engine's answer)")
    seg = _seg(session, doc_id, SRC, 6)
    stats = {
        "provider_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "errors": 0,
        "translated": 0,
        "needs_review": 0,
        "failovers": 0,
        "provider_used": {},
    }
    res = {
        "text": "Source:\n" + JA,
        "provider_used": "mock",
        "model_used": "gemma-4-26b-a4b",
        "attempts": 1,
        "error": None,
        "usage": {},
    }
    translate_svc._apply_result(session, seg, res, stats)
    session.commit()
    session.refresh(seg)
    if seg.target_text != JA or seg.status != SegStatus.MT or len(_flags(seg, FLAG)) != 1:
        _fail(f"plain MT path stored {seg.target_text!r} / {seg.status!r} / {seg.qa_flags!r}")
    if stats["translated"] != 1 or stats["errors"]:
        _fail(f"stats should count one clean translation, got {stats!r}")
    _ok("plain MT: target is the bare translation, mt, flagged minor")


class _CannedCompletion:
    def __init__(self, text):
        self.text = text
        self._orig = None

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            return {
                "text": self.text,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def _workflow_run(session, pid, canned, tag):
    doc = Document(project_id=pid, filename=f"i664-{tag}.md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    seg = _seg(session, doc.id, SRC, 0)
    wf = Workflow(name=f"i664-wf-{tag}-{doc.id}", scope="global")
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Step0",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
        )
    )
    session.commit()
    doc_id, wf_id, seg_id = doc.id, wf.id, seg.id
    with _CannedCompletion(canned):
        for _ in orchestrator.run_workflow_stream(session, wf_id, doc_id, LOCALE):
            pass
    session.expire_all()
    return session.get(Segment, seg_id)


def check_workflow_path(session, pid):
    print("G. path 2: workflow orchestrator (LLM step) write-back")
    # Single-line shapes: a multi-line answer to a one-line source is already withheld upstream
    # by the orchestrator's over-translation guard, so these are the shapes that reach the seam.
    seg = _workflow_run(session, pid, "Source: " + JA, "strip")
    if seg.target_text != JA:
        _fail(f"workflow path stored {seg.target_text!r}, wanted the bare translation")
    if seg.status != SegStatus.MT or len(_flags(seg, FLAG)) != 1:
        _fail(f"workflow path status/flags wrong: {seg.status!r} {seg.qa_flags!r}")
    _ok("workflow: bare translation, mt, one minor flag")
    echoed = "Source: " + SRC
    seg = _workflow_run(session, pid, echoed, "review")
    if seg.status != SegStatus.NEEDS_REVIEW:
        _fail(f"workflow path must keep NEEDS_REVIEW after the status=MT reset, got {seg.status!r}")
    if len(_flags(seg, refusal.NONTRANSLATION_FLAG_TYPE)) != 1:
        _fail(f"workflow review path lost its flag: {seg.qa_flags!r}")
    _ok("workflow: echoed source routes to NEEDS_REVIEW and survives the status reset")


def main():
    print("verify_label_echo -- #664 (prompt label echoed into the target)")
    init_db()
    with Session(engine) as session:
        p = Project(
            name="I664",
            source_locale="en-US",
            target_locales=[LOCALE],
            default_provider="mock",
            project_type=ProjectType.DOCUMENT,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="i664.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        check_fail_before(session, d.id)
        check_single_definition()
        check_detector()
        check_seam(session, d.id)
        check_kill_switch(session, d.id)
        check_translate_path(session, d.id)
        check_workflow_path(session, p.id)
    print("verify_label_echo OK")


if __name__ == "__main__":
    main()
