"""orchestrator: part2

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import re
import threading
import time
import uuid
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlmodel import Session, select

from app import config
from app import db as _db
from app.models import (
    Document,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    prompts,
    tap_envelope,
)
from app.services.orchestrator_terminology_backen import _locale_phrase
from app.services.providers import (
    ProviderSpec,
)
from app.services.segment import est_tokens

log = logging.getLogger("loc_tms.orchestrator")


def _notify_human_review_paused(run, step) -> None:
    """Feature B pause chokepoint: fire the human-review assignment email as an
    ADDITIVE side effect, wrapped so it can NEVER break the generator/run. The
    notification is itself internally guarded (own session, mailer no-op without
    SMTP, no-op without an assignee), but this outer try/except catches even an
    import/attribute error — the run path stays behavior-identical (mirrors the
    _mark_project_completed guard)."""
    try:
        from app.services import notifications

        notifications.on_human_review_paused(run, step)
    except Exception:
        log.exception("Human-review notification hook failed for run %s", getattr(run, "id", None))


def _lang_directive(src_locale: str = "", tgt_locale: str = "") -> str:
    """The sentence naming THIS call's language pair, or "" when no target is known.

    GitHub #43 root cause A: `orchestrator` passes `payload={"target": locale, ...}` to
    every completion, and NO LLM provider reads `payload` — LocalProvider, Claude,
    Gemini and OpenAI all send `_chat_messages(prompt, system)` and nothing else (only
    MockProvider and the NMT profile engines consume it). So on a workflow whose step
    prompt never interpolated {target_locale} — a hand-written or DRAFTED prompt; every
    built-in ROLE_PRESET carries it, and the plain-MT path hard-codes the pair — the
    request that went on the wire named NO language anywhere, and the model echoed the
    English source into most of a live 100-key namespace.

    The SYSTEM message is the right home for it precisely because it does not depend on
    the step author remembering a placeholder: it is composed here, unconditionally, for
    every llm_agent call on both run loops. The step prompt keeps its own {target_locale}
    when it has one (saying it twice costs nothing and reinforces it); `prompts.
    locale_warning` nudges the author when it does not."""
    tgt = _locale_phrase(tgt_locale)
    if not tgt:
        return ""
    src = _locale_phrase(src_locale)
    if src:
        return (
            f"You are working on a {src} -> {tgt} localization task. The text you output "
            f"MUST be written in {tgt}, never in {src}. "
        )
    return (
        f"You are working on a localization task whose target language is {tgt}. The "
        f"text you output MUST be written in {tgt}. "
    )


def get_steps(session: Session, workflow_id: int) -> list[WorkflowStep]:
    return sorted(
        session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)).all(),
        key=lambda s: s.order_index,
    )


def _latest_paused_steprun(session: Session, run_id: int):
    """The newest (highest-id) PAUSED StepRun for a run, or None. Shared by the
    resume-blob recovery and the headless review-inspection read so both agree on
    which pause is 'current' (newest-row-wins under a Phase-3 loop revisit)."""
    return session.exec(
        select(StepRun)
        .where(StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED)
        .order_by(StepRun.id.desc())
    ).first()


class ResumeMismatch(ValueError):
    """Issue #23 (b): an edited resume payload whose segment count does NOT match the
    paused run's boundary count. Raised by prepare_resume_seed BEFORE any run-state
    mutation so the caller can REFUSE cleanly (the run stays PAUSED and re-resumable)
    rather than terminally FAILing the run. Carries the human-readable reason."""


class UnderTranslationError(Exception):
    """Honest-failure (2026-08-13 big-doc flood): a FULL_TEXT chunk emitted an
    implausibly tiny fraction of its input, so it is not a translation. Raised from the
    FULL_TEXT chunk loop and caught alongside DeadEngineError so the step fails LOUD (no
    partial stitched body committed) instead of shipping green over an untranslated body.
    The message starts with the stable classifiable prefix `under-translation:` and names
    step / language pair / chunk / char counts / ratio. See config.UNDER_TRANSLATION_*."""


def _resolved_glossary_id(
    session, steps, source_locale, target_locale, project_content_type_id=None, notes=None
):
    """The module Glossary a run's steps PIN, or None (fall through to auto-attach /
    project-scoped matching, byte-identical to pre-module when nothing is pinned).

    A workflow selects a glossary via a step's glossary_id (the builder dropdown).
    We honour the FIRST pinned glossary that is APPROVED, whose pairs still cover the
    run's (source, target) — "first COVERING pin wins" — AND whose content type
    matches the running project (SPEC-content-types §3.3). A pin that is un-approved,
    non-covering, OR content-type-mismatched is SKIPPED (keep scanning later steps),
    rather than aborting: a step-0 pin that fails must not suppress a valid pin on a
    later step, and — critically — workflows are SHARED across projects, so a step
    pinned to the Marketing glossary must NOT inject marketing terms into a Product
    project reusing that workflow. A content-type SKIP is a run-trace note, never a
    hard failure. Falls back to None only when no step supplies a valid pin.

    project_content_type_id NULL (every untyped project/install) makes the content-type
    check a no-op — content_type_allows(g.ct, None) is True only for a NULL-type
    glossary, which is byte-identical to today (all glossaries are NULL-type)."""
    from app.models import GlossaryStatus
    from app.services import glossary_module as gm

    for st in steps:
        gid = getattr(st, "glossary_id", None)
        if not gid:
            continue
        g = gm.get(session, gid)
        if g is None or g.status != GlossaryStatus.APPROVED:
            continue
        if not gm.covers_pair(session, gid, source_locale, target_locale):
            if notes is not None:
                notes.append(
                    f"Glossary {g.name} skipped: it does not cover "
                    f"{source_locale} to {target_locale}."
                )
            continue  # non-covering pin: keep scanning later steps for a covering one
        if not gm.content_type_allows(g.content_type_id, project_content_type_id):
            if notes is not None:
                notes.append(
                    f"skipped pinned glossary {gid} (content type "
                    f"{g.content_type_id}) does not match project content type "
                    f"{project_content_type_id}"
                )
            continue  # content-type mismatch: skip, fall through to auto-attach / None
        return gid
    return None


_DRAFT_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)


_DRAFT_PREAMBLE_RE = re.compile(
    r"^(?:sure|certainly|okay|ok|of course|got it|absolutely|"
    r"here(?:'s| is| are))\b[^\n{}]*:\s*$",
    re.IGNORECASE,
)


# ONLY genuine conversational chat-openers -- content words like prompt/draft/
# response/output must NEVER be starters here (a real instruction routinely opens
# with one of those: "Output only the translated text:", "Response format:",
# "Draft in a formal register:") -- see _clean_draft docstring for why this
# alternation is deliberately narrow.
def _clean_draft(text: str) -> str:
    """Make a drafted prompt usable even when a WEAKER model (typically a small
    local/openai_compatible engine) ignores the 'Output only the prompt' meta-
    instruction and wraps its answer in chatty filler.

    This strip is FENCE-GATED, not line-shape-gated -- the ONLY case it touches is
    the classic weak-model shape "Here's your prompt:\n```\n<real prompt>\n```",
    where the real content is unambiguously delimited by a markdown code fence:
      - If the response contains EXACTLY ONE fenced block (a single ``` open/close
        pair) AND the text preceding the fence, once its (optional) leading
        conversational-opener line is removed, resolves to NOTHING BUT that fence,
        then the fence is unwrapped to its inner body -- dropping the preamble
        line along with it.
      - Otherwise (no fence, more than one fenced block, or a fence that ISN'T the
        entire remainder after the first line) the text is returned COMPLETELY
        UNTOUCHED. In particular, a preamble-shaped first line is NEVER stripped on
        its own: a real instruction commonly starts with a word like "Output" or
        "Response" and ends in ':' ("Output the translation of the text below:"),
        and even a genuine-sounding chat opener ("Here is the context:") may be
        followed directly by real content ({text}) rather than a fence -- so
        without a fence to prove the split is safe, we do nothing. A slightly
        chatty first line (or a couple of stray fences) left in the draft is far
        better than silently eating real instruction text.
      - Multiple top-level fences are also left alone (never unwrapped): there is
        no single unambiguous body to extract, and unwrapping the first one would
        produce an orphan-fence Frankenstein result.

    This NEVER raises and NEVER returns empty: if the fence-unwrap would empty the
    draft, or no unwrap condition is met, the raw (stripped) text is returned
    as-is. A draft is advisory text the user edits in the Prompt box, so a
    slightly-off shape must yield usable text, not an error and not a blank box.
    The deterministic MockProvider output (clean, no fence/preamble) passes
    through byte-identical, so the offline/hermetic suite is unaffected."""
    if not text or not text.strip():
        return text
    raw = s = text.strip()
    # Exactly one fenced block == exactly two ``` markers (open + close), whether
    # on their own lines or not. Zero means nothing to unwrap; more than two means
    # multiple/ambiguous blocks -- both left alone below.
    if s.count("```") == 2:
        lines = s.split("\n")
        candidate = s
        if len(lines) > 1 and _DRAFT_PREAMBLE_RE.match(lines[0].strip()):
            rest = "\n".join(lines[1:]).strip()
            if rest:
                candidate = rest
        # Only commit the strip if, AFTER dropping the preamble line (if any), the
        # remainder is NOTHING BUT the fence -- i.e. the fence regex consumes the
        # ENTIRE candidate (anchored ^...$). If it doesn't (real content sits
        # outside/before the fence), `s` stays the untouched raw text.
        m = _DRAFT_FENCE_RE.match(candidate)
        if m and m.group(1).strip():
            s = m.group(1).strip()
    return s or raw


def _source_text(doc: Document) -> str:
    units = (doc.skeleton or {}).get("units", [])
    return "\n\n".join(u["text"] for u in units)


def _split_for_chunks(text: str, max_tokens: int) -> list[str]:
    paras = text.split("\n\n")
    chunks: list[str] = []
    cur: list[str] = []
    tok = 0
    for p in paras:
        t = est_tokens(p)
        if cur and tok + t > max_tokens:
            chunks.append("\n\n".join(cur))
            cur, tok = [], 0
        cur.append(p)
        tok += t
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


_render = prompts.render


# placeholder substitution now lives in app.services.prompts (alias kept for compat)
def _spec_for_step(step: WorkflowStep) -> ProviderSpec:
    provider = step.provider or config.DEFAULT_PROVIDER
    return ProviderSpec(
        provider=provider,
        model=step.model or config.DEFAULT_MODELS.get(provider),
        fallback_provider=step.fallback_provider,
        fallback_model=step.fallback_model,
        retry_max=step.retry_max,
        backoff_base=step.backoff_base,
    )


def _qe_spec_for_step(step: WorkflowStep) -> ProviderSpec:
    """The judge's provider spec for a QE-gated step (roadmap gap #1/#6). Uses the
    step's optional `qe_provider` override (a built-in name or engine:<id>) when set,
    else the step's OWN provider — cheap-by-default. No failover: the judge soft-
    degrades to a skip on ANY failure (qe.score_segment), so a failover chain would
    only spend more on a nicety. Model follows the resolved provider's default."""
    provider = step.qe_provider or step.provider or config.DEFAULT_PROVIDER
    return ProviderSpec(
        provider=provider,
        model=config.DEFAULT_MODELS.get(provider),
        retry_max=step.retry_max,
        backoff_base=step.backoff_base,
    )


# Per-run QE tallies (roadmap gap #1). Threaded through a step's execution and
# surfaced on the SSE 'step' event + the run summary/'done' event. All zero unless a
# step has qe_gate ON (default OFF), so the shape is additive and off = no-op.
def _new_qe_stats() -> dict:
    return {"scored": 0, "passed": 0, "failed": 0, "skipped": 0}


DEFAULT_MAX_VISITS = 5


DEFAULT_TOTAL_STEP_CAP = 50


# --- conditional-workflow Phase 1 seams (SPEC-conditional-workflows) ---------
# These three helpers are the single shared control points BOTH orchestrator loops
# call, so the two near-duplicate paths can never drift when Phase 2/3 routing +
# scratchpad reads land on top of them. In PHASE 1 they are deliberately trivial /
# no-ops, so the `while cursor` loops execute byte-identically to the old `for` loops.
# --- conditional-workflow Phase 3: branching + bounded loop-back --------------
# The Phase-1 seam (_next_cursor) becomes load-bearing here.
# RunGuard + _next_cursor are called IDENTICALLY from both orchestrator loops so the
# two paths can never drift. All of this is INERT on a plain / Phase-2 workflow
# (no branch, no max_visits): the guard never writes run.context and _next_cursor
# always returns cursor+1 -> byte-identical output, the sacred gate.
# per-step per-run ceiling fallback (D4; overridable)
# run-level absolute backstop (D4; overridable)
def _int_setting(key: str, default: int) -> int:
    """Read a positive-int loop-safety cap from settings, tolerating a blank/garbage
    stored value (falls back to the default)."""
    from app.services import settings

    try:
        v = int(str(settings.get(key)).strip())
        return v if v >= 1 else default
    except (TypeError, ValueError):
        return default


def _step_ref(step: WorkflowStep) -> str:
    """The STABLE per-run identity a step is tracked under in the RunGuard scratchpad:
    its human key when set, else a synthetic 'id:<pk>'. Branch targets always name a
    KEY, but visit-tracking + total-step counting must cover keyless steps too (loop
    safety + outcome_index apply regardless of whether a step was named)."""
    return step.key or f"id:{step.id}"


def _has_graph(steps) -> bool:
    """True iff ANY step carries a branch or an explicit max_visits — i.e. this workflow
    is a branching graph, not a plain linear list. When False the RunGuard is inert and
    NEVER writes WorkflowRun.context (byte-identical to a plain/Phase-2 run), and
    _next_cursor always returns cursor+1 (no branch exists to resolve)."""
    return any(
        getattr(s, "branch", None) or getattr(s, "max_visits", None) is not None for s in steps
    )


# --- parallel step routing (SPEC-parallel-routing §5.2-§5.4) ------------------
# A PARALLEL GROUP is a maximal set of CONSECUTIVE steps (by order_index) sharing a
# `parallel_group` slug. Execution reaches the group's first member and FORKS; all
# members run concurrently against the SAME frozen snapshot of the working text; the
# step after the group starts only when every member has finished (the JOIN).
#
# THE INERTNESS GATE, in the same spirit as `_has_graph`: `_has_parallel(steps)` is
# computed ONCE per run, and when it is False the loops never call `_group_at` at all.
# A workflow with no groups therefore pays a single boolean per iteration and executes
# byte-identically to pre-parallel -- proven by verify_parallel_routing's
# ungrouped-byte-stability group, which diffs the full event stream and every StepRun
# row of an ungrouped run against a recording made with the seam disabled.


def _has_parallel(steps) -> bool:
    """True iff ANY step carries a parallel_group slug. When False the whole fork/join
    seam is unreachable (byte-identical to a pre-parallel run)."""
    return any(getattr(s, "parallel_group", None) for s in steps)


def _group_at(steps, cursor: int):
    """(first, last) INCLUSIVE bounds when `cursor` is the FIRST member of a parallel
    group of TWO OR MORE members; None otherwise.

    Two deliberate "None" cases:
      * a LONE member (a group of one) -> None, so it runs down the ordinary
        single-step path, byte-identically. This is why a singleton group is inert
        rather than an error (see workflows._SINGLETON_GROUP_IS_INERT).
      * a cursor that lands MID-group -> None, so the step runs alone rather than
        re-forking a partially-executed group. Save-time validation already refuses a
        branch that targets a non-first member, and Phase-1 groups contain no
        human_review step (so `resume_workflow`'s blind cursor+1 can never land here),
        making this a defense-in-depth fall-through, not a reachable path."""
    if not (0 <= cursor < len(steps)):
        return None
    g = getattr(steps[cursor], "parallel_group", None)
    if not g:
        return None
    if cursor > 0 and getattr(steps[cursor - 1], "parallel_group", None) == g:
        return None  # mid-group: not a fork point
    last = cursor
    while last + 1 < len(steps) and getattr(steps[last + 1], "parallel_group", None) == g:
        last += 1
    return (cursor, last) if last > cursor else None


def _key_index(steps) -> dict:
    """Map each keyed step's slug -> its index in the current steps list (the branch
    target lookup). Keyless steps are absent (they are never branch targets)."""
    return {s.key: i for i, s in enumerate(steps) if s.key}


_RC_REFS = ("prev_lqa", "prev_qe")


_RC_OPS = ("passed", "failed", "score_gte", "score_lt")


_RC_STEP_PREFIX = "step:"


# --- conditional-workflow Phase 2 pre-step GUARD (SPEC §5) -------------------
# The single shared control point BOTH orchestrator loops call at the loop seam to
# decide run-vs-skip. Kept out of the loop bodies (like _next_cursor) so the two
# near-duplicate paths can never drift.
# The allowlists a guard is validated against at SAVE time AND re-checked here at run
# time (defense-in-depth). Mirror wf_service._RC_REFS / _RC_OPS.
# SPEC-parallel-routing §5.5: the one new ref FORM. `step:<key>` names ONE step by its
# stable key, which is what makes a parallel group's non-routing lanes addressable (a
# bare prev_lqa is REFUSED at save time when 2+ members of one group could answer it).
def _is_step_ref(ref) -> bool:
    return isinstance(ref, str) and ref.startswith(_RC_STEP_PREFIX)


KEY_TRASH_RUN_REFUSAL = (
    "this document holds soft-deleted keys' values and is never translated; restore the keys first."
)


#: The message every launcher answers a TRASH-document run with. One string so the
#: eager refusal and the seam refusal cannot drift apart.
def _refuse_key_trash_document(doc) -> None:
    """Raise if `doc` is a key project's TRASH document (SPEC-key-based-projects §2.5).

    THE ONE DEFINITION, called from `run_workflow_stream` (the seam every launcher
    funnels through) and from `start_run` (which refuses EAGERLY, before a
    WorkflowRun row exists, so an HTTP caller gets a clean error instead of a run
    row that fails on a background thread).

    The literal `fmt` compare comes first and the keys service is imported only
    inside that branch (`retention.delete_document`'s idiom, re-review R4), so a pure
    document install imports nothing new and pays one attribute read per run."""
    if doc is not None and getattr(doc, "fmt", None) == "keys":
        from app.services import keys as _keys

        if _keys.is_trash_document(doc):
            raise ValueError(KEY_TRASH_RUN_REFUSAL)


def _refuse_archived(session, workflow_id, doc) -> None:
    """Refuse to LAUNCH a new run whose workflow or project is ARCHIVED (residual sweep:
    archive verbs). Archive retires a workflow/project from pickers and blocks new work,
    but is NOT a kill switch -- an in-flight run finishes untouched, so this fires only at
    a fresh launch (start_run / run_workflow), never on the resume path. Raises the
    service refusal (Workflow/ProjectArchivedError, both ValueError) the launch routes
    translate to a clean 400/usage error. Lazy imports (workflows imports orchestrator at
    module scope; keep the cycle out of import time). Best-effort resolution: a missing
    doc/project is left to the caller's own 404 -- this only refuses a REAL archived one."""
    from app.services import content as _content
    from app.services import workflows as _wf

    _wf.refuse_if_workflow_archived(session, workflow_id)
    # #542 item 3: a workflow of ANOTHER customer never runs on this document.
    _wf.refuse_if_workflow_customer_mismatch(session, workflow_id, doc=doc)
    if doc is not None:
        _content.refuse_if_project_archived(session, getattr(doc, "project_id", None))


_UNSET = object()


#: Sentinel distinguishing "the seam did not produce a findings block" from a block
#: whose VALUE is None (review F1: `render_findings` is annotated `-> str` today, but
#: the seam exists to host future step types -- a None-valued block must REBIND the
#: loop variable exactly as main's unconditional assignment did, never silently carry
#: the previous LQA's stale block forward).
class _StepDirective(NamedTuple):
    """What a shared `_execute_step` dispatch tells the run loop to do next.

    Exactly one outcome is in force per call:
      * stop=True         -> the loop RETURNS (the run is already PAUSED or FAILED and
                             finalized inside the seam: the pause site, the LQA dead-
                             engine failure, or a spend-cap block);
      * fallthrough=True  -> the seam handled nothing; the loop runs its OWN mode-
                             specific llm_agent fan-out for this step (the genuinely
                             divergent work stays in each loop);
      * neither (a HANDLED step, i.e. LQA) -> the loop sets cursor=next_cursor and
                             continues; `lqa_findings_block`, when not _UNSET, is the
                             fresh rendered block to thread onto the next step (a None
                             VALUE rebinds -- see _UNSET).
    """

    stop: bool = False
    fallthrough: bool = False
    next_cursor: int | None = None
    lqa_findings_block: object = _UNSET


def _load_segments(session: Session, doc_id: int, locale: str) -> list[Segment]:
    """The document's segments for one locale, in order_index order — the same
    load translate.pretranslate_stream uses."""
    return sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all(),
        key=lambda s: s.order_index,
    )


class ProjectBudgetExhausted(ValueError):
    """A NEW workflow run is refused because the document's project has used up its
    monthly USD budget (CV-2). A ValueError (like Project/WorkflowArchivedError) so the
    launch surfaces catch it and surface a clean refusal, never a 500. Resume/edit of an
    existing paused run does NOT pass through here and is never blocked."""


_IN_FLIGHT_RUN_STATUSES = (
    RunStatus.PENDING,
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
)


QUEUE_LAUNCH_FAILED_NOTE = (
    "Queued launch failed: %s. Nothing was translated for this locale/document — "
    "retry it once the install is healthy."
)


#: Every status that means "this (workflow, document, locale) already has work in flight",
#: so a second launch must re-attach to it rather than start a rival execution over the same
#: segment set. QUEUED joins the three it always held (gate A round 3): a queued row IS the
#: work, it just has not been given a slot yet, and queueing a second copy behind it would
#: double-run the segment set the moment both were admitted. Mirrored by
#: `workflows._ACTIVE_RUN_STATUSES` (the authoring guards) and `batch_runs._ACTIVE`.
#: The note stamped on a QUEUED run whose ADMISSION raised (gate A round 3, HIGH-2), so the
#: failure lands on the row itself instead of in one `log.exception` line nobody reads. The
#: sibling of WRITE_CONTENTION_NOTE / RESTART_NOTE: `run_reason` classifies it, so every
#: surface that shows a failed run shows WHY this one never started. Takes the bounded
#: exception text.
def new_queue_group() -> str:
    """A fresh opaque token identifying ONE fan-out (one 'translate all' click / one
    `keys translate` call). Written to `WorkflowRun.queue_group` on every row that fan-out
    queues; the caller reports it so an operator, an /api client or a later CLI drain can
    name exactly the rows this action created."""
    return "fanout-" + uuid.uuid4().hex[:12]


def queued_runs(session: Session, group: str) -> list[WorkflowRun]:
    """Every run row this fan-out created that is STILL waiting for a slot, oldest first."""
    if not group:
        return []
    return list(
        session.exec(
            select(WorkflowRun)
            .where(WorkflowRun.queue_group == group, WorkflowRun.status == RunStatus.QUEUED)
            .order_by(WorkflowRun.id)
        ).all()
    )


@contextmanager
def _null_scope():
    yield


def _bookkeeping_session() -> AbstractContextManager[Session]:
    """A CONTEXT MANAGER yielding a Session for a MUST-NOT-FAIL bookkeeping write (recording
    why a run died) -- use it as `with _bookkeeping_session() as s:` (gate round 10 LOW-1:
    bookkeeping.session() is a @contextmanager, so this returns a context manager, NOT a bare
    Session; the previous `-> Session` annotation invited `s = _bookkeeping_session()` which
    would hand back a _GeneratorContextManager with no `.exec`).

    Normally the pooled engine, exactly as before. But the write this exists for is the
    one that has to succeed precisely WHEN the pool is exhausted — that was the flood's
    real damage: the crash handler opened a second pooled session, timed out on it too,
    logged "Failed to mark run failed after crash", and left the run RUNNING at step 0
    with no reason for the rest of the install's life. So a pool-checkout timeout (and
    only that) falls back to `db.emergency_session()`, a pool-less engine that owes the
    QueuePool nothing.

    The checkout is FORCED here (`s.connection()`) rather than left lazy, so a pool
    timeout raises inside this function where it can be handled, not later inside the
    caller's `with` block where it would look like the original crash."""
    from app.services import bookkeeping

    return bookkeeping.session()


RUN_THREAD_PREFIX = "loc-run-"


#: Prefix of a workflow-execution thread's name; the run id follows immediately.
def _run_thread_name(run_id, workflow_id, doc_id) -> str:
    return "{}{}-wf{}-doc{}".format(
        RUN_THREAD_PREFIX,
        run_id if run_id is not None else "x",
        workflow_id,
        doc_id,
    )


_RUN_CREATE_LOCK = threading.RLock()


RESTART_NOTE = "Interrupted by a server restart."


STALE_RUN_AFTER = timedelta(hours=6)


STALE_NOTE = "Superseded: no activity for over %d hours; restarted as run #%%d." % (  # noqa: UP031
    STALE_RUN_AFTER.total_seconds() // 3600
)


STALE_NOTE_NO_SUCCESSOR = (
    f"Superseded: no activity for over {int(STALE_RUN_AFTER.total_seconds() // 3600)} hours; "
    f"swept while the install was at run capacity, so no replacement run was started."
)


RECOVERY_ACTIVITY_GRACE = timedelta(minutes=10)


SUPERSEDED_ABORT_NOTE = (
    "This run was already failed by a recovery sweep while it "
    "was still executing, so its results were discarded rather "
    "than written over a newer run's."
)


WRITE_CONTENTION_NOTE = (
    "Write contention: the database was locked by too many "
    "concurrent writes, so this run could not save its results. "
    "No partial data was written — please retry."
)


# Serializes the "is there an active run for this exact (workflow, document, locale)?
# then create one" check-and-create so it is ATOMIC across every dispatcher in this
# process (the request-thread start_run below AND the batch dispatcher, which imports
# this same lock). Without it the SELECT and the INSERT are a TOCTOU: two concurrent
# launchers (two tabs, a double-POST, two batches over one document — SPEC Phase 5a
# fan-out) both read "nothing active" and both create a run for the same segment set,
# racing writes to the same Segment rows. Held only around the brief select+insert+
# commit; the actual workflow EXECUTION runs on a separate thread AFTER release, so
# real concurrency (max_parallel members) is unaffected. In-process only — a second OS
# process (a CLI run while the server is live) is not serialized by this lock.
#: The note stamped on a run this module FAILED without the step machinery ever
#: having a say: a run whose executing thread a server restart killed, or a
#: RUNNING corpse the staleness floor swept aside. Deliberately the SAME sentence
#: `jobs.recover_interrupted` and `batch_runs.recover_interrupted_batches` use, so
#: one phrase means one thing across jobs, batches and plain runs.
#: How long a RUNNING plain run may go WITHOUT ACTIVITY before it stops being a
#: valid re-attach target (GitHub #44's staleness floor). The corpse case this exists
#: for -- a Fly redeploy / machine auto-stop / hung provider call that left the row
#: RUNNING with nothing executing -- is otherwise PERMANENT: every later attempt at
#: that (workflow, document, locale) silently re-attaches to it and the locale never
#: runs again. Six hours is comfortably longer than any real run (observe_run's own
#: tail cap is 15 minutes, the slowest observed prod run ~12 minutes) so a genuinely
#: in-flight run is never swept, and short enough that a corpse heals the same day
#: even on an install that is never restarted.
#:
#: MEASURED FROM LAST ACTIVITY, NOT ROW AGE (review F2). Row age alone made a run
#: that had been PAUSED overnight for human review and then RESUMED instantly
#: "stale" -- it kept its original started_at -- so a rival launch would sweep a run
#: that was at that moment executing. See `_last_activity_at`.
#: The note the STALENESS floor stamps, distinct from RESTART_NOTE (review F8b).
#: Nothing restarted: the run simply stopped showing any sign of life and a later
#: launch took its place, and the operator reading the run page needs to be told
#: THAT, plus which run superseded it. Takes the new run's id. Derived from
#: STALE_RUN_AFTER so the sentence cannot drift away from the rule it describes.
#: The SAME floor, stamped when the replacement launch was REFUSED at capacity (gate A
#: round 1, LOW-5). There is no successor run to name, so the sentence says what actually
#: happened instead of inventing one: the corpse is cleared, and the (workflow, document,
#: locale) it was blocking is free for the next launch that gets a slot.
#: How recently a run must have shown activity for the STARTUP healer to leave it
#: alone (review F4). The healer's founding assumption -- "nothing is executing at
#: startup" -- holds for THIS process and is false for any other: a synchronous CLI
#: `workflow run` against the same DB keeps executing while the server reboots, and
#: the boot healer would fail it out from under itself. A true boot corpse has stale
#: (or no) StepRun rows; a live run stamps one per step as it goes.
#: The note an executing run stamps on ITSELF when it discovers, at the finish line,
#: that something else already terminated its row (review F1b).
#: P-C5: the HONEST, retryable note a run gets when its execution died on a TRANSIENT
#: SQLite writer-lock (the single writer exhausted under many concurrent runs). Before
#: this the crash handler marked the run FAILED but recorded the reason nowhere the
#: observer could read, so `_failure_reason` fell through to run.notes="" and every
#: surface showed a bare `failed` / `error: null` — indistinguishable from a real
#: failure. This distinct wording tells the operator the data is intact and the fix is
#: to RETRY. `_failure_reason` reads run.notes, so it reaches SSE, the wait=true
#: summary, the /api response and the CLI alike.
def _crash_note(exc: BaseException) -> str:
    """A bounded, honest one-line reason for a run that died OUTSIDE the step machinery
    (an exception the step loop never caught, so there is no StepRun.error to fall back
    on — the same gap that produced `error: null`). Records the exception type + first
    message line so the notes fallback in `_failure_reason` shows the operator something
    actionable. Bounded because this text is rendered in a browser (matching the 500-char
    ceiling `_failure_reason` already applies to provider-supplied errors)."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return ("Run failed: {}{}".format(exc.__class__.__name__, ": " + first if first else ""))[:300]


def _caused_by_pool_timeout(exc: BaseException) -> bool:
    """True when `exc` IS a connection-pool checkout timeout, or was explicitly raised
    FROM one (`raise X from pool_timeout`, the `__cause__` chain, bounded). A wrapper
    must not hide pool exhaustion behind a generic crash note (GitHub #350)."""
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 6:
        if _db.is_pool_timeout_error(cur):
            return True
        cur = cur.__cause__
        seen += 1
    return False


def _crash_terminal_note(exc: BaseException) -> str:
    """THE one note selection every run crash handler uses (GitHub #350): the sync
    `run_workflow` arm, `_run_in_background` and the batch member `_run_member`. They used
    to carry three copies and drifted (the member stamped the crash note AND the pool
    note, so it reported `run_crashed` while the plain arms reported `reason: null`).

    Order is the contract: a pool timeout -> POOL_EXHAUSTED_NOTE (a capacity problem the
    operator can size away), a lost writer lock -> WRITE_CONTENTION_NOTE (retry in
    seconds), anything else -> the bounded `_crash_note` text. Exactly ONE sentence is
    returned, so `run_reason` resolves to `pool_exhausted` / `write_contention` /
    `run_crashed` identically on every handler."""
    if _caused_by_pool_timeout(exc):
        return _db.POOL_EXHAUSTED_NOTE
    if _db.is_locked_error(exc):
        return WRITE_CONTENTION_NOTE
    return _crash_note(exc)


def _stamp_note(run: WorkflowRun, note: str) -> None:
    """Append `note` to a run's notes (space-joined), mirroring how
    batch_runs._finalize appends to WorkflowBatch.notes. Never clobbers."""
    run.notes = ((run.notes + " ") if run.notes else "") + note


def _progress_pool_has_slot() -> bool:
    """Can a connection be checked out RIGHT NOW without queueing? (gate F round 1, H1.)

    A telemetry write may never WAIT on the connection pool -- the whole point of the
    background writer is that nothing blocks, and a writer parked 30 s inside
    `QueuePool.connect()` is a pool slot held for a counter while real work needs it. This
    is the try-acquire: a checkout can only queue when the pool has no idle connection AND
    the overflow is already at its ceiling, so that is exactly what is refused. Anything
    else (a non-QueuePool pool -- NullPool, StaticPool -- or an engine that does not expose
    the counters) never queues, so it answers True.

    Benign race: a slot seen free can be taken by another thread a microsecond later, in
    which case this ONE write waits -- bounded by the engine's `pool_timeout` (30 s by
    SQLAlchemy default, 5 s once the sibling pool train lands). It waits on the background
    writer thread, never on a translation, which is the invariant that matters; but the
    writer is SERIAL, so that one stall is head-of-line for every run's telemetry while it
    lasts (gate F round 2, L5)."""
    try:
        from app.db import engine

        pool = engine.pool
        checkedin = getattr(pool, "checkedin", None)
        max_overflow = getattr(pool, "_max_overflow", None)
        if checkedin is None or max_overflow is None:
            return True  # not a QueuePool: a checkout here never queues
        if max_overflow < 0:
            # SQLAlchemy's UNLIMITED-overflow sentinel (gate F round 2, L4): such a pool
            # never queues, so the admission test below (`overflow() < -1`, false forever)
            # would switch telemetry off permanently. Unreachable while `app/db.py` clamps
            # DB_MAX_OVERFLOW to a minimum of 0; one `minimum=` edit re-arms it.
            return True
        if checkedin() > 0:
            return True
        return pool.overflow() < max_overflow
    except Exception:
        return True


def _commit_run_terminal_direct(session: Session, run: WorkflowRun, status: str) -> None:
    """Set `run.status` to a terminal `status`, STAGE its envelope's matching terminal
    transition on the SAME session, and commit -- the direct-terminal twin of
    _commit_run_terminal (fix/tap-finalize-race, H2) for the SEGMENTED / step-failure sites
    that set run.status WITHOUT the superseded/cancel CAS. Segmented mode's only
    _commit_run_terminal call is hardcoded COMPLETED, so every SEGMENTED (and FULL_TEXT step-)
    FAILURE reached run.status=FAILED directly and left the envelope 'running' until the
    wrapper's `finally`; routing those sites through here finalizes the envelope in the SAME
    commit as run.status, closing the same poller-observable window on the failure paths.

    These sites hold the run's OWN session and set exactly `run.status`/`finished_at` before
    committing, so this is a byte-equivalent replacement PLUS the atomic envelope stage. It
    writes run.status FIRST (like _commit_run_terminal) so the stage's flush takes the writer
    lock from NONE, never a SHARED->RESERVED upgrade (N1). The stage is best-effort (SAVEPOINT-
    isolated; a caller-side flush failure surfaces as itself).

    RESIDUAL terminal sites NOT routed through here (by design -- each finalizes its envelope
    elsewhere or has none), documented so the invariant claim is honestly scoped:
      * `_empty_document_run` -- fails BEFORE `_begin_run_envelope`, so the run has NO
        envelope; there is nothing to finalize.
      * `_run_in_background`'s crash handler -- runs on a FOREIGN session after the run's
        thread died; the run's own wrapper `finally` (exception path) finalizes the envelope.
      * `recover_interrupted_runs` -- startup heal of runs whose threads DIED (no `finally`
        will ever run); their envelopes are the orphan reaper's job (-> ORPHANED, by design).
      * `start_run`'s stale-run supersede -- a corpse being FAILED; likewise the reaper's."""
    # Issue #492: never overwrite an operator CANCEL with a failure verdict. The committed
    # status is read through a SEPARATE session (the run's own identity map still holds the
    # row as this thread last saw it). Only CANCELLED is protected here: a failure exit on
    # a cancelled run just discards its staged writes and the cancel stands.
    from app.db import engine as _engine

    with Session(_engine) as _probe:
        _cur = _probe.get(WorkflowRun, run.id)
        _cur_status = _cur.status if _cur is not None else None
    if _cur_status == RunStatus.CANCELLED and status != RunStatus.CANCELLED:
        session.rollback()
        log.warning(
            "Run %s was cancelled while it was failing; its %s verdict was DISCARDED",
            run.id,
            status,
        )
        return
    run.status = status
    run.finished_at = datetime.utcnow()
    session.add(run)
    tap_envelope.stage_run_finalize_in_session(
        session,
        workflow_run_id=run.id,
        document_id=run.document_id,
        target_locale=run.target_locale,
        status=tap_envelope.status_for_run_status(status),
    )
    session.commit()


def _mark_execution_start(run: WorkflowRun) -> None:
    """Restart the staleness clock because this run is (re)entering execution NOW.

    Called at every seam where a run transitions into RUNNING, which for a RESUME is
    the fix for review F2: a run paused overnight for human review kept the
    started_at of its ORIGINAL launch, so the moment a reviewer resumed it, it was
    already older than the staleness floor and a rival launch would sweep a live,
    executing run. `started_at` now means "when this execution attempt began", which
    is also what the run page's wall-clock total should measure -- the hours a human
    spent deciding are not run time."""
    run.started_at = datetime.utcnow()


def _last_activity_at(session: Session, run: WorkflowRun):
    """The most recent sign of life for `run`: the later of when this execution
    attempt began and the newest timestamp on any of its StepRun rows.

    A step commits its StepRun the moment it finishes, so a run working through a
    long workflow keeps refreshing this even though its started_at never moves. This
    is what makes both sweepers safe against a genuinely slow run rather than merely
    a short one."""
    latest = run.started_at
    rows = session.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
    for sr in rows:
        for ts in (sr.finished_at, sr.started_at):
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    return latest


_TERMINAL = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)


def _step_event(session: Session, sr: StepRun) -> dict:
    """Render a persisted StepRun as the SAME 'step' SSE payload the live
    generator emitted, so run_view.html's handler is unchanged."""
    step = session.get(WorkflowStep, sr.step_id)
    extra = {}
    if step and step.step_type == StepType.TM_MATCH:
        run = session.get(WorkflowRun, sr.workflow_run_id)
        counts = ((run.context or {}).get("tm_offer_steps") or {}).get(str(sr.id)) if run else None
        if counts is not None:
            extra["tm_match"] = counts
    return {
        **extra,
        "type": "step",
        "run_id": sr.workflow_run_id,
        "step_run_id": sr.id,
        "step_index": step.order_index if step else 0,
        "step_name": (step.name or step.step_type) if step else "step",
        "step_type": step.step_type if step else "",
        "provider": sr.provider_used,
        "model": sr.model_used,
        "chunks": 1,
        "attempts": sr.attempts,
        "error": sr.error,
        "output": sr.output_text,
    }


def _latch_seg_shape(
    r: dict, finish: str | None, error_class: str | None
) -> tuple[str | None, str | None]:
    """Accumulate a segmented step's (finish_reason, error_class) across its segment results
    (run-190 Item C / M-3 gate round 4). WORST-CASE WINS, order-independent and self-consistent:
    the FIRST failing segment (truthy error_class) latches BOTH columns TOGETHER and is never
    overwritten, so a later CLEAN segment can neither clobber an earlier "length"/ThinkingBurn
    nor leave "stop" beside a burn. A clean "stop" is recorded only while no failure has latched.
    Called per segment in order-index order on BOTH fan-out branches (results are read in
    pending-index order, not completion order), so the stamped pair does not depend on segment
    order or on which provider call finished first."""
    if r.get("error_class"):
        if error_class is None:
            # the first failing segment: latch its finish reason + error class together.
            return r.get("finish_reason"), r.get("error_class")
        return finish, error_class  # a failure already latched -> keep it (never overwrite)
    if error_class is None:
        # no failure has latched yet: a clean segment's finish reason (a "stop") stands.
        return (r.get("finish_reason") or finish), error_class
    return finish, error_class  # a failure latched earlier -> a clean segment cannot erase it


def _observer_session(engine, attempts: int = 3, pause: float = 0.3) -> Session:
    """A Session for ONE `observe_run` poll, with a bounded retry over pool exhaustion
    (gate A round 3, MED-2).

    The checkout is FORCED (`s.connection()`) rather than left lazy, so a pool timeout
    raises HERE, where losing this poll is the whole cost, instead of inside the caller's
    `with` block where it would kill the generator and the operator's only live view of a
    running job. Only a POOL TIMEOUT is retried — a real database error is the caller's to
    surface. `attempts` x `pool_timeout` is the worst case, which is why it is 3 and not 30:
    the observer is a tail, not a transaction."""
    last: BaseException | None = None
    for _ in range(max(1, attempts)):
        s = Session(engine)
        try:
            s.connection()
            return s
        except Exception as exc:
            try:  # noqa: SIM105
                s.close()
            except Exception:  # noqa: S110 -- closing a session that never connected
                pass
            if not _db.is_pool_timeout_error(exc):
                raise
            last = exc
            time.sleep(pause)
    raise last  # type: ignore[misc]
