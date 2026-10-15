"""The UTCP (Universal Tool-Calling Protocol) discovery manual — BACKLOG 6b, Option B.

A single READ-ONLY manual, served at ``GET /utcp``, describing a curated subset of
this install's own ``/api`` surface as UTCP tools. It is **generated from the app's
OpenAPI schema at request time**, never hand-maintained, so it cannot rot when routes
change (the same anti-drift rule that makes ``/help/llm`` defer to ``/openapi.json``).

WHY THIS EXISTS, AND ITS LIMITS. MCP stays the first-class agent protocol
(Option A). This manual is the additive,
experimental hedge (Option B): buzzword coverage + optionality if UTCP ever gains a
real client. No agent client consumes UTCP natively today. The manual's own ``info``
block says so and points MCP-capable clients at the MCP server instead.

============================================================================
THE LOAD-BEARING SECURITY BOUNDARY: an EXPLICIT ALLOW-LIST (fail-closed).
============================================================================
UTCP has **no confirmation / consent / two-phase primitive** — that is the entire
reason we did not migrate off MCP (whose two-phase spend law our guardrails depend
on). A UTCP client calls the raw HTTP endpoint directly with no server in the path to
enforce policy. Therefore **no write, spend, config, credential, or identity surface
may EVER appear in this manual.**

The manual is built from ``_ALLOWLIST`` below — an explicit, checked-in set of
param-name-agnostic path templates that have each been classified read-only and
non-sensitive. A route is INVISIBLE until it is deliberately added here and classified.
This is deliberately FAIL-CLOSED: a newly added ``GET /api/secrets/{id}`` (or any other
future route) does not appear in the manual until a human puts it on this list — the
allow-list, not the app's route table, is the security artifact. (An earlier draft used
a deny-list; adversarial review proved it FAILED OPEN — an injected sensitive GET
landed in the manual with the tests still green — so it was inverted.)

Belt-and-suspenders on top of the allow-list: ``is_included`` still requires
``method == GET`` and a path under ``/api/``. The allow-list already implies both, but
the redundant checks mean a mistake on the list alone can never expose a non-GET verb.

A tool in here is a tool a client will call with no confirmation, so the list is
restricted to reads that are safe to call repeatedly. NOTE (adversarial review #8): four
included exports (``.../export``, ``.../download``) append an AUDIT row on each call —
the only side effect anywhere in the set; there is no MT, no spend, no external egress
and no state change. That is why "read-only" here means "does not change translation
state", not "literally zero writes".

DETERMINISM. Tools are emitted sorted by name; the manual carries no timestamps and no
randomness, so two renders against the same base URL are byte-identical (cacheable,
diffable). ``manual_version`` embeds a content hash of the tool set so a version-aware
client can tell two tool sets apart, and it too is deterministic per render.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from __future__ import annotations

import hashlib
import json
import re

from app import branding
from app.services import settings

# --------------------------------------------------------------------------- #
# Spec pins + auth declaration
# --------------------------------------------------------------------------- #
#: The UTCP protocol version this manual is written against (utcp.io, verified
#: 2026-08-09: current version is 1.1). This is the SPEC version, distinct from the
#: manual_version (ours) built below.
UTCP_SPEC_VERSION = "1.1"
#: Base of our manual's own semver. The rendered ``manual_version`` appends a short
#: content hash of the tool set (``1.0.0+<hash>``), so a client caching by version sees
#: it change when the exposed tools change. Bump the BASE when the manual's SHAPE/rules
#: change (not merely its contents).
MANUAL_VERSION_BASE = "1.0.0"

#: Every tool authenticates with a personal API key in the X-Loc-Token header — the
#: same credential the whole /api surface uses. The value is a LITERAL client-side
#: substitution placeholder (UTCP ``${VAR}`` syntax): the manual never contains, and
#: cannot leak, a real key. The client supplies UTCP_LOC_STUDIO_KEY from its own env.
_AUTH_VAR_NAME = "X-Loc-Token"
_AUTH_PLACEHOLDER = "${UTCP_LOC_STUDIO_KEY}"


def _norm(path: str) -> str:
    """Param-name-agnostic path template: every ``{param}`` becomes ``{}`` (adversarial
    review #5). Allow-list membership must not depend on a parameter's NAME, or renaming
    ``{pid}`` to ``{project_id}`` would silently drop a path off (or onto) the list while
    a name-literal test still passed. Every comparison in this module and its tests goes
    through this normalization."""
    return re.sub(r"\{[^}]+\}", "{}", path)


# =========================================================================== #
# THE ALLOW-LIST — the security artifact. Every entry is a normalized (``{}``)
# path template that has been reviewed as a READ-ONLY, NON-SENSITIVE GET. Grouped
# by category so the classification of each is explicit and auditable. Adding a
# route to the manual is adding a line here — nowhere else.
# =========================================================================== #
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # -- discovery / catalog (no tenant data) --
        "/api/capabilities/{}",  # a repo-tracked capability card (docs)
        "/api/languages",  # the locale catalog
        "/api/formats",  # supported file formats
        # -- projects & customers (read; RBAC still enforced per-route on call) --
        "/api/projects",  # list projects visible to the key
        "/api/projects/{}",  # one project's detail
        "/api/projects/{}/documents",  # a project's document list
        "/api/projects/{}/changes",  # the Delta API change feed (read)
        "/api/projects/{}/export",  # raw bytes: translated bundle (audit-logged)
        "/api/projects/{}/keys",  # key listing (key projects)
        "/api/projects/{}/keys/export",  # raw bytes: one locale's file (audit-logged)
        "/api/projects/{}/glossary",  # legacy per-project glossary (read)
        "/api/projects/{}/glossary/export",  # raw bytes: legacy glossary (audit-logged)
        "/api/projects/{}/glossaries",  # module-glossary pins on the project (read)
        "/api/projects/{}/tm-sources",  # attached TM sources (read)
        "/api/projects/{}/tm/export",  # TMX export (JSON envelope)
        "/api/customers",  # customer list
        "/api/customers/{}/detail",  # customer-360 (business data, read)
        # -- documents & segments (read) --
        "/api/documents/{}/segments",  # segment view
        "/api/documents/{}/paragraphs",  # full_text paragraph view
        "/api/documents/{}/download",  # raw bytes: translated file (audit-logged)
        # -- keys (read) --
        "/api/keys/{}/values",  # a key's per-locale values
        "/api/keys/{}/images",  # a key's in-context image METADATA (Batch 14; never bytes)
        # -- language assets (read; author/config verbs are all non-GET, excluded) --
        "/api/glossaries",  # module glossary list
        "/api/glossaries/{}",  # one module glossary
        "/api/glossaries/{}/candidates",  # extraction candidates (read)
        "/api/glossaries/{}/export",  # raw bytes: termbase (audit-logged)
        "/api/style-guides",  # style-guide list
        "/api/style-guides/{}",  # one style guide
        "/api/content-types",  # content-type list
        "/api/langsets",  # language-set list
        "/api/templates",  # project-template list
        "/api/templates/{}",  # one project template
        "/api/tm",  # TM entry browse
        "/api/tm/stats",  # TM stats
        "/api/tm/{}",  # one TM entry
        "/api/context/resolve",  # effective context package for a scope (read)
        # -- workflows & runs (read; run/author verbs are non-GET, excluded) --
        "/api/workflows",  # workflow list
        "/api/workflows/{}",  # one workflow's full step graph
        "/api/workflow-groups",  # workflow-group list
        "/api/workflow-groups/{}",  # one workflow group
        "/api/prompt-templates",  # prompt-template list
        "/api/system-prompts",  # built-in pipeline system prompts (read-only, #356)
        "/api/runs/{}",  # one workflow run (status/findings)
        "/api/batches/{}",  # one batch's status
        # -- jobs & reports (read) --
        "/api/jobs",  # execution history
        "/api/jobs/{}",  # one job
        "/api/reports/run",  # in-scope report (read; ?format=csv also)
        # -- data export (read METADATA only; RBAC + _export_owns still enforced per-route
        #    on call). The list + one run's status are safe, repeatable reads of the CALLER'S
        #    OWN export jobs. The artifact DOWNLOAD (/api/exports/{}/artifact) is DELIBERATELY
        #    NOT here: a workspace-scope artifact carries the identity + audit group, which is
        #    exactly the "identity surface" this fail-closed allow-list must never expose to a
        #    no-confirmation UTCP client -- so only the metadata reads are classified. --
        "/api/exports",  # the caller's own export runs (actor-filtered)
        "/api/exports/{}",  # one export run's status/counts
        # -- images (read; upload/translate verbs are non-GET, excluded) --
        "/api/images",  # image-doc list
        "/api/images/{}",  # one image doc
        "/api/images/{}/export",  # raw bytes: localized image (audit-logged)
    }
)

#: Included GETs that stream RAW BYTES rather than a JSON body (adversarial review #6):
#: declaring ``outputs: {type: object}`` for these is a false contract. Normalized
#: paths. Everything else is a JSON object body. (Note ``/tm/export``
#: returns a JSON ENVELOPE carrying the rows/bytes, so it is NOT here.)
_BINARY_OUTPUT_PATHS: frozenset[str] = frozenset(
    {
        "/api/projects/{}/export",
        "/api/projects/{}/keys/export",
        "/api/projects/{}/glossary/export",
        "/api/documents/{}/download",
        "/api/glossaries/{}/export",
        "/api/images/{}/export",
    }
)

# --------------------------------------------------------------------------- #
# Input overrides (adversarial review #2). The routes below read query params via
# request.query_params, so FastAPI/OpenAPI does not know about them and the derived
# ``inputs`` would be empty. This is the ONE auditable place those params are declared.
# Values are merged ON TOP of the OpenAPI-derived inputs (properties added; required unioned).
# Normalized paths. ``req`` lists the required names.
# --------------------------------------------------------------------------- #
_S = {"type": "string"}
_I = {"type": "integer"}
_INPUT_OVERRIDES: dict[str, dict] = {
    "/api/reports/run": {
        "props": {
            "kind": _S,
            "group_by": _S,
            "from": _S,
            "to": _S,
            "customer_id": _I,
            "project_id": _I,
            "provider": _S,
            "format": {"type": "string", "enum": ["json", "csv"]},
        },
        "req": [],
    },
    "/api/tm": {
        "props": {
            "customer": _I,
            "customer_id": _I,
            "page": _I,
            "per_page": _I,
            "order": _S,
            "q": _S,
            "source_locale": _S,
            "target_locale": _S,
            "project_id": _I,
        },
        "req": [],
    },
    "/api/tm/stats": {"props": {"customer": _I}, "req": []},
    # Docs/API parity review (10-08): query params these handlers read through
    # request.query_params, so OpenAPI could not see them. Declared here so the tool
    # inputs match the API reference on /help. (include_string_images on the key export is
    # deliberately NOT listed: it stays off the agent-facing descriptors, see keys_hub2.)
    "/api/batches/{}": {"props": {"detail": {"type": "string", "enum": ["steps"]}}, "req": []},
    "/api/documents/{}/segments": {
        "props": {"detail": {"type": "string", "enum": ["segments"]}},
        "req": [],
    },
    "/api/glossaries": {"props": {"customer": _I, "unassigned": _S}, "req": []},
    "/api/glossaries/{}": {"props": {"cursor": _I}, "req": []},
    "/api/glossaries/{}/candidates": {"props": {"status": _S}, "req": []},
    "/api/projects/{}/glossary": {"props": {"cursor": _I}, "req": []},
    "/api/runs/{}": {"props": {"findings": _S, "visits": _S}, "req": []},
    "/api/workflows": {"props": {"archived": _S}, "req": []},
}

# One-line descriptions for read routes whose handler carries no docstring (FastAPI then
# derives a "summary" from the function name, which reads "Api Tm List" to an agent).
# Keyed by the param-name-agnostic path; used only when the operation has no description.
_DESCRIPTION_FALLBACKS: dict[str, str] = {
    "/api/content-types": "List the content types in the caller's scope.",
    "/api/documents/{}/paragraphs": (
        "A full-text document's paragraphs for one target locale, source beside target, for "
        "post-editing. Query: locale (required)."
    ),
    "/api/images": "List the image-localization documents in scope.",
    "/api/images/{}": "Show one image document with its detected regions. Query: locale.",
    "/api/images/{}/export": (
        "Download the localized image for one locale. Query: locale (required)."
    ),
    "/api/jobs/{}": "Status and result of one background job.",
    "/api/keys/{}/values": "One string key with its per-locale values.",
    "/api/langsets": "List the saved language sets: id, name, source and target locales.",
    "/api/projects/{}": (
        "Project detail: locales, customer, provider settings, documents with their status, "
        "TM and glossary counts, and the AI opt-out fields."
    ),
    "/api/projects/{}/documents": (
        "List a project's documents with id, filename, format, mode and derived status."
    ),
    "/api/projects/{}/keys": (
        "Page through a key project's string keys. Query: namespace, tag, q, status, "
        "untranslated_in, archived, include_deleted, cursor, limit."
    ),
    "/api/style-guides": "List the style guides in the caller's scope (without their body text).",
    "/api/style-guides/{}": "Show one style guide with its body text.",
    "/api/tm": (
        "Browse translation memory. Query: customer, page, per_page, order, q, source_locale, "
        "target_locale, project_id."
    ),
    "/api/tm/{}": "Show one translation-memory entry.",
    "/api/tm/stats": "Translation-memory statistics for one customer. Query: customer.",
    "/api/workflows": "List workflows (globals plus the caller's customer's). Query: archived.",
    "/api/workflows/{}": "Show one workflow with its steps, shape and usage.",
}


def enabled() -> bool:
    """The kill switch (settings ``utcp_manual_enabled``, default ON).

    Default-on is justified in the setting's own comment: the manual exposes a strict
    read-only SUBSET of what ``/openapi.json`` enumerates (that full schema needs a
    session or an API key once the login wall is on, issue #286). An
    unreadable Setting table (bare import-time contexts) resolves to the default (on);
    every runtime surface has the DB initialized before this is consulted."""
    try:
        return str(settings.get("utcp_manual_enabled", "1")) == "1"
    except Exception:
        return True


def is_included(method: str, path: str) -> bool:
    """THE inclusion predicate: a route is a UTCP tool iff it is a GET under /api whose
    normalized path is on the explicit allow-list. FAIL-CLOSED — anything not on the
    list (every non-GET, every non-/api path, every unclassified new route) is excluded.
    The GET + /api checks are redundant with the allow-list and kept as belt-and-
    suspenders so a list mistake can never leak a write verb."""
    return method.upper() == "GET" and path.startswith("/api/") and _norm(path) in _ALLOWLIST


def tool_name(method: str, path: str) -> str:
    """A STABLE, deterministic, collision-free snake_case tool name from a route.

    ``get`` + each path segment, with ``{param}`` placeholders rendered as
    ``by_<param>`` so no two distinct paths can ever collapse to the same name (a bare
    "drop the params" scheme would collide ``/api/projects`` with ``/api/projects/{pid}``
    and ``/api/tm`` with ``/api/tm/{entry_id}``). Hyphens become underscores. Examples::

        GET /api/projects                     -> get_projects
        GET /api/projects/{pid}               -> get_projects_by_pid
        GET /api/projects/{pid}/keys          -> get_projects_by_pid_keys
        GET /api/style-guides/{gid}           -> get_style_guides_by_gid

    Collision-freedom + stability were preferred over a prettier singularized form."""
    parts = [method.lower()]
    for seg in path.strip("/").split("/"):
        if not seg or seg == "api":
            continue
        m = re.fullmatch(r"\{(.+)\}", seg)
        if m:
            parts.append("by_" + _slug(m.group(1)))
        else:
            parts.append(_slug(seg))
    return "_".join(parts)


def _slug(s: str) -> str:
    """Lower-case, non-alphanumerics to underscores, collapse + trim."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s.lower())).strip("_")


def _description_of(op: dict, name: str, norm_path: str = "") -> str:
    """A one-line description from the OpenAPI operation: the FIRST paragraph of the
    ``description`` (the route's docstring), else the ``summary``, collapsed to single
    spaces and cut on a WORD boundary (adversarial review #11 — a mid-token cut once
    invented fake ``allow_held=`` syntax). The docstring is preferred because FastAPI
    auto-generates ``summary`` from the function name, which is far less useful."""
    desc = (op.get("description") or "").strip()
    if desc:
        # First paragraph only (up to the first blank line).
        text = re.split(r"\n\s*\n", desc, maxsplit=1)[0]
    else:
        text = _DESCRIPTION_FALLBACKS.get(norm_path) or (op.get("summary") or "").strip()
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = f"Read endpoint {name}."
    if len(text) > 280:
        # cut at the last word boundary within the budget, never mid-token.
        cut = text[:280].rsplit(" ", 1)[0].rstrip(" ,;:-")
        text = (cut or text[:277]) + "..."
    return text


def _inputs_of(op: dict, norm_path: str) -> dict:
    """A JSON-Schema ``inputs`` object: the operation's OpenAPI parameters (path +
    query) MERGED with any ``_INPUT_OVERRIDES`` entry for this path. Path params are
    always required; declared query params carry their OpenAPI ``required`` flag; an
    override may add both properties and required names the schema could not see."""
    props: dict = {}
    required: set[str] = set()
    for p in op.get("parameters", []) or []:
        loc = p.get("in")
        if loc not in ("path", "query"):
            continue  # header/cookie params are not part of the tool's declared inputs
        pname = p.get("name")
        if not pname:
            continue
        schema = dict(p.get("schema") or {"type": "string"})
        pdesc = (p.get("description") or "").strip()
        if pdesc:
            schema.setdefault("description", re.sub(r"\s+", " ", pdesc))
        props[pname] = schema
        if loc == "path" or p.get("required"):
            required.add(pname)
    override = _INPUT_OVERRIDES.get(norm_path)
    if override:
        for pname, schema in override["props"].items():
            props.setdefault(pname, dict(schema))
        required.update(override["req"])
    inputs: dict = {"type": "object", "properties": props}
    if required:
        inputs["required"] = sorted(required)
    return inputs


def _outputs_of(norm_path: str) -> dict:
    """``outputs`` schema. Raw-byte routes get a binary string schema (adversarial
    review #6 — an object schema was a false contract for file/zip/image bytes);
    everything else returns a JSON object body."""
    if norm_path in _BINARY_OUTPUT_PATHS:
        return {"type": "string", "format": "binary"}
    return {"type": "object"}


def _call_template(base_url: str, method: str, path: str) -> dict:
    """The UTCP HTTP call template for one route. ``url`` keeps the OpenAPI ``{param}``
    single-brace path syntax (which UTCP substitutes client-side); query/path args come
    from ``inputs``. Auth is the api_key-in-header declaration with a literal ``${...}``
    placeholder (never a real key)."""
    return {
        "call_template_type": "http",
        "url": f"{base_url}{path}",
        "http_method": method.upper(),
        "auth": {
            "auth_type": "api_key",
            "api_key": _AUTH_PLACEHOLDER,
            "var_name": _AUTH_VAR_NAME,
            "location": "header",
        },
    }


def build_tools(openapi_schema: dict, base_url: str) -> list[dict]:
    """Every included route in the schema, as UTCP tools, SORTED by name (deterministic).

    Two distinct paths can never produce the same tool name (see ``tool_name``), so the
    sort is a total order and the output is stable."""
    tools: list[dict] = []
    for path, item in (openapi_schema.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            if not is_included(method, path):
                continue
            norm_path = _norm(path)
            name = tool_name(method, path)
            tools.append(
                {
                    "name": name,
                    "description": _description_of(op, name, norm_path),
                    "tags": ["read-only", "get"],
                    "inputs": _inputs_of(op, norm_path),
                    "outputs": _outputs_of(norm_path),
                    "tool_call_template": _call_template(base_url, method, path),
                }
            )
    tools.sort(key=lambda t: t["name"])
    return tools


def _manual_version(tools: list[dict]) -> str:
    """``<base>+<hash>`` where the hash is over the serialized tool set (adversarial
    review #7). Deterministic for a fixed tool set, so it does not break byte-identity
    across renders, but it CHANGES when the exposed tools change — a version-aware
    client caching by version then sees a new value instead of serving stale."""
    blob = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{MANUAL_VERSION_BASE}+{hashlib.sha256(blob).hexdigest()[:8]}"


def _info(base_url: str) -> dict:
    """The manual's ``info`` block. Additive to the core UtcpManual model (which has no
    ``info`` field — pydantic ignores unknown fields by default, so this is safe) and it
    is where the EXPERIMENTAL warning + the "prefer MCP" pointer live, per BACKLOG 6b."""
    return {
        "title": f"{branding.brand_name()} (read-only, EXPERIMENTAL)",
        "description": (
            f"EXPERIMENTAL, additive UTCP manual for {branding.brand_name()}, a local MT-first "
            "Translation Management System. It describes a READ-ONLY subset of the "
            "/api surface (an explicit allow-list of GET routes only; no write, spend, "
            "config, credential or identity endpoints — UTCP has no confirmation "
            "primitive, so nothing side-effectful is exposed here). Generated from the "
            "OpenAPI schema at request time. If your client speaks MCP, prefer the "
            "first-class MCP server (integrations/mcp-server) instead: it carries the "
            "full tool set with two-phase confirmation on spend, server-side credential "
            "custody, and resources/prompts this manual cannot express. Authenticate "
            "every call with a personal API key in the X-Loc-Token header, supplied "
            "client-side as ${UTCP_LOC_STUDIO_KEY}. Full driver's manual: "
            f"{base_url}/help/llm."
        ),
    }


def build_manual(openapi_schema: dict, base_url: str) -> dict:
    """The complete UTCP manual as a plain dict, deterministic for a given base URL.

    ``base_url`` must have no trailing slash (the caller strips it), so ``url`` is
    ``{base_url}{path}`` with ``path`` carrying its own leading slash."""
    tools = build_tools(openapi_schema, base_url)
    return {
        "utcp_version": UTCP_SPEC_VERSION,
        "manual_version": _manual_version(tools),
        "info": _info(base_url),
        "tools": tools,
    }
