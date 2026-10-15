"""workflows: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app.kernel import (
    access,
    ids,
    orchestrator,
    resource_visibility,
    run_overlay,
    security,
)
from app.kernel import (
    customers as customer_service,
)
from app.kernel import workflows as wf_service
from app.kernel.providers import (
    DeadEngineError,
    ProviderCallError,
    ProviderTruncated,
)
from app.models import (
    Customer,
    WorkflowGroup,
)
from app.web.deps import (
    _error_page,
    _my_customer_id,
    _my_org_id,
    _org_block,
)

router = APIRouter()


def _notice_url(path: str, notice: str) -> str:
    """`path` with a short human notice attached as the `?notice=` query param the builder
    GET route reads into a one-shot banner (empty notice = `path` unchanged). Split out of
    `_redirect_with_notice` so a route that answers a fetch with JSON can hand the CLIENT
    the very same URL to navigate to — one definition of "where to go and what to say"."""
    if notice:
        from urllib.parse import quote

        return f"{path}?notice={quote(notice)}"
    return path


def _redirect_with_notice(path: str, notice: str):
    """303 back to `path`, carrying the notice as a query param (see `_notice_url`).
    Kept tiny so every POST that wants to say something after a redirect shares it."""
    return RedirectResponse(_notice_url(path, notice), status_code=303)


def _with_prompt_warning(notice: str, warning: str) -> str:
    """`notice` with the step-prompt language WARNING appended (GitHub #43 / adversarial
    review P6). One seam so the add-step and edit-step routes phrase it identically and
    compose it with whatever else they had to say. Byte-identical (`notice` returned
    unchanged) when nothing warned, which is every save of a well-formed prompt."""
    if not warning:
        return notice
    text = "Warning: " + warning
    return (notice + " " + text) if notice else text


def _shared_base_notice(report: dict) -> str:
    """Render an update_step 4b propagation report into a concise builder banner (''
    = nothing to surface). Lists affected variants by their language-pair label so the
    operator sees exactly what a base edit swept, protected, and skipped — the report
    buckets the spec requires (propagated / skipped-adapted / pinned / validation-
    skipped / dead-fields-cleared), plus the auto-pin note when a VARIANT step was
    edited directly, and (SPEC-lqa-step-honest-options) the fields a TYPE SWITCH on
    THIS step force-cleared on the step itself."""
    if not report:
        return ""

    def _labels(key):
        return ", ".join(e.get("label", "?") for e in report.get(key, []))

    parts = []
    if report.get("auto_pinned"):
        parts.append(
            "This step is now pinned to this variant — base edits will skip it until you unpin."
        )
    # SPEC-lqa-step-honest-options (adversarial-review finding): a type switch never
    # refuses a now-dead field (the builder hides it, so there is nothing to act on) --
    # it force-clears it instead, reported here so the user sees exactly what changed.
    tsc = report.get("type_switch_cleared") or []
    if tsc:
        labels = [wf_service._DEAD_FIELD_LABELS.get(f, f) for f in tsc]
        parts.append(f"Cleared {', '.join(labels)} (no effect on the new step type).")
    prop = _labels("propagated")
    if prop:
        parts.append(f"Propagated to {prop}.")
    sa = _labels("skipped_adapted")
    if sa:
        parts.append(f"Kept the adapted prompt (flagged for review) on {sa}.")
    pinned = _labels("pinned")
    if pinned:
        parts.append(f"Skipped pinned variant(s): {pinned}.")
    vskip = _labels("validation_skipped")
    if vskip:
        parts.append(f"Skipped (the edit would break the variant's graph): {vskip}.")
    # SPEC-parallel-routing (adversarial review, PROPAGATION-10). Two buckets that must
    # never be silent: a variant left UNCHANGED because it has a run in flight (its
    # config would otherwise change under a paused run), and a variant where the swept
    # group landed as a LONE member, so it runs its checks sequentially while the base
    # runs them in parallel.
    ras = _labels("run_active_skipped")
    if ras:
        parts.append(
            f"Skipped (a run is in flight there — graph changes were not "
            f"applied): {ras}. Re-apply after the run finishes."
        )
    pas = report.get("propagated_as_singleton") or []
    if pas:
        bits = "; ".join(f"{e.get('label', '?')} (group {e.get('group', '?')})" for e in pas)
        parts.append(
            "Heads up — on " + bits + " no adjacent step shares that group, so "
            "those checks run SEQUENTIALLY there while this workflow runs them "
            "in parallel."
        )
    dfc = report.get("dead_fields_cleared") or []
    if dfc:
        bits = []
        for e in dfc:
            flds = ", ".join(wf_service._DEAD_FIELD_LABELS.get(f, f) for f in e.get("fields", []))
            bits.append(f"{e.get('label', '?')} ({flds})")
        parts.append("Cleared now-dead field(s) on: " + "; ".join(bits) + ".")
    return " ".join(parts)


def _wf_customer_block(request, row_customer_id):
    """R2-3 (Finding D): the customer ring for a Workflow/WorkflowGroup row.

    Unlike a Project (always customer-owned) or a Customer row itself, a Workflow/
    WorkflowGroup's customer_id may be NULL meaning GLOBAL scope — a shared
    resource every customer-bound user must still be able to open (mirrors the
    `/workflows` list rule: `scope == GLOBAL | customer_id == my_cust`). So this is
    deliberately NOT `_customer_block` (which treats NULL as foreign): a bound
    user is blocked only when the row has a customer_id AND it isn't their own.
    404-not-403 (not enumerable). No-op for internal staff / auth off."""
    me = security.session_user(request)
    cid = access.current_customer_id(me)
    if access.customer_ring_blocks(cid, row_customer_id):
        return _error_page(request, 404, "Not found.")
    return None


def _wf_write_block(request, row_customer_id):
    """#468: the WRITE-side customer ring for a Workflow / WorkflowGroup / PromptTemplate /
    style guide / language set row (the read ring is _wf_customer_block). A customer-bound
    user may change only its own customer's rows; a GLOBAL row (customer_id NULL) or an
    org-level row (pass None) is read-only for it: 403 with the shared message (the row is
    readable, so this reveals nothing). No-op for internal staff / auth off."""
    me = security.session_user(request)
    if access.customer_write_blocks(access.current_customer_id(me), row_customer_id):
        return _error_page(request, 403, access.SHARED_ASSET_READ_ONLY)
    return None


def _wf_create_customer_block(request, session, customer_id):
    """chip task_56b8b2bf: refuse a customer_id the caller may not scope a NEW workflow
    to. The web POST /workflows route skipped this entirely (a pre-existing cross-tenant
    plant), while the /api route's _api_workflow_customer funnel enforced it. Mirrors
    that funnel with the web's own access seams:
      * a customer-BOUND caller may only name its OWN customer (ring) -> 404;
      * in MULTI-ORG mode a customer that EXISTS in another ORG -> 404;
    a nonexistent / non-digit id is left to the existing path (blank -> a global
    workflow); an in-scope-but-ARCHIVED customer is handled by _archived_customer_block.
    404-not-403, and checked BEFORE the archived-400 so a foreign customer's name is
    never leaked in an error (the H1 cross-org enumeration oracle the /api route avoids).
    No-op for internal staff / auth off (the ring + org predicates are inert there)."""
    cid = ids.coerce_id(customer_id)
    if cid is None:
        return None
    me = security.session_user(request)
    if access.customer_ring_blocks(access.current_customer_id(me), cid):
        return _error_page(request, 404, "Not found.")
    cust = session.get(Customer, cid)
    if cust is not None and not access.same_org(me, getattr(cust, "org_id", None)):
        return _error_page(request, 404, "Not found.")
    return None


def _archived_customer_block(request, session, customer_id, what):
    """T2-01: refuse a create/bind that names an ARCHIVED customer, mirroring
    content.project_create's ArchivedCustomerError guard. Archive retires a customer
    from every 'choose a customer' surface, so a new workflow / workflow-group can't be
    attached to one — enforced at the ROUTE seam (not just omitted from the picker) so a
    forged POST / stray id is refused too. Returns an error response, or None when the
    customer is active / absent. A missing / non-digit id is left to the existing path
    (the service coerces it); ONLY an existing-but-archived customer is rejected here."""
    # coerce_id never raises: a malformed id (Unicode digit / >2**63) resolves to None
    # and is left to the existing path (the service coerces it) exactly as a non-digit id
    # already was -- only an EXISTING-but-archived customer is blocked here.
    cid = ids.coerce_id(customer_id)
    if cid is None:
        return None
    cust = session.get(Customer, cid)
    if cust is not None and getattr(cust, "archived_at", None) is not None:
        return _error_page(
            request,
            400,
            f"Customer {cid} ({cust.name}) is archived - unarchive it first, or pick "
            f"an active customer, to {what}.",
        )
    return None


_WF_SORT_FIELDS = ("name", "scope", "runs", "runs30", "last", "customers", "spend")


# --- workflows dashboard (SPEC Workflow Studio Wave A) ------------------------
# Server-side sort: the sortable dashboard columns and how each maps to a row's
# stats. Kept small + explicit (an unknown/absent sort falls back to the default),
# so a forged ?sort= can only pick from this whitelist — never an arbitrary attr.
# A None value (never run / untracked spend / no last-run) always sorts LAST
# regardless of direction, so the "nothing here yet" rows sink to the bottom.
def _wf_sort_key(row, field):
    """(present, value) so None sinks last in BOTH directions (Python sorts the
    False/True flag first; we invert it below so present rows lead)."""
    st = row["stats"]
    if field == "name":
        return (True, (row["name"] or "").lower())
    if field == "scope":
        return (True, (row["scope"] or "").lower())
    if field == "runs":
        return (True, st["runs_total"])
    if field == "runs30":
        return (True, st["runs_30d"])
    if field == "customers":
        return (True, st["customers_served"])
    if field == "last":
        v = st["last_run_at"]
        return (v is not None, v or "")
    if field == "spend":
        v = st["spend_usd"]
        return (v is not None, v if v is not None else 0.0)
    return (True, (row["name"] or "").lower())


def _sort_rows(rows, sort, direction):
    """Sort the dashboard rows by a whitelisted field/direction, keeping a stable
    name tiebreak so equal rows read alphabetically. None-valued cells (never run /
    untracked spend / no last-run) ALWAYS sink to the bottom, in BOTH directions —
    the toggle only orders the rows that actually have a value."""
    field = sort if sort in _WF_SORT_FIELDS else "last"
    desc = direction != "asc"
    # Stable name tiebreak first: it survives the later value sort for equal values,
    # and orders the value-less (missing) tail alphabetically too.
    rows.sort(key=lambda r: (r["name"] or "").lower())
    present = [r for r in rows if _wf_sort_key(r, field)[0]]
    missing = [r for r in rows if not _wf_sort_key(r, field)[0]]
    present.sort(key=lambda r: _wf_sort_key(r, field)[1], reverse=desc)
    rows[:] = present + missing
    return field, ("desc" if desc else "asc")


# --- multilingual workflow groups (P5, item 4) ---
def _grp_or_404(request, session, group_id, write=False):
    """R2-3 (Finding D): mirrors _wf_or_404 — org-gate then the customer ring
    (_wf_customer_block; NULL customer_id = GLOBAL group, stays visible).
    `write=True` (#468) adds the write ring: a global group is read-only for a
    customer-bound user (403)."""
    grp = session.get(WorkflowGroup, group_id)
    if grp is None:
        return None, _error_page(request, 404, "Not found.")
    blocked = _org_block(request, getattr(grp, "org_id", None))
    if blocked:
        return None, blocked
    blocked = _wf_customer_block(request, getattr(grp, "customer_id", None))
    if blocked:
        return None, blocked
    if write:
        blocked = _wf_write_block(request, getattr(grp, "customer_id", None))
        if blocked:
            return None, blocked
    return grp, None


def _copy_pairs_notice(result: dict) -> str:
    """One-shot banner for a copy_to_pairs submit: what was created, and — issue #46(7) —
    which requested pairs were REFUSED because the group already covers them. A silent
    skip is the whole bug ("I added German and it looks like it overrode French"), so the
    skipped pairs are named explicitly and the reader is told nothing was overwritten."""
    made = ", ".join(c["pair"] for c in result.get("created") or [])
    skipped = ", ".join(s["pair"] for s in result.get("skipped") or [])
    parts = []
    if made:
        parts.append(f"Added language pair(s): {made}.")
    if skipped:
        parts.append(
            f"Skipped {skipped} -- this group already has a variant for "
            f"{'those pairs' if len(result['skipped']) > 1 else 'that pair'}. "
            f"Nothing was replaced."
        )
    if not made and not skipped:
        parts.append("Nothing to copy.")
    return " ".join(parts)


def _rail_selected_customer(request, customers, my_cust):
    """Resolve the aids-rail's active customer for /workflows/new (Wave B), mirroring
    the /glossaries customer-first rule so the "What we already know" panel confines to
    a customer the VIEWER can see:
      * a customer-BOUND caller is FORCED to its own customer (?customer_id= ignored);
      * else the ?customer_id= param when it names a VISIBLE customer;
      * else None -> the panel prompts the author to pick a customer.
    Never auto-selects on a single customer here (unlike the glossaries page): the
    create hero is customer-agnostic, so the panel stays quiet until asked. A foreign /
    archived / non-digit id resolves to None — it can never scope the panel."""
    customer_ids = {c.id for c in customers}
    if my_cust is not None:
        return my_cust
    n = ids.coerce_id(request.query_params.get("customer_id"))
    if n is not None and n in customer_ids:
        return n
    return None


def _wf_glossary_customer(request, session, wf):
    """The customer whose glossaries a workflow's step dropdown is confined to
    (2026-07-14, customer-first). A customer-BOUND caller is ALWAYS confined to
    its own (R2-3, unchanged). Otherwise the workflow's OWNING customer (its group's
    customer_id) so a staff author building Acme's workflow sees Acme's + legacy
    unassigned glossaries — options_for_step adds the unassigned tier. A GLOBAL/ungrouped
    workflow (no owning customer) stays None -> every approved glossary is offered, since
    a shared workflow legitimately runs on any customer's project (run-time enforcement
    is the guard there)."""
    bound = _my_customer_id(request)
    if bound is not None:
        return bound
    grp = session.get(WorkflowGroup, wf.group_id) if getattr(wf, "group_id", None) else None
    return getattr(grp, "customer_id", None) if grp is not None else None


def _visible_workflows(request, session, workflows):
    """Keep workflow lists inside the same org/customer/project rings as detail.
    The rule lives in app/services/resource_visibility.py (shared with global search, #621)."""
    return resource_visibility.visible_workflows(session, security.session_user(request), workflows)


def _visible_group_variants(request, session, group_id):
    """R1 (adversarial review, merge-blocking): a group's members the REQUESTER may
    actually see — the SAME predicate `_wf_or_404` enforces on a workflow reached by id
    (org, customer, and project rings; a shared unbound member stays visible).

    A group can legally hold members from other rings: `add_to_group` only refuses a
    CROSS-ORG attach, and a GLOBAL group is offered to every customer. So the raw
    `group_variants` list is NOT a safe copy source. Copying an invisible member would
    clone customer B's prompt text into a workflow the requester can never open — and
    dispatch (`_variant_match._compatible`) would filter that clone straight back out,
    so it would never run either: issue #46's "it did nothing" confusion, reintroduced
    with a tenancy leak attached. Every add-a-pair read AND write picks from here.

    Project visibility is also required, including for variants of a shared group.
    Coverage and copy-source selection receive this same filtered list."""
    return _visible_workflows(request, session, wf_service.group_variants(session, group_id))


def _group_pair_source(variants):
    """The variant an "add a language pair" copies FROM: the group's DEFAULT, else the
    lowest-sorting member (group_variants orders by pair then id, so it is stable — not
    "the first one added"). `variants` must already be the requester-visible list."""
    return next((v for v in variants if v.is_default), variants[0] if variants else None)


def _group_pair_targets_ctx(request, session, grp, source_locale):
    """R9 (adversarial review): the group page's add-a-pair picker gets the SAME
    active-pairs confinement the builder's copy picker has (the maintainer's standing law: a
    picker confines to the customer's active pairs, with a show-all escape). Customer
    resolution mirrors `_wf_copy_targets_ctx`: a BOUND caller's own customer, else the
    group's. No customer / no pairs -> the full enabled set, confined=False."""
    cid = _my_customer_id(request)
    if cid is None:
        cid = getattr(grp, "customer_id", None)
    customer = session.get(Customer, cid) if cid is not None else None
    rows, confined = customer_service.confined_target_rows(
        session, customer, source_locale=source_locale
    )
    return {
        "pair_target_rows": rows,
        "pair_targets_confined": confined,
        "pair_targets_customer": customer,
    }


def _visible_prompt_templates(request, session):
    """PromptTemplates offered in a step's prompt-template picker (SPEC-prompt-template-
    runtime): org-scoped + the caller's own customer visibility ring (GLOBAL + own
    customer; a foreign customer-scoped template filtered out) — the SAME
    list_prompt_templates seam the /templates page uses, so the picker and its POST
    re-validation can never drift."""
    return wf_service.list_prompt_templates(
        session, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
    )


def _shape_ctx(session, wf, steps) -> dict:
    """SPEC-workflows-redesign Phase 3: the builder's shape surface — chips (with a
    fan-out chip when the workflow is a multi-variant group member), a one-line headline,
    and the origin label. Shared by both builder render sites (GET + no-JS author POST).

    Also carries the Track B, B2 STYLE-GUIDE availability note and (B0) the per-step
    PACKAGE/VERIFY views for the builder panel. Style-guide resolution is per-DOCUMENT at
    run time (it needs the doc's content type + language pair, unknown at build time), so
    the builder does NOT claim a specific guide resolves — it only flags, honestly, that
    an APPROVED guide is AVAILABLE in the workflow's customer scope and WILL be resolved
    per document. `style_guide_note` is "" (renders nothing) when none is available — the
    honest muted signal, never a fake certainty chip on a step. `has_guide` is computed
    ONCE and threaded to both the note and the package view (single query, no drift)."""
    from app.kernel import package_view as pv
    from app.kernel import workflow_shapes as wshapes

    result = wshapes.detect_shapes(steps)
    fanout = (
        bool(getattr(wf, "group_id", None)) and len(wf_service.group_variants(session, wf.id)) > 1
    )
    has_guide = pv.workflow_has_guide(session, wf)
    note = (
        "A style guide is available for this workflow's scope and will be "
        "resolved per document at run time."
        if has_guide
        else ""
    )
    package_views = pv.workflow_package_view(session, wf, steps, has_guide=has_guide)
    # Prompt-tap builder link (only meaningful when capturing is on): the count of
    # captures correlated to THIS workflow, linking to the filtered viewer.
    from app.kernel import prompt_tap

    tap_enabled = prompt_tap.enabled()
    tap_count = prompt_tap.count_records(workflow_id=wf.id) if tap_enabled else 0
    return {
        "shape_chips": wshapes.chip_descriptors(result, fanout=fanout),
        "shape_headline": wshapes.shape_headline(steps) if steps else "",
        "origin_label": wshapes.origin_label(getattr(wf, "created_via", None)),
        "style_guide_note": note,
        "package_views": package_views,
        "prompt_tap_enabled": tap_enabled,
        "prompt_tap_count": tap_count,
        "scope_note": pv.SCOPE_NOTE,
    }


# Shared author-prompt core for BOTH the JSON (fetch, no-refresh) route and the
# full-page no-JS fallback route, so the engine:<id> / spend / truncation / hard-
# provider-error handling lives in ONE place and can't drift between the two
# surfaces (and mirrors the CLI's `workflow author-prompt`). Returns (draft, err):
# exactly one is non-None; err is (status_code, message) for a clean, user-facing
# failure. It is STATELESS — author_prompt persists nothing; the draft is returned
# for the client to drop into the (still-unsaved) Prompt box.
def _author_draft(description: str, provider: str, org_id=None, model=None):
    try:
        return orchestrator.author_prompt(
            description, provider or None, org_id=org_id, model=(model or None)
        ), None
    except DeadEngineError as exc:
        # The provider dropdown includes engine:<id> refs; a dead one (deleted/
        # disabled/keyless/undecryptable) surfaces as a clean error, not a raw 500.
        return None, (400, str(exc))
    except ProviderTruncated:
        # author_prompt() routes through run_completion, which ESCALATES the output
        # budget toward the model ceiling on truncation and re-raises this only when the
        # draft STILL overflows AT the ceiling -- so it genuinely exceeds what this model
        # can emit. Surface clean (a shorter description barely helps; the fix is a
        # larger-output model).
        return None, (
            400,
            "The model ran out of output space before finishing the prompt draft, even "
            "after automatically expanding the output budget to this model's ceiling. "
            "Pick a provider/model with a larger output token limit.",
        )
    except ProviderCallError as exc:
        # A hard provider failure surfaced by run_completion (bad key / unreachable
        # local server / malformed response / NMT engine with no prompt channel) --
        # a clean 502, never a traceback.
        return None, (502, str(exc))


# --- promptable workflow setup (SPEC-promptable-setup, Branch B) -------------
#
# Describe a whole workflow in plain language -> the LLM proposes name + customer +
# language pair(s) + ordered steps -> the server validates every reference against
# the caller's real catalogs -> an EDITABLE review page -> confirm re-validates the
# UNTRUSTED payload and creates atomically through the SAME builder seams. The LLM
# never creates anything; the review page is a plain form POST chain (no JS needed).
def _workflow_review_customers(request, session):
    """The active, visibility-scoped customers offered in the review page's customer
    select (mirrors workflows_index: org filter + a bound caller collapses to their
    own customer). Never leaks a customer name the caller could not see."""
    me = security.session_user(request)
    cq = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        cq = cq.where(Customer.id == my_cust)
    return session.exec(cq).all()


_STEP_TEXT_FIELDS = (
    "name",
    "step_type",
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "prompt_text",
    "assignee_email",
    "glossary_id",
    "qe_threshold",
    "qe_provider",
    "qe_autofix_provider",
    "qe_autofix_max_rounds",
    "lqa_threshold",
    "tm_examples_window",
    "key",
    "max_visits",
)


_STEP_BOOL_FIELDS = (
    "needs_review",
    "qe_gate",
    "qe_autofix",
    "qe_autofix_cross_check",
    "tm_examples",
)


_MAX_REVIEW_STEPS = 50


# The per-step review-form field names (indexed s{i}_<field>), parsed back into the
# UNTRUSTED payload apply_workflow_draft re-validates. Booleans are absent-when-off
# (an unchecked checkbox posts nothing), so their presence in the form == True.
# SPEC §5 Phase 3/4: the stable key + per-step loop ceiling. Both are
# plain scalars; apply_workflow_draft re-validates them (slug/uniqueness
# for key, positive-int for max_visits).
# Upper bound on how many steps a single review-form parse will materialize. A real
# workflow has a handful of steps; this only exists so a FORGED step_count cannot
# drive an unbounded loop. apply_workflow_draft re-validates the parsed list anyway.
def _parse_run_condition_fields(ref, op, value):
    """Assemble a run_condition dict from the review form's ref/op/value scalars, or
    None when no ref is chosen (= always run). A numeric value box is coerced to float;
    a non-numeric string is left as-is so apply_workflow_draft's validate_run_condition
    rejects it cleanly (never a 500). NOT trusted — re-validated at confirm."""
    ref = (ref or "").strip()
    if not ref:
        return None
    raw = (value or "").strip()
    try:
        val = float(raw) if raw else None
    except ValueError:
        val = raw
    return {"ref": ref, "op": (op or "").strip(), "value": val}


def _parse_branch_fields(on_pass, on_fail):
    """Assemble a post-step branch dict from the review form's on_pass/on_fail selects,
    or None when neither slot is set. Each value is 'next'/'end'/a step KEY, turned into
    the stored form via parse_branch_str ('next'/'end' -> the sentinel; a key ->
    {'step': slug}). NOT trusted — apply_workflow_draft's validate_branch re-validates it
    (the D3 gate rule + target resolution, fail-closed), never a 500."""
    from app.kernel import workflows as _wf

    branch = {}
    if (on_pass or "").strip():
        branch["on_pass"] = _wf.parse_branch_str(on_pass)
    if (on_fail or "").strip():
        branch["on_fail"] = _wf.parse_branch_str(on_fail)
    return branch or None


def _int_or_none(v):
    """Coerce a form scalar to an id int, or None when empty/blank/non-numeric/malformed.
    Used so the error re-render path feeds the template int-typed customer_id/glossary_id
    (the same types the validated happy-path draft carries), keeping the Jinja
    `== c.id` / `== g.id` option tests true and the selection <option selected>. Routes
    through the app-wide id law so a malformed value (Unicode digit / >2**63) is None,
    never a 500 on the error re-render."""
    return ids.coerce_id(v)


def _run_graph_ctx(session, run, steps, step_runs):
    """Shared context for the "How this run went" card (graph overlay + Gantt),
    used by BOTH the full run page AND the graph-fragment endpoint so the fragment
    renders byte-identically to the card in the page. Pure/read-only."""
    overlay = run_overlay.build_overlay(step_runs, steps, run_status=run.status) if steps else None
    # Real run wall time (WorkflowRun DOES persist started_at/finished_at, unlike the
    # per-StepRun timestamps): the honest footer total. None while still in flight.
    wall_ms = None
    if run.started_at and run.finished_at:
        wall_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
    gantt = (
        run_overlay.build_gantt(step_runs, steps, run_status=run.status, wall_ms=wall_ms)
        if steps
        else None
    )
    return {"steps": steps, "overlay": overlay, "gantt": gantt}


# --- Building blocks (SPEC Workflow Studio Wave C) ------------------------------
# The former top-level "Templates" page is FOLDED into the Workflows area as the
# "Building blocks" tab (/workflows/building-blocks): the caller's prompt-template
# library (full CRUD, unchanged routes/RBAC/scoping/guards), the built-in role presets
# rendered read-only, and the clonable global-workflow templates — now with honest
# cross-links (which visible workflows bind each template). The old /templates URL 302s
# here so no bookmark dies.
@router.get("/templates")
def templates_redirect(request: Request):
    """RETIRED top-level Templates page -> the Building blocks tab. A permanent move,
    but a 302 (not 301) so browsers/proxies never hard-cache the redirect while the
    IA is still settling. Preserves any ?err= flash the POST CRUD routes bounce back."""
    from urllib.parse import urlencode

    err = request.query_params.get("err")
    target = "/workflows/building-blocks"
    if err:
        target += "?" + urlencode({"err": err})
    return RedirectResponse(target, status_code=302)
