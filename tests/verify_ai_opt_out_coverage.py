"""AI opt-out COVERAGE -- every engine call site in app/ is classified (HECVAT DPAI-08).

The runtime choke point (ai_optout.guard inside every Provider.complete, Embedder.embed,
run_completion, the vision/OCR seams, the Jev post and local_complete) only refuses when
a project scope is set. A call site with no scope is protected only by its own entry
refusal. This suite makes that a STATIC, reviewable fact instead of a hope:

  1. It walks every module under app/ with ast and finds every ENGINE CALL SITE: a call
     to (or a reference to, e.g. handed to a thread pool) run_completion,
     local_complete, embed_one, ocr_image, _guarded_vision_call, score_segment
     (qe / Jev), the Jev _post, any `.complete(` / `.embed(` method call, and the raw SDK
     calls (`messages.create`, `completions.create`, `embeddings.create`,
     `generate_content`).
  2. Each site must be classified in CLASSIFIED below as one of:
       guarded       -- the function (or an enclosing function) itself calls
                        ai_optout (refuse*/scope/guard/is_opted_out/...). VERIFIED.
       under:<entry> -- reached only through the named entry function(s), each of which
                        calls ai_optout. Every named entry is VERIFIED to contain the call.
       seam          -- the engine boundary itself (it runs guard(), directly or through
                        the Provider/Embedder base-class wrap). SDK and HTTP calls INSIDE a
                        Provider.complete / Embedder.embed subclass are covered by the
                        base-class wrap whatever their shape (verify_ai_opt_out proves it
                        with a Provider subclass defined after the app loaded).
       project_less  -- handles no project content (Evaluations Lab on synthetic data,
                        setup/prompt authoring from a description, engine connection
                        tests, the card-grounded Assistant, research CLI over pair files).
                        The reason is written next to the entry.
  3. A NEW call site, or a classified site that disappeared, fails the suite until it is
     classified (or the stale entry removed). A "guarded"/"under" claim whose ai_optout
     call is removed fails too (mutation-proven below).

KNOWN LIMITS (reviewed, no instance in app/ today): dynamic indirection such as
getattr(module, "name") or functools.partial is not resolved; a method's callers
(self.x / obj.x) are not resolved, so each method site needs a METHOD_SITES entry; a
route whose URL names a project/document is accepted only when listed in SCOPED_ROUTES.

ASCII-only output. Reads source only; imports nothing from app.
"""

import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

#: Names whose CALL or REFERENCE is an engine call site.
REF_NAMES = {
    "run_completion",
    "complete_direct",
    "local_complete",
    "embed_one",
    "ocr_image",
    "_guarded_vision_call",
    "score_segment",
}
#: SDK `.create(` only counts under these receivers (messages / completions / embeddings).
SDK_CREATE_PARENTS = {"messages", "completions", "embeddings", "responses"}
#: The Jev HTTP seam (a module-private _post); other modules' _post are connector HTTP.
JEV_POST_FILES = {"app/services/qe_jev.py"}
#: ai_optout functions that count as "this function protects itself".
AIO_CALLS = {
    "refuse",
    "refuse_for_document",
    "refuse_for_eval_dataset",
    "refuse_workflow",
    "refuse_workflow_for_project",
    "scope",
    "scoped_stream",
    "guard",
    "is_opted_out",
    "opted_out_project_ids",
}

G = ("guarded", "")
SEAM = ("seam", "")


def U(*entries):
    return ("under", entries)


def PL(reason):
    return ("project_less", reason)


# Key: "path::qualname::callee". Every entry is reviewed; keep the reason honest.
CLASSIFIED = {
    # --- the engine boundary itself ------------------------------------------------
    "app/services/providers.py::run_completion::complete": SEAM,  # guard() at its top
    "app/services/spend.py::complete_direct::complete": SEAM,  # Provider.complete base wrap
    "app/services/providers_hub.py::OpenAIProvider.complete::create": SEAM,
    "app/services/providers_hub.py::ClaudeProvider.complete::stream": SEAM,  # wrapped complete
    "app/services/providers_hub.py::GeminiProvider.complete::generate_content_stream": SEAM,
    # #437: the local wire call lives in _complete, a helper ONLY called by the
    # ai_optout._guarded-wrapped complete (it closes the client in its finally).
    "app/services/providers_hub.py::LocalProvider._complete::create": SEAM,
    "app/services/providers_part3.py::local_complete::create": SEAM,  # guard() at its top
    "app/services/ocr.py::ocr_image::_guarded_vision_call": SEAM,  # guard() inside
    "app/services/ocr.py::ocr_image._call::create": SEAM,  # run only by _guarded_vision_call
    "app/services/ocr.py::ocr_image._call::generate_content": SEAM,
    "app/services/image_localization.py::_guarded_vision_call::create": SEAM,  # guard() first
    "app/services/image_localization.py::_guarded_vision_call::generate_content": SEAM,
    "app/services/qe_jev.py::score_segment::_post": SEAM,  # _post runs guard() first
    "app/services/qe_jev.py::score_segment::score_segment": SEAM,  # recursion / retry
    "app/web/documents.py::segment_alternates::run_completion": G,  # refuse()
    "app/services/context_ab.py::run_ab::score_segment": G,  # refuse_for_document
    "app/services/style_ab.py::run_ab::score_segment": G,  # refuse_for_document
    "app/services/orchestrator.py::_run_workflow_stream_impl::run_completion": G,
    (
        "app/services/orchestrator.py::_run_workflow_stream_segmented_impl._score_seg"
        "::score_segment"
    ): G,
    "app/services/translate_hub2.py::_pretranslate_stream_impl::run_completion": U(
        "app/services/translate.py::pretranslate_stream"
    ),
    # #683: the per-item call moved into the nested _run_item (a chunked segment goes
    # piece by piece through mt_chunking.complete_pieces with the same run_completion);
    # it runs only inside _pretranslate_stream_impl, so the same protected entry covers it.
    "app/services/translate_hub2.py::_pretranslate_stream_impl._run_item::run_completion": U(
        "app/services/translate.py::pretranslate_stream"
    ),
    # --- reached only through a protected entry ----------------------------------
    "app/filters/pdf_filter.py::PdfFilter.parse::ocr_image": U(
        "app/services/ingest.py::ingest_document", "app/services/ingest.py::reingest_document"
    ),
    "app/services/image_localization.py::context_brief_for_image::_guarded_vision_call": U(
        "app/services/image_localization.py::ingest_image"
    ),
    "app/services/image_localization.py::escalate_recognition::_guarded_vision_call": U(
        # image_filter.parse -> escalate_recognition, run by ingest inside the scope
        "app/services/ingest.py::ingest_document",
        "app/services/ingest.py::reingest_document",
    ),
    "app/services/adaptive.py::build_document_brief::run_completion": U(
        "app/services/translate.py::pretranslate_stream",
        "app/services/orchestrator.py::_run_workflow_stream_finalized",
    ),
    "app/services/context_ab.py::_translate_once::run_completion": U(
        "app/services/context_ab.py::run_ab", "app/services/style_ab.py::run_ab"
    ),
    "app/services/eval_metrics_judge_qe_qe_score_segmen.py::_judge_qe::score_segment": PL(
        "shared metric library: judges are dispatched by name only from the paid Evaluations "
        "add-on's runner, which refuses an opted-out dataset first"
    ),
    "app/services/eval_metrics_judge_qe_qe_score_segmen.py::_judge_qe_jev::score_segment": PL(
        "shared metric library: judges are dispatched by name only from the paid Evaluations "
        "add-on's runner, which refuses an opted-out dataset first"
    ),
    "app/services/glossary_hub.py::_choose_sense::complete_direct": U(
        # multi-sense glossary resolution: the ONE caller skips it for an opted-out project
        "app/services/glossary_hub.py::_emit_for_entry",
    ),
    "app/services/glossary_hub.py::build_from_text::complete_direct": U(
        "app/services/term_extraction.py::extract",
        "app/web/documents_document_level_context_o.py::glossary_suggest",
    ),
    "app/services/lqa.py::_llm_errors::run_completion": U(
        "app/services/lqa.py::score_document",
        "app/services/orchestrator.py::_run_workflow_stream_finalized",
        "app/services/context_ab.py::run_ab",
    ),
    "app/services/qe.py::score_segment::run_completion": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized",
        "app/services/context_ab.py::run_ab",
        "app/services/style_ab.py::run_ab",
    ),
    "app/services/qe_autofix.py::run_loop.scorer::score_segment": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized"
    ),
    "app/services/qe_jev_loop.py::refine::run_completion": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized"  # via qe_autofix
    ),
    "app/services/qe_jev_loop.py::refine._default_scorer::score_segment": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized"
    ),
    "app/services/qe_jev_loop.py::refine.judge::score_segment": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized"
    ),
    "app/services/orchestrator_part8.py::_run_step_on_segment::run_completion": U(
        "app/services/orchestrator.py::_run_workflow_stream_finalized",
        "app/services/orchestrator.py::run_workflow_stream_segmented",
    ),
    "app/services/tag_llm_repair.py::repair::run_completion": U(
        # the on-demand sweep, and make_repairer's closure, which the MT paths hand to the
        # tag gate: orchestrator (run seam) and translate_hub2 (pretranslate refusal)
        "app/services/tag_llm_repair.py::repair_stored_segments",
        "app/services/orchestrator.py::_run_workflow_stream_impl",
        "app/services/orchestrator.py::_run_workflow_stream_segmented_impl",
        "app/services/translate.py::pretranslate_stream",
    ),
    "app/services/tag_llm_repair.py::_verify_once::run_completion": U(
        "app/services/tag_llm_repair.py::repair_stored_segments"
    ),
    "app/services/term_extraction.py::_propose_translations::complete_direct": U(
        "app/services/term_extraction.py::extract"
    ),
    "app/services/term_extraction.py::_prune::complete_direct": U(
        "app/services/term_extraction.py::extract"
    ),
    "app/services/translate_hub.py::_translate_full_text._one::run_completion": U(
        "app/services/translate.py::pretranslate_stream"
    ),
    # --- no project content ------------------------------------------------------
    "app/cli/cmd_lqa.py::_judge_scorer.scorer::score_segment": PL(
        "`lqa qe-compare` research CLI over a local pairs FILE, never project rows"
    ),
    "app/cli/cmd_lqa.py::_qe_compare::score_segment": PL(
        "`lqa qe-compare` research CLI over a local pairs FILE, never project rows"
    ),
    "app/services/assist_settings_keys_registered.py::_complete::run_completion": PL(
        "Ask: the capability CARDS plus the user's own question, no project content"
    ),
    "app/services/jev_inspector.py::_try_admitted::score_segment": PL(
        "Jev Inspector 'try it': a pair the admin types into the form"
    ),
    "app/services/orchestrator_part3.py::author_prompt::run_completion": PL(
        "prompt authoring from a plain-language description of a step"
    ),
    "app/services/quality_lens.py::judge_unit::run_completion": PL(
        "Quality Lens judge: its callers are paid add-ons that refuse an opted-out project first"
    ),
    "app/services/setup_author_part2.py::_author_json::run_completion": PL(
        "setup authoring (project/workflow JSON) from a plain-language description"
    ),
    "app/services/translate_hub.py::translate_text_once::run_completion": PL(
        "POST /api/translate/text: ad-hoc text in the request body, bound to no project; a "
        "key bound to an opted-out customer is refused (409) before any context card loads"
    ),
    "app/services/workflows_part3.py::adapt_instruction::run_completion": PL(
        "workflow variant authoring: rewrites a step INSTRUCTION, never content"
    ),
    "app/web/admin.py::setup_ai_local_test::local_complete": PL(
        "Setup AI 'Test connection' with a fixed probe string"
    ),
    "app/web/admin.py::setup_ai_test::run_completion": PL(
        "Setup AI 'Test engine' with a fixed probe string"
    ),
}


def _callee(node):
    """The engine name a Call (or bare reference) names, or None."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in REF_NAMES:
            return f.id
        if isinstance(f, ast.Attribute):
            if f.attr in REF_NAMES:
                return f.attr
            if f.attr in ("complete", "embed", "generate_content", "generate_content_stream"):
                return f.attr
            if f.attr in ("create", "stream"):
                parent = f.value
                pname = parent.attr if isinstance(parent, ast.Attribute) else None
                return f.attr if pname in SDK_CREATE_PARENTS else None
    return None


def _is_aio_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr in AIO_CALLS
        and isinstance(f.value, ast.Name)
        and f.value.id in ("ai_optout", "_ai_optout")
    )


_SCOPED_ROUTE_RE = re.compile(r"/(projects|documents|images|runs|segments)/\{")


def _middleware_scoped_route(fn) -> bool:
    """A route whose URL carries a project/document/image/run/segment id: the pure-ASGI
    AIScopeMiddleware scopes every request to it, so guard() sees the project."""
    for dec in getattr(fn, "decorator_list", []):
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr in ("get", "post", "put", "patch", "delete")
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
            and _SCOPED_ROUTE_RE.search(dec.args[0].value)
        ):
            return True
    return False


def scan_source(rel: str, src: str, refs=None):
    """-> (sites: {key: lineno}, protected_funcs: set[qualname]). When `refs` (a dict) is
    given it is filled name -> {"rel::outermost-function-qual"} for every call or bare
    reference, which is the caller index the one-level-and-up walk uses."""
    tree = ast.parse(src)
    sites: dict = {}
    protected: set = set()

    def visit(node, stack, fn_chain):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nstack = [*stack, ch.name]
                q = ".".join(nstack)
                nchain = [*fn_chain, q] if not isinstance(ch, ast.ClassDef) else fn_chain
                if _middleware_scoped_route(ch):
                    protected.add("ROUTE:" + q)
                visit(ch, nstack, nchain)
                continue
            if refs is not None and fn_chain and isinstance(ch, ast.Call):
                owner = f"{rel}::{fn_chain[0]}"
                for node_ in (ch.func, *ch.args, *(k.value for k in ch.keywords)):
                    if isinstance(node_, ast.Name):
                        refs.append((rel, owner, None, node_.id))
                    elif isinstance(node_, ast.Attribute) and isinstance(node_.value, ast.Name):
                        refs.append((rel, owner, node_.value.id, node_.attr))
            if _is_aio_call(ch):
                for q in fn_chain:
                    protected.add(q)
            name = _callee(ch)
            if name == "_post" and rel not in JEV_POST_FILES:
                name = None
            if name is None and isinstance(ch, ast.Call):
                # a reference handed on (e.g. ai_optout.submit(pool, run_completion, ...))
                for a in ch.args:
                    if isinstance(a, ast.Name) and a.id in REF_NAMES:
                        name = a.id
                    elif isinstance(a, ast.Attribute) and a.attr in REF_NAMES:
                        # Scoped calls also pass module-qualified judges as callables.
                        name = a.attr
            if (
                isinstance(ch, ast.Call)
                and isinstance(ch.func, ast.Name)
                and ch.func.id == "_post"
                and rel in JEV_POST_FILES
            ):
                name = "_post"
            if name is not None:
                q = ".".join(stack) or "<module>"
                sites.setdefault(f"{rel}::{q}::{name}", getattr(ch, "lineno", 0))
            visit(ch, stack, fn_chain)

    visit(tree, [], [])
    # a qualname is protected when it OR an enclosing function carries the call
    return sites, protected, fn_owner_map(tree)


def fn_owner_map(tree) -> dict:
    """qualname -> the OUTERMOST function qual that contains it (a method keeps its class
    prefix, a nested function maps to its enclosing function)."""
    out = {}

    def visit(node, stack, outer):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nstack = [*stack, ch.name]
                q = ".".join(nstack)
                is_fn = not isinstance(ch, ast.ClassDef)
                o = outer or (q if is_fn else None)
                if o:
                    out[q] = o
                visit(ch, nstack, o)
            else:
                visit(ch, stack, outer)

    visit(tree, [], None)
    return out


def _module_of(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _imports(tree, rel):
    """(names: local -> (module, name), mods: alias -> module) for one module. app.kernel.X
    is the alias facade for app.services.X, so it is mapped back."""
    names, mods = {}, {}
    pkg = _module_of(rel).rsplit(".", 1)[0] if not rel.endswith("__init__.py") else _module_of(rel)

    def canon(m):
        if m and m.startswith("app.kernel."):
            return "app.services." + m[len("app.kernel.") :]
        return m

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            base = canon(base)
            for a in node.names:
                local = a.asname or a.name
                if base in ("app.services", "app.kernel", "app.web", "app.cli"):
                    mods[local] = canon(f"{base}.{a.name}")
                else:
                    names[local] = (base, a.name)
                    mods.setdefault(local, canon(f"{base}.{a.name}"))
        elif isinstance(node, ast.Import):
            for a in node.names:
                mods[a.asname or a.name.split(".")[0]] = canon(a.name)
    return names, mods


def scan_tree(overrides=None):
    overrides = overrides or {}
    sites, protected, owners, raw_refs = {}, {}, {}, []
    imports = {}
    for dp, _dn, files in os.walk(os.path.join(_ROOT, "app")):
        if "__pycache__" in dp:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            rel = os.path.relpath(path, _ROOT).replace("\\", "/")
            if rel == "app/services/ai_optout.py":
                continue
            src = overrides.get(rel)
            if src is None:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
            s, p, own = scan_source(rel, src, raw_refs)
            sites.update(s)
            protected[rel] = p
            owners[rel] = own
            imports[rel] = _imports(ast.parse(src), rel)
    return sites, protected, owners, _resolve_refs(raw_refs, imports, owners)


def _resolve_refs(raw_refs, imports, owners) -> dict:
    """-> {"rel::func": {caller "rel::outer-func", ...}} for MODULE-LEVEL functions,
    resolved through each caller's imports (plain, aliased, relative, the app.kernel
    facade, and one hop of re-export such as translate.py re-exporting translate_hub's
    functions). Method calls (self.x / obj.x) cannot be resolved statically and are not
    indexed; a method site's protection rests on its named entry (see _walk_ok)."""
    mod_to_rel = {_module_of(r): r for r in owners}
    top_funcs = {r: {q for q in own if "." not in q} for r, own in owners.items()}

    def origin(mod, name, hops=0):
        rel = mod_to_rel.get(mod)
        if rel is None:
            return None
        if name in top_funcs.get(rel, ()):
            return f"{rel}::{name}"
        if hops < 2:
            nm = imports.get(rel, ({}, {}))[0].get(name)
            if nm:
                return origin(nm[0], nm[1], hops + 1)
        return None

    out: dict = {}
    for rel, owner, recv, name in raw_refs:
        names, mods = imports.get(rel, ({}, {}))
        target = None
        if recv is None:
            if name in top_funcs.get(rel, ()):
                target = f"{rel}::{name}"
            elif name in names:
                target = origin(*names[name])
        elif recv in mods:
            target = origin(mods[recv], name)
        if target and target != owner:
            out.setdefault(target, set()).add(owner)
    return out


def _is_protected(protected, rel, qual) -> bool:
    quals = protected.get(rel, set())
    parts = qual.split(".")
    return any(".".join(parts[:i]) in quals for i in range(1, len(parts) + 1))


#: Callers the walk may treat as protected although they call no ai_optout function
#: themselves, each with its reason. Keep this tiny; every entry is a reviewed claim.
CALLER_OK = {
    # The run seam: every step runs inside ai_optout.scoped_stream (set by the finalizing
    # wrappers) and _run_workflow_stream_*_impl call refuse_workflow first.
    "app/services/orchestrator.py::_run_workflow_stream_impl": "refuse_workflow + run scope",
    "app/services/orchestrator.py::_run_workflow_stream_segmented_impl": "refuse_workflow",
    # Research CLI over a local pairs FILE (never project rows): `lqa qe-compare` and
    # `lqa jev-refine` reach qe.score_segment / qe_jev_loop.refine without a project.
    "app/services/eval_metrics.py::_register_judges": (
        "judge registry of the shared metric library; judges run only from the paid Evaluations "
        "add-on's runner, which refuses an opted-out dataset first"
    ),
    "app/cli/cmd_lqa.py::_qe_compare": "research CLI over a local pairs file",
    "app/cli/cmd_lqa.py::_jev_refine": "research CLI over a local pairs file",
    # Jev Inspector "try it" (web, /api and CLI): scores a pair the admin types in.
    "app/services/jev_inspector.py::try_it": "admin-typed pair, no project content",
}

_WALK_DEPTH = 8

#: Routes whose URL id is the ONLY project context for the engine call they reach (the
#: AIScopeMiddleware scopes the request to it). Each is reviewed: the handler works only
#: on the project/document its own URL names, never on another project's content.
SCOPED_ROUTES = {}

#: Method sites whose callers cannot be resolved statically; each named with the reason
#: its only callers are protected.
METHOD_SITES = {
    "app/filters/pdf_filter.py::PdfFilter.parse": (
        "filters are run by ingest.ingest_document / reingest_document inside the project scope"
    ),
    "app/filters/image_filter.py::ImageFilter._parse_raster": (
        "filters are run by ingest.ingest_document / reingest_document inside the project scope"
    ),
}


def _walk_ok(fnkey, entries, protected, refs, seen, depth, trail) -> bool:
    """True when `fnkey` is a named entry, protected, allowlisted, or EVERY in-tree caller
    of it is (recursively, bounded). Records the first unprotected root in `trail`."""
    rel, qual = fnkey.split("::")
    if fnkey in entries or fnkey in CALLER_OK or _is_protected(protected, rel, qual):
        return True
    if "ROUTE:" + qual in protected.get(rel, set()):
        # A route whose URL names a project/document/...: the middleware scopes it to THAT
        # id at runtime. Accepted only when reviewed (SCOPED_ROUTES), because a handler that
        # reads ANOTHER project's content from the body would not be covered by that scope.
        if fnkey in SCOPED_ROUTES:
            return True
        trail.append(f"{fnkey} (middleware-scoped route not reviewed in SCOPED_ROUTES)")
        return False
    if fnkey in seen:
        return True  # a cycle adds no new caller
    if depth >= _WALK_DEPTH:
        trail.append(f"{fnkey} (walk too deep)")
        return False
    seen = seen | {fnkey}
    if "." in qual:
        # a METHOD: its callers (self.x / obj.x) are not statically resolvable, so the
        # site's named entry is the whole claim for it (reviewed, listed in METHOD_SITES).
        if fnkey in METHOD_SITES:
            return True
        trail.append(f"{fnkey} (method with no reviewed entry)")
        return False
    callers = refs.get(fnkey, set()) - {fnkey}
    if not callers:
        trail.append(f"{fnkey} (unprotected root)")
        return False
    return all(_walk_ok(c, entries, protected, refs, seen, depth + 1, trail) for c in callers)


def problems(sites, protected, owners=None, refs=None) -> list[str]:
    out = []
    for key in sorted(sites):
        if key not in CLASSIFIED:
            out.append(f"UNCLASSIFIED engine call site {key} (line {sites[key]})")
            continue
        kind, arg = CLASSIFIED[key]
        rel, qual, _callee_name = key.split("::")
        if kind == "guarded" and not _is_protected(protected, rel, qual):
            out.append(f"{key} is classified 'guarded' but calls no ai_optout refusal/scope")
        elif kind == "under":
            for entry in arg:
                erel, equal = entry.split("::")
                if not _is_protected(protected, erel, equal):
                    out.append(f"{key}: entry {entry} calls no ai_optout refusal/scope")
            if refs is not None:
                # CALLER WALK: every in-tree caller of the site's function must reach a
                # named entry or a protected function; an unprotected root fails.
                own = (owners or {}).get(rel, {}).get(qual, qual)
                fnkey = f"{rel}::{own}"
                trail: list = []
                if not _walk_ok(fnkey, set(arg), protected, refs, frozenset(), 0, trail):
                    out.append(f"{key}: a caller path reaches no protection: {trail[:3]}")
        elif kind == "project_less" and len(arg.strip()) < 20:
            out.append(f"{key}: project_less needs a real reason")
    for key in sorted(set(CLASSIFIED) - set(sites)):
        out.append(f"STALE classification {key} (no such call site any more)")
    return out


def test_every_site_classified():
    sites, protected, owners, refs = scan_tree()
    probs = problems(sites, protected, owners, refs)
    assert not probs, "\n".join(probs)
    kinds = {}
    for k in sites:
        kinds[CLASSIFIED[k][0]] = kinds.get(CLASSIFIED[k][0], 0) + 1
    print(f"  {len(sites)} engine call sites classified: {kinds}")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_mutations_are_caught():
    # Passing a qualified judge through the PII scope is still an engine call site.
    scoped = "def added():\n    return pii_boundary.call_with_redaction(True, qe.score_segment)\n"
    sites, _, _ = scan_source("app/services/qa.py", scoped)
    assert "app/services/qa.py::added::score_segment" in sites
    # M2: remove the refusal from context_ab.run_ab ('under' entry of _translate_once).
    src = _read("app/services/context_ab.py")
    mut = src.replace("ai_optout.refuse_for_document(", "_nope(")
    assert mut != src
    probs = problems(*scan_tree({"app/services/context_ab.py": mut}))
    assert any("_translate_once" in p for p in probs), probs
    # M3: a brand-new unclassified engine call site fails.
    src = _read("app/services/qa.py")
    mut = src + "\n\ndef _new_ai_feature(spec, text):\n    return run_completion(spec, text)\n"
    probs = problems(*scan_tree({"app/services/qa.py": mut}))
    assert any("UNCLASSIFIED" in p and "_new_ai_feature" in p for p in probs), probs
    # M4: the ingest scope removed -> the PDF OCR site's entry is no longer protected.
    src = _read("app/services/ingest.py")
    mut = src.replace("with ai_optout.scope(", "with _nope(")
    probs = problems(*scan_tree({"app/services/ingest.py": mut}))
    assert any("pdf_filter" in p for p in probs), probs
    # M5 CALLER WALK: glossary sense choice is classified under _emit_for_entry; remove
    # the opt-out skip there and the walk reaches unprotected callers (e.g. CLI paths).
    src = _read("app/services/glossary_hub.py")
    mut = src.replace("ai_optout.is_opted_out(session, project_id)", "_nope()")
    assert mut != src
    probs = problems(*scan_tree({"app/services/glossary_hub.py": mut}))
    assert any("_choose_sense" in p for p in probs), probs
    # M6 CALLER WALK: a new unprotected direct caller of an "under" site's function fails
    # even though the named entry is still protected.
    src = _read("app/services/qa.py")
    mut = (
        src
        + "\n\nfrom app.services.tag_llm_repair import repair as _r\n\n\n"
        + "def _new_unscoped_repair(a, b):\n    return _r(a, b)\n"
    )
    probs = problems(*scan_tree({"app/services/qa.py": mut}))
    assert any("tag_llm_repair.py::repair::" in p for p in probs), probs
    # M7 (gate round 3, G2): a route under /api/projects/{pid}/... that reaches an "under"
    # site's inner function directly is NOT accepted just because its URL is scoped.
    src = _read("app/web/api_part2.py")
    mut = (
        src
        + "\n\nfrom app.services.context_ab import _translate_once as _t1\n\n\n"
        + '@router.post("/api/projects/{pid}/rawprobe")\n'
        + "def _raw_probe(pid: int):\n    return _t1(None, None, None)\n"
    )
    probs = problems(*scan_tree({"app/web/api_part2.py": mut}))
    assert any("_raw_probe" in p and "SCOPED_ROUTES" in p for p in probs), probs
    print("  mutations M1-M7 each caught (M5-M7: the caller walk)")


def main():
    test_every_site_classified()
    test_mutations_are_caught()
    print("verify_ai_opt_out_coverage OK")


if __name__ == "__main__":
    main()
