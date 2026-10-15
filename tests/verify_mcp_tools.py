"""MCP waves 1+2 + R1a/R1b + Wave 3 -- the registry-driven dispatcher and all 72 tools.

docs/specs/mcp-server.md waves 1-2. `verify_mcp.py` stays the Phase-A outcome suite
(the five demand-side tools plus the stdio protocol round trip); this is the
suite for what the dispatcher waves added: tools built FROM `registry.py`,
families A-D (wave 1: the continuous-localization loop) and E-I (wave 2:
workflows/runs, language assets + TM, LLMemory, the Evaluations Lab, the
operational reads), the two-phase spend contract on all six spend tools, and
the one route wave 1 added to `app/`.

WAVE-2 SPECIFICS this suite pins, beyond the per-tool happy paths:

  plan-mode POST   start_evaluation's plan legitimately POSTs the API's OWN
                   unconfirmed estimate call (whitelisted in
                   _PLAN_SAFE_REQUESTS) - proven to write NOTHING by the row
                   census, while a planner POSTing the same route WITH
                   confirm=true, or any other route, still raises.
  echo integrity   the tool NEVER fills in acknowledge_estimate_usd: confirm
                   without the echo relays the API's 400, a wrong echo relays
                   the 409, and only the number read from the plan launches.
  kill-switch      REALITY, per family: keys + images answer 404
  reality          (indistinguishable from missing, switch named for the
                   operator); LLMemory reads keep answering while its writes
                   refuse; Evaluations Lab writes refuse naming the switch -
                   and the relayed 409 no longer appends the mock-engine
                   advice that belongs to a different 409.
  silent drops     edit_glossary's `meta` is allowlist-filtered (the app would
                   silently DROP unknown keys; the tool refuses them by name).

HARNESS. The real FastAPI app is served by a background uvicorn thread on a free
127.0.0.1 port, exactly as verify_mcp does -- the MCP server is an HTTP client, so
a real socket is the faithful stand-in and an injected transport would test a
different program. Three API keys are minted directly (all-scope, docs-only,
api-only) so a scope refusal is a real 403 from the real gate. Everything is torn
down in `finally`.

WHAT IT ASSERTS, per the wave's brief:

  registration   registry <-> server in BOTH directions: every Tool has a handler,
                 every registered tool is a Tool, and the schema the model is shown
                 is the registry's own text -- not a pydantic rendering of it that
                 could drift from what was reviewed.
  per tool       a happy-path round trip against the live app, and a schema
                 rejection of a bad input through the REAL FastMCP arg model.
                 (list_languages / list_capabilities take no arguments, so there
                 is no bad input to reject -- stated rather than skipped silently.)
  scope          a wrong-scope token is a 403 whose message names the grant.
  spend          two-phase: an unconfirmed spend call returns a PLAN and writes
                 NOTHING -- asserted by counting rows in every table a spend
                 touches (job, segment, spendledger, auditlog, document,
                 keychangelog, stringkey, project) across the call, not by reading
                 the reply, and driven through FastMCP's own arg model because
                 that is the path a client takes. Then confirm=true executes.
                 Separately: confirm="false"/"0"/"no"/"off" must PLAN on the raw
                 dispatcher too, since every one of those is truthy in Python.
  delta          the cursor round trip (page, store next_cursor, re-read, get
                 nothing twice) and the resync_required shape.
  kill switch    with key_projects_enabled off, every key tool relays the API's
                 404 verbatim -- indistinguishable from a missing object.
  the new route  GET /api/capabilities/{slug}: 200 + real content on a BASE api
                 key, 404 on an unknown slug, 401 unauthenticated.
  no retirement  upsert_keys refuses is_archived / is_hidden / any unknown field
                 in BOTH write modes: archiving removes a key from every
                 downstream build file while the change feed still says
                 op="updated", which would contradict key_changes' pinned
                 deletion contract.
  regressions    the Content-Disposition basename (a load-bearing security control
                 on an attacker-influenceable header), the plan-only invariant,
                 the spend handlers' own confirm buckle, and that import_keys
                 never puts `cleanup` on the wire.
"""

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_mcptools_")

_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)

try:
    import server as mcp_server
except ModuleNotFoundError as exc:  # pragma: no cover -- only without the SDK
    if "mcp" in str(exc):
        # FAIL LOUDLY, never a green skip. This is the MONEY-PROVING suite: the
        # nothing-spent census, both seams, the handler buckle and the
        # kill-switch reality all live here. A "SKIP ... MCP TOOLS OK" would
        # assert the spend contract ZERO times behind a success line -- exactly
        # the shape a silent regression hides in. verify_mcp.py is the protocol
        # test that may skip without the SDK; this one matches the parity guard's
        # discipline instead: without the SDK it cannot run, so it must be RED,
        # not a cheerful green. Install the mcp SDK to run it.
        print(
            "  FATAL: the mcp SDK is not installed, so the money-proving suite "
            "(census, both seams, the spend buckle, the kill switches) cannot "
            "run. This suite MUST NOT be skipped -- a green skip would assert "
            "the spend contract zero times behind a success message. Install "
            "the mcp SDK.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

# This suite exercises the tools as a LOCAL STDIO server would, so it must say so:
# caller-supplied file reads are denied by default now (fail-closed), because the old
# default left them enabled for any start path that never called main() -- including
# serving the ASGI app directly. Emulating stdio means opting in explicitly.
# #755: and it names the folders its fixtures live in (temp + this checkout), because a
# stdio read is now confined to the allowed input folders.
mcp_server._allow_local_file_reads(True, roots=[tempfile.gettempdir(), _ROOT])


import httpx
import registry
import uvicorn
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.services import apikeys, security, settings
from app.services import keys as keys_svc
from app.services.providers import Provider, register_provider


class LiveStubProvider(Provider):
    """A 'configured' engine, so the mock-hard-fail is off for the happy paths."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornThread:
    def __init__(self, port: int):
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


# --- calling conventions ----------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def call(tool, /, **arguments):
    """Invoke a tool through the dispatcher WITHOUT the arg model, and parse it.

    `call_tool_unvalidated` is the raw seam: it is where a hostile argument
    reaches `_dispatch` unfiltered, which makes it the right harness for proving
    the dispatcher safe on its own and the WRONG one for proving a contract the
    arg model helps enforce. The money contract is therefore proved with
    `via_mcp` (below); the review found the previous version proving it here, i.e.
    on the one path a real client never takes.

    Positional-only, because two of the sixteen tools have a `name` parameter."""
    arguments = _planned(tool, arguments, validated_path=False)
    raw = _run(mcp_server.call_tool_unvalidated(tool, arguments))
    _check_structured(tool, raw)
    return json.loads(raw)


#: #750 gate: tools whose REAL answers were validated against their outputSchema.
_STRUCTURED_VALIDATED: set = set()


def _check_structured(tool: str, raw: str) -> None:
    """Every answer this suite reads must turn into structuredContent that equals the
    parsed text and validates against the tool's advertised outputSchema, the check
    an mcp client (and Claude Code) applies before it accepts the result."""
    import jsonschema
    import tool_meta

    spec = registry.tool_by_name(tool)
    if spec is None:
        return
    result = mcp_server._structured(raw, spec)
    jsonschema.validate(result.structured_content, tool_meta.output_schema_for(spec))
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        assert result.structured_content == parsed, (
            f"{tool}: a real answer collides with a typed outputSchema field: {sorted(parsed)[:20]}"
        )
    assert result.content[0].text == raw
    _STRUCTURED_VALIDATED.add(tool)


def _planned(name: str, arguments: dict, *, validated_path: bool) -> dict:
    """#757: a confirmed spend call must carry the plan_id of its own plan, so these
    helpers do what a real agent does: plan with the same arguments (no confirm) on the
    SAME path, then confirm with the returned plan_id. A call that already names a
    plan_id, is not confirmed, or is not a spend tool passes through untouched. On the
    raw seam a plan that itself fails RAISES its error: that is the first answer a real
    agent gets (a wrong-scope key meets its 403 while planning, before it could confirm)."""
    args = dict(arguments or {})
    spec = registry.tool_by_name(name)
    if spec is None or spec.tier != "spend" or args.get("confirm") is not True:
        return args
    if "plan_id" in args:
        return args
    plan_args = {k: v for k, v in args.items() if k != "confirm"}
    if validated_path:
        res = _run(mcp_server.mcp.call_tool(name, plan_args))
        try:
            plan = json.loads("".join(getattr(b, "text", "") for b in res.content))
        except ValueError:
            return args
    else:
        plan = json.loads(_run(mcp_server.call_tool_unvalidated(name, plan_args)))
    if isinstance(plan, dict) and plan.get("plan_id"):
        args["plan_id"] = plan["plan_id"]
    return args


def via_mcp(name: str, arguments: dict):
    """Invoke a tool through FastMCP itself -- THE PATH A CLIENT TAKES.

    The pydantic arg model built from the registry's schema validates and coerces
    first, exactly as it does for a real MCP client."""
    arguments = _planned(name, arguments, validated_path=True)
    result = _run(mcp_server.mcp.call_tool(name, arguments))
    if not getattr(result, "is_error", False) and result.structured_content is not None:
        spec = registry.tool_by_name(name)
        if spec is not None:
            import jsonschema
            import tool_meta

            jsonschema.validate(result.structured_content, tool_meta.output_schema_for(spec))
            _STRUCTURED_VALIDATED.add(name)
    return result


def validated(name: str, arguments: dict):
    """via_mcp, with the tool's JSON reply parsed out of the content blocks."""
    result = via_mcp(name, arguments)
    # mcp 2: call_tool returns a CallToolResult (v1 returned content blocks or a
    # (blocks, structured) tuple); read its .content.
    blocks = result.content
    text = "".join(getattr(b, "text", "") for b in blocks)
    return json.loads(text)


def expect_error(name: str, arguments: dict, needle: str, what: str) -> str:
    try:
        arguments = _planned(name, arguments, validated_path=False)
        _run(mcp_server.call_tool_unvalidated(name, arguments))
    except mcp_server.ToolError as exc:
        msg = str(exc)
        assert needle.lower() in msg.lower(), f"{what}: expected {needle!r} in: {msg}"
        return msg
    raise AssertionError(f"{what}: expected a ToolError mentioning {needle!r}")


def expect_schema_refusal(name: str, arguments: dict) -> None:
    """A bad input must be refused by the arg model, never handed to a handler."""
    try:
        via_mcp(name, arguments)
    except Exception as exc:
        text = str(exc)
        assert "validation" in text.lower() or "Input" in text or "Field" in text, (
            f"{name}: refused, but not by schema validation: {text[:200]}"
        )
        return
    raise AssertionError(f"{name}: {arguments!r} was accepted; the schema validates nothing")


#: Tables a real spend touches, and a couple more besides. Counting two of them
#: was too narrow to prove "nothing happened": a spend writes a SpendLedger row
#: and an AuditLog row before anyone looks at a Job. Named rather than derived
#: wholesale so the assertion stays readable when it fires.
#:
#: KNOWN GAP (wave-2 review, item 8): this names ~16 of the ~57 `table=True`
#: models, so a plan-mode write to one of the other ~41 (e.g. departmentbudget,
#: steprun) would be invisible to the census. There is NO live leak today - every
#: wave-2 plan leg is a GET or the one whitelisted estimate POST, and the
#: estimate's single audit row is the only movement any plan produces (see
#: _plan_delta_ok). Deriving the watch set as "all table=True minus a justified
#: ignore list" was considered and left out on purpose: several tables move on
#: any request (audit/session/rate-limit rows), which would make the exact-equality
#: census flaky rather than stricter. The set is instead every table a spend in
#: THESE families actually writes; widen it if a new spend path touches a new one.
_WATCHED_TABLES = (
    "job",
    "segment",
    "spendledger",
    "auditlog",
    "document",
    "keychangelog",
    "stringkey",
    "project",
    # wave 2: every table a family E-I spend would touch
    "workflowrun",
    "workflowbatch",
    "evalrun",
    "evalscore",
    "memoryentry",
    "memoryevent",
    "termcandidate",
    "glossaryentry",
)


def _counts() -> dict:
    """Row counts for every table a spend or a write would touch.

    The unconfirmed-spend assertion compares this dict across the call: if
    planning wrote ANYTHING - a ledger row, an audit row, a job - the diff says
    which table, instead of the reply saying "spent": false and being believed."""
    from sqlalchemy import func
    from sqlmodel import SQLModel

    out = {}
    with Session(engine) as s:
        for name, table in SQLModel.metadata.tables.items():
            if name.lower() not in _WATCHED_TABLES:
                continue
            out[name] = s.exec(select(func.count()).select_from(table)).one()
    assert len(out) == len(_WATCHED_TABLES), (
        "only %s of the %d watched tables exist -- a model was renamed and this "  # noqa: UP031
        "assertion went blind on the rest" % (sorted(out), len(_WATCHED_TABLES))
    )
    return out


# ============================================================================
def check_registration() -> None:
    """Check 7's server side: registry <-> server, both directions, plus the
    schema the model is actually shown."""
    # The SDK's own list (every registered tool): mcp.list_tools() is narrowed by the
    # key's scopes and the toolsets since #751 (verify_mcp_tool_filter owns that).
    tools = _run(mcp_server.MCPServer.list_tools(mcp_server.mcp))
    registered = {t.name for t in tools}
    declared = {t.name for t in registry.TOOLS}
    assert registered == declared, (
        f"registry/server disagree: only in server {sorted(registered - declared)}; only in registry {sorted(declared - registered)}"  # noqa: E501
    )
    assert set(mcp_server.HANDLERS) == declared, "HANDLERS and TOOLS disagree"

    by_name = {t.name: t for t in tools}
    manager = mcp_server.mcp._tool_manager._tools
    for spec in registry.TOOLS:
        shown = by_name[spec.name].input_schema
        assert shown == spec.input_schema, (
            f"{spec.name}: the model is shown a schema that is not the registry's:\n  {json.dumps(shown)[:300]}"  # noqa: E501
        )
        assert by_name[spec.name].description.startswith(spec.summary), (
            f"{spec.name}: description does not start with the registry summary"
        )
        # #752: the guide pointer is said ONCE in the server instructions, not
        # repeated in all 84 descriptions.
        assert "locstudio://guide/llm" not in by_name[spec.name].description, (
            f"{spec.name}: description still carries the per-tool guide pointer"
        )
        # ...and the DERIVED model (what actually validates a call) must agree
        # with the advertised schema, or the tool would advertise one contract and
        # enforce another. This is the assertion that goes red if the SDK internals
        # move under _advertise_registry_schema.
        derived = manager[spec.name].fn_metadata.arg_model.model_json_schema()
        assert set(derived.get("properties") or {}) == set(
            spec.input_schema.get("properties") or {}
        ), "{}: validated parameters {} != advertised {}".format(
            spec.name,
            sorted(derived.get("properties") or {}),
            sorted(spec.input_schema.get("properties") or {}),
        )
        assert set(derived.get("required") or []) == set(spec.input_schema.get("required") or []), (
            "{}: validated required {} != advertised {}".format(
                spec.name,
                sorted(derived.get("required") or []),
                sorted(spec.input_schema.get("required") or []),
            )
        )
    resources = _run(mcp_server.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert {"locstudio://guide/llm", "locstudio://openapi"} <= uris, uris
    print(
        "  registration: %d registry tools == %d registered tools == %d handlers; "  # noqa: UP031
        "every advertised schema is the registry's verbatim and the derived arg "
        "model agrees; %d resources"
        % (len(declared), len(registered), len(mcp_server.HANDLERS), len(uris))
    )


def check_capabilities_route(base_url: str, api_only: str, docs_key: str) -> None:
    """The ONE route wave 1 added to app/ (SPEC section 10, decision 7)."""
    with httpx.Client(base_url=base_url, timeout=15.0) as c:
        r = c.get("/api/capabilities/llm", headers={"X-Loc-Token": api_only})
        assert r.status_code == 200, (r.status_code, r.text[:200])
        body = r.json()
        assert body["slug"] == "llm" and body["title"], body
        assert "Are you an LLM" in body["title"], body["title"]
        assert len(body["body"]) > 2000, "the card body came back truncated"
        assert "X-Loc-Token" in body["text"], "the card text lost its own contract"
        assert isinstance(body["card"].get("nouns"), dict), "front matter missing"
        print(
            "  /api/capabilities/llm: 200 on a BASE api key (no docs grant), "  # noqa: UP031
            "%d chars of body, front matter intact" % len(body["body"])
        )

        r = c.get("/api/capabilities/keys", headers={"X-Loc-Token": docs_key})
        assert r.status_code == 200 and r.json()["slug"] == "keys", r.text[:200]

        r = c.get("/api/capabilities/no-such-card", headers={"X-Loc-Token": api_only})
        assert r.status_code == 404, r.status_code
        assert r.json() == {"ok": False, "error": "Not found."}, r.json()
        print("  /api/capabilities/no-such-card: 404 without enumeration hints")

        r = c.get("/api/capabilities/llm")
        assert r.status_code == 401, ("an unauthenticated card read must 401", r.status_code)
        print(
            "  /api/capabilities/llm unauthenticated: 401 (identity always "
            "required, even for documentation)"
        )


def check_orientation() -> None:
    me = call("whoami")
    assert me["account"]["email"] == "admin", me
    # #751: whoami reports the key's own grants (the suite's env key is api docs keys).
    assert me["scopes"] == ["api", "docs", "keys"], me["scopes"]
    assert me["toolsets"] == "all", me
    assert "card" not in me, "whoami fetched a card nobody asked for"
    withcard = call("whoami", card="keys")
    assert withcard["card"]["slug"] == "keys" and withcard["card"]["text"], withcard
    print(
        "  whoami: role %s, honest about scopes; card=keys returns %d chars of "  # noqa: UP031
        "the live install's own guide" % (me["account"]["role"], len(withcard["card"]["text"]))
    )

    from app.services import content_types, languages

    languages.create("MCP Wave1 Set", "en-US", ["de-DE", "fr-FR"], actor="test")
    with Session(engine) as s:
        content_types.create(s, "MCP Wave1 Type", description="for the suite")

    langs = call("list_languages")
    assert langs["language_sets"] and langs["locales"], langs
    caps = call("list_capabilities")
    exts = {f["ext"] for f in caps["formats"]}
    assert {"docx", "txt", "json"} <= exts, sorted(exts)
    assert any(ls["name"] == "MCP Wave1 Set" for ls in caps["language_sets"]), caps
    assert any(ct["name"] == "MCP Wave1 Type" for ct in caps["content_types"]), caps
    print(
        "  list_capabilities: %d formats + %d language sets + %d content types "  # noqa: UP031
        "(the wave-1 widening: one call, three catalogues)"
        % (len(exts), len(caps["language_sets"]), len(caps["content_types"]))
    )

    expect_error("whoami", {"card": "../../etc/passwd"}, "not found", "card-traversal")
    print("  whoami: a traversal-shaped card slug is a plain 404, not a file read")


def check_projects() -> int:
    made = call(
        "create_project",
        name="MCP Wave1 Docs",
        customer_name="Wave One Ltd",
        target_locales=["de-DE", "fr-FR"],
    )
    pid = made["project"]["id"]
    assert made["customer_created"] is True, made
    assert made["project"]["project_type"] == "document", made
    again = call("create_project", name="MCP Wave1 Second", customer_name="wave one ltd")
    assert again["customer_created"] is False, (
        "the composite created a duplicate customer instead of reusing one"
    )
    assert again["customer_id"] == made["customer_id"], (again, made)
    # F-2: a PADDED customer name must REUSE, not duplicate -- the client-side re-filter
    # must AGREE with the server's casefold+trim ?name= match (the old `.strip().lower()`
    # trimmed the stored value but not the query, so the server's correct match was
    # discarded and a duplicate created anyway).
    padded = call("create_project", name="MCP Wave1 Padded", customer_name="  Wave One Ltd  ")
    assert padded["customer_created"] is False, (
        "a padded customer name must reuse the existing customer, not duplicate it"
    )
    assert padded["customer_id"] == made["customer_id"], (padded, made)
    print(
        "  create_project: composite created customer %d then project %d; a second "  # noqa: UP031
        "project with the same name -- incl. a PADDED variant -- REUSED it "
        "(created=False)" % (made["customer_id"], pid)
    )

    listed = call("list_projects")
    assert any(p["id"] == pid for p in listed["projects"]), listed
    assert any(c["id"] == made["customer_id"] for c in listed["customers"]), listed
    one = call("list_projects", view="project", project_id=pid)
    assert one["project"]["name"] == "MCP Wave1 Docs", one
    assert set(one["project"]["target_locales"]) == {"de-DE", "fr-FR"}, one
    docs = call("list_projects", view="documents", project_id=pid)
    assert docs["documents"] == [], docs
    expect_error("list_projects", {"view": "project"}, "needs a project_id", "view-without-id")
    print(
        "  list_projects: 3 views (projects / project / documents) over 4 routes; "
        "view=project without an id is refused before any call"
    )

    renamed = call("configure_project", project_id=pid, action="rename", name="MCP Wave1 Documents")
    assert renamed["result"]["name"] == "MCP Wave1 Documents", renamed
    added = call("configure_project", project_id=pid, action="add-locale", locale="ja-JP")
    assert added["result"]["locale"] == "ja-JP", added
    call("configure_project", project_id=pid, action="due", due_date="2027-01-31")
    after = call("list_projects", view="project", project_id=pid)
    assert after["project"]["due_date"] == "2027-01-31", after
    assert "ja-JP" in after["project"]["target_locales"], after
    # EVERY action, because the registry claims all seven routes for this tool and
    # an unexercised claim is an unverified one.
    caps = call("list_capabilities")
    ct_id = next(c["id"] for c in caps["content_types"] if c["name"] == "MCP Wave1 Type")
    ls_id = next(s["id"] for s in caps["language_sets"] if s["name"] == "MCP Wave1 Set")
    other = call("create_project", name="MCP Wave1 Other", customer_name="Second Ltd")
    call("configure_project", project_id=pid, action="content-type", content_type_id=ct_id)
    call("configure_project", project_id=pid, action="apply-langset", langset_id=ls_id)
    call(
        "configure_project",
        project_id=pid,
        action="pair-providers",
        provider_mode="per_pair",
        pair_providers={"en-US:de-DE": "claude"},
    )
    # set-customer is ASSIGNMENT ONLY: /api refuses to re-home a project that
    # already has one, because TM and glossary provenance is scoped by customer.
    # Every project this tool can create has a customer (POST /api/projects
    # requires one), so the honest thing to verify is that the refusal is relayed
    # with its real reason -- and that the registry's description says so.
    msg = expect_error(
        "configure_project",
        {"project_id": pid, "action": "set-customer", "customer_id": other["customer_id"]},
        "reassignment is not supported",
        "set-customer-reassign",
    )
    assert "provenance" in msg, msg
    schema = registry.tool_by_name("configure_project").input_schema
    assert "REASSIGNMENT IS REFUSED" in schema["properties"]["customer_id"]["description"], (
        "the schema still promises a move it cannot do"
    )
    final = call("list_projects", view="project", project_id=pid)
    assert final["project"]["content_type_id"] == ct_id, final
    assert final["project"]["provider_mode"] == "per_pair", final
    assert "claude" in json.dumps(final["project"]["pair_providers"]), final
    assert final["project"]["customer_id"] == made["customer_id"], final
    assert "fr-FR" in final["project"]["target_locales"], final

    expect_error(
        "configure_project",
        {"project_id": pid, "action": "rename"},
        "needs name",
        "action-without-field",
    )
    expect_error(
        "configure_project",
        {"project_id": pid, "action": "remove-locale"},
        "unknown action",
        "action-not-offered",
    )
    print(
        "  configure_project: all 7 actions exercised (rename, due, add-locale, "
        "content-type, apply-langset, pair-providers, set-customer there and "
        "back) and read back; a missing field is refused, and remove-locale is "
        "not an action at all - it demands a typed echo no agent may fabricate"
    )
    return pid


def check_documents(pid: int, out_dir: str) -> int:
    src = os.path.join(tempfile.mkdtemp(prefix="mcp_w1_"), "hello.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("First sentence.\nSecond sentence.\nThird one here.\n")

    # --- THE TWO-PHASE CONTRACT ---------------------------------------------
    # THROUGH `validated`, i.e. FastMCP's own arg model, because that is the path a
    # real client takes. The raw seam is exercised separately below, on purpose.
    before = _counts()
    plan = validated(
        "submit_translation", {"file_path": src, "targets": ["de-DE", "fr-FR"], "project_id": pid}
    )
    after = _counts()
    assert after == before, "an unconfirmed spend call wrote to the database: %s" % {
        k: (before[k], after[k]) for k in before if before[k] != after[k]
    }
    assert plan["plan"] is True and plan["spent"] is False, plan
    assert plan["tool"] == "submit_translation" and plan["tier"] == "spend", plan
    assert plan["scope"]["locale_count"] == 2, plan["scope"]
    assert plan["scope"]["source_lines"] == 3, plan["scope"]
    assert plan["engine"]["resolved"], "the plan did not resolve an engine"
    assert plan["budget"]["readable"] is False and plan["budget"]["note"], plan
    assert "confirm=true" in plan["next_step"], plan["next_step"]
    print(
        "  submit_translation WITHOUT confirm (through the arg model): plan only "  # noqa: UP031
        "- engine %r, %d locales, %d source lines, and NOTHING written across %d "
        "tables incl. spendledger + auditlog"
        % (
            plan["engine"]["resolved"],
            plan["scope"]["locale_count"],
            plan["scope"]["source_lines"],
            len(before),
        )
    )

    # A STRING THAT LOOKS LIKE CONSENT IS NOT CONSENT. Every one of these was
    # truthy in Python and SPENT before the fix (spend-ledger rows to prove it).
    # Both seams are checked: the arg model coerces them to real booleans, and the
    # raw dispatcher refuses anything that is not the boolean True.
    for bogus in ("false", "0", "no", "off", "", 0, None):
        before = _counts()
        got = call(
            "submit_translation", file_path=src, targets=["de-DE"], project_id=pid, confirm=bogus
        )
        assert got.get("plan") is True and got.get("spent") is False, (
            f"confirm={bogus!r} SPENT through the raw dispatcher: {got}"
        )
        assert _counts() == before, f"confirm={bogus!r} wrote to the database"
    for coerced in ("false", "0", "no", "off"):
        before = _counts()
        got = validated(
            "submit_translation",
            {"file_path": src, "targets": ["de-DE"], "project_id": pid, "confirm": coerced},
        )
        assert got.get("plan") is True, f"confirm={coerced!r} SPENT through the arg model: {got}"
        assert _counts() == before, f"confirm={coerced!r} wrote to the database"
    print(
        "  spend gate: confirm in ('false','0','no','off','',0,None) is a PLAN on "
        "the raw dispatcher AND through the arg model - 11 probes, zero rows "
        "written. Only the boolean True spends."
    )

    missing = validated(
        "submit_translation", {"file_path": os.path.join(out_dir, "nope.txt"), "targets": ["de-DE"]}
    )
    assert missing["plan"] is True and missing["warnings"], missing
    assert "would fail" in missing["warnings"][0], missing["warnings"]
    # ...and with no project_id the plan must not claim to know where the document
    # lands: routing rules decide the destination AND can override the engine.
    assert "routing rules" in str(missing["scope"]["project"]), missing["scope"]
    assert "inbox" not in str(missing["scope"]["project"]).split("only if")[0], (
        "the plan still leads with the inbox claim: {}".format(missing["scope"]["project"])
    )
    assert "UNRESOLVED" in missing["engine"]["note"], missing["engine"]
    print(
        "  submit_translation plan with no project: warns on the missing file and "
        "says the SERVER's routing rules pick the project and can override the "
        "engine - no confident inbox claim, because /api cannot preview one"
    )

    # --- and now with confirm ------------------------------------------------
    out = validated(
        "submit_translation",
        {"file_path": src, "targets": ["de-DE"], "project_id": pid, "confirm": True},
    )
    doc_id = out["document_id"]
    assert doc_id and len(out["jobs"]) == 1, out
    assert _counts()["job"] > before["job"], "confirm=true did not start a job"
    job_id = out["jobs"][0]["job_id"]
    print("  submit_translation WITH confirm: document %d, job %d started" % (doc_id, job_id))  # noqa: UP031

    finished = None
    for _ in range(20):
        st = call("job_status", job_id=job_id)
        if st["finished"]:
            finished = st
            break
        time.sleep(0.1)
    assert finished and str(finished["status"]).lower() == "completed", finished
    listing = call("job_status", limit=5)
    assert listing["total"] >= 1 and listing["jobs"], listing
    print(
        "  job_status: polled job %d to COMPLETED (%s/%s), and the wave-1 listing "  # noqa: UP031
        "widening returned %d of %d rows"
        % (job_id, finished["done"], finished["total"], len(listing["jobs"]), listing["total"])
    )

    got = call("get_result", document_id=doc_id, locale="de-DE")
    assert os.path.isfile(got["path"]), got
    assert os.path.commonpath(
        [os.path.abspath(out_dir), os.path.abspath(got["path"])]
    ) == os.path.abspath(out_dir), "wrote outside LOC_STUDIO_OUTPUT_DIR"
    with open(got["path"], "rb") as fh:
        assert b"[de-DE]" in fh.read(), "the downloaded file is not the translation"
    print(
        "  get_result: %d bytes written inside the output dir, translated content "  # noqa: UP031
        "present, bytes never in the tool result" % got["bytes"]
    )

    # An EMPTY targets list uploads only - the contract that makes a separate
    # upload_document tool unnecessary - and gives apply_tm something real to
    # fill from the memory the first document just produced.
    # Seed one 100% memory the way a human approval does, so apply_tm has a real
    # hit to find rather than proving only that the route answers.
    from app.services import tm as tm_svc

    with Session(engine) as s:
        tm_svc.write_back(s, pid, "Second sentence.", "Zweiter Satz.", "en-US", "de-DE")
    staged = call("submit_translation", file_path=src, targets=[], project_id=pid, confirm=True)
    assert staged["jobs"] == [], f"targets=[] started a job anyway: {staged}"
    tm_out = call("apply_tm", document_id=staged["document_id"], locale="de-DE")
    assert tm_out["applied"] >= 1, (
        f"apply_tm filled nothing although the same source was just translated into de-DE: {tm_out}"
    )
    print(
        "  submit_translation with targets=[]: document %d staged, 0 jobs; "  # noqa: UP031
        "apply_tm then filled %d segment(s) from the memory the first "
        "translation produced - a free win before any spend"
        % (staged["document_id"], tm_out["applied"])
    )

    before = _counts()
    plan = validated("translate_text", {"text": "Hello there", "target_locale": "de-DE"})
    assert _counts() == before, "an unconfirmed translate_text wrote to the database"
    assert plan["plan"] is True and plan["scope"]["characters"] == 11, plan
    assert "provider_used" in plan["engine"]["note"], plan["engine"]
    for bogus in ("false", "0", "no"):
        before = _counts()
        assert (
            call("translate_text", text="Hello there", target_locale="de-DE", confirm=bogus).get(
                "plan"
            )
            is True
        ), f"translate_text spent on confirm={bogus!r}"
        assert _counts() == before, f"confirm={bogus!r} wrote to the database"
    # ...and through the arg model (the client path), per-tool: pydantic coerces
    # these to boolean False, so each must PLAN rather than spend.
    for coerced in ("false", "0", "no", "off"):
        before = _counts()
        assert (
            validated(
                "translate_text",
                {"text": "Hello there", "target_locale": "de-DE", "confirm": coerced},
            ).get("plan")
            is True
        ), f"translate_text spent on confirm={coerced!r} through the arg model"
        assert _counts() == before, f"confirm={coerced!r} wrote to the database"
    real = validated(
        "translate_text", {"text": "Hello there", "target_locale": "de-DE", "confirm": True}
    )
    assert real.get("translation"), real
    print(
        "  translate_text: unconfirmed -> plan (%d chars, engine note names "  # noqa: UP031
        "provider_used); confirmed -> %r"
        % (plan["scope"]["characters"], str(real.get("translation"))[:40])
    )
    return doc_id


def check_keys(out_dir: str) -> int:
    made = call(
        "create_project",
        name="MCP Wave1 Keys",
        customer_name="Wave One Ltd",
        project_type="key_based",
        target_locales=["de-DE"],
    )
    kpid = made["project"]["id"]
    assert made["project"]["project_type"] == "key_based", made
    print("  create_project: key-based project %d (type is fixed at creation)" % kpid)  # noqa: UP031

    up = call(
        "upsert_keys",
        project_id=kpid,
        keys=[
            {"name": "app.title", "base": "Localization OS"},
            {"name": "app.subtitle", "base": "Translate everything"},
            {"name": "", "base": "this one has no name"},
        ],
    )
    assert len(up["created"]) == 2, up
    assert len(up["errors"]) == 1 and up["errors"][0]["index"] == 2, up
    print(
        "  upsert_keys bulk: 2 created, 1 rejected - PARTIAL SUCCESS rides back "
        "in errors[] rather than failing the batch"
    )

    page = call("list_keys", project_id=kpid, limit=10)
    assert len(page["keys"]) == 2 and page["has_more"] is False, page
    assert page["source_locale"], page
    key_id = page["keys"][0]["id"]
    one = call("list_keys", key_id=key_id)
    assert one["key"]["id"] == key_id, one
    print(
        "  list_keys: paged %d keys (next_cursor %s), and one key by id"  # noqa: UP031
        % (len(page["keys"]), page["next_cursor"])
    )

    set_val = call("upsert_keys", key_id=key_id, locale="de-DE", text="Localization OS DE")
    assert set_val["key"]["id"] == key_id, set_val
    edited = call("upsert_keys", key_id=key_id, fields={"description": "the product name"})
    assert edited["key"]["description"] == "the product name", edited
    expect_error("upsert_keys", {"key_id": key_id}, "exactly one mode", "upsert-ambiguous")
    print(
        "  upsert_keys: value mode and edit mode both land; a half-specified call "
        "is refused rather than guessed"
    )

    # --- THE SILENT-RETIREMENT ESCAPE (live-review find) --------------------
    # `is_archived` is in the app's own EDIT_FIELDS, and archiving REMOVES a key
    # from every downstream build file while the change feed still reports
    # op="updated" - a deletion nobody is told about. Both write modes forward a
    # free-form object, so both are checked, and the key is then proved still
    # present in the export the CI consumer actually reads.
    before_feed = call("key_changes", project_id=kpid)["next_cursor"]
    for args, what in (
        ({"key_id": key_id, "fields": {"is_archived": True}}, "edit mode"),
        ({"key_id": key_id, "fields": {"is_hidden": True}}, "edit mode"),
        ({"project_id": kpid, "keys": [{"name": "app.title", "is_archived": True}]}, "bulk mode"),
        ({"project_id": kpid, "keys": [{"name": "app.new", "is_hidden": True}]}, "bulk mode"),
        ({"key_id": key_id, "fields": {"deleted_at": "2020-01-01"}}, "an unknown field"),
    ):
        msg = expect_error("upsert_keys", args, "does not accept", f"archive-escape-{what}")
        assert "Allowed:" in msg, msg
    assert call("key_changes", project_id=kpid, since=before_feed)["entries"] == [], (
        "a refused archive still moved the change feed"
    )
    still = call("list_keys", project_id=kpid, limit=50)
    assert any(k["name"] == "app.title" for k in still["keys"]), (
        "the key vanished from the catalogue after a refused archive"
    )
    print(
        "  upsert_keys: is_archived / is_hidden / an unknown field are REFUSED in "
        "both write modes (allowlist, not denylist), the change feed did not "
        "move, and the key is still in the catalogue - a silent retirement with "
        "op=updated would have contradicted key_changes' pinned promise"
    )

    # --- FORCE: the plural-demotion lever is a human's, not the model's ------
    # 2026-08-05: demoting a plural key whose non-`other` forms hold a
    # translation DESTROYS them; the app takes `force=True` to accept that loss,
    # and the MCP surface deliberately does not forward it. Two halves proved: the
    # edit seam REFUSES a `force` the model tries to pass (structural + at the
    # allowlist), and a legitimate demotion the tool DOES send never carries force
    # on the wire - it returns as the honest refusal with the human surfaces named.
    real_call = mcp_server._call
    assert "force" not in mcp_server._KEY_EDIT_FIELDS, (
        "`force` is back in the MCP key-edit allowlist - an LLM could destroy a "
        "plural key's translated forms under an op=updated"
    )
    force_msg = expect_error(
        "upsert_keys",
        {"key_id": key_id, "fields": {"force": True}},
        "does not accept",
        "force-refused-at-seam",
    )
    assert "force" in force_msg and "Allowed:" in force_msg, force_msg
    assert "human" in force_msg or "CLI" in force_msg, force_msg
    # A real demotion the tool sends: a plural key with a translated non-`other`
    # form, then the demotion attempt, watched on the wire.
    call("upsert_keys", project_id=kpid, keys=[{"name": "app.items", "is_plural": True}])
    pid = call("list_keys", project_id=kpid, q="app.items", limit=5)["keys"][0]["id"]
    call(
        "upsert_keys",
        key_id=pid,
        locale="de-DE",
        forms={"one": "ein Element", "other": "%d Elemente"},
    )
    sent_force = []

    async def recording_call_force(client, method, path, **kw):
        sent_force.append((method, path, kw.get("json_body")))
        return await real_call(client, method, path, **kw)

    mcp_server._call = recording_call_force
    try:
        demote_msg = expect_error(
            "upsert_keys",
            {"key_id": pid, "fields": {"is_plural": False}},
            "translated plural forms",
            "plural-demotion-refused",
        )
    finally:
        mcp_server._call = real_call
    assert "unavailable over MCP" in demote_msg, demote_msg
    assert "keys edit --single --force" in demote_msg, demote_msg
    patch_bodies = [b for m, _p, b in sent_force if m == "PATCH" and isinstance(b, dict)]
    assert patch_bodies, f"the demotion sent no PATCH: {sent_force}"
    assert all("force" not in b for b in patch_bodies), (
        f"upsert_keys put `force` on the wire: {sent_force}"
    )
    assert patch_bodies[0].get("is_plural") is False, patch_bodies
    still_plural = call("list_keys", project_id=kpid, q="app.items", limit=5)["keys"][0]
    assert still_plural["is_plural"] is True, "a refused demotion still flipped the key to single"
    print(
        "  upsert_keys: `force` is REFUSED at the edit seam (a human-only lever, "
        "not in _KEY_EDIT_FIELDS) and never rides the wire; a real plural "
        "demotion returns the honest refusal naming the web grid + `keys edit "
        "--single --force`, and the key stays plural"
    )

    # --- import: MERGE ONLY, and `cleanup` never reaches the wire ------------
    payload = json.dumps({"app.title": "Localization OS", "app.footer": "(c) 2026"})
    sent = []
    real_call = mcp_server._call

    async def recording_call(client, method, path, **kw):
        sent.append((method, path, kw.get("json_body")))
        return await real_call(client, method, path, **kw)

    mcp_server._call = recording_call
    try:
        rep = call("import_keys", project_id=kpid, data=payload, filename="en.json", locale="en-US")
    finally:
        mcp_server._call = real_call
    assert rep.get("created") or rep.get("added") or rep.get("updated"), rep
    bodies = [b for _m, p, b in sent if p.endswith("/keys/import")]
    assert bodies and isinstance(bodies[0], dict), sent
    assert "cleanup" not in bodies[0], f"import_keys put `cleanup` on the wire: {sorted(bodies[0])}"
    assert "cleanup" not in (registry.tool_by_name("import_keys").input_schema["properties"]), (
        "cleanup is offered"
    )
    after_import = call("list_keys", project_id=kpid, limit=50)
    names = {k["name"] for k in after_import["keys"]}
    assert {"app.title", "app.subtitle", "app.footer"} <= names, names
    print(
        "  import_keys: merged (%d keys now, incl. app.subtitle which the file "  # noqa: UP031
        "did NOT contain and a cleanup import would have retired); no `cleanup` "
        "on the wire and none on the schema" % len(names)
    )

    exported = call("export_keys", project_id=kpid, locale="de-DE")
    assert os.path.isfile(exported["path"]), exported
    assert os.path.commonpath(
        [os.path.abspath(out_dir), os.path.abspath(exported["path"])]
    ) == os.path.abspath(out_dir), "export escaped the output dir"
    with open(exported["path"], encoding="utf-8") as fh:
        assert "Localization OS DE" in fh.read(), "the export lost the de-DE value"
    print(
        "  export_keys: %d bytes to %s (path back, bytes never in the tool result)"  # noqa: UP031
        % (exported["bytes"], os.path.basename(exported["path"]))
    )

    # K9 (live-QA F5): the status/empty_as/archived filters were the fourth-surface
    # parity gap -- MCP offered only project_id/locale/fmt/namespace while the route,
    # the CLI and the web form all took the filters too. Proved through the REAL client
    # path (`validated` -> mcp.call_tool -> the pydantic arg model built from the
    # registry schema), so it also proves the SCHEMA offers them and the arg model
    # accepts them; the recorded params prove they reach the wire.
    assert {"status", "empty_as", "archived"} <= set(
        registry.tool_by_name("export_keys").input_schema["properties"]
    ), "export_keys schema must offer the status/empty_as/archived filters (K9 F5)"
    sent_params = []
    real_call_k9 = mcp_server._call

    async def recording_params(client, method, path, **kw):
        sent_params.append((path, kw.get("params")))
        return await real_call_k9(client, method, path, **kw)

    mcp_server._call = recording_params
    try:
        validated(
            "export_keys",
            {
                "project_id": kpid,
                "locale": "de-DE",
                "status": "mt",
                "empty_as": "base",
                "archived": True,
            },
        )
    finally:
        mcp_server._call = real_call_k9
    k9p = [p for path, p in sent_params if path.endswith("/keys/export")]
    assert k9p, ("export_keys made no GET to the export route", sent_params)
    assert k9p[0].get("status") == "mt", k9p
    assert k9p[0].get("empty_as") == "base", k9p
    assert k9p[0].get("archived") == "1", k9p
    print(
        "  export_keys: status/empty_as/archived accepted by the arg model and "
        "forwarded to the route (K9 F5)"
    )

    # --- the Delta loop -----------------------------------------------------
    boot = call("key_changes", project_id=kpid)
    assert boot["entries"] and boot["resync_required"] is False, boot
    cursor = boot["next_cursor"]
    assert cursor and cursor > 0, boot
    ops = {e["op"] for e in boot["entries"]}
    assert ops, boot
    quiet = call("key_changes", project_id=kpid, since=cursor)
    assert quiet["entries"] == [] and quiet["has_more"] is False, quiet
    assert quiet["next_cursor"] == cursor, quiet
    call("upsert_keys", key_id=key_id, locale="de-DE", text="Localization OS DE v2")
    moved = call("key_changes", project_id=kpid, since=cursor)
    assert moved["entries"], "a write after the cursor produced no delta entry"
    assert moved["next_cursor"] > cursor, moved
    print(
        "  key_changes: bootstrap %d entries (ops %s) -> cursor %d -> nothing "  # noqa: UP031
        "twice -> one write -> %d new entr(ies), cursor %d"
        % (
            len(boot["entries"]),
            "/".join(sorted(ops)),
            cursor,
            len(moved["entries"]),
            moved["next_cursor"],
        )
    )

    # resync_required: a cursor below the pruned floor. The floor is a watermark
    # settings row, so raising it is exactly what retention pruning does.
    settings.set_many({keys_svc._PROJECT_FLOOR_PREFIX + str(kpid): str(cursor + 5)})
    try:
        stale = call("key_changes", project_id=kpid, since=1)
        assert stale["resync_required"] is True, stale
        assert stale["entries"] == [], stale
        assert "since=0" in str(stale.get("message", "")), stale
    finally:
        settings.set_many({keys_svc._PROJECT_FLOOR_PREFIX + str(kpid): "0"})
    print(
        "  key_changes: a cursor below the retained floor answers "
        "resync_required=true with no entries and an honest message - never a "
        "confident 'you are up to date'"
    )
    return kpid


# ============================================================================
# Wave 2 -- families E-I
# ============================================================================
def _plan_delta_ok(name: str, before: dict, after: dict, audit_rows_ok: bool = False) -> None:
    """Assert a plan wrote nothing - with one precisely-stated exception.

    app/main.py's access-audit middleware appends ONE auditlog row for EVERY
    non-GET request (metadata about the HTTP request itself, written whether
    the route succeeds, refuses or plans). start_evaluation's plan sends the
    one whitelisted plan-mode POST, so its census legitimately shows exactly
    auditlog+1 and NOTHING else - a spend would also move spendledger, evalrun,
    evalscore or job, and those staying flat is the actual claim."""
    if after == before:
        return
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert audit_rows_ok, f"{name}'s plan wrote to the database: {diff}"
    assert set(diff) == {"auditlog"} and after["auditlog"] - before["auditlog"] == 1, (
        f"{name}'s plan wrote beyond the middleware's single request-audit row: {diff}"
    )


def _spend_probe(name: str, args: dict, audit_rows_ok: bool = False) -> None:
    """BOTH seams of the hostile-confirm half of the two-phase proof for one spend
    tool. A string that merely looks like consent must PLAN, not spend, whether it
    arrives raw (truthy-in-Python, the wave-1 money bug) or through FastMCP's own
    arg model (the path a real client takes; pydantic coerces it to boolean
    False). Nothing may land in any watched table on either seam.

    Wave 1's lesson (encoded in `call` vs `via_mcp`): the money contract must be
    proven through the arg model, with the raw seam as an ADDITIONAL probe -- so
    this runs the validated seam per-tool too, not only the raw dispatcher."""
    # raw dispatcher: every one of these is truthy in Python and would have spent
    # before the `confirm is not True` gate.
    for bogus in ("false", "0", "no"):
        before = _counts()
        got = call(name, **{**args, "confirm": bogus})
        assert got.get("plan") is True and got.get("spent") is False, (
            f"{name} spent on confirm={bogus!r} (raw seam): {got}"
        )
        _plan_delta_ok(
            f"{name} confirm={bogus!r} (raw)",
            before,
            _counts(),
            audit_rows_ok,  # noqa: RUF100, UP032
        )
    # the arg-model seam -- THE CLIENT PATH. pydantic coerces these to boolean
    # False, so they must PLAN here too; proven per-tool, not only on the raw seam.
    for coerced in ("false", "0", "no", "off"):
        before = _counts()
        got = validated(name, {**args, "confirm": coerced})
        assert got.get("plan") is True and got.get("spent") is False, (
            f"{name} spent on confirm={coerced!r} (arg model): {got}"
        )
        _plan_delta_ok(
            f"{name} confirm={coerced!r} (validated)",
            before,
            _counts(),
            audit_rows_ok,  # noqa: RUF100, UP032
        )


def check_213_run_conflict_relay(pid: int, out_dir: str) -> None:
    """GitHub #213 fix round 1: MCP RELAYS the run_conflict 409. A run of workflow A
    holds a (document, locale); a client-driven `run_workflow` of workflow B there must
    surface the server's refusal sentence -- run id, workflow name, both cancel paths
    -- as a ToolError, plus the family's standing "(Nothing was started...)" tail, and
    write no row. Same sentence every other surface prints."""
    from datetime import datetime

    from app.models import RunStatus, WorkflowRun
    from app.services import workflows as wf_svc

    with Session(engine) as s:
        rival = wf_svc.create_workflow(s, "MCP213 Rival")
        launch = wf_svc.create_workflow(s, "MCP213 Launch")
        for wf in (rival, launch):
            wf_svc.add_step(
                s,
                wf.id,
                name="Translator",
                provider="claude",
                model="m1",
                prompt_text="Translate {text}",
                chunk_policy="none",
            )
        rival_id, launch_id = rival.id, launch.id
    src = os.path.join(tempfile.mkdtemp(prefix="mcp213_"), "conflict.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("Alpha line.\n")
    staged = call("submit_translation", file_path=src, targets=[], project_id=pid, confirm=True)
    doc_id = staged["document_id"]
    with Session(engine) as s:
        held = WorkflowRun(
            workflow_id=rival_id,
            document_id=doc_id,
            target_locale="de-DE",
            status=RunStatus.RUNNING,
            created_by="mcp213",
            started_at=datetime.utcnow(),
        )
        s.add(held)
        s.commit()
        held_id = held.id
    try:
        msg = expect_error(
            "run_workflow",
            {
                "mode": "run",
                "workflow_id": launch_id,
                "document_id": doc_id,
                "locale": "de-DE",
                "confirm": True,
            },
            "already running",
            "MCP relays the run_conflict 409 naming the running run",
        )
        assert f"run #{held_id}" in msg, msg
        assert "Nothing was started" in msg, msg
        with Session(engine) as s:
            rows = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == launch_id)).all()
        assert not rows, f"the refused MCP launch wrote a row: {[x.id for x in rows]}"
    finally:
        with Session(engine) as s:
            row = s.get(WorkflowRun, held_id)
            if row is not None:
                row.status = RunStatus.CANCELLED
                row.finished_at = datetime.utcnow()
                s.add(row)
                s.commit()
    print(
        "  #213 fix 1: MCP relays the run_conflict 409 verbatim (naming the running "
        "run and the cancel path) and writes no row"
    )


def check_workflows(pid: int, out_dir: str) -> int:
    from app.services import workflows as wf_svc

    with Session(engine) as s:
        wf = wf_svc.create_workflow(s, "MCP Wave2 Flow")
        wf_svc.add_step(
            s,
            wf.id,
            name="Translator",
            provider="claude",
            model="m1",
            prompt_text="Translate {text}",
            chunk_policy="none",
        )
        wf_id = wf.id
    listing = call("list_workflows")
    assert any(w["id"] == wf_id for w in listing["workflows"]), listing
    shown = call("list_workflows", workflow_id=wf_id)
    steps = shown["workflow"]["steps"]
    assert len(steps) == 1 and steps[0]["provider"] == "claude", shown
    print(
        "  list_workflows: catalogue + one workflow's step graph (provider {!r})".format(
            steps[0]["provider"]
        )
    )

    src = os.path.join(tempfile.mkdtemp(prefix="mcp_w2_"), "flow.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("Alpha line.\nBeta line.\n")
    staged = call("submit_translation", file_path=src, targets=[], project_id=pid, confirm=True)
    doc_id = staged["document_id"]

    # --- two-phase, through the arg model, census-proven ---------------------
    before = _counts()
    plan = validated(
        "run_workflow",
        {"mode": "run", "workflow_id": wf_id, "document_id": doc_id, "locale": "de-DE"},
    )
    assert _counts() == before, "an unconfirmed run_workflow wrote to the database"
    assert plan["plan"] is True and plan["spent"] is False, plan
    assert plan["scope"]["workflow"]["id"] == wf_id, plan["scope"]
    assert plan["scope"]["document"]["locales"].get("de-DE"), plan["scope"]
    assert any("done:false" in w and "work_status" in w for w in plan["warnings"]), plan["warnings"]
    engines = plan["engine"]["resolved"]
    assert engines and engines[0]["provider"] == "claude", plan["engine"]
    _spend_probe(
        "run_workflow",
        {"mode": "run", "workflow_id": wf_id, "document_id": doc_id, "locale": "de-DE"},
    )
    bad_locale = validated(
        "run_workflow",
        {"mode": "run", "workflow_id": wf_id, "document_id": doc_id, "locale": "xx-XX"},
    )
    assert any("would fail before spending" in w for w in bad_locale["warnings"]), bad_locale[
        "warnings"
    ]
    print(
        "  run_workflow WITHOUT confirm: plan names the step engines, the "
        "document's segment counts and the bounded-wait warning; bogus confirm "
        "strings plan; a locale not on the document is warned before any spend"
    )

    ran = validated(
        "run_workflow",
        {
            "mode": "run",
            "workflow_id": wf_id,
            "document_id": doc_id,
            "locale": "de-DE",
            "confirm": True,
        },
    )
    assert ran["mode"] == "run" and ran.get("run_id"), ran
    assert str(ran.get("status", "")).lower() == "completed", ran
    run_id = ran["run_id"]

    batch = validated(
        "run_workflow",
        {
            "mode": "run_batch",
            "workflow_id": wf_id,
            "document_id": doc_id,
            "locales": ["de-DE", "fr-FR"],
            "confirm": True,
        },
    )
    assert batch.get("id") and batch.get("counts"), batch
    batch_id = batch["id"]
    rerun = validated(
        "run_workflow", {"mode": "rerun", "batch_id": batch_id, "locale": "de-DE", "confirm": True}
    )
    assert rerun["mode"] == "rerun" and rerun.get("members"), rerun
    noop = validated(
        "run_workflow", {"mode": "retry_failed", "batch_id": batch_id, "confirm": True}
    )
    assert noop.get("retried") == [], noop
    print(
        "  run_workflow WITH confirm: run %d COMPLETED, batch %d over 2 "  # noqa: UP031
        "locales, rerun landed, retry_failed with nothing failed is a no-op "
        "(retried=[])" % (run_id, batch_id)
    )

    st_run = call("work_status", run_id=run_id, detail=True)
    assert st_run["run_id"] == run_id and st_run["status"], st_run
    assert "lqa_findings" in st_run, "detail=true did not attach findings"
    st_batch = call("work_status", batch_id=batch_id, detail=True)
    assert st_batch.get("members"), st_batch
    st_doc = call("work_status", document_id=doc_id, detail=True)
    seg_detail = st_doc["locales"]["de-DE"]["segment_details"]
    assert seg_detail and "qa_flags" in seg_detail[0], seg_detail
    expect_error("work_status", {}, "exactly one", "work-status-ambiguous")
    expect_error(
        "work_status",
        {"run_id": run_id, "batch_id": batch_id},
        "exactly one",
        "work-status-two-ids",
    )
    print(
        "  work_status: run / batch / document views; detail attaches "
        "step-level and per-segment rows incl. qa_flags; zero or two ids are "
        "refused before any call"
    )

    # --- workflow AUTHORING tools (SPEC-workflow-authoring-api) ---------------
    # create_workflow builds the graph in ONE call; the LAST step omits {text} and
    # must be REPORTED (per-step accounting), not silently dropped, while the rest land.
    built = validated(
        "create_workflow",
        {
            "name": "MCP Authored Flow",
            "scope": "global",
            "steps": [
                {
                    "step_type": "llm_agent",
                    "prompt_text": "Translate into {target_locale}: {text}",
                    "key": "tr",
                },
                {"step_type": "lqa", "name": "check"},
                {"step_type": "llm_agent", "prompt_text": "no source token here"},
            ],
        },
    )
    awf = built["id"]
    assert built["steps_added"] == 2 and built["steps_failed"] == 1, built
    assert built["steps"][2]["ok"] is False and "{text}" in built["steps"][2]["error"], built[
        "steps"
    ]
    graph = call("list_workflows", workflow_id=awf)["workflow"]["steps"]
    assert [g["type"] for g in graph] == ["llm_agent", "lqa"], graph
    tr_id = graph[0]["id"]
    print(
        "  create_workflow: workflow + 2 steps land, the {text}-less step is "
        "reported (per-step accounting), the rest still land"
    )

    # edit_workflow: add_step, edit_step (PARTIAL + three-state bool), move, delete.
    added = call(
        "edit_workflow",
        action="add_step",
        workflow_id=awf,
        fields={
            "step_type": "llm_agent",
            "prompt_text": "Refine {prior} for {target_locale}",
            "key": "fix",
        },
    )
    fix_id = added["id"]
    assert added["step_type"] == "llm_agent" and added["key"] == "fix", added
    edited = call(
        "edit_workflow",
        action="edit_step",
        workflow_id=awf,
        step_id=tr_id,
        fields={"name": "Translator", "qe_gate": True, "qe_threshold": 90},
    )
    assert edited["name"] == "Translator" and "qe_gate" in edited["changed"], edited
    moved = call("edit_workflow", action="move_step", workflow_id=awf, step_id=fix_id, before="tr")
    assert moved["order"][0]["id"] == fix_id, moved
    deleted = call("edit_workflow", action="delete_step", workflow_id=awf, step_id=fix_id)
    assert deleted["deleted"] is True, deleted
    # An unknown field is REFUSED by name (not dropped); an unknown action too.
    expect_error(
        "edit_workflow",
        {"action": "edit_step", "workflow_id": awf, "step_id": tr_id, "fields": {"bogus_field": 1}},
        "does not accept",
        "edit-workflow-unknown-field",
    )
    expect_error(
        "edit_workflow",
        {"action": "frobnicate", "workflow_id": awf},
        "unknown action",
        "edit-workflow-bad-action",
    )
    # #630: a workflow's locked prompts over MCP: read, set, restore default (the System prompt
    # overrides feature must be on; the routes own the gate and the audit row).
    settings.set_many({"system_prompt_overrides_enabled": "1"})
    settings.invalidate_cache()
    try:
        heads = call("edit_workflow", action="get_system_prompts", workflow_id=awf)
        assert [e["key"] for e in heads["editable"]] == ["step_head", "mt_head"], heads
        text = "Be formal. Output only the result."
        saved = call(
            "edit_workflow",
            action="set_system_prompt",
            workflow_id=awf,
            key="step_head",
            body=text,
        )
        assert saved["action"] == "set_system_prompt" and saved["version"] == 1, saved
        shown = call("edit_workflow", action="get_system_prompts", workflow_id=awf)
        own = {e["key"]: e for e in shown["editable"]}["step_head"]
        assert own["override"]["body"] == text and own["effective_source"] == "workflow", own
        back = call(
            "edit_workflow",
            action="restore_default_system_prompt",
            workflow_id=awf,
            key="step_head",
        )
        assert back["changed"] is True, back
        shown = call("edit_workflow", action="get_system_prompts", workflow_id=awf)
        assert {e["key"]: e for e in shown["editable"]}["step_head"]["override"] is None
        expect_error(
            "edit_workflow",
            {"action": "set_system_prompt", "workflow_id": awf, "body": "x"},
            "needs key",
            "edit-workflow-prompt-key",
        )
        expect_error(
            "edit_workflow",
            {"action": "set_system_prompt", "workflow_id": awf, "key": "mt_head"},
            "needs body",
            "edit-workflow-prompt-body",
        )
        # a step prompt: an empty prompt_text is the restore-default for that step
        cleared = call(
            "edit_workflow",
            action="edit_step",
            workflow_id=awf,
            step_id=tr_id,
            fields={"prompt_text": ""},
        )
        assert "prompt_text" in cleared["changed"], cleared
    finally:
        settings.set_many({"system_prompt_overrides_enabled": "0"})
        settings.invalidate_cache()
    print("  edit_workflow: get / set / restore default of a workflow's prompts land (#630)")
    # A {text}-less edit is refused with the canonical message (route validation parity).
    expect_error(
        "edit_workflow",
        {
            "action": "edit_step",
            "workflow_id": awf,
            "step_id": tr_id,
            "fields": {"prompt_text": "no source"},
        },
        "{text}",
        "edit-workflow-prompt-rule",
    )
    print(
        "  create_workflow / edit_workflow: add_step, edit_step (PARTIAL + "
        "three-state bool), move_step, delete_step land; unknown field, unknown "
        "action and the {text}-less prompt are each refused"
    )
    return wf_id


def check_assets(pid: int, out_dir: str) -> int:
    customers = call("list_projects")["customers"]
    cust_id = next(c["id"] for c in customers if c["name"] == "Wave One Ltd")

    made = call("edit_glossary", action="create", name="MCP Wave2 Glossary", customer_id=cust_id)
    gid = made["id"]
    assert made["status"] == "draft", made
    call("edit_glossary", action="set_pairs", glossary_id=gid, pairs=["en-US:de-DE"])
    term = call(
        "edit_glossary",
        action="add_term",
        glossary_id=gid,
        source_term="Localization OS",
        target_term="Localization OS",
        source="en-US",
        target="de-DE",
        dnt=True,
        meta={"part_of_speech": "noun"},
    )
    assert term["dnt"] is True and term["meta"].get("part_of_speech") == "noun", term

    # THE SILENT-DROP REFUSAL: the app's clean_meta would silently discard an
    # unknown meta key; the tool refuses it by name instead, so an agent can
    # never report success for metadata that was never stored.
    msg = expect_error(
        "edit_glossary",
        {
            "action": "add_term",
            "glossary_id": gid,
            "source_term": "x",
            "meta": {"is_archived": True},
        },
        "does not accept",
        "meta-allowlist",
    )
    assert "Allowed:" in msg and "part_of_speech" in msg, msg

    shown = call("list_assets", view="glossary", glossary_id=gid)
    entry_id = shown["glossary"]["terms"][0]["id"]
    edited = call(
        "edit_glossary",
        action="edit_term",
        glossary_id=gid,
        entry_id=entry_id,
        target_term="LocalizationOS DE",
    )
    # PRESENT-ONLY SEMANTICS: the edit above did not mention dnt, so the flag
    # set at add_term must survive - sending a default dnt=false would have
    # silently cleared a do-not-translate decision.
    assert edited["dnt"] is True and edited["target_term"] == "LocalizationOS DE", edited
    approved = call("edit_glossary", action="approve", glossary_id=gid)
    assert approved["status"] == "approved", approved
    imported = call(
        "edit_glossary",
        action="import",
        glossary_id=gid,
        data="source_term,target_term\nBeta,Beta-DE\n",
        filename="terms.csv",
        source="en-US",
        target="de-DE",
    )
    assert imported.get("entries") == 1, imported
    # import is a content edit -> approval reverted; approve again via tool
    call("edit_glossary", action="approve", glossary_id=gid)

    # Candidates: seed one directly (extraction is exercised separately) so the
    # accept/drop verbs run against a real pending row.
    from app.models import TermCandidate

    with Session(engine) as s:
        s.add(
            TermCandidate(
                glossary_id=gid,
                source_term="Gamma",
                proposed_target="Gamma-DE",
                source_locale="en-US",
                target_locale="de-DE",
                frequency=2,
            )
        )
        s.add(
            TermCandidate(
                glossary_id=gid,
                source_term="Delta",
                proposed_target="Delta-DE",
                source_locale="en-US",
                target_locale="de-DE",
                frequency=1,
            )
        )
        s.commit()
    cands = call("list_assets", view="candidates", glossary_id=gid)["candidates"]
    assert len(cands) == 2, cands
    acc = call(
        "edit_glossary",
        action="accept_candidate",
        glossary_id=gid,
        candidate_id=cands[0]["id"],
        target_term="Gamma-DE!",
    )
    assert acc["target_term"] == "Gamma-DE!", acc
    call("edit_glossary", action="drop_candidate", glossary_id=gid, candidate_id=cands[1]["id"])
    left = call("list_assets", view="candidates", glossary_id=gid)["candidates"]
    assert left == [], f"accept/drop left pending candidates behind: {left}"

    exported = call("list_assets", view="glossary_export", glossary_id=gid, fmt="csv")
    assert os.path.isfile(exported["path"]), exported
    with open(exported["path"], encoding="utf-8") as fh:
        assert "Localization OS" in fh.read(), "the export lost the term"
    print(
        "  edit_glossary + list_assets: create -> pairs -> term (meta "
        "allowlisted, unknown keys REFUSED not dropped) -> edit (dnt "
        "survives an edit that never mentioned it) -> approve -> import "
        "(reverts approval) -> candidates accepted/dropped -> export to {}".format(
            os.path.basename(exported["path"])
        )
    )

    # --- extract_terms: two-phase, then a real run on the stub engine --------
    plan = validated("extract_terms", {"glossary_id": gid, "document_id": 1})
    assert plan["plan"] is True, plan
    assert plan["scope"]["glossary"]["id"] == gid, plan["scope"]
    _spend_probe("extract_terms", {"glossary_id": gid, "document_id": 1})
    docs = call("list_projects", view="documents", project_id=pid)["documents"]
    ext = validated(
        "extract_terms",
        {"glossary_id": gid, "document_id": docs[0]["id"], "target": "de-DE", "confirm": True},
    )
    assert "proposed" in ext and ext.get("blocked") is False, ext
    print(
        "  extract_terms: plan names the glossary; confirmed run against the "
        "stub engine answered proposed={} extracted={} (a 200 that used to be "
        "a 500 - the route read doc.id after its session closed)".format(
            ext["proposed"], ext["extracted"]
        )
    )

    # --- style guides ---------------------------------------------------------
    guide = call(
        "manage_style_guide",
        action="create",
        name="MCP Wave2 Guide",
        body="Be terse.",
        customer_id=cust_id,
    )
    sgid = guide["id"]
    assert guide["status"] == "draft", guide
    upd = call("manage_style_guide", action="update", style_guide_id=sgid, body="Be very terse.")
    assert upd["status"] == "draft", upd
    ok = call("manage_style_guide", action="set_status", style_guide_id=sgid, status="approved")
    assert ok["status"] == "approved", ok
    guides = call("list_assets", view="style_guides")["style_guides"]
    assert any(g["id"] == sgid for g in guides), guides
    one = call("list_assets", view="style_guide", style_guide_id=sgid)
    assert one["style_guide"]["body"] == "Be very terse.", one
    print(
        "  manage_style_guide: create -> update (stays draft) -> approved; "
        "list_assets reads both guide views"
    )
    return cust_id


def check_tm(pid: int, cust_id: int, out_dir: str) -> None:
    # More than one customer exists (Wave One Ltd, Second Ltd), so the browse
    # MUST answer needs_customer rather than dump every customer's TM.
    first = call("search_tm")
    assert first.get("needs_customer") is True and first.get("customers"), first
    page = call("search_tm", customer_id=cust_id)
    assert page.get("entries"), "no TM entries visible although wave 1 wrote them"
    entry = page["entries"][0]
    stats = call("search_tm", view="stats", customer_id=cust_id)
    assert stats.get("total") or stats.get("entries") or stats.get("pairs"), stats
    one = call("search_tm", view="entry", entry_id=entry["id"])
    assert one["entry"]["id"] == entry["id"], one
    print(
        "  search_tm: needs_customer with %d to pick from, then %d entr(ies), "  # noqa: UP031
        "stats, and one entry by id" % (len(first["customers"]), len(page["entries"]))
    )

    edited = call("manage_tm", action="edit", entry_id=entry["id"], target_text="Zweiter Satz!!")
    assert edited["target_text"] == "Zweiter Satz!!", edited
    tmx = (
        '<?xml version="1.0"?><tmx version="1.4"><header srclang="en-US"/>'
        '<body><tu><tuv xml:lang="en-US"><seg>Imported sentence.</seg></tuv>'
        '<tuv xml:lang="de-DE"><seg>Importierter Satz.</seg></tuv></tu>'
        "</body></tmx>"
    )
    imp = call(
        "manage_tm",
        action="import",
        project_id=pid,
        data=tmx,
        source_locale="en-US",
        target_locale="de-DE",
    )
    assert imp.get("imported") == 1, imp
    exp = call(
        "manage_tm", action="export", project_id=pid, source_locale="en-US", target_locale="de-DE"
    )
    assert os.path.isfile(exp["path"]), exp
    with open(exp["path"], encoding="utf-8") as fh:
        body = fh.read()
    assert "Importierter Satz." in body and "Zweiter Satz!!" in body, (
        "the TMX export lost the imported or edited entry"
    )
    print(
        "  manage_tm: edit landed, TMX import (1 unit), TMX export to {} "
        "carries both the edit and the import".format(os.path.basename(exp["path"]))
    )


def check_ops(out_dir: str) -> None:
    rep = call("run_report", kind="cost")
    assert "rows" in rep and "columns" in rep, rep
    expect_error("run_report", {"kind": "frobnicate"}, "unknown", "report-bad-kind")
    conns = call("list_connectors")
    assert conns["connections"] == [], conns
    imgs = call("list_images")
    assert imgs["images"] == [], imgs
    expect_error("list_images", {"document_id": 999999}, "not found", "image-missing-doc")
    print(
        "  ops reads: run_report (cost: %d row(s)); an unknown kind is a "  # noqa: UP031
        "clean 400; list_connectors and list_images answer honest empties" % len(rep["rows"])
    )


def check_connectors() -> None:
    """R4 connector tools: the NEVER_COVER amendment, proven end-to-end HERMETICALLY.

    Every external egress is a STUB (the connector-suite pattern: monkeypatch the
    conn_store flow before the confirmed call), so no real Jira/Airtable is touched -
    connector live QA is maintainer-gated. What this proves against the real app + real
    dispatcher: connector_status reads; connector_pull writes additively; each
    two-phase spend tool's UNCONFIRMED call returns a plan that NAMES the external
    target and SURFACES the allow_mock_egress posture while writing NOTHING (census);
    a hostile confirm string still plans; a CONFIRMED call executes; and an absent/
    foreign cid 404s through the tool exactly as through /api (no cross-tenant leak in
    the plan's enrichment read)."""
    import types

    from app.services import jobs
    from app.services.connectors import store as conn_store

    conn = conn_store.create(
        "jira",
        "MCP Jira",
        {"base_url": "https://example.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        actor="mcp",
    )
    cid = conn.id

    # --- connector_status (read): health probe + the caller's roster ----------
    st = call("connector_status")
    assert st["healthy"] is True and "jira" in st["connector_kinds"], st
    assert any(c["id"] == cid and c["kind"] == "jira" for c in st["connections"]), st

    # --- connector_pull (WRITE, additive): stub the egress, prove it relays ----
    real_pull = conn_store.pull_connection
    set_pieces(
        "app.services.connectors.store",
        "pull_connection",
        lambda c, actor="", transport=None: (True, "pulled 3 issues", 4321),
    )
    try:
        pulled = call("connector_pull", connection_id=cid)
    finally:
        set_pieces("app.services.connectors.store", "pull_connection", real_pull)
    assert pulled["connection_id"] == cid and pulled.get("document_id") == 4321, pulled

    # --- the four two-phase spend tools ---------------------------------------
    # connector_translate: plan names the external target + mock-egress, writes nothing
    before = _counts()
    plan = validated(
        "connector_translate",
        {"connection_id": cid, "item_ids": ["LOC-1"], "target_locales": ["de-DE"]},
    )
    assert _counts() == before, "an unconfirmed connector_translate wrote to the DB"
    assert plan["plan"] is True and plan["spent"] is False, plan
    assert (
        plan["scope"]["external_target"]["kind"] == "jira"
        and plan["scope"]["external_target"]["connection_id"] == cid
    ), plan
    assert "allow_mock_egress" in json.dumps(plan["scope"]["mock_egress"]), plan
    _spend_probe("connector_translate", {"connection_id": cid, "item_ids": ["LOC-1"]})
    real_jt = conn_store.jira_translate
    set_pieces(
        "app.services.connectors.store",
        "jira_translate",
        (
            lambda c, issue_keys=None, jql=None, target_locales=None, actor="", transport=None, only_pairs=None: (  # noqa: E501
                True,
                "translated 1 issue",
                {"written": 1},
            )
        ),
    )
    try:
        done = validated(
            "connector_translate",
            {
                "connection_id": cid,
                "item_ids": ["LOC-1"],
                "target_locales": ["de-DE"],
                "confirm": True,
            },
        )
    finally:
        set_pieces("app.services.connectors.store", "jira_translate", real_jt)
    assert done["connection_id"] == cid and done.get("written") == 1, done

    # connector_push
    before = _counts()
    pplan = validated("connector_push", {"connection_id": cid})
    assert _counts() == before, "an unconfirmed connector_push wrote to the DB"
    assert pplan["scope"]["external_target"][
        "kind"
    ] == "jira" and "allow_mock_egress" in json.dumps(pplan["scope"]["mock_egress"]), pplan
    _spend_probe("connector_push", {"connection_id": cid})
    real_push = conn_store.push_connection
    set_pieces(
        "app.services.connectors.store",
        "push_connection",
        lambda c, actor="", transport=None: (True, "pushed 2", 2),
    )
    try:
        pdone = validated("connector_push", {"connection_id": cid, "confirm": True})
    finally:
        set_pieces("app.services.connectors.store", "push_connection", real_push)
    assert pdone.get("pushed") == 2, pdone

    # connector_sync (synchronous path -> conn_store.translate_now)
    before = _counts()
    splan = validated("connector_sync", {"connection_id": cid})
    assert _counts() == before, "an unconfirmed connector_sync wrote to the DB"
    assert splan["scope"]["external_target"]["kind"] == "jira", splan
    _spend_probe("connector_sync", {"connection_id": cid})
    real_tn = conn_store.translate_now
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda c, actor="", transport=None: (True, "synced", {"written": 3}),
    )
    try:
        sdone = validated("connector_sync", {"connection_id": cid, "confirm": True})
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_tn)
    assert sdone.get("written") == 3, sdone

    # connector_retry (enqueues a retry job; stub jobs.get + submit_unique)
    before = _counts()
    rplan = validated("connector_retry", {"connection_id": cid, "job_id": 555})
    assert _counts() == before, "an unconfirmed connector_retry wrote to the DB"
    assert rplan["scope"].get("retry_job_id") == 555, rplan
    _spend_probe("connector_retry", {"connection_id": cid, "job_id": 555})
    fake_job = types.SimpleNamespace(
        id=555,
        kind="connector_translate",
        payload={"connection_id": cid},
        result={"failures": [{"issue_key": "LOC-2", "locale": "de-DE", "error": "400"}]},
        created_by="mcp",
    )
    real_get, real_su = jobs.get, jobs.submit_unique
    jobs.get = lambda j: fake_job if int(j) == 555 else real_get(j)
    jobs.submit_unique = lambda kind, payload, actor="": (777, True)
    try:
        rdone = validated("connector_retry", {"connection_id": cid, "job_id": 555, "confirm": True})
    finally:
        jobs.get, jobs.submit_unique = real_get, real_su
    assert rdone.get("job_id") == 777, rdone

    # --- foreign / absent cid: 404 through the tool, and no leak in the plan ---
    expect_error("connector_pull", {"connection_id": 999999}, "not found", "connector-absent-cid")
    expect_error(
        "connector_translate",
        # item_ids so the #757 plan (which refuses an id-less run) can be made first
        {"connection_id": 999999, "item_ids": ["LOC-1"], "confirm": True},
        "not found",
        "connector-translate-absent-cid",
    )
    ghost = validated("connector_sync", {"connection_id": 999999})
    assert ghost["plan"] is True, ghost
    assert ghost["scope"]["external_target"] == {"note": "resolved at execution"}, (
        "the plan leaked a connection it cannot see: {}".format(ghost["scope"]["external_target"])
    )
    assert any("not visible" in w for w in ghost["warnings"]), ghost

    print(
        "  connectors (R4): connector_status reads health+roster; connector_pull "
        "writes additively; connector_translate/push/sync/retry each PLAN (naming "
        "the external target + the allow_mock_egress posture) and write nothing on "
        "an unconfirmed or hostile-confirm call, then execute against a stubbed "
        "connector; an absent cid 404s through the tool and never leaks into a plan"
    )


def check_workflow_groups(wf_id: int) -> None:
    """manage_workflow_group (R1b, write): create/list/show/add/set-default round-trip
    over the write tool; an unknown action is a clean error."""
    created = call("manage_workflow_group", action="create", name="MCP Group")
    gid = created["id"]
    listing = call("manage_workflow_group", action="list")
    assert any(g["id"] == gid for g in listing["groups"]), listing
    added = call(
        "manage_workflow_group",
        action="add",
        group_id=gid,
        workflow_id=wf_id,
        pair="en-US:de-DE",
        make_default=True,
    )
    assert added["workflow_id"] == wf_id, added
    shown = call("manage_workflow_group", action="show", group_id=gid)
    assert any(v["workflow_id"] == wf_id for v in shown["variants"]), shown
    dflt = call("manage_workflow_group", action="set-default", group_id=gid, workflow_id=wf_id)
    assert dflt["is_default"] is True, dflt
    expect_error(
        "manage_workflow_group",
        {"action": "frobnicate", "group_id": gid},
        "unknown action",
        "group-bad-action",
    )
    print(
        "  manage_workflow_group: create/list/show/add/set-default round-trip; "
        "an unknown action is a clean 400"
    )


def check_clone_workflow(wf_id: int) -> None:
    """clone_workflow (R1b, write): deep-copies a workflow + its steps, bills nothing."""
    cloned = call("clone_workflow", workflow_id=wf_id)
    assert cloned["id"] != wf_id and cloned["steps"] >= 1, cloned
    assert cloned["source_id"] == wf_id, cloned
    print(
        "  clone_workflow: deep-copies a workflow (#%d -> #%d, %d step(s))"  # noqa: UP031
        % (wf_id, cloned["id"], cloned["steps"])
    )


def check_translate_image() -> None:
    """translate_image (R1b, SPEND): the two-phase proof. An UNCONFIRMED call plans and
    writes NOTHING (census); a CONFIRMED one translates. The image is ingested directly
    (there is no upload MCP tool -- upload is /api-only). The project's provider is the
    LiveStubProvider-backed `claude`, so the mock-409 does not fire."""
    from app.services import content
    from app.services import image_localization as il_svc

    settings.set_many({"image_localization_enabled": "1"})
    svg = (
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
        b'width="200" height="80"><text x="10" y="40">Hello world</text></svg>'
    )
    with Session(engine) as s:
        p = content.project_create(
            s,
            "MCP Img",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=None,
            creator=None,
            default_provider="claude",
        )
        doc = il_svc.ingest_image(s, p, "mcp.svg", svg, actor="mcp")
        doc_id = doc.id
    before = _counts()
    plan = validated("translate_image", {"document_id": doc_id, "locale": "de-DE"})
    assert _counts() == before, "an unconfirmed translate_image wrote to the database"
    assert plan["plan"] is True and plan["spent"] is False, plan
    assert plan["scope"]["document_id"] == doc_id, plan["scope"]
    _spend_probe("translate_image", {"document_id": doc_id, "locale": "de-DE"})
    done = validated("translate_image", {"document_id": doc_id, "locale": "de-DE", "confirm": True})
    assert done.get("document_id") == doc_id and done.get("locale") == "de-DE", done
    print(
        "  translate_image WITHOUT confirm: plans and writes nothing (census); a "
        "hostile confirm string plans; a CONFIRMED call translates region(s)"
    )


def check_wave2_kill_switches(cust_id: int) -> None:
    """Kill-switch REALITY per family - not the keys family's 404 story
    retold, but what each gate actually does, asserted so the registry's
    descriptions cannot drift from the product."""
    # images: 404-style, indistinguishable, switch named for the operator
    settings.set_many({"image_localization_enabled": "0"})
    try:
        msg = expect_error("list_images", {}, "not found", "images-gate-off")
        assert "image_localization_enabled" in msg, msg
        assert "403" not in msg, msg
    finally:
        settings.set_many({"image_localization_enabled": "1"})

    print("  wave-2 kill switches: images relay a 404 naming the switch")


def check_scopes(
    docs_key: str, keys_key: str, api_key: str, kpid: int, pid: int, all_key: str
) -> None:
    """EVERY tool, held against a token that lacks the grant it declares.

    Systematic rather than sampled: the registry says which grant each tool needs,
    so the test can hand each tool the WRONG key and demand a real 403 from the
    real gate. Arguments are minimal-but-structurally-valid, because the scope gate
    runs before any body validation - a refusal that came from a bad argument
    instead of the gate would prove nothing.

    `job_status` is listed with NO job_id on purpose: polling one job needs only a
    base `api` key, and it is the wave-1 LISTING leg that is docs-gated. That is
    precisely why the tool declares `docs` (its widest route), and this is where
    that claim gets checked."""
    # #755: a document the upload route accepts (a .py is refused locally, before the
    # scope gate could answer), inside the allowed input folders.
    probe_doc = os.path.join(tempfile.gettempdir(), "scope-probe.txt")
    with open(probe_doc, "w", encoding="utf-8") as fh:
        fh.write("Scope probe.\n")
    minimal = {
        "release_manifest": {},
        "whoami": {},
        "list_languages": {},
        "list_capabilities": {},
        "list_projects": {},
        "create_project": {"name": "scope probe", "customer_id": 1},
        "configure_project": {"project_id": pid, "action": "rename", "name": "x"},
        "project_automation": {"project_id": pid, "action": "get"},
        "project_run": {"project_id": pid, "confirm": True},
        "submit_translation": {"file_path": probe_doc, "targets": [], "confirm": True},
        "job_status": {},
        "get_result": {"document_id": 1, "locale": "de-DE"},
        "apply_tm": {"document_id": 1, "locale": "de-DE"},
        "approve_segment": {"segment_id": 1},
        "translate_text": {"text": "hi", "target_locale": "de-DE", "confirm": True},
        "list_keys": {"project_id": kpid},
        "upsert_keys": {"project_id": kpid, "keys": []},
        "import_keys": {"project_id": kpid, "data": "{}"},
        "export_keys": {"project_id": kpid},
        "key_changes": {"project_id": kpid},
        # wave 2 -- every arg set is minimal-but-structurally-valid so the
        # refusal below can only come from the scope gate itself
        "list_workflows": {},
        "run_workflow": {"mode": "retry_failed", "batch_id": 1, "confirm": True},
        "work_status": {"run_id": 1},
        "create_workflow": {"name": "scope probe"},
        "edit_workflow": {"action": "delete_step", "workflow_id": 1, "step_id": 1},
        "cancel_workflow_run": {"run_id": 1},
        "list_assets": {},
        "edit_glossary": {"action": "approve", "glossary_id": 1},
        "extract_terms": {"glossary_id": 1, "document_id": 1, "confirm": True},
        "manage_style_guide": {"action": "set_status", "style_guide_id": 1, "status": "draft"},
        "manage_custom_fields": {"action": "list"},
        "set_project_fields": {"project_id": pid, "fields": {"scope_probe": "x"}},
        "search_tm": {},
        "search_workspace": {"q": "scope probe"},
        "manage_formality": {"action": "registers"},
        "project_notifications": {"action": "channels"},
        "manage_tm": {"action": "edit", "entry_id": 1, "target_text": "x"},
        "attach_tm_source": {"project_id": pid, "action": "list"},
        "pin_glossary": {"project_id": pid, "action": "list"},
        "translate_keys": {
            "project_id": kpid,
            "workflow_id": 1,
            "locale": "de-DE",
            "confirm": True,
        },
        # Batch 14 T1: set_primary / list go straight to their /api routes, so the wrong-
        # scope token trips the keys-scope gate (403) before any key resolution.
        "manage_key_image": {"action": "set_primary", "key_id": 1, "image_id": 1},
        "list_key_images": {"key_id": 1},
        "run_report": {},
        "list_connectors": {},
        "connector_status": {},
        "connector_pull": {"connection_id": 1},
        "connector_translate": {"connection_id": 1, "confirm": True},
        "connector_push": {"connection_id": 1, "confirm": True},
        "connector_sync": {"connection_id": 1, "confirm": True},
        "connector_retry": {"connection_id": 1, "job_id": 1, "confirm": True},
        # Wave E -- vendor quote/rework SPEND tools (docs scope). Structurally valid so the
        # only refusal is the scope gate itself (a wrong-scope key 403s before any leg touch).
        "vendor_approve_quote": {"did": 1, "confirm": True},
        "vendor_reject_rework": {"did": 1, "confirm": True},
        "list_images": {},
        "manage_workflow_group": {"action": "list"},
        "clone_workflow": {"workflow_id": 1},
        "translate_image": {"document_id": 1, "locale": "de-DE", "confirm": True},
        # Wave R2 -- admin & identity (write tier, docs scope). Structurally valid so
        # the only refusal is the scope gate itself (a wrong-scope key 403s in _gate
        # before any user/rule is touched).
        "manage_users": {"action": "disable", "user_id": 1},
        "manage_admin_ops": {"action": "toggle_rule", "rule_id": 1},
        # R3 -- config & credentials (write tier, admin scope). Structurally valid;
        # the admin-scope gate refuses a non-admin key before any config is touched.
        "manage_install": {"action": "get_config"},
        # Wave 3 -- queries (read/write, docs scope). Structurally valid so the only
        # refusal is the scope gate itself (a wrong-scope key 403s in _gate before any
        # thread is touched).
        "list_queries": {},
        "get_query": {"query_id": 1},
        "open_query": {"body": "x", "segment_id": 1},
        "reply_to_query": {"query_id": 1, "body": "x"},
        "set_query_status": {"query_id": 1, "status": "open"},
        "assign_query": {"query_id": 1},
        # Wave 3 -- worklist (read, docs scope). All params optional, so the empty call is
        # structurally valid and the only refusal is the scope gate itself.
        "my_work": {},
        # Wave 3 §6.5 reviewer-LQA-finding tools (core; the scope gate 403s a wrong-scope
        # key in _gate before any finding is touched — reads are doc-visibility scoped).
        "list_lqa_findings": {"document_id": 1},
        "log_lqa_finding": {
            "document_id": 1,
            "segment_id": 1,
            "dimension": "Accuracy",
            "severity": "major",
            "note": "x",
        },
        "edit_lqa_finding": {
            "finding_id": 1,
            "dimension": "Accuracy",
            "severity": "major",
            "note": "x",
        },
        "withdraw_lqa_finding": {"finding_id": 1},
        # run-190 Item C: the LLM Ledger bodies-withheld summary (read, docs scope). All
        # params optional -> the empty LIST call is structurally valid; the only refusal is
        # the scope gate itself (a wrong-scope key 403s before any capture is read).
        "ledger_summary": {},
        # ROADMAP #6: the LLM Ledger RUN-first bodies-withheld view (read, docs scope). All
        # params optional -> the empty LIST call is structurally valid; the only refusal is
        # the scope gate itself (a wrong-scope key 403s before any capture is read).
        "ledger_runs": {},
    }
    assert set(minimal) == {t.name for t in registry.TOOLS}, (
        "the scope probe does not cover every tool"
    )
    wrong_key = {"docs": keys_key, "keys": docs_key}
    probed = {"docs": 0, "keys": 0}
    for spec in registry.TOOLS:
        token = wrong_key.get(spec.scope)
        if token is None:
            continue  # scope="api": every key carries it, see below
        mcp_server.TOKEN = token
        msg = expect_error(spec.name, minimal[spec.name], "403", f"wrong-scope-{spec.name}")
        scope_words = {"docs": "document-scoped", "keys": "keys-scoped", "admin": "admin-scoped"}
        assert spec.scope in msg or scope_words.get(spec.scope, spec.scope) in msg, (
            f"{spec.name}: the 403 does not name the missing grant {spec.scope!r}: {msg}"
        )
        probed[spec.scope] += 1

    # scope="api" is the base grant every key carries, so it cannot be withheld by
    # scope at all -- translate_text is gated by ROLE instead. Say so rather than
    # leave a silent hole in the loop above.
    api_scoped = [t.name for t in registry.TOOLS if t.scope == "api"]
    mcp_server.TOKEN = api_key
    expect_error("list_capabilities", {}, "403", "api-key-on-discovery")

    mcp_server.TOKEN = ""
    expect_error("whoami", {}, "LOC_STUDIO_TOKEN", "no-token")
    mcp_server.TOKEN = all_key
    print(
        "  scopes: every tool refused by a token lacking its declared grant - "  # noqa: UP031
        "%d docs-scoped tools against a keys-only key, %d keys-scoped tools "
        "against a docs-only key, each 403 naming the grant. %s declare(s) the "
        "base `api` grant every key carries (role-gated, not scope-gated). An "
        "empty token never reaches the network."
        % (probed["docs"], probed["keys"], ", ".join(api_scoped))
    )


def check_kill_switch(kpid: int) -> None:
    settings.set_many({"key_projects_enabled": "0"})
    try:
        for name, args in (
            ("list_keys", {"project_id": kpid}),
            ("key_changes", {"project_id": kpid}),
            ("export_keys", {"project_id": kpid}),
            ("import_keys", {"project_id": kpid, "data": "{}"}),
            ("upsert_keys", {"project_id": kpid, "keys": []}),
        ):
            msg = expect_error(name, args, "not found", f"gate-off-{name}")
            assert "403" not in msg, (
                "a switched-off feature must not be distinguishable from a missing object: {}".format(  # noqa: E501, UP032
                    msg
                )
            )
            assert "key_projects_enabled" in msg, (
                f"the relayed 404 should still tell the operator which switch: {msg}"
            )
    finally:
        settings.set_many({"key_projects_enabled": "1"})
    print(
        "  kill switch: with key_projects_enabled off, all 5 key tools relay the "
        "API's 404 verbatim (never a 403) while naming the switch for a human"
    )


def check_schema_refusals() -> None:
    """Every tool that HAS an input refuses a bad one, at the arg model."""
    bad = {
        "whoami": {"card": ["llm"]},
        "list_projects": {"view": "everything"},
        "create_project": {},
        "configure_project": {"project_id": "seven", "action": "rename"},
        "project_automation": {"project_id": "seven", "action": "get"},
        "project_run": {"project_id": "seven"},
        "submit_translation": {"file_path": "/tmp/x", "targets": "de-DE"},
        "job_status": {"job_id": "abc"},
        "get_result": {"document_id": 1},
        "apply_tm": {"locale": "de-DE"},
        "approve_segment": {"segment_id": "abc"},
        "translate_text": {"text": "hi"},
        "list_keys": {"limit": "many"},
        "upsert_keys": {"keys": "not-a-list"},
        "import_keys": {},
        "export_keys": {"project_id": "abc"},
        "key_changes": {"since": []},
        # wave 2
        "list_workflows": {"workflow_id": "abc"},
        "run_workflow": {"mode": "everything"},
        "work_status": {"run_id": "abc"},
        "create_workflow": {"name": "x", "scope": "banana"},
        "edit_workflow": {"action": "add_step", "workflow_id": "abc"},
        "cancel_workflow_run": {"run_id": "abc"},
        "list_assets": {"view": "nope"},
        "edit_glossary": {},
        "extract_terms": {"glossary_id": 1},
        "manage_style_guide": {"action": "destroy"},
        "manage_custom_fields": {"action": "destroy"},
        "set_project_fields": {"project_id": "abc", "fields": {"k": "v"}},
        "search_tm": {"per_page": "many"},
        "search_workspace": {"q": "abc", "limit": "many"},
        "manage_formality": {"action": "destroy"},
        "project_notifications": {"action": "destroy"},
        "manage_tm": {},
        "attach_tm_source": {"project_id": "abc", "action": "list"},
        "pin_glossary": {"project_id": "abc", "action": "list"},
        "translate_keys": {"project_id": 1, "workflow_id": "abc", "locale": "de-DE"},
        # Batch 14 T1: a non-integer key_id is a schema type refusal on both image tools.
        "manage_key_image": {"action": "attach", "key_id": "abc"},
        "list_key_images": {"key_id": "abc"},
        "run_report": {"customer_id": "abc"},
        "list_images": {"document_id": "abc"},
        # R1b
        "manage_workflow_group": {"action": "list", "group_id": "abc"},
        "clone_workflow": {"workflow_id": "abc"},
        "translate_image": {"document_id": "abc", "locale": "de-DE"},
        # R4 connectors
        "connector_pull": {"connection_id": "abc"},
        "connector_translate": {"connection_id": "abc"},
        "connector_push": {"connection_id": "abc"},
        "connector_sync": {"connection_id": "abc"},
        "connector_retry": {"connection_id": 1, "job_id": "abc"},
        # R2 -- admin & identity: a non-integer where the schema types an integer.
        "manage_users": {"action": "role", "user_id": "abc"},
        "manage_admin_ops": {"action": "toggle_rule", "rule_id": "abc"},
        # R3 -- config & credentials: a non-integer where the schema types an integer.
        "manage_install": {"action": "test_engine", "engine_id": "abc"},
        # Wave E -- vendor quote/rework: a non-integer where the schema types the leg id.
        "vendor_approve_quote": {"did": "abc"},
        "vendor_reject_rework": {"did": "abc"},
        # Wave 3 -- queries: a non-integer where the schema types an integer / a bad enum.
        "list_queries": {"limit": "many"},
        "get_query": {"query_id": "abc"},
        "open_query": {"body": "x", "segment_id": "abc"},
        "reply_to_query": {"query_id": "abc", "body": "x"},
        "set_query_status": {"query_id": 1, "status": "banana"},
        "assign_query": {"query_id": "abc"},
        # Wave 3 -- worklist: a non-integer where the schema types the days window.
        "my_work": {"days": "abc"},
        # Wave 3 §6.5 -- reviewer LQA findings: a non-integer where the schema types an
        # integer / a bad severity enum.
        "list_lqa_findings": {"document_id": "abc"},
        "log_lqa_finding": {
            "document_id": 1,
            "segment_id": 1,
            "dimension": "Accuracy",
            "severity": "banana",
            "note": "x",
        },
        "edit_lqa_finding": {
            "finding_id": "abc",
            "dimension": "Accuracy",
            "severity": "major",
            "note": "x",
        },
        "withdraw_lqa_finding": {"finding_id": "abc"},
        # run-190 Item C: a non-int for the int-typed limit is refused at the arg model
        # before any /api call.
        "ledger_summary": {"limit": "abc"},
        # ROADMAP #6: a non-int for the int-typed limit is refused at the arg model too.
        "ledger_runs": {"limit": "abc"},
    }
    no_input = sorted(
        t.name for t in registry.TOOLS if not (t.input_schema.get("properties") or {})
    )
    covered = set(bad) | set(no_input)
    declared = {t.name for t in registry.TOOLS}
    assert covered == declared, f"tools with no bad-input case: {sorted(declared - covered)}"
    for name, args in sorted(bad.items()):
        expect_schema_refusal(name, args)

    # A constraint the schema ADVERTISES must be one the tool ENFORCES, or the
    # model's contract is a suggestion and the refusal arrives late, as a 422.
    bounded = [
        (t.name, p, s.get("minimum"), s.get("maximum"))
        for t in registry.TOOLS
        for p, s in (t.input_schema.get("properties") or {}).items()
        if "maximum" in s or "minimum" in s
    ]
    assert len(bounded) >= 5, "only %d bounded properties left to check" % len(bounded)  # noqa: UP031
    for tool_name, prop, floor, ceiling in bounded:
        if ceiling is not None:
            expect_schema_refusal(tool_name, {prop: ceiling + 1})
        if floor is not None:
            expect_schema_refusal(tool_name, {prop: floor - 1})
    print(
        "  bounds: all %d bounded integer properties refuse out-of-range values at "  # noqa: UP031
        "the arg model - limit=0 no longer becomes a silent default page and "
        "since=-1 no longer re-bootstraps without the resync signal" % len(bounded)
    )
    print(
        "  schema validation: all %d tools with inputs reject a malformed call at "  # noqa: UP031
        "the arg model built from the registry (%s take no arguments, so there is "
        "no bad input to reject)" % (len(bad), " and ".join(no_input))
    )


def check_handler_buckle() -> None:
    """The spend handlers' own `confirm` assertion, which nothing else covers.

    `_dispatch` normally answers an unconfirmed spend call with a plan, so
    `_require_confirmed` is only reachable by a DIRECT handler call - a script, a
    test, a refactor that bypasses the dispatcher. It is the backstop that would
    catch a future refactor which reintroduced the wave-1 money bug behind the
    dispatcher. So it is exercised on ALL SIX spend handlers, and with the whole
    family of HOSTILE confirm values - every string that looks like consent but is
    truthy in Python ("false"/"0"/"no"/"off"), plus 1/[]/{}/None - each of which
    must RAISE rather than spend, because the backstop now demands the boolean
    True exactly like the dispatcher (`confirm is not True`)."""
    # factory per spend handler: minimal-but-structurally-valid required args, so
    # the ONLY thing under test is that _require_confirmed fires before any
    # network call (each handler asserts confirm first).
    make = {
        "submit_translation": lambda c: mcp_server.submit_translation(
            __file__, ["de-DE"], confirm=c
        ),
        "translate_text": lambda c: mcp_server.translate_text("hi", "de-DE", confirm=c),
        "run_workflow": lambda c: mcp_server.run_workflow(
            "run", workflow_id=1, document_id=1, locale="de-DE", confirm=c
        ),
        "extract_terms": lambda c: mcp_server.extract_terms(1, 1, confirm=c),
        "translate_keys": lambda c: mcp_server.translate_keys(1, 1, "de-DE", confirm=c),
        "translate_image": lambda c: mcp_server.translate_image(1, "de-DE", confirm=c),
        "connector_translate": lambda c: mcp_server.connector_translate(1, confirm=c),
        "connector_push": lambda c: mcp_server.connector_push(1, confirm=c),
        "connector_sync": lambda c: mcp_server.connector_sync(1, confirm=c),
        "connector_retry": lambda c: mcp_server.connector_retry(1, 1, confirm=c),
        "project_run": lambda c: mcp_server.project_run(1, confirm=c),
        "vendor_approve_quote": lambda c: mcp_server.vendor_approve_quote(1, confirm=c),
        "vendor_reject_rework": lambda c: mcp_server.vendor_reject_rework(1, confirm=c),
    }
    spend_tools = {t.name for t in registry.TOOLS if t.tier == "spend"}
    assert set(make) == spend_tools, (
        f"the buckle test does not cover every spend handler: missing {sorted(spend_tools - set(make))}, extra {sorted(set(make) - spend_tools)}"  # noqa: E501
    )
    hostile = ("false", "0", "no", "off", 1, [], {}, None)
    for name, factory in make.items():
        for c in hostile:
            try:
                _run(factory(c))
            except mcp_server.ToolError as exc:
                assert "without confirm=true" in str(exc), (name, repr(c), str(exc))
                assert "read-only plan" in str(exc), (name, repr(c), str(exc))
            else:
                raise AssertionError(
                    f"{name} spent on a DIRECT handler call with confirm={c!r} -- the "
                    "dispatcher is the contract, but the handler is the buckle, "
                    f"and {c!r} is not the boolean True"
                )
    print(
        "  handler buckle: all %d spend handlers, each against %d hostile "  # noqa: UP031
        "confirm values ('false'/'0'/'no'/'off'/1/[]/{}/None), raise and point "
        "the caller back at the plan - the dispatcher bypass cannot spend by "
        "accident, and only the boolean True gets through" % (len(make), len(hostile))
    )


def check_unit_regressions() -> None:
    """Two controls that are cheaper to prove directly than through the app."""
    hostile = 'attachment; filename="../../evil.json"'
    assert mcp_server._filename_from_disposition(hostile) == "evil.json", (
        "the Content-Disposition basename is a LOAD-BEARING security control: "
        "AUDIT-keys-live-qa-2026-08-03 found ?namespace=../../evil survives the "
        "server-side sanitizer, so this is what keeps the bytes inside the "
        "output dir"
    )
    assert mcp_server._filename_from_disposition(
        'attachment; filename="C:\\\\win\\\\evil.json"'
    ).endswith("evil.json")

    async def naughty_planner(kwargs):
        async with mcp_server._client() as client:
            await mcp_server._call(client, "POST", "/api/documents")
        return {}

    async def confirming_planner(kwargs):
        # The whitelisted route, but WITH confirm: the one body plan mode must
        # never send. If the gate failed, the live API would answer the 400
        # asking for the acknowledgement - a different message the assert
        # below would expose.
        async with mcp_server._client() as client:
            await mcp_server._call(
                client, "POST", "/api/evaluations/runs/start", json_body={"confirm": True}
            )
        return {}

    spec = registry.tool_by_name("submit_translation")
    saved_token, mcp_server.TOKEN = mcp_server.TOKEN, "lsu_whatever"
    try:
        for label, planner in (
            ("POST a plain write", naughty_planner),
            ("POST the whitelisted route WITH confirm", confirming_planner),
        ):
            mcp_server.PLANNERS["submit_translation"] = planner
            try:
                _run(mcp_server._plan(spec, {}))
            except mcp_server.ToolError as exc:
                assert "read-only by contract" in str(exc), (label, str(exc))
            else:
                raise AssertionError(
                    f"a PLAN was allowed to {label} -- the plan-only invariant is not enforced"
                )
    finally:
        mcp_server.PLANNERS["submit_translation"] = mcp_server._plan_submit_translation
        mcp_server.TOKEN = saved_token
    print(
        "  regressions: a hostile Content-Disposition is basenamed before any "
        "write; a planner POSTing a plain write is refused, and so is one "
        "POSTing the whitelisted estimate route WITH confirm - the "
        "_PLAN_SAFE_REQUESTS crack admits exactly the confirm-less estimate "
        "call and nothing else"
    )


def check_optional_string_json(kpid: int) -> None:
    """M1 (AUDIT-mcp-live-2026-08-05): a value that is itself valid JSON must
    survive as a STRING on an OPTIONAL string parameter and reach the handler
    intact, over the path a real client takes.

    FastMCP's func_metadata.pre_parse_json (mcp==1.28.1) JSON-decodes any argument
    whose validating-model field annotation `is not str`. Localization OS's optional
    strings used to be `str | None`, so `import_keys(data='{"k":"v"}')` arrived at
    the pydantic model as a dict and was rejected with a baffling "Input should be a
    valid string". The fix annotates every optional PLAIN string as exactly `str`
    (default None keeps it optional), which pre_parse_json leaves untouched.

    The pre-existing import test used the `call()` seam (call_tool_unvalidated),
    which SKIPS pre-parse -- that is exactly why this class went unseen. Every check
    here goes through `validated()`/`via_mcp` -> `mcp.call_tool`, where pre-parse
    lives."""
    # (1) STRUCTURAL -- every optional plain-string param, across all 36 tools, is
    # annotated EXACTLY `str` in the model that actually validates the call. This is
    # the property that makes pre_parse_json a no-op for them; it covers all ~67 of
    # today's params and any future one a contributor adds.
    manager = mcp_server.mcp._tool_manager._tools
    checked = 0
    for spec in registry.TOOLS:
        schema = spec.input_schema or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        fields = manager[spec.name].fn_metadata.arg_model.model_fields
        for pname, prop in props.items():
            if not (
                isinstance(prop, dict)
                and prop.get("type") == "string"
                and pname not in required
                and "default" not in prop
                and not prop.get("enum")
            ):
                continue
            fi = fields.get(pname) or fields.get("field_" + pname)
            assert fi is not None, f"{spec.name}.{pname} vanished from the arg model"
            assert fi.annotation is str, (
                f"{spec.name}.{pname} validates as {fi.annotation!r}, not exactly `str`. FastMCP's pre_parse_json "  # noqa: E501
                "JSON-decodes any arg whose annotation `is not str`, so a JSON-shaped "
                "value on it is coerced to a dict/list and rejected (M1). An optional "
                "plain string MUST be exactly `str` with default None -- see "
                "_signature_from_schema."
            )
            checked += 1
    assert checked >= 60, (
        "expected ~67 optional string params to guard, saw %d -- the enumeration "  # noqa: UP031
        "went blind" % checked
    )
    print(
        "  optstr structural: %d optional string params validate as exactly `str` "  # noqa: UP031
        "(pre_parse_json cannot coerce them)" % checked
    )

    # (2a) THE AUDIT'S EXACT REPRO: import_keys.data as a JSON-OBJECT string, over
    # mcp.call_tool. Pre-fix this raised ToolError "Input should be a valid string";
    # post-fix the string reaches the handler and the keys merge.
    rep = validated(
        "import_keys",
        {
            "project_id": kpid,
            "data": '{"m1.obj.probe": "Beta value"}',
            "filename": "m1_obj.json",
            "locale": "en-US",
        },
    )
    assert rep.get("created") or rep.get("updated") or rep.get("added"), (
        f"import_keys(data=<json object>) over the real path did not merge: {rep!r}"
    )
    assert "m1.obj.probe" in {
        k["name"] for k in call("list_keys", project_id=kpid, limit=200)["keys"]
    }, "the JSON-object-string import did not land the key"

    # (2b) A JSON-ARRAY string on the same optional param. The point is that the
    # handler is REACHED with the raw string (a JSON array is not a valid keys blob,
    # so the API refuses it -- but with a BACKEND error, never pre-parse's pydantic
    # string_type coercion, which pre-fix fired before the handler ran at all).
    sent: list = []
    real_call = mcp_server._call

    async def _rec(client, method, path, **kw):
        sent.append((method, path))
        return await real_call(client, method, path, **kw)

    mcp_server._call = _rec
    try:
        try:
            validated(
                "import_keys",
                {
                    "project_id": kpid,
                    "data": '["a","b","c"]',
                    "filename": "m1_arr.json",
                    "locale": "en-US",
                },
            )
        except Exception as exc:
            # A backend format refusal is fine (an array is not a valid keys blob);
            # pre-parse's string_type coercion is NOT -- that would mean the value
            # never reached the handler as a string. Distinguish the two by message.
            assert "valid string" not in str(exc).lower(), (
                f"a JSON-array string was still coerced before the handler ran: {exc}"
            )
    finally:
        mcp_server._call = real_call
    assert any(p.endswith("/keys/import") for _m, p in sent), (
        "a JSON-array string on import_keys.data never reached the handler -- "
        "pre_parse_json intercepted it before dispatch (M1 regression)"
    )

    # (2c) REGRESSION: a plain non-JSON string on an optional param still works.
    plain = validated("list_keys", {"project_id": kpid, "q": "app.title"})
    assert isinstance(plain.get("keys"), list), (
        f"a plain-string optional arg (list_keys.q) broke: {plain!r}"
    )

    # (2c-null) F1 PIN: an EXPLICIT null on an optional string arg is accepted over
    # the real wire and reaches the handler as None (exactly as `str | None` did on
    # main). The exact-`str` annotation alone would refuse it -- LLM and typed-object
    # clients routinely send null for an unset optional -- so WrapValidator(
    # _none_passthrough) maps null -> None. A spy confirms the handler ran with q
    # absent (None is dropped from the querystring), and the call answers normally.
    seen: list = []
    real_get = mcp_server._get_json

    async def _spy_get(client, path, **kw):
        seen.append((path, kw.get("params") or {}))
        return await real_get(client, path, **kw)

    mcp_server._get_json = _spy_get
    try:
        nulled = validated(
            "list_keys", {"project_id": kpid, "q": None, "namespace": None, "tag": None}
        )
    finally:
        mcp_server._get_json = real_get
    assert isinstance(nulled.get("keys"), list), (
        "an explicit null on an optional string arg was refused over the wire (F1 "
        f"regression -- exact-`str` must keep the null-passthrough): {nulled!r}"
    )
    keys_calls = [(p, params) for p, params in seen if p.endswith("/keys")]
    assert keys_calls, "list_keys with null optionals never reached the handler"
    # The handler received None (not the string "None"): None is dropped, so the
    # forwarded querystring carries none of q/namespace/tag.
    forwarded = keys_calls[-1][1]
    assert not ({"q", "namespace", "tag"} & set(forwarded)), (
        "a null optional was coerced to a value and forwarded to the API instead of "
        f"arriving as None: {forwarded!r}"
    )

    print(
        "  optstr behavioral: import_keys(data=<json object>) merges over "
        "mcp.call_tool; a <json array> string reaches the handler uncoerced; a "
        "plain string still filters"
    )


def check_admin_identity() -> None:
    """Wave R2 -- manage_users + manage_admin_ops happy paths through the dispatcher.

    Bills nothing (both write tier). create/role are account-security actions and need
    an admin-scoped key (#270 r2-apiauth-10): a docs-scoped key is refused, so the
    create/role/enable/disable loop runs under an admin key and a docs key is proven
    refused on create. Also proves the last-admin guard relayed as a 409 and the
    routing/budget/rulebook admin ops."""
    prev_token = mcp_server.TOKEN
    admin = security.get_by_email("admin")
    _k, admin_key = apikeys.create_key(admin.id, "mcp admin identity", scopes="api docs admin")
    _k, docs_key = apikeys.create_key(admin.id, "mcp docs identity", scopes="api docs")
    # create is an account-security action: a docs-scoped key is refused, not run.
    mcp_server.TOKEN = docs_key
    expect_error(
        "manage_users",
        {"action": "create", "email": "mcp.docsrefused@example.com", "role": "translator"},
        "admin-scoped",
        "docs-key-create-refused",
    )
    mcp_server.TOKEN = admin_key
    made = call(
        "manage_users",
        action="create",
        email="mcp.user@example.com",
        role="translator",
        name="MCP User",
        department="docs",
    )
    uid = made["id"]
    assert made["email"] == "mcp.user@example.com" and made["role"] == "translator", made
    roled = call("manage_users", action="role", user_id=uid, role="reviewer")
    assert roled["new"] == "reviewer", roled
    off = call("manage_users", action="disable", user_id=uid)
    assert off["active"] is False, off
    on = call("manage_users", action="enable", user_id=uid)
    assert on["active"] is True, on
    from app.services import login_throttle

    email = "mcp.user@example.com"
    for i in range(20):
        login_throttle.settle(login_throttle.reserve(email, str(i)), failed=True)
    assert login_throttle.locked_seconds(email) > 0
    unlocked = call("manage_users", action="unlock", user_id=uid)
    assert unlocked["email"] == email and login_throttle.locked_seconds(email) == 0
    # A never-seen account must be recoverable even after unknown-name admission fills.
    from app.models import LoginThrottle

    with Session(engine) as session:
        session.delete(session.get(LoginThrottle, login_throttle._key(email)))
        session.commit()
    unlocked = call("manage_users", action="unlock", user_id=uid)
    assert unlocked["email"] == email
    with Session(engine) as session:
        cleared = session.get(LoginThrottle, login_throttle._key(email))
        assert cleared is not None, "MCP unlock must create a missing throttle row"
        assert cleared.attempts == "[]" and cleared.locked_until == 0 and cleared.level == 0
    # last-admin guard: demoting the ONLY admin is refused (409 relayed), never done.
    admin_uid = security.get_by_email("admin").id
    expect_error(
        "manage_users",
        {"action": "role", "user_id": admin_uid, "role": "translator"},
        "administrator",
        "last-admin-guard",
    )
    assert security.get_by_email("admin").role == "admin", (
        "the last-admin guard let the sole admin be demoted"
    )
    print(
        "  manage_users: create -> role -> disable -> enable landed; demoting the "
        "sole admin is a relayed 409 and the admin stays admin"
    )

    rule = call("manage_admin_ops", action="add_rule", ext="docx", department="docs", priority=50)
    rid = rule["id"]
    tg = call("manage_admin_ops", action="toggle_rule", rule_id=rid)
    assert "active" in tg and tg["id"] == rid, tg
    bud = call("manage_admin_ops", action="set_budget", department="docs", monthly_limit=100000)
    assert bud["department"] == "docs" and bud["monthly_token_limit"] == 100000, bud
    rb = call(
        "manage_admin_ops",
        action="set_rulebook",
        source="en-US",
        target="fr-FR",
        rulebook_text="Be formal.",
    )
    assert rb["chars"] == len("Be formal."), rb
    print(
        "  manage_admin_ops: add_rule -> toggle_rule, set_budget, set_rulebook all "
        "landed (org-scoped writes, no delete/config/credential verbs on MCP)"
    )
    mcp_server.TOKEN = prev_token


def check_install_admin() -> None:
    """Wave R3 -- manage_install through the dispatcher: masked config read, a
    NON-SECRET config write, engine create + test, provider-key clear + masked show;
    AND the security refinement -- a SECRET-marked or install-locked key is refused
    CLIENT-SIDE, and every secret-SET path is /api-only (no MCP tool). Needs an
    admin-scope key + remote_admin_enabled ON."""
    prev_token = mcp_server.TOKEN
    admin = security.get_by_email("admin")
    _k, admin_key = apikeys.create_key(admin.id, "mcp install", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    mcp_server.TOKEN = admin_key
    try:
        cfg = call("manage_install", action="get_config")["settings"]
        assert "org_name" in cfg and cfg.get("smtp_password") in ("(set)", "(unset)"), cfg
        # NON-SECRET write lands.
        out = call("manage_install", action="set_config", settings={"org_name": "R3 Co"})
        assert "org_name" in out.get("changed", []), out
        assert settings.get("org_name") == "R3 Co"
        # SECRET-marked key refused CLIENT-SIDE (never reaches HTTP).
        expect_error(
            "manage_install",
            {"action": "set_config", "settings": {"smtp_password": "x"}},
            "secret",
            "install-secret-refused",
        )
        # install-LOCKED key refused.
        expect_error(
            "manage_install",
            {"action": "set_config", "settings": {"remote_admin_enabled": "1"}},
            "install-locked",
            "install-locked-refused",
        )
        # engine create (non-secret) + test.
        eng = call(
            "manage_install",
            action="create_engine",
            name="MCP-Eng",
            kind="deepl",
            base_url="http://127.0.0.1:9",
        )
        eid = eng["engine"]["id"]
        assert eng["engine"]["key_set"] is False, eng
        tested = call("manage_install", action="test_engine", engine_id=eid)
        assert "ok" in tested and "message" in tested, tested
        # provider key CLEAR + masked SHOW (never a value).
        cleared = call("manage_install", action="clear_provider_key", provider="gemini")
        assert cleared["provider"] == "gemini", cleared
        shown = call("manage_install", action="show_provider_key", provider="gemini")
        assert "last4" in shown and "source" in shown and "key" not in shown, shown
        print(
            "  manage_install: masked config read, non-secret set, engine create+test, "
            "provider-key clear+masked-show landed; a secret-marked or install-locked "
            "key is refused client-side (secret-SET paths are /api-only)"
        )

        # --- M-HIGH-1 regression (AUDIT 2026-08-18): `provider` is MODEL-AUTHORED
        # free text spliced into the URL path. Before the _seg() fix a probe passed
        # provider="../../customers/1/archive?" and httpx normalised it onto
        # POST /api/customers/1/archive -- a NEVER_COVER route -- and the customer
        # row MUTATED. Record the OUTBOUND path at the _call seam and prove it now
        # stays ONE segment under /api/admin/providers/ and cannot resolve onto the
        # customer-archive route, on BOTH provider-key actions.
        malicious = "../../customers/1/archive?"
        captured = []
        real_call = mcp_server._call

        async def _capture_path(client, method, path, **kw):
            captured.append((method, path))
            return httpx.Response(
                200, json={"provider": "x", "last4": "0000", "source": "env", "cleared": True}
            )

        mcp_server._call = _capture_path
        try:
            for act in ("clear_provider_key", "show_provider_key"):
                captured.clear()
                call("manage_install", action=act, provider=malicious)
                assert len(captured) == 1, (act, captured)
                sent_path = captured[0][1]
                # the traversal is percent-encoded into ONE segment, so the built
                # path never contains a real /customers/ or /archive boundary...
                assert sent_path.startswith("/api/admin/providers/"), (act, sent_path)
                assert "/customers/" not in sent_path and "/archive" not in sent_path, (
                    act,
                    sent_path,
                )
                # ...and httpx's OWN normalisation (the step that landed the mutation)
                # cannot walk it back onto the customer-archive route.
                resolved = httpx.URL("http://x" + sent_path).path
                assert resolved.startswith("/api/admin/providers/"), (act, resolved)
                assert resolved != "/api/customers/1/archive", (act, resolved)
        finally:
            mcp_server._call = real_call
        print(
            "  manage_install: a traversal-shaped provider is percent-encoded by "
            "_seg() into one segment under /api/admin/providers/ -- httpx cannot "
            "normalise it onto the /api/customers/{cid}/archive NEVER_COVER route "
            "(M-HIGH-1 path-injection regression, both key actions)"
        )
    finally:
        settings.set_many({"remote_admin_enabled": "0"})
        settings.invalidate_cache()
        mcp_server.TOKEN = prev_token


# ============================================================================
def _error_tail(msg: str) -> dict:
    """The #756 JSON last line of a tool error, parsed."""
    last = msg.rstrip().splitlines()[-1]
    return json.loads(last)["error"]


def check_wire_metadata() -> None:
    """#749 #750 #752 on the wire: titles, annotations, outputSchema, structured
    content and the server's identity + instructions, read back through the SDK."""
    import jsonschema
    import tool_meta

    tools = {t.name: t for t in _run(mcp_server.MCPServer.list_tools(mcp_server.mcp))}
    for spec in registry.TOOLS:
        tool = tools[spec.name]
        want = tool_meta.annotations_for(spec)
        assert tool.title and tool.title == tool_meta.TITLES[spec.name], spec.name
        got = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        assert got == want, f"{spec.name}: annotations {got} != derived {want}"
        assert got["readOnlyHint"] is (spec.tier == "read"), spec.name
        if spec.tier == "spend":
            assert got["destructiveHint"] is True, spec.name
        assert tool.output_schema == tool_meta.output_schema_for(spec), (
            f"{spec.name}: outputSchema is not the registry-derived one: {tool.output_schema}"
        )
        assert tool.output_schema.get("type") == "object", spec.name
        assert tool.output_schema.get("properties", {}).get("result") is None, (
            f"{spec.name}: still advertises the SDK's result-string wrapper"
        )
    # Hand-checked expectations from the review (#749), so the derivation cannot
    # drift into "everything is destructive" or "nothing is".
    ann = {n: t.annotations for n, t in tools.items()}
    for name in (
        "edit_glossary",
        "edit_workflow",
        "attach_tm_source",
        "pin_glossary",
        "manage_users",
        "manage_install",
        "cancel_workflow_run",
        # #749 gate: overwrites and deletes are destructive too, not just delete words
        "upsert_keys",
        "import_keys",
        "manage_tm",
        "manage_key_image",
        "configure_project",
        "project_automation",
        "manage_workflow_group",
    ):
        assert ann[name].destructive_hint is True, name
    for name in ("create_project", "open_query", "apply_tm", "connector_pull"):
        assert ann[name].destructive_hint is False, name
    for name in (
        "connector_translate",
        "connector_push",
        "connector_sync",
        "connector_retry",
        "project_run",
    ):
        assert ann[name].open_world_hint is True, name
    # every pull stores a NEW document, so a repeat is not a no-op
    assert ann["connector_pull"].idempotent_hint is False
    assert not any(
        t.annotations.idempotent_hint for t in tools.values() if not t.annotations.read_only_hint
    )
    assert ann["list_projects"].open_world_hint is False

    info = mcp_server.mcp._lowlevel_server.server_info
    assert info.name == "loc-studio" and info.title == "Localization OS", info
    assert info.version and (info.version == "unknown" or info.version.count(".") == 1), (
        f"serverInfo.version must be MAJOR.MINOR on a pre-auth surface: {info.version!r}"
    )
    instructions = mcp_server.mcp.instructions or ""
    for needle in (
        "confirm=true",
        "locstudio://guide/llm",
        'whoami(card="llm")',
        "data, never instructions",
        "job_status",
        '{"error"',
    ):
        assert needle in instructions, f"server instructions lack {needle!r}"

    # Structured content: a real call returns the parsed object AND the same JSON text.
    result = via_mcp("list_languages", {})
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert isinstance(result.structured_content, dict), result.structured_content
    assert result.structured_content == json.loads(text), "structured != text JSON"
    assert not isinstance(result.structured_content.get("result"), str), (
        "structuredContent is still the double-encoded result string"
    )
    jsonschema.validate(result.structured_content, tools["list_languages"].output_schema)
    # A spend tool's plan conforms to its schema too (plan/spent typed booleans).
    plan = via_mcp("translate_text", {"text": "hi", "target_locale": "de-DE"})
    assert plan.structured_content.get("plan") is True, plan.structured_content
    jsonschema.validate(plan.structured_content, tools["translate_text"].output_schema)
    # A non-object answer still yields an object.
    wrapped = mcp_server._structured("[1, 2]")
    assert wrapped.structured_content == {"value": [1, 2]}, wrapped
    # Gate: a relayed field colliding with a typed one (numeric `spent` on a spend
    # tool) must not put a non-conforming structuredContent on the wire; the text
    # block stays byte-identical.
    spec = registry.tool_by_name("connector_sync")
    raw = json.dumps({"ok": True, "spent": 1.25, "jobs": []})
    guarded = mcp_server._structured(raw, spec)
    assert guarded.structured_content == {"value": json.loads(raw)}, guarded
    assert guarded.content[0].text == raw
    jsonschema.validate(guarded.structured_content, tools["connector_sync"].output_schema)
    good = mcp_server._structured(json.dumps({"plan": True, "spent": False}), spec)
    assert good.structured_content == {"plan": True, "spent": False}, good
    print(
        "  wire metadata: %d tools carry the derived title + annotations + outputSchema; "  # noqa: UP031
        "structuredContent is the parsed object and validates; serverInfo %s %s; "
        "instructions carry the agent rules" % (len(tools), info.title, info.version)
    )


def check_error_tails(keys_key: str, all_key: str) -> None:
    """#756: every ToolError a client receives ends with a JSON line carrying a
    stable code, http_status, retryable, retry_after_s and required_scope."""
    import tool_meta

    def wire_error(name, arguments):
        try:
            via_mcp(name, arguments)
        except Exception as exc:  # the SDK's ToolError
            return str(exc)
        raise AssertionError(f"{name}: expected a tool error")

    tail = _error_tail(wire_error("no_such_tool", {}))
    assert tail["code"] == "unknown_tool" and tail["retryable"] is False, tail
    tail = _error_tail(wire_error("get_query", {"query_id": "abc"}))
    assert tail["code"] == "invalid_argument", tail
    tail = _error_tail(wire_error("get_query", {"query_id": 987654}))
    assert tail["code"] == "not_found_or_hidden" and tail["http_status"] == 404, tail
    mcp_server.TOKEN = keys_key
    try:
        msg = wire_error("list_workflows", {})
    finally:
        mcp_server.TOKEN = all_key
    tail = _error_tail(msg)
    assert tail["http_status"] == 403 and tail["required_scope"] == "docs", tail
    assert tail["code"] in ("insufficient_scope", "forbidden"), tail
    for key in ("code", "http_status", "retryable", "retry_after_s", "required_scope"):
        assert key in tail, (key, tail)
    assert set(tool_meta.ERROR_CODES) >= {
        "unauthorized",
        "insufficient_scope",
        "not_found_or_hidden",
        "capacity_retry",
        "timeout_maybe_running",
        "conflict",
    }
    # unit: retry hints and the timeout class are read from the exception chain
    err = mcp_server._HttpToolError("busy")
    err.status_code = 503
    mcp_server._annotate_http_error(
        err,
        httpx.Response(503, json={"error": "busy", "reason": "run_capacity", "retry_after": 15}),
    )
    tail = json.loads(mcp_server._error_tail("run_workflow", err))["error"]
    assert tail == {
        "code": "capacity_retry",
        "http_status": 503,
        "retryable": True,
        "retry_after_s": 15,
        "required_scope": None,
        "server_code": "run_capacity",
    }, tail
    err = mcp_server._HttpToolError("slow")
    err.status_code = 429
    mcp_server._annotate_http_error(
        err, httpx.Response(429, text="slow down", headers={"Retry-After": "inf"})
    )
    assert err.retry_after_s is None, "a non-finite Retry-After must not reach the tail"
    try:
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as exc:
            raise mcp_server._friendly_connection_error(exc) from exc
    except mcp_server.ToolError as wrapped:
        tail = json.loads(mcp_server._error_tail("connector_sync", wrapped))["error"]
    assert tail["code"] == "timeout_maybe_running" and tail["retryable"] is False, tail
    # 409: "nothing was spent" only where it is known true
    req = httpx.Request("POST", "http://x/api/connectors/1/push")
    msg = mcp_server._explain_status(httpx.Response(409, json={"error": "Busy."}, request=req))
    assert "nothing was spent" not in msg.lower() and "check it before retrying" in msg, msg
    msg = mcp_server._explain_status(
        httpx.Response(409, json={"error": "Already running.", "code": "run_conflict"}, request=req)
    )
    assert "Nothing was started and nothing was spent" in msg, msg
    # 403 advice for an admin-scope tool says how an admin key is issued
    tok = mcp_server._CURRENT_TOOL.set("manage_users")
    try:
        msg = mcp_server._explain_status(httpx.Response(403, request=req))
    finally:
        mcp_server._CURRENT_TOOL.reset(tok)
    assert "'admin' scope" in msg and "issued locally" in msg, msg
    print(
        "  error tails: unknown tool, bad argument, 404, 403 (+scope), 503 retry, "
        "timeout, 409 policy OK"
    )


def check_round5_uniform_wait():
    from types import SimpleNamespace
    from unittest.mock import patch

    async def exercise():
        for mode in ("run", "run_batch", "rerun", "retry_failed"):
            for finish in (True, False):
                gaps = []
                now = [0.0]

                async def launch(*args, mode=mode, **kwargs):
                    assert kwargs["json_body"]["wait"] is False, mode
                    return httpx.Response(
                        200, json={"run_id": 123} if mode == "run" else {"id": 123}
                    )

                async def status(mode=mode, finish=finish, **kwargs):
                    expected = {"run_id": 123} if mode == "run" else {"batch_id": 123}
                    if mode in {"rerun", "retry_failed"}:
                        expected["detail"] = True
                    assert kwargs == expected
                    return json.dumps({"status": "completed" if finish else "running"})

                async def sleep(gap, gaps=gaps, now=now):
                    gaps.append(gap)
                    now[0] += 10

                clock = SimpleNamespace(
                    get_running_loop=lambda now=now: SimpleNamespace(time=lambda: now[0]),
                    timeout=asyncio.timeout,
                    sleep=sleep,
                )
                with (
                    patch.object(mcp_server, "_call", launch),
                    patch.object(mcp_server, "work_status", status),
                    patch.object(mcp_server, "_WORKFLOW_WAIT_S", 10),
                    patch.object(mcp_server, "asyncio", clock),
                ):
                    out = json.loads(
                        await mcp_server.run_workflow(
                            mode,
                            workflow_id=1,
                            document_id=2,
                            locale="es-ES",
                            batch_id=123,
                            confirm=True,
                        )
                    )
                    assert out["done"] is finish, out
                    if not finish:
                        assert gaps == [mcp_server._POLL_GAP_S], gaps
                        assert out["status"] == "running" and "work_status" in out["next"], out
                        assert ("run_id=123" if mode == "run" else "batch_id=123") in out["next"]

    asyncio.run(exercise())


def main() -> None:
    init_db()
    security.seed_initial()
    settings.set_many({"audit_enabled": "1", "key_projects_enabled": "1"})

    admin = security.get_by_email("admin")
    _k, all_key = apikeys.create_key(admin.id, "mcp all", scopes="api docs keys")
    _k, docs_key = apikeys.create_key(admin.id, "mcp docs", scopes="api docs")
    _k, keys_key = apikeys.create_key(admin.id, "mcp keys", scopes="api keys")
    _k, api_key = apikeys.create_key(admin.id, "mcp api", scopes="api")

    out_dir = tempfile.mkdtemp(prefix="loctms_mcpw1_out_")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    import pathlib

    mcp_server.BASE_URL = base_url
    mcp_server.TOKEN = all_key
    mcp_server.OUTPUT_DIR = pathlib.Path(out_dir)

    srv = _UvicornThread(port)
    register_provider("claude", LiveStubProvider())
    try:
        srv.start()
        check_registration()
        check_wire_metadata()
        check_round5_uniform_wait()
        check_capabilities_route(base_url, api_key, docs_key)
        check_orientation()
        pid = check_projects()
        check_documents(pid, out_dir)
        kpid = check_keys(out_dir)
        wf_id = check_workflows(pid, out_dir)
        check_213_run_conflict_relay(pid, out_dir)
        check_workflow_groups(wf_id)
        check_clone_workflow(wf_id)
        cust_id = check_assets(pid, out_dir)
        check_tm(pid, cust_id, out_dir)
        check_ops(out_dir)
        check_connectors()
        check_translate_image()
        check_admin_identity()
        check_install_admin()
        check_scopes(docs_key, keys_key, api_key, kpid, pid, all_key)
        check_error_tails(keys_key, all_key)
        check_kill_switch(kpid)
        check_wave2_kill_switches(cust_id)
        check_schema_refusals()
        check_handler_buckle()
        check_unit_regressions()
        check_optional_string_json(kpid)
        # #750 gate: real answers validated against outputSchema, tool by tool.
        # 45 before the open-core strip removed the paid memory/evaluation/advanced tools.
        assert len(_STRUCTURED_VALIDATED) >= 42, len(_STRUCTURED_VALIDATED)
        print(
            "  structured output: real answers of %d tools validated against their "  # noqa: UP031
            "outputSchema (structured == text JSON)" % len(_STRUCTURED_VALIDATED)
        )
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)
        srv.stop()
        settings.set_many({"security_auth_required": "0"})

    print("MCP TOOLS OK")


if __name__ == "__main__":
    main()
