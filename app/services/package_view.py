"""Per-step PACKAGE / VERIFY view (SPEC-workflows-redesign Track B, B0).

The SINGLE SOURCE OF TRUTH for the two questions the whole of Track B exists to make
legible, per workflow step:

  * PACKAGE — what is assembled and sent to the model for this step (engine/model +
    fallback, glossary, TM few-shot, context, style guide, PII redaction, adaptive
    feedback, doc pre-pass); and
  * VERIFY — what is checked on the RETURN for this step (refusal guard, DNT survival,
    glossary adherence, QE gate, LQA structural/rulebook/judge/style adherence) plus the
    routing facts that act on the outcome (run_condition guard, branch, max_visits).

The builder "package panel" (workflow_builder.html), the CLI `workflow show` block +
`--json`, and the `/api/workflows/{id}` show endpoint ALL render from this module, so the
three surfaces can never drift. Values are ASCII-clean so the CLI block is Windows-console
safe and the web panel renders them verbatim.

HONESTY CONTRACT (generalising B2's per-document style-guide precedent — never fake
certainty, never a wall of "no"):
  * A fact resolved PER DOCUMENT at run time (context, an approved style guide, an
    unpinned glossary, DNT/adherence gated on the document's matched terms) carries
    ``scope="document"``.
  * A fact resolved PER LANGUAGE PAIR at run time (the LQA rulebook's FORBID/REQUIRE
    lines) carries ``scope="pair"``.
  * A fact certain at DEFINITION time (the pinned model, a pinned glossary's name, the
    PII toggle, the QE threshold, the branch graph) carries ``scope=""``.
  * Anything OFF/absent is simply OMITTED — the panel shows what IS, not a checklist of
    what isn't.

A "fact" is a plain dict ``{"key","label","value","scope"}``:
  key    stable slug — a JSON value, a test hook, a CSS/─anchor hook (never shown raw).
  label  the human column label ("Model", "Glossary", "Refusal guard", ...).
  value  ASCII-clean human text (the CLI prints it; the web panel renders it verbatim).
  scope  "" | "document" | "pair" — drives the honest per-* tag on every surface.

Robustness: every field read goes through ``_g`` so a step may be a ``WorkflowStep`` ORM
row (builder / CLI / api) OR a plain dict (tests / a future draft surface); a missing
field reads as its default and its fact simply doesn't show. Pure + never raises.

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

from app.models import StepType

# scope tags (honesty contract)
DEFINITION = ""  # certain at definition time
PER_DOCUMENT = "document"  # resolved per document at run time
PER_PAIR = "pair"  # resolved per language pair at run time

# Human phrase for a scope tag, appended by each surface as the honest qualifier.
SCOPE_NOTE = {PER_DOCUMENT: "per document", PER_PAIR: "per language pair"}

# User-facing role verb by StepType (Track-B vocabulary — never "agent").
_ROLE = {
    StepType.LLM_AGENT: "Translate",
    StepType.LQA: "Check",
    StepType.HUMAN_REVIEW: "Review",
    StepType.TM_MATCH: "Match TM",
    StepType.EXTERNAL: "Send to vendor",
}


def _g(step, name, default=None):
    """Read one field from a WorkflowStep ORM row OR a plain dict (defensive)."""
    if isinstance(step, dict):
        return step.get(name, default)
    return getattr(step, name, default)


def _fact(key, label, value, scope=DEFINITION):
    return {"key": key, "label": label, "value": value, "scope": scope}


def role_label(step_type) -> str:
    """The user-facing role verb for a step type (Translate / Check / Review)."""
    return _ROLE.get(step_type, str(step_type or "Step"))


def _model_value(provider, model, fallback_provider=None, fallback_model=None) -> str:
    """ASCII engine line: "<provider> / <model>" (provider blank -> "default"), with an
    optional "; fallback: <fp> / <fm>" tail when a failover engine is pinned."""
    prov = provider or "default"
    line = f"{prov} / {model}" if model else prov
    if fallback_provider:
        fb = f"{fallback_provider} / {fallback_model}" if fallback_model else fallback_provider
        line += f"; fallback: {fb}"
    return line


def _branch_value(branch) -> str:
    """Compact ASCII branch summary: "pass -> end; fail -> fix". '' when not a dict."""
    if not isinstance(branch, dict):
        return ""
    parts = []
    for slot in ("on_pass", "on_fail"):
        if slot in branch:
            t = branch[slot]
            tgt = t["step"] if isinstance(t, dict) and "step" in t else str(t)
            parts.append(f"{slot[3:]} -> {tgt}")
    return "; ".join(parts)


def _routing_facts(step) -> list:
    """VERIFY-column routing facts common to EVERY step type — the guard that decides
    whether the step runs, the post-step branch graph, and the loop ceiling. Each shows
    only when SET (a plain linear step contributes none)."""
    out = []
    rc = _g(step, "run_condition")
    if isinstance(rc, dict) and rc:
        ref, op, val = rc.get("ref"), rc.get("op"), rc.get("value")
        v = f"runs only when {ref} {op}"
        if val is not None:
            v += f" {val}"
        out.append(_fact("run_condition", "Run condition", v))
    br = _g(step, "branch")
    if isinstance(br, dict) and br:
        out.append(_fact("branch", "Branch", _branch_value(br)))
    mv = _g(step, "max_visits")
    if mv:
        # #94: the ceiling binds this step's OWN executions in every role (loop-back
        # target AND branch source), so the operator-facing wording says "executions".
        out.append(_fact("max_visits", "Max visits", f"{mv} executions (loop ceiling)"))
    return out


def _llm_agent_view(
    step, *, has_guide, has_context, glossary_name, prompt_fact=None, has_check=True
) -> dict:
    """PACKAGE/VERIFY for a Translate (llm_agent) step."""
    pkg = [
        _fact(
            "model",
            "Model",
            _model_value(
                _g(step, "provider"),
                _g(step, "model"),
                _g(step, "fallback_provider"),
                _g(step, "fallback_model"),
            ),
        )
    ]
    # SPEC-prompt-template-runtime: the RESOLVED prompt source ("inline prompt" /
    # "template: <name>" / "default"), single-sourced from the SAME pure resolver the
    # orchestrator runs, so the panel never over/under-claims which prompt actually runs.
    # Sits right after Model; omitted when the caller did not resolve it (a direct
    # step_facts caller without a session — the three real surfaces always pass it).
    if prompt_fact is not None:
        pkg.append(prompt_fact)
    gid = _g(step, "glossary_id")
    if gid:
        name = glossary_name or f"#{gid}"
        pkg.append(_fact("glossary", "Glossary", f"pinned: {name}"))
    else:
        # The glossary auto-attach + TM few-shot facts below describe an always-on step
        # MECHANISM ("if the project/content type has terms, they attach"; "inject the
        # top-N TM matches when any exist") whose per-document scope tag already carries
        # the "when available" honesty — they never assert a specific resource resolves.
        # Context and the style guide are different: each asserts a specific CONFIGURED
        # resource ("Context — resolved") that on a bare install does NOT exist, so both
        # are gated on scope-level AVAILABILITY (has_context / has_guide) — QA-8 (#38):
        # the Context fact used to render UNCONDITIONALLY, over-claiming a resolvable
        # context on every brand-new install. Now it mirrors the style-guide gate.
        pkg.append(
            _fact("glossary", "Glossary", "auto-attach (project + content type)", PER_DOCUMENT)
        )
    tm_window = _g(step, "tm_examples_window", 3)
    if _g(step, "tm_examples", True) and (tm_window or 0) > 0:
        pkg.append(
            _fact("tm", "TM few-shot", f"top {tm_window} fuzzy matches per segment", PER_DOCUMENT)
        )
    if has_context:
        pkg.append(_fact("context", "Context", "resolved", PER_DOCUMENT))
    if has_guide:
        pkg.append(_fact("style_guide", "Style guide", "resolved", PER_DOCUMENT))
    if _g(step, "redact_pii"):
        pkg.append(_fact("pii", "PII redaction", "scrubbed before the engine call, restored after"))
    if _g(step, "adaptive_feedback"):
        w = _g(step, "adaptive_feedback_window", 5)
        pkg.append(
            _fact(
                "adaptive_feedback",
                "Adaptive feedback",
                f"earlier confirmed pairs feed later prompts (n={w})",
            )
        )
    if _g(step, "doc_context_prepass"):
        pkg.append(
            _fact("doc_prepass", "Doc pre-pass", "a doc-level brief is prepended to every segment")
        )

    vf = [_fact("refusal", "Refusal guard", "a hedged/refused output is quarantined for review")]
    vf.append(
        _fact(
            "output_guard",
            "Output guard",
            "(segmented documents) an echoed step instruction or a short-source blow-up is "
            "quarantined for review; a dropped or added number is flagged for review",
        )
    )
    # #153: DNT + glossary adherence are only ever JUDGED when the run contains an LQA
    # step. `tap_enrich._adherence` is explicit about it -- "No LQA step in the run -> every
    # matched term is `not_checked` (honest: nothing judged it)" -- so on a workflow with no
    # check these two lines promised the operator a protection that could not fire. That is
    # worse than saying nothing: the panel exists to tell them what is guarding their
    # output, and the failure is SILENT (a required term is missed, the run is green).
    # `has_check` is a workflow-SCOPE flag computed once by the caller, exactly like
    # `has_guide` / `has_context` above.
    if has_check:
        vf.append(
            _fact(
                "dnt",
                "DNT terms",
                "matched do-not-translate terms must survive verbatim",
                PER_DOCUMENT,
            )
        )
        vf.append(
            _fact(
                "glossary_adherence",
                "Glossary adherence",
                "required target terms must appear",
                PER_DOCUMENT,
            )
        )
    else:
        vf.append(
            _fact(
                "no_term_check",
                "Terminology not verified",
                "this workflow has no quality check, so nothing verifies DNT terms or "
                "required glossary renderings",
            )
        )
    if _g(step, "qe_gate"):
        from app import config
        from app.services import qe_autofix

        thr = _g(step, "qe_threshold", 70)
        judge = _g(step, "qe_provider") or _g(step, "provider") or config.DEFAULT_PROVIDER
        vf.append(
            _fact(
                "qe_gate",
                "QE gate",
                f"judge {judge}; route below {thr}/100 to review; "
                + qe_autofix.display_status(step),
            )
        )
    vf += _routing_facts(step)
    return {"package": pkg, "verify": vf}


def _lqa_view(step, *, has_guide) -> dict:
    """PACKAGE/VERIFY for a Check (lqa) step — the judge engine + the checks it runs.

    CROSS-BRANCH DEPENDENCY (SPEC-lqa-step-honest-options, the maintainer's 2026-07-22
    decision): the judge line shows provider + model again (restored to its
    original, pre-honest-options shape) because feat/prompt-package-engine
    (merges BEFORE this branch) makes the LQA judge honor step.model via
    _spec_for_step. This fact is only honest once that branch has actually
    landed -- if it hasn't, re-verify against lqa.py/orchestrator.py before
    trusting this line (a prior version of THIS branch found model silently
    ignored here and hid it for exactly that reason). fallback_provider/
    fallback_model are omitted here as they always were (this fact never showed
    a fallback tail even for llm_agent's engine call the same way -- see
    _model_value's optional fallback args, unused by this call site)."""
    pkg = [_fact("model", "Judge model", _model_value(_g(step, "provider"), _g(step, "model")))]
    if _g(step, "redact_pii"):
        pkg.append(_fact("pii", "PII redaction", "source & target scrubbed before scoring"))

    vf = [
        _fact("structural", "Structural checks", "tag / number / placeholder integrity"),
        _fact("rulebook", "Rulebook", "FORBID / REQUIRE lines", PER_PAIR),
        _fact("judge", "LLM judge", "MQM fluency / accuracy / terminology findings"),
    ]
    # chip task_8face0e6: state the configurable pass bar wherever step facts render
    # (web builder / package / CLI `workflow show` / `GET /api/workflows/{id}` / MCP).
    # An unset threshold reads the built-in default so the fact is never blank.
    from app.services.lqa import LQA_PASS_THRESHOLD

    _thr = _g(step, "lqa_threshold")
    _eff = _thr if _thr is not None else LQA_PASS_THRESHOLD
    vf.append(
        _fact(
            "lqa_gate",
            "LQA gate",
            "pass at score >= %d (no major/critical errors)%s"  # noqa: UP031
            % (_eff, "" if _thr is not None else " [default]"),
        )
    )
    if has_guide:
        # B3: appended to the judge prompt when an approved guide resolves; a lone
        # NO_STYLE_GUIDE rulebook line suppresses it -> honestly per document.
        vf.append(
            _fact(
                "style_adherence",
                "Style adherence",
                "checked against the resolved style guide",
                PER_DOCUMENT,
            )
        )
    vf += _routing_facts(step)
    return {"package": pkg, "verify": vf}


def _human_review_view(step) -> dict:
    """PACKAGE/VERIFY for a Review (human_review) step — no model call; a person gate."""
    pkg = [_fact("review", "Review", "no model call - the run pauses for a person")]
    email = _g(step, "assignee_email")
    if email:
        pkg.append(_fact("assignee", "Assignee", f"{email} (emailed on pause)"))
    vf = [_fact("human", "Human review", "a person reviews before the run continues")]
    vf += _routing_facts(step)
    return {"package": pkg, "verify": vf}


def _tm_match_view(step) -> dict:
    """PACKAGE/VERIFY for a Match-TM (tm_match) step (PROPOSAL-tm-matching §7). HONEST:
    this step makes NO model call and assembles NO prompt -- it is orchestration only.
    So the panel says exactly that, instead of claiming a model / glossary / TM few-shot /
    refusal guard the way the llm_agent view would if this fell through to it (item 10)."""

    # Batch 15 T1 (SPEC-tm-match-step-config §8.F): render the step's EFFECTIVE config
    # (an override's concrete value, or "inherit" when NULL) instead of the old
    # hardcoded "global" facts. A NULL override reads "inherit"; a concrete value is shown.
    def _eff(name):
        v = _g(step, name)
        return "inherit" if v is None else v

    _fill = _eff("tm_fill_threshold")
    _fuzzy = _eff("tm_fuzzy_lock_mode")
    _pools = _g(step, "tm_pool_selection")
    # M4: NULL = all readable (inherit); a NON-NULL empty list = "nothing selected" (matches
    # nothing at run time, distinct from NULL, LAW-TM-9); a populated list lists the ids.
    if _pools is None:
        _pools_txt = "all readable"
    elif not _pools:
        _pools_txt = "nothing selected"
    else:
        _pools_txt = ", ".join(str(p) for p in _pools)
    _fill_txt = f"{_fill}%" if _fill != "inherit" else "the inherited threshold"
    _ice = _eff("tm_lock_ice")
    _hundreds = _eff("tm_hundreds_enter_lqa")
    _floor = _eff("tm_context_floor")
    pkg = [
        _fact(
            "tm_match",
            "TM matching",
            f"no model call - fills TM matches at or above {_fill_txt} and locks them",
        ),
        _fact(
            "tm_policy",
            "Lock policy",
            f"fill threshold {_fill}, fuzzy lock mode {_fuzzy}, lock_ice {_ice}, "
            f"hundreds_enter_lqa {_hundreds}, context_floor {_floor} "
            "(each 'inherit' = follow the global)",
            PER_DOCUMENT,
        ),
        _fact("tm_pools", "TM pools", f"reads: {_pools_txt}"),
    ]
    vf = [
        _fact(
            "tm_lock",
            "TM locks",
            "matched segments are locked; later steps skip them (hard locks are "
            "refused at the write seam)",
        )
    ]
    vf += _routing_facts(step)
    return {"package": pkg, "verify": vf}


def _external_view(step) -> dict:
    """PACKAGE/VERIFY for an EXTERNAL (Send-to-vendor) step (M5). HONEST: this step makes
    NO model call and assembles NO prompt -- it PACKAGES the current translation state and
    DISPATCHES it to a vendor, then the run parks awaiting the vendor's return (Wave A is
    dispatch-only; the guarded return lands in Wave B). It must NEVER claim a model /
    glossary / TM few-shot / refusal guard the way the llm_agent view would."""
    vname = None
    vid = _g(step, "vendor_id")
    if vid:
        try:
            from sqlmodel import Session as _S

            from app.db import engine as _eng
            from app.models import Vendor

            with _S(_eng) as _s:
                v = _s.get(Vendor, vid)
                vname = v.name if v is not None else None
        except Exception:
            vname = None
    target = f"vendor {vname}" if vname else (f"vendor #{vid}" if vid else "a vendor")
    pkg = [
        _fact(
            "external",
            "External dispatch",
            f"no model call - packages the current text and dispatches it to {target}",
        )
    ]
    email = _g(step, "assignee_email")
    if email:
        pkg.append(
            _fact("assignee", "Assignee", f"{email} (emailed on dispatch / expiry escalation)")
        )
    vf = [
        _fact(
            "external_wait",
            "Awaits return",
            "the run pauses until the vendor returns the reviewed content "
            "(guarded re-ingest lands in a later wave)",
        )
    ]
    vf += _routing_facts(step)
    return {"package": pkg, "verify": vf}


def _prompt_source_fact(step, template_row):
    """The RESOLVED prompt-source fact for an llm_agent step (SPEC-prompt-template-runtime),
    or None for a non-llm_agent step (lqa/human_review never resolve a bound template).
    `template_row` is (body, name) for the step's bound PromptTemplate, or None when it is
    unbound / uses inline text / the row is missing. Uses the SAME pure resolver as the
    orchestrator so the fact matches what actually runs; `default_text` is unused for the
    label so a harmless "" is passed."""
    from app.services import prompts

    if _g(step, "step_type") != StepType.LLM_AGENT:
        return None
    tid = _g(step, "prompt_template_id")
    exists = template_row is not None
    body = template_row[0] if template_row else None
    name = template_row[1] if template_row else None
    r = prompts.resolve_effective_prompt(
        StepType.LLM_AGENT,
        _g(step, "prompt_text"),
        default_text="",
        template_id=tid,
        template_exists=exists,
        template_body=body,
        template_name=name,
    )
    if r.source == prompts.PROMPT_SOURCE_INLINE:
        value = "inline prompt"
    elif r.source == prompts.PROMPT_SOURCE_TEMPLATE:
        value = f"template: {r.template_name}"
    else:
        value = "default"
    return _fact("prompt", "Prompt", value)


def _bound_templates(session, steps) -> dict:
    """{template_id: (body, name)} for every llm_agent step that BINDS a PromptTemplate,
    in one batch query (mirrors the glossary-name batch). Empty when no step binds one."""
    ids = {
        _g(s, "prompt_template_id")
        for s in steps
        if _g(s, "step_type") == StepType.LLM_AGENT and _g(s, "prompt_template_id")
    }
    ids.discard(None)
    if not ids:
        return {}
    from sqlmodel import select

    from app.models import PromptTemplate

    return {
        t.id: (t.body, t.name)
        for t in session.exec(select(PromptTemplate).where(PromptTemplate.id.in_(ids))).all()
    }


def step_facts(
    step,
    *,
    has_guide=False,
    has_context=False,
    glossary_name=None,
    prompt_fact=None,
    has_check=True,
) -> dict:
    """The {"package": [...], "verify": [...]} fact lists for ONE step.

    `has_guide` — whether an APPROVED style guide is available in the workflow's scope
    (computed once per workflow by the caller; gates the per-document style-guide
    PACKAGE fact on translate steps and the style-adherence VERIFY fact on check steps).
    `has_context` — whether a ContextProfile is resolvable in the workflow's scope (QA-8
    #38; gates the per-document Context PACKAGE fact on translate steps the same way
    has_guide gates the style-guide fact — an honest scope-level availability check, NOT a
    promise a specific context resolves).
    `glossary_name` — the resolved name of a PINNED glossary_id (the caller batch-
    resolves; falls back to "#<id>").
    `has_check` (#153) — whether the WORKFLOW contains an lqa step. The DNT and
    glossary-adherence VERIFY facts are only true when one does, because that is the
    condition `tap_enrich._adherence` itself keys on. Defaults True so a caller that has
    not been taught the flag keeps today's output rather than silently dropping facts."""
    st = _g(step, "step_type")
    if st == StepType.LQA:
        return _lqa_view(step, has_guide=has_guide)
    if st == StepType.HUMAN_REVIEW:
        return _human_review_view(step)
    if st == StepType.TM_MATCH:
        return _tm_match_view(step)
    if st == StepType.EXTERNAL:
        return _external_view(step)
    return _llm_agent_view(
        step,
        has_guide=has_guide,
        has_context=has_context,
        glossary_name=glossary_name,
        prompt_fact=prompt_fact,
        has_check=has_check,
    )


# --- workflow-scope helpers --------------------------------------------------
def _owning_customer(session, wf):
    """The workflow's OWNING customer id (its own, else its group's), or None for a
    GLOBAL/ungrouped workflow — the scope the style-guide availability check runs in.
    The single home for this rule now (the web builder's B2 note calls workflow_has_guide
    here rather than duplicating it), so the note + panel can never disagree."""
    cid = getattr(wf, "customer_id", None)
    if cid is not None:
        return cid
    gid = getattr(wf, "group_id", None)
    if gid:
        from app.models import WorkflowGroup

        grp = session.get(WorkflowGroup, gid)
        return getattr(grp, "customer_id", None) if grp is not None else None
    return None


def workflow_has_guide(session, wf) -> bool:
    """True when an APPROVED style guide is resolvable in this workflow's (org, owning
    customer) scope. Scope-level availability only — resolution is per document at run
    time (content type + pair), so this NEVER claims a specific guide resolves."""
    from app.services import style_guides as sg

    return sg.has_resolvable_guide(
        session, org_id=getattr(wf, "org_id", None), customer_id=_owning_customer(session, wf)
    )


def workflow_has_context(session, wf) -> bool:
    """True when at least one ContextProfile is resolvable in this workflow's (org, owning
    customer) scope — QA-8 (#38). The exact analogue of workflow_has_guide: a cheap
    scope-level AVAILABILITY probe, one indexed LIMIT-1 existence query (no per-step /
    per-document resolution storm on the workflows list), that gates the per-document
    Context PACKAGE fact. A profile counts when it is org-visible AND either the owning
    customer's own card or an org-global one (customer_id NULL — the org default,
    content-type, and project cards all sit under NULL customer). The effective context is
    still merged per document at run time (context_pkg.resolve's layered chain), so this
    NEVER claims a specific context resolves — it only answers "could any context resolve
    for a document under this workflow?", exactly as the style-guide gate does."""
    from sqlmodel import select

    from app.models import ContextProfile

    org_id = getattr(wf, "org_id", None)
    customer_id = _owning_customer(session, wf)
    stmt = select(ContextProfile.id)
    if org_id is not None:
        stmt = stmt.where(ContextProfile.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(
            (ContextProfile.customer_id == customer_id) | (ContextProfile.customer_id.is_(None))
        )
    # A staff/global workflow (no owning customer): any profile in the org counts.
    return session.exec(stmt.limit(1)).first() is not None


def _glossary_names(session, steps) -> dict:
    """{glossary_id: name} for every step's PINNED glossary, in one batch query (mirrors
    the CLI/api read path). Empty when no step pins a glossary."""
    gids = {_g(s, "glossary_id") for s in steps if _g(s, "glossary_id") is not None}
    if not gids:
        return {}
    from sqlmodel import select

    from app.models import Glossary

    return {g.id: g.name for g in session.exec(select(Glossary).where(Glossary.id.in_(gids))).all()}


def workflow_package_view(
    session, wf, steps, *, has_guide=None, has_context=None, glossary_names=None
) -> list:
    """One ``{index, id, name, type, role, package, verify}`` object per step — the whole
    workflow's package view, single-sourced for the builder panel, the CLI block +
    `--json`, and the `/api` show endpoint.

    `has_guide` / `has_context` / `glossary_names` may be passed by a caller that already
    computed them (the web route, the CLI/api read path) to avoid a duplicate query; each
    is computed here once per workflow when omitted (QA-8 #38: `has_context` gates the
    per-document Context fact exactly as `has_guide` gates the style-guide fact, and the
    CLI/api callers get it for free by not passing it — one cheap LIMIT-1 query, no N+1)."""
    steps = list(steps or [])
    if has_guide is None:
        has_guide = workflow_has_guide(session, wf)
    if has_context is None:
        has_context = workflow_has_context(session, wf)
    if glossary_names is None:
        glossary_names = _glossary_names(session, steps)
    # SPEC-prompt-template-runtime: batch-resolve bound PromptTemplate bodies+names once
    # so each llm_agent step's RESOLVED prompt-source fact ("inline prompt" / "template:
    # <name>" / "default") is single-sourced + N+1-free across the builder/CLI/api.
    bound_templates = _bound_templates(session, steps)
    # #153: one workflow-scope question, asked once -- does ANY step in this workflow judge
    # the text? Without one, the DNT / glossary-adherence verifications cannot run at all.
    has_check = any(_g(x, "step_type") == StepType.LQA for x in steps)
    out = []
    for i, s in enumerate(steps):
        gid = _g(s, "glossary_id")
        tid = _g(s, "prompt_template_id")
        facts = step_facts(
            s,
            has_guide=has_guide,
            has_context=has_context,
            glossary_name=glossary_names.get(gid) if gid else None,
            prompt_fact=_prompt_source_fact(s, bound_templates.get(tid) if tid else None),
            has_check=has_check,
        )
        st = _g(s, "step_type")
        out.append(
            {
                "index": _g(s, "order_index", i),
                "id": _g(s, "id"),
                "name": _g(s, "name") or "",
                "type": st,
                "role": role_label(st),
                "package": facts["package"],
                "verify": facts["verify"],
            }
        )
    return out
