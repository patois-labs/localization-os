"""Q10 regression lock: the SCAFFOLD-ECHO gate at the sole MT write seam.

Core-QA campaign, the planning notes finding Q10 (K4-class).
On prod (doc09.docx = doc 83, es-ES) the model echoed our OWN injected prompt scaffolding
back and it was PERSISTED as translation content:
  * seg 0 / 12 target = the fenced STYLE-GUIDE block
    (`<style_guide source="Core QA es" trust="data-only">Guia de estilo - Core QA ...`)
  * seg 11 target = `Si, no lo se pero hace algo con tus archivos y demas. MQM LQA
    encuentra para resolver:` -- the auto-injected LQA-findings preamble ("MQM LQA findings
    to resolve:") echoed and HALF-TRANSLATED. Stored as CLEAN `mt` (mt_confidence 0.9).
The structural LENGTH-RUNAWAY gate (K4) abstained: the echo was not disproportionately
long relative to its source. So seg 11 became a clean, TM-eligible stored translation of
our own prompt structure -- the exact defect this gate closes.

THE FIX (see app/services/scaffold.py + quality.commit_mt_output):
  * app.services.scaffold SINGLE-SOURCES the markers: it defines the fence tokens
    (`trust="data-only"`, the `<style_guide>` / `<translation_context>` / `<lqa_findings>`
    tag pairs) and the LQA-findings label; the prompt BUILDERS (style_guides, context_pkg,
    lqa, orchestrator) build their fences FROM those tokens, and the gate detects the SAME
    tokens. A marker can never drift from what a prompt injects. Section C pins this.
  * a new perimeter lane at the sole write seam (`commit_mt_output`), SOFT and riding the
    EXISTING `refusal.NONTRANSLATION_FLAG_TYPE` family with its own reason -- like the
    language / meta / length verdicts, but it is the STRONGEST member (they subordinate to
    it). The text is committed (a reviewer must see the leaked structure), the segment routes
    to NEEDS_REVIEW and is forced TM-INELIGIBLE. It can NEVER be a clean stored `mt`.

SEVERITY (recorded, per the finding): SOFT, not withhold. A refusal WITHHOLDS the write; a
scaffold echo is arguably worse (leaked internal structure), which argues for withholding
too. It is kept SOFT because the marker set is not uniformly zero-false-positive -- the fence
tokens are, but the "MQM LQA" invariant needed to catch seg 11's TRANSLATED echo has a narrow
real false-positive class (a document about MQM/LQA tooling). Withholding on a false positive
would SILENTLY DROP a real translation; SOFT makes a false positive cost one review flag, and
the abstain-when-in-source rule spares the obvious legitimate case (section D). The KEY
REQUIREMENT holds either way: a scaffold echo can never be a clean stored `mt`.

FAIL-BEFORE is proven by FILE COPY of the pre-fix quality.py (never `git stash` -- the stash
stack is shared across worktrees): `git show <base>:...` is written
to a temp dir, imported under a private name, and seg 11's shape is driven through the pre-fix
seam, where it commits `status=mt`, zero qa_flags, TM-ELIGIBLE. That is the shipped bug.

Hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys). ASCII-only FAILURE MESSAGES
(fixtures carry accented Spanish and a Windows console would raise UnicodeEncodeError on the
report instead of showing the real failure); final line 'verify_scaffold_echo OK'.
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

init_env("loctms_q10_")

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
from app.services import (
    context_pkg,
    lqa,
    orchestrator,
    quality,
    refusal,
    scaffold,
    style_guides,
)
from app.services.providers import MockProvider

# ---------------------------------------------------------------------------
# THE Q10 FIXTURES -- the four scaffold shapes the audit records, plus the exact seg-11 row.
# ---------------------------------------------------------------------------
# seg 0 / 12: the fenced STYLE-GUIDE block, echoed verbatim (opener + trust attr + closer).
STYLE_ECHO = (
    '<style_guide source="Core QA es" trust="data-only">Guia de estilo - Core QA '
    "(es-ES) 1. Trate de usted. 2. No use signos de exclamacion.</style_guide>"
)
# seg 11: the LQA-findings preamble echoed and HALF-TRANSLATED. The ONLY surviving scaffold
# token is the "MQM LQA" bigram -- the length gate abstains (too short), so this is the row
# that shipped as clean `mt`. This is the KEY case: it must become impossible.
SEG11_ECHO = (
    "Si, no lo se pero hace algo con tus archivos y demas. MQM LQA encuentra para resolver:"
)
# The sibling context fence, and the LQA-findings fence, and the ENGLISH preamble verbatim.
CONTEXT_ECHO = (
    '<translation_context source="Core QA" trust="data-only">domain: software | '
    "formality: formal</translation_context>"
)
LQA_FINDINGS_ECHO = (
    '<lqa_findings score="83" gate="fail">1. [Accuracy/major] wrong term '
    'used for "device"</lqa_findings>'
)
ENGLISH_PREAMBLE_ECHO = "MQM LQA findings to resolve: fix the terminology in segment two."

# A short, clean source with NO markers -- what the segment was actually asked to translate.
CLEAN_SRC = "It does something with your files and more."

# LEGITIMATE prose that MENTIONS style guides / findings / quality but carries NO fence token
# and NOT the exact preamble -- the false-positive discipline the finding calls for.
CLEAN_PROSE = [
    (
        "Consult the project style guide for the details.",
        "Consulte la guia de estilo del proyecto para ver los detalles.",
    ),
    (
        "The audit report lists several important findings to resolve.",
        "El informe de auditoria enumera varios hallazgos importantes que resolver.",
    ),
    (
        "Review the quality assessment conclusions before shipping.",
        "Revise las conclusiones de la evaluacion de calidad antes de publicar.",
    ),
    (
        "This data is confidential; keep it private.",
        "Estos datos son confidenciales; mantengalos privados.",
    ),
]


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


# ---------------------------------------------------------------------------
# A. FAIL-BEFORE, by file copy of the PRE-FIX quality module (never git stash).
#
# `24e139c` is the branch base -- the exact tree that shipped the Q10 defect. Pinning the
# hash (rather than HEAD:) keeps the proof from inverting once the fix becomes HEAD, the
# technique verify_length_runaway._PRE_FIX_COMMIT already uses.
# ---------------------------------------------------------------------------
_PRE_FIX_COMMIT = "24e139c"


def _load_pre_fix(rel_path, mod_name):
    """Import `git show <_PRE_FIX_COMMIT>:<rel_path>` under `mod_name` from a throwaway
    file. Returns None when git history is unavailable (shallow/detached checkout)."""
    blob = subprocess.run(
        ["git", "show", _PRE_FIX_COMMIT + ":" + rel_path], cwd=_ROOT, capture_output=True
    )
    if blob.returncode != 0 or not blob.stdout:
        return None
    tmpdir = tempfile.mkdtemp(prefix="loctms_q10_pre_")
    path = os.path.join(tmpdir, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(blob.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


PRE_QUALITY = None


def check_fail_before(session, doc_id):
    """Drive seg 11's shape through the PRE-FIX seam and watch it commit CLEAN `mt`."""
    global PRE_QUALITY
    print("A. fail-before (pre-fix quality.py, loaded by file copy)")
    PRE_QUALITY = _load_pre_fix("app/services/quality.py", "_q10_pre_quality")
    if PRE_QUALITY is None:
        print("  skip - no git history available (shallow/detached checkout)")
        return
    for absent in ("scaffold_echo_flag", "clear_scaffold_echo_flag", "SCAFFOLD_ECHO_REASON"):
        if hasattr(PRE_QUALITY, absent):
            _fail(f"pre-fix quality.py already exposes {absent!r} -- the pin is not pre-fix")
    _ok("pre-fix quality.py has no scaffold gate (3 names absent)")

    # THE SHIPPED BUG: seg 11 commits status=mt, zero flags, TM-eligible.
    seg = _seg(session, doc_id, CLEAN_SRC, 900, locale="es-ES")
    out = PRE_QUALITY.commit_mt_output(session, seg, SEG11_ECHO, source="workflow")
    session.commit()
    session.refresh(seg)
    if not out.committed or out.flagged:
        _fail(f"pre-fix: expected a plain clean commit, got {out!r}")
    if seg.status != SegStatus.MT:
        _fail(f"pre-fix: seg 11 must land status=mt (the defect), got {seg.status!r}")
    if seg.qa_flags:
        _fail(f"pre-fix: seg 11 must land with ZERO qa_flags (the defect), got {seg.qa_flags!r}")
    if not out.tm_eligible:
        _fail("pre-fix: seg 11 was TM-eligible (the defect); got tm_eligible=False")
    _ok("pre-fix seam commits the seg-11 preamble echo as status=mt, qa_flags=None, TM-ELIGIBLE")


# ---------------------------------------------------------------------------
# B. THE DETECTOR (scaffold.scaffold_echo): recall on every shape, precision on prose.
# ---------------------------------------------------------------------------
def check_detector():
    print("B. scaffold.scaffold_echo (recall + the abstain-in-source rule)")
    for label, echo in (
        ("style-guide fence (open+trust+close)", STYLE_ECHO),
        ("seg-11 half-translated preamble (MQM LQA invariant)", SEG11_ECHO),
        ("translation_context fence", CONTEXT_ECHO),
        ("lqa_findings fence", LQA_FINDINGS_ECHO),
        ("verbatim English preamble", ENGLISH_PREAMBLE_ECHO),
    ):
        if not scaffold.scaffold_echo(echo, CLEAN_SRC):
            _fail("recall MISS: " + label)
    _ok("all five scaffold shapes are detected (three fences, the invariant, the preamble)")

    # OPENING token alone, and CLOSING token alone -- a truncated echo of either must fire.
    if not scaffold.scaffold_echo('<style_guide source="x" trust="data-only">only the head'):
        _fail("an OPENING-only style-guide echo must fire")
    if not scaffold.scaffold_echo("...only the tail slipped through</style_guide>"):
        _fail("a CLOSING-only style-guide echo must fire")
    _ok("opening-only and closing-only fence echoes both fire (truncation-robust)")

    # PRECISION: legitimate prose that MENTIONS the concepts but carries no fence token and
    # not the exact preamble must stay silent -- the false-positive discipline.
    for src, tgt in CLEAN_PROSE:
        if scaffold.scaffold_echo(tgt, src):
            _fail(f"FALSE POSITIVE on legitimate prose: {tgt[:40]!r}")
    _ok("legit prose ('style guide', 'findings to resolve', 'quality') stays silent")

    # THE ABSTAIN-IN-SOURCE RULE (per marker): a source that itself carries a marker hands it
    # to a faithful translation, so that is not a LEAK. Proven for a fence token AND for the
    # "MQM LQA" bigram (a document genuinely about the framework).
    if scaffold.scaffold_echo(
        "Keep the </style_guide> tag verbatim.", "Do not translate the </style_guide> tag."
    ):
        _fail("abstain-in-source failed for a fence token present in the source")
    if scaffold.scaffold_echo(
        "Configure your MQM LQA thresholds here.", "Set up MQM LQA thresholds in the panel."
    ):
        _fail("abstain-in-source failed for the MQM LQA bigram present in the source")
    _ok("abstain-in-source: a marker also in the SOURCE is not a leak (fence + MQM LQA)")

    # Empty / whitespace / None target -> "" (never a crash, never a verdict).
    for t in (None, "", "   \n ", "\t"):
        if scaffold.scaffold_echo(t, CLEAN_SRC):
            _fail(f"empty/whitespace target must never produce a verdict: {t!r}")
    _ok("empty / whitespace / None target -> no verdict")

    # MockProvider's plain echo is the SOURCE (plus at most a locale tag) -- no markers unless
    # the source had them, so an offline install with clean sources is never tripped.
    for src in (CLEAN_SRC, "Save changes", "Start now"):
        for echo in ("[es-ES] " + src, src + " [es-ES]", src):
            if scaffold.scaffold_echo(echo, src):
                _fail(f"FALSE POSITIVE on a MockProvider echo shape: {echo!r}")
    _ok("the MockProvider echo-of-source never trips the gate (offline installs unaffected)")


# ---------------------------------------------------------------------------
# C. SINGLE SOURCE OF TRUTH -- the gate's markers cannot drift from the prompt builders.
# ---------------------------------------------------------------------------
def check_single_source():
    print("C. single-source: the gate detects EXACTLY what the builders inject")
    # Every rendered fence must CONTAIN its scaffold marker. If a builder's fence changes,
    # it changes via the shared token, so the marker follows -- or this assertion breaks.
    sg = style_guides.render_style_guide(
        type("G", (), {"body": "Rule 1. Use usted.", "name": "Core QA es"})()
    )
    if scaffold.TRUST_ATTR not in sg or (f"</{scaffold.STYLE_GUIDE_TAG}>") not in sg:
        _fail("the rendered style-guide fence does not contain its scaffold markers")
    if not scaffold.scaffold_echo(sg):
        _fail("a rendered style-guide block must be detected as a scaffold echo")
    _ok("style_guides.render_style_guide output carries (and is detected by) its markers")

    ctx = context_pkg.render_block({"source": {"system": "Core QA"}, "domain": "software"})
    if scaffold.TRUST_ATTR not in ctx or (f"</{scaffold.CONTEXT_TAG}>") not in ctx:
        _fail("the rendered translation_context fence does not contain its markers")
    if not scaffold.scaffold_echo(ctx):
        _fail("a rendered translation_context block must be detected as a scaffold echo")
    _ok("context_pkg.render_block output carries (and is detected by) its markers")

    card = {
        "score": 83,
        "gate": "fail",
        "errors": [{"dimension": "Accuracy", "severity": "major", "note": "x"}],
    }
    fnd = lqa.render_findings(card)
    if (f"</{scaffold.LQA_FINDINGS_TAG}>") not in fnd:
        _fail("the rendered lqa_findings fence does not contain its closing marker")
    if not scaffold.scaffold_echo(fnd):
        _fail("a rendered lqa_findings block must be detected as a scaffold echo")
    _ok("lqa.render_findings output carries (and is detected by) its closing marker")

    # The orchestrator's auto-inject preamble is built from the SHARED label, so the label a
    # model can echo and the marker the gate matches are literally the same string.
    if scaffold.LQA_FINDINGS_LABEL not in orchestrator._LQA_FINDINGS_PREAMBLE:
        _fail("the orchestrator preamble is not built from scaffold.LQA_FINDINGS_LABEL")
    if not scaffold.scaffold_echo(orchestrator._LQA_FINDINGS_PREAMBLE.strip()):
        _fail("the orchestrator preamble must be detected as a scaffold echo")
    _ok("orchestrator._LQA_FINDINGS_PREAMBLE is the shared label (gate matches it verbatim)")

    # And the builders' fence constants are byte-identical to the pre-fix literals (the
    # single-source refactor must not have perturbed a single prompt byte).
    if style_guides._HEAD != '<style_guide source="%s" trust="data-only">':
        _fail(f"style_guides._HEAD drifted from the byte-exact fence: {style_guides._HEAD!r}")
    if style_guides._TAIL != "</style_guide>":
        _fail(f"style_guides._TAIL drifted: {style_guides._TAIL!r}")
    if orchestrator._LQA_FINDINGS_PREAMBLE != "\n\nMQM LQA findings to resolve:\n":
        _fail(f"the preamble drifted: {orchestrator._LQA_FINDINGS_PREAMBLE!r}")
    _ok("the builder fence constants are byte-identical to the pre-fix literals")


# ---------------------------------------------------------------------------
# D. THE SEAM: every scaffold shape must-flag; text kept; NEVER clean mt; TM-ineligible.
# ---------------------------------------------------------------------------
def _seg(session, doc_id, source_text, order, locale="es-ES", key_id=None):
    s = Segment(
        document_id=doc_id,
        target_locale=locale,
        order_index=order,
        source_text=source_text,
        status=SegStatus.NEW,
        key_id=key_id,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _assert_scaffold_flagged(seg, out, label, kept_text):
    """The full must-flag battery for one scaffold-echo commit."""
    if not out.committed or out.flagged:
        _fail(f"{label}: the gate must be SOFT (committed, not a refusal-withhold), got {out!r}")
    if seg.target_text != kept_text:
        _fail(f"{label}: the gate must NEVER rewrite or withhold the text")
    if seg.status != SegStatus.NEEDS_REVIEW:
        _fail(f"{label}: must route to NEEDS_REVIEW (never clean mt), got {seg.status!r}")
    if out.tm_eligible:
        _fail(f"{label}: a scaffold echo must be TM-INELIGIBLE")
    if not out.scaffold_echo:
        _fail(f"{label}: CommitOutcome.scaffold_echo must name the leak")
    nf = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf) != 1:
        _fail("%s: expected exactly ONE nontranslation-family flag, got %d" % (label, len(nf)))  # noqa: UP031
    if nf[0].get("severity") != "major":
        _fail("{}: the flag must be MAJOR, got {!r}".format(label, nf[0].get("severity")))
    if not str(nf[0].get("reason") or "").startswith(scaffold.SCAFFOLD_ECHO_REASON):
        _fail(f"{label}: the flag must carry the distinct SCAFFOLD reason")
    for other in (
        refusal.NONTRANSLATION_REASON,
        quality.META_COMMENTARY_REASON,
        quality.LENGTH_RUNAWAY_REASON,
    ):
        if nf[0].get("reason") == other:
            _fail(f"{label}: the scaffold verdict must be DISTINGUISHABLE from the others")


def check_seam(session, doc_id):
    print("D. the write seam -- MUST-FLAG (never a clean stored mt)")
    shapes = [
        ("style-guide echo (seg 0/12)", STYLE_ECHO, CLEAN_SRC),
        ("seg-11 half-translated preamble", SEG11_ECHO, CLEAN_SRC),
        ("translation_context echo", CONTEXT_ECHO, CLEAN_SRC),
        ("lqa_findings echo", LQA_FINDINGS_ECHO, CLEAN_SRC),
        ("verbatim English preamble", ENGLISH_PREAMBLE_ECHO, CLEAN_SRC),
    ]
    order = 0
    first_seg = None
    for label, echo, src in shapes:
        s = _seg(session, doc_id, src, order)
        order += 1
        out = quality.commit_mt_output(session, s, echo, source="mt")
        session.commit()
        session.refresh(s)
        _assert_scaffold_flagged(s, out, label, echo)
        if first_seg is None:
            first_seg = s
        _ok(f"{label}: committed + text kept + NEEDS_REVIEW + scaffold flag + TM-ineligible")

    # THE KEY REQUIREMENT, stated on its own: seg 11's exact shape can NEVER be clean mt.
    s11 = _seg(session, doc_id, CLEAN_SRC, order)
    order += 1
    out11 = quality.commit_mt_output(session, s11, SEG11_ECHO, source="mt")
    session.commit()
    session.refresh(s11)
    if out11.committed and s11.status == SegStatus.MT and not s11.qa_flags:
        _fail("REGRESSION: seg 11 became a clean stored mt again -- the Q10 defect is back")
    _ok("KEY REQUIREMENT: seg 11's exact shape is impossible to store as clean mt")

    # SURFACE PARITY: the flag rides the established family, so it is visible headlessly with
    # NO edit to api_cli's allowlist or the editor's surfaced set.
    from app.web import api_cli

    row = api_cli._segment_detail_row(first_seg)
    if not any(f["type"] == refusal.NONTRANSLATION_FLAG_TYPE for f in row["qa_flags"]):
        _fail("the scaffold flag must survive the headless surfaced-flag filter")
    _ok("the flag is surfaced by the /api + CLI detail row (no allowlist edit needed)")


def check_false_positive_seam(session, doc_id):
    print("E. the seam LEAVES CLEAN prose clean (byte-identical to a no-gate commit)")
    order = 100
    for src, tgt in CLEAN_PROSE:
        s = _seg(session, doc_id, src, order)
        order += 1
        out = quality.commit_mt_output(session, s, tgt, source="mt")
        session.commit()
        session.refresh(s)
        if out.scaffold_echo:
            _fail(f"FALSE POSITIVE at the seam on legit prose: {tgt[:40]!r}")
        if s.status != SegStatus.MT or s.qa_flags is not None or not out.tm_eligible:
            _fail(
                "clean prose must commit status=mt, qa_flags None, TM-eligible; got "
                f"{s.status!r} / {s.qa_flags!r} / {out.tm_eligible!r}"
            )
    _ok("all four legit-prose pairs commit clean (mt, no flag, TM-eligible, qa_flags None)")


# ---------------------------------------------------------------------------
# F. MOCKPROVIDER REPRODUCTION -- the offline hermetic path (finding item 4).
# ---------------------------------------------------------------------------
def check_mock_reproduction(session, doc_id):
    """The mock 'reviewer' / post-editor returns the (scaffolded) text it was given; that
    echo flows through the seam and the gate catches it -- no provider, no network."""
    print("F. MockProvider echoes its scaffolded input; the gate catches it")
    # Build a REAL reviewer prompt: a rendered <style_guide> block seated at the top, exactly
    # as ROLE_PRESETS['In-country reviewer'] does via style_guide_prefix.
    guide = type(
        "G", (), {"body": "1. Use usted. 2. No exclamation marks.", "name": "Core QA es"}
    )()
    prompt = (
        style_guides.style_guide_prefix(guide)
        + "You are a native es-ES reviewer. Improve the translation below.\n\n"
        + "Hace algo con tus archivos."
    )
    mock = MockProvider()
    # The reviewer branch (task='review') returns payload['text']; a free-form agent gets the
    # generic echo of the prompt. BOTH carry the fenced scaffolding.
    echoed_review = mock.complete(prompt, task="review", payload={"text": prompt})
    echoed_agent = mock.complete(prompt)
    for label, echoed in (("task=review", echoed_review), ("generic agent echo", echoed_agent)):
        if scaffold.TRUST_ATTR not in echoed:
            _fail(f"setup: the mock {label} echo should carry the fenced scaffold")
    _ok("MockProvider (task=review AND generic agent) echoes the fenced <style_guide> block")

    seg = _seg(session, doc_id, "It does something with your files.", 200)
    out = quality.commit_mt_output(session, seg, echoed_agent, source="workflow")
    session.commit()
    session.refresh(seg)
    _assert_scaffold_flagged(seg, out, "mock-echoed scaffolded prompt", echoed_agent)
    _ok("the mock-echoed scaffolded prompt is caught at the seam (offline, hermetic)")


# ---------------------------------------------------------------------------
# G. REAL orchestrator run -- the family re-assertion survives the status=MT reset.
# ---------------------------------------------------------------------------
class _CannedCompletion:
    """Swap `orchestrator.run_completion` for one that returns a fixed text -- the reviewer
    that echoed the scaffold, replayed deterministically (no provider, no network)."""

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


def check_orchestrator_shape(session, pid):
    """The orchestrator RESETS status to MT after the seam, then re-asserts NEEDS_REVIEW off
    the nontranslation FAMILY (any flag of that type this commit wrote). The scaffold flag
    rides that family, so it is protected with NO write-back edit -- proven end to end."""
    print("G. REAL orchestrator run + the status=MT reset (BUG-06 regression class)")
    doc = Document(project_id=pid, filename="q10-wf.md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    seg = _seg(session, doc.id, CLEAN_SRC, 0)
    wf = Workflow(name="q10-wf-%d" % doc.id, scope="global")  # noqa: UP031
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
    with _CannedCompletion(SEG11_ECHO):
        for _ in orchestrator.run_workflow_stream(session, wf_id, doc_id, "es-ES"):
            pass
    session.expire_all()
    seg = session.get(Segment, seg_id)
    if (seg.target_text or "") != SEG11_ECHO:
        _fail(
            "the real workflow run must COMMIT the text (soft gate), got {!r}".format(
                (seg.target_text or "")[:40]
            )
        )
    if seg.status != SegStatus.NEEDS_REVIEW:
        _fail(
            "a REAL workflow run let the scaffold verdict be wiped by the status=MT reset "
            f"-- the BUG-06 class is back (got {seg.status!r})"
        )
    fam = [
        f
        for f in (seg.qa_flags or [])
        if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        and str(f.get("reason") or "").startswith(scaffold.SCAFFOLD_ECHO_REASON)
    ]
    if len(fam) != 1:
        _fail(f"the real run must leave exactly one scaffold verdict, got {seg.qa_flags!r}")
    if fam[0].get("source") != "workflow":
        _fail(f"the real run must record the workflow provenance, got {fam[0]!r}")
    _ok(
        "REAL run_workflow_stream: text committed, NEEDS_REVIEW survives the status=MT reset, "
        "one scaffold flag tagged source=workflow"
    )


# ---------------------------------------------------------------------------
# H. Kill switch: REFUSAL_GUARD_ENABLED=0 -> byte-identical to pre-fix.
# ---------------------------------------------------------------------------
def check_kill_switch(session, doc_id):
    print("H. REFUSAL_GUARD_ENABLED=0 -> the gate is inert (byte-identical to pre-fix)")
    prev = config.REFUSAL_GUARD_ENABLED
    try:
        config.REFUSAL_GUARD_ENABLED = False
        seg = _seg(session, doc_id, CLEAN_SRC, 300)
        out = quality.commit_mt_output(session, seg, STYLE_ECHO, source="mt")
        session.commit()
        session.refresh(seg)
        if out.scaffold_echo:
            _fail("the kill switch must disable the scaffold gate")
        if seg.status != SegStatus.MT or seg.qa_flags or not out.tm_eligible:
            _fail(
                f"guard OFF must be byte-identical to pre-fix, got {seg.status!r} / {seg.qa_flags!r} / {out.tm_eligible!r}"  # noqa: E501
            )
        if seg.target_text != STYLE_ECHO:
            _fail("guard OFF must still write the text")
        # ...and with the guard off, a stale scaffold flag is NOT cleared either.
        seg2 = _seg(session, doc_id, "Hello world.", 301)
        seg2.qa_flags = [quality.scaffold_echo_flag(scaffold.SCAFFOLD_ECHO_REASON + ": stale")]
        session.add(seg2)
        session.commit()
        quality.commit_mt_output(session, seg2, "Hola mundo.", source="mt")
        session.commit()
        session.refresh(seg2)
        if not seg2.qa_flags:
            _fail("guard OFF must not run the clearing half either")
        _ok("guard OFF: no flag, status mt, TM-eligible, and the clearing half is inert too")
    finally:
        config.REFUSAL_GUARD_ENABLED = prev


# ---------------------------------------------------------------------------
# I. Stale-badge clearing -- a clean re-commit drops the scaffold badge and NOTHING else.
# ---------------------------------------------------------------------------
def check_stale_clearing(session, doc_id):
    print("I. stale-badge clearing (scoped to the scaffold verdict only)")
    seg = _seg(session, doc_id, CLEAN_SRC, 400)
    quality.commit_mt_output(session, seg, STYLE_ECHO, source="workflow")
    session.commit()
    session.refresh(seg)
    if not [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]:
        _fail("setup: the scaffold commit must set a nontranslation-family flag")
    # A proper clean translation lands -> the badge must go, status back to MT.
    quality.commit_mt_output(session, seg, "Hace algo con tus archivos.", source="workflow")
    session.commit()
    session.refresh(seg)
    if [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]:
        _fail(f"a clean re-commit must clear the scaffold badge, got {seg.qa_flags!r}")
    if seg.status != SegStatus.MT:
        _fail(f"a clean re-commit must return the segment to MT, got {seg.status!r}")
    _ok("a clean re-commit clears the scaffold badge and restores status=mt")

    # SCOPING: clearing the scaffold verdict leaves every other family -- including the
    # LANGUAGE and LENGTH verdicts that share its flag TYPE -- intact.
    left = quality.clear_scaffold_echo_flag(
        [
            quality.scaffold_echo_flag(scaffold.SCAFFOLD_ECHO_REASON + ": x"),
            refusal.nontranslation_flag("mt"),
            quality.length_runaway_flag(quality.LENGTH_RUNAWAY_REASON + ": y"),
            {"type": "qe", "severity": "minor", "message": "keep me"},
            {"type": "dnt_violation", "severity": "major", "message": "keep me too"},
        ]
    )
    reasons = sorted(
        f.get("reason") for f in left if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
    )
    if reasons != sorted([refusal.NONTRANSLATION_REASON, quality.LENGTH_RUNAWAY_REASON + ": y"]):
        _fail(f"the LANGUAGE and LENGTH verdicts must survive the scaffold clear, got {left!r}")
    types = [f.get("type") for f in left]
    for kept in ("qe", "dnt_violation"):
        if kept not in types:
            _fail(f"clearing the scaffold badge must not touch {kept!r}, got {left!r}")
    _ok("clearing is scoped: qe / dnt / the LANGUAGE + LENGTH verdicts all survive")
    if quality.clear_scaffold_echo_flag(None) is not None:
        _fail("an untouched column must stay None (byte-identity)")
    if (
        quality.clear_scaffold_echo_flag(
            [quality.scaffold_echo_flag(scaffold.SCAFFOLD_ECHO_REASON)]
        )
        is not None
    ):
        _fail("clearing the only flag must yield None, never []")
    _ok("an unflagged / fully cleared column reads as None, never []")


# ---------------------------------------------------------------------------
# J. Subordination -- scaffold is the STRONGEST member of the shared family.
# ---------------------------------------------------------------------------
def check_subordination(session, doc_id):
    """The family holds ONE entry. Scaffold outranks language / meta / length, so a reviewer
    sees the most-specific verdict when more than one fires."""
    print("J. scaffold outranks language / meta / length in the shared family")
    # A style-guide echo against a SHORT source ALSO trips the length gate (huge surplus +
    # ratio). Scaffold must win the single family slot; length must still be REPORTED.
    short_src = "Intro"
    if not quality.length_runaway(short_src, STYLE_ECHO):
        _fail("fixture drift: the style echo vs a short source must ALSO be a length runaway")
    seg = _seg(session, doc_id, short_src, 500)
    out = quality.commit_mt_output(session, seg, STYLE_ECHO, source="workflow")
    session.commit()
    session.refresh(seg)
    nf = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf) != 1:
        _fail("the shared family must hold exactly ONE entry, got %d" % len(nf))  # noqa: UP031
    if not str(nf[0].get("reason") or "").startswith(scaffold.SCAFFOLD_ECHO_REASON):
        _fail("SCAFFOLD must win the family slot over LENGTH, got {!r}".format(nf[0].get("reason")))
    # Subordination is about the FLAG, not the FIELD: length is still reported on the outcome.
    if not out.scaffold_echo or not out.length_runaway:
        _fail("both the scaffold and length reasons must still be REPORTED on the outcome")
    if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
        _fail("either verdict must route to review and block TM")
    _ok("scaffold beats length: one flag (scaffold reason), both fields reported, TM-ineligible")

    # A scaffold echo with lang_guard ON and an English-looking target: scaffold still wins.
    seg2 = _seg(session, doc_id, "Intro", 501)
    quality.commit_mt_output(session, seg2, ENGLISH_PREAMBLE_ECHO, source="mt", lang_guard=True)
    session.commit()
    session.refresh(seg2)
    nf2 = [f for f in (seg2.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf2) != 1 or not str(nf2[0].get("reason") or "").startswith(
        scaffold.SCAFFOLD_ECHO_REASON
    ):
        _fail(f"SCAFFOLD must win over the language verdict too, got {seg2.qa_flags!r}")
    _ok("scaffold beats language: one flag (scaffold reason) even with lang_guard on")


def main():
    print("verify_scaffold_echo -- Q10 (MT scaffold-echo gate; single-sourced markers)")
    init_db()
    with Session(engine) as session:
        p = Project(
            name="Q10",
            source_locale="en-US",
            target_locales=["es-ES"],
            default_provider="mock",
            project_type=ProjectType.DOCUMENT,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="q10.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        check_fail_before(session, d.id)
        check_detector()
        check_single_source()
        check_seam(session, d.id)
        check_false_positive_seam(session, d.id)
        check_mock_reproduction(session, d.id)
        check_orchestrator_shape(session, p.id)
        check_kill_switch(session, d.id)
        check_stale_clearing(session, d.id)
        check_subordination(session, d.id)
    print("verify_scaffold_echo OK")


if __name__ == "__main__":
    main()
