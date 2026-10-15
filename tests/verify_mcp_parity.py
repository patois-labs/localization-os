"""MCP accounting parity -- the guard that makes forgetting the MCP impossible.

docs/specs/mcp-server.md wave 0. This is the FOURTH mirror guard in this repo and the
reason the other three exist: the cards are pinned to the argparse tree by
verify_ask_draft, docs/CLI.md is pinned to the CLI leaves by verify_cli_parity,
the /help API reference and the LLM card are pinned to the real router by
verify_help. An MCP tool registry with no guard would be the only unpinned one,
and this repo has already watched an unguarded mirror rot in public (/help Part 2
documented 98 of the routes it served under a sentence claiming 93).

WHAT IT PINS. `integrations/mcp-server/registry.py` declares, per /api route,
either the tool that may call it or a categorised reason it is deliberately out
of reach. This suite asserts that declaration against the ROUTER ITSELF -- walked
off `app.routes` via tests/_routes.py (shared with verify_help so the two mirror
guards cannot disagree about what "the routes" are), not the OpenAPI schema, so a
route with `include_in_schema=False` is still held to being accounted for. The
load-bearing consequence: a new /api route that ships with no tool and no
exemption makes this suite RED.

ARITHMETIC IS NOT SAFETY -- the lesson of this guard's first adversarial review.
Twenty-two mutations were run against the first version. Every field it
cross-checked against a real source held. Every field the registry declared about
ITSELF was gameable, and the worst of them balanced perfectly: moving
POST /api/runs/{run_id}/resume out of EXEMPT and onto a READ-tier tool's routes
took covered 6 -> 7 and exempt 152 -> 151, the union still equalled the router,
every count agreed -- and a model had been handed the human-review sign-off.
Demoting the one spend tool to `read` made check 7 announce "all 0 spend tools
carry confirm". An exemption reading "" or "nah" passed. `Planned:` with a
capital P silently became a permanent exemption. So the self-declared fields are
now constrained by tables that say WHICH routes may be covered and BY WHAT KIND
of tool (registry.NEVER_COVER / SPEND_ROUTES / MUTATING_TIERS), by a validated
category vocabulary, and by ratchets that only move one way.

WHAT IT STILL DOES NOT PIN, said plainly so a green run is not over-read. It does
not execute a single tool and does not read RBAC; behaviour is verify_mcp.py's
job. It reads server.py's signatures via AST but not its bodies, so it cannot
prove a handler calls the routes it declares. And the scope re-derivation reads
the shapes the gates use TODAY (`"x" in apikeys.scope_set(...)` in either
polarity, `apikeys.has_scope(...)`); refactoring a gate into a differently-named
helper would shrink the derived vocabulary -- which fails loudly here rather than
silently, but the fix is then to teach this function the new shape.

verify_mcp.py needs the MCP SDK and skips cleanly without it -- correct for a
protocol test, fatal for an accounting guard. THIS suite must never skip, which
is why registry.py imports nothing and why check 1 proves it.

THE TEN CHECKS
  1  registry.py is import-free AND side-effect-free (sys.modules delta, an AST
     read of its imports, a ban on __import__/eval/exec/open/importlib anywhere
     in it, and a rule that every module-level call is a data constructor -- a
     bare `open(...)` at module scope executes when the guard loads the file)
  2  every tool route exists in the real router
  3  every tool route's METHOD matches (the likeliest defect: all tokens real,
     only the verb wrong)
  4  THE LOAD-BEARING ONE: covered U exempt == real, disjoint, nothing unaccounted
  5  every input_schema is a well-formed object schema (required subset of
     properties; every property typed AND described) and offers no FORBIDDEN
     property -- a flag that turns a legitimately covered route destructive
     (`cleanup` on the key import retires every key absent from the file)
  6  the destructive tier is EMPTY (v1 law, maintainer decision 2), no tool may claim a
     NEVER_COVER route, a non-GET route forces a mutating tier, and a SPEND_ROUTE
     may only sit on a spend-tier tool
  7  every spend-tier tool's schema carries a boolean `confirm` (decision 5's
     two-phase contract, enforced structurally rather than by prose)
  8  no stale exemptions, and every exemption carries a real CATEGORY plus a
     reason long enough to be a reason
  9  every `planned:` marker (matched case-insensitively) names a tool really in
     the spec's scope table, which must still list all 34
  10 field vocabularies are real (tier/family from the registry, scope re-derived
     from the app's own gates, feature_gate from settings.DEFAULTS); descriptions
     cannot lie (pins present verbatim, no summary promises a foreign route);
     and the wave-1 debt list is verified against server.py's real signatures, so
     it cannot be closed by deleting it

...plus a RATCHET block (planned may only shrink, covered may only grow, debt may
only shrink, pins may only grow), because every check above is equally satisfied
by a registry with fewer tools and more exemptions.

MUTATION-PROVEN. A guard that only caught invented routes would have passed on
the 98-of-158 page this pattern exists to have caught (the count at the time of
that rot; this build now serves more), so the MISSING mutation is
the load-bearing one. Every forgery below is asserted to go red, in-suite, on
every run. The A/B/C/D labels are the adversarial reviewer's, and each of those
was GREEN against the first version of this file:

  M1   a tool route that does not exist                        -> check 2
  M2   a real path under the wrong verb                        -> check 3
  M3   a real route removed from BOTH covered and EXEMPT       -> check 4
       (three variants, incl. a brand-new route: the real R2 scenario)
  M4   an exemption naming a route that does not exist         -> check 4
  M5   a `pins` string deleted from a summary                  -> check 10
  A1   an exemption justified by the empty string              -> check 8
  A2   an exemption justified by "nah", or by a made-up category-> check 8
  A3   `Planned:` with a capital P, or a phantom tool name     -> check 9
  A4   a locked perimeter exemption relabelled and then covered-> check 6
  A5   a read tool swallowing POST /api/runs/{id}/resume       -> check 6
  A6   routes growing to absorb apikeys + a TM wipe + a purge  -> check 6
  A6c  a SEMANTIC-destructive route (customer archive) dropped
       from NEVER_COVER -- the gap a suffix regex cannot see    -> check 6
  A6d  a read tool swallowing tm scrub-hedges                   -> check 6
  B1   module-level side effect (`open(...)` at import time)   -> check 1
  B2   `__import__` of app, then scrubbing sys.modules         -> check 1
  B3   a registry field computed by eval()                     -> check 1
  C3   a tool name dropped from the spec's scope table         -> check 9
  D1   two tools with the same name                            -> check 10
  D2   a tool with no routes                                   -> check 10
  D3   a route both covered and exempt                         -> check 4
  D4   `confirm` declared as a string, or removed              -> check 7
  D5   the spend tool demoted to `read`                        -> check 6
  D6   `planned:` with no tool name after it                   -> check 9
  D7   a tool's `pins` tuple emptied                           -> check 10
  D8   a blank summary                                         -> check 10
  D9   the wave-1 debt list emptied instead of the debt        -> check 10
       (wave 1 PAID the debt, so the drift is now FORGED -- one handler parameter
       renamed in a copy of server.py -- because a check whose subject went away
       must be re-proved against a synthetic subject, not quietly deleted)
  F2   the import_keys `cleanup` flag put back, or its
       merge-only promise unpinned                             -> checks 5, 10
  P1   a plain write whitelisted into plan-mode POSTs          -> check 6
  P2   the plan-safe whitelist outliving its spend tool        -> check 6
  P3   a REAL launch route (a SPEND_ROUTES route on a spend
       tool) forged into plan mode -- GREEN under the old
       SPEND_ROUTES-only whitelist, RED against PLAN_ESTIMATE   -> check 6
  P4   the PLAN_ESTIMATE perimeter widened to a non-spend route -> check 6

WAVE 1 (16 tools, 34 covered routes) also MOVED one constraint. `cleanup` used to
be pinned on the EXEMPT entry for the key-import route, which wave 1 now covers;
it lives on the TOOL instead, in _FORBIDDEN_SCHEMA_PROPS. That move is exactly
what check 8's "carries a pinned constraint but is no longer exempt" failure
exists to force -- a constraint must travel with its subject, not evaporate with
the exemption that used to carry it.

WAVE 2 (34 tools, 90 covered routes, planned = 0) changed two things a reader
of the proofs should know. (1) The A3b/D6/A3c forges used to pick a LIVE
planned marker off the map; the map is now empty, so - the D9 lesson applied a
second time - they forge the marker onto a permanent exemption's key instead of
quietly disappearing with their subject. (2) server.py gained ONE deliberate
crack in "planning never sends a non-GET": _PLAN_SAFE_REQUESTS, so
start_evaluation's plan can ask the API's own two-phase POST for its estimate.
The P proofs pin that whitelist to the spend perimeter (SPEND_ROUTES entries on
spend-tier tools, nothing else), read out of the AST like every other claim
this suite makes about server.py.
"""

import ast
import glob
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpparity_")

_REGISTRY_PATH = os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
_SERVER_PATH = os.path.join(_ROOT, "integrations", "mcp-server", "server.py")
_SPEC_PATH = os.path.join(_ROOT, "docs", "specs", "mcp-server.md")

# --- check 1 vocabularies ---------------------------------------------------
#: Roots registry.py must NEVER pull in. `app` would couple the accounting guard
#: to the application; `mcp` would make it skippable in an SDK-less environment.
_FORBIDDEN_IMPORT_ROOTS = {
    "app",
    "mcp",
    "httpx",
    "fastapi",
    "sqlmodel",
    "pydantic",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
}

#: The only modules registry.py may name in an import statement.
_ALLOWED_IMPORT_ROOTS = {"__future__", "dataclasses", "typing"}

#: Names registry.py may not reference ANYWHERE, at module scope or inside a
#: helper. An `import` statement is not the only way to reach the app: the review
#: got `__import__("importlib").import_module("app.services.settings")` past the
#: first version of check 1, read DEFAULTS, wrote the result to a file, and then
#: deleted its own entries from sys.modules so the delta came back clean.
_BANNED_NAMES = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "open",
    "globals",
    "locals",
    "vars",
    "importlib",
    "__builtins__",
    "input",
    "getattr",
    "setattr",
    "delattr",
}

#: The only callables allowed at MODULE scope (statements that run at load time).
#: Everything the registry legitimately does is construct data.
_ALLOWED_MODULE_CALLS = {"Tool", "dataclass", "frozenset", "tuple", "dict", "set"}

#: Statement types allowed at module scope. A bare `for`/`if`/`with`/expression
#: statement at module level is code that executes when the guard opens the file.
_ALLOWED_MODULE_STMTS = (
    ast.Import,
    ast.ImportFrom,
    ast.Assign,
    ast.AnnAssign,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)

# --- ratchets ---------------------------------------------------------------
# THESE MOVE ONE WAY ONLY. Planned exemptions convert into tools, so their count
# may shrink and never grow. Covered routes accumulate, so their count may grow
# and never shrink. Wave-1 debt is paid off, so it may shrink and never grow.
# Editing any of these numbers is the reviewable act -- it is a claim that the
# surface moved, and it belongs in a diff someone reads, not in a silent drift.
MAX_PLANNED = 0  # wave 2 converted the last 56; a NEW marker is new debt
# open-core S2 (2026-10-14): 224 -> 209, 15 paid-add-on routes left with their tools; was 224
# measured 2026-10-10: next/connectors (223, bitbucket) + gdocs/translate
MIN_COVERED = 209
# history: 150 = 136 through R2 + 7 R3 (manage_install) + 1 approve_segment
# (chip task_9c665111) + Vendor Wave E routes -> merged 148;
# + 1 akeneo/translate (connector factory generated tier #1) -> 149;
# + 1 P3 T1 GET /api/admin/providers/{name}/search-key (manage_install show_search_key) -> 150;
# + 1 sitecore/translate (Sitecore connector) -> 151;
# + 1 sanity/translate (NEW kind) -> 152
# + 1 plytix/translate (wave 3 PIM connector) -> measured 218
# + 1 msgraph/translate (Microsoft 365 Files) -> 219
# + 1 servicenow/translate (ServiceNow connector)
# + 1 gitlab/translate (git core provider)
# + 1 bitbucket/translate (Bitbucket connector) -> measured 223
MAX_DEBT = 0  # entries in registry.SERVER_PY_WAVE1_DEBT -- wave 1 PAID it
MIN_TOTAL_PINS = 97  # #122 glossary opt-in cursor contract +1 (previous floor 96)
# description pins across all tools (R3 manage_install +2,
# approve_segment +1, Vendor Wave E tools -> merged 96)
MIN_SPEC_TOOLS = 66  # tools in docs/specs/mcp-server.md's section-3 scope table. Wave 3 Train B
# +6 (the six Queries tools, documents family); Wave 3 Train C +4
# (the reviewer-LQA-finding tools, documents family); Wave 3 Train D
# +1 (my_work, documents family). Prior (R3 +1: manage_install;
# run-cancel train +1: cancel_workflow_run; chip task_9c665111 +1:
# approve_segment; Vendor Wave E +2 -> was 55, then 61 with Train B's
# Queries tools, 65 with Train C's findings, now 66 with Train D).
# Kept in lockstep with verify_badinput_folded's exact-match (66, 9).
MIN_REASON_CHARS = 40  # an exemption reason shorter than this is a placeholder

#: SPEC section 9 names the pins that must exist. Each needle must appear inside
#: at least one of that tool's `pins` -- not merely somewhere in the summary, so
#: emptying the pins tuple (which leaves the sentence rewritable) fails too.
_SPEC_REQUIRED_PINS = {
    "list_assets": ("Glossary cursor paging is opt-in",),
    "submit_translation": ("efuses", "mock"),
    "job_status": ("does not block",),
    # The Delta loop's three contract facts. Getting any of them wrong silently
    # rots every downstream file, which is why they are pinned rather than trusted
    # to prose (SPEC section 3, family D).
    "key_changes": ("next_cursor", "resync_required", "op=deleted"),
    # The other half of the cleanup constraint below: the schema may not OFFER the
    # flag, and the description must PROMISE the tool never retires a key.
    "import_keys": ("never retires keys",),
    # Wave 2 (SPEC section 3, families E-H). work_status: counts alone hide
    # quarantined segments, and a batch that half-succeeded must not read as
    # done. run_workflow: paused means a HUMAN, and the resume verb is
    # deliberately absent. edit_glossary: the two rung rules the tool cannot
    # enforce and must therefore state. start_evaluation: the estimate is
    # returned and STOPS; the acknowledgement is never auto-filled.
    "work_status": ("qa_flags", "partial is not success"),
    "run_workflow": ("never invent an approval",),
    "edit_glossary": ("draft glossary contributes nothing", "can_tune_ai"),
}

#: Constraints that must survive on a specific EXEMPT entry's prose, because the
#: wave that converts that route inherits them from there and nowhere else.
#:
#: WAVE 1 MOVED ONE OF THESE. The `cleanup` constraint used to live on the EXEMPT
#: entry for POST /api/projects/{pid}/keys/import; that route is now COVERED by
#: import_keys, so the constraint moved onto the tool itself
#: (_FORBIDDEN_SCHEMA_PROPS below) rather than evaporating with the exemption --
#: which is exactly what check 8's "carries a pinned constraint but is no longer
#: exempt" failure exists to force.
_EXEMPT_REASON_PINS = {
    ("POST", "/api/keys/{key_id}/restore"): ("import_keys",),
}

#: Properties a tool's input_schema may NEVER offer, per tool, with the reason.
#:
#: `cleanup=true` on the key import RETIRES every key absent from the uploaded
#: file and emits a tombstone per key to every downstream CI consumer -- a bulk
#: delete wearing an import's clothes. Exposing it would contradict the
#: DELETE /api/keys/{key_id} entry in NEVER_COVER and would falsify the
#: /api/keys/{key_id}/restore exemption's "a delete an agent cannot perform".
#: The route is legitimately covered; the FLAG is what may not be.
_FORBIDDEN_SCHEMA_PROPS = {
    "import_keys": {
        "cleanup": (
            "retires every key absent from the file -- a bulk delete, and "
            "v1 ships zero destructive tools (SPEC changelog, wave-0 "
            "review, constraint F2)"
        ),
    },
}


# =============================================================================
# check 1 -- the registry loads with nothing behind it, and does nothing when it
# =============================================================================
def import_free_problems(source: str, where: str = "registry.py") -> list:
    """Everything about `source` that makes it unsafe for the guard to load.

    A pure function of the TEXT on purpose: a forged registry can be red-proofed
    without ever executing it, which is the only honest way to test a check whose
    whole subject is "what happens when this file executes"."""
    problems = []
    try:
        tree = ast.parse(source, where)
    except SyntaxError as exc:
        return [f"{where}: does not parse ({exc})"]

    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            named.add("<relative>" if node.level else (node.module or "").split(".")[0])
    for root in sorted(named - _ALLOWED_IMPORT_ROOTS):
        problems.append(
            f"{where} imports {root!r}; the accounting guard must run with no MCP SDK and no "
            f"app import, so only {sorted(_ALLOWED_IMPORT_ROOTS)} are allowed"
        )

    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in _BANNED_NAMES:
            problems.append(
                "{} references {!r} (line {}) -- an import statement is not the only "
                "way to reach the app, and a registry that can call it is a "
                "registry that can lie to the guard that loads it".format(
                    where, name, getattr(node, "lineno", "?")
                )
            )

    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # the module docstring
    for stmt in body:
        if not isinstance(stmt, _ALLOWED_MODULE_STMTS):
            problems.append(
                "{} has a {} statement at module scope (line {}) -- module-level "
                "code EXECUTES when the guard opens the file; the registry is data".format(
                    where, type(stmt).__name__, getattr(stmt, "lineno", "?")
                )
            )
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # helper bodies run only when called
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = (
                func.id
                if isinstance(func, ast.Name)
                else getattr(func, "attr", type(func).__name__)
            )
            if fname not in _ALLOWED_MODULE_CALLS:
                problems.append(
                    "{} calls {}(...) at module scope (line {}) -- the only calls "
                    "that may run at load time are data constructors {}".format(
                        where, fname, getattr(node, "lineno", "?"), sorted(_ALLOWED_MODULE_CALLS)
                    )
                )
    return problems


def load_registry_import_free():
    """Load registry.py and prove it dragged nothing in and did nothing on the way.

    Three independent reads, because each alone has a hole. The AST read catches
    a static import and any reference to an escape hatch. The module-scope rules
    catch code that would run at load time. The sys.modules DELTA is the backstop
    for anything the first two missed -- and is itself defeatable (the reviewer
    scrubbed sys.modules after importing), which is exactly why it is the
    backstop and not the check.

    Runs FIRST, before `app` is imported anywhere in this process, so the delta
    is meaningful."""
    with open(_REGISTRY_PATH, encoding="utf-8") as fh:
        source = fh.read()
    problems = import_free_problems(source, os.path.basename(_REGISTRY_PATH))
    assert not problems, "the registry is not safe to load:\n  " + "\n  ".join(problems)

    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location("loc_mcp_registry", _REGISTRY_PATH)
    assert spec and spec.loader, f"could not load {_REGISTRY_PATH}"
    registry = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__], so the
    # module must be registered BEFORE exec_module -- it is the only entry the
    # delta below is expected to contain from this file itself.
    sys.modules[spec.name] = registry
    spec.loader.exec_module(registry)
    dragged = sorted(
        m
        for m in (set(sys.modules) - before - {spec.name})
        if m.split(".")[0] in _FORBIDDEN_IMPORT_ROOTS
    )
    assert not dragged, f"registry.py pulled in {dragged}"

    print(
        "  [1] registry.py is import-free and side-effect-free: stdlib imports "  # noqa: UP031
        "only, no reference to any of the %d banned escape hatches, every "
        "module-level call is a data constructor, sys.modules delta clean OK" % len(_BANNED_NAMES)
    )
    return registry


# =============================================================================
# checks 2 + 3 -- tool routes exist, method-for-method
# =============================================================================
def route_problems(real: set, covered: set) -> list:
    """Every tool route the app does not serve, as reader-facing sentences.

    The two failure modes are reported DIFFERENTLY on purpose. A path the router
    has never heard of is a rename or a typo; a real path under the wrong verb is
    the likelier defect (every token is real) and is the one verify_help proved
    worth its own assertion."""
    real_paths = {p for _m, p in real}
    problems = []
    for method, path in sorted(covered - real, key=lambda p: (p[1], p[0])):
        if path in real_paths:
            verbs = sorted(m for m, p in real if p == path)
            problems.append(
                "WRONG METHOD: a tool declares {} {}; the app serves that path as {}".format(
                    method, path, "/".join(verbs)
                )
            )
        else:
            problems.append(
                f"NO SUCH ROUTE: a tool declares {method} {path}, which this build does not serve"
            )
    return problems


# =============================================================================
# check 4 -- the accounting closes (and no exemption outlives its route)
# =============================================================================
def accounting_problems(real: set, covered: set, exempt: set) -> list:
    """Everything wrong with the three-way accounting, as sentences.

    The rule (SPEC section 5): every /api route is named by a tool, reachable
    through a documented composite tool, or in the exemption map with a reason.
    There is no fourth outcome."""
    problems = []
    for method, path in sorted(real - covered - exempt, key=lambda p: (p[1], p[0])):
        problems.append(
            f"UNACCOUNTED: {method} {path} is served but no tool claims it and nothing "
            "exempts it -- add a routes entry or one EXEMPT line with a reason"
        )
    for method, path in sorted(exempt - real, key=lambda p: (p[1], p[0])):
        problems.append(
            f"STALE EXEMPTION: {method} {path} is exempt but is no longer a route -- a stale "
            "exemption silently un-accounts whatever route later lands on that path"
        )
    for method, path in sorted(covered & exempt, key=lambda p: (p[1], p[0])):
        problems.append(f"COVERED AND EXEMPT: {method} {path} -- drop the exemption")
    if (covered | exempt) != real:
        problems.append(
            "COUNTS DO NOT CLOSE: %d covered + %d exempt (union %d) against %d "  # noqa: UP031
            "served routes" % (len(covered), len(exempt), len(covered | exempt), len(real))
        )
    return problems


# =============================================================================
# check 6 -- WHICH routes may be covered, and by WHAT KIND of tool
# =============================================================================
def coverage_policy_problems(
    tools, never_cover: frozenset, spend_routes: frozenset, mutating_tiers: frozenset
) -> list:
    """The constraints the accounting arithmetic cannot express.

    Balance proves nothing about safety: the review moved the human-review resume
    onto a read tool and every count still agreed. These rules are what a tool's
    own `tier` field is checked AGAINST, instead of being believed."""
    problems = []
    for tool in tools:
        if tool.tier == "destructive":
            problems.append(
                f"DESTRUCTIVE TIER POPULATED by {tool.name} -- v1 has zero destructive "
                "tools by design; adding one needs a spec change"
            )
        for route in tool.routes:
            if route in never_cover:
                problems.append(
                    f"NEVER_COVER VIOLATION: tool {tool.name} declares {route[0]} {route[1]}, a route no "  # noqa: E501
                    "tool may claim in any tier -- see registry.NEVER_COVER"
                )
            if route in spend_routes and tool.tier != "spend":
                problems.append(
                    f"SPEND ROUTE ON A {tool.tier!r} TOOL: {tool.name} declares {route[0]} {route[1]}, which bills the "  # noqa: E501
                    "operator; only tier='spend' may carry it (relabelling the "
                    "tier is how the two-phase confirm contract gets shed)"
                )
            if route[0] != "GET" and tool.tier not in mutating_tiers:
                problems.append(
                    f"MUTATING ROUTE ON A {tool.tier!r} TOOL: {tool.name} declares {route[0]} {route[1]}; a non-GET "  # noqa: E501
                    f"route forces tier in {sorted(mutating_tiers)}"
                )
    return problems


#: Routes that are destructive BY SEMANTICS, not by a path suffix the regex in
#: never_cover_completeness_problems can see. Archiving a customer withdraws ALL
#: its projects from every working surface at once; scrub-hedges rewrites/removes
#: matching TM entries in bulk. Their delete-family SIBLINGS are caught
#: mechanically off the router; these two verbs are not, so they are named here
#: and REQUIRED in NEVER_COVER the same way -- otherwise "a destructive route
#: cannot leave the set unnoticed" would hold only for the ones whose path ends in
#: a delete word. Kept honest by the staleness half of the check below: if either
#: route stops being served, the fixture must be updated in the same diff.
_SEMANTIC_DESTRUCTIVE_ROUTES = frozenset(
    {
        ("POST", "/api/customers/{cid}/archive"),
        ("POST", "/api/tm/scrub-hedges"),
        # Residual sweep (archive verbs): archiving a project/workflow withdraws it from every
        # working surface at once (listings, pickers, dispatch) and blocks new runs -- the same
        # semantic-destruction class as customer archive, and likewise invisible to the
        # delete-suffix regex. UNARCHIVE (the inverse) is not destructive and is accounted by a
        # plain EXEMPT line, mirroring customer unarchive.
        ("POST", "/api/projects/{pid}/archive"),
        ("POST", "/api/workflows/{wid}/archive"),
    }
)


#: Routes whose path ENDS in a delete verb but that are AUTHORING edits, not content
#: destruction -- so the mechanical delete-suffix rule below must not force them into
#: NEVER_COVER (SPEC-workflow-authoring-api, a reviewed carve-out).
#:
#: A workflow STEP delete is a graph-authoring edit: can_tune_ai (NOT the is_admin gate
#: every real delete uses), fail-closed re-validated, reversible by re-adding the step,
#: and it destroys no translation content and writes to no external tenant. It is
#: covered by edit_workflow (action=delete_step) exactly as add/edit/move are. This is
#: DELIBERATELY narrow -- one route, named in full -- and the WHOLE-OBJECT workflow /
#: group deletes (POST /api/workflows/{wid}/delete, /workflow-groups/{gid}/delete) stay
#: in NEVER_COVER, is_admin, uncovered, exactly as before. Kept honest by the staleness
#: half below: if the route stops being served, this fixture must be updated in the same
#: diff.
_AUTHORING_DELETE_ROUTES = frozenset(
    {
        ("POST", "/api/workflows/{wid}/steps/{sid}/delete"),
        # Batch 14 (D1): a key-image delete is a REVERSIBLE soft-delete (can_write, not the
        # is_admin gate a real delete uses; the image is re-attachable), covered by
        # manage_key_image (action=delete) exactly as attach/set_primary are -- the
        # workflow-step-delete precedent generalizes. It is an HTTP DELETE, so the
        # completeness rule's `method == "DELETE"` arm fires on it independent of any
        # /delete path suffix; without this carve-out that arm trips "NEVER_COVER IS
        # INCOMPLETE". The carve-out matches on (method, path) tuples, so a DELETE-method
        # entry is accepted, and the staleness half keeps it honest (the route is served).
        ("DELETE", "/api/keys/{key_id}/images/{image_id}"),
        # SPEC-terminology-backend T1 (§7.5/H16): a concept/term-store delete is a
        # content-authoring edit (can_tune_ai, reverts approval, no external tenant, the
        # concept/term is re-addable), covered by edit_glossary (actions delete_concept /
        # delete_concept_term) exactly as the workflow-step + key-image deletes are. The
        # WHOLE-glossary delete (POST /api/glossaries/{gid}/delete) stays in NEVER_COVER,
        # is_admin, uncovered. The staleness half keeps these honest (both routes served).
        ("POST", "/api/glossaries/{gid}/concepts/{cid}/delete"),
        ("POST", "/api/glossaries/{gid}/concepts/{cid}/terms/{tid}/delete"),
    }
)


#: Connector-family routes the Wave R4 amendment (SPEC-full-remote-parity.md, "Wave
#: R4 -- MCP connector tools", 2026-08-07) moved OUT of NEVER_COVER and ONTO
#: tools -- so the mechanical `/api/connectors/{cid}/` completeness rule below must
#: NOT force them back into NEVER_COVER. This is the connector analogue of
#: _AUTHORING_DELETE_ROUTES: a DELIBERATELY narrow, named carve-out, kept sound three
#: ways so it can never become a hole:
#:   * completeness carves out EXACTLY these routes; every OTHER `/api/connectors/
#:     {cid}/` route still MUST be in NEVER_COVER, so a new connector write route
#:     cannot slip out unnoticed (the whole point of the family re-derivation);
#:   * the staleness half below RED-flags any entry that stops being a served route,
#:     so it cannot silently pre-approve whatever route next takes that path;
#:   * main() additionally asserts this set is a SUBSET of covered() and DISJOINT
#:     from NEVER_COVER -- a carved-out route that is not actually toolified would be
#:     an unaccounted route (check 4) or a covered-and-never-cover contradiction, so
#:     the carve-out can only name routes a tool genuinely reaches.
#: These are the generic pull/push/sync/retry plus every per-platform
#: `{kind}/translate` route. The Airtable record+webhook family, jira count/
#: translate-pending, salesforce/rediscover-fields and the three resolve-links are
#: NOT here -- they stay in NEVER_COVER, so covering one still goes red.
_TOOLIFIED_CONNECTOR_ROUTES = frozenset(
    {
        ("POST", "/api/connectors/{cid}/pull"),
        ("POST", "/api/connectors/{cid}/push"),
        ("POST", "/api/connectors/{cid}/sync"),
        ("POST", "/api/connectors/{cid}/retry"),
        ("POST", "/api/connectors/{cid}/jira/translate"),
        ("POST", "/api/connectors/{cid}/confluence/translate"),
        ("POST", "/api/connectors/{cid}/document360/translate"),
        ("POST", "/api/connectors/{cid}/freshdesk/translate"),
        ("POST", "/api/connectors/{cid}/zoho/translate"),
        ("POST", "/api/connectors/{cid}/strapi/translate"),
        ("POST", "/api/connectors/{cid}/contentful/translate"),
        ("POST", "/api/connectors/{cid}/shopify/translate"),
        ("POST", "/api/connectors/{cid}/hubspot/translate"),
        ("POST", "/api/connectors/{cid}/intercom/translate"),
        ("POST", "/api/connectors/{cid}/salesforce/translate"),
        ("POST", "/api/connectors/{cid}/notion/translate"),
        ("POST", "/api/connectors/{cid}/helpscout/translate"),
        ("POST", "/api/connectors/{cid}/wordpress/translate"),
        ("POST", "/api/connectors/{cid}/akeneo/translate"),
        ("POST", "/api/connectors/{cid}/mcp_cms/translate"),
        ("POST", "/api/connectors/{cid}/gdocs/translate"),
        ("POST", "/api/connectors/{cid}/msgraph/translate"),
        ("POST", "/api/connectors/{cid}/drupal/translate"),
        ("POST", "/api/connectors/{cid}/github/translate"),
        ("POST", "/api/connectors/{cid}/sanity/translate"),
        ("POST", "/api/connectors/{cid}/servicenow/translate"),
        ("POST", "/api/connectors/{cid}/sitecore/translate"),
        ("POST", "/api/connectors/{cid}/zendesk/translate"),
        ("POST", "/api/connectors/{cid}/plytix/translate"),
        ("POST", "/api/connectors/{cid}/bitbucket/translate"),
        ("POST", "/api/connectors/{cid}/gitlab/translate"),
        # factory:anchor mcp.parity_toolified  (scripts/factory inserts new connector routes above this line)  # noqa: E501
    }
)


def never_cover_completeness_problems(
    real: set,
    never_cover: frozenset,
    semantic: frozenset = None,  # noqa: RUF013
    authoring: frozenset = None,  # noqa: RUF013
    toolified: frozenset = None,  # noqa: RUF013
) -> list:
    """NEVER_COVER is a hand-kept list, so it rots the way hand-kept lists do.

    Re-derive its two MECHANICAL families off the REAL router -- anything whose
    verb is DELETE or whose path ends in a delete/purge/invalidate verb, and
    every per-connection connector route -- and demand they are all listed, so a
    new /api/foo/{id}/delete cannot land outside the list unnoticed. Then pin the
    two verbs that are destructive by SEMANTICS but whose path the regex cannot
    see (customer archive, tm scrub-hedges) by name, so the claim in registry.py
    -- "a destructive route cannot land outside this set unnoticed, whether or not
    its path ends in a delete word" -- is literally true rather than aspirational."""
    semantic = _SEMANTIC_DESTRUCTIVE_ROUTES if semantic is None else semantic
    authoring = _AUTHORING_DELETE_ROUTES if authoring is None else authoring
    toolified = _TOOLIFIED_CONNECTOR_ROUTES if toolified is None else toolified
    problems = []
    destructive = re.compile(
        r"/(delete|purge|invalidate|invalidate-since|"
        r"bulk-delete)$"
    )
    # Staleness half of the authoring carve-out: a listed route that no longer ships
    # would silently pre-approve whatever route next takes that path, so it must be
    # pruned in the same diff (the same discipline the semantic fixture keeps below).
    for method, path in sorted(authoring):
        if (method, path) not in real:
            problems.append(
                f"AUTHORING-DELETE FIXTURE STALE: {method} {path} is carved out of the "
                "delete-suffix rule as an authoring edit but is no longer a route -- "
                "prune _AUTHORING_DELETE_ROUTES in the same diff"
            )
    # Staleness half of the R4 connector carve-out (same discipline): a toolified
    # route that stops being served would silently pre-approve whatever route next
    # takes its path, so it must be pruned in the same diff. (Soundness -- that each
    # is actually COVERED and NOT in NEVER_COVER -- is asserted in main(); here we
    # only keep the carve-out from going stale.)
    for method, path in sorted(toolified):
        if (method, path) not in real:
            problems.append(
                f"TOOLIFIED-CONNECTOR FIXTURE STALE: {method} {path} is carved out of the "
                "connector-family rule as an R4-toolified route but is no longer a "
                "route -- prune _TOOLIFIED_CONNECTOR_ROUTES in the same diff"
            )
    for method, path in sorted(real, key=lambda p: (p[1], p[0])):
        mechanical = (
            method == "DELETE"
            or destructive.search(path)
            or path.startswith("/api/connectors/{cid}/")
        )
        # A workflow STEP delete is a graph-authoring edit covered by edit_workflow, not
        # content destruction -- carved out by name (see _AUTHORING_DELETE_ROUTES). The
        # whole-object workflow/group deletes are NOT carved out and stay required here.
        if mechanical and (method, path) in authoring:
            continue
        # Wave R4: the generic pull/push/sync/retry and every per-platform
        # {kind}/translate route are now COVERED by the connector tools, so they are
        # carved out of the connector-family requirement by name. Every OTHER
        # /api/connectors/{cid}/ route (the Airtable record+webhook family, jira
        # count/translate-pending, salesforce/rediscover-fields, the resolve-links)
        # is NOT carved out and still MUST be in NEVER_COVER.
        if mechanical and (method, path) in toolified:
            continue
        if mechanical and (method, path) not in never_cover:
            problems.append(
                f"NEVER_COVER IS INCOMPLETE: {method} {path} is a destructive or live-tenant "
                "route but is not listed -- add it (or, if it genuinely belongs to "
                "a tool, change the spec first)"
            )
    for method, path in sorted(semantic):
        if (method, path) not in real:
            problems.append(
                f"SEMANTIC-DESTRUCTIVE FIXTURE STALE: {method} {path} is named as destructive "
                "by semantics but is no longer a route this build serves -- update "
                "_SEMANTIC_DESTRUCTIVE_ROUTES in the same diff"
            )
        elif (method, path) not in never_cover:
            problems.append(
                f"NEVER_COVER IS INCOMPLETE: {method} {path} is destructive by SEMANTICS (its "
                "path has no delete suffix, so the regex misses it) but is not "
                "listed -- add it (or change the spec first if it genuinely "
                "belongs to a tool)"
            )
    # Credential / identity SET routes (AUDIT 2026-08-18, M-HIGH-2/3). A route whose
    # path ends in /secret, /oauth-secret or /reset-password sets a credential or a
    # password from a MODEL-AUTHORED value; no agent may ever claim one. The delete-
    # suffix regex above cannot see them (no delete word), and relying on each being
    # hand-listed is EXACTLY how reset-password and vendors/{vid}/secret fell through
    # -- their same-commit siblings (apikey issue/revoke, connectors/{cid}/secret)
    # were listed, these two were not. Re-derive the family off the REAL router so a
    # NEW /secret or /reset-password route cannot land outside NEVER_COVER unnoticed.
    credential = re.compile(r"/(secret|oauth-secret|reset-password)$")
    for method, path in sorted(real, key=lambda p: (p[1], p[0])):
        if credential.search(path) and (method, path) not in never_cover:
            problems.append(
                f"NEVER_COVER IS INCOMPLETE: {method} {path} sets a credential/password from a "
                "model-authored value (its path ends in /secret|/oauth-secret|"
                "/reset-password) but is not listed -- add it (or change the spec "
                "first if it genuinely belongs to a tool)"
            )
    return problems


# =============================================================================
# check 5 -- input schemas are well-formed
# =============================================================================
def schema_problems(tools) -> list:
    """Hand-validated JSON Schema (no jsonschema dependency -- the accounting
    guard takes no dependencies it could be blocked on).

    Every property typed AND described, because an untyped or undocumented
    parameter is the model's problem at call time, not the author's at edit
    time."""
    problems = []
    valid_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    for tool in tools:
        schema = tool.input_schema
        where = f"tool {tool.name}"
        if not isinstance(schema, dict):
            problems.append(f"{where}: input_schema is not a dict")
            continue
        if schema.get("type") != "object":
            problems.append(
                "{}: input_schema type is {!r}, must be 'object'".format(where, schema.get("type"))
            )
        props = schema.get("properties")
        if not isinstance(props, dict):
            problems.append(f"{where}: input_schema has no properties dict")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list):
            problems.append(f"{where}: required is not a list")
            required = []
        for name in required:
            if name not in props:
                problems.append(f"{where}: required names {name!r}, which is not a property")
        for name, prop in sorted(props.items()):
            if not isinstance(prop, dict):
                problems.append(f"{where}: property {name!r} is not a dict")
                continue
            ptype = prop.get("type")
            if ptype not in valid_types:
                problems.append(
                    f"{where}: property {name!r} has type {ptype!r}, not a JSON Schema type"
                )
            if not str(prop.get("description", "")).strip():
                problems.append(
                    f"{where}: property {name!r} has no description -- the model sees this"
                )
            if ptype == "array" and not isinstance(prop.get("items"), dict):
                problems.append(f"{where}: array property {name!r} has no items schema")
    return problems


def forbidden_property_problems(tools, forbidden: dict = None) -> list:  # noqa: RUF013
    """Flags a tool may never OFFER, even on a route it legitimately covers.

    Route-level tables (NEVER_COVER) cannot express this: the route is fine, one
    of its body flags is not. `cleanup` on the key import is the worked example --
    the same POST that merges 4,000 keys will, with one extra boolean, retire
    every key that is not in the file."""
    forbidden = _FORBIDDEN_SCHEMA_PROPS if forbidden is None else forbidden
    problems = []
    for tool in tools:
        banned = forbidden.get(tool.name) or {}
        props = (tool.input_schema or {}).get("properties") or {}
        for name, why in sorted(banned.items()):
            if name in props:
                problems.append(
                    f"FORBIDDEN PROPERTY: {tool.name}'s schema offers {name!r}, which {why}"
                )
    return problems


# =============================================================================
# check 7 -- the spend contract
# =============================================================================
def confirm_problems(tools) -> list:
    """Every spend-tier tool carries a boolean `confirm` (maintainer decision 5).

    Say the honest thing about it: an agent can set confirm=true by itself, so
    this is a disclosure mechanism and a speed bump, not a security control --
    the real controls are the mock-engine 409, department budgets, spend_cap_usd,
    rate limits and the acknowledged-estimate 409, all server-side. What this
    check buys is that the two-phase shape cannot be dropped from a tool that
    bills the operator. Its blind spot -- a spend tool relabelled `read`, which
    leaves this check with nothing to enforce -- is covered by SPEND_ROUTES in
    check 6, not here."""
    problems = []
    for tool in tools:
        if tool.tier != "spend":
            continue
        props = (tool.input_schema or {}).get("properties") or {}
        if "confirm" not in props:
            problems.append(
                f"SPEND TOOL WITHOUT confirm: {tool.name} bills the operator but its schema "
                "has no `confirm` parameter, so nothing makes the agent state the "
                "spend before making it"
            )
        elif props["confirm"].get("type") != "boolean":
            problems.append(
                "{}: `confirm` must be a boolean, not {!r}".format(
                    tool.name, props["confirm"].get("type")
                )
            )
    return problems


# =============================================================================
# check 8 -- exemptions are real exemptions
# =============================================================================
def exempt_shape_problems(registry, exempt_map: dict, categories: set) -> list:
    """A justification nobody validates is a justification nobody writes.

    The review put "" and "nah" in this map and the guard reported a perfectly
    closed accounting over both. So every entry now carries a CATEGORY from the
    spec's own section-5a taxonomy and a reason long enough to be one, and the
    category must agree with the marker: `planned` iff the prose parses to a
    planned tool name."""
    problems = []
    for key, value in sorted(exempt_map.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        where = "{} {}".format(*key)
        if not (isinstance(value, tuple) and len(value) == 2):
            problems.append(f"{where}: exemption value must be (category, reason), got {value!r}")
            continue
        category, reason = value
        if category not in categories:
            problems.append(f"{where}: category {category!r} is not one of {sorted(categories)}")
        is_planned = registry.planned_tool(value) is not None
        # The length floor is for PERMANENT exemptions, which are the ones whose
        # whole content is the argument ("" and "nah" both passed review). A
        # planned marker is validated differently and more strongly: it must name
        # a tool that is really in the spec's scope table (check 9), so its prose
        # is a pointer, not an argument, and is allowed to be terse.
        if not isinstance(reason, str) or (
            not is_planned and len(reason.strip()) < MIN_REASON_CHARS
        ):
            problems.append(
                "%s: justification is %d chars (%r) -- an exemption is a decision, "  # noqa: UP031
                "and under %d chars it is a placeholder"
                % (where, len(str(reason).strip()), str(reason)[:30], MIN_REASON_CHARS)
            )
        if is_planned and category != "planned":
            problems.append(f"{where}: reason is a planned marker but category is {category!r}")
        if category == "planned" and not is_planned:
            problems.append(
                f"{where}: category is 'planned' but the reason does not parse to a "
                f"tool name ({str(reason)[:40]!r})"
            )
    for key, needles in _EXEMPT_REASON_PINS.items():
        if key not in exempt_map:
            problems.append(
                "{} {} carries a pinned constraint but is no longer exempt -- the "
                "constraint has to move with it".format(*key)
            )
            continue
        reason = registry.exempt_reason(exempt_map[key])
        for needle in needles:
            if needle not in reason:
                problems.append(
                    f"LOST CONSTRAINT: {key[0]} {key[1]}'s justification no longer contains {needle!r} "  # noqa: E501
                    "-- the wave that builds this route inherits the constraint "
                    "from here and nowhere else"
                )
    return problems


# =============================================================================
# check 9 -- planned markers stay in sync with the spec's scope table
# =============================================================================
def spec_tool_names(spec_text: str) -> set:
    """Every tool name in docs/specs/mcp-server.md's section-3 family tables.

    Anchored on the TIER COLUMN, not on the row number, because the row number is
    the part an author reformats. Bolding the names, renumbering the rows by
    family (A1, B4...) or adding a leading status column each collapsed the first
    version of this parse to zero -- and a parse that dies on a cosmetic edit
    trains people to loosen the guard. `| ... | `name` | read | ...` is the real
    structure: a backticked identifier immediately followed by a tier."""
    return set(
        re.findall(
            r"^\|(?:[^|\n]*\|){0,2}\s*\**\s*`([a-z_][a-z0-9_]*)`\s*\**[^|\n]*\|"
            r"\s*(?:read|write|spend|destructive)\s*\|",
            spec_text,
            re.M,
        )
    )


def planned_problems(registry, exempt_map: dict, names: set) -> list:
    """Every `planned:` marker must name a tool the spec actually plans.

    This keeps the plan and the registry from drifting apart while waves 1-2 are
    in flight: a marker naming a tool nobody will build is a route that looks
    accounted for and is not. Both failure shapes the review found are caught
    here -- a marker whose name is missing from the spec, and a marker that does
    not parse at all (`Planned:` with a capital P, or `planned:` with nothing
    after it) which would otherwise be miscounted as a permanent exemption."""
    problems = []
    for key, value in sorted(exempt_map.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        planned = registry.planned_tool(value)
        if planned is None:
            if registry.looks_planned(value):
                problems.append(
                    f"MALFORMED PLAN MARKER: {key[0]} {key[1]} starts with 'planned' but does "
                    f"not parse to a tool name ({registry.exempt_reason(value)[:40]!r}) -- it would be counted as a "  # noqa: E501
                    "PERMANENT exemption and never checked against the spec"
                )
            continue
        if planned not in names:
            problems.append(
                f"PHANTOM PLAN: {key[0]} {key[1]} is exempt as 'planned:{planned}', but {planned!r} is not a "  # noqa: E501
                "tool in docs/specs/mcp-server.md's scope table"
            )
    return problems


# =============================================================================
# check 10 -- vocabularies, descriptions, and the debt list
# =============================================================================
def app_scope_vocabulary() -> set:
    """The API-key scopes this build really enforces, re-derived from the code.

    Read off every gate in app/web/ rather than a hand-kept list or two named
    files: `docs` from deps._require_doc_scope, `keys` from
    keys._require_keys_scope, plus the base grant taken from
    apikeys.normalize_scopes itself (it adds "api" to every key). Globbing the
    package rather than naming two files is the difference between noticing a
    third gate and not."""
    from app.services import apikeys

    scopes = set(apikeys.normalize_scopes("").split())
    patterns = (
        re.compile(r"[\"']([a-z_]+)[\"']\s*(?:not\s+)?in\s+apikeys\.scope_set"),
        re.compile(r"apikeys\.has_scope\([^)]*[\"']([a-z_]+)[\"']"),
    )
    files = sorted(glob.glob(os.path.join(_ROOT, "app", "web", "**", "*.py"), recursive=True))
    assert len(files) >= 10, "the app/web glob found only %d files" % len(files)  # noqa: UP031
    for path in files:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for pattern in patterns:
            scopes.update(pattern.findall(text))
    return scopes


def field_problems(tools, families, tiers, scopes, gate_keys) -> list:
    problems = []
    for tool in tools:
        if tool.family not in families:
            problems.append(f"{tool.name}: family {tool.family!r} is not one of {sorted(families)}")
        if tool.tier not in tiers:
            problems.append(f"{tool.name}: tier {tool.tier!r} is not one of {sorted(tiers)}")
        if tool.scope not in scopes:
            problems.append(
                f"{tool.name}: scope {tool.scope!r} is not a grant this build enforces ({sorted(scopes)}) -- the "  # noqa: E501
                "startup scope report would then promise a grant nobody checks"
            )
        if tool.feature_gate is not None and tool.feature_gate not in gate_keys:
            problems.append(
                f"{tool.name}: feature_gate {tool.feature_gate!r} is not a real settings key -- a typo'd kill "  # noqa: E501
                "switch makes the tool description lie about what turns it off"
            )
        if not tool.routes:
            problems.append(f"{tool.name}: declares no routes")
        if not 40 <= len(tool.summary) <= 320:
            problems.append(
                "%s: summary is %d chars; it must be 40-320 -- a blank or stub "  # noqa: UP031
                "summary is what the MODEL reads to decide whether to call it"
                % (tool.name, len(tool.summary))
            )
    seen = set()
    for tool in tools:
        if tool.name in seen:
            problems.append(f"duplicate tool name {tool.name!r}")
        seen.add(tool.name)
    return problems


def description_problems(tools) -> list:
    """A description that promises what the tool cannot do is worse than none:
    it carries the authority of the registry.

    Three ways it can lie. A `pins` string that a rewrite dropped means a
    load-bearing contract sentence silently left the description. An /api path
    mentioned in a summary that is not in that tool's `routes` means the
    description promises a call the tool may not make. And -- the review's find
    -- deleting the PINS TUPLE rather than the sentence leaves the first loop
    iterating over nothing, so SPEC section 9's required pins are checked against
    the tuple, not merely through it."""
    problems = []
    for tool in tools:
        for pin in tool.pins:
            if pin not in tool.summary:
                problems.append(
                    f"LOST PIN: {tool.name}'s summary no longer contains {pin!r} -- a pinned "
                    "contract sentence was dropped or reworded"
                )
        own = {p for _m, p in tool.routes}
        for path in re.findall(r"(/api/[A-Za-z0-9_{}/.\-]+)", tool.summary):
            path = path.rstrip(".,;")
            if path not in own:
                problems.append(
                    f"SUMMARY PROMISES A FOREIGN ROUTE: {tool.name} mentions {path}, which is "
                    "not in its routes"
                )
        for needle in _SPEC_REQUIRED_PINS.get(tool.name, ()):
            if not any(needle in pin for pin in tool.pins):
                problems.append(
                    f"UNPINNED CONTRACT: {tool.name} must PIN {needle!r} (SPEC section 9); an "
                    "emptied `pins` tuple leaves the sentence free to be reworded "
                    "away"
                )
    return problems


def server_signature_gaps(server_source: str, tools) -> dict:
    """Where the registry's declared schemas and server.py's real tool signatures
    disagree, per tool name.

    AST only -- server.py imports `mcp` and `httpx`, so this suite must never
    import it. This is the SDK-free half of the spec's registry-to-server check,
    and it is what makes the wave-1 debt list unfakeable: the divergences are
    computed from the code, not asserted by the person editing the list."""
    tree = ast.parse(server_source, "server.py")
    params = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = [
                a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            ]
            params[node.name] = [n for n in names if n != "self"]
    gaps = {}
    for tool in tools:
        if tool.name not in params:
            continue  # not shipped yet -- not a divergence
        declared = set((tool.input_schema or {}).get("properties") or {})
        if tool.tier == "spend":
            # #757: `plan_id` is consumed by _dispatch before the handler runs (spend
            # tools only), so it is declared but never a handler parameter. Named, not
            # derived, so no other property can hide behind this exemption.
            declared -= {"plan_id"}
        actual = set(params[tool.name])
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        if missing or extra:
            gaps[tool.name] = [
                f"declared in the registry but not a server.py parameter: {m}" for m in missing
            ] + [f"a server.py parameter with no schema property: {e}" for e in extra]
    return gaps


def debt_problems(gaps: dict, debt) -> list:
    """The wave-1 debt list may only be closed by closing the debt.

    The review's mutation was to empty the tuple -- "wave 1 done" -- while
    server.py still diverged, and nothing objected. Now every divergence computed
    off the real signatures must be named by an entry, so deleting the list while
    the code still diverges is red and the only green path is to fix server.py."""
    problems = []
    text = "\n".join(debt)
    for name, details in sorted(gaps.items()):
        if name not in text:
            problems.append(
                "UNRECORDED DIVERGENCE: the registry and server.py disagree about "
                "{} ({}) but SERVER_PY_WAVE1_DEBT does not mention it -- the debt "
                "list is closed by fixing the code, not by deleting the line".format(
                    name, "; ".join(details)
                )
            )
    return problems


def server_literal_collection(server_source: str, name: str):
    """A module-level literal collection out of server.py, by name, via AST.

    The guard must never import server.py (it needs the MCP SDK and httpx), so
    every claim it makes about that file is read statically. Handles the
    `NAME: frozenset = frozenset({...})` shape the allowlists use."""
    tree = ast.parse(server_source, "server.py")
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else list(getattr(node, "targets", []))
        )
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Call):
            fname = getattr(value.func, "id", "")
            if fname in ("frozenset", "set", "tuple", "list"):
                return set(ast.literal_eval(value.args[0])) if value.args else set()
        return set(ast.literal_eval(value))
    return None


def install_locked_parity_problems(server_source: str, remote_immutable) -> list:
    """server.py's `_INSTALL_LOCKED_KEYS` MUST equal settings.REMOTE_IMMUTABLE_KEYS.

    THE HOLE THIS CLOSES. manage_install refuses install-locked keys CLIENT-SIDE as
    defense-in-depth over the /api route's server-side refusal (the real enforcer).
    That mirror is a hand-maintained frozenset in server.py, and it silently rotted:
    it froze at the original 6 keys while REMOTE_IMMUTABLE_KEYS grew to 25 across
    R2/R3 and later sprints (Q1 blobstore_s3_*, Q2 metrics/otel_*, P pii_ner_*). A
    key host-only over /api but ABSENT here is a defense-in-depth GAP; a key here but
    NOT host-only over /api would over-block a legitimate remote set. Both directions
    are red. The server may not import `app` (it is a pure HTTP client), so read the
    frozenset off the AST -- exactly how every other server.py claim in this guard is
    made -- and demand exact equality. A new REMOTE_IMMUTABLE_KEYS member now forces
    the server.py copy to be updated in the SAME diff, instead of drifting unnoticed."""
    locked = server_literal_collection(server_source, "_INSTALL_LOCKED_KEYS")
    if locked is None:
        return [
            "could not read _INSTALL_LOCKED_KEYS out of server.py -- the manage_install "
            "client-side host-only refusal is what this check pins; find it or the check "
            "is vacuous"
        ]
    remote = set(remote_immutable)
    problems = []
    for key in sorted(remote - locked):
        problems.append(
            f"INSTALL-LOCKED GAP: {key!r} is on settings.REMOTE_IMMUTABLE_KEYS (host-only "
            "over /api) but MISSING from server.py's _INSTALL_LOCKED_KEYS -- manage_install "
            "would accept it client-side and only the /api route would refuse it (defense-in-"
            "depth drift). Add it to _INSTALL_LOCKED_KEYS."
        )
    for key in sorted(locked - remote):
        problems.append(
            f"INSTALL-LOCKED EXTRA: {key!r} is in server.py's _INSTALL_LOCKED_KEYS but NOT on "
            "settings.REMOTE_IMMUTABLE_KEYS -- manage_install over-blocks a key the /api route "
            "would accept. Remove it from _INSTALL_LOCKED_KEYS (or add it to REMOTE_IMMUTABLE_KEYS "
            "if it is genuinely host-only)."
        )
    return problems


def server_dict_literal(server_source: str, name: str):
    """A module-level DICT literal out of server.py, by name, via AST.

    Same import-free discipline as server_literal_collection (server.py needs the
    MCP SDK), but returns the whole dict rather than a set of keys -- needed for
    _CONNECTOR_TRANSLATE_ROUTES, whose VALUES (the path suffixes) are the load-
    bearing half. Handles the `NAME: dict = {...}` and `NAME = {...}` shapes."""
    tree = ast.parse(server_source, "server.py")
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else list(getattr(node, "targets", []))
        )
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    return None


def connector_translate_dict_problems(server_source: str, tools) -> list:
    """server.py's _CONNECTOR_TRANSLATE_ROUTES, pinned to connector_translate's
    REGISTRY route-list -- the one connector seam the rest of the wave left unpinned.

    THE HOLE THIS CLOSES (adversarial review F1). connector_translate builds its
    outbound path as "/api/connectors/%d/%s" % (cid, suffix), where `suffix` comes
    from _CONNECTOR_TRANSLATE_ROUTES in server.py -- a SECOND source of truth for
    which routes the tool actually POSTs to. The parity guard's "no tool touches a
    NEVER_COVER route" check (coverage_policy_problems) reasons only over the
    REGISTRY route-list; today the dict and the registry agree, but a one-line edit
    adding e.g. "airtable": ("airtable/records", ...) to the dict would make
    connector_translate POST to a NEVER_COVER destructive route while every registry
    check stayed GREEN. So derive the dict's routes off the AST and demand they equal
    connector_translate's declared POST routes, exactly -- a drift in either
    direction is red. Read import-free like every other claim this guard makes about
    server.py."""
    routes_dict = server_dict_literal(server_source, "_CONNECTOR_TRANSLATE_ROUTES")
    if not routes_dict:
        return [
            "could not read _CONNECTOR_TRANSLATE_ROUTES out of server.py -- the "
            "connector_translate dispatch map is what this check pins; find it or "
            "the check is vacuous"
        ]
    problems = []
    derived = set()
    for value in routes_dict.values():
        # each value is (path_suffix, id_field); tolerate a shape drift loudly
        if not (isinstance(value, tuple) and len(value) == 2):
            problems.append(f"MALFORMED _CONNECTOR_TRANSLATE_ROUTES ENTRY: {value!r}")
            continue
        derived.add(("POST", f"/api/connectors/{{cid}}/{value[0]}"))
    tool = next((t for t in tools if t.name == "connector_translate"), None)
    if tool is None:
        return problems + [  # noqa: RUF005
            "connector_translate is not in the registry -- the F1 pin has no subject"
        ]
    registry_posts = {r for r in tool.routes if r[0] == "POST"}
    for route in sorted(derived - registry_posts):
        problems.append(
            "CONNECTOR-TRANSLATE DICT DRIFT: server.py's _CONNECTOR_TRANSLATE_ROUTES "
            "makes connector_translate POST to {} {}, which its REGISTRY route-list "
            "does not declare -- the tool can reach a route the parity guard never "
            "checks against NEVER_COVER (adding it to the dict must also add it to the "
            "registry, where coverage_policy_problems then vets it)".format(*route)
        )
    for route in sorted(registry_posts - derived):
        problems.append(
            "CONNECTOR-TRANSLATE DICT GAP: connector_translate declares {} {} in the "
            "registry but server.py's _CONNECTOR_TRANSLATE_ROUTES never POSTs to it -- "
            "a declared route the handler cannot actually reach".format(*route)
        )
    return problems


#: The `%s` splices into an "/api..." path LITERAL in server.py that do NOT go
#: through _seg() and are nonetheless safe, each with the argument's provenance.
#: server.py builds outbound paths as "/api/.../%s..." % value. A MODEL-AUTHORED
#: free-text value spliced UNQUOTED is the M-HIGH-1 path-injection class: a probe
#: passed provider="../../customers/1/archive?" to manage_install and httpx
#: normalised it onto POST /api/customers/{cid}/archive -- a NEVER_COVER route --
#: and the customer row MUTATED. The guard below demands every %s in an /api path
#: literal be fed by _seg() (which percent-encodes the whole value into ONE
#: segment, so `/` and `..` cannot escape it) UNLESS the (path-template, argument-
#: source) pair is reviewed here. Keyed on BOTH halves, so a new splice on a listed
#: path -- or a listed name on a new path -- is NOT silently pre-approved; staleness-
#: checked, so an entry that no longer matches a real splice is pruned in the same diff.
_APPROVED_PATH_SPLICES = {
    (
        "/api/documents/%s/translate",
        "doc_id",
    ): "doc_id is the id the install's OWN upload response returned "
    "(up.json()['document_id']) -- server-authored, not model free-text.",
    (
        "/api/glossaries/%d/%s",
        "act",
    ): "act is a FIXED vocabulary ('approve'/'unapprove'), selected by an "
    "`elif act in (...)` branch before the splice, never free text.",
    (
        "/api/glossaries/%d/candidates/%d/%s",
        "verb",
    ): "verb is derived to 'accept'/'drop' by the handler -- a fixed vocabulary.",
    (
        "/api/connectors/%d/%s",
        "suffix",
    ): "suffix is a VALUE of _CONNECTOR_TRANSLATE_ROUTES, a module-level literal "
    "dict itself pinned by connector_translate_dict_problems -- never free text.",
    ("/api/users/%d/%s", "act"): "act is a FIXED vocabulary ('enable'/'disable'), selected by an "
    "`if act in (...)` branch before the splice, never free text.",
}

#: "/api...%s" path TEMPLATES that live in a table (a dict/tuple/list value) rather
#: than at the `%` site, and are nonetheless safe. The BinOp scan cannot classify
#: these: the substitution happens elsewhere as `tpl % X` with `tpl` a VARIABLE the
#: guard cannot trace to _seg() (e.g. server.py's _CONFIGURE_ACTIONS, formatted as
#: `path_tpl % int(project_id)`). So a `%s` in ANY table-held /api template is treated
#: as free-text-unsafe by default and must be listed here to be allowed -- EMPTY
#: today, because every such template in server.py uses `%d` (int-safe) and keeps free
#: text in the request BODY. NOTE the one table that DOES feed a free-text value into
#: a path -- _CONNECTOR_TRANSLATE_ROUTES -- holds bare route SUFFIXES ("jira/translate"),
#: not "/api..." templates, so it is invisible to this scan by construction and is
#: pinned separately by connector_translate_dict_problems. Staleness-checked below.
_APPROVED_TABLE_SPLICES: dict = {}


def path_splice_problems(server_source, approved=None, approved_tables=None) -> list:
    """Every free-text `%s` reaching an "/api..." path in server.py that is neither
    _seg()-quoted nor a reviewed exception -- M-HIGH-1's class, made un-reintroducible.

    Read import-free off the AST, like every other claim this guard makes about
    server.py. The threat is a MODEL-AUTHORED value spliced into the URL PATH
    unquoted: httpx then normalises `../..` across path boundaries and the tool
    reaches a route it never declared (a NEVER_COVER mutation, in the probe). The
    fix -- _seg() -- percent-encodes the value into ONE segment; this guard makes
    that mandatory, so the coverage_policy_problems reasoning ('a tool may not
    declare a NEVER_COVER route') cannot be side-stepped by a handler that builds a
    forbidden path at runtime out of a declared one.

    THREE surfaces, because the smuggle has three shapes:
      * `%` at a STRING LITERAL -- "/api/.../%s" % value -- classified per-%s: an
        _seg() call or a reviewed (path, arg) exception passes, anything else is red.
      * f-string (JoinedStr) -- f"/api/.../{value}..." -- classified per interpolation
        exactly like the %-literal: _seg() or int() (the %d analogue) passes, a reviewed
        (reconstructed-template, arg) exception passes, anything else is red. The template
        is reconstructed to the SAME %-form key the %-literal surface uses, so the
        UP031 %->f-string modernisation cannot blind the guard to an /api path. A
        `%s`/`%d` left in the f-string's LITERAL text is red on sight: that value is
        substituted at a `%` use site NEITHER surface can classify (a BinOp with a
        JoinedStr left operand), so the mixed form would otherwise free it.
      * `%` at a TABLE-held template -- the "/api/.../%s" string lives in a dict/
        tuple/list and is formatted elsewhere as `tpl % X` with `tpl` a variable
        (the _CONFIGURE_ACTIONS pattern). The BinOp scan is blind to that use site,
        so a `%s` in ANY table-held /api template is flagged unless the template is
        in _APPROVED_TABLE_SPLICES. Without this surface a free-text %s added
        via the table pattern would reintroduce the exact class undetected."""
    approved = _APPROVED_PATH_SPLICES if approved is None else approved
    approved_tables = _APPROVED_TABLE_SPLICES if approved_tables is None else approved_tables
    problems = []
    seen = set()
    literal_lefts = set()  # id() of the Constant nodes the BinOp scan owns
    try:
        tree = ast.parse(server_source, "server.py")
    except SyntaxError as exc:
        return [f"server.py does not parse ({exc})"]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)):
            continue
        left = node.left
        if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
            continue
        fmt = left.value
        if not fmt.startswith("/api/"):
            continue
        literal_lefts.add(id(left))  # owned here, so the table scan skips it
        specs = re.findall(r"%[sd]", fmt)
        if "%s" not in specs:
            continue
        args = list(node.right.elts) if isinstance(node.right, ast.Tuple) else [node.right]
        if len(args) != len(specs):
            problems.append(
                "PATH SPLICE UNREADABLE: %r takes %d conversions but %d argument(s) "  # noqa: UP031
                "-- the guard cannot map %%s to its value; keep the splice legible"
                % (fmt, len(specs), len(args))
            )
            continue
        for spec, arg in zip(specs, args, strict=False):
            if spec != "%s":
                continue  # %d coerces via int(): path-safe
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "_seg"
            ):
                continue  # percent-encoded into one segment
            arg_src = ast.unparse(arg)
            key = (fmt, arg_src)
            seen.add(key)
            if key not in approved:
                problems.append(
                    f"UNQUOTED PATH SPLICE: server.py splices {arg_src} into the path {fmt!r} "
                    "with no _seg() -- a model-authored value can carry `../..` "
                    "across path boundaries (httpx normalises it) and reach a route "
                    "the tool never declared. Wrap it in _seg(), or, if it is "
                    f"provably not free text, add ({fmt!r}, {arg_src!r}) to _APPROVED_PATH_SPLICES "
                    "with the reason"
                )
    # Surface 1b: f-string (JoinedStr) "/api/...{value}..." paths. The same threat and
    # the same rules as the `%`-literal surface, read off the JoinedStr AST instead: each
    # interpolated value is classified exactly like a %s -- an _seg() call passes (one
    # percent-encoded segment) and an int() coercion passes (the f-string analogue of %d,
    # digits only), a reviewed (template, arg) exception passes, and anything else is a
    # model-authored value reaching the URL PATH unquoted and goes red. The template is
    # reconstructed with int()-values -> %d and every other value -> %s, then keyed into
    # the SAME _APPROVED_PATH_SPLICES table and the SAME `seen` set as the %-literal
    # surface -- so an approved splice reads identically whether it is written `"..." % x`
    # or f"...{x}...", and converting one form to the other neither loses coverage nor
    # trips the staleness ratchet. Without this surface, the UP031 %->f-string modernisation
    # would silently blind the guard to exactly the /api paths it exists to watch.
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        template_parts = []
        literal_parts = []  # only the LITERAL text of the f-string, no interpolations
        interpolations = []  # (inner_node, is_int, is_seg), in source order
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                template_parts.append(value.value)
                literal_parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = value.value
                is_seg = (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_seg"
                )
                is_int = (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "int"
                )
                template_parts.append("%d" if is_int else "%s")
                interpolations.append((inner, is_int, is_seg))
            else:  # defensive: an unexpected node shape is treated as free text
                template_parts.append("%s")
        fmt = "".join(template_parts)
        if not fmt.startswith("/api/"):
            continue
        # A `%s`/`%d` in the f-string's LITERAL text is a second, un-classifiable
        # substitution: the value arrives at a `%` USE SITE the JoinedStr scan cannot
        # see (`f"/api/x/{_seg(a)}/%s" % evil` -- the BinOp surface skips it because its
        # left operand is a JoinedStr, not a Constant, and the loop below only classifies
        # INTERPOLATIONS). The pure-%-literal spelling of that same path is red; without
        # this rule the f-string spelling silently frees the %-arg. No approval table:
        # mixing the two forms in one /api path is never the right way to write it.
        stray = re.findall(r"%[sd]", "".join(literal_parts))
        if stray:
            problems.append(
                f"MIXED-FORM PATH TEMPLATE: server.py's f-string /api path {fmt!r} carries a "
                f"literal {stray[0]} conversion -- that value is substituted at a `%` use site "
                "this scan cannot classify (and the `%`-literal surface skips a BinOp whose "
                "left operand is an f-string), so it reaches the URL PATH unguarded. Write the "
                "whole path as one f-string with _seg()/int() interpolations, or as one "
                "`%`-literal -- never half of each"
            )
        for inner, is_int, is_seg in interpolations:
            if is_int or is_seg:
                continue  # int() coercion (the %d analogue) or _seg()-quoted: path-safe
            arg_src = ast.unparse(inner)
            key = (fmt, arg_src)
            seen.add(key)
            if key not in approved:
                problems.append(
                    f"UNQUOTED PATH SPLICE: server.py interpolates {arg_src} into the f-string "
                    f"path {fmt!r} with no _seg() -- a model-authored value can carry `../..` "
                    "across path boundaries (httpx normalises it) and reach a route the tool "
                    "never declared. Wrap it in _seg(), coerce it with int(), or, if it is "
                    f"provably not free text, add ({fmt!r}, {arg_src!r}) to _APPROVED_PATH_SPLICES "
                    "with the reason"
                )
    for key in sorted(set(approved) - seen):
        problems.append(
            "STALE PATH-SPLICE EXCEPTION: {!r} {!r} is approved as a non-_seg path "
            "splice but no such splice exists in server.py any more -- prune it in "
            "the same diff so it cannot pre-approve a future splice".format(*key)
        )
    # Surface 2: TABLE-held "/api...%s" templates. Any "/api/..." string constant
    # that contains %s and is NOT the left operand of an /api `%` BinOp above is fed
    # from a use-site the guard cannot trace (a table formatted as `tpl % X`), so its
    # %s is treated as free-text-unsafe. Every such /api template today uses %d only,
    # so this finds nothing -- until a free-text %s is added via the table pattern.
    table_seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        tpl = node.value
        if not (tpl.startswith("/api/") and "%s" in tpl):
            continue
        if id(node) in literal_lefts:
            continue  # already classified by the BinOp scan
        table_seen.add(tpl)
        if tpl not in approved_tables:
            problems.append(
                f"PATH-TEMPLATE TABLE SPLICE: server.py holds the path template {tpl!r} "
                "outside a `%`-literal (a dict/tuple/list value formatted elsewhere "
                "as `tpl % X` with a VARIABLE the guard cannot trace to _seg()) -- a "
                "%s reaching an /api path this way is the M-HIGH-1 class through the "
                "back door. Use %d (int) in the template and keep free text in the "
                f"request BODY, or, if the value is provably not free text, add {tpl!r} to "
                "_APPROVED_TABLE_SPLICES with the reason"
            )
    for tpl in sorted(set(approved_tables) - table_seen):
        problems.append(
            f"STALE TABLE-SPLICE EXCEPTION: {tpl!r} is approved as a table-held /api path "
            "template but no such template exists in server.py any more -- prune it "
            "in the same diff so it cannot pre-approve a future one"
        )
    return problems


def schema_keyword_problems(tools, honoured) -> list:
    """Every JSON-Schema keyword a registry schema uses that the tool derivation
    does not translate into the model that validates the call.

    THE FAILURE THIS PREVENTS IS SILENT. `_signature_from_schema` used to ignore
    an unknown keyword, so the first tool to write `"pattern": "^[a-z]+$"` would
    have advertised a constraint nothing enforced, with every suite green. The
    honoured set is read out of server.py rather than restated here, so the two
    cannot drift: teaching the derivation a keyword is what permits the schema to
    use it."""
    problems = []
    if not honoured:
        return [
            "could not read _HONOURED_SCHEMA_KEYWORDS out of server.py -- the "
            "derivation's own allowlist is what this check reads; find it or "
            "this check is vacuous"
        ]
    for tool in tools:
        for name, prop in sorted(((tool.input_schema or {}).get("properties") or {}).items()):
            if not isinstance(prop, dict):
                continue
            for where, node in (
                ((f"{tool.name}.{name}"), prop),
                ((f"{tool.name}.{name}.items"), prop.get("items")),
            ):
                if not isinstance(node, dict):
                    continue
                for kw in sorted(set(node) - set(honoured)):
                    problems.append(
                        f"UNENFORCED SCHEMA KEYWORD: {where} advertises {kw!r}, which the "
                        "tool derivation does not translate -- the model would be "
                        "promised a constraint nothing checks"
                    )
    return problems


#: Free-form object properties (`{"type": "object"}` with no sub-schema, or an
#: array of them) are the OTHER way a destructive flag reaches the API: the
#: name-based _FORBIDDEN_SCHEMA_PROPS above only sees property NAMES, and a
#: property whose VALUE is an arbitrary dict smuggles field names inside itself.
#: The live review used exactly that: `upsert_keys(fields={"is_archived": true})`
#: archived a key -- removing it from every downstream build file while the change
#: feed still said op="updated".
#:
#: So every such property must be declared here as one of:
#:   ("allowlist", "_CONST")   the handler filters it through that module-level
#:                             allowlist in server.py, which the guard reads and
#:                             checks for destructive names;
#:   ("value-shaped", "why")   its keys are DATA (a plural category, a locale
#:                             pair), not field names, so there is nothing to
#:                             allowlist -- with a reason someone can argue with.
#: An undeclared one is RED. Adding a free-form object is thereby a decision.
_FREEFORM_OBJECT_POLICY = {
    # SPEC-project-automation part C: custom project fields. The keys are field KEYS the
    # install's admins defined (custom_fields.CustomFieldDef); the /api route refuses an
    # unknown key (400) and type-checks, length-caps and URL-allowlists every value, all
    # or nothing, so no field name of another entity and no credential crosses this object.
    ("list_projects", "fields"): (
        "value-shaped",
        "keys are admin-defined custom field keys and values are filter expressions; "
        "/api/projects refuses an unknown key with a 400 and parses each expression "
        "against the field's type.",
    ),
    ("create_project", "custom_fields"): (
        "value-shaped",
        "keys are admin-defined custom field keys; POST /api/projects validates every key "
        "and value (type, length, https-only URLs, required) before any row is written, "
        "and one bad value creates no project.",
    ),
    ("set_project_fields", "fields"): (
        "value-shaped",
        "keys are admin-defined custom field keys; PATCH /api/projects/{pid}/fields refuses "
        "an unknown or archived key, validates every value, and is all or nothing.",
    ),
    ("upsert_keys", "fields"): ("allowlist", "_KEY_EDIT_FIELDS"),
    ("upsert_keys", "keys"): ("allowlist", "_KEY_ROW_FIELDS"),
    ("upsert_keys", "forms"): (
        "value-shaped",
        "keys are CLDR plural categories (one/other/few...) and values are text; "
        "the API sets one value per category and refuses an unknown one. No field "
        "name of any kind crosses this boundary.",
    ),
    # Project automation: the scope object NARROWS what a project reads inside a connection's
    # credential and is FIELD-NAMED (max_articles, content_types...), so it gets the allowlist
    # treatment: the handler refuses any name outside _AUTOMATION_SCOPE_FIELDS before the call.
    ("project_automation", "scope"): ("allowlist", "_AUTOMATION_SCOPE_FIELDS"),
    ("configure_project", "pair_providers"): (
        "value-shaped",
        'keys are locale pairs ("en-US:de-DE") and values are provider names; '
        "pair_providers.parse_pair_mapping validates both halves and a bad "
        "mapping is a clean 400. No field name crosses this boundary.",
    ),
    # Wave 2: term metadata IS field-named (part_of_speech, definition...), so
    # it gets the allowlist treatment, not a value-shaped waiver. The app's own
    # glossary.clean_meta allowlists the same keys but silently DROPS the rest;
    # the server-side filter refuses them by name instead, because a silent
    # drop is how an agent reports success for a thing that did not happen.
    ("edit_glossary", "meta"): ("allowlist", "_GLOSSARY_TERM_META_FIELDS"),
    # SPEC-workflow-authoring-api: a workflow step is FIELD-NAMED (step_type, provider,
    # prompt_text...), so create_workflow's step objects and edit_workflow's field map
    # get the allowlist treatment (refuse an unknown field by name, never drop it),
    # same as the key/glossary allowlists. Both point at the SAME server.py collection.
    ("create_workflow", "steps"): ("allowlist", "_WORKFLOW_STEP_FIELDS"),
    ("edit_workflow", "fields"): ("allowlist", "_WORKFLOW_EDIT_FIELDS"),
    # Wave R3 -- manage_install. Both objects are open namespaces validated on the way
    # in, and the ONE secret each could smuggle is refused by NAME client-side.
    ("manage_install", "settings"): (
        "value-shaped",
        "keys are setting NAMES from a controlled namespace (the /api route refuses "
        "any key not in settings.DEFAULTS with a 400), and the handler refuses a "
        "secret-marked (password/secret/token/_enc/api_key) or install-locked key by "
        "name before any HTTP call; values are the setting strings. The name-based "
        "check the guard exists to protect is present on BOTH surfaces.",
    ),
    ("manage_install", "config"): (
        "value-shaped",
        "keys are non-secret ENGINE config params (formality/region/model_type/...); "
        "values are the params. The one secret an engine config can carry "
        "(access_key_secret, the Lara/Amazon pair) matches the secret-marker rule and "
        "is refused by name in the handler (_reject_secret_config) before any HTTP "
        "call, so no credential rides this free-form object.",
    ),
}

#: Field names that must never appear in a forwarding allowlist. Each destroys
#: content the change feed does not report as destroyed: is_archived/is_hidden/
#: cleanup/delete/... retire a KEY from downstream output with no op=deleted entry;
#: `force` (2026-08-05) accepts the destruction of a plural key's translated
#: non-`other` FORMS on demotion while the feed still says op="updated". Same
#: silent-loss class, different casualty - and both are a human's call, not an
#: LLM's, so neither may ride an MCP forwarding allowlist.
_DESTRUCTIVE_FIELD_NAMES = frozenset(
    {
        "is_archived",
        "is_hidden",
        "cleanup",
        "delete",
        "deleted",
        "purge",
        "confirm_all",
        "prune",
        "replace_all",
        "force",
    }
)


def freeform_object_problems(tools, server_source, policy=None, check_stale=True) -> list:
    """Every free-form object property, held to a declared, checkable policy."""
    policy = _FREEFORM_OBJECT_POLICY if policy is None else policy
    problems = []
    if check_stale:
        # A policy entry that outlives its property is the exemption-rot pattern
        # one table over: it silently pre-approves whatever property later takes
        # that name.
        live = {
            (t.name, p) for t in tools for p in ((t.input_schema or {}).get("properties") or {})
        }
        for key in sorted(set(policy) - live):
            problems.append(
                "STALE FREE-FORM POLICY: {}.{} no longer exists as a property -- a "
                "stale entry pre-approves whatever property next takes that name".format(*key)
            )
    for tool in tools:
        for name, prop in sorted(((tool.input_schema or {}).get("properties") or {}).items()):
            if not isinstance(prop, dict):
                continue
            node = prop.get("items") if prop.get("type") == "array" else prop
            if not isinstance(node, dict) or node.get("type") != "object":
                continue
            if node.get("properties") or node.get("additionalProperties"):
                continue  # constrained by its own sub-schema
            entry = policy.get((tool.name, name))
            if entry is None:
                problems.append(
                    f"UNDECLARED FREE-FORM OBJECT: {tool.name}.{name} takes an arbitrary object "
                    "with no sub-schema and no entry in _FREEFORM_OBJECT_POLICY -- "
                    "a property whose VALUE is a dict smuggles field names past "
                    "every name-based check"
                )
                continue
            kind, detail = entry
            if kind == "value-shaped":
                if len(str(detail).strip()) < MIN_REASON_CHARS:
                    problems.append(
                        "%s.%s is declared value-shaped with a %d-char reason -- "  # noqa: UP031
                        "that is the argument, so it has to be one"
                        % (tool.name, name, len(str(detail).strip()))
                    )
                continue
            allowed = server_literal_collection(server_source, detail)
            if allowed is None:
                problems.append(
                    f"MISSING ALLOWLIST: {tool.name}.{name} names {detail}, which is not a module-level "  # noqa: E501
                    "literal collection in server.py"
                )
                continue
            if not allowed:
                problems.append(f"{tool.name}: {detail} is empty")
            leaked = sorted(set(allowed) & _DESTRUCTIVE_FIELD_NAMES)
            if leaked:
                problems.append(
                    "DESTRUCTIVE FIELD IN AN ALLOWLIST: server.py's {} forwards {} "
                    "for {}.{} -- each destroys content the change feed does not "
                    "report as destroyed (a key retired with no op=deleted entry, or "
                    "`force` discarding a plural key's translated forms under an "
                    "op=updated), the human-only silent-loss class the keys family "
                    "refuses over MCP".format(detail, ", ".join(leaked), tool.name, name)
                )
    return problems


def plan_estimate_rot_problems(
    estimate_routes: frozenset, spend_routes: frozenset, real: set
) -> list:
    """registry.PLAN_ESTIMATE_ROUTES is the TIGHTEST perimeter set -- the routes
    whose own unconfirmed POST is genuinely the estimate. Guard it against rot the
    way the others are: every entry must be a real route AND a SPEND_ROUTES route
    (a route that estimates today still bills when confirmed). Widening it to name
    a launch route is the only way to re-open the plan-mode hole, so it is the
    reviewable act this check forces into a diff someone reads."""
    problems = []
    for route in sorted(estimate_routes):
        if not (isinstance(route, tuple) and len(route) == 2):
            problems.append(f"PLAN-ESTIMATE MALFORMED ENTRY: {route!r}")
            continue
        if route not in real:
            problems.append(
                "PLAN-ESTIMATE STALE: {} {} is named an estimate-POST route but is "
                "no longer served".format(*route)
            )
        if route not in spend_routes:
            problems.append(
                "PLAN-ESTIMATE NON-SPEND: {} {} is whitelisted as an estimate POST "
                "but is not even a SPEND_ROUTES route -- it does not bill, so it "
                "has no business in a spend perimeter".format(*route)
            )
    return problems


def plan_safe_problems(
    server_source: str, spend_routes: frozenset, tools, estimate_routes: frozenset
) -> list:
    """server.py's plan-mode POST whitelist, held to the ESTIMATE perimeter.

    Wave 2 gave `_call` ONE exception to "planning never sends a non-GET":
    _PLAN_SAFE_REQUESTS, so start_evaluation's plan can ask the API's own
    two-phase POST for its estimate (the unconfirmed call computes a price and
    returns before anything launches - there is no GET that serves it). An
    unconstrained whitelist would be a hole the exact shape of the contextvar
    backstop, so it is read out of server.py's AST and every entry must be
    (a) a registry.PLAN_ESTIMATE_ROUTES route - the narrow set of billing routes
    whose unconfirmed POST launches NOTHING; SPEND_ROUTES is too broad, eight of
    its nine routes launch the instant they are POSTed, so a change could
    whitelist POST /api/workflows/{wid}/run and PASS the old SPEND_ROUTES-only
    check while spending in plan mode; (b) still a SPEND_ROUTES route (belt and
    braces, since ESTIMATE is a subset); and (c) declared by a spend-tier tool, so
    the whitelist cannot outlive or outgrow the tool it serves. A missing/empty
    set is fine: no escape hatch at all is the safest shape."""
    entries = server_literal_collection(server_source, "_PLAN_SAFE_REQUESTS")
    if not entries:
        return []
    problems = []
    spend_tool_routes = {r for t in tools if t.tier == "spend" for r in t.routes}
    for route in sorted(entries):
        if not (isinstance(route, tuple) and len(route) == 2):
            problems.append(f"PLAN-SAFE MALFORMED ENTRY: {route!r}")
            continue
        if route not in estimate_routes:
            problems.append(
                "PLAN-SAFE VIOLATION: server.py whitelists {} {} for plan mode, "
                "but it is not a PLAN_ESTIMATE_ROUTES route - the only excuse for "
                "a plan-mode POST is an API whose own unconfirmed call IS the read "
                "(the estimate). SPEND_ROUTES is too broad: eight of its nine "
                "routes launch the instant they are POSTed".format(*route)
            )
        if route not in spend_routes:
            problems.append(
                "PLAN-SAFE NON-SPEND: server.py whitelists {} {} for plan mode, "
                "but it is not even a SPEND_ROUTES route".format(*route)
            )
        if route not in spend_tool_routes:
            problems.append(
                "PLAN-SAFE ORPHAN: server.py whitelists {} {} for plan mode but "
                "no spend-tier tool declares it".format(*route)
            )
    return problems


# =============================================================================
# check 11 -- #749/#750/#752: every tool ships with a title, derivable MCP
# annotations and an outputSchema, and server.py registers them
# =============================================================================
_TOOL_META_PATH = os.path.join(_ROOT, "integrations", "mcp-server", "tool_meta.py")
_ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def load_tool_meta_import_free():
    """tool_meta.py is held to the registry's own load rules (check 1): this guard
    must never need the SDK or the app to read it."""
    with open(_TOOL_META_PATH, encoding="utf-8") as fh:
        source = fh.read()
    problems = import_free_problems(source, "tool_meta.py")
    assert not problems, "tool_meta.py is not safe to load:\n  " + "\n  ".join(problems)
    spec = importlib.util.spec_from_file_location("loc_mcp_tool_meta", _TOOL_META_PATH)
    assert spec and spec.loader, f"could not load {_TOOL_META_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wire_metadata_problems(tools, meta) -> list:
    """Titles complete and exact, annotations derivable and consistent with the tier,
    exception lists naming only real tools, and an object outputSchema per tool."""
    problems = []
    names = {t.name for t in tools}
    titles = dict(meta.TITLES)
    for name in sorted(names - set(titles)):
        problems.append(f"NO TITLE: tool {name!r} has no tool_meta.TITLES entry")
    for name in sorted(set(titles) - names):
        problems.append(f"STALE TITLE: tool_meta.TITLES names {name!r}, which is not a tool")
    for name, title in sorted(titles.items()):
        if not isinstance(title, str) or not title.strip() or len(title) > 60:
            problems.append(f"BAD TITLE: {name!r} -> {title!r} (1..60 chars)")
        elif "\u2014" in title:
            problems.append(f"BAD TITLE: {name!r} uses an em dash")
    for label, members in (
        ("OPEN_WORLD_TOOLS", meta.OPEN_WORLD_TOOLS),
        ("IDEMPOTENT_WRITE_TOOLS", meta.IDEMPOTENT_WRITE_TOOLS),
        ("ADDITIVE_WRITE_TOOLS", meta.ADDITIVE_WRITE_TOOLS),
    ):
        for name in sorted(set(members) - names):
            problems.append(f"STALE {label}: {name!r} is not a tool")
    by_name = {t.name: t for t in tools}
    for name in sorted(set(meta.ADDITIVE_WRITE_TOOLS) | set(meta.IDEMPOTENT_WRITE_TOOLS)):
        if name in by_name and by_name[name].tier != "write":
            problems.append(
                f"WRONG TIER: {name!r} is listed as a write exception but is {by_name[name].tier}"
            )
    # #749 gate: destructiveHint=false means "only additive updates" (spec), so an
    # additive entry may not reach a route that updates or removes, nor offer a
    # delete-style action.
    for name in sorted(meta.ADDITIVE_WRITE_TOOLS):
        tool = by_name.get(name)
        if tool is None:
            continue
        bad = sorted(f"{m} {p}" for m, p in tool.routes if m.upper() in meta.NON_ADDITIVE_METHODS)
        if bad or meta.destructive_actions(tool):
            problems.append(
                f"NOT ADDITIVE: {name!r} is in ADDITIVE_WRITE_TOOLS but reaches "
                f"{bad + meta.destructive_actions(tool)}"
            )
    for tool in tools:
        ann = meta.annotations_for(tool)
        missing = [k for k in _ANNOTATION_KEYS if not isinstance(ann.get(k), bool)]
        if missing:
            problems.append(f"NO ANNOTATION: {tool.name} lacks boolean {missing}")
            continue
        if ann.get("title") != titles.get(tool.name):
            problems.append(f"TITLE MISMATCH: {tool.name} annotations title != TITLES")
        if ann["readOnlyHint"] is not (tool.tier == "read"):
            problems.append(
                f"READONLY LIE: {tool.name} is {tool.tier} but readOnlyHint={ann['readOnlyHint']}"
            )
        if tool.tier == "spend" and ann["destructiveHint"] is not True:
            problems.append(f"SPEND NOT DESTRUCTIVE: {tool.name}")
        if tool.tier == "write" and meta.destructive_actions(tool) and not ann["destructiveHint"]:
            problems.append(
                f"DESTRUCTIVE ACTION HIDDEN: {tool.name} offers {meta.destructive_actions(tool)} "
                "but destructiveHint is false"
            )
        if tool.tier == "read" and ann["destructiveHint"]:
            problems.append(f"READ TOOL DESTRUCTIVE: {tool.name}")
        if (
            tool.tier == "write"
            and tool.name not in meta.ADDITIVE_WRITE_TOOLS
            and not ann["destructiveHint"]
        ):
            problems.append(
                f"UNREVIEWED WRITE NOT DESTRUCTIVE: {tool.name} is not in ADDITIVE_WRITE_TOOLS"
            )
        schema = meta.output_schema_for(tool)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"NO OUTPUT SCHEMA: {tool.name} outputSchema is not an object schema")
        elif tool.tier == "spend" and "plan" not in (schema.get("properties") or {}):
            problems.append(f"PLAN NOT DESCRIBED: spend tool {tool.name} outputSchema lacks `plan`")
    return problems


def registration_metadata_problems(server_source: str) -> list:
    """server.py's register_tools must hand the SDK the title and the annotations and
    attach the outputSchema; and a tool's description must be its summary alone."""
    problems = []
    tree = ast.parse(server_source)
    reg = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register_tools"),
        None,
    )
    if reg is None:
        return ["NO register_tools in server.py"]
    calls = [
        n
        for n in ast.walk(reg)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_tool"
    ]
    if not calls:
        problems.append("NO add_tool call in register_tools")
    for call in calls:
        kws = {k.arg for k in call.keywords}
        for needed in ("title", "annotations"):
            if needed not in kws:
                problems.append(f"UNANNOTATED REGISTRATION: add_tool(...) passes no {needed}=")
    called = {getattr(n.func, "id", None) for n in ast.walk(reg) if isinstance(n, ast.Call)}
    if "_advertise_output_schema" not in called:
        problems.append("NO OUTPUT SCHEMA: register_tools never calls _advertise_output_schema")
    if "_GUIDE_POINTER" in server_source:
        problems.append("GUIDE POINTER BACK: _GUIDE_POINTER is repeated per tool again (#752)")
    return problems


def _forge_signature_drift(server_source: str, tools):
    """A copy of server.py with ONE shipped handler's parameter renamed.

    D9's material once wave 1 pays the debt: with no real divergence left, the
    only honest way to prove `server_signature_gaps` still bites is to introduce
    one. Picks the first tool that has both a handler in the file and a parameter
    to rename, so it survives tools being added or reordered."""
    for tool in tools:
        for prop in sorted((tool.input_schema or {}).get("properties") or {}):
            needle = f"async def {tool.name}({prop}"
            if needle in server_source:
                return (
                    server_source.replace(needle, f"async def {tool.name}(zz_{prop}", 1),
                    f"{tool.name}({prop} -> zz_{prop})",
                )
    raise AssertionError(
        "no handler signature found to forge a drift against -- either server.py "
        "stopped defining tools as top-level functions (in which case the AST "
        "check is now vacuous and must be rewritten) or the naming changed"
    )


def exempt_map_with(registry, key, value):
    """registry.EXEMPT with one entry replaced -- the forge for the A/D proofs."""
    forged = dict(registry.EXEMPT)
    assert key in forged, f"forge target {key} is not exempt"
    forged[key] = value
    return forged


# =============================================================================
def main() -> None:
    import dataclasses

    # --- check 1 (FIRST -- before `app` exists in this process) -------------
    registry = load_registry_import_free()

    from app.main import app
    from app.services import settings
    from tests import _routes

    problems = _routes.mount_problems(app)
    assert not problems, "the route walk has a blindspot:\n  " + "\n  ".join(problems)

    real = _routes.real_api_routes(app)
    covered = registry.covered_routes()
    exempt = registry.exempt_routes()
    permanent = registry.permanent_exemptions()
    planned = registry.planned_exemptions()

    assert len(real) >= 150, (
        "only %d /api routes walked off the router -- the walk stopped reading "  # noqa: UP031
        "the route table" % len(real)
    )

    # --- checks 2 + 3 -------------------------------------------------------
    problems = route_problems(real, covered)
    assert not problems, (
        "the registry points at routes this build does not serve:\n  " + "\n  ".join(problems)
    )
    print(
        "  [2,3] all %d routes claimed by %d tools exist in the router, "  # noqa: UP031
        "method-for-method OK" % (len(covered), len(registry.TOOLS))
    )

    # --- check 4 (THE load-bearing one) -------------------------------------
    problems = accounting_problems(real, covered, exempt)
    assert not problems, "the MCP accounting and the app disagree:\n  " + "\n  ".join(problems)
    print(
        "  [4] accounting closes: %d covered + %d exempt (%d permanent + %d "  # noqa: UP031
        "planned) == %d served routes, disjoint, 0 unaccounted, 0 stale OK"
        % (len(covered), len(exempt), len(permanent), len(planned), len(real))
    )

    # --- check 5 ------------------------------------------------------------
    with open(_SERVER_PATH, encoding="utf-8") as fh:
        server_src = fh.read()
    honoured = server_literal_collection(server_src, "_HONOURED_SCHEMA_KEYWORDS")
    problems = schema_problems(registry.TOOLS)
    problems += forbidden_property_problems(registry.TOOLS)
    problems += schema_keyword_problems(registry.TOOLS, honoured)
    problems += freeform_object_problems(registry.TOOLS, server_src)
    assert not problems, "input schemas are malformed:\n  " + "\n  ".join(problems)
    print(
        "  [5] all %d input schemas are well-formed objects (required subset of "  # noqa: UP031
        "properties; every property typed and described); none offers a "
        "forbidden flag (%s); every keyword used is one the derivation enforces "
        "(%s); all %d free-form object properties carry a declared policy, and "
        "no forwarding allowlist contains a silent-retirement field OK"
        % (
            len(registry.TOOLS),
            "; ".join(
                f"{t}.{p}" for t, ps in sorted(_FORBIDDEN_SCHEMA_PROPS.items()) for p in sorted(ps)
            ),
            " ".join(sorted(honoured or ())),
            len(_FREEFORM_OBJECT_POLICY),
        )
    )

    # --- check 6 ------------------------------------------------------------
    problems = coverage_policy_problems(
        registry.TOOLS, registry.NEVER_COVER, registry.SPEND_ROUTES, registry.MUTATING_TIERS
    )
    problems += never_cover_completeness_problems(real, registry.NEVER_COVER)
    problems += plan_estimate_rot_problems(
        registry.PLAN_ESTIMATE_ROUTES, registry.SPEND_ROUTES, real
    )
    problems += plan_safe_problems(
        server_src, registry.SPEND_ROUTES, registry.TOOLS, registry.PLAN_ESTIMATE_ROUTES
    )
    # F1 (adversarial review): connector_translate's server.py dispatch map is a
    # second source of truth for the routes it POSTs to. Pin it to the registry
    # route-list so a dict entry cannot reach a NEVER_COVER route the guard never
    # sees. Read import-free off the AST, like every other server.py claim here.
    problems += connector_translate_dict_problems(server_src, registry.TOOLS)
    # M-HIGH-1 (AUDIT 2026-08-18): coverage_policy_problems reasons only over the
    # DECLARED route-list, so a handler that builds a NEVER_COVER path at RUNTIME
    # out of a model-authored value (provider="../../customers/1/archive?") reaches
    # a route no registry check sees. Demand every %s spliced into an /api path
    # literal be _seg()-quoted or a reviewed exception -- read import-free off the AST.
    problems += path_splice_problems(server_src)
    # SOUNDNESS of the R4 connector carve-out. never_cover_completeness_problems
    # skips these routes when checking the connector family; that is only safe if
    # each is genuinely toolified. Assert it here, where covered/never_cover are in
    # hand: every carved-out route must be COVERED by a tool (else it is unaccounted)
    # and must NOT be in NEVER_COVER (else a tool covering it is a check-6 violation).
    # A route that stopped being served is caught by the fixture-staleness half above.
    uncovered_toolified = sorted(_TOOLIFIED_CONNECTOR_ROUTES - covered)
    assert not uncovered_toolified, (
        "the R4 connector carve-out names routes NO tool covers, so they are carved "
        f"out of NEVER_COVER completeness AND unaccounted: {uncovered_toolified} -- either toolify them "  # noqa: E501
        "or drop them from _TOOLIFIED_CONNECTOR_ROUTES"
    )
    contradiction = sorted(_TOOLIFIED_CONNECTOR_ROUTES & registry.NEVER_COVER)
    assert not contradiction, (
        f"the R4 connector carve-out names routes STILL in NEVER_COVER: {contradiction} -- a route "
        "cannot be both toolified and never-cover; remove them from NEVER_COVER"
    )
    assert not problems, (
        "a tool covers a route it may not, or under a tier it may not:\n  " + "\n  ".join(problems)
    )
    print(
        "  [6] destructive tier EMPTY; no tool touches any of the %d NEVER_COVER "  # noqa: UP031
        "routes (re-derived off the router: every DELETE/purge/invalidate, the "
        "two semantic-destructive verbs, and every connector write EXCEPT the %d "
        "R4-toolified routes -- each of which is proven COVERED and out of "
        "NEVER_COVER); non-GET forces a mutating tier; every billing route sits on "
        "a spend tool; the plan-mode POST whitelist holds only the %d "
        "PLAN_ESTIMATE_ROUTES entry(ies) - not just any SPEND_ROUTES route - on "
        "spend tools OK"
        % (
            len(registry.NEVER_COVER),
            len(_TOOLIFIED_CONNECTOR_ROUTES),
            len(registry.PLAN_ESTIMATE_ROUTES),
        )
    )
    print(
        "  [6+] the credential-suffix rule pins every /secret|/oauth-secret|"  # noqa: UP031
        "/reset-password route into NEVER_COVER; every free-text value reaching an /api "
        "path in server.py -- whether at a `%%`-literal or an f-string interpolation "
        "(%d reviewed exceptions across both) or via a table-held template (%d) -- is "
        "_seg()-quoted or int-safe, so M-HIGH-1/2/3 cannot recur even through the "
        "_CONFIGURE_ACTIONS table pattern"
        % (len(_APPROVED_PATH_SPLICES), len(_APPROVED_TABLE_SPLICES))
    )

    # --- check 7 ------------------------------------------------------------
    problems = confirm_problems(registry.TOOLS)
    assert not problems, "the spend contract is broken:\n  " + "\n  ".join(problems)
    spend = [t.name for t in registry.TOOLS if t.tier == "spend"]
    print(
        "  [7] all %d spend tool(s) carry a boolean `confirm` (%s) OK"  # noqa: UP031
        % (len(spend), ", ".join(spend) or "none")
    )

    # --- check 8 ------------------------------------------------------------
    problems = exempt_shape_problems(registry, registry.EXEMPT, set(registry.CATEGORIES))
    assert not problems, "exemptions are malformed:\n  " + "\n  ".join(problems)
    print(
        "  [8] all %d exemptions carry a real category and a >=%d-char reason, "  # noqa: UP031
        "and the %d pinned wave-inherited constraints are intact OK"
        % (len(exempt), MIN_REASON_CHARS, len(_EXEMPT_REASON_PINS))
    )

    # --- check 9 ------------------------------------------------------------
    with open(_SPEC_PATH, encoding="utf-8") as fh:
        spec_text = fh.read()
    names = spec_tool_names(spec_text)
    assert len(names) >= MIN_SPEC_TOOLS, (
        "only %d tool names parsed out of docs/specs/mcp-server.md's scope table; the "  # noqa: UP031
        "spec plans %d -- either the plan shrank or the extractor stopped reading"
        % (len(names), MIN_SPEC_TOOLS)
    )
    for tool in registry.TOOLS:
        assert tool.name in names, f"shipped tool {tool.name!r} is not in the spec's scope table"
    problems = planned_problems(registry, registry.EXEMPT, names)
    assert not problems, "a planned marker does not resolve:\n  " + "\n  ".join(problems)
    print(
        "  [9] all %d `planned:` markers name tools in the spec's %d-tool scope "  # noqa: UP031
        "table (case-insensitive, no malformed markers; all %d shipped tools "
        "are in it too) OK" % (len(planned), len(names), len(registry.TOOLS))
    )

    # --- check 10 -----------------------------------------------------------
    scopes = app_scope_vocabulary()
    assert scopes == set(registry.SCOPES), (
        f"the registry's scope vocabulary {sorted(registry.SCOPES)} does not match the grants the app enforces {sorted(scopes)}"  # noqa: E501
    )
    problems = field_problems(
        registry.TOOLS, set(registry.FAMILIES), set(registry.TIERS), scopes, set(settings.DEFAULTS)
    )
    problems += description_problems(registry.TOOLS)
    with open(_SERVER_PATH, encoding="utf-8") as fh:
        server_source = fh.read()
    gaps = server_signature_gaps(server_source, registry.TOOLS)
    problems += debt_problems(gaps, registry.SERVER_PY_WAVE1_DEBT)
    # manage_install's client-side host-only refusal (_INSTALL_LOCKED_KEYS) must
    # mirror settings.REMOTE_IMMUTABLE_KEYS exactly -- a rotted subset was a
    # defense-in-depth gap (Train Q1 round-2 gate, 2026-08-24). Read import-free
    # off the AST, like every server.py claim here.
    problems += install_locked_parity_problems(server_source, settings.REMOTE_IMMUTABLE_KEYS)
    assert not problems, (
        "registry fields, descriptions or the debt list are wrong:\n  " + "\n  ".join(problems)
    )
    pinned = sum(len(t.pins) for t in registry.TOOLS)
    print(
        "  [10] vocabularies real (scopes re-derived from every gate in app/web: "  # noqa: UP031
        "%s); %d description pins verbatim incl. every SPEC section-9 pin; no "
        "summary promises a foreign route; %d server.py signature divergence(s), "
        "each recorded in the debt list; manage_install's _INSTALL_LOCKED_KEYS "
        "mirrors all %d REMOTE_IMMUTABLE_KEYS exactly OK"
        % (" ".join(sorted(scopes)), pinned, len(gaps), len(settings.REMOTE_IMMUTABLE_KEYS))
    )

    # --- check 11 (#749/#750/#752) ----------------------------------------
    tool_meta = load_tool_meta_import_free()
    problems = wire_metadata_problems(registry.TOOLS, tool_meta)
    problems += registration_metadata_problems(server_source)
    assert not problems, "MCP wire metadata is incomplete:\n  " + "\n  ".join(problems)
    destructive = sorted(
        t.name for t in registry.TOOLS if tool_meta.annotations_for(t)["destructiveHint"]
    )
    print(
        "  [11] all %d tools carry a title, boolean annotations consistent with their tier "  # noqa: UP031
        "(%d destructive, %d open-world) and an object outputSchema; tool_meta.py is "
        "import-free; register_tools passes title + annotations and attaches the schema"
        % (
            len(registry.TOOLS),
            len(destructive),
            sum(1 for t in registry.TOOLS if tool_meta.annotations_for(t)["openWorldHint"]),
        )
    )
    _wire_metadata_mutation_proofs(registry, tool_meta, server_source)

    # =========================================================================
    # THE RATCHET. These move ONE WAY only. Planned exemptions convert into
    # tools, so they may only shrink; covered routes accumulate, so they may only
    # grow; wave-1 debt is paid off, so it may only shrink; pins are deleted, not
    # outgrown. Editing a number here is the reviewable act -- a claim that the
    # surface moved, in a diff someone reads. Without it, coverage can quietly
    # regress to nothing while every check above stays green, because every check
    # above is equally satisfied by a registry with fewer tools and more
    # exemptions.
    # =========================================================================
    assert len(planned) <= MAX_PLANNED, (
        "planned exemptions grew to %d (ratchet: %d) -- planned entries convert "  # noqa: UP031
        "INTO tools; a new one is new debt and needs the number raised on purpose"
        % (len(planned), MAX_PLANNED)
    )
    assert len(covered) >= MIN_COVERED, (
        "covered routes fell to %d (ratchet: %d) -- the MCP surface may not shrink "  # noqa: UP031
        "silently" % (len(covered), MIN_COVERED)
    )
    assert len(registry.SERVER_PY_WAVE1_DEBT) <= MAX_DEBT, (
        "the wave-1 debt list grew to %d entries (ratchet: %d)"  # noqa: UP031
        % (len(registry.SERVER_PY_WAVE1_DEBT), MAX_DEBT)
    )
    assert pinned >= MIN_TOTAL_PINS, (
        "description pins fell to %d (ratchet: %d) -- pins are deleted, not "  # noqa: UP031
        "outgrown" % (pinned, MIN_TOTAL_PINS)
    )
    print(
        "  [ratchet] planned %d <= %d, covered %d >= %d, debt %d <= %d, pins "  # noqa: UP031
        "%d >= %d OK"
        % (
            len(planned),
            MAX_PLANNED,
            len(covered),
            MIN_COVERED,
            len(registry.SERVER_PY_WAVE1_DEBT),
            MAX_DEBT,
            pinned,
            MIN_TOTAL_PINS,
        )
    )

    _mutation_proofs(registry, real, covered, exempt, names, dataclasses)

    for line in registry.SERVER_PY_WAVE1_DEBT:
        print(f"  NOTE (wave 1 debt): {line}")

    # Open core: no tool of a paid add-on ships in this tree (its routes are not served either,
    # which the NO-SUCH-ROUTE check above already enforces for every declared route).
    paid_prefixes = (
        "/api/memories",
        "/api/memory-suggestions",
        "/api/evaluations",
        "/api/advanced",
        "/api/admin/databricks",
        "/api/admin/semantic",
    )
    leaked = sorted(
        t.name for t in registry.TOOLS if any(p.startswith(paid_prefixes) for _m, p in t.routes)
    )
    assert not leaked, f"paid add-on tool(s) shipped in the open core: {leaked}"
    assert "memory" not in registry.FAMILIES and "evaluations" not in registry.FAMILIES
    print(
        f"  no paid add-on tool or family ships in the open core ({len(registry.TOOLS)} tools) OK"
    )
    print("MCP PARITY OK")


# =============================================================================
# MUTATION PROOFS -- a guard nobody has tried to break is a guard nobody knows
# works. M1-M5 are the spec's own five; A/B/C/D are the adversarial review's, and
# every one of those was GREEN against the first version of this file.
# =============================================================================
def _mutation_proofs(registry, real, covered, exempt, names, dataclasses):
    invented = ("POST", "/api/projects/{pid}/frobnicate")
    wrong_verb = ("DELETE", "/api/projects")
    assert invented not in real and wrong_verb not in real, "pick other fixtures"
    tools = list(registry.TOOLS)
    a_read_tool = next(t for t in tools if t.tier == "read")
    a_spend_tool = next(t for t in tools if t.tier == "spend")
    resume = ("POST", "/api/runs/{run_id}/resume")
    caught = []

    def red(label, problems, needle=None):
        assert problems, f"{label}: the guard stays GREEN, so it proves nothing"
        if needle:
            assert any(needle in p for p in problems), (
                f"{label}: expected {needle!r} in {problems[:3]}"
            )
        caught.append(label)

    # --- M1/M2: a route a tool may not claim -------------------------------
    red("M1 invented tool route", route_problems(real, covered | {invented}), "NO SUCH ROUTE")
    red("M2 real path, wrong verb", route_problems(real, covered | {wrong_verb}), "WRONG METHOD")

    # --- M3: the load-bearing one, three ways ------------------------------
    red(
        "M3a EXEMPT line deleted",
        accounting_problems(real, covered, exempt - {sorted(exempt)[0]}),
        "UNACCOUNTED",
    )
    red(
        "M3b route dropped from a tool",
        accounting_problems(real, covered - {sorted(covered)[0]}, exempt),
        "UNACCOUNTED",
    )
    m3c = accounting_problems(real | {invented}, covered, exempt)
    red("M3c a brand-new /api route with no accounting", m3c, "UNACCOUNTED")
    assert any("frobnicate" in p for p in m3c), "M3c named the wrong route"

    # --- M4: an exemption that outlived its route --------------------------
    red(
        "M4 stale exemption",
        accounting_problems(real, covered, exempt | {invented}),
        "STALE EXEMPTION",
    )

    # --- M5: descriptions ---------------------------------------------------
    pinned_tool = next(t for t in tools if t.pins)
    red(
        "M5 pin deleted from a summary",
        description_problems(
            [
                dataclasses.replace(
                    pinned_tool, summary=pinned_tool.summary.replace(pinned_tool.pins[0], "")
                )
            ]
        ),
        "LOST PIN",
    )
    red(
        "M5b summary promises a foreign route",
        description_problems(
            [dataclasses.replace(pinned_tool, summary=pinned_tool.summary + " Calls /api/apikeys.")]
        ),
        "FOREIGN ROUTE",
    )

    # --- A1/A2: justifications that are not justifications -----------------
    cats = set(registry.CATEGORIES)
    jobs_run = ("POST", "/api/jobs/run")
    for label, forged in (
        ("A1 an exemption justified by the empty string", ("generic-executor", "")),
        ("A2 an exemption justified by 'nah'", ("generic-executor", "nah")),
    ):
        red(
            label,
            exempt_shape_problems(registry, exempt_map_with(registry, jobs_run, forged), cats),
            "placeholder",
        )
    red(
        "A2b an invented exemption category",
        exempt_shape_problems(
            registry, exempt_map_with(registry, jobs_run, ("because-i-said-so", "x" * 60)), cats
        ),
        "is not one of",
    )

    # --- A3/D6: markers that do not parse ----------------------------------
    # Wave 2 converted the LAST planned marker, so there is no live one to
    # forge against any more - the D9 lesson again: a check whose subject went
    # away is re-proved against a synthetic subject, not quietly deleted. Any
    # permanent exemption key works as the vehicle, because the forgery is in
    # the VALUE.
    assert not registry.planned_exemptions(), (
        "planned markers exist again - retarget these proofs at a live one"
    )
    account = jobs_run
    # A3's damage was CLASSIFICATION: "Planned:" with a capital P did not parse,
    # so the route was silently counted as a PERMANENT exemption and check 9
    # never looked at it again. Two proofs -- that the parse now sees it, and
    # that writing it under a permanent category is caught.
    assert registry.planned_tool(("planned", "Planned:whoami - one keystroke.")) == "whoami", (
        "A3: a capital-P marker still falls through to permanent"
    )
    caught.append("A3 'Planned:' with a capital P is no longer read as permanent")
    red(
        "A3b a planned marker filed under a permanent category",
        exempt_shape_problems(
            registry,
            exempt_map_with(
                registry,
                account,
                ("operator-admin", "Planned:whoami - wave 1, filed as permanent."),
            ),
            cats,
        ),
        "is a planned marker but category is",
    )
    red(
        "D6 'planned:' with no tool name after it",
        planned_problems(
            registry,
            exempt_map_with(registry, account, ("planned", registry.PLANNED_PREFIX)),
            names,
        ),
        "MALFORMED PLAN MARKER",
    )
    red(
        "A3c a marker naming a tool the spec does not plan",
        planned_problems(
            registry,
            exempt_map_with(
                registry, account, ("planned", "planned:frobnicate - an invented tool.")
            ),
            names,
        ),
        "PHANTOM PLAN",
    )

    # --- A4/A5/A6: covering what may never be covered ----------------------
    # Relabelling a perimeter exemption keeps the accounting closed, so the relabel
    # alone is harmless; it is only useful as a prelude to COVERING the route, and
    # that is the step that must go red.
    red(
        "A4/A5 a read tool swallowing the human-review resume",
        coverage_policy_problems(
            [dataclasses.replace(a_read_tool, routes=a_read_tool.routes + (resume,))],  # noqa: RUF005
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "NEVER_COVER VIOLATION",
    )
    red(
        "A6 routes growing to absorb apikeys + a TM wipe + a memory purge",
        coverage_policy_problems(
            [
                dataclasses.replace(
                    a_read_tool,
                    routes=a_read_tool.routes  # noqa: RUF005
                    + (
                        ("GET", "/api/apikeys"),
                        ("POST", "/api/tm/bulk-delete"),
                        ("POST", "/api/memories/{mid}/purge"),
                    ),
                )
            ],
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "NEVER_COVER VIOLATION",
    )
    red(
        "A6b NEVER_COVER itself going stale",
        never_cover_completeness_problems(
            real, registry.NEVER_COVER - {("POST", "/api/tm/bulk-delete")}
        ),
        "NEVER_COVER IS INCOMPLETE",
    )
    # A6c/A6d: the SEMANTIC-destructive verbs the regex cannot see. Dropping one
    # from NEVER_COVER must go red (it would otherwise slip the completeness
    # check, which was the whole gap), and a tool swallowing one must go red too.
    red(
        "A6c a semantic-destructive route dropped from NEVER_COVER",
        never_cover_completeness_problems(
            real, registry.NEVER_COVER - {("POST", "/api/customers/{cid}/archive")}
        ),
        "NEVER_COVER IS INCOMPLETE",
    )
    red(
        "A6d a read tool swallowing tm scrub-hedges",
        coverage_policy_problems(
            [
                dataclasses.replace(
                    a_read_tool,
                    routes=a_read_tool.routes + (("POST", "/api/tm/scrub-hedges"),),  # noqa: RUF005
                )
            ],
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "NEVER_COVER VIOLATION",
    )

    # --- M-HIGH-1/2/3 (AUDIT 2026-08-18, MCP security train) ---------------
    with open(_SERVER_PATH, encoding="utf-8") as fh:
        splice_src = fh.read()
    # M-HIGH-1: the static path-splice guard. The real server.py must be clean (the
    # _seg() fix landed), and re-introducing the bug -- str(provider) back on the
    # manage_install f-string splice -- must go red. The forge targets the FIXED site by
    # its exact text, so it also fails loudly if the fix is ever reverted or reworded.
    # (manage_install is an f-string since the UP031 modernisation, so this exercises the
    # guard's JoinedStr surface end-to-end; the synthetic M-HIGH-1e proof below pins that
    # surface independently of where the real site sits.)
    assert path_splice_problems(splice_src) == [], (
        "the real server.py has an unquoted {} splice into an /api path -- the "
        "M-HIGH-1 fix regressed: {}".format(*path_splice_problems(splice_src)[:1])
    )
    forged_splice = splice_src.replace(
        'f"/api/admin/providers/{_seg(provider)}/key/clear"',
        'f"/api/admin/providers/{str(provider)}/key/clear"',
        1,
    )
    assert forged_splice != splice_src, (
        "M-HIGH-1 forge did not find the fixed manage_install splice -- the "
        "regression proof has no subject (the fix moved or was reworded)"
    )
    red(
        "M-HIGH-1 a model-authored value spliced into an /api path unquoted",
        path_splice_problems(forged_splice),
        "UNQUOTED PATH SPLICE",
    )
    # M-HIGH-1e: the JoinedStr (f-string) surface, proven on SYNTHETIC source so the
    # proof survives any move of the real converted sites. A bare free-text value
    # interpolated into an /api f-string path is the same M-HIGH-1 class and must be red;
    # the _seg()-quoted form and the int()-coerced form (the %d analogue) of the same path
    # must stay green -- the surface flags free text, not the f-string shape. Empty
    # approved maps so the whole-file staleness ratchet does not fire on the tiny source.
    red(
        "M-HIGH-1e a bare free-text value interpolated into an /api f-string path",
        path_splice_problems('x = f"/api/widgets/{name}/frob"\n', approved={}, approved_tables={}),
        "UNQUOTED PATH SPLICE",
    )
    for _label, _safe in (
        ("_seg()-quoted", 'x = f"/api/widgets/{_seg(name)}/frob"\n'),
        ("int()-coerced (the %d analogue)", 'x = f"/api/widgets/{int(name)}/frob"\n'),
    ):
        assert path_splice_problems(_safe, approved={}, approved_tables={}) == [], (
            f"M-HIGH-1e': a {_label} f-string /api path must NOT be flagged -- the "
            f"JoinedStr surface over-reaches: {path_splice_problems(_safe, approved={}, approved_tables={})[:1]}"  # noqa: E501
        )
    caught.append(
        "M-HIGH-1e' _seg()/int() f-string /api paths are not flagged "
        "(the JoinedStr surface flags free text, not the f-string form)"
    )
    # M-HIGH-1f: the MIXED form -- an /api f-string whose interpolations are all safe but
    # whose LITERAL text still carries a `%s`, fed from a `% value` use site. The BinOp
    # surface skips it (its left operand is a JoinedStr, not a Constant) and the JoinedStr
    # surface only classifies interpolations, so before this rule the exact path that is
    # RED as a pure `%`-literal was GREEN once half of it became an f-string -- a real
    # `../..` smuggle (probe: f"/api/a/{_seg(p)}/%s" % "../../admin/users/1/promote").
    red(
        "M-HIGH-1f a `%s` left in the literal text of an /api f-string path",
        path_splice_problems(
            'x = f"/api/widgets/{_seg(a)}/%s" % evil\n', approved={}, approved_tables={}
        ),
        "MIXED-FORM PATH TEMPLATE",
    )
    _clean_mixed = 'x = f"/api/widgets/{_seg(a)}/frob"\n'
    assert path_splice_problems(_clean_mixed, approved={}, approved_tables={}) == [], (
        "M-HIGH-1f': a %-free f-string /api path must NOT be flagged as mixed-form: "
        f"{path_splice_problems(_clean_mixed, approved={}, approved_tables={})[:1]}"
    )
    caught.append(
        "M-HIGH-1f' a %-free f-string /api path is not flagged as mixed-form "
        "(the rule fires on a stray literal conversion, not on f-strings)"
    )
    # ...and the staleness half: an approved non-_seg exception that matches no
    # real splice must be pruned, not left to pre-approve a future one.
    red(
        "M-HIGH-1b a stale path-splice exception",
        path_splice_problems(
            splice_src,
            approved={**_APPROVED_PATH_SPLICES, ("/api/frobnicate/%s", "whatever"): "x" * 40},
        ),
        "STALE PATH-SPLICE EXCEPTION",
    )
    # M-HIGH-1c (gate MED): the TABLE surface. A free-text %s smuggled through the
    # _CONFIGURE_ACTIONS pattern -- a path template held in a dict/tuple and formatted
    # elsewhere as `tpl % X` -- is invisible to the BinOp scan but must go red, or the
    # 'un-reintroducible' claim is false. Anchor on the real first _CONFIGURE_ACTIONS
    # row so the forge fails loudly if that table is renamed or restructured.
    anchor = '"rename": ("/api/projects/%d/rename", "name", lambda v: {"name": v}),'
    assert anchor in splice_src, (
        "M-HIGH-1c anchor not found -- _CONFIGURE_ACTIONS changed shape; retarget the "
        "table-splice forge so it keeps proving the guard non-vacuous"
    )
    forged_table = splice_src.replace(
        anchor,
        anchor + '\n    "tag": ("/api/projects/%d/tag/%s", "tag_name", lambda v: {"tag_name": v}),',
        1,
    )
    assert forged_table != splice_src, "M-HIGH-1c table forge did not apply"
    red(
        "M-HIGH-1c a free-text %s path template smuggled via a table "
        "(the _CONFIGURE_ACTIONS pattern)",
        path_splice_problems(forged_table),
        "PATH-TEMPLATE TABLE SPLICE",
    )
    # ...and the int-safe (%d) form of the SAME injection stays GREEN -- the guard
    # flags free text, not the table pattern itself (no false positive).
    green_table = splice_src.replace(
        anchor,
        anchor + '\n    "tag": ("/api/projects/%d/tag/%d", "tag_id", lambda v: {"tag_id": v}),',
        1,
    )
    assert path_splice_problems(green_table) == [], (
        "M-HIGH-1c': an int-only (%d) table template must NOT be flagged -- the guard "  # noqa: UP031
        "over-reaches: %s" % (path_splice_problems(green_table)[:1])
    )
    caught.append(
        "M-HIGH-1c' an int-only table template is not flagged "
        "(the table surface flags free text, not the pattern)"
    )
    # ...and the table-exception staleness half goes red on an unmatched entry.
    red(
        "M-HIGH-1d a stale table-splice exception",
        path_splice_problems(splice_src, approved_tables={"/api/frobnicate/%s": "x" * 40}),
        "STALE TABLE-SPLICE EXCEPTION",
    )
    # M-HIGH-2/3: the credential-suffix completeness rule. Both routes are in
    # NEVER_COVER now; dropping either must go red via the mechanical suffix rule,
    # so a future edit cannot quietly un-list one the way its siblings shielded it.
    reset_pw = ("POST", "/api/users/{uid}/reset-password")
    vendor_secret = ("POST", "/api/vendors/{vid}/secret")
    assert reset_pw in registry.NEVER_COVER and vendor_secret in registry.NEVER_COVER, (
        "M-HIGH-2/3 fixture: both credential routes must be in NEVER_COVER to matter"
    )
    assert reset_pw in real and vendor_secret in real, (
        "M-HIGH-2/3 fixture: both credential routes must still be served to matter"
    )
    red(
        "M-HIGH-2 reset-password dropped from NEVER_COVER",
        never_cover_completeness_problems(real, registry.NEVER_COVER - {reset_pw}),
        "NEVER_COVER IS INCOMPLETE",
    )
    red(
        "M-HIGH-3 a vendor secret-set dropped from NEVER_COVER",
        never_cover_completeness_problems(real, registry.NEVER_COVER - {vendor_secret}),
        "NEVER_COVER IS INCOMPLETE",
    )
    # ...and a tool that tries to COVER either must go red (they are NEVER_COVER).
    red(
        "M-HIGH-2b a tool swallowing reset-password",
        coverage_policy_problems(
            [dataclasses.replace(a_read_tool, routes=a_read_tool.routes + (reset_pw,))],  # noqa: RUF005
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "NEVER_COVER VIOLATION",
    )

    # --- R4: the connector NEVER_COVER amendment ---------------------------
    # SPEC-full-remote-parity.md's Wave R4 moved pull/push/sync/retry + every
    # {kind}/translate route OUT of NEVER_COVER and onto tools, carved out of the
    # connector-family completeness rule by _TOOLIFIED_CONNECTOR_ROUTES. Four proofs
    # that the amendment did NOT open the perimeter beyond exactly those routes.
    airtable_records = ("POST", "/api/connectors/{cid}/airtable/records")
    jira_translate = ("POST", "/api/connectors/{cid}/jira/translate")
    assert airtable_records in registry.NEVER_COVER, (
        "R4 fixture: airtable/records must STILL be NEVER_COVER to matter"
    )
    assert jira_translate in _TOOLIFIED_CONNECTOR_ROUTES, (
        "R4 fixture: jira/translate must be a toolified route to matter"
    )
    # R4a: a STILL-locked connector write (Airtable record create -- the destructive
    # verb the maintainer's decision record keeps out) dropped from NEVER_COVER must go red;
    # the carve-out does NOT cover it, so the connector-family rule still requires it.
    red(
        "R4a a locked connector write (airtable/records) dropped from NEVER_COVER",
        never_cover_completeness_problems(real, registry.NEVER_COVER - {airtable_records}),
        "NEVER_COVER IS INCOMPLETE",
    )
    # R4b: a tool COVERING a still-locked connector route must go red -- the amendment
    # freed the translate/push/sync/retry routes, not the Airtable record family.
    red(
        "R4b a tool covering the still-locked airtable/records",
        coverage_policy_problems(
            [dataclasses.replace(a_spend_tool, routes=a_spend_tool.routes + (airtable_records,))],  # noqa: RUF005
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "NEVER_COVER VIOLATION",
    )
    # R4c: the carve-out going STALE (naming a route no longer served) must go red,
    # so it cannot pre-approve whatever route next takes that path.
    red(
        "R4c the toolified carve-out naming a route the app does not serve",
        never_cover_completeness_problems(
            real, registry.NEVER_COVER, toolified=_TOOLIFIED_CONNECTOR_ROUTES | {invented}
        ),
        "TOOLIFIED-CONNECTOR FIXTURE STALE",
    )
    # R4d: THE load-bearing one -- removing a route from the carve-out must RE-LOCK
    # it. With an EMPTY carve-out the freed translate/push/sync/retry routes are no
    # longer in NEVER_COVER and no longer excused, so the connector-family rule flags
    # them incomplete. This proves the carve-out is exactly what lets them out.
    r4d = never_cover_completeness_problems(real, registry.NEVER_COVER, toolified=frozenset())
    red(
        "R4d an emptied toolified carve-out re-requires the freed connector routes in NEVER_COVER",
        r4d,
        "NEVER_COVER IS INCOMPLETE",
    )
    assert any("jira/translate" in p for p in r4d), (
        "R4d did not re-flag the freed jira/translate route"
    )
    # R4e: soundness -- a carve-out naming a route NO tool covers is caught by the
    # main() subset assertion, not by completeness (completeness would silently
    # excuse it). Prove the gap the assertion closes: completeness stays GREEN on
    # such a forged carve-out, so the subset check is the only thing standing.
    bogus_conn = ("POST", "/api/connectors/{cid}/airtable/webhook/register")
    assert bogus_conn in registry.NEVER_COVER and bogus_conn not in covered, (
        "R4e fixture: pick a connector route that is NEVER_COVER and uncovered"
    )
    assert (
        never_cover_completeness_problems(
            real,
            registry.NEVER_COVER - {bogus_conn},
            toolified=_TOOLIFIED_CONNECTOR_ROUTES | {bogus_conn},
        )
        == []
    ), (
        "R4e: completeness should GREEN-excuse a carved-out route (that is why the "
        "main() subset+disjoint assertion exists to catch an uncovered carve-out)"
    )
    assert bogus_conn not in covered, (
        f"R4e: the main() assertion 'toolified <= covered' is what red-flags {bogus_conn} "
        "being carved out without a tool"
    )
    caught.append(
        "R4e a carve-out route with no covering tool is caught by the "
        "main() subset assertion (completeness alone would excuse it)"
    )
    # R4f (adversarial review F1): server.py's _CONNECTOR_TRANSLATE_ROUTES is a
    # SECOND source of truth for the routes connector_translate POSTs to. A dict
    # entry pointing at a NEVER_COVER route the registry route-list does NOT declare
    # (e.g. airtable/records) must go red -- otherwise the handler reaches a
    # destructive route every registry-only check stays blind to.
    with open(_SERVER_PATH, encoding="utf-8") as fh:
        _srv = fh.read()
    assert connector_translate_dict_problems(_srv, tools) == [], (
        "R4f: the real _CONNECTOR_TRANSLATE_ROUTES already drifts from its registry routes"
    )
    forged_dict = _srv.replace(
        '"jira": ("jira/translate", "issue_keys"),',
        '"jira": ("jira/translate", "issue_keys"),\n'
        '    "airtable": ("airtable/records", "record_ids"),',
        1,
    )
    assert forged_dict != _srv, "R4f forge did not find the dict entry to drift"
    assert ("POST", "/api/connectors/{cid}/airtable/records") in registry.NEVER_COVER, (
        "R4f fixture: airtable/records must be NEVER_COVER for the drift to matter"
    )
    red(
        "R4f a _CONNECTOR_TRANSLATE_ROUTES entry reaching a NEVER_COVER route the "
        "registry does not declare",
        connector_translate_dict_problems(forged_dict, tools),
        "CONNECTOR-TRANSLATE DICT DRIFT",
    )
    red(
        "R4f' the dispatch map losing a route the registry still declares",
        connector_translate_dict_problems(
            _srv.replace('"jira": ("jira/translate", "issue_keys"),\n', "", 1), tools
        ),
        "CONNECTOR-TRANSLATE DICT GAP",
    )

    # --- Q1 (Train Q1 round-2 gate, 2026-08-24): _INSTALL_LOCKED_KEYS parity ---
    # server.py's client-side host-only refusal must mirror REMOTE_IMMUTABLE_KEYS
    # exactly. The real file is proven GREEN in main(); here we prove the guard is
    # non-vacuous in BOTH drift directions off synthetic sources, and that it fails
    # loudly if the frozenset is renamed away.
    from app.services import settings as _settings

    _remote = set(_settings.REMOTE_IMMUTABLE_KEYS)
    assert len(_remote) >= 2, "Q1 fixture: REMOTE_IMMUTABLE_KEYS must have >=2 keys to drift"
    _some = sorted(_remote)[0]
    red(
        "Q1 a host-only key missing from _INSTALL_LOCKED_KEYS (the exact rot that was found)",
        install_locked_parity_problems(
            "_INSTALL_LOCKED_KEYS = frozenset({" + repr(_some) + "})\n", _remote
        ),
        "INSTALL-LOCKED GAP",
    )
    red(
        "Q1b _INSTALL_LOCKED_KEYS over-blocking a key /api would accept",
        install_locked_parity_problems(
            "_INSTALL_LOCKED_KEYS = frozenset({'not_a_real_setting_key'})\n", {_some}
        ),
        "INSTALL-LOCKED EXTRA",
    )
    red(
        "Q1c the frozenset renamed away entirely",
        install_locked_parity_problems("_SOMETHING_ELSE = frozenset()\n", _remote),
        "could not read _INSTALL_LOCKED_KEYS",
    )
    _mirror_src = "_INSTALL_LOCKED_KEYS = frozenset(" + repr(sorted(_remote)) + ")\n"
    assert install_locked_parity_problems(_mirror_src, _remote) == [], (
        "Q1': an exact mirror must stay GREEN -- the guard over-reaches"
    )
    caught.append("Q1' an exact _INSTALL_LOCKED_KEYS mirror is not flagged (no false positive)")

    # --- B1-B5: the import-free property ------------------------------------
    with open(_REGISTRY_PATH, encoding="utf-8") as fh:
        reg_src = fh.read()
    red(
        "B1 a module-level side effect that runs at load time",
        import_free_problems(
            reg_src + '\n_os = __import__("os")\nopen("pwned.txt", "w").write("x")\n'
        ),
    )
    red(
        "B2 __import__ of app, then scrubbing sys.modules",
        import_free_problems(
            reg_src + '\n_sys = __import__("sys")\n'
            '_a = __import__("importlib").import_module("app.services.settings")\n'
        ),
    )
    red(
        "B3 a registry field computed by eval()",
        import_free_problems(
            reg_src.replace('PLANNED_PREFIX = "planned:"', "PLANNED_PREFIX = eval(\"'planned:'\")")
        ),
    )
    red(
        "B4 a plain import of app",
        import_free_problems(reg_src + "\nimport app.services.settings\n"),
    )
    red(
        "B5 a for-loop at module scope",
        import_free_problems(reg_src + "\nfor _t in TOOLS:\n    pass\n"),
    )

    # --- C3: the spec's scope table losing names ---------------------------
    with open(_SPEC_PATH, encoding="utf-8") as fh:
        spec_text = fh.read()
    # Un-backtick one name wherever it appears: that is what "a name left the
    # scope table" looks like whether or not the table has since been bolded,
    # renumbered or given an extra column.
    victim = sorted(names)[0]
    dropped = spec_text.replace(f"`{victim}`", victim)
    assert len(spec_tool_names(dropped)) < len(names), (
        f"C3: dropping {victim!r} from the spec's scope table does not change the parse"
    )
    caught.append("C3 a tool name dropped from the spec's scope table")
    # ...and the parse must SURVIVE cosmetic reformats, or it trains people to
    # loosen it: bolding the names, renumbering rows by family, an extra column.
    for label, forged in (
        (
            "bolded names",
            re.sub(r"^(\|\s*\d+\s*\|\s*)(`[a-z_][a-z0-9_]*`)", r"\1**\2**", spec_text, flags=re.M),
        ),
        (
            "rows renumbered by family",
            re.sub(r"^\|\s*(\d+)\s*\|(\s*`)", r"| A\1 |\2", spec_text, flags=re.M),
        ),
        (
            "an extra leading column",
            re.sub(r"^\|(\s*\d+\s*\|\s*`)", r"| x |\1", spec_text, flags=re.M),
        ),
    ):
        assert spec_tool_names(forged) == names, (
            "the spec parse collapses on a cosmetic reformat (%s): %d names"  # noqa: UP031
            % (label, len(spec_tool_names(forged)))
        )
    caught.append("C1/C2/C4 cosmetic spec reformats do NOT collapse the parse")

    # --- D1/D2/D8: self-declared tool fields --------------------------------
    fams, tiers, scopes = (set(registry.FAMILIES), set(registry.TIERS), set(registry.SCOPES))
    red(
        "D1 two tools with the same name",
        field_problems(
            tools + [dataclasses.replace(tools[-1], name=tools[0].name)],  # noqa: RUF005
            fams,
            tiers,
            scopes,
            {"x"},  # noqa: RUF005, RUF100
        ),
        "duplicate tool name",
    )
    red(
        "D2 a tool with no routes",
        field_problems([dataclasses.replace(a_read_tool, routes=())], fams, tiers, scopes, {"x"}),
        "declares no routes",
    )
    red(
        "D8 a blank summary",
        field_problems(
            [dataclasses.replace(a_read_tool, summary="", pins=())], fams, tiers, scopes, {"x"}
        ),
        "summary is 0 chars",
    )

    # --- D3: a double-counted route -----------------------------------------
    red(
        "D3 a route both covered and exempt",
        accounting_problems(real, covered, exempt | {sorted(covered)[0]}),
        "COVERED AND EXEMPT",
    )

    # --- D4/D5: the spend contract ------------------------------------------
    schema = dict(a_spend_tool.input_schema)
    schema["properties"] = dict(schema["properties"])
    schema["properties"]["confirm"] = dict(schema["properties"]["confirm"], type="string")
    red(
        "D4 `confirm` declared as a string",
        confirm_problems([dataclasses.replace(a_spend_tool, input_schema=schema)]),
        "must be a boolean",
    )
    no_confirm = dict(a_spend_tool.input_schema)
    no_confirm["properties"] = {k: v for k, v in no_confirm["properties"].items() if k != "confirm"}
    red(
        "D4b `confirm` removed entirely",
        confirm_problems([dataclasses.replace(a_spend_tool, input_schema=no_confirm)]),
        "SPEND TOOL WITHOUT confirm",
    )
    red(
        "D5 the spend tool demoted to `read`",
        coverage_policy_problems(
            [dataclasses.replace(a_spend_tool, tier="read")],
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "SPEND ROUTE ON A",
    )
    red(
        "D5b a mutating route on a read-tier tool",
        coverage_policy_problems(
            [dataclasses.replace(a_read_tool, tier="read", routes=(("POST", "/api/documents"),))],
            registry.NEVER_COVER,
            frozenset(),
            registry.MUTATING_TIERS,
        ),
        "MUTATING ROUTE ON A",
    )
    red(
        "D5c a destructive-tier tool declared at all",
        coverage_policy_problems(
            [dataclasses.replace(a_read_tool, tier="destructive")],
            registry.NEVER_COVER,
            registry.SPEND_ROUTES,
            registry.MUTATING_TIERS,
        ),
        "DESTRUCTIVE TIER POPULATED",
    )

    # --- D7: pins emptied rather than the sentence deleted -----------------
    red(
        "D7 a tool's `pins` tuple emptied",
        description_problems(
            [dataclasses.replace(next(t for t in tools if t.name in _SPEC_REQUIRED_PINS), pins=())]
        ),
        "UNPINNED CONTRACT",
    )

    # --- D9: the debt list emptied instead of the debt ---------------------
    # Wave 1 PAID the debt, so the real gap set is empty and the old proof ("the
    # list is empty while the code diverges") has no material to work with. The
    # honest replacement is to FORGE a divergence: rename one parameter of one
    # shipped tool function in a copy of server.py, confirm the AST read notices,
    # and confirm an empty debt list is then red. A check whose subject has gone
    # away must be re-proved against a synthetic subject, not quietly deleted.
    with open(_SERVER_PATH, encoding="utf-8") as fh:
        server_source = fh.read()
    real_gaps = server_signature_gaps(server_source, tools)
    assert not real_gaps, (
        "server.py diverges from the registry (%s) while SERVER_PY_WAVE1_DEBT is "  # noqa: UP031
        "empty and MAX_DEBT is %d -- pay the debt or record it" % (sorted(real_gaps), MAX_DEBT)
    )
    forged_src, forged_what = _forge_signature_drift(server_source, tools)
    forged_gaps = server_signature_gaps(forged_src, tools)
    assert forged_gaps, (
        f"D9's forge did not produce a divergence ({forged_what}) -- the AST "
        "signature read has stopped reading"
    )
    red(
        "D9 a server.py signature drifting from the registry, debt list empty",
        debt_problems(forged_gaps, ()),
        "UNRECORDED DIVERGENCE",
    )
    caught.append(
        "D9b the real debt list is empty because the CODE agrees, "
        "AST-verified against every shipped handler signature"
    )

    # --- the constraint that has to outlive this wave ----------------------
    # F2 moved: the key-import route is now COVERED by import_keys, so the
    # `cleanup` constraint lives on the TOOL. Both halves are proved -- the schema
    # may not offer the flag, and the description may not stop promising it.
    import_keys = registry.tool_by_name("import_keys")
    assert import_keys is not None, "F2: import_keys is no longer in the registry"
    with_cleanup = dict(import_keys.input_schema)
    with_cleanup["properties"] = dict(with_cleanup["properties"])
    with_cleanup["properties"]["cleanup"] = {
        "type": "boolean",
        "description": "Retire keys missing from the file.",
    }
    red(
        "F2 the import_keys `cleanup` flag put back on the schema",
        forbidden_property_problems([dataclasses.replace(import_keys, input_schema=with_cleanup)]),
        "FORBIDDEN PROPERTY",
    )
    red(
        "F2b import_keys' merge-only promise unpinned",
        description_problems([dataclasses.replace(import_keys, pins=())]),
        "UNPINNED CONTRACT",
    )

    # --- G: the VALUE-shaped escape the name-based check cannot see ---------
    # The live review's find: `upsert_keys(fields={"is_archived": true})` archived
    # a key -- gone from every downstream build file, with the change feed still
    # reporting op="updated". The property name was innocent; the damage was
    # inside its value. Three ways that can come back, three proofs.
    upsert = registry.tool_by_name("upsert_keys")
    assert upsert is not None, "G: upsert_keys is no longer in the registry"
    red(
        "G1 a new free-form object property with no declared policy",
        freeform_object_problems(
            [
                dataclasses.replace(
                    a_read_tool,
                    input_schema={
                        "type": "object",
                        "properties": {"anything": {"type": "object", "description": "arbitrary."}},
                        "required": [],
                    },
                )
            ],
            server_source,
            check_stale=False,
        ),
        "UNDECLARED FREE-FORM OBJECT",
    )
    # A silent-loss field added back to the key-edit forwarding allowlist. `force`
    # is the worked example (2026-08-05): re-adding it to _KEY_EDIT_FIELDS
    # would let an LLM demote a plural key and destroy its translated non-`other`
    # forms under an op="updated". The count=1 mutates the FIRST occurrence
    # (_KEY_EDIT_FIELDS precedes the byte-identical _KEY_ROW_FIELDS tail).
    red(
        "G2 `force` (a silent-loss field) added back to the key-edit allowlist",
        freeform_object_problems(
            [upsert],
            server_source.replace(
                '"source_file",\n        "custom",\n        "is_plural",\n        "plural_arg_name",',  # noqa: E501
                '"source_file",\n        "custom",\n        "is_plural",\n        "plural_arg_name",\n        "force",',  # noqa: E501
                1,
            ),
        ),
        "DESTRUCTIVE FIELD IN AN ALLOWLIST",
    )
    red(
        "G3 a policy naming an allowlist server.py does not define",
        freeform_object_problems(
            [upsert],
            server_source,
            policy={
                ("upsert_keys", "fields"): ("allowlist", "_NO_SUCH_LIST"),
                ("upsert_keys", "keys"): ("allowlist", "_KEY_ROW_FIELDS"),
                ("upsert_keys", "forms"): ("value-shaped", "x" * 60),
            },
            check_stale=False,
        ),
        "MISSING ALLOWLIST",
    )

    # --- H: a schema keyword the derivation would silently drop -------------
    honoured = server_literal_collection(server_source, "_HONOURED_SCHEMA_KEYWORDS")
    assert honoured, "H: _HONOURED_SCHEMA_KEYWORDS is not readable out of server.py"
    patterned = dict(a_read_tool.input_schema)
    patterned["properties"] = {
        "slug": {"type": "string", "description": "a slug.", "pattern": "^[a-z]+$"}
    }
    patterned["required"] = []
    red(
        "G4 a policy entry that outlived its property",
        freeform_object_problems(
            [upsert],
            server_source,
            policy={
                **_FREEFORM_OBJECT_POLICY,
                ("upsert_keys", "gone_away"): ("value-shaped", "x" * 60),
            },
        ),
        "STALE FREE-FORM POLICY",
    )

    red(
        "H1 a schema advertising a constraint the derivation cannot enforce",
        schema_keyword_problems(
            [dataclasses.replace(a_read_tool, input_schema=patterned)], honoured
        ),
        "UNENFORCED SCHEMA KEYWORD",
    )
    red(
        "H2 the derivation's honoured-keyword list gone missing",
        schema_keyword_problems(tools, set()),
        "could not read",
    )

    # --- P: the plan-mode POST whitelist (wave 2) ----------------------------
    # _PLAN_SAFE_REQUESTS is the one deliberate crack in "planning never sends
    # a non-GET". Four ways it could widen, four proofs: a plain write smuggled
    # in, a REAL launch route that IS a SPEND_ROUTES route on a spend tool (the
    # gap the ESTIMATE perimeter closes -- this was GREEN under the old
    # SPEND_ROUTES-only check), the whitelist outliving its spend tool, and the
    # ESTIMATE perimeter itself being widened to name a non-spend route.
    est = registry.PLAN_ESTIMATE_ROUTES
    assert plan_safe_problems(server_source, registry.SPEND_ROUTES, tools, est) == [], (
        "P0: the real whitelist violates its own perimeter"
    )
    assert plan_estimate_rot_problems(est, registry.SPEND_ROUTES, real) == [], (
        "P0b: PLAN_ESTIMATE_ROUTES violates its own rot guard"
    )
    # Open core (S2): the shipped whitelist is EMPTY (the only estimate-POST route belonged to
    # the paid Evaluations add-on), so the proofs run against a PLANTED estimate route on a
    # planted spend tool: the same four ways to widen the whitelist, each still caught.
    fake_route = ("POST", "/api/example/estimate")
    fake_est = frozenset({fake_route})
    fake_spend = frozenset(registry.SPEND_ROUTES | fake_est)
    a_spend_tool = next(t for t in tools if t.tier == "spend")
    fake_tool = dataclasses.replace(a_spend_tool, name="fake_estimate", routes=(fake_route,))
    fake_tools = [*tools, fake_tool]

    def planted(*extra):
        entries = ", ".join(repr(r) for r in (fake_route, *extra))
        return "_PLAN_SAFE_REQUESTS = frozenset({" + entries + "})\n"

    assert plan_safe_problems(planted(), fake_spend, fake_tools, fake_est) == [], (
        "P0c: the planted whitelist violates its own perimeter"
    )
    red(
        "P1 a plain write whitelisted into plan mode",
        plan_safe_problems(planted(("POST", "/api/documents")), fake_spend, fake_tools, fake_est),
        "PLAN-SAFE VIOLATION",
    )
    # P3 is THE proof the ESTIMATE perimeter exists for: a REAL launch route that
    # is a SPEND_ROUTES route (POST /api/workflows/{wid}/run) AND declared by a
    # spend tool. Under the old SPEND_ROUTES-only whitelist this passed, and it
    # WOULD spend in plan mode. Now it is not in PLAN_ESTIMATE_ROUTES, so red.
    assert ("POST", "/api/workflows/{wid}/run") in registry.SPEND_ROUTES, (
        "P3 fixture: the forged route must be a real SPEND_ROUTES route to matter"
    )
    red(
        "P3 a REAL launch route (a SPEND_ROUTES route on a spend tool) forged "
        "into the plan-mode whitelist",
        plan_safe_problems(
            planted(("POST", "/api/workflows/{wid}/run")), fake_spend, fake_tools, fake_est
        ),
        "PLAN-SAFE VIOLATION",
    )
    red(
        "P2 the whitelist outliving its spend tool",
        plan_safe_problems(planted(), fake_spend, tools, fake_est),
        "PLAN-SAFE ORPHAN",
    )
    red(
        "P4 the ESTIMATE perimeter widened to a non-spend route",
        plan_estimate_rot_problems(est | {("POST", "/api/documents")}, registry.SPEND_ROUTES, real),
        "PLAN-ESTIMATE NON-SPEND",
    )

    print(
        "  mutation-proven %d ways -- every GREEN case from the adversarial "  # noqa: UP031
        "review is now RED:" % len(caught)
    )
    for label in caught:
        print(f"      {label}")


def _wire_metadata_mutation_proofs(registry, meta, server_source):
    """Check 11 must go RED on each way a tool could ship without its metadata."""
    import dataclasses
    import types

    tools = list(registry.TOOLS)
    caught = []

    def red(label, problems, needle):
        assert any(needle in p for p in problems), (
            f"{label}: check 11 stays GREEN ({problems[:2]}), so it proves nothing"
        )
        caught.append(label)

    def forged(**changes):
        fake = types.SimpleNamespace(
            **{k: getattr(meta, k) for k in dir(meta) if not k.startswith("__")}
        )
        for key, value in changes.items():
            setattr(fake, key, value)
        return fake

    titles = dict(meta.TITLES)
    dropped = dict(titles)
    dropped.pop(tools[0].name)
    red(
        "W1 a tool with no title", wire_metadata_problems(tools, forged(TITLES=dropped)), "NO TITLE"
    )
    red(
        "W2 a title for a tool that left",
        wire_metadata_problems(tools, forged(TITLES={**titles, "gone_tool": "Gone"})),
        "STALE TITLE",
    )
    new_tool = dataclasses.replace(
        next(t for t in tools if t.tier == "write"),
        name="brand_new_tool",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "delete_all"], "description": "x"}
            },
        },
    )
    red(
        "W3 a new tool shipped with no title",
        wire_metadata_problems([*tools, new_tool], meta),
        "NO TITLE",
    )
    lying = forged(annotations_for=lambda t: {**meta.annotations_for(t), "destructiveHint": False})
    red(
        "W4 a delete action hidden as non-destructive",
        wire_metadata_problems(tools, lying),
        "HIDDEN",
    )
    red(
        "W4b a spend tool marked non-destructive",
        wire_metadata_problems(tools, lying),
        "SPEND NOT DESTRUCTIVE",
    )
    ro = forged(annotations_for=lambda t: {**meta.annotations_for(t), "readOnlyHint": True})
    red("W5 a write tool claiming read-only", wire_metadata_problems(tools, ro), "READONLY LIE")
    red(
        "W6 an open-world entry naming no tool",
        wire_metadata_problems(tools, forged(OPEN_WORLD_TOOLS=frozenset({"nope"}))),
        "STALE OPEN_WORLD_TOOLS",
    )
    red(
        "W7 a tool with no output schema",
        wire_metadata_problems(tools, forged(output_schema_for=lambda t: {"type": "string"})),
        "NO OUTPUT SCHEMA",
    )
    red(
        "W10 a write tool with a DELETE route reviewed as additive",
        wire_metadata_problems(
            tools,
            forged(
                ADDITIVE_WRITE_TOOLS=frozenset({*meta.ADDITIVE_WRITE_TOOLS, "manage_key_image"})
            ),
        ),
        "NOT ADDITIVE",
    )
    red(
        "W11 a non-additive write tool marked non-destructive",
        wire_metadata_problems(
            tools,
            forged(
                annotations_for=lambda t: {
                    **meta.annotations_for(t),
                    "destructiveHint": meta.annotations_for(t)["destructiveHint"]
                    and t.name != "upsert_keys",
                }
            ),
        ),
        "UNREVIEWED WRITE NOT DESTRUCTIVE",
    )
    stripped = server_source.replace(
        "            annotations=ToolAnnotations.model_validate(meta),\n", ""
    )
    assert stripped != server_source, "W8 fixture: the annotations keyword moved; update the forge"
    red("W8 add_tool without annotations", registration_metadata_problems(stripped), "UNANNOTATED")
    red(
        "W9 the per-tool guide pointer back",
        registration_metadata_problems(server_source + "\n_GUIDE_POINTER = 'x'\n"),
        "GUIDE POINTER BACK",
    )
    print("  [11] mutation-proven %d ways: %s" % (len(caught), "; ".join(caught)))  # noqa: UP031


if __name__ == "__main__":
    main()
