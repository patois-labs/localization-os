"""`connector` command group: manage integration connections headlessly.

Airtable / Jira / Confluence / Zendesk connections are CRUD-able, testable, rotatable,
schedulable, and drivable (pull/push/sync/retry + the targeted translate flows)
entirely from the CLI. The business logic lives in
app.services.connectors.store (already a clean service surface) and
app.services.scheduler; this module is a thin CLI shell over them.

All in-process (local-root, no RBAC — same trust as editing the DB directly).
--remote is reserved for a later phase; every command calls require_local() so a
--remote invocation fails cleanly rather than silently running locally.

SECRETS: tokens/passwords are password-like inputs, never echoed. `show`/`list`
report only whether credentials are set and the auth mode (token vs oauth), never
the values. `rotate` refuses OAuth connections (their tokens are minted by the
provider). `token-regen` prints the shared connector API token ONCE.

OAUTH — WHAT A CLI CANNOT DO: the interactive authorize leg (browser redirect +
provider consent) is inherently UI-only — Airtable/Atlassian have no device flow
for these apps, so there is no headless way to complete the consent. The CLI
therefore ships two OAuth *adjuncts*: `oauth-status` reports whether the provider
app is configured and a connection's auth mode/status, and `set-oauth-secret`
injects provider-minted tokens from a JSON file (advanced/CI). Setting up OAuth
from scratch still happens in the web UI.

TEST HOOK: `_set_transport()` installs a module-level httpx transport that the
commands pass into every store call. In normal use it is None (so the CLI never
touches the network unless the operator explicitly drives a live connection); the
verify suite injects an httpx.MockTransport so connector CRUD/test/rotate are
exercised fully offline, exactly like tests/verify_connectors.py.

--offline (CI/agent smoke flag, backlog item): `connector test` and `connector
browse` accept an explicit `--offline` flag that swaps in a tiny, DETERMINISTIC
canned httpx.MockTransport (see `_offline_transport`) instead of touching the
network — so a CI pipeline or an agent can smoke-test those two verbs with no
provider credentials and no live systems reachable. EXPLICIT-ONLY BY DESIGN:
there is no env var or implicit trigger, only the flag on the two verbs it
actually wires up (never the whole connector group), so an operator can never
accidentally run a "live" command against canned fixtures. Only a couple of
"core" kinds are mocked (see `_OFFLINE_KINDS`) — the fixtures are enough to prove
the CLI wiring end to end, not a full simulation of every provider's API; any
other kind refuses cleanly with a clear message rather than pretending to work.
SAFETY: the canned transport is always an httpx.MockTransport, which
egress_guard.egress_is_real() always classifies as FAKE egress (see
app/services/connectors/egress_guard.py) — so --offline output can NEVER be
eligible for a real external write-back by construction, the same invariant the
mock-egress guard already enforces for the rest of the hermetic suite (proven in
tests/verify_connector_offline.py). `connector test --offline` ALSO never
persists: it bypasses store.test_connection's unconditional _set_status/_log
write and calls store.build(...).test() directly, so running it against a REAL
connection id can never overwrite that row's status/last_message/last_test with
the canned result -- a genuinely-broken production connection can't read
false-green from an offline smoke test (also proven in
tests/verify_connector_offline.py).

CONCURRENCY (CLI vs server): the *-translate / *-webhook translate-pending
subcommands below run write_target synchronously in THIS process, guarded by a
stripe-lock pool that is per-process (app/services/connectors/strategy.py). It
is NOT coordinated with the live server's own writers. Avoid running a CLI
translate flow against a connection the server is actively pulling/pushing/
syncing at the same time -- two uncoordinated writers can race the same
(connection, record, locale) exists->create/update span. Cross-process
serialization is deferred to ADR-003 Tier 3.

Documented in: docs/architecture/connectors.md
"""

import json
import os
import sys

from app import branding

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    bounded_id,
    confirm,
    get_scoped_or_404,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_local,
    require_operator,
    require_org_scope,
)
from ._common import parse_kv as _parse_kv

# Module-level transport override for tests (see the module docstring). None in
# normal operation; the verify suite installs an httpx.MockTransport here so the
# store's live-network paths (test/rotate/pull/...) run fully offline.
_TRANSPORT = None


def _set_transport(transport) -> None:
    """Install (or clear) the module-level httpx transport used for store calls.
    Tests only — normal CLI runs leave this None."""
    global _TRANSPORT
    _TRANSPORT = transport


# --- --offline (CI/agent smoke flag) ------------------------------------------
# Kinds cheap to mock meaningfully for `test` + `browse` (module docstring). Kept
# small and honest rather than a full simulation of every provider's API.
_OFFLINE_KINDS = ("airtable", "jira")


def _offline_transport(kind: str):
    """A tiny, deterministic httpx.MockTransport for `kind`, or None if `kind`
    is not one of the supported _OFFLINE_KINDS. Enough for `test` (credential
    check) and `browse` (container listing) to succeed with no network — NOT a
    simulation of the whole provider API. Always an httpx.MockTransport, which
    egress_guard.egress_is_real() always classifies as fake egress (module
    docstring's safety note; proven in tests/verify_connector_offline.py)."""
    import httpx

    if kind == "jira":

        def handler(req: httpx.Request) -> httpx.Response:
            path, method = req.url.path, req.method
            if method == "GET" and path == "/rest/api/3/myself":
                return httpx.Response(200, json={"displayName": "Offline Mock User"})
            if method == "GET" and path == "/rest/api/3/project/search":
                return httpx.Response(
                    200, json={"values": [{"key": "OFFLINE", "name": "Offline Mock Project"}]}
                )
            return httpx.Response(404, json={"path": path, "method": method})

        return httpx.MockTransport(handler)

    if kind == "airtable":

        def handler(req: httpx.Request) -> httpx.Response:
            path, method = req.url.path, req.method
            if method == "GET" and path == "/v0/meta/bases":
                return httpx.Response(
                    200, json={"bases": [{"id": "appOFFLINE", "name": "Offline Mock Base"}]}
                )
            if method == "GET" and path == "/v0/meta/bases/appOFFLINE/tables":
                return httpx.Response(
                    200, json={"tables": [{"id": "tblOFFLINE", "name": "Offline Mock Table"}]}
                )
            # Record read (`test`'s credential check + any other base/table): any
            # non-meta GET under /v0/ reads as an empty, valid table.
            if method == "GET" and path.startswith("/v0/") and "/meta/" not in path:
                return httpx.Response(200, json={"records": []})
            return httpx.Response(404, json={"path": path, "method": method})

        return httpx.MockTransport(handler)

    return None


def _resolve_transport(args, kind: str):
    """The transport a `test`/`browse` call should use: the canned --offline
    mock (explicit flag only — never implicit) when requested, else the normal
    _TRANSPORT (None in real use; a test's injected MockTransport under the
    existing TEST HOOK). Refuses cleanly (usage error) when --offline is given
    for a kind with no canned fixture, rather than silently falling through to
    the network or to a misleading no-op."""
    if getattr(args, "offline", False):
        t = _offline_transport(kind)
        if t is None:
            raise CliError(
                f"--offline has no canned fixture for connector kind {kind!r} yet "
                f"(supported: {', '.join(_OFFLINE_KINDS)}).",
                EXIT_USAGE,
            )
        return t
    return _TRANSPORT


# factory:anchor cli.kinds  (scripts/factory appends new kinds to the _KINDS tuple below)
_KINDS = ("airtable", "jira", "confluence", "zendesk", "document360", "freshdesk", "zoho", "intercom", "salesforce", "notion", "helpscout", "strapi", "contentful", "shopify", "hubspot", "wordpress", "akeneo", "mcp_cms", "github", "sanity", "sitecore", "drupal", "plytix", "msgraph", "servicenow", "gitlab", "bitbucket", "gdocs")  # fmt: skip  # noqa: E501


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    conn = subparsers.add_parser(
        "connector",
        help="Integration connectors (Airtable/Jira/Confluence/Zendesk): "
        "CRUD, test, browse, rotate, schedule, sync, retry, OAuth adjuncts.",
        epilog="CAUTION: the *-translate (and *-webhook translate-pending) "
        "subcommands run synchronously in this CLI process and are NOT "
        "lock-coordinated with the live server -- avoid running one "
        "against a connection the server is actively writing to (see "
        "the module docstring / audit D1).",
    )
    sub = conn.add_subparsers(dest="connector_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a connection.")
    p.add_argument(
        "--kind",
        required=True,
        choices=_KINDS,
        help="connector kind for the NEW connection. Archived kinds "
        "(retired from new connections) are refused with a clear "
        "message; existing connections of any kind keep working.",
    )
    p.add_argument("--name", required=True, help="a display name for the connection.")
    p.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="a non-secret config key=value (repeatable), e.g. "
        "--config base_id=appX --config table=Content.",
    )
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="K[=V]",
        help="a SECRET credential (repeatable). Preferred forms keep the "
        "value off the command line: `--secret api_key` prompts "
        "(getpass, not echoed); `--secret api_key=-` reads one line "
        "from stdin. `--secret api_key=VALUE` still works for "
        "back-compat but warns (argv is visible in shell history "
        "and `ps`).",
    )
    p.add_argument(
        "--customer",
        dest="customer_id",
        default="",
        help="scope this connection to a customer id (P4; blank = global).",
    )
    p.add_argument(
        "--content-type",
        dest="content_type_id",
        default="",
        help="default content type id copied onto the auto-created sync "
        "project (SPEC-content-types; blank = General/untyped).",
    )
    p.set_defaults(func=_create)

    p = sub.add_parser("list", help="List connections (id, kind, name, status, auth).")
    p.add_argument("--kind", choices=_KINDS, help="only this kind.")
    p.set_defaults(func=_list)

    p = sub.add_parser(
        "show",
        help="Show one connection (config + credential/auth state; never the secret values).",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.set_defaults(func=_show)

    p = sub.add_parser(
        "update",
        help="Update a connection's name/config (config is "
        "merged, not replaced). Secrets are updated only if given.",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument("--name", default="", help="new display name (unchanged if omitted).")
    p.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="config key=value to set/merge (repeatable). base_url requires http:// or https://. "
        "Scope fields (Jira jql, Confluence space_key/cql, ServiceNow knowledge_base/kb_category, "
        "Notion parent_page_id) are admin only on the web form and /api once a credential is "
        "saved; this local CLI runs as the host operator (admin).",
    )
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="K[=V]",
        help="a SECRET credential to merge (repeatable). Omit to leave "
        "stored credentials untouched. Preferred forms keep the "
        "value off the command line: `--secret api_key` prompts "
        "(getpass, not echoed); `--secret api_key=-` reads one line "
        "from stdin. `--secret api_key=VALUE` still works for "
        "back-compat but warns (argv is visible in shell history "
        "and `ps`).",
    )
    p.add_argument(
        "--customer",
        dest="customer_id",
        default=None,
        help="re-scope to a customer id (P4). Pass '' to clear (make "
        "global); omit to leave the scope unchanged.",
    )
    p.add_argument(
        "--content-type",
        dest="content_type_id",
        default=None,
        help="set the default content type id (SPEC-content-types). Pass "
        "'' to clear; omit to leave it unchanged.",
    )
    p.set_defaults(func=_update)

    p = sub.add_parser("delete", help="Delete a connection (DESTRUCTIVE).")
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_delete)

    p = sub.add_parser(
        "prune-config",
        help="Prune stale content-hash entries a dropped "
        "target locale left in a connection's config (append-only "
        "content-hash maps are never pruned at write time). DRY-RUN by "
        "default (lists exactly what would be removed, per map); pass "
        "--apply to execute (audited).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("connection_id", type=bounded_id, nargs="?", help="connection id.")
    g.add_argument(
        "--all", action="store_true", dest="all_conns", help="prune EVERY connection (aggregated)."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually remove the entries; without it, only reports them.",
    )
    p.set_defaults(func=_prune_config)

    p = sub.add_parser(
        "test",
        help="Test a connection's credentials against the external system (store.test_connection).",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument(
        "--offline",
        action="store_true",
        help=f"CI/agent smoke flag: use a deterministic canned mock "
        f"transport instead of the network (no credentials/live "
        f"system needed); the connection's status/last_message/"
        f"last_test are NOT updated (a canned result never touches "
        f"real health state). Explicit only. Core kinds only "
        f"({', '.join(_OFFLINE_KINDS)}); any other kind refuses "
        f"cleanly.",
    )
    p.set_defaults(func=_test)

    p = sub.add_parser(
        "browse",
        help="List the connection's containers (Airtable "
        "bases / Jira projects / Confluence spaces), or an Airtable "
        "table's fields with --table.",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument("--base", help="Airtable base id (required with --table).")
    p.add_argument(
        "--table",
        help="Airtable table - list its field names instead of containers (Airtable only).",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help=f"CI/agent smoke flag: use a deterministic canned mock "
        f"transport instead of the network (no credentials/live "
        f"system needed). Explicit only. Core kinds only "
        f"({', '.join(_OFFLINE_KINDS)}); any other kind refuses "
        f"cleanly.",
    )
    p.set_defaults(func=_browse)

    p = sub.add_parser(
        "rotate",
        help="Replace a connection's credentials (proves the "
        "NEW ones work first; refuses OAuth connections).",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="K[=V]",
        required=True,
        help="the full NEW secret (repeatable). Replaces the stored "
        "credentials wholesale on success. Preferred forms keep the "
        "value off the command line: `--secret api_key` prompts "
        "(getpass, not echoed); `--secret api_key=-` reads one line "
        "from stdin. `--secret api_key=VALUE` still works for "
        "back-compat but warns (argv is visible in shell history "
        "and `ps`).",
    )
    p.set_defaults(func=_rotate)

    p = sub.add_parser(
        "schedule", help="Set or clear the automated-sync schedule (scheduler.set_schedule)."
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--every-minutes",
        type=int,
        metavar="N",
        help="run an automated sync every N minutes (floored at the service minimum).",
    )
    g.add_argument("--off", action="store_true", help="turn scheduling off.")
    p.set_defaults(func=_schedule)

    p = sub.add_parser(
        "pull",
        help=(
            f"Pull source content into a {branding.brand_name()} document (store.pull_connection)."
        ),
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.set_defaults(func=_pull)

    p = sub.add_parser(
        "push", help="Push translated segments back to the external system (store.push_connection)."
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.set_defaults(func=_push)

    p = sub.add_parser(
        "sync",
        help="Automated round-trip: pull -> MT-translate -> "
        "push, in-process synchronously (store.translate_now).",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.set_defaults(func=_sync)

    p = sub.add_parser(
        "retry",
        help="Re-run ONLY the items that failed in a prior "
        "run (store.retry_failed) from a failures JSON file.",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument(
        "--from-json",
        required=True,
        help="path to a JSON file: a summary['failures'] list, or the "
        "whole summary object with a 'failures' key.",
    )
    p.set_defaults(func=_retry)

    p = sub.add_parser(
        "jira-translate",
        help="Translate Jira issues into the "
        "connection's locales, writing labelled comments/fields.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Jira connection id.")
    p.add_argument(
        "--issue-keys",
        default="",
        help="comma/space separated issue keys (default: the connection's configured JQL).",
    )
    p.add_argument("--jql", default="", help="override the JQL query.")
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_jira_translate)

    p = sub.add_parser(
        "jira-translate-pending",
        help="Translate the Jira "
        "connection's PENDING issues (recorded by verified pushes "
        "while auto-translate-on-change is off) and clear them.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Jira connection id.")
    p.set_defaults(func=_jira_translate_pending)

    p = sub.add_parser(
        "jira-count",
        help="Preview how many Jira issues a JQL "
        "(scoped to the connection) would touch, translating none.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Jira connection id.")
    p.add_argument(
        "--issue-keys", default="", help="count only these issue keys (comma/space separated)."
    )
    p.add_argument(
        "--jql", default="", help="override the JQL query (ANDed onto the connection's scope)."
    )
    p.set_defaults(func=_jira_count)

    p = sub.add_parser(
        "confluence-translate",
        help="Translate Confluence pages into the connection's locales as localized child pages.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Confluence connection id.")
    p.add_argument("--page-ids", default="", help="comma/space separated page ids.")
    p.add_argument("--space-key", default="", help="override the space key.")
    p.add_argument("--cql", default="", help="override the CQL query.")
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_confluence_translate)

    p = sub.add_parser(
        "airtable-translate",
        help="Convention-driven Airtable translation (auto-detects target-language columns).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.add_argument("--base", default="", help="override base id.")
    p.add_argument("--table", default="", help="override table.")
    p.add_argument("--source-field", default="", help="override the source field.")
    p.add_argument(
        "--record-ids", default="", help="only these record ids (comma/space separated)."
    )
    p.set_defaults(func=_airtable_translate)

    # Official Airtable Webhooks API lifecycle (Phase 2). A subgroup so the leaves
    # are `connector airtable-webhook {register|unregister|status|translate-pending}`.
    awh = sub.add_parser(
        "airtable-webhook",
        help="Manage a connection's official "
        "Airtable webhook (register/unregister/status; requires a "
        "PUBLIC app_base_url -- a tunnel or public URL).",
    )
    awh_sub = awh.add_subparsers(dest="airtable_webhook_cmd", metavar="<subcommand>")
    awh_sub.required = True  # #20(e): a bare `airtable-webhook` must show the SCOPED
    # usage/error, not the top-level one (house convention for every nested subgroup).
    p = awh_sub.add_parser(
        "register", help="Register the official Airtable webhook (needs a public app_base_url)."
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.add_argument("--base", default="", help="override base id (else connection config).")
    p.add_argument("--table", default="", help="override table (else connection config).")
    p.add_argument("--source-field", default="", help="override the source field.")
    p.set_defaults(func=_airtable_webhook_register)
    p = awh_sub.add_parser(
        "unregister", help="Delete the official Airtable webhook and clear its stored state."
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.set_defaults(func=_airtable_webhook_unregister)
    p = awh_sub.add_parser(
        "status",
        help="Show the official Airtable webhook state (id/expiry/last ping/pending records).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.set_defaults(func=_airtable_webhook_status)
    p = awh_sub.add_parser(
        "translate-pending",
        help="Translate the records a ping "
        "recorded as pending (when auto-translate-on-change is off).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.set_defaults(func=_airtable_webhook_translate_pending)
    p = awh_sub.add_parser(
        "health",
        help="Probe the official webhook's health and "
        "surface a watched-field-deleted / paused error state "
        "(Airtable does not 404 a disabled hook).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.set_defaults(func=_airtable_webhook_health)

    # Record create/delete (round-2 #1) -- manual/API-triggered only, NOT the sync
    # loop. A subgroup so the leaves are `connector airtable-record {create|delete}`.
    arc = sub.add_parser(
        "airtable-record",
        help="Create or delete Airtable record(s) "
        "(manual push; not part of the auto-translate sync).",
    )
    arc_sub = arc.add_subparsers(dest="airtable_record_cmd", metavar="<subcommand>")
    arc_sub.required = True
    p = arc_sub.add_parser("create", help="Create new record(s) from a JSON array of field maps.")
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.add_argument("--base", default="", help="override base id (else connection config).")
    p.add_argument("--table", default="", help="override table (else connection config).")
    p.add_argument(
        "--fields-json",
        default="",
        help="JSON array of field maps, e.g. "
        '\'[{"Name":"Widget","Notes":"hi"}]\'. A single object is also '
        "accepted.",
    )
    p.add_argument(
        "--typecast",
        action="store_true",
        help="best-effort coerce string "
        "values into their field types (e.g. create a select option).",
    )
    p.set_defaults(func=_airtable_record_create)
    p = arc_sub.add_parser("delete", help="Delete record(s) by id.")
    p.add_argument("connection_id", type=bounded_id, help="Airtable connection id.")
    p.add_argument("--base", default="", help="override base id (else connection config).")
    p.add_argument("--table", default="", help="override table (else connection config).")
    p.add_argument("--record-ids", default="", help="record ids to delete (comma/space separated).")
    p.set_defaults(func=_airtable_record_delete)

    p = sub.add_parser(
        "zendesk-translate",
        help="Translate Zendesk Guide articles "
        "into the connection's locales as per-locale translation objects.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Zendesk connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article ids "
        "(comma/space separated; default: the connection's label/section scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_zendesk_translate)

    p = sub.add_parser(
        "document360-translate",
        help="Translate Document360 articles "
        "into the connection's langCodes as per-language article versions.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Document360 connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article ids "
        "(comma/space separated; default: the connection's version/category scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_document360_translate)

    p = sub.add_parser(
        "strapi-translate",
        help="Translate Strapi collection-type entries "
        "into the connection's locales as per-locale entry variants.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Strapi connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these entry ids "
        "(comma/space separated; '<contentType>:<id>', or a bare id for a "
        "single-content-type connection; default: the connection's content-type scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_strapi_translate)

    p = sub.add_parser(
        "contentful-translate",
        help="Translate Contentful entries into the "
        "connection's locales as new per-locale field keys on each entry.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Contentful connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these entry ids "
        "(comma/space separated; default: the connection's content-type scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_contentful_translate)

    p = sub.add_parser(
        "shopify-translate",
        help="Translate Shopify resources into the "
        "connection's locales as registered per-locale translations.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Shopify connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="optional resource GIDs to translate "
        "(comma/space separated, e.g. gid://shopify/Product/123); default = "
        "discover.",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_shopify_translate)

    p = sub.add_parser(
        "hubspot-translate",
        help="Translate HubSpot pages/blog posts "
        "into the connection's languages as HubSpot language variations.",
    )
    p.add_argument("connection_id", type=bounded_id, help="HubSpot connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="optional object ids to translate "
        "(comma/space separated, e.g. site-pages:123); default = discover.",
    )
    p.add_argument(
        "--locales", default="", help="override target languages (comma/space separated)."
    )
    p.set_defaults(func=_hubspot_translate)

    p = sub.add_parser(
        "akeneo-translate",
        help="Translate Akeneo product and product-model attribute values into the connection's locales as per-locale attribute values on the same record.",  # noqa: E501
    )
    p.add_argument("connection_id", type=bounded_id, help="Akeneo connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_akeneo_translate)
    p = sub.add_parser(
        "mcp_cms-translate",
        help="Translate CMS items through the CMS's own MCP server into the connection's "
        "locales as per-locale item variants.",
    )
    p.add_argument("connection_id", type=bounded_id, help="CMS via MCP connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_mcp_cms_translate)
    p = sub.add_parser(
        "servicenow-translate",
        help="Translate ServiceNow Knowledge articles into the connection's languages as "
        "separate Draft articles linked to the source by Parent.",
    )
    p.add_argument("connection_id", type=bounded_id, help="ServiceNow connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article sys_ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_servicenow_translate)

    p = sub.add_parser(
        "servicenow-resolve-link",
        help="Resolve a ServiceNow needs_link id-map row (a translation that already exists "
        "under the source's Parent but that the map never made): adopt it or ignore it. "
        "Never overwrites content.",
    )
    p.add_argument("connection_id", type=bounded_id, help="ServiceNow connection id.")
    p.add_argument("source_id", help="the SOURCE article sys_id of the flagged row.")
    p.add_argument("target_locale", help="the target language of the flagged row.")
    p.add_argument(
        "action",
        choices=("adopt", "ignore"),
        help="adopt = accept the existing article (next run updates it while it is Draft); "
        "ignore = drop the row (it re-flags next run).",
    )
    p.set_defaults(func=_servicenow_resolve_link)

    p = sub.add_parser(
        "msgraph-translate",
        help="Translate SharePoint or OneDrive files from the connection's source folder into "
        "the connection's locales as translated copies in per-locale folders.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Microsoft 365 Files connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_msgraph_translate)
    p = sub.add_parser(
        "plytix-translate",
        help="Translate Plytix product text attributes into the connection's per-language "
        "target attributes (T1 attribute map).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Plytix connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_plytix_translate)
    p = sub.add_parser(
        "github-translate",
        help="Localize GitHub resource files into the connection's locales as ONE commit "
        "on a working branch and ONE pull request.",
    )
    p.add_argument("connection_id", type=bounded_id, help="GitHub connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_github_translate)
    p = sub.add_parser(
        "gdocs-translate",
        help="Translate Google Docs from the connection's source Drive folder into the connection's locales as new Google Docs in per-locale folders.",  # noqa: E501
    )
    p.add_argument("connection_id", type=bounded_id, help="Google Docs connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_gdocs_translate)
    p = sub.add_parser(
        "sitecore-translate",
        help="Translate Sitecore XM Cloud content items into the connection's languages as language versions of the same item.",  # noqa: E501
    )
    p.add_argument("connection_id", type=bounded_id, help="Sitecore connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_sitecore_translate)
    p = sub.add_parser(
        "sanity-translate",
        help=(
            "Translate Sanity documents into the connection's languages (field-level language "
            "items or linked per-language documents, as configured), writing drafts only."
        ),
    )
    p.add_argument("connection_id", type=bounded_id, help="Sanity connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_sanity_translate)
    p = sub.add_parser(
        "drupal-translate",
        help="EXPERIMENTAL (alpha JSON:API Multilingual module). Translate Drupal entities (nodes, terms, blocks) into the connection's languages as the entity's own translations.",  # noqa: E501
    )
    p.add_argument("connection_id", type=bounded_id, help="Drupal connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_drupal_translate)
    p = sub.add_parser(
        "bitbucket-translate",
        help="Localize Bitbucket resource files into the connection's locales as ONE commit on a working branch and ONE pull request.",  # noqa: E501
    )
    p.add_argument("connection_id", type=bounded_id, help="Bitbucket connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_bitbucket_translate)
    p = sub.add_parser(
        "gitlab-translate",
        help=(
            "Localize GitLab resource files into the connection's locales as ONE commit on a "
            "working branch and ONE merge request."
        ),
    )
    p.add_argument("connection_id", type=bounded_id, help="GitLab connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these ids (comma/space separated; default: the connection scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_gitlab_translate)
    # factory:anchor cli.parsers  (scripts/factory inserts new <kind>-translate parser blocks above this line)  # noqa: E501

    p = sub.add_parser(
        "freshdesk-translate",
        help="Translate Freshdesk Solutions "
        "articles into the connection's languages as per-language "
        "article versions.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Freshdesk connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article ids "
        "(comma/space separated; default: the connection's category/folder scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_freshdesk_translate)

    p = sub.add_parser(
        "zoho-translate",
        help="Translate Zoho Desk articles into the connection's locales as per-locale "
        "translations.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Zoho Desk connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article ids "
        "(comma/space separated; default: the connection's department/category scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_zoho_translate)

    p = sub.add_parser(
        "intercom-translate",
        help="Translate Intercom Help Center "
        "articles into the connection's locales, written into each "
        "article's per-locale translated_content map (T1).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Intercom connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these article ids "
        "(comma/space separated; default: the connection's article list).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_intercom_translate)

    p = sub.add_parser(
        "salesforce-translate",
        help="Translate Salesforce Knowledge "
        "articles into the connection's languages, each written as the "
        "article's own translation version (T2).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Salesforce connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these KnowledgeArticleIds "
        "(comma/space separated; default: the connection's record-type scope).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_salesforce_translate)

    p = sub.add_parser(
        "salesforce-rediscover-fields",
        help="Describe Knowledge__kav and persist the per-record-type translatable field map.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Salesforce connection id.")
    p.set_defaults(func=_salesforce_rediscover_fields)

    p = sub.add_parser(
        "notion-translate",
        help="Translate Notion pages into the "
        "connection's locales, each created as a per-locale child page "
        "of the source (T3, document-per-locale).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Notion connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these source page ids "
        "(comma/space separated; default: the root's child pages or the database's rows).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_notion_translate)

    p = sub.add_parser(
        "notion-resolve-link",
        help="Resolve a Notion needs_link id-map "
        "row (a pre-existing target page the map never made): adopt it "
        "or ignore it. Never overwrites content.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Notion connection id.")
    p.add_argument("source_id", help="the SOURCE page id of the flagged row.")
    p.add_argument("target_locale", help="the target locale of the flagged row.")
    p.add_argument(
        "action",
        choices=("adopt", "ignore"),
        help="adopt = accept the existing page (next run updates it); "
        "ignore = drop the row (it re-flags next run).",
    )
    p.set_defaults(func=_notion_resolve_link)

    p = sub.add_parser(
        "helpscout-translate",
        help="Translate Help Scout Docs articles "
        "into the connection's locales, each created as a per-locale "
        "article in the mapped target collection (T3, document-per-locale).",
    )
    p.add_argument("connection_id", type=bounded_id, help="Help Scout connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these source article ids "
        "(comma/space separated; default: the mapped source collections).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_helpscout_translate)

    p = sub.add_parser(
        "helpscout-resolve-link",
        help="Resolve a Help Scout needs_link "
        "id-map row (a pre-existing target article the map never made): "
        "adopt it or ignore it. Never overwrites content.",
    )
    p.add_argument("connection_id", type=bounded_id, help="Help Scout connection id.")
    p.add_argument("source_id", help="the SOURCE article id of the flagged row.")
    p.add_argument("target_locale", help="the target locale of the flagged row.")
    p.add_argument(
        "action",
        choices=("adopt", "ignore"),
        help="adopt = accept the existing article (next run updates it); "
        "ignore = drop the row (it re-flags next run).",
    )
    p.set_defaults(func=_helpscout_resolve_link)

    p = sub.add_parser(
        "wordpress-translate",
        help="Translate WordPress posts/pages into "
        "the connection's locales, each created as a per-locale post in the "
        "target language (T3, document-per-locale via Polylang).",
    )
    p.add_argument("connection_id", type=bounded_id, help="WordPress connection id.")
    p.add_argument(
        "--article-ids",
        default="",
        help="only these source refs "
        "(comma/space separated '<type>:<id>', e.g. posts:12; a bare id for a "
        "single-type connection; default: the configured post types).",
    )
    p.add_argument("--locales", default="", help="override target locales (comma/space separated).")
    p.set_defaults(func=_wordpress_translate)

    p = sub.add_parser(
        "wordpress-resolve-link",
        help="Resolve a WordPress needs_link "
        "id-map row (a pre-existing Polylang-linked target post the map "
        "never made): adopt it or ignore it. Never overwrites content.",
    )
    p.add_argument("connection_id", type=bounded_id, help="WordPress connection id.")
    p.add_argument("source_id", help="the SOURCE post ref ('<type>:<id>') of the flagged row.")
    p.add_argument("target_locale", help="the target locale of the flagged row.")
    p.add_argument(
        "action",
        choices=("adopt", "ignore"),
        help="adopt = accept the existing post (next run updates it); "
        "ignore = drop the row (it re-flags next run).",
    )
    p.set_defaults(func=_wordpress_resolve_link)

    p = sub.add_parser(
        "token-regen",
        help="Regenerate the shared connector API token (printed ONCE; invalidates the old one).",
    )
    p.add_argument(
        "--yes", action="store_true", help="skip the confirmation (the old token stops working)."
    )
    p.set_defaults(func=_token_regen)

    p = sub.add_parser(
        "oauth-status", help="Report OAuth provider-app config + a connection's auth mode/status."
    )
    p.add_argument(
        "connection_id",
        type=bounded_id,
        nargs="?",
        help="connection id (omit to report only provider-app config).",
    )
    p.add_argument(
        "--kind", choices=_KINDS, help="report provider-app config for this kind (no connection)."
    )
    p.set_defaults(func=_oauth_status)

    p = sub.add_parser(
        "set-oauth-secret",
        help="Inject provider-minted OAuth tokens "
        "from a JSON file (advanced/CI - the interactive authorize "
        "leg stays UI-only).",
    )
    p.add_argument("connection_id", type=bounded_id, help="connection id.")
    p.add_argument(
        "--from-json",
        required=True,
        help="path to a JSON file: the oauth sub-dict (access_token, "
        'refresh_token, expires_at, ...) OR a {"oauth": {...}} wrapper.',
    )
    p.set_defaults(func=_set_oauth_secret)


# --- helpers -----------------------------------------------------------------
def _parse_secret_kv(pairs, what: str, emit) -> dict:
    """Parse `--secret` entries into a dict, keeping live credential values off
    argv where possible (D3 - audit Cluster D). Three forms:
      - `KEY` (no '=')  -> prompt via getpass (never echoed); requires a tty.
      - `KEY=-`         -> read the value from a single stdin line (e.g. a
        pipe: `printf '%s' "$TOKEN" | loctms connector create ... --secret
        api_key=-`).
      - `KEY=VALUE`     -> back-compat: use VALUE as-is, but warn to stderr -
        the OAuth path already uses --from-json and --password elsewhere uses
        getpass; this is the one credential input that still landed in argv
        (shell history, `ps`/`/proc`). The prompt or stdin form is preferred.
    A missing/blank key is a usage error either way, matching _parse_kv."""
    import getpass

    out = {}
    for raw in pairs or []:
        if "=" not in raw:
            key = raw.strip()
            if not key:
                raise CliError(
                    f"bad {what}: empty key (expected KEY, KEY=-, or KEY=VALUE)", EXIT_USAGE
                )
            if not sys.stdin.isatty():
                raise CliError(
                    f"{what} {key} has no value and stdin is not a terminal - use "
                    f"{what} {key}=VALUE, {what} {key}=- (stdin), or run "
                    "interactively to be prompted.",
                    EXIT_USAGE,
                )
            out[key] = getpass.getpass(f"{what} {key}: ")
            continue
        key, val = raw.split("=", 1)
        key = key.strip()
        if not key:
            # Never echo `raw` here: on this branch it can carry a live secret
            # value (`--secret =VALUE`), and stderr may be captured into logs/CI
            # transcripts where argv was not (D3 review finding).
            raise CliError(f"bad {what}: empty key (expected KEY, KEY=-, or KEY=VALUE)", EXIT_USAGE)
        if val == "-":
            out[key] = sys.stdin.readline().rstrip("\r\n")
        else:
            emit.warn(
                f"{what} {key}=VALUE puts the credential value on the command "
                "line (visible in shell history and `ps`/`/proc`); prefer "
                f"`{what} {key}` (prompts) or `{what} {key}=-` (stdin)."
            )
            out[key] = val
    return out


def _validate_secret_keys(kind: str, secret: dict) -> None:
    """#72: reject a `--secret` whose KEY the connector kind does not use. A mis-keyed
    credential (`api_token=` where airtable wants `api_key=`) otherwise lands in a key the
    connector never reads, yet the connection reports `auth: token` and looks authenticated
    -- a dead connection that only fails later at test/sync. Validated against the kind's
    DECLARED secret_fields (the same keys the web form renders), so a typo is caught at
    create/set time. An empty secret dict is a no-op (no credential to check)."""
    if not secret:
        return
    from app.kernel import connectors as _reg

    sp = _reg.spec(kind)
    if sp is None:
        return
    known = {f.name for f in (sp.secret_fields or [])}
    unknown = [k for k in secret if k not in known]
    if unknown:
        known_list = ", ".join(sorted(known)) or "(this kind takes no --secret)"
        raise CliError(
            f"unknown --secret key(s) for the {kind} connector: "
            f"{', '.join(sorted(unknown))}. Known credential key(s): {known_list}.",
            EXIT_USAGE,
        )


def _parse_list(raw) -> list:
    """Comma/space separated -> a de-duplicated, order-preserving list."""
    if not raw:
        return []
    out, seen = [], set()
    for tok in raw.replace(",", " ").split():
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _get_conn(cid: int, org_id=None):
    """Fetch a Connection or raise a clean CLI error. In-process --org scoping:
    a connection in another org is 404 (indistinguishable from missing).

    ffadmin Wave 5: this is THE local-CLI chokepoint every connection-targeting verb
    (show/update/delete/test/browse/rotate/schedule/pull/push/sync/retry/prune-config,
    every <kind>-translate, resolve-link, oauth-status, set-oauth-secret) funnels
    through, so a connection whose KIND is disabled on this install refuses here with a
    clear message naming the setting. Only the LOCAL path reaches this -- the --remote
    verbs delegate to the server before calling _get_conn, so the server's own 404
    answers (a laptop's flag state must not veto a healthy server), exactly like the
    feature-flag CLI dispatch gate."""
    from app.kernel import connectors as _reg
    from app.kernel.connectors import store

    conn = get_scoped_or_404(store.get(cid), cid, "connection", org_id)
    if not _reg.kind_enabled(conn.kind):
        raise CliError(
            f"connector kind '{conn.kind}' is disabled (settings connector_disabled_kinds).", 1
        )
    return conn


def _read_json(path: str):
    if not os.path.isfile(path):
        raise CliError(f"file not found: {path}", EXIT_USAGE)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise CliError(f"could not read JSON: {exc}", EXIT_USAGE) from exc


def _auth_summary(conn) -> str:
    """'oauth' | 'token' | 'none' — the connection's credential/auth state, never
    revealing the secret values."""
    from app.kernel.connectors import store

    secret = store.dec_secret(conn.secret_enc)
    if not secret:
        return "none"
    return store.auth_mode(secret)


def _conn_row(conn) -> dict:
    from app.kernel.connectors import store

    return {
        "id": conn.id,
        "kind": conn.kind,
        "name": conn.name,
        "status": conn.status,
        "auth": _auth_summary(conn),
        "project_id": conn.project_id,
        "customer_id": getattr(conn, "customer_id", None),
        "content_type_id": getattr(conn, "content_type_id", None),
        # connected projects: the project whose language list this connection follows
        "languages_project_id": store.languages_project_id(conn.id),
    }


# --- create ------------------------------------------------------------------
def _create(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import connectors as connectors_registry
    from app.kernel.connectors import store

    # ffadmin Wave 5: a DISABLED kind refuses creation (before the archived check -- a
    # disabled kind is gone entirely, whatever its tier), naming the setting.
    if not connectors_registry.kind_enabled(args.kind):
        raise CliError(
            f"connector kind '{args.kind}' is disabled (settings connector_disabled_kinds).", 1
        )
    # §6b: an archived connector is retired from the add picker on every surface
    # (web grid, /connectors/{kind} add form, and this verb). Refuse a NEW
    # connection with a clear message; existing connections keep working untouched.
    if connectors_registry.is_archived(args.kind):
        sp = connectors_registry.spec(args.kind)
        raise CliError(
            f"the {sp.name} connector is archived and no longer accepts new "
            f"connections; existing {sp.name} connections keep working, and new "
            "ones re-enable when the connector is rebuilt.",
            EXIT_USAGE,
        )
    cfg = _parse_kv(args.config, "--config")
    secret = _parse_secret_kv(args.secret, "--secret", emit)
    _validate_secret_keys(args.kind, secret)
    actor = getattr(args, "actor", None) or "cli"
    # #73 COUPLING: op_org_SCOPE (not op_org_id) -- the content-type pre-flight and
    # store.create both resolve through content_types.attachable, which now fails closed
    # on a bare None under an active gate. The --operator door hands access.UNSCOPED here
    # so the create still sees every org's global/attachable types; store.create collapses
    # the sentinel back to None for the persisted org (org_scope_value). --org / gate-off
    # are byte-identical.
    org_id = op_org_scope(args)
    cust_raw = getattr(args, "customer_id", "") or ""
    ct_raw = getattr(args, "content_type_id", "") or ""
    if ct_raw and not store.content_type_attachable(ct_raw, org_id, cust_raw):
        # SPEC-content-types phase-3 re-review LOW: store.create() would silently drop
        # an invalid/out-of-scope id to untyped (that contract is unchanged -- e.g. the
        # web UI still gets it). An EXPLICIT CLI flag gets a clean error instead, so a
        # typo/wrong-scope id doesn't create a connector that silently isn't what was
        # asked for.
        raise CliError(
            f"--content-type {ct_raw!r} is not attachable to this connection's scope "
            "(it must exist and be global or scoped to the same customer/org).",
            EXIT_USAGE,
        )
    try:
        conn = store.create(
            args.kind,
            args.name,
            cfg,
            secret,
            actor=actor,
            org_id=org_id,
            customer_id=cust_raw,
            content_type_id=ct_raw,
        )
    except store.ConnectorError as exc:  # e.g. an unreadable base URL (gate round 2)
        raise CliError(str(exc), EXIT_USAGE) from exc
    audit.log(
        actor=actor,
        action="connector.create",
        target=f"connection:{conn.id}",
        detail=f"kind={conn.kind}",
        customer_id=conn.customer_id,
    )
    emit.result(
        f"Created {conn.kind} connection #{conn.id}: {conn.name} (auth: {_auth_summary(conn)}).",
        {"ok": True, **_conn_row(conn)},
    )
    return 0


# --- list --------------------------------------------------------------------
def _list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.connector_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import connectors as _reg
    from app.kernel.connectors import store

    conns = store.list_connections(kind=args.kind, org_id=op_org_id(args))
    # ffadmin Wave 5: EXCLUDE disabled-kind connections so the LOCAL `connector list`
    # matches its remote twin GET /api/connectors (which filters) -- a disabled kind is
    # non-enumerable on BOTH transports, honouring the docs/CLI.md local/remote parity
    # promise. The rows are untouched and reappear when the kind is re-enabled.
    conns = [c for c in conns if _reg.kind_enabled(c.kind)]
    rows = [(c.id, c.kind, c.name, c.status, _auth_summary(c)) for c in conns]
    if emit.as_json:
        emit.json({"ok": True, "connections": [_conn_row(c) for c in conns]})
    else:
        emit.table(rows, headers=("id", "kind", "name", "status", "auth"))
    return 0


# --- show --------------------------------------------------------------------
def _show(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    conn = _get_conn(args.connection_id, op_org_id(args))
    cfg = conn.config or {}
    # Contentful: surface the PERSISTENT validation-blocked worklist (round-3 finding 2b
    # CLI parity) — pairs a scheduled run skips deterministically until fixed + retried.
    blocked = []
    if conn.kind == "contentful":
        for pair, reason in sorted((cfg.get("contentful_blocked") or {}).items()):
            aid, _sep, loc = str(pair).partition(":")
            blocked.append({"entry": aid, "locale": loc, "reason": str(reason)})
    info = {
        **_conn_row(conn),
        "last_message": conn.last_message,
        "last_test": conn.last_test,
        "last_sync": conn.last_sync,
        "created_by": conn.created_by,
        "config": cfg,
        "blocked": blocked,
    }
    if emit.as_json:
        emit.json({"ok": True, **info})
    else:
        emit.say(f"Connection #{conn.id}: {conn.name} ({conn.kind})")
        emit.say(
            f"  status: {conn.status}   auth: {info['auth']}"
            + (f"   project: {conn.project_id}" if conn.project_id else "")
        )
        if conn.last_message:
            emit.say(f"  last message: {conn.last_message}")
        emit.say("  config:")
        emit.table(
            sorted((k, str(v)) for k, v in cfg.items()) or [("(none)", "")],
            headers=("key", "value"),
        )
        if blocked:
            emit.say(
                f"  validation-blocked pairs ({len(blocked)}) - fix in Contentful, then "
                f"`connector retry`:"
            )
            emit.table(
                [(f"{b['entry']}:{b['locale']}", b["reason"]) for b in blocked],
                headers=("entry:locale", "reason"),
            )
    return 0


# --- update ------------------------------------------------------------------
def _update(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    cfg = _parse_kv(args.config, "--config")
    secret = _parse_secret_kv(args.secret, "--secret", emit)
    _validate_secret_keys(conn.kind, secret)
    # OAuth connections must not have their tokens overwritten via a plain update:
    # store.update merges only non-blank secret values, but injecting token creds
    # here would silently downgrade a v2 OAuth secret. Refuse a secret update on
    # an OAuth connection (use set-oauth-secret for that).
    if secret and store.is_oauth(conn):
        raise CliError(
            "this is an OAuth connection - use `connector set-oauth-secret` "
            "to change its tokens, not `update --secret`.",
            EXIT_USAGE,
        )
    # customer_id is only re-scoped when --customer was passed (None sentinel = leave
    # as-is; '' clears to global). Threaded to store.update only when supplied.
    upd_kwargs = {}
    if getattr(args, "customer_id", None) is not None:
        upd_kwargs["customer_id"] = args.customer_id
    # SPEC-content-types phase 3: same sentinel contract (None = leave as-is; '' clears).
    ct_raw = getattr(args, "content_type_id", None)
    if ct_raw is not None:
        upd_kwargs["content_type_id"] = ct_raw
        if ct_raw:
            # SPEC-content-types phase-3 re-review LOW: pre-flight the SAME check
            # store.update() applies internally, against the connection's RESOLVED
            # scope (the --customer just requested in THIS call, else its existing
            # one) -- an EXPLICIT --content-type request that is invalid/out-of-scope
            # is a clean CLI error rather than a silent drop to unset (store.update()'s
            # own silent-drop contract is unchanged; this is CLI-flag UX only).
            eff_customer = (
                args.customer_id
                if getattr(args, "customer_id", None) is not None
                else conn.customer_id
            )
            if not store.content_type_attachable(ct_raw, conn.org_id, eff_customer):
                raise CliError(
                    f"--content-type {ct_raw!r} is not attachable to this "
                    "connection's scope (it must exist and be global or scoped to "
                    "the same customer/org).",
                    EXIT_USAGE,
                )
    actor = getattr(args, "actor", None) or "cli"
    try:
        store.update(
            conn.id, args.name or conn.name, cfg, secret or None, actor=actor, **upd_kwargs
        )
    except store.ConnectorError as exc:  # e.g. an unreadable base URL (gate round 2)
        raise CliError(str(exc), EXIT_USAGE) from exc
    fresh = store.get(conn.id)
    emit.result(f"Updated connection #{fresh.id}: {fresh.name}.", {"ok": True, **_conn_row(fresh)})
    return 0


# --- delete ------------------------------------------------------------------
def _prune_config(args, emit) -> int:
    """Prune (or dry-run) stale content-hash entries left by dropped target locales.
    Local-only maintenance verb (residual sweep / adversarial-review H2/M3). DRY-RUN by
    default; --apply executes + audits."""
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    apply = bool(getattr(args, "apply", False))
    actor = getattr(args, "actor", None) or "cli"
    if getattr(args, "all_conns", False):
        agg = store.prune_all_orphaned_config(apply=apply, actor=actor, org_id=op_org_scope(args))
        verb = "Removed" if apply else "Would remove"
        lines = [
            f"{verb} {agg['total']} orphaned content-hash entr"
            f"{'y' if agg['total'] == 1 else 'ies'} across "
            f"{len(agg['connections'])} connection(s)"
            f"{'' if apply else ' (dry-run; pass --apply to execute)'}."
        ]
        for rep in agg["connections"]:
            for m in rep["removed"]:
                lines.append(
                    f"  connection {rep['connection_id']} [{m['map']}]: "
                    f"{m['count']} entr{'y' if m['count'] == 1 else 'ies'}"
                )
        emit.result("\n".join(lines), {"ok": True, "applied": apply, **agg})
        return 0
    # Single connection: resolve it through the org guard first (clean 404).
    conn = _get_conn(args.connection_id, op_org_id(args))
    rep = store.prune_orphaned_config(conn.id, apply=apply, actor=actor)
    verb = "Removed" if apply else "Would remove"
    lines = [
        f"{verb} {rep['total']} orphaned content-hash entr"
        f"{'y' if rep['total'] == 1 else 'ies'} from connection #{conn.id} "
        f"({conn.name}){'' if apply else ' (dry-run; pass --apply to execute)'}."
    ]
    for m in rep["removed"]:
        lines.append(f"  [{m['map']}]: {', '.join(m['keys'])}")
    emit.result("\n".join(lines), {"ok": True, "applied": apply, **rep})
    return 0


def _delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if not confirm(emit, f"Delete {conn.kind} connection #{conn.id} ({conn.name})?", args.yes):
        raise CliError("aborted.", 1)
    retained = store.delete(conn.id)
    actor = getattr(args, "actor", None) or "cli"
    audit.log(
        actor=actor,
        action="connector.delete",
        target=f"connection:{conn.id}",
        detail=f"kind={conn.kind}",
        customer_id=conn.customer_id,
    )
    # Retention truth (audit Finding 4), web+CLI parity: the connection's synced project
    # and its pulled content survive the connection delete — full erasure = delete the
    # project separately. Never auto-delete it (would destroy human post-edits + TM).
    text = f"Deleted connection #{conn.id} ({conn.name})."
    if retained:
        text += (
            f" Project '{retained}' and its pulled content are retained; "
            "delete the project separately for full erasure."
        )
    emit.result(text, {"ok": True, "id": conn.id, "deleted": True, "retained_project": retained})
    return 0


# --- test --------------------------------------------------------------------
def _test(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    transport = _resolve_transport(args, conn.kind)
    if getattr(args, "offline", False):
        # Offline smoke test: deliberately bypasses store.test_connection, which
        # unconditionally persists status/last_message/last_test via _set_status +
        # _log. A canned mock result must NEVER overwrite a REAL connection's health
        # state -- a genuinely-broken production connection must not read
        # false-green just because someone ran an offline smoke test against it
        # (review finding C1). store.build (not build_fresh) also skips any OAuth
        # proactive-refresh network/persist side effect. The row is left
        # byte-untouched; the result is printed only, never written.
        ok, msg = store.build(conn, transport=transport).test()
        emit.result(
            f"{'OK' if ok else 'FAILED'}: {msg} (offline smoke test - "
            "connection health NOT updated)",
            {"ok": ok, "connection_id": args.connection_id, "message": msg, "offline": True},
        )
        return 0 if ok else 1
    actor = getattr(args, "actor", None) or "cli"
    ok, msg = store.test_connection(args.connection_id, actor=actor, transport=transport)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": args.connection_id, "message": msg},
    )
    return 0 if ok else 1


# --- browse ------------------------------------------------------------------
def _browse(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import base as cbase
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    transport = _resolve_transport(args, conn.kind)
    if args.table:
        if conn.kind != "airtable":
            raise CliError("field browse (--table) is Airtable-only.", EXIT_USAGE)
        if not args.base:
            raise CliError("--base is required with --table.", EXIT_USAGE)
        try:
            fields = store.schema_fields(conn.id, args.base, args.table, transport=transport)
        except cbase.ConnectorError as exc:
            raise CliError(str(exc), 1) from exc
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "connection_id": conn.id,
                    "base": args.base,
                    "table": args.table,
                    "fields": fields,
                }
            )
        else:
            emit.say(f"Fields in {args.base}/{args.table}:")
            emit.table([(f,) for f in fields] or [("(none)",)], headers=("field",))
        return 0
    containers = store.containers(conn.id, transport=transport)
    if emit.as_json:
        emit.json({"ok": True, "connection_id": conn.id, "containers": containers})
    else:
        emit.table(
            [(c.get("id", ""), c.get("name", "")) for c in containers] or [("(none)", "")],
            headers=("id", "name"),
        )
    return 0


# --- rotate ------------------------------------------------------------------
def _rotate(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if store.is_oauth(conn):
        raise CliError(
            "OAuth connections reconnect via the provider, not manual "
            "rotation (use the web UI reconnect flow).",
            EXIT_USAGE,
        )
    new_secret = _parse_secret_kv(args.secret, "--secret", emit)
    if not new_secret:
        raise CliError("at least one --secret KEY=VALUE is required.", EXIT_USAGE)
    _validate_secret_keys(conn.kind, new_secret)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg = store.rotate_secret(conn.id, new_secret, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'Rotated' if ok else 'Rotation failed'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg},
    )
    return 0 if ok else 1


# --- schedule ----------------------------------------------------------------
def _schedule(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import scheduler
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    actor = getattr(args, "actor", None) or "cli"
    minutes = 0 if args.off else max(0, args.every_minutes)
    scheduler.set_schedule(conn.id, minutes, actor=actor)
    # Re-read: set_schedule floors a nonzero interval at the service minimum, so
    # report the effective value the store actually persisted.
    fresh = scheduler.interval_minutes(store.get(conn.id))
    if fresh > 0:
        emit.result(
            f"Scheduled connection #{conn.id}: automated sync every {fresh} minute(s) "
            f"(first run one interval out).",
            {"ok": True, "connection_id": conn.id, "every_minutes": fresh},
        )
    else:
        emit.result(
            f"Scheduling turned OFF for connection #{conn.id}.",
            {"ok": True, "connection_id": conn.id, "every_minutes": 0},
        )
    return 0


# --- pull / push / sync ------------------------------------------------------
def _pull(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.connector_pull(args, emit)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    _get_conn(args.connection_id, op_org_id(args))
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, doc_id = store.pull_connection(args.connection_id, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": args.connection_id, "message": msg, "document_id": doc_id},
    )
    return 0 if ok else 1


def _push(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.connector_push(args, emit)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    _get_conn(args.connection_id, op_org_id(args))
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, n = store.push_connection(args.connection_id, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": args.connection_id, "message": msg, "pushed": n},
    )
    return 0 if ok else 1


def _sync(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.connector_sync(args, emit)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    _get_conn(args.connection_id, op_org_id(args))
    actor = getattr(args, "actor", None) or "cli"
    # translate_now dispatches to the right flow per kind; runs synchronously
    # in-process (no job worker — the one-shot CLI has none).
    ok, msg, summary = store.translate_now(args.connection_id, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": args.connection_id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _retry(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    _get_conn(args.connection_id, op_org_id(args))
    data = _read_json(args.from_json)
    if isinstance(data, dict):
        # Dual-surface parity with POST /api/connectors/{cid}/retry: retry the run's real
        # failures AND any INCOMPLETE / validation-BLOCKED pairs (Shopify/Contentful partial
        # writes). Both keys are absent for other connectors, so this is a no-op for them.
        # Piping a run summary must NOT silently skip incomplete pairs — for Shopify those
        # are the sole recovery route for a held/poison-dropped key.
        failures = (
            (data.get("failures") or [])
            + (data.get("incomplete") or [])
            + (data.get("blocked") or [])
        )
    else:
        failures = data
    if not isinstance(failures, list):
        raise CliError(
            "the JSON must be a failures list, or an object with a "
            "'failures'/'incomplete'/'blocked' list.",
            EXIT_USAGE,
        )
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.retry_failed(
        args.connection_id, failures, actor=actor, transport=_TRANSPORT
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": args.connection_id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


# --- targeted translate flows ------------------------------------------------
def _jira_translate(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "jira":
        raise CliError(f"connection #{conn.id} is not a Jira connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.jira_translate(
        conn.id,
        issue_keys=_parse_list(args.issue_keys) or None,
        jql=args.jql or None,
        target_locales=_parse_list(args.locales) or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _jira_translate_pending(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "jira":
        raise CliError(f"connection #{conn.id} is not a Jira connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.jira_translate_pending(conn.id, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _jira_count(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "jira":
        raise CliError(f"connection #{conn.id} is not a Jira connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.jira_count(
        conn.id,
        jql=args.jql or None,
        issue_keys=_parse_list(args.issue_keys) or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _confluence_translate(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "confluence":
        raise CliError(f"connection #{conn.id} is not a Confluence connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.confluence_translate(
        conn.id,
        page_ids=_parse_list(args.page_ids) or None,
        space_key=args.space_key or None,
        cql=args.cql or None,
        target_locales=_parse_list(args.locales) or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _airtable_translate(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "airtable":
        raise CliError(f"connection #{conn.id} is not an Airtable connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.dynamic_translate(
        conn.id,
        base_id=args.base or None,
        table=args.table or None,
        source_field=args.source_field or None,
        record_ids=_parse_list(args.record_ids) or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _require_airtable(args):
    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "airtable":
        raise CliError(f"connection #{conn.id} is not an Airtable connection.", EXIT_USAGE)
    return conn


def _airtable_webhook_register(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg = store.register_airtable_webhook(
        conn.id,
        base_id=args.base or None,
        table=args.table or None,
        source_field=args.source_field or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}", {"ok": ok, "connection_id": conn.id, "message": msg}
    )
    return 0 if ok else 1


def _airtable_webhook_unregister(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg = store.unregister_airtable_webhook(conn.id, actor=actor, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}", {"ok": ok, "connection_id": conn.id, "message": msg}
    )
    return 0 if ok else 1


def _airtable_webhook_status(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    st = store.airtable_webhook_status(conn.id)
    if st.get("registered"):
        line = (
            f"webhook {st['id']} -- expires {st.get('expiration_time') or '?'}, "
            f"last ping {st.get('last_ping_at') or 'never'}, "
            f"{len(st.get('pending_record_ids') or [])} pending, "
            f"auto-translate {'ON' if st.get('auto_translate_on_change') else 'OFF'}"
        )
        if st.get("last_error"):
            line += f" -- WARNING: {st['last_error']}"
    else:
        line = "no official webhook registered" + (
            ""
            if st.get("app_base_url_ok")
            else " (app_base_url is not a public https URL -- set it to register)"
        )
    emit.result(line, {"ok": True, "connection_id": conn.id, **st})
    return 0


def _airtable_webhook_translate_pending(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    actor = getattr(args, "actor", None) or "cli"
    # force=True drains the pending queue AND pulls any newer payloads first.
    ok, msg, summary = store.consume_airtable_payloads(
        conn.id, actor=actor, transport=_TRANSPORT, force=True
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _airtable_webhook_health(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    ok, msg, info = store.check_airtable_webhook_health(conn.id, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'WARNING'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, **info},
    )
    return 0 if ok else 1


def _airtable_record_create(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    import json as _json

    from app.kernel.connectors import store

    conn = _require_airtable(args)
    actor = getattr(args, "actor", None) or "cli"
    try:
        parsed = _json.loads(args.fields_json or "[]")
    except ValueError as e:
        raise CliError(f"--fields-json is not valid JSON: {e}", EXIT_USAGE) from e
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise CliError(
            "--fields-json must be a JSON array of field maps (or one object).", EXIT_USAGE
        )
    ok, msg, summary = store.airtable_create_records(
        conn.id,
        parsed,
        base_id=args.base or None,
        table=args.table or None,
        typecast=bool(getattr(args, "typecast", False)),
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _airtable_record_delete(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _require_airtable(args)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.airtable_delete_records(
        conn.id,
        _parse_list(args.record_ids),
        base_id=args.base or None,
        table=args.table or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


# The article-style KB connectors share one translate shape (article_ids + locales ->
# store.<kind>_translate) and, for two of them, one resolve-link shape. Only the kind
# guard's article/display phrase and the store method name vary, so the bodies live in
# these two generics and each command is a thin, named delegate (kept named because the
# connector verify suites import the per-kind handlers directly). jira/confluence/
# airtable stay bespoke (multiple selectors / different signatures).
def _kind_translate(kind: str, article: str, args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != kind:
        raise CliError(f"connection #{conn.id} is not {article} connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = getattr(store, f"{kind}_translate")(
        conn.id,
        article_ids=_parse_list(args.article_ids) or None,
        target_locales=_parse_list(args.locales) or None,
        actor=actor,
        transport=_TRANSPORT,
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _kind_resolve_link(kind: str, article: str, args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != kind:
        raise CliError(f"connection #{conn.id} is not {article} connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = getattr(store, f"{kind}_resolve_link")(
        conn.id, args.source_id, args.target_locale, args.action, actor=actor
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _zendesk_translate(args, emit) -> int:
    return _kind_translate("zendesk", "a Zendesk", args, emit)


def _document360_translate(args, emit) -> int:
    return _kind_translate("document360", "a Document360", args, emit)


def _strapi_translate(args, emit) -> int:
    return _kind_translate("strapi", "a Strapi", args, emit)


def _contentful_translate(args, emit) -> int:
    return _kind_translate("contentful", "a Contentful", args, emit)


def _shopify_translate(args, emit) -> int:
    return _kind_translate("shopify", "a Shopify", args, emit)


def _hubspot_translate(args, emit) -> int:
    return _kind_translate("hubspot", "a HubSpot", args, emit)


def _freshdesk_translate(args, emit) -> int:
    return _kind_translate("freshdesk", "a Freshdesk", args, emit)


def _zoho_translate(args, emit) -> int:
    return _kind_translate("zoho", "a Zoho Desk", args, emit)


def _intercom_translate(args, emit) -> int:
    return _kind_translate("intercom", "an Intercom", args, emit)


def _salesforce_translate(args, emit) -> int:
    return _kind_translate("salesforce", "a Salesforce", args, emit)


def _salesforce_rediscover_fields(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    if conn.kind != "salesforce":
        raise CliError(f"connection #{conn.id} is not a Salesforce connection.", EXIT_USAGE)
    actor = getattr(args, "actor", None) or "cli"
    ok, msg, summary = store.salesforce_rediscover_fields(
        conn.id, actor=actor, transport=_TRANSPORT
    )
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "connection_id": conn.id, "message": msg, "summary": summary},
    )
    return 0 if ok else 1


def _notion_translate(args, emit) -> int:
    return _kind_translate("notion", "a Notion", args, emit)


def _notion_resolve_link(args, emit) -> int:
    return _kind_resolve_link("notion", "a Notion", args, emit)


def _helpscout_translate(args, emit) -> int:
    return _kind_translate("helpscout", "a Help Scout", args, emit)


def _helpscout_resolve_link(args, emit) -> int:
    return _kind_resolve_link("helpscout", "a Help Scout", args, emit)


def _wordpress_translate(args, emit) -> int:
    return _kind_translate("wordpress", "a WordPress", args, emit)


def _wordpress_resolve_link(args, emit) -> int:
    return _kind_resolve_link("wordpress", "a WordPress", args, emit)


def _akeneo_translate(args, emit) -> int:
    return _kind_translate("akeneo", "an Akeneo product", args, emit)


def _mcp_cms_translate(args, emit) -> int:
    return _kind_translate("mcp_cms", "a CMS (via MCP)", args, emit)


def _servicenow_translate(args, emit) -> int:
    return _kind_translate("servicenow", "a ServiceNow", args, emit)


def _servicenow_resolve_link(args, emit) -> int:
    return _kind_resolve_link("servicenow", "a ServiceNow", args, emit)


def _msgraph_translate(args, emit) -> int:
    return _kind_translate("msgraph", "a SharePoint or OneDrive file", args, emit)


def _plytix_translate(args, emit) -> int:
    return _kind_translate("plytix", "a Plytix product", args, emit)


def _github_translate(args, emit) -> int:
    return _kind_translate("github", "a GitHub resource file", args, emit)


def _sanity_translate(args, emit) -> int:
    return _kind_translate("sanity", "a Sanity", args, emit)


def _gdocs_translate(args, emit) -> int:
    return _kind_translate("gdocs", "a Google Doc", args, emit)


def _sitecore_translate(args, emit) -> int:
    return _kind_translate("sitecore", "a Sitecore item", args, emit)


def _drupal_translate(args, emit) -> int:
    return _kind_translate("drupal", "a Drupal", args, emit)


def _bitbucket_translate(args, emit) -> int:
    return _kind_translate("bitbucket", "a Bitbucket resource file", args, emit)


def _gitlab_translate(args, emit) -> int:
    return _kind_translate("gitlab", "a GitLab resource file", args, emit)


# factory:anchor cli.handlers  (scripts/factory inserts new _<kind>_translate handlers above this line)  # noqa: E501


# --- token-regen -------------------------------------------------------------
def _token_regen(args, emit) -> int:
    # #515: the shared connector token is install-wide (every org's integrations use it),
    # so rotating it is a host-operator action: --org cannot scope it.
    require_operator(args, emit)
    import secrets as _secrets

    from app.kernel import audit, settings

    if not confirm(
        emit,
        "Regenerate the shared connector API token? (the current token stops working immediately)",
        args.yes,
    ):
        raise CliError("aborted.", 1)
    token = "lsk_" + _secrets.token_urlsafe(24)
    settings.set_many({"connector_api_token": token})
    actor = getattr(args, "actor", None) or "cli"
    audit.log(
        actor=actor,
        action="connector.token_regenerate",
        target="connector_api_token",
        detail="(secret rotated)",
    )
    if emit.as_json:
        emit.json({"ok": True, "connector_api_token": token})
    else:
        emit.say("Regenerated the shared connector API token (shown ONCE):")
        emit.say("=" * 60)
        emit.say(f"  {token}")
        emit.say("=" * 60)
        emit.say("Store it now - it cannot be recovered.")
    return 0


# --- oauth-status ------------------------------------------------------------
def _oauth_status(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.connectors import oauth

    # Resolve which provider kind we're reporting on.
    conn = None
    if args.connection_id is not None:
        conn = _get_conn(args.connection_id, op_org_id(args))
        kind = conn.kind
    elif args.kind:
        kind = args.kind
    else:
        raise CliError("give a connection id or --kind.", EXIT_USAGE)

    # ffadmin Wave 5: `oauth-status --kind X` takes the kind off the FLAG (no connection to
    # gate through _get_conn), so gate it here -- a disabled kind must not disclose its
    # OAuth config (rc0 would be an enumeration signal). The connection-id branch above
    # already refused a disabled kind inside _get_conn.
    from app.kernel import connectors as _reg

    if not _reg.kind_enabled(kind):
        raise CliError(
            f"connector kind '{kind}' is disabled (settings connector_disabled_kinds).", 1
        )

    configured = oauth.provider_app_configured(kind)
    supported = oauth.supports_oauth(kind)
    payload = {
        "ok": True,
        "kind": kind,
        "oauth_supported": supported,
        "provider_app_configured": configured,
    }
    if conn is not None:
        payload.update(
            {"connection_id": conn.id, "auth_mode": _auth_summary(conn), "status": conn.status}
        )
    if emit.as_json:
        emit.json(payload)
    else:
        emit.say(
            f"OAuth for kind '{kind}': supported={supported}, provider-app configured={configured}."
        )
        if not configured:
            emit.say(
                "  (Set oauth_*_client_id/secret via `config set` or the "
                "Settings UI, then authorize IN THE WEB UI - the browser "
                "consent leg cannot be done headlessly.)"
            )
        if conn is not None:
            emit.say(
                f"  connection #{conn.id}: auth_mode={payload['auth_mode']}, status={conn.status}."
            )
    return 0


# --- set-oauth-secret --------------------------------------------------------
def _set_oauth_secret(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel.connectors import store

    conn = _get_conn(args.connection_id, op_org_id(args))
    data = _read_json(args.from_json)
    # Accept either the raw oauth sub-dict or a {"oauth": {...}} wrapper.
    oauth_dict = data.get("oauth") if isinstance(data, dict) and "oauth" in data else data
    if not isinstance(oauth_dict, dict) or not oauth_dict.get("access_token"):
        raise CliError(
            "the JSON must be an oauth dict with an 'access_token' "
            '(or a {"oauth": {...}} wrapper).',
            EXIT_USAGE,
        )
    store.set_oauth_secret(conn.id, oauth_dict)
    actor = getattr(args, "actor", None) or "cli"
    audit.log(
        actor=actor,
        action="connector.set_oauth_secret",
        target=f"connection:{conn.id}",
        detail="(oauth tokens injected)",
        customer_id=conn.customer_id,
    )
    fresh = store.get(conn.id)
    emit.result(
        f"Injected OAuth tokens for connection #{conn.id} (auth: {_auth_summary(fresh)}). "
        f"Note: the interactive authorize leg stays UI-only.",
        {"ok": True, "connection_id": conn.id, "auth": _auth_summary(fresh)},
    )
    return 0
