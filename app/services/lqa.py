"""LLM-based LQA — MQM/DQF scoring + per-language-pair rulebooks.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports MQM_DIMENSIONS, PENALTY, LQA_PASS_THRESHOLD,
JUDGE_SKIPPED, JUDGE_OK, JUDGE_UNAVAILABLE, clamp_threshold, downgrade_numeral_identical,
_rulebook_errors, STYLE_GUIDE_OFF,
style_guide_suppressed, _style_clause, _memory_clause, GLOSSARY_CAP, APPROVED_TERM_MISUSE,
_DNT_RENDER, _glossary_entry_line, _glossary_order_key, FINDINGS_CAP, FINDINGS_NOTE_CAP,
_san_finding, render_findings, render_findings_with_spans, result_note, canon_severity,
canon_dimension, sanitize_axis_key,
_PLACEHOLDER, _is_placeholder_finding, _reject_non_verdict, _judge_shape, _score, _surviving,
_capture_lqa_findings, HumanFindingError, HumanFindingForbidden, _finding_identity,
_require_reviewer, _require_edit_owner, _require_human_dimension, log_human_finding,
_human_vendor_attribution, edit_human_finding, withdraw_human_finding from
app.services.lqa_downstream_findings_bloc.

Scoring follows the MQM model: errors are categorized by dimension and severity,
each severity carries a penalty, and the score is 100 minus the total penalty.

Offline (no provider key) the engine still produces a real, deterministic
scorecard from structural checks + the rulebook DSL, so the feature — and the
"edit a rulebook, watch the score change" loop — works with zero keys. With a
live provider, an LLM adds fluency/accuracy/style findings on top, guided by the
MQM categories and the language-pair rulebook injected into the prompt.

Rulebook DSL (data/rulebooks/<src>__<tgt>.md), one rule per line:
  FORBID <text>     -> flag target CONTAINING <text>   (Style/Awkward, minor;
                       lowercase substring match)
  REQUIRE <term>    -> flag target MISSING <term>      (Terminology, MAJOR;
                       whole-token match, case-insensitive). A MAJOR error fails
                       the LQA gate, so a REQUIRE line makes glossary/term adherence
                       gate-eligible: the LQA step's durable gate can drive a
                       run_condition/branch Fix loop (Track B / B1).
  everything else is free-form guidance passed to the LLM.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import logging
import re
import uuid
from datetime import datetime

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app import config
from app.models import LQAResult, Project, Segment, WorkflowRun
from app.services import llm_json, lqa_safety, qa, quality, scaffold
from app.services.lqa_downstream_findings_bloc import (
    _DNT_RENDER,
    _PLACEHOLDER,
    APPROVED_TERM_MISUSE,
    FINDINGS_CAP,
    FINDINGS_NOTE_CAP,
    GLOSSARY_CAP,
    JUDGE_OK,
    JUDGE_SKIPPED,
    JUDGE_UNAVAILABLE,
    LQA_PASS_THRESHOLD,
    MQM_DIMENSIONS,
    PENALTY,
    STYLE_GUIDE_OFF,
    HumanFindingError,
    HumanFindingForbidden,
    _capture_lqa_findings,
    _finding_identity,
    _glossary_entry_line,
    _glossary_order_key,
    _human_vendor_attribution,
    _is_placeholder_finding,
    _judge_shape,
    _memory_clause,
    _reject_non_verdict,
    _require_edit_owner,
    _require_human_dimension,
    _require_reviewer,
    _rulebook_errors,
    _san_finding,
    _score,
    _style_clause,
    _surviving,
    canon_dimension,
    canon_severity,
    clamp_threshold,
    downgrade_numeral_identical,
    edit_human_finding,
    log_human_finding,
    render_findings,
    render_findings_with_spans,
    result_note,
    sanitize_axis_key,
    style_guide_suppressed,
    withdraw_human_finding,
)
from app.services.providers import (
    DeadEngineError,
    ProviderSpec,
    provider_is_live,
    run_completion,
    usage_token_counts,
)

__all__ = [
    "APPROVED_TERM_MISUSE",
    "FINDINGS_CAP",
    "FINDINGS_NOTE_CAP",
    "GLOSSARY_CAP",
    "JUDGE_OK",
    "JUDGE_SKIPPED",
    "JUDGE_UNAVAILABLE",
    "LQA_PASS_THRESHOLD",
    "MQM_DIMENSIONS",
    "PENALTY",
    "STYLE_GUIDE_OFF",
    "_DNT_RENDER",
    "_PLACEHOLDER",
    "HumanFindingError",
    "HumanFindingForbidden",
    "WorkflowRun",
    "_capture_lqa_findings",
    "_finding_identity",
    "_glossary_entry_line",
    "_glossary_order_key",
    "_human_vendor_attribution",
    "_is_placeholder_finding",
    "_judge_shape",
    "_memory_clause",
    "_reject_non_verdict",
    "_require_edit_owner",
    "_require_human_dimension",
    "_require_reviewer",
    "_rulebook_errors",
    "_san_finding",
    "_score",
    "_style_clause",
    "_surviving",
    "canon_dimension",
    "canon_severity",
    "clamp_threshold",
    "datetime",
    "downgrade_numeral_identical",
    "edit_human_finding",
    "log_human_finding",
    "logging",
    "quality",
    "render_findings",
    "render_findings_with_spans",
    "result_note",
    "sanitize_axis_key",
    "scaffold",
    "style_guide_suppressed",
    "withdraw_human_finding",
]


# map the cheap structural QA flags onto MQM categories
_QA_TO_MQM = {
    "empty": ("Accuracy/Omission", "major"),
    "untranslated": ("Accuracy/Untranslated", "major"),
    "tags": ("Design/Markup", "major"),
    # AI Tagging 1.3 (the LQA-gate fix): map the inline-fidelity placeholder flag
    # (⟦rN⟧/⟦gN⟧/⟦xN⟧ drop/add/dup/reorder/nesting, from qa.check_segment) to
    # Design/Markup/MAJOR. Before this it was ABSENT from the map, so a dropped run
    # token fell through _structural_errors to the generic ("Fluency","minor") default
    # (_structural_errors' `.get(...)`), scored 99, and PASSED the LQA gate
    # (score>=90 and no major) — so tag damage almost never triggered the fix loop. A
    # MAJOR fails the gate (_score) and drives the correction loop. Operates on the
    # RESTORED text qa.check_segment receives (HIGH-1: the LQA structural path scores
    # real ⟦⟧ tokens, never «TAG_N» sentinels).
    "placeholder": ("Design/Markup", "major"),
    "numbers": ("Accuracy/Number", "minor"),
}

# QA-1 text-mechanics findings that are DELIBERATELY EXCLUDED from the LQA structural sum
# (reviewer M4). Whitespace and terminal-punctuation are editor badges + (whitespace only) a
# soft write-seam gate; they must NOT reach the LQA score, because as minors they would sum
# in and flip a card sitting exactly at the 90 pass bar, firing a correction loop (real
# provider spend) on a workflow that passed before. It also keeps the offline MockProvider's
# "[locale] " echo — which can itself trip whitespace on a leading-space source — out of the
# LQA path on keyless installs (reviewer L5).
_LQA_STRUCTURAL_SKIP = frozenset({"whitespace", "punctuation"})

# QA campaign BUG-06 — PER-KIND severity overrides on top of _QA_TO_MQM.
#
# `_structural_errors` took its severity SOLELY from the type map, so every "numbers"
# flag landed as MINOR no matter what it said. BUG-06 split that check into kinds
# (`qa.number_findings`): a VALUE CHANGE ("25 items" -> "25.000 articulos") is a
# different class of defect from a dropped/invented number or an ambiguous rendering,
# and it is the one the write seam itself gates on. A minor scores 99 and PASSES the LQA
# gate, so a corrupted figure could never drive the correction loop through this seam —
# the same shape of hole AI-Tagging 1.3 fixed for the placeholder flag.
#
# An OVERRIDE map, not "trust the flag's own severity": qa's severities are calibrated
# for the editor grid and disagree with the MQM map in the other direction too (an
# `empty` flag is minor there and MAJOR here), so adopting them wholesale would silently
# DOWNGRADE existing findings. Only the kinds named here move; every other flag is
# byte-identical.
_QA_KIND_SEVERITY = {
    ("numbers", "value"): "major",
}


def rulebook_path(source_locale: str, target_locale: str):
    # Locale tags only, never path syntax (including encoded separators).
    for locale in (source_locale, target_locale):
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale):
            raise ValueError("Invalid rulebook locale.")
    root = (config.DATA_DIR / "rulebooks").resolve()
    path = (root / f"{source_locale}__{target_locale}.md").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid rulebook path.")
    return path


def load_rulebook(source_locale: str, target_locale: str) -> str:
    """The rulebook text, or "" when there is none.

    GATE R1 MED-2: `rulebook_path` now REFUSES an invalid locale, and a READ is not a
    place to surface that. Five of the six call sites did not catch it, so a stored or
    passed-through bad locale turned into a 500 on both /api routes, a CLI traceback, and
    an aborted LQA step mid-run in the two orchestrator sites. A missing rulebook and an
    unusable locale are the same thing to every reader: nothing to apply. The WRITE path
    still raises, because there the operator must be told."""
    try:
        p = rulebook_path(source_locale, target_locale)
    except ValueError:
        return ""
    return p.read_text(encoding="utf-8") if p.exists() else ""


#: #425: the refusal every web and /api rulebook door shows under multi-org.
RULEBOOK_HOST_ONLY = (
    "LQA rulebooks apply to every organization on this install, so under multi-org they are "
    "read and changed by the host operator only. Use the CLI on the host: "
    "`admin lqa rulebook --help`."
)


def rulebook_host_only() -> bool:
    """#425: True when the rulebook doors are host-operator only. A rulebook is ONE file
    per language pair for the whole install (no org or customer key) and its text goes
    into every org's LQA judge prompt and gate, so under multi-org an org admin may
    neither read another org's rules nor change them for every org. Only the host CLI
    may. A strict no-op with the multi-org gate off (every install today)."""
    from app.services import access

    return access.multi_org_active()


def save_rulebook(source_locale: str, target_locale: str, text: str) -> None:
    """Write a pair's rulebook atomically (temp file + os.replace), so a concurrent LQA
    run never reads a half-written file. Who may call it is the door's decision (#425)."""
    import contextlib
    import os
    import tempfile

    p = rulebook_path(source_locale, target_locale)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rulebook.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _structural_errors(source: str, target: str) -> list[dict]:
    out = []
    for f in qa.check_segment(source, target):
        if f["type"] in _LQA_STRUCTURAL_SKIP:
            continue  # QA-1 whitespace/punctuation never feed LQA scoring (reviewer M4)
        dim, sev = _QA_TO_MQM.get(f["type"], ("Fluency", "minor"))
        # BUG-06: a per-KIND override wins over the type default when one exists (a
        # number VALUE change -> Accuracy/Number MAJOR, which fails the gate and drives
        # the correction loop). `kind` is absent on every other flag -> no-op.
        sev = _QA_KIND_SEVERITY.get((f["type"], f.get("kind")), sev)
        # Wave 2 (§3.1.2): stamp the machine-actor at CREATION. An additive key ignored by
        # _score / render_findings / the judge prompt; read only at persist.
        out.append(
            {"dimension": dim, "severity": sev, "note": f["message"], "source": "structural"}
        )
    return out


def _glossary_clause(glossary_terms, target: str = "", cap: int = GLOSSARY_CAP) -> str:
    """The approved-terminology instruction + injection-fenced term block, or "" when no
    terms matched. EMPTY -> ZERO bytes, so the judge prompt is byte-identical to the
    pre-Phase-2 prompt (the sacred gate).

    Capped at `cap` chars over the WHOLE clause with an honest "(+N more)" marker, so a
    2k-entry termbase matching heavily on one document can never crowd out the rulebook,
    the style guide or the text being judged."""
    terms = [t for t in (glossary_terms or []) if isinstance(t, dict)]
    terms = [
        t
        for t in terms
        if (t.get("source_term") or "") and (t.get("dnt") or (t.get("target_term") or ""))
    ]
    if not terms:
        return ""
    terms = sorted(terms, key=lambda t: _glossary_order_key(t, target))
    head = "<approved_terminology>"
    tail = "</approved_terminology>"
    lines: list[str] = []
    dropped = 0
    for i, t in enumerate(terms, 1):
        frag = _glossary_entry_line(t)
        candidate = head + "\n" + "\n".join(lines + [frag]) + "\n" + tail  # noqa: RUF005
        if len(candidate) > cap and lines:
            dropped = len(terms) - (i - 1)
            break
        lines.append(frag)
    if dropped:
        # HONEST truncation (adversarial review): the ordering puts DNT entries BELOW
        # every already-present entry, so on a document that matches many approved terms
        # a DO-NOT-TRANSLATE entry — the one HARD constraint in the block — can be the
        # thing that falls off. Say so, rather than reporting an undifferentiated count.
        # (Enforcement never depended on the judge seeing it: `quality.dnt_violations`
        # still gates the write. This is about the block not lying.)
        dnt_dropped = sum(1 for t in terms[len(lines) :] if t.get("dnt"))
        lines.append(
            "(+%d more approved term%s not shown%s)"  # noqa: UP031
            % (
                dropped,
                "" if dropped == 1 else "s",
                (
                    ", including %d do-not-translate entr%s"  # noqa: UP031
                    % (dnt_dropped, "y" if dnt_dropped == 1 else "ies")
                )
                if dnt_dropped
                else "",
            )
        )
    return (
        "The source->target mappings below are the customer's APPROVED terminology and "
        "they take PRECEDENCE OVER THE STYLE GUIDE. Do NOT report the mere use of an "
        "approved target term as an error, and do NOT report a style-guide deviation "
        "that an approved term causes. DO report an approved term that is wrong IN THIS "
        "CONTEXT -- the wrong sense, the wrong part of speech, or grammatically "
        'mis-integrated -- as an error whose "dimension" is exactly '
        f'"{APPROVED_TERM_MISUSE}". These mappings license nothing beyond the terms '
        "themselves: STILL report every style-guide deviation that an approved term did "
        "NOT cause, even in a sentence that contains one, and judge accuracy, fluency "
        "and everything else exactly as strictly as you otherwise would.\n"
        f"{head}\n" + "\n".join(lines) + f"\n{tail}\n\n"
    )


# #163: the span instruction used to live at the end of `_glossary_clause`, so the field
# was only ever requested when approved glossary terms happened to match this text. `span`
# is the structured answer to "which part of the translation is this error about", and the
# orchestrator now attributes findings to segments with it, so EVERY judge call must ask
# for it. One word changed with the move: it opened "Also give ..." when it trailed the
# approved-terminology block, and "Also" is false now that it stands on its own.
# The LQA judge system message (listed read-only by system_prompts, #356).
_JUDGE_SYSTEM = "Output only JSON."

_SPAN_CLAUSE = (
    'Give EVERY error object a "span" key whose value is the EXACT substring of '
    "the translation that the error is about, copied verbatim and never paraphrased; "
    "omit the key only when no such substring exists.\n\n"
)


def _llm_errors(
    source,
    target,
    src,
    tgt,
    rulebook,
    provider_name,
    style_guide="",
    tap_meta=None,
    spec=None,
    glossary_terms=None,
    memory_block="",
) -> tuple[list[dict], float, int, int, str, dict | None]:
    """(errors, cost_usd, tokens_in, tokens_out, judge_status, judge_shape). The
    cost/token trailer is
    the EXACT ledger spend of the ONE judge call (0s when no live call was made — not-live,
    dead engine, or a soft-degrade), so score_text can surface per-step LQA spend for the
    reports workflow axis (feat/workflow-spend).

    `judge_status` (issue #56) is one of JUDGE_SKIPPED / JUDGE_OK / JUDGE_UNAVAILABLE (see
    the constants above). It is the HONESTY seam: the empty-findings result no longer hides
    whether a live judge was even configured, ran cleanly, or failed to deliver a verdict.
    A live-but-failed judge (dead engine, provider error, empty/blank completion, reasoning-
    burned finish=length, or unparseable output) returns JUDGE_UNAVAILABLE and `score_text`
    fails the gate closed — it must never read as a clean 100/auto_approved.

    `judge_shape` (issue #96) is `_judge_shape`'s metadata dict for the one judge call, or
    None when NO call was attempted (JUDGE_SKIPPED — an offline/mock install, or the #52
    GET posture). It is returned on EVERY arm that made or attempted a call, the
    fail-closed ones included, so the orchestrator can stamp the judge's engine / attempts
    / finish reason on the LQA StepRun exactly as it does for an `llm_agent` step.

    `spec` (workflow addendum): the CALLER's resolved ProviderSpec — the workflow LQA
    step's own `_spec_for_step` result, carrying its PINNED model AND its fallback
    provider/model chain. When given it is used verbatim, so the judge honors what the
    step config says (previously this function built its OWN spec at the provider default
    model and ignored the step's model + fallback entirely — the reviewed prod bug). When
    None (every non-workflow caller — score_document, ab-tests) it falls back to the
    provider default spec, byte-identical to before this parameter existed."""
    if not provider_is_live(provider_name):
        # No live judge configured -> structural/rulebook-only scoring is the complete,
        # correct answer (offline/mock install). JUDGE_SKIPPED, NOT unavailable.
        return ([], 0.0, 0, 0, JUDGE_SKIPPED, None)
    # Undecryptable-secret guard: provider_is_live only checks that an engine:<id>'s
    # api_key_enc is PRESENT (mirrors providers.provider_is_live), not that it still
    # decrypts. A cloud engine profile whose ciphertext no longer decrypts (rotated/
    # lost data/.enc_key) passes the check above but would raise DeadEngineError out
    # of get_provider inside run_completion. LQA is a soft-degrading feature (it
    # already returns [] for a not-live provider), so treat "live but undecryptable"
    # the same way -- [] here, not a propagated exception -- rather than failing the
    # whole workflow run/score_text call over an LLM-polish step. The try/except
    # below is defense-in-depth for the same reason (belt-and-suspenders: any other
    # DeadEngineError this call raises, e.g. a race with a disable, degrades the
    # same way instead of escaping score_text).
    # Honor the step's resolved spec (pinned model + fallback chain) when the caller
    # supplied one; otherwise fall back to the provider-default spec (non-workflow
    # callers, byte-identical to before the `spec` parameter existed).
    if spec is None:
        spec = ProviderSpec(provider=provider_name, model=config.DEFAULT_MODELS.get(provider_name))
    # B3: the style-adherence clause is spliced in AFTER the rulebook and BEFORE the
    # Source/Translation block. It is "" whenever no guide block was supplied (no
    # APPROVED guide resolved, or the rulebook opted out via NO_STYLE_GUIDE), so this
    # concatenation is byte-identical to the pre-B3 prompt in the no-guide case.
    # Phase 2 (A): the approved-terminology clause is spliced in AFTER the rulebook and
    # BEFORE the style clause, so the two terminology-adjacent blocks are adjacent and
    # the glossary-beats-style-guide precedence sentence sits between them. "" whenever
    # no glossary term matched this text (or the kill switch is off) -> this
    # concatenation is byte-identical to the pre-Phase-2 prompt.
    prompt = (
        f"You are an LQA reviewer scoring an {src}->{tgt} translation using the MQM "
        f"error typology (dimensions: {', '.join(MQM_DIMENSIONS)}; severities: minor, "
        "major, critical). Apply this language-pair rulebook:\n"
        f"{rulebook or '(none)'}\n\n"
        f"{_glossary_clause(glossary_terms, target)}"
        f"{_style_clause(style_guide)}"
        # Judge-recall plumbing (Train D §7.4/LAW 5): "" and byte-identical by default —
        # inert unless llmemory_judge_recall_enabled is on AND a caller wired a block.
        f"{_memory_clause(memory_block)}"
        f"{_SPAN_CLAUSE}"
        f"Source: {source}\nTranslation: {target}\n\n"
        'Return ONLY JSON: {"errors":[{"dimension":"...","severity":"...","note":"...",'
        '"span":"..."}]}'
    )
    try:
        res = run_completion(
            spec,
            prompt,
            system=_JUDGE_SYSTEM,
            task="lqa",
            payload={"source": source, "target": target},
            tap_meta=tap_meta,
        )
    except DeadEngineError:
        # Live-configured but the engine is dead (undecryptable secret / raced disable).
        # A configured judge that could not run is UNAVAILABLE, not a clean pass (#56).
        # #96: a dead engine IS a judge failure shape -- name the class that stopped us
        # rather than a silent null/0/null row.
        #
        # NOT-RUN, NOT ATTRIBUTED (gate C round 1, L4). `provider`/`model` in this shape
        # mean "the engine that ACTUALLY RAN" -- they are written to
        # `StepRun.provider_used`/`model_used`, which `report --group-by engine` buckets
        # on. A dead engine ran nothing, so stamping it there would add a row and a count
        # for an engine that made no call. The engine we could not reach is named in the
        # human-readable `unreachable` field instead (carried on the card, never in the
        # bucketing columns), and `attempts: 0` says plainly that no call was made.
        return (
            [],
            0.0,
            0,
            0,
            JUDGE_UNAVAILABLE,
            {
                "provider": None,
                "model": None,
                "attempts": 0,
                "finish_reason": None,
                "error_class": "DeadEngineError",
                "unreachable": f"{spec.provider}/{spec.model}",
            },
        )
    # Real per-step LQA spend (feat/workflow-spend): the exact ledger cost + tokens of
    # this judge call, returned even when the parse below soft-degrades to [] (the call
    # already spent). 0s only when run_completion returned an error (no paid call).
    _cost = res.get("cost_usd") or 0.0
    _tin, _tout = usage_token_counts(res.get("usage"))
    # #96: the judge call's own shape (engine/model/attempts/finish/error class),
    # captured ONCE here so every arm below -- clean verdict and fail-closed alike --
    # returns the same honest metadata.
    _shape = _judge_shape(res)
    # #56: run_completion swallows every provider failure and returns error=<str> with an
    # empty text (all attempts + fallback exhausted — the reasoning-burned finish=length
    # case ends here), and the "billing trap" arm returns a blank text with error=None.
    # BOTH are a judge that did not deliver a verdict: fail closed to UNAVAILABLE rather
    # than parse "" into [] and report a perfect score. (The cost trailer is still returned
    # so a paid-but-empty call is booked/surfaced honestly.)
    if res.get("error") is not None or not (res.get("text") or "").strip():
        return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
    try:
        # Tolerant parse (QA-2): meta-llama-3-8b (the reference live local model)
        # reliably wraps its JSON in a prose preamble / ```json fence, which a bare
        # json.loads rejected -> the ENTIRE judge dimension (incl. B3 style adherence)
        # silently no-op'd to [] while the gate still reported auto_approved. The
        # extractor recovers the wrapped object; genuine junk still raises (a
        # JSONDecodeError) and degrades to [] exactly as before. Schema handling below
        # is unchanged — tolerance is for the WRAPPER, not the content.
        # `reject=_reject_non_verdict` (R2-3 + emptyobj/straydict sibling): the prompt's
        # template is valid JSON AND a judge may quote a STRAY dict (`Example: {}`,
        # `{"errors":"none"}`) in its preamble; either is a non-verdict dict that llm_json's
        # verdict-preference rule would return AHEAD of the real `{"errors":[...]}` verdict.
        # The reject belt skips any dict that is not a real verdict (no `"errors"` key, a
        # non-list `"errors"`, or a pure-placeholder echo) so the actual verdict after it wins
        # — or raises when the non-verdict is all there is -> JUDGE_UNAVAILABLE via the except
        # below. The CLEAN fast-path case bypasses the belt by design and is closed per shape:
        # a STRAY dict by the errors-shape backstop below, a template ECHO by the SEVERITY
        # guard further down (its `"..."` severity is off-vocab — see §7b case 2b).
        data = llm_json.loads(res["text"], reject=_reject_non_verdict)
        # Shape normalization (list-judge fix, HIGH, live-repro'd on translategemma-12b-it):
        # some judges return the findings as a TOP-LEVEL JSON ARRAY `[{...}, ...]` whenever
        # they have >=1 finding, reserving `{"errors":[...]}` for the zero-finding case.
        # For clean/fenced JSON (tonight's live shape) `llm_json.loads` hands that top-level
        # list straight back, so `data` is a `list` here and the `data.get("errors", [])`
        # below raised AttributeError, which the broad `except Exception` swallowed -> the
        # segments WITH real findings degraded to JUDGE_UNAVAILABLE, the gate fell to
        # needs_review, and a canonical tm_match->translate->lqa->post_edit run halted before
        # post-edit. Zero-finding segments returned a dict and parsed fine, which is exactly
        # why the bug hid.
        #   * an ALL-NON-DICT non-empty list (e.g. ["Accuracy: ...", "Style: ..."] — a
        #     plausible small-model emission) is NOT a findings array we can read -> fail
        #     CLOSED to JUDGE_UNAVAILABLE, preserving the pre-fix posture for that shape
        #     (it degraded to unavailable/needs_review/held before this fix; wrapping it into
        #     an empty errors list would flip it to a false auto_approve). This matches the
        #     sibling QE judge's fail-closed doctrine the last arm cites.
        #   * a list with >=1 dict IS the errors list -> wrap it so the dict path below is
        #     reused VERBATIM (its `isinstance(e, dict)` per-item filter keeps the dict
        #     findings and drops any interleaved non-dict junk — a strict improvement).
        #   * a dict passes through UNCHANGED (dict path byte-identical to before this fix).
        #   * any other JSON value (str/int/float/bool/None) is not a verdict -> fail closed
        #     to JUDGE_UNAVAILABLE EXPLICITLY here, matching the sibling QE judge's
        #     `if not isinstance(data, dict)` guard, rather than relying on an incidental
        #     exception from a later `.get()` (belt: mirrors the #56 fail-closed doctrine).
        if isinstance(data, list):
            if data and not any(isinstance(e, dict) for e in data):
                return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
            data = {"errors": data}
        elif not isinstance(data, dict):
            return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
        # R2-3 SIBLING (emptyobj/straydict) — errors-shape backstop covering the CLEAN fast
        # path. A clean reply that is nothing but a stray dict (`{}`, `{"note":"looks fine"}`,
        # `{"errors":"none"}`, `{"errors":{"x":1}}`) is returned by llm_json.loads' fast path
        # BEFORE the `reject` belt runs, so the belt above never screened it. A dict without a
        # LIST-valued `"errors"` is not a verdict: falling through would iterate to zero
        # findings -> JUDGE_OK/100/auto_approve, a SILENT pass of flagged content. Fail closed.
        # This MUST run AFTER the list normalization above, so a clean `[]` and a top-level
        # findings array (both already normalized to `{"errors": [...]}`) are unaffected; a
        # dict verdict WITH a list `"errors"` (even `{"errors":[]}`, the legit zero-finding
        # pass) is untouched. Using `isinstance(..., list)` here also turns `{"errors":5}` /
        # `{"errors":null}` / `{"errors":true}` from incidental-TypeError fail-closed (a later
        # `.get(...)` iteration would raise into the broad `except`) into an EXPLICIT
        # fail-closed — the same preference for explicit guards over incidental exceptions the
        # list-normalization block just above states for its own non-dict arm.
        if not isinstance(data.get("errors"), list):
            return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
        # ALL-NON-DICT errors list backstop (gate round 2), for the clean fast path. A
        # NON-EMPTY `errors` list with NO dict members (`{"errors":["Accuracy: ...","Style:
        # ..."]}`) survives the isinstance-list guard above but the per-item `isinstance(e,
        # dict)` filter below keeps ZERO findings -> a false JUDGE_OK/100/auto_approve.
        # WRAPPED/UNWRAPPED SYMMETRY: the IDENTICAL content as a bare top-level array already
        # fails closed at the list-normalization all-non-dict guard (~13 lines up), so the
        # wrapped shape — the one the prompt actually requests — must fail closed the same way.
        # `{"errors":[]}` (empty) stays the legit zero-finding pass; a MIXED list (>=1 dict) is
        # untouched here and lands its dict finding(s) via the filter below.
        _errs = data.get("errors")
        if _errs and not any(isinstance(e, dict) for e in _errs):
            return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
        # Strip any `adjudicated` key the JUDGE supplied (adversarial review). The
        # marker is OURS: `lqa_safety.adjudicate` writes it, `render_findings` withholds
        # every finding carrying it from the fixer's block, and `result_note` stamps it
        # into the audit trail. A judge that emits the key — on its own, or steered by
        # hostile text inside the source/target it is reading — would hide its own
        # finding from the correction loop AND make the block claim it was "adjudicated
        # as approved-glossary usage", which is a lie. Untrusted content is sanitized at
        # the boundary it enters, exactly like `_san_finding` does for the rendered text.
        #
        # Wave 2 (§3.1.2): the strip is WIDENED to also remove judge-supplied
        # `source`/`judge` keys, for the same reason it removes `adjudicated` — a judge
        # must NEVER author its own provenance (a judge emitting `"source":"human"` would
        # forge an attribution that the report ranks people by). We then stamp OUR OWN
        # `source="llm"` + the resolved `provider/model` after the strip, so every LLM
        # finding carries a truthful, machine-authored provenance.
        # L11: no trailing slash when the model is unknown — 'provider', not 'provider/'.
        _judge = (f"{spec.provider}/{spec.model}") if spec.model else str(spec.provider)
        findings = [
            {
                **{
                    k: v
                    for k, v in e.items()
                    if k not in ("adjudicated", "source", "judge", "advisory_numeral")
                },
                "source": "llm",
                "judge": _judge,
            }
            for e in data.get("errors", [])
            if isinstance(e, dict)
        ]
        # SEVERITY FAIL-CLOSED (R2-3). The judge vocabulary is prompt-constrained to
        # minor/major/critical (PENALTY also carries `neutral`=0, which lqa_safety.adjudicate
        # owns and a judge may legitimately echo — it stays in-vocab here, unchanged). Nothing
        # normalizes severities on THIS (scoring) path: canon_severity is PERSIST-ONLY (it runs
        # only when building an LQAResult row — see its docstring: "`_score` ... stay
        # byte-identical"), so `_score` maps severities straight through PENALTY.get(sev, 1). An
        # OFF-VOCABULARY severity (a hallucinated "moderate", or a placeholder "..." that slipped
        # a partial echo the reject belt did not fold) would therefore score as a benign penalty-1
        # minor and, being neither major nor critical, could AUTO-APPROVE real findings. We cannot
        # honestly score a verdict we cannot read -> treat it as UNREADABLE (UNAVAILABLE), the same
        # fail-closed posture the #56 empty/blank-text guard takes.
        #
        # NORMALIZE-AND-WRITE-BACK: `_score` reads the RAW severity value (PENALTY.get(sev, 1)
        # and membership in ("major","critical")), so a merely-uncanonical severity — "CRITICAL",
        # "Major", " major " — would PASS this guard (it is in PENALTY after strip/lower) yet
        # score as a penalty-1 non-major minor. We write the stripped/lowered key back onto the
        # finding so every downstream reader (score, adjudicate, _surviving's neutral check,
        # render_findings, canon_severity) sees the canonical key. One off-vocab severity discards
        # ALL findings in the reply (the whole verdict is unreadable) -> JUDGE_UNAVAILABLE — a
        # deliberate fail-safe: a judge that cannot name a severity delivered no scorable verdict.
        for e in findings:
            _sev = str(e.get("severity") or "").strip().lower()
            if _sev not in PENALTY:
                return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)
            e["severity"] = _sev
        return (findings, _cost, _tin, _tout, JUDGE_OK, _shape)
    except Exception:
        # A live judge ran (and may have been paid) but returned output the tolerant
        # parser still could not read as a verdict. That is a judge that did not deliver,
        # NOT a clean pass -> UNAVAILABLE (#56), so the gate cannot auto-approve on it.
        return ([], _cost, _tin, _tout, JUDGE_UNAVAILABLE, _shape)


def score_text(
    source,
    target,
    source_locale,
    target_locale,
    rulebook="",
    provider_name=None,
    style_guide="",
    tap_meta=None,
    spec=None,
    glossary_terms=None,
    lqa_threshold=None,
    skip_llm=False,
    memory_block="",
) -> dict:
    """Score one (source, target) pair -> {score, passed, errors, gate, threshold}.

    `skip_llm` (issue #52): when True, the LLM-judge dimension is NOT run at all — only
    the cheap structural + rulebook checks are computed. This is the READ (GET) posture of
    the scorecard surface, which must never make an inline per-segment provider call (O(segs)
    live calls + real spend just to LOOK at the page). The judge status is JUDGE_SKIPPED, so
    (exactly like an offline install) NO `llm_judge` key is added to the card and the surface
    treats it as "structural checks only — run Re-score for LLM findings". POST (Re-score &
    save) leaves this False and runs the full judge, unchanged.

    `lqa_threshold` (feat/lqa-configurable-threshold): the LQA step's configured
    numeric pass bar (WorkflowStep.lqa_threshold), threaded from the orchestrator's
    LQA branches. None (the default, and every non-workflow caller — score_document,
    the A/B harness) -> the built-in LQA_PASS_THRESHOLD (90), so the gate is
    byte-identical to before. When set (e.g. 94), the card PASSES only at score >= 94
    (AND still no major/critical error — the no-major policy is unchanged). The
    effective bar is echoed back on the card as `threshold` for the run surfaces.

    `style_guide` (B3): the RENDERED, already-fenced `<style_guide>` block for THIS
    run's scope (style_guides.render_style_guide), or "" when no APPROVED guide
    resolved. When present (and not suppressed by a NO_STYLE_GUIDE rulebook directive),
    the LLM judge ALSO checks adherence and reports violations under the MQM "Style"
    dimension, merged into the SAME errors list -> the SAME score/gate. When "" -> the
    judge prompt is byte-identical to today and the gate is unchanged (the sacred gate).
    A major Style violation fails the gate (_score), which run_condition/branch route
    into a Fix step — the correction loop, no orchestrator changes.

    `spec` (workflow addendum): the caller's resolved ProviderSpec (the workflow LQA
    step's `_spec_for_step`), threaded to the judge so it honors the step's PINNED model
    AND its fallback provider/model chain — the prod bug where a step-pinned judge model
    was silently replaced by the provider default and no fallback was ever attempted.
    None (every non-workflow caller) -> the provider-default spec, byte-identical.

    `glossary_terms` (Phases 2+3 of PROPOSAL-lqa-glossary-adjudication): this text's
    ALREADY-RESOLVED, org/customer-scoped glossary matches — the exact
    `glossary_module.match_terms_merged` output the run already computed for its
    translate/fix steps. This function NEVER queries the glossary itself (§8 risk 7: a
    hand-rolled scope filter is the project's most reliably-found review defect class).
    It feeds two independent, independently-kill-switched layers:
      * Phase 2 (A) — the judge prompt gains the approved-terminology block + the
        glossary-beats-style-guide precedence sentence + the
        `Terminology/Approved-term-misuse` category (`lqa_glossary_context_enabled`);
      * Phase 3 (B) — `lqa_safety.adjudicate` DOWNGRADES to `neutral` any finding whose
        span targets a legitimately-applied approved term, BEFORE `_score` and therefore
        before the gate (`lqa_adjudication_enabled`).
    None / [] -> both layers are exact identity functions and the judge prompt is
    byte-identical to the pre-Phase-2 prompt.

    `memory_block` (Train D §7.4, judge-recall plumbing, LAW 5): an OPTIONAL pre-rendered
    memory block for the judge. DEFAULT "" and byte-identical; even a supplied block is
    inert unless `llmemory_judge_recall_enabled` is on (see `_memory_clause`). No live
    caller passes it — the injection point is IDENTIFIED, not wired — so the LQA judge
    stays memory-blind, which is what lets it independently measure whether memory helped.
    Never turn the flag on without Train E's four-arm harness measuring."""
    errors = _structural_errors(source, target)
    errors += _rulebook_errors(target, rulebook)
    # Suppress the resolved guide when the rulebook opts out (zero-schema per-pair off
    # switch); "" otherwise -> _llm_errors renders a byte-identical prompt.
    sg = "" if style_guide_suppressed(rulebook) else (style_guide or "")
    if skip_llm:
        # #52 GET posture: skip the judge entirely — zero provider calls, zero spend.
        # Structural+rulebook only; the card is marked JUDGE_SKIPPED so the surface can
        # say the LLM dimension did not run on this render.
        llm_errs, cost_usd, tokens_in, tokens_out, judge_status, judge_shape = (
            [],
            0.0,
            0,
            0,
            JUDGE_SKIPPED,
            None,
        )
    else:
        llm_errs, cost_usd, tokens_in, tokens_out, judge_status, judge_shape = _llm_errors(
            source,
            target,
            source_locale,
            target_locale,
            rulebook,
            provider_name,
            style_guide=sg,
            tap_meta=tap_meta,
            spec=spec,
            # The cheap check first, so an install with no glossary never pays a Setting
            # read per LQA call (`score_text` is otherwise DB-free). None and [] both render
            # zero clause bytes, so the prompt is identical either way.
            glossary_terms=(
                glossary_terms
                if (glossary_terms and lqa_safety.glossary_context_enabled())
                else None
            ),
            # Judge-recall plumbing (Train D §7.4): threaded through but INERT by default
            # — no orchestrator caller passes a block, and `_memory_clause` is "" unless
            # llmemory_judge_recall_enabled is on. LAW 5: the judge stays memory-blind.
            memory_block=memory_block,
        )
    errors += llm_errs
    # Phase 3 (B): adjudication sits between the error ASSEMBLY and the SCORE — that
    # single placement is the parity seam (workflow LQA steps, score_document and the
    # A/B-test callers all inherit it) and it is why BOTH the numeric score AND the gate
    # reflect adjudication. A document full of approved-term "errors" used to score low
    # and FAIL the gate, triggering a fix loop that should never have run. Structural /
    # rulebook findings carry no `span`, so they are structurally untouchable here.
    errors, adjudications = lqa_safety.adjudicate(
        errors, source, target, glossary_terms, target_locale=target_locale
    )
    # Issue #345 (owner decision 2026-09-29): on a numeral/punctuation-only segment
    # whose target equals its source, an "identical to source" finding is ADVISORY,
    # not a gate-failing error — the same stance the Jev structural rule took for #335.
    # The judge prompt is not the guarantee: this deterministic downgrade runs on the
    # assembled card (judge AND structural findings alike) at the SAME parity seam as
    # Phase-3 adjudication, so every caller (workflow LQA steps, score_document, the
    # A/B harness) reflects it in both the score and the gate before `_score`. No-op
    # on every other segment, byte-identical to before.
    downgrade_numeral_identical(errors, source, target)
    # LLMemory capture seam 3 (PROPOSAL-llmemory §4.3) — placed HERE, immediately AFTER
    # adjudication and before scoring, because "surviving" is only knowable on this side
    # of the adjudicator. See `_capture_lqa_findings` for the surviving-only guard.
    _capture_lqa_findings(errors, tap_meta, target_locale)
    # Resolve the effective bar ONCE: None/blank/junk (unset) -> LQA_PASS_THRESHOLD (90);
    # a configured value clamped to [0, 100]. `_score` applies the same rule; echoing
    # `eff_threshold` on the card lets the run surfaces show the number the gate used.
    _clamped = clamp_threshold(lqa_threshold)
    eff_threshold = LQA_PASS_THRESHOLD if _clamped is None else _clamped
    score, passed = _score(errors, threshold=_clamped)
    # #56 — FAIL CLOSED ON THE GATE when a CONFIGURED-LIVE judge did not deliver a verdict.
    # A judge outage must never read as a clean 100/auto_approved: a release/workflow gate
    # cannot be passed on the strength of a MISSING judge. JUDGE_SKIPPED (offline/mock, or
    # the #52 GET posture) leaves the gate untouched — structural-only scoring is correct
    # there. Only JUDGE_UNAVAILABLE (live judge configured but errored/empty/unparseable)
    # forces needs_review; the marker below makes the card say so.
    if judge_status == JUDGE_UNAVAILABLE:
        passed = False
    # cost_usd/tokens_* (feat/workflow-spend): the exact ledger spend of the LQA judge
    # call, so an LQA workflow step stamps its real per-visit cost on its StepRun. 0s
    # when no live call was made (structural/rulebook-only scoring). Additive keys —
    # existing callers (lqa.score_document, ab-tests) ignore them.
    card = {
        "score": score,
        "passed": passed,
        "errors": errors,
        "gate": "auto_approved" if passed else "needs_review",
        # The numeric bar this card was gated against (default 90 or the step's
        # configured lqa_threshold). Additive key — existing callers ignore it;
        # the run event/step-error surfaces read it to show "gate 94".
        "threshold": eff_threshold,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }
    # #56 honesty marker on the CARD. Present ONLY when a live judge was configured (OK or
    # UNAVAILABLE) — JUDGE_SKIPPED omits the key so the offline/mock card shape is
    # byte-identical to before this change (the 318-suite baseline). "unavailable" tells
    # every surface (scorecard, workflow step-error, report) that the LLM dimension was
    # attempted but did not run, so a clean-looking structural score is not mistaken for a
    # judge-verified pass.
    if judge_status != JUDGE_SKIPPED:
        card["llm_judge"] = judge_status
    # #96 JUDGE ATTRIBUTION. The one judge call's failure SHAPE (engine, model, attempts,
    # finish reason, provider error class) rides the card so the orchestrator's LQA step
    # can stamp it on its StepRun -- the same metadata an llm_agent step already writes,
    # which is what `failure_shape` and `report run tokens --group-by engine` read.
    # Attached under the
    # SAME condition as `llm_judge` (a live judge was configured) so an offline/mock card
    # keeps its pre-#56 shape byte-identical.
    if judge_shape is not None:
        card["judge_shape"] = judge_shape
    # Audit counter (§4.B "Audit trail"): count + per-rule breakdown, attached ONLY when
    # something was actually adjudicated — so the card shape is byte-identical for every
    # install that has no glossary, and `card.get("adjudications")` is the honest probe.
    # NOTHING VANISHES: the adjudicated findings themselves are still in card["errors"],
    # at `neutral`, carrying their `adjudicated` marker with the original severity.
    if adjudications:
        card["adjudications"] = lqa_safety.adjudication_summary(adjudications)
    return card


def score_document(
    session: Session, doc_id: int, locale: str, persist: bool = True, llm: bool = True
) -> dict:
    """AI opt-out (DPAI-08): for an opted-out project the LLM judge is SKIPPED (llm=False),
    so the structural + rulebook checks, which are not AI, still run and persist."""
    from app.models import Document
    from app.services import ai_optout

    if llm:
        _doc = session.get(Document, doc_id)
        if _doc is not None and ai_optout.is_opted_out(session, _doc.project_id):
            llm = False
    return _score_document(session, doc_id, locale, persist=persist, llm=llm)


def _score_document(
    session: Session, doc_id: int, locale: str, persist: bool = True, llm: bool = True
) -> dict:
    """`llm` (issue #52): when False, every per-segment card is scored structural+rulebook
    ONLY — no inline LLM-judge call, no provider spend. This is the GET (scorecard READ)
    posture: a page view must never fan O(segments) live judge calls. The default True is
    the POST (Re-score & save) and workflow-parity behaviour.

    ISSUE #56 / F1 — DO NOT REPLACE A GOOD SCORING WITH A JUDGE OUTAGE. A `persist=True`
    scoring whose live judge came back UNAVAILABLE on any segment does NOT delete the prior
    findings and does NOT write structural-only rows over them: the pre-score delete + the
    inserts are SKIPPED entirely, the prior scoring is preserved intact, and the returned
    card carries `persisted=False` so the caller can surface "LLM judge unavailable —
    previous scoring preserved". A structural-only persist still happens ONLY when NO live
    judge was configured (every segment JUDGE_SKIPPED — the offline/mock install, today's
    behaviour, unchanged). `persisted` is True on any scoring that was actually written
    (OK or offline-SKIPPED), False on the aborted-unavailable case, and None when
    persist=False (nothing was ever going to be written)."""
    # L3 (SPEC-wave3-collaboration): the WAVE-3 re-score guard that protects hand-logged
    # human findings lives in the persist-delete predicate BELOW. Any future /api or CLI
    # re-score twin MUST call THIS function (never re-implement the pre-score delete), or
    # it will resurrect the §6.3 data-loss bug the guard closes. The single web persist
    # call site (documents.py) already routes through here.
    from app.models import Document

    doc = session.get(Document, doc_id)
    project = session.get(Project, doc.project_id)
    src = project.source_locale
    rulebook = load_rulebook(src, locale)
    segments = sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all(),
        key=lambda s: s.order_index,
    )

    # SCORE FIRST, WRITE LATER (F1): build every card in memory BEFORE touching the DB, so
    # the persist decision can see the aggregate judge status and abort a judge-outage
    # scoring without having destroyed the prior generation. The scoring loop reads only
    # Segments (never LQAResult), so moving the delete after it is behaviour-preserving.
    rows, total_pen = [], 0
    for seg in segments:
        if not (seg.target_text and seg.target_text.strip()):
            continue
        card = score_text(
            seg.source_text,
            seg.target_text,
            src,
            locale,
            rulebook,
            project.default_provider,
            skip_llm=not llm,
        )
        rows.append({"seg": seg, "card": card})
        total_pen += 100 - card["score"]

    scored = [r for r in rows if True]
    overall = round(sum(r["card"]["score"] for r in scored) / len(scored)) if scored else 100
    gate = "auto_approved" if all(r["card"]["passed"] for r in scored) else "needs_review"
    # #52/#56 aggregate LLM-judge state for the surface: "skipped" when the judge did not
    # run on this recompute (GET, or an offline install), "unavailable" when a configured
    # judge failed on any segment (the gate already reads needs_review via `passed`), else
    # "ok". Drives the "structural checks only — run Re-score for LLM findings" banner and
    # the per-segment marker without the view re-deriving it.
    statuses = [r["card"].get("llm_judge") for r in scored]
    if any(s == JUDGE_UNAVAILABLE for s in statuses):
        agg = JUDGE_UNAVAILABLE
    elif any(s == JUDGE_OK for s in statuses):
        agg = JUDGE_OK
    else:
        agg = JUDGE_SKIPPED

    persisted = None
    if persist:
        if agg == JUDGE_UNAVAILABLE:
            # F1 ABORT: a configured judge did not deliver a verdict. Preserve the prior
            # scoring — write nothing, delete nothing — so a dead-judge re-score cannot
            # wipe real findings and render a clean page. The caller reads persisted=False
            # and surfaces the hold banner.
            persisted = False
        else:
            # LOCALE-FILTERED pre-score delete (§3.1.5). The pre-Wave-2 code deleted EVERY
            # LQAResult for this document, so re-scoring `fr-FR` destroyed the `es-ES`
            # findings and a multi-locale QA report was meaningless. The predicate now
            # deletes only:
            #   * this locale's own prior findings (`== locale`) — the core fix;
            #   * legacy un-attributable rows (`IS NULL` / `== ""`, N5) — the one-time
            #     convergence arm: both mean un-attributable, the old code deleted them
            #     anyway (never worse), and after one re-score per locale every survivor
            #     carries a real locale;
            #   * AND ONLY segment-level rows (`segment_id IS NOT NULL`) — the regime filter
            #     (§3.1.5, N2). score_document always writes a real segment_id; both
            #     orchestrator LQA sites always write segment_id=None. This predicate
            #     separates the two regimes EXACTLY, so the ad-hoc re-score replaces only
            #     its own prior segment-level findings and STOPS destroying workflow-step
            #     history. WAVE 3 (§6.5): the scorecard renders persisted rows — human ones
            #     in their own block, machine ones read-only — but the Wave 2 property (no
            #     stale MACHINE row renders beside a fresh one) is preserved by construction,
            #     and the re-score guard below keeps the human rows out of this delete.
            stale = session.exec(
                select(LQAResult).where(
                    LQAResult.document_id == doc_id,
                    # L5: case-normalize BOTH sides so a row stored 'es-ES' and a re-score of
                    # 'es-es' match. The IS NULL / == '' arms stay explicit — func.lower(NULL)
                    # is NULL and lower('') never equals a real locale.
                    or_(
                        func.lower(LQAResult.target_locale) == (locale or "").lower(),
                        LQAResult.target_locale.is_(None),
                        LQAResult.target_locale == "",
                    ),
                    LQAResult.segment_id.is_not(None),
                    # WAVE 3 RE-SCORE GUARD (§6.3). A HUMAN-authored finding is never replaced
                    # by a scoring event's output. The IS NULL arm keeps the Wave 2 legacy-
                    # convergence behaviour byte-identical.
                    or_(LQAResult.finding_source.is_(None), LQAResult.finding_source != "human"),
                )
            ).all()
            for old in stale:
                session.delete(old)
            for r in rows:
                seg, card = r["seg"], r["card"]
                # One card == one scoring_id (§3.1.3), stamped on EVERY row this card
                # produces. The ad-hoc path has no workflow_run_id, so vendor attribution
                # is always (None, None) — nothing is invented (§3.1.7). dimension/severity
                # are canonicalized at persist only (§3.1.4); the in-memory card is
                # untouched, so _score and render_findings stay byte-identical.
                card_scoring_id = uuid.uuid4().hex
                for e in card["errors"]:
                    session.add(
                        LQAResult(
                            document_id=doc_id,
                            segment_id=seg.id,
                            dimension=canon_dimension(e["dimension"]),
                            severity=canon_severity(e["severity"]),
                            note=e["note"],
                            score=card["score"],
                            target_locale=locale,
                            finding_source=e.get("source"),
                            judge=e.get("judge"),
                            scoring_id=card_scoring_id,
                            vendor_id=None,
                            dispatch_id=None,
                        )
                    )
            session.commit()
            persisted = True

    return {
        "rows": rows,
        "overall": overall,
        "gate": gate,
        "segments_scored": len(scored),
        "llm_judge": agg,
        "persisted": persisted,
    }
