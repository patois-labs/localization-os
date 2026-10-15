"""cmd_workflow: part 2 (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _wf_show, _wf_add_step, _wf_edit_step,
_wf_delete_step, _wf_move_step, _wf_clone from app.cli.cmd_workflow_part2_leaf.

Documented in: docs/architecture/workflow-engine.md
"""

from app import branding
from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _branch_label,
    _opts_label,
    _prompt_snippet,
    _session,
    bounded_id,
    op_org_id,
    op_org_scope,
    remote_mode,
    render_package_view,
    require_org_scope,
)
from app.cli.cmd_workflow_common import (
    _get_wf,
    _parallel_brackets,
    _parse_branch_flags,
    _resolve_step_ref,
    _tm_match_config_args,
    _tristate_bool,
    _wait_hours,
)
from app.cli.cmd_workflow_part2_leaf import (
    _wf_add_step,
    _wf_clone,
    _wf_delete_step,
    _wf_edit_step,
    _wf_move_step,
    _wf_show,
)

__all__ = [
    "EXIT_USAGE",
    "CliError",
    "_branch_label",
    "_get_wf",
    "_opts_label",
    "_parallel_brackets",
    "_parse_branch_flags",
    "_prompt_snippet",
    "_remote_impl",
    "_resolve_step_ref",
    "_session",
    "_wf_add_step",
    "_wf_clone",
    "_wf_delete_step",
    "_wf_edit_step",
    "_wf_move_step",
    "_wf_show",
    "op_org_id",
    "op_org_scope",
    "remote_mode",
    "render_package_view",
    "require_org_scope",
]


def register_cmd_workflow_part2_sub(sub) -> None:
    p = sub.add_parser("show", help="Show a workflow and its steps.")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.set_defaults(func=_wf_show)

    p = sub.add_parser("add-step", help="Append a step to a workflow.")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.add_argument(
        "--type",
        dest="step_type",
        default="llm_agent",
        choices=["llm_agent", "lqa", "human_review", "tm_match", "external"],
        help="step type (default llm_agent). lqa/human_review/tm_match/external "
        "offer a SMALLER effective field set -- a non-default value for a "
        "field that type ignores is refused, not silently dropped; "
        "see docs/CLI.md 'Honest step-type options'. An external step "
        "dispatches the packaged text to a vendor (--vendor); its only "
        "live config is --vendor + --external-wait-hours.",
    )
    p.add_argument("--name", default="", help="step name / role label.")
    p.add_argument(
        "--provider",
        default="",
        help="provider override. Refused on human_review (no provider call is ever made).",
    )
    p.add_argument(
        "--model",
        default="",
        help="model override (free text; blank = the "
        "provider default). List a provider's models with "
        "`providers models --provider <ref>`. Refused on human_review (no "
        "engine call is ever made); an lqa step's judge honors this.",
    )
    p.add_argument(
        "--fallback-provider",
        default="",
        help="failover provider. Refused on human_review (no engine call is "
        "ever made); an lqa step's judge honors this failover chain.",
    )
    p.add_argument(
        "--fallback-model",
        default="",
        help="failover model (free text; see "
        "`providers models --provider <ref>`). Refused on human_review; an "
        "lqa step's judge honors this.",
    )
    p.add_argument(
        "--prompt",
        dest="prompt_text",
        default="",
        help="inline prompt text. Refused on lqa/human_review -- an lqa "
        "step's judge prompt is built internally (MQM template + pair "
        "rulebook) and a human_review step makes no model call.",
    )
    p.add_argument(
        "--prompt-template",
        dest="prompt_template_id",
        type=bounded_id,
        default=None,
        help="bind a SAVED prompt template (its id; see `workflow prompt "
        "list`). At run time an inline --prompt WINS; else the bound "
        "template's body is used; else the default for this type. The "
        "template must exist and (for llm_agent) its body must contain "
        "{text}/{prior}. Refused on lqa/human_review (same as --prompt).",
    )
    p.add_argument(
        "--chunk-policy",
        default="auto",
        choices=["auto", "none"],
        help="chunking policy (default auto). Refused (non-default) on "
        "lqa/human_review -- neither chunks.",
    )
    p.add_argument(
        "--redact-pii",
        dest="redact_pii",
        action="store_true",
        help="scrub PII before this step's engine call, restore after. "
        "Also honoured on lqa (scrubs before scoring); refused on "
        "human_review (no engine call to scrub for).",
    )
    # F2 engine-layer upgrades (default OFF; parity with the web step editor).
    # Refused (if set) on lqa/human_review -- both are llm_agent-only levers.
    p.add_argument(
        "--adaptive-feedback",
        dest="adaptive_feedback",
        action="store_true",
        help="F2.1: feed earlier confirmed source->target pairs of the "
        "same document into later segments' prompts (default off). "
        "Refused on lqa/human_review.",
    )
    p.add_argument(
        "--adaptive-window",
        dest="adaptive_feedback_window",
        type=int,
        default=5,
        help="F2.1 rolling window size (default 5; 0 disables).",
    )
    p.add_argument(
        "--doc-context-prepass",
        dest="doc_context_prepass",
        action="store_true",
        help="F2.2: one cheap pre-pass extracts a doc-level brief "
        "prepended to every segment prompt (default off). Refused on "
        "lqa/human_review.",
    )
    # MQM-aligned QE score + routing gate (roadmap gap #1/#6; default OFF; parity
    # with the web step editor). QE only ever runs on an llm_agent step -- refused
    # (if set) on lqa/human_review.
    p.add_argument(
        "--qe-gate",
        dest="qe_gate",
        action="store_true",
        help="score each produced segment with a QE judge (an MQM-aligned LLM judge, "
        "or a Jev quality judge picked with --qe-provider) and route below-threshold "
        "segments to human review (default off). Refused on lqa/human_review -- QE "
        "never runs there.",
    )
    p.add_argument(
        "--qe-threshold",
        dest="qe_threshold",
        type=int,
        default=70,
        help="QE pass/route cutoff, whole numbers 0-100 only; out-of-range refused "
        "(default 70; score >= threshold "
        "proceeds, below routes to review). Refused (non-default) on "
        "lqa/human_review.",
    )
    p.add_argument(
        "--qe-provider",
        dest="qe_provider",
        default="",
        help="engine for the QE judge (a provider name or engine:<id>); "
        "blank reuses the step's own provider (cheap-by-default). A quality judge "
        "is engine:<id> of kind jev (hosted, priced) or jev_local (e.g. SemIf, free), "
        "or 'jev' for the TYPESAFE_API_KEY setup; --qe-autofix works only with a Jev "
        "judge. Unknown names and missing engines are refused; disabled judges warn. "
        "Refused on lqa/human_review.",
    )
    # QE auto-fix (Train B; default OFF; parity with the web step editor). Live only with
    # --qe-gate and a Jev judge (--qe-provider jev or engine:<id> of kind jev/jev_local).
    p.add_argument(
        "--qe-autofix",
        dest="qe_autofix",
        action="store_true",
        help="auto-fix QE failures: an LLM post-editor rewrites a segment that fails "
        "a Jev QE gate and Jev re-scores it; still failing -> review with the "
        "scoring history (default off; needs --qe-gate with a Jev judge).",
    )
    p.add_argument(
        "--qe-autofix-provider",
        dest="qe_autofix_provider",
        default="",
        help="the post-editor LLM (a provider name or engine:<id> of an LLM kind); "
        "blank = the step's own provider. NMT and judge engines are refused.",
    )
    p.add_argument(
        "--qe-autofix-max-rounds",
        dest="qe_autofix_max_rounds",
        default=3,
        help="post-editor rounds per failed segment, whole numbers 1-5 only; "
        "out-of-range refused (default 3; blank restores 3).",
    )
    p.add_argument(
        "--qe-autofix-cross-check",
        dest="qe_autofix_cross_check",
        action="store_true",
        help="also score each accepted rewrite with the post-editor LLM's own second opinion "
        "shown to it (default off).",
    )
    # LQA step configurable pass threshold (feat/lqa-configurable-threshold). lqa-only
    # -- refused (if set) on non-lqa steps. Default None so an unset lqa step keeps the
    # built-in gate (score >= 90 AND no major/critical error).
    p.add_argument(
        "--lqa-threshold",
        dest="lqa_threshold",
        type=int,
        default=None,
        help="LQA pass threshold 0-100 for an lqa step: this is the NUMERIC "
        "FLOOR only -- the step passes when score >= this AND the card "
        "carries no major/critical MQM error. A major/critical error "
        "routes to review REGARDLESS of score (so a 94 can still fail "
        "even at a bar of 90). Default 90. Refused on non-lqa steps.",
    )
    # TM few-shot examples (SPEC-context-metadata phase 2B; default ON; parity with
    # the web step editor). llm_agent-only -- refused (if turned off) on lqa/human_review.
    p.add_argument(
        "--no-tm-examples",
        dest="tm_examples",
        action="store_false",
        default=True,
        help="disable TM few-shot: do NOT inject the top-N fuzzy TM matches "
        "as example pairs into this step's prompt (default ON). "
        "Refused on lqa/human_review.",
    )
    p.add_argument(
        "--tm-examples-window",
        dest="tm_examples_window",
        type=int,
        default=3,
        help="how many top fuzzy TM matches to inject, clamped 0-5 "
        "(default 3; 0 disables like --no-tm-examples).",
    )
    # Per-string image context (Batch 14 T2, SPEC §25; default ON; the --no-tm-examples
    # store_false convention). llm_agent-only; inert (accepted, ignored) on other types.
    p.add_argument(
        "--no-image-context",
        dest="use_image_context",
        action="store_false",
        default=True,
        help="do NOT send the string's attached images to this step (default ON). "
        "Only affects llm_agent steps on a vision-capable engine.",
    )
    # Concept-glossary renderer (Terminology backend T3, SPEC §9-T3; default OFF, the
    # store_true convention). llm_agent-only; refused (if set) on non-agent step types.
    p.add_argument(
        "--concept-glossary",
        dest="concept_glossary_render",
        action="store_true",
        default=False,
        help="render matched glossary concepts (audience-gated, ordered, capped) into "
        "this step's prompt instead of the plain term list (default OFF). "
        "Only affects llm_agent steps.",
    )
    p.add_argument(
        "--glossary-concept-cap",
        dest="glossary_concept_cap",
        type=int,
        default=10,
        help="at most this many concepts in the rendered glossary block "
        "(default 10; 0 = no concept cap). Only with --concept-glossary.",
    )
    p.add_argument(
        "--glossary-token-cap",
        dest="glossary_token_cap",
        type=int,
        default=0,
        help="token ceiling for the rendered glossary block "
        "(default 0 = inherit the install glossary_prompt_token_cap). "
        "Only with --concept-glossary.",
    )
    # LQA safety floor D1 (PROPOSAL-lqa-glossary-adjudication Phase 1): the TM
    # provenance guard's per-step TRISTATE override (parity with the web step editor's
    # TM protection select). llm_agent-only -- refused (if set) on lqa/human_review.
    p.add_argument(
        "--tm-protect",
        dest="tm_protect",
        default="auto",
        choices=["auto", "on", "off"],
        help="TM provenance guard: exclude segments filled by a 100%% TM "
        "match from THIS step. 'auto' (default) protects only a "
        "correction-loop step (gated on prev_lqa:failed, or an LQA "
        "on_fail loop-back target) so a fix step cannot silently "
        "rewrite an approved 100%% match and report it as fresh MT; "
        "'on' always protects; 'off' never does. Refused on "
        "lqa/human_review. Global kill switch: config setting "
        "tm_fix_protection_enabled.",
    )
    # run-190 Item D / M5: the over-translation (page-dump) guard's per-step TRISTATE
    # opt-out (parity with the web step editor + /api + MCP). llm_agent-only.
    p.add_argument(
        "--over-translation-guard",
        dest="over_translation_guard",
        default="auto",
        choices=["auto", "on", "off"],
        help="over-translation (page-dump) guard for THIS llm_agent step: "
        "'auto' (default) / 'on' inherit the install guard (rejects a candidate "
        "that looks like a whole-page dump, far longer than the source, and "
        "withholds it to review); 'off' opts this step out (use for a "
        "legitimately EXPANDING rewrite / post-edit / summarize step). Refused on "
        "lqa/human_review/tm_match/external. Global kill switch: config setting "
        "over_translation_max_ratio (0 disables install-wide).",
    )
    # LLMemory step-level opt-out (PROPOSAL-llmemory §6): parity with the web step
    # editor's LLMemory recall select. llm_agent-only -- refused (if set) on
    # lqa/human_review, same as --tm-protect.
    p.add_argument(
        "--memory-mode",
        dest="memory_mode",
        default="auto",
        choices=["auto", "off"],
        help=f"{branding.llmemory_name()} recall opt-out: 'auto' (default) lets this step "
        "participate in memory recall normally; 'off' means NO memory "
        "block is ever injected into this step's prompts. Opt-out ONLY "
        "-- cannot turn recall on for a step the orchestrator's own "
        "role predicate excludes (an LQA/judge or correction-loop "
        "fixer step). Refused on lqa/human_review.",
    )
    # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the tm_match step's per-step TM policy.
    # LIVE only on a tm_match step (refused on every other type). Defaults mean "inherit",
    # which SEEDS a concrete snapshot of the current globals on a NEW tm_match step.
    _tm_match_config_args(p, editing=False)
    p.add_argument(
        "--glossary",
        dest="glossary_id",
        type=bounded_id,
        default=None,
        help="pin an APPROVED module glossary (parity with the web step glossary "
        "dropdown; an invalid/unapproved/foreign id is an error). A glossary that does "
        "not cover a run's languages is skipped at run time with a note. "
        "Refused on lqa/human_review -- neither step's own scoring/"
        "review reads it (pin it on an llm_agent step instead).",
    )
    p.add_argument(
        "--run-condition",
        dest="run_condition",
        default="",
        help="pre-step GUARD: run this step only if the prior LQA/QE outcome "
        "matches. Compact form 'ref:op' or 'ref:op:value' where ref is "
        "prev_lqa|prev_qe|step:<key> and op is "
        "passed|failed|score_gte|score_lt (e.g. prev_lqa:failed, "
        "prev_lqa:score_lt:90, step:lqa_main:failed). Requires a "
        "matching earlier LQA/QE-gated step or it is rejected. The "
        "step:<key> form names ONE earlier gate step by its key -- "
        "required when the nearest matching steps are members of a "
        "parallel group (a bare prev_lqa would be ambiguous). Blank = "
        "always run.",
    )
    # Conditional-workflow Phase 3 (SPEC §5; D1/D3/D4): stable key + post-step branch +
    # per-step loop ceiling (parity with the web builder's branch editor).
    p.add_argument(
        "--key",
        dest="key",
        default="",
        help="stable step key (a slug, unique within this workflow) so branch "
        "targets can name this step. Blank = unnamed (not a branch target).",
    )
    p.add_argument(
        "--branch",
        dest="branch",
        action="append",
        default=[],
        metavar="on_pass=<target>|on_fail=<target>",
        help="post-step BRANCH on a gate-producing step (an LQA step, or an "
        "llm_agent step with --qe-gate). Repeatable; target is 'next', "
        "'end', or an existing step KEY (an earlier key = a bounded "
        "loop-back). E.g. --branch on_fail=fix --branch on_pass=end.",
    )
    p.add_argument(
        "--max-visits",
        dest="max_visits",
        type=int,
        default=None,
        help="per-step per-run execution ceiling for loop safety (D4). Binds THIS "
        "step whatever role it plays in the graph -- as a loop-back target (the "
        "jump into it is refused) and as any other step (issue #94: it is checked "
        "before the step runs, so a gate that is the branch SOURCE is bounded too). "
        "A capped CHECK (lqa / QE-gated) ends the run; a capped producer/fixer is "
        "SKIPPED so the check still judges the last produced text. A run that ends "
        "with an LQA check's standing verdict still FAILING is HELD, whatever the loop "
        "shape (and so is a check the judge never answered; a QE routing gate is NOT a "
        "hold -- it routes its low segments to review and the document still ships): "
        "its segments land needs-review and the export is refused (409, not bypassable "
        "with --allow-held) until a reviewer approves them, or until a later run whose "
        "OWN check PASSES over the text that run saves (a polish step that REWRITES a "
        "unit after the check leaves it held; one that rewrites nothing withholds "
        "nothing, so step order never decides it). A unit a human approved or edited is "
        "never demoted by a later run, and approving one clears its badge. When it was a "
        "QE-gated step's OWN correction loop that ended the run, a later run whose QE "
        "gate passes over the new text releases it too. A soft cap either way -- only "
        "the run-level "
        "total-step cap FAILS a run. Blank = inherit the run-level default (config "
        "workflow_max_visits, 5).",
    )
    # SPEC-parallel-routing §6: parallel step routing (parity with the web builder's
    # "Parallel steps" field). lqa-only in Phase 1 -- refused on llm_agent/human_review.
    p.add_argument(
        "--parallel-group",
        dest="parallel_group",
        default="",
        metavar="SLUG",
        help="run this step IN PARALLEL with the CONSECUTIVE neighbouring "
        "steps that carry the same group name. All members score the "
        "SAME frozen text at the same time and the next step waits for "
        "all of them (the join). Phase 1 is CHECKS ONLY: members must "
        "be lqa steps (refused on llm_agent/human_review). At most one "
        "member may carry a --branch (it routes at the join). Blank = "
        "not in a group (the default).",
    )
    # PROPOSAL-external-processing-step §9 (decision 6): the LIVE config of an external
    # step -- the Vendor it dispatches to + the optional wait-window override. Both are
    # DEAD fields on every non-external type (the service validates fail-closed and
    # refuses a stray --vendor on an llm_agent/lqa/... step, so nothing is added here).
    p.add_argument(
        "--vendor",
        dest="vendor_id",
        type=bounded_id,
        default=None,
        help="the Vendor an external step dispatches to (must be selectable "
        "and linked to the document's customer -- the fail-closed scope). "
        "Refused on non-external steps.",
    )
    p.add_argument(
        "--external-wait-hours",
        dest="external_wait_hours",
        type=_wait_hours,
        default=None,
        help="external step: hours to wait for the vendor's return before the "
        "run pauses/times out (blank = the service default). Refused on "
        "non-external steps.",
    )
    p.set_defaults(func=_wf_add_step)

    # --- edit-step: PARTIAL update of an existing step (only passed flags change) ---
    p = sub.add_parser(
        "edit-step",
        help="Edit an existing step in place (PARTIAL: only the flags you "
        "pass change; the rest are untouched). Re-validates the whole graph, fail-closed.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="workflow id (for context).")
    p.add_argument("step_id", type=bounded_id, help="step id to edit.")
    # Non-boolean fields: default None = "not passed = leave unchanged"; an explicit
    # value (incl. an empty string for the clearable text fields) is a real edit.
    p.add_argument("--name", default=None, help="new step name / role label.")
    p.add_argument(
        "--type",
        dest="step_type",
        default=None,
        choices=["llm_agent", "lqa", "human_review", "tm_match", "external"],
        help="new step type. A type switch never refuses: every field "
        "the NEW type ignores is force-cleared to its safe default "
        "and reported (type_switch_cleared) -- see docs/CLI.md "
        "'Honest step-type options'.",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="provider override ('' = clear to the default; a step pinned to "
        "an engine:<id> keeps its pin on '' -- re-pin explicitly to change it).",
    )
    p.add_argument("--model", default=None, help="model override ('' = clear).")
    p.add_argument(
        "--fallback-provider",
        default=None,
        help="failover provider ('' = clear; an engine:<id> pin is kept, same as --provider).",
    )
    p.add_argument("--fallback-model", default=None, help="failover model ('' = clear).")
    p.add_argument("--prompt", dest="prompt_text", default=None, help="inline prompt text.")
    p.add_argument(
        "--prompt-template",
        dest="prompt_template_id",
        default=None,
        help="bind a SAVED prompt template by id; '' clears the binding. "
        "Inline --prompt still wins at run time. The template must exist "
        "and (for llm_agent) its body must contain {text}/{prior}.",
    )
    p.add_argument(
        "--chunk-policy", default=None, choices=["auto", "none"], help="chunking policy."
    )
    p.add_argument(
        "--assignee-email",
        default=None,
        help="assignee email for a human_review OR external step (emailed on "
        "pause/dispatch/expiry-escalation; '' = clear; inert on other types).",
    )
    p.add_argument(
        "--adaptive-window",
        dest="adaptive_feedback_window",
        type=int,
        default=None,
        help="F2.1 rolling window size (0 disables).",
    )
    p.add_argument(
        "--qe-threshold",
        dest="qe_threshold",
        type=int,
        default=None,
        help="QE pass/route cutoff, whole numbers 0-100 only; out-of-range refused.",
    )
    p.add_argument(
        "--qe-provider",
        dest="qe_provider",
        default=None,
        help="engine for the QE judge ('' = reuse the step's own provider; an "
        "engine:<id> pin is kept, same as --provider). An LLM, a quality judge "
        "(engine:<id> of kind jev / jev_local), or 'jev' for the TYPESAFE_API_KEY setup; "
        "QE auto-fix works only with a Jev judge.",
    )
    p.add_argument(
        "--qe-autofix-provider",
        dest="qe_autofix_provider",
        default=None,
        help="the QE auto-fix post-editor LLM; '' clears a pinned post-editor back to "
        "the step's own provider. NMT/judge engines and unknown names are refused.",
    )
    p.add_argument(
        "--qe-autofix-max-rounds",
        dest="qe_autofix_max_rounds",
        default=None,
        help="QE auto-fix post-editor rounds per failed segment, whole numbers 1-5 only; "
        "out-of-range refused; blank restores 3.",
    )
    p.add_argument(
        "--lqa-threshold",
        dest="lqa_threshold",
        type=int,
        default=None,
        help="LQA pass threshold 0-100 for an lqa step: the NUMERIC FLOOR "
        "only -- passes when score >= this AND no major/critical MQM "
        "error; a major/critical error routes to review regardless of "
        "score. Not passed = unchanged; refused on non-lqa steps.",
    )
    p.add_argument(
        "--tm-examples-window",
        dest="tm_examples_window",
        type=int,
        default=None,
        help="top fuzzy TM matches to inject, clamped 0-5.",
    )
    p.add_argument(
        "--tm-protect",
        dest="tm_protect",
        default=None,
        choices=["auto", "on", "off"],
        help="TM provenance guard tristate: 'auto' (correction-loop steps "
        "only), 'on' (always), 'off' (never). Not passed = unchanged.",
    )
    # run-190 Item D / M5: update-step twin of --over-translation-guard (default None =
    # unchanged). llm_agent-only opt-out of the page-dump guard.
    p.add_argument(
        "--over-translation-guard",
        dest="over_translation_guard",
        default=None,
        choices=["auto", "on", "off"],
        help="over-translation (page-dump) guard tristate for an llm_agent step: "
        "'auto'/'on' inherit the install guard, 'off' opts this step out. "
        "Not passed = unchanged.",
    )
    p.add_argument(
        "--memory-mode",
        dest="memory_mode",
        default=None,
        choices=["auto", "off"],
        help=f"{branding.llmemory_name()} recall opt-out: 'auto' (participate normally) or "
        "'off' (never inject a memory block here). Not passed = "
        "unchanged.",
    )
    # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the tm_match config edit flags. All
    # default None (not passed = unchanged); an "auto"/blank value clears the override.
    _tm_match_config_args(p, editing=True)
    p.add_argument(
        "--glossary",
        dest="glossary_id",
        type=bounded_id,
        default=None,
        help="pin an APPROVED module glossary (re-validated like add-step).",
    )
    p.add_argument(
        "--run-condition",
        dest="run_condition",
        default=None,
        help="pre-step GUARD 'ref:op[:value]' (e.g. prev_lqa:failed, "
        "step:lqa_main:failed); '' = clear the guard (always run).",
    )
    p.add_argument(
        "--key",
        dest="key",
        default=None,
        help="stable step key (slug, unique in this workflow); '' = clear.",
    )
    p.add_argument(
        "--branch",
        dest="branch",
        action="append",
        default=None,
        metavar="on_pass=<target>|on_fail=<target>",
        help="post-step BRANCH on a gate step; repeatable. Target is 'next', "
        "'end', or a step KEY. Use --clear-branch to remove a branch.",
    )
    p.add_argument(
        "--clear-branch",
        dest="clear_branch",
        action="store_true",
        help="remove this step's branch entirely (mutually exclusive with --branch).",
    )
    p.add_argument(
        "--max-visits",
        dest="max_visits",
        type=int,
        default=None,
        help="per-step per-run execution ceiling (loop safety, D4). Binds this "
        "step's OWN executions in every role (issue #94); reaching it ends the run "
        "there, COMPLETED and flagged.",
    )
    # PROPOSAL-external-processing-step §9: an external step's live config (partial:
    # not passed = unchanged). Threaded into update_step only when given; the service
    # re-validates fail-closed (a stray vendor on a non-external step is refused, and a
    # switch AWAY from external force-clears the vendor).
    p.add_argument(
        "--vendor",
        dest="vendor_id",
        type=bounded_id,
        default=None,
        help="set the Vendor an external step dispatches to (re-validated: "
        "selectable + linked to the document's customer).",
    )
    p.add_argument(
        "--external-wait-hours",
        dest="external_wait_hours",
        type=_wait_hours,
        default=None,
        help="external step: hours to wait for the vendor's "
        "return before the run pauses/times out.",
    )
    # SPEC-parallel-routing §6: join / leave a parallel group. Two flags rather than the
    # '' convention the other clearable text fields use, because argparse cannot tell
    # "--parallel-group ''" apart from "not passed" once the default is also '' -- and
    # LEAVING a group is a real edit that must be expressible.
    par_grp = p.add_mutually_exclusive_group()
    par_grp.add_argument(
        "--parallel-group",
        dest="parallel_group",
        default=None,
        metavar="SLUG",
        help="join this step to a parallel group (see add-step). "
        "Members must be CONSECUTIVE lqa steps.",
    )
    par_grp.add_argument(
        "--no-parallel-group",
        dest="no_parallel_group",
        action="store_true",
        help="remove this step from its parallel group (it then runs "
        "on its own, in order, as before).",
    )
    # Tri-state boolean toggles: --flag sets True, --no-flag sets False, neither leaves
    # it unchanged (default None sentinel).
    _tristate_bool(p, "--redact-pii", "redact_pii", "scrub PII before this step's call.")
    _tristate_bool(
        p, "--adaptive-feedback", "adaptive_feedback", "feed earlier segments into later prompts."
    )
    _tristate_bool(
        p,
        "--doc-context-prepass",
        "doc_context_prepass",
        "prepend a doc-level brief to every segment prompt.",
    )
    _tristate_bool(
        p,
        "--qe-gate",
        "qe_gate",
        "score each segment with the QE judge (an LLM or a Jev quality judge, see "
        "--qe-provider) and route below-threshold to review.",
    )
    # QE auto-fix (Train B): tri-state on edit, the --qe-gate pattern.
    _tristate_bool(
        p,
        "--qe-autofix",
        "qe_autofix",
        "auto-fix Jev QE failures with an LLM post-editor before routing to review.",
    )
    _tristate_bool(
        p,
        "--qe-autofix-cross-check",
        "qe_autofix_cross_check",
        "score each accepted rewrite with the post-editor LLM's own second opinion; "
        "no baseline call.",
    )
    _tristate_bool(
        p, "--tm-examples", "tm_examples", "inject top-N fuzzy TM matches as example pairs."
    )
    # Batch 14 T2 (SPEC §25.5-D): the per-step image-context toggle, tri-state on edit
    # (present-true / present-false / absent-unchanged) via the --qe-gate pattern.
    _tristate_bool(
        p,
        "--image-context",
        "use_image_context",
        "send the string's attached images to this llm_agent step (vision engines only).",
    )
    # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle (tri-state on edit)
    # + the two caps (present sets / absent leaves unchanged; update_step clamps to >=0).
    _tristate_bool(
        p,
        "--concept-glossary",
        "concept_glossary_render",
        "render matched glossary concepts (audience-gated, ordered, capped) into this "
        "llm_agent step's prompt instead of the plain term list.",
    )
    p.add_argument(
        "--glossary-concept-cap",
        dest="glossary_concept_cap",
        type=int,
        default=None,
        help="at most this many concepts in the rendered glossary block (0 = no cap).",
    )
    p.add_argument(
        "--glossary-token-cap",
        dest="glossary_token_cap",
        type=int,
        default=None,
        help="token ceiling for the rendered glossary block (0 = inherit the install cap).",
    )
    # SPEC-workflows-redesign 4b: the DELIBERATE pin/unpin act. --pin freezes this VARIANT
    # step against base-edit propagation; --unpin rejoins it (from the NEXT base edit; no
    # retro-sync). FLAGS on the EXISTING edit-step command (no new command path -> no new
    # verify_cli_parity entry). Usable ALONE (a pure pin/unpin, run-safe) or alongside field
    # edits. A direct field edit of a variant AUTO-pins it; an explicit --pin/--unpin always
    # wins over that heuristic. Mutually exclusive with each other.
    pin_grp = p.add_mutually_exclusive_group()
    pin_grp.add_argument(
        "--pin",
        dest="pin",
        action="store_true",
        default=False,
        help="pin this VARIANT step (freeze it against base-edit propagation).",
    )
    pin_grp.add_argument(
        "--unpin",
        dest="unpin",
        action="store_true",
        default=False,
        help="unpin this VARIANT step (rejoin base-edit propagation; no retro-sync).",
    )
    p.set_defaults(func=_wf_edit_step)

    p = sub.add_parser("delete-step", help="Delete a step from a workflow.")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id (for context).")
    p.add_argument("step_id", type=bounded_id, help="step id to delete.")
    p.set_defaults(func=_wf_delete_step)

    # --- move-step: the reorder seam's headless half (SPEC-workflow-canvas 4.2) ---
    p = sub.add_parser("move-step", help="Move a step to a new position in the workflow (reorder).")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id (for context).")
    p.add_argument("step_id", type=bounded_id, help="step id to move.")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--to",
        dest="to_position",
        type=int,
        default=None,
        metavar="N",
        help="0-based position the step should END UP at (0 = first). "
        "NOTE: moving a translate step to position 0 makes it "
        "TM-first-eligible (a 100%% TM match skips the model); "
        "moving it away removes that.",
    )
    where.add_argument(
        "--before",
        dest="before_ref",
        default=None,
        metavar="KEY|ID",
        help="place the step immediately BEFORE this step (key or id).",
    )
    where.add_argument(
        "--after",
        dest="after_ref",
        default=None,
        metavar="KEY|ID",
        help="place the step immediately AFTER this step (key or id).",
    )
    # Lane membership composes into the SAME call, so the headless surface can do
    # everything the drag can: join a group, leave one, or switch groups -- move + slug
    # validated together, one commit. Two flags rather than the '' convention because
    # argparse cannot tell `--parallel-group ''` apart from "not passed".
    mv_grp = p.add_mutually_exclusive_group()
    mv_grp.add_argument(
        "--parallel-group",
        dest="parallel_group",
        default=None,
        metavar="SLUG",
        help="also set this step's parallel group as part of the move "
        "(join or switch groups atomically; lqa steps only).",
    )
    mv_grp.add_argument(
        "--clear-parallel-group",
        dest="clear_parallel_group",
        action="store_true",
        help="also LEAVE the parallel group as part of the move (the "
        "headless equivalent of dragging a member out of a group; "
        "leaving without moving out of the members' run is refused, "
        "so this is the only way that succeeds).",
    )
    p.set_defaults(func=_wf_move_step)

    p = sub.add_parser("clone", help="Deep-copy a workflow and its steps.")
    p.add_argument("workflow_id", type=bounded_id, help="source workflow id.")
    p.set_defaults(func=_wf_clone)
