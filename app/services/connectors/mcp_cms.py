"""CMS via MCP (`mcp_cms`) - PROTOTYPE connector: Localization OS as an MCP CLIENT of a
CMS's own MCP server ("option 2"). Prototype only.

WHAT IT IS. Instead of a vendor REST API, the connector reaches the CMS through the MCP
server the CMS already runs. The operator maps FOUR operations onto that server's tools:

    list    the source items           (tool + JSON args template + where the items are)
    get     one item in one locale     (also the T2 existence probe)
    write   one item in one locale     (create-or-update; `{{fields}}` carries the text)
    publish optional, after a complete write, off by default

Everything else is the shared framework, unchanged: flow.run_translate_flow (egress guard,
write ceiling, NEEDS_REVIEW exclusion, #640 held-back rules, content-hash suppression,
retry, run messages), SubResourceStrategy (T2 probe -> create or update, D1 lock), budgets.

SECURITY (spec section 5).
  * The MCP URL goes through the connector egress seam: `_validate_target` at config use and
    `egress.GuardedTransport` on every hop (SSRF resolve-and-pin, private allowlist, no
    redirects followed), 32 MiB bounded reads, the 30 s I/O budget (mcp_wire.py).
  * Only the four mapped tools can ever be called, with arguments built ONLY from the
    operator's templates plus our own values (item id, locale, translated fields). A tool
    whose name reads as destructive (delete/remove/unpublish/discard/...) or that the server
    annotates `destructiveHint: true` is refused for every role. A server response never
    names a tool, adds an argument or changes the mapping: results are parsed as data.
  * Changing the URL OR the tool mapping clears the stored token (store_part2 credential
    pin), so only someone who holds the token can point it at different tools.
  * `fields` (an explicit allowlist) is REQUIRED: MCP tool schemas carry no CMS i18n flags
    (lesson #638). When the write tool's input schema declares the fields object, a listed
    field must be a string there, and a property marked `x-localized: false` (or
    `localized: false`) is skipped.
  * #640: a held-back unit never ships. A new locale variant is not created while a unit is
    held (flow engine); an update leaves the held field out, filling a schema-REQUIRED field
    from that variant's OWN stored value (never the source text); no hash; retried later.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import base64
import json
import logging
import re

from app.services.connectors import flow
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    fetch_cap,
    harvest_context_field,
)
from app.services.connectors.base import content_hash as _content_hash
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.mcp_wire import (
    McpSession,
    McpToolError,
    normalize_mode,
    normalize_timeout,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult
from app.services.connectors.tracking import SelectionTracker

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "2025-06-18"

_HELD_BACK_PREFIX = "held-back:"
_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)
_DRAFT_SENTINEL = "draft"  # SubResourceStrategy injects payload["draft"] = bool
_ALIAS_PREFIX = "\x00field:"
_SYSTEM_FIELDS = frozenset(
    {"id", "documentId", "locale", "localizations", "createdAt", "updatedAt", "publishedAt"}
)
_TOOL_RE = re.compile(r"[A-Za-z0-9_.\-/]{1,128}")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_DESTRUCTIVE_RE = re.compile(
    r"(^|[^a-z])(delete|remove|destroy|drop|purge|truncate|unpublish|discard|archive|trash)",
    re.I,
)
_ABSENT_RE = re.compile(r"not found|does not exist|no such|\b404\b", re.I)
_MAX_ID = 256
_MAX_LIST_PAGES = 50

# role -> (placeholders allowed, placeholders required)
_ROLE_VARS = {
    "list": ({"source_locale", "page", "page_size"}, set()),
    "get": ({"id", "locale"}, {"id", "locale"}),
    "write": ({"id", "locale", "fields"}, {"id", "locale", "fields"}),
    "publish": ({"id", "locale"}, {"id"}),
}
_ROLES = ("list", "get", "write", "publish")

# Presets fill any mapping key the operator left blank. `{ct}` is the connection's
# `content_type` (Strapi's MCP names one tool set per content type: list_page, get_page...).
PRESETS: dict = {
    "strapi": {
        "requires": ("content_type",),
        "list_tool": "list_{ct}",
        "list_args": {
            "locale": "{{source_locale}}",
            "status": "draft",
            "page": "{{page}}",
            "pageSize": "{{page_size}}",
        },
        "list_items_path": "results",
        "get_tool": "get_{ct}",
        "get_args": {"documentId": "{{id}}", "locale": "{{locale}}", "status": "draft"},
        "get_item_path": "data",
        "write_tool": "update_{ct}",
        "write_args": {"documentId": "{{id}}", "locale": "{{locale}}", "data": "{{fields}}"},
        "publish_tool": "publish_{ct}",
        "publish_args": {"documentId": "{{id}}", "locale": "{{locale}}"},
        "id_field": "documentId",
        "locale_field": "locale",
    },
    # The Northwind demo WordPress MCP (MCP Adapter + four abilities) can LIST the campaign
    # and PUBLISH a post, but has no tool that reads or writes a post in a language, so it
    # cannot be a full connector as shipped (spec section 7). The preset maps what exists;
    # translate refuses with a clear "no get/write tool" message.
    "wordpress-northwind": {
        "requires": (),
        "list_tool": "northwind-list-campaign-posts",
        "list_args": {"campaign": "Fall 2026"},
        "list_items_path": "posts",
        "publish_tool": "northwind-publish-post",
        "publish_args": {"post_id": "{{id}}"},
        "id_field": "id",
        "locale_field": "language",
    },
}

# OAuth-only vendor MCP servers (RESEARCH-G1). EVERY entry below is UNVERIFIED until a live run:
# tool names come from vendor docs read on 2026-10-10 and the argument names are best guesses,
# so `test()` says so and checks each mapped tool against the server's real tools/list. Any tool
# field the operator fills overrides these. `mcp_url` is used only when the connection leaves the
# URL blank. Tool NAMES come from vendor docs; the ARGUMENT names are guesses (no vendor page
# documents them), so a preset's own arguments are REFUSED at run time (`_need`) until the
# operator supplies get_args / write_args from the server's tools/list: a guessed `language`
# argument that a server ignores would read or write the SOURCE language item.
# Entries without tools are endpoint-only: the vendor has no per-locale read/write
# tool pair we can map, so translate refuses with the usual "maps no get tool" message.
_UNVERIFIED = "UNVERIFIED against the live server"
PRESETS.update(
    {
        "aem-fragments": {
            "requires": (),
            "unverified": True,
            "mcp_url": "https://mcp.adobeaemcloud.com/adobe/mcp/content",
            "notes": "UNVERIFIED. "
            "Adobe AEM as a Cloud Service, Content Fragments. Adobe ID OAuth. Adobe's "
            "docs name get_fragment (fields plus an ETag) and patch_fragment (a JSON Patch "
            "document guarded by that ETag), with NO argument names and no language-copy tool. "
            "patch_fragment does not take a plain fields object, so no write tool is preset: "
            "map one with write_args from tools/list. Language copies need to exist.",
            "get_tool": "get_fragment",
            "get_args": {"path": "{{id}}", "language": "{{locale}}"},
            "get_item_path": "",
            "id_field": "path",
            "locale_field": "language",
        },
        "sitecore-marketer": {
            "requires": (),
            "unverified": True,
            "mcp_url": "https://marketer.sitecorecloud.io/mcp/marketer-mcp-prod",
            "notes": "UNVERIFIED. "
            "Sitecore Marketer MCP (XM Cloud / SitecoreAI). OAuth. A missing language "
            "version needs add_language_to_page first, which no role here can call: create the "
            "language versions in Sitecore, then run. No publish tool. 50 requests per 10 s.",
            "list_tool": "get_all_pages_by_site",
            "list_args": {},
            "list_items_path": "pages",
            "get_tool": "get_page",
            "get_args": {"pageId": "{{id}}", "language": "{{locale}}"},
            "get_item_path": "",
            "write_tool": "update_content",
            "write_args": {"pageId": "{{id}}", "language": "{{locale}}", "fields": "{{fields}}"},
            "id_field": "id",
            "locale_field": "language",
        },
        "document360": {
            "requires": (),
            "unverified": True,
            "mcp_url": "",
            "notes": "UNVERIFIED. "
            "Document360 MCP. OAuth. The language parameter of get-article is not "
            "documented; confirm it with Test (tools/list shows the schema).",
            "list_tool": "document360-mcp-list-documents",
            "list_args": {},
            "list_items_path": "documents",
            "get_tool": "document360-mcp-get-article",
            "get_args": {"articleId": "{{id}}", "langCode": "{{locale}}"},
            "get_item_path": "",
            "write_tool": "document360-mcp-update-article",
            "write_args": {"articleId": "{{id}}", "langCode": "{{locale}}", "data": "{{fields}}"},
            "publish_tool": "document360-mcp-publish-article",
            "publish_args": {"articleId": "{{id}}", "langCode": "{{locale}}"},
            "id_field": "id",
            "locale_field": "langCode",
        },
        "notion": {
            "requires": (),
            "unverified": True,
            "mcp_url": "https://mcp.notion.com/mcp",
            "notes": "UNVERIFIED. "
            "Notion hosted MCP. User OAuth only. Notion has no per-locale variant of "
            "a page (a page per locale), so no tool pair maps to the four roles. Endpoint "
            "and consent only: use the Notion REST connector for translation.",
        },
        "google-workspace-docs": {
            "requires": (),
            "unverified": True,
            "mcp_url": "https://docsmcp.googleapis.com/mcp/v1",
            "notes": "UNVERIFIED. "
            "Google Workspace Docs MCP (developer preview): read_doc and "
            "update_doc. Google offers NO dynamic client registration for it: before Connect "
            "with OAuth, create an OAuth client in the Google Cloud console with this install's "
            "callback URL, then enter its client id (OAuth client id) and secret (OAuth client "
            "secret) on the connection. A document per locale (copy_file on the Drive server), "
            "so no per-locale read/write pair maps. Endpoint and consent only.",
        },
        "microsoft-work-iq": {
            "requires": (),
            "unverified": True,
            "mcp_url": "",
            "notes": "UNVERIFIED. "
            "Microsoft Work IQ (SharePoint, OneDrive, Word). Entra OAuth plus a "
            "Microsoft 365 Copilot licence. File per locale, and Word cannot edit the body: "
            "no per-locale read/write pair maps. Endpoint and consent only.",
        },
    }
)


def effective_mcp_url(cfg: dict | None) -> str:
    """The MCP URL a connection talks to: its own `mcp_url`, else its preset's. The connector
    (`_url`) and OAuth discovery (oauth._begin_mcp) both read it here, so the host that gets
    the token is always the host the grant was obtained for."""
    cfg = cfg or {}
    raw = str(cfg.get("mcp_url") or "").strip()
    if not raw:
        preset = PRESETS.get(str(cfg.get("preset") or "").strip().lower(), {})
        raw = str(preset.get("mcp_url") or "").strip()
    return raw


_MAPPING_KEYS = (
    "preset",
    "content_type",
    "list_tool",
    "list_args",
    "list_items_path",
    "get_tool",
    "get_args",
    "get_item_path",
    "write_tool",
    "write_args",
    "publish_tool",
    "publish_args",
    "id_field",
    "locale_field",
)


# OAuth settings decide WHICH client and consent the stored grant belongs to: changing them
# clears it, like a tool remap does.
_OAUTH_PIN_KEYS = ("oauth_client_id", "oauth_scope")


def mapping_fingerprint(cfg: dict | None) -> str:
    """A stable digest of everything that decides WHICH tools the stored token drives and
    with WHAT arguments. store_part2's credential pin clears the token when it changes."""
    cfg = cfg or {}
    raw = {k: str(cfg.get(k) or "").strip() for k in (*_MAPPING_KEYS, *_OAUTH_PIN_KEYS)}
    return _content_hash(json.dumps(raw, sort_keys=True))


def _bool(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _dig(obj, path: str):
    """Follow a dot path ('results', 'data.items') through dicts; '' = the object itself."""
    cur = obj
    for part in [p for p in str(path or "").split(".") if p]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _placeholders(obj) -> set:
    found: set = set()
    if isinstance(obj, dict):
        for v in obj.values():
            found |= _placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _placeholders(v)
    elif isinstance(obj, str):
        found |= set(_PLACEHOLDER_RE.findall(obj))
    return found


def render(template, values: dict):
    """Fill a JSON args template. A string that is EXACTLY one placeholder becomes the value
    itself (any JSON type: `{{fields}}` -> the field object, `{{page}}` -> an int); a
    placeholder inside a longer string is replaced by its string form. Values are our own
    data (item id, locale, translations); they are never parsed as templates themselves."""
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    if isinstance(template, str):
        m = _PLACEHOLDER_RE.fullmatch(template.strip())
        if m:
            return values[m.group(1)]
        return _PLACEHOLDER_RE.sub(lambda mm: str(values[mm.group(1)]), template)
    return template


def _fields_path(template, path=()) -> tuple | None:
    """The key path at which a template holds exactly `{{fields}}`."""
    if isinstance(template, dict):
        for k, v in template.items():
            if (
                isinstance(v, str)
                and _PLACEHOLDER_RE.fullmatch(v.strip())
                and (_PLACEHOLDER_RE.fullmatch(v.strip()).group(1) == "fields")
            ):
                return (*path, k)
            hit = _fields_path(v, (*path, k))
            if hit:
                return hit
    return None


def _check_id(raw) -> str:
    """An item id is opaque data. It only ever travels inside a JSON argument (never a URL),
    but it is still bounded and refused when it carries control characters."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ConnectorError(f"Item id {str(raw)[:40]!r} is not a string or number.")
    s = str(raw).strip()
    if not s or len(s) > _MAX_ID or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise ConnectorError(f"Unsafe item id {s[:40]!r}.")
    return s


SPEC = ConnectorSpec(
    kind="mcp_cms",
    name="CMS via MCP",
    icon="🔌",
    cat="Headless CMS",
    blurb="Translate content in any CMS that runs its own MCP server. You map four of the "
    "server's tools (list items, get an item in a locale, write an item in a locale, optional "
    "publish) and each translation is written back as that item's per-locale variant.",
    config_fields=[
        FieldSpec(
            "mcp_url",
            "MCP server URL",
            "The CMS's streamable HTTP MCP endpoint, e.g. https://cms.acme.com/mcp. The same "
            "SSRF guard as every connector applies: private addresses need "
            "egress_private_allowlist, loopback is always refused.",
            placeholder="https://cms.acme.com/mcp",
        ),
        FieldSpec(
            "preset",
            "Preset",
            "'strapi' fills the Strapi 5 MCP tool mapping (needs Content type), "
            "'wordpress-northwind' maps the demo WordPress server (list and publish only). "
            "OAuth vendor presets, all UNVERIFIED against the live servers (tool names from "
            "vendor docs; set get_args and write_args yourself from Test before a run): "
            "'aem-fragments' (UNVERIFIED), 'sitecore-marketer' (UNVERIFIED), 'document360' "
            "(UNVERIFIED), and endpoint plus consent only: 'notion' (UNVERIFIED), "
            "'google-workspace-docs' (UNVERIFIED; no dynamic registration, so enter an OAuth "
            "client id and secret from Google Cloud first), 'microsoft-work-iq' (UNVERIFIED). "
            "Blank = map every tool yourself. Any tool field you fill overrides the preset.",
            required=False,
            placeholder="strapi",
        ),
        FieldSpec(
            "content_type",
            "Content type",
            "For the Strapi preset: the singular API id whose tools you map (page -> "
            "list_page, get_page, update_page, publish_page).",
            required=False,
            placeholder="page",
        ),
        FieldSpec("list_tool", "List tool", "Tool that lists source items.", required=False),
        FieldSpec(
            "list_args",
            "List arguments",
            "JSON object. Placeholders: {{source_locale}}, {{page}}, {{page_size}}.",
            required=False,
            placeholder='{"locale": "{{source_locale}}"}',
        ),
        FieldSpec(
            "list_items_path",
            "List items path",
            "Dot path to the item array in the list result (e.g. results).",
            required=False,
        ),
        FieldSpec(
            "get_tool",
            "Get tool",
            "Tool that reads ONE item in ONE locale (also the existence check).",
            required=False,
        ),
        FieldSpec(
            "get_args",
            "Get arguments",
            "JSON object. Must use {{id}} and {{locale}}.",
            required=False,
            placeholder='{"id": "{{id}}", "locale": "{{locale}}"}',
        ),
        FieldSpec(
            "get_item_path",
            "Get item path",
            "Dot path to the item in the get result (e.g. data).",
            required=False,
        ),
        FieldSpec(
            "write_tool",
            "Write tool",
            "Tool that creates or updates ONE item in ONE locale.",
            required=False,
        ),
        FieldSpec(
            "write_args",
            "Write arguments",
            "JSON object. Must use {{id}}, {{locale}} and {{fields}} (the translated "
            "fields object).",
            required=False,
            placeholder='{"id": "{{id}}", "locale": "{{locale}}", "data": "{{fields}}"}',
        ),
        FieldSpec("publish_tool", "Publish tool", "Optional publish tool.", required=False),
        FieldSpec(
            "publish_args",
            "Publish arguments",
            "JSON object. Must use {{id}}.",
            required=False,
        ),
        FieldSpec(
            "publish_after_write",
            "Publish after write",
            "1 = call the publish tool after a COMPLETE write (never for a pair with a field "
            "held for review). Default 0: translations stay drafts.",
            required=False,
            placeholder="0",
        ),
        FieldSpec("id_field", "Id field", "Item key holding its id.", required=False),
        FieldSpec("locale_field", "Locale field", "Item key holding its locale.", required=False),
        FieldSpec(
            "locales",
            "Enabled locales",
            "Only when the get tool's schema does not list locales: comma-separated codes "
            "the CMS accepts.",
            required=False,
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "REQUIRED comma-separated allowlist of text fields to translate. MCP tool "
            "schemas do not say which fields are localized, so nothing is guessed.",
            placeholder="title, tagline, body",
        ),
        FieldSpec("title_field", "Title field", "Display title field.", required=False),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Locale to translate FROM (default: the get tool's locale default, else en).",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locale codes to write.",
            required=False,
            placeholder="fr, es, ja",
        ),
        FieldSpec("max_entries", "Max items", "Safety cap per run.", required=False),
        FieldSpec(
            "mcp_protocol",
            "MCP protocol",
            "auto (default): ask the server with server/discover and use the stateless "
            "2026-07-28 protocol when it speaks it, else the initialize handshake (2025-11-25 "
            "and older). 'legacy' always uses the handshake; '2026-07-28' never does.",
            required=False,
            placeholder="auto",
        ),
        FieldSpec(
            "call_timeout_s",
            "Call timeout (seconds)",
            "I/O time allowed for each MCP call, 5 to 300 (default 30). Raise it for a CMS "
            "whose write tool is slow: a write that times out is never resent.",
            required=False,
            placeholder="30",
        ),
        FieldSpec(
            "oauth_client_id",
            "OAuth client id",
            "Only for servers that need OAuth (use Connect with OAuth on the connection page). "
            "Blank = register this app automatically when the server supports it; otherwise "
            "paste the client id from the vendor's developer console.",
            required=False,
        ),
        FieldSpec(
            "oauth_scope",
            "OAuth scope",
            "Optional space-separated scopes to ask for. Blank = what the server advertises.",
            required=False,
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "oauth_client_secret",
            "OAuth client secret",
            "Only with an OAuth client id from the vendor's console. Encrypted; moved into the "
            "OAuth grant when you connect. Leave blank for a registered public client.",
            required=False,
            secret=True,
        ),
        FieldSpec(
            "bearer_token",
            "Bearer token",
            "Sent as Authorization: Bearer to the MCP URL only.",
            required=False,
            secret=True,
            placeholder="a long token",
        ),
        FieldSpec(
            "basic_user",
            "Basic auth user",
            "For servers that take HTTP Basic (e.g. a WordPress application password).",
            required=False,
            secret=True,
        ),
        FieldSpec(
            "basic_password",
            "Basic auth password",
            "Application password for the user above.",
            required=False,
            secret=True,
        ),
    ],
    setup_steps=[
        "Turn on the CMS's MCP server and create a token for it with read and write access "
        "to the content you will translate.",
        "Enter the MCP server URL and the token. A private address must be on "
        "egress_private_allowlist first.",
        "Pick a preset (Strapi: also the content type) or map the list, get and write tools "
        "and their JSON argument templates yourself.",
        "Set the translatable fields allowlist and the target locales, then use Test.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Unchanged items are "
    "suppressed by content hash. Only the mapped tools are ever called; writes stay drafts "
    "unless Publish after write is 1.",
    docs_url="https://modelcontextprotocol.io/specification/2026-07-28",
    supports_oauth=True,
)


class McpCmsConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="mcp_cms",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY, AuthKind.OAUTH3LO),
        api_version=API_VERSION,
        rich_text=RichText.NONE,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="updatedAt", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        self.sel = SelectionTracker(noun="field", label="mcp_cms", loud_markers=("schema",))
        self._session: McpSession | None = None
        self._map: dict | None = None
        self._existing: dict = {}  # (aid, locale) -> the variant the probe last read
        self.published = 0
        self.publish_errors: list = []

    # ------------------------------------------------------------- mapping
    def mapping(self) -> dict:
        """The effective, validated mapping: preset defaults + operator overrides. Raises
        ConnectorError naming the first problem. Validated before any network call."""
        if self._map is not None:
            return self._map
        cfg = self.config
        preset_name = str(cfg.get("preset") or "").strip().lower()
        if preset_name and preset_name not in PRESETS:
            raise ConnectorError(
                f"Unknown preset {preset_name!r}; known: {', '.join(sorted(PRESETS))}."
            )
        preset = PRESETS.get(preset_name, {})
        ct = str(cfg.get("content_type") or "").strip()
        for req in preset.get("requires", ()):
            if not str(cfg.get(req) or "").strip():
                raise ConnectorError(f"The {preset_name} preset needs `{req}` (e.g. page).")
        if ct and not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", ct):
            raise ConnectorError(f"Invalid content type {ct!r}.")
        m: dict = {}
        guessed: set = set()
        for role in _ROLES:
            tool = str(cfg.get(f"{role}_tool") or "").strip() or str(
                preset.get(f"{role}_tool") or ""
            ).replace("{ct}", ct)
            raw_args = cfg.get(f"{role}_args")
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    args = json.loads(raw_args)
                except ValueError as e:
                    raise ConnectorError(f"{role}_args is not valid JSON: {e}") from e
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = preset.get(f"{role}_args")
            if not tool:
                m[role] = None
                continue
            if not _TOOL_RE.fullmatch(tool):
                raise ConnectorError(f"Invalid {role} tool name {tool[:60]!r}.")
            if _DESTRUCTIVE_RE.search(tool):
                raise ConnectorError(
                    f"Refusing to map {tool!r} as the {role} tool: its name reads as "
                    f"destructive. Only list, get, write and publish tools may be mapped."
                )
            if args is None:
                args = {}
            if preset.get("unverified") and not (
                (isinstance(raw_args, str) and raw_args.strip()) or isinstance(raw_args, dict)
            ):
                guessed.add(role)
            if not isinstance(args, dict):
                raise ConnectorError(f"{role}_args must be a JSON object.")
            allowed, required = _ROLE_VARS[role]
            used = _placeholders(args)
            bad = used - allowed
            if bad:
                raise ConnectorError(
                    f"{role}_args uses unknown placeholder(s) {sorted(bad)}; allowed: "
                    f"{sorted(allowed)}."
                )
            missing = required - used
            if missing:
                raise ConnectorError(f"{role}_args must use {sorted(missing)}.")
            m[role] = {"tool": tool, "args": args}
        tools = [m[r]["tool"] for r in _ROLES if m.get(r)]
        if len(set(tools)) != len(tools):
            raise ConnectorError("Each role needs its own tool (one tool mapped twice).")
        m["list_items_path"] = str(
            cfg.get("list_items_path") or preset.get("list_items_path") or ""
        ).strip()
        m["get_item_path"] = str(cfg.get("get_item_path") or preset.get("get_item_path") or "")
        m["id_field"] = str(cfg.get("id_field") or preset.get("id_field") or "id").strip()
        m["locale_field"] = str(
            cfg.get("locale_field") or preset.get("locale_field") or "locale"
        ).strip()
        m["preset"] = preset_name
        m["unverified"] = bool(preset.get("unverified"))
        m["guessed"] = guessed
        self._map = m
        return m

    def _need(self, role: str) -> dict:
        r = self.mapping().get(role)
        if not r:
            what = {"get": "read an item in a locale", "write": "write an item in a locale"}
            raise ConnectorError(
                f"This connection maps no {role} tool, so it cannot "
                f"{what.get(role, role + ' items')}. The server may not offer one."
            )
        if role in (self.mapping().get("guessed") or ()):
            raise ConnectorError(
                f"The {self.mapping().get('preset')} preset's {role} arguments are unverified "
                f"guesses. Open Test, read the {role} tool's schema, and set {role}_args "
                "yourself before running."
            )
        return r

    # ------------------------------------------------------------- session
    def _auth_headers(self) -> dict:
        if (self.secret or {}).get("auth_mode") == "oauth":
            # MCP authorization grant: the access token is kept fresh upstream by
            # store.build_fresh -> ensure_fresh_token (the single-flight refresh).
            grant = (self.secret.get("oauth")) or {}
            at = str(grant.get("access_token") or "").strip()
            if not at or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in at):
                raise ConnectorError("The OAuth grant has no usable access token; reconnect.")
            # Audience check: the token only ever goes to the resource it was issued for. The
            # credential pin already clears the grant when mcp_url or the preset moves; this
            # refuses a grant whose recorded resource no longer matches, whatever the cause.
            res = str(((grant.get("mcp") or {}).get("resource")) or "")
            if res:
                from app.services.connectors.mcp_oauth import _resource_matches

                if not _resource_matches(res, self._url()):
                    raise ConnectorError(
                        "The OAuth grant was issued for a different MCP server than this "
                        "connection's URL; reconnect with OAuth."
                    )
            return {"Authorization": f"Bearer {at}"}
        tok = str(self.secret.get("bearer_token") or "").strip()
        user = str(self.secret.get("basic_user") or "").strip()
        pw = str(self.secret.get("basic_password") or "").strip()
        if tok and (user or pw):
            raise ConnectorError("Set either a bearer token or basic auth, not both.")
        if tok:
            if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in tok):
                raise ConnectorError("The bearer token has characters a header cannot carry.")
            return {"Authorization": f"Bearer {tok}"}
        if user and pw:
            raw = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
            return {"Authorization": f"Basic {raw}"}
        raise ConnectorError(
            "An MCP bearer token (or basic auth user + password, or Connect with OAuth) "
            "is required."
        )

    def _url(self) -> str:
        raw = effective_mcp_url(self.config)
        if not raw:
            raise ConnectorError("MCP server URL is required.")
        return _validate_target(raw)

    def session(self) -> McpSession:
        if self._session is None:
            self.mapping()  # refuse a bad mapping before any network call
            self._session = McpSession(
                self,
                self._url(),
                self._auth_headers(),
                mode=normalize_mode(self.config.get("mcp_protocol")),
                call_timeout_s=normalize_timeout(self.config.get("call_timeout_s")),
            )
            self._session.connect()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _tool_schema(self, role: str) -> dict:
        r = self.mapping().get(role)
        if not r:
            return {}
        t = self.session().list_tools().get(r["tool"])
        sch = (t or {}).get("inputSchema")
        return sch if isinstance(sch, dict) else {}

    def verify_tools(self) -> list[str]:
        """Check every mapped tool against the server's tools/list: present, and not
        annotated destructive. Returns the problems (empty = fine)."""
        problems = []
        listed = self.session().list_tools()
        for role in _ROLES:
            r = self.mapping().get(role)
            if not r:
                continue
            t = listed.get(r["tool"])
            if t is None:
                problems.append(f"{role} tool {r['tool']!r} is not offered by this server")
                continue
            ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
            if ann.get("destructiveHint") is True:
                problems.append(f"{role} tool {r['tool']!r} is marked destructive by the server")
            if role in ("list", "get") and ann.get("readOnlyHint") is False:
                problems.append(f"{role} tool {r['tool']!r} is marked as writing by the server")
        return problems

    def _call(self, role: str, values: dict, *, idempotent: bool = True):
        r = self._need(role)
        problems = self.verify_tools()
        if problems:
            raise ConnectorError("Tool mapping refused: " + "; ".join(problems) + ".")
        return self.session().call_tool(r["tool"], render(r["args"], values), idempotent=idempotent)

    # ------------------------------------------------------------- locales
    def _locale_arg_schema(self) -> dict:
        r = self.mapping().get("get") or self.mapping().get("list")
        if not r:
            return {}
        props = (
            self._tool_schema("get" if self.mapping().get("get") else "list").get("properties")
            or {}
        )
        for k, v in (r["args"] or {}).items():
            hit = _PLACEHOLDER_RE.fullmatch(v.strip()) if isinstance(v, str) else None
            if hit and hit.group(1) in ("locale", "source_locale"):
                sch = props.get(k)
                return sch if isinstance(sch, dict) else {}
        return {}

    def enabled_locales(self) -> list[str] | None:
        """Locales the CMS accepts: the enum on the get tool's locale argument, else the
        `locales` config, else None (cannot check; the run message says so)."""
        enum = self._locale_arg_schema().get("enum")
        if isinstance(enum, list) and enum:
            return [str(x).strip().lower() for x in enum if str(x).strip()]
        cfgd = _parse_names(self.config.get("locales"))
        return [x.lower() for x in cfgd] or None

    def _source_locale(self) -> str:
        cfg = str(self.config.get("source_locale") or "").strip()
        if cfg:
            return cfg
        try:
            d = self._locale_arg_schema().get("default")
        except ConnectorError:
            d = None
        return str(d).strip() if isinstance(d, str) and d.strip() else "en"

    # ------------------------------------------------------------- verbs
    def test(self) -> tuple[bool, str]:
        try:
            m = self.mapping()
            s = self.session()
            tools = s.list_tools()
            problems = self.verify_tools()
            if not _parse_names(self.config.get("fields")):
                problems.append("set the `fields` allowlist (required)")
            mapped = ", ".join(f"{r}={m[r]['tool']}" for r in _ROLES if m.get(r))
            missing = [r for r in ("list", "get", "write") if not m.get(r)]
            if missing:
                problems.append(f"no {'/'.join(missing)} tool mapped: translate cannot run")
            locs = self.enabled_locales()
            name = str((s.server_info or {}).get("name") or "MCP server")[:60]
            msg = (
                f"Connected to {name} (MCP {s.protocol}), {len(tools)} tool(s). Mapped: "
                f"{mapped or '(none)'}. Locales: "
                f"{', '.join(locs) if locs else 'not declared by the server'}."
            )
            if m.get("unverified"):
                msg += (
                    f" Preset {m['preset']} is {_UNVERIFIED}: check the mapped tools and "
                    "argument names above. " + str(PRESETS.get(m["preset"], {}).get("notes") or "")
                ).rstrip()
            if problems:
                return False, msg + " Problems: " + "; ".join(problems) + "."
            return True, msg
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:200]}"

    def containers(self) -> list[dict]:
        try:
            m = self.mapping()
        except ConnectorError:
            return []
        lst = m.get("list")
        return (
            [{"id": lst["tool"], "name": f"{lst['tool']} (MCP list tool)", "tables": []}]
            if lst
            else []
        )

    # ----------------------------------------------------------- discovery
    def _flatten(self, item: dict) -> dict:
        m = self.mapping()
        return {
            "id": _check_id(item.get(m["id_field"])),
            "locale": item.get(m["locale_field"]),
            "updatedAt": item.get("updatedAt"),
            "fields": item,
        }

    def _list_page(self, page: int, size: int) -> list:
        data = self._call(
            "list",
            {"source_locale": self._source_locale(), "page": page, "page_size": size},
        )
        items = _dig(data, self.mapping()["list_items_path"])
        if not isinstance(items, list):
            raise ConnectorError(
                f"The list tool's result has no item array at "
                f"{self.mapping()['list_items_path'] or '(root)'!r}."
            )
        return [x for x in items if isinstance(x, dict)]

    def _list_all(self, cap: int) -> list[dict]:
        paged = "page" in _placeholders(self.mapping()["list"]["args"])
        size = max(1, min(100, cap))
        out: list[dict] = []
        page = 1
        while len(out) < cap and page <= _MAX_LIST_PAGES:
            rows = self._list_page(page, size)
            out.extend(self._keep_rows(rows))
            if not paged or len(rows) < size or not rows:
                break
            page += 1
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source-locale items. Caller ids are SCOPE-CHECKED: only ids the mapped list tool
        returns are accepted (fail closed), so an id from elsewhere in the CMS is refused."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        self._need("list")
        rows = [
            self._flatten(x)
            for x in self._list_all(cap if not article_ids else fetch_cap(None, 1000))
        ]
        src = self._source_locale().lower()
        rows = [r for r in rows if not r["locale"] or str(r["locale"]).lower() == src]
        if article_ids:
            wanted = [_check_id(a) for a in article_ids]
            by_id = {r["id"]: r for r in rows}
            unknown = [w for w in wanted if w not in by_id]
            if unknown:
                raise ConnectorError(
                    f"Item id(s) {', '.join(unknown[:5])} are not returned by this connection's "
                    f"list tool, refusing to target them."
                )
            rows = [by_id[w] for w in wanted][:cap]
        out = []
        need = self._allowlist()
        for r in rows:
            if need and not all(n in r["fields"] for n in need) and self.mapping().get("get"):
                full = self._get_item(r["id"], self._source_locale())
                if full is not ABSENT:
                    r = self._charge_record(self._flatten(full))
            out.append(r)
        return out

    # -------------------------------------------------------------- fields
    def _allowlist(self) -> list[str]:
        return _parse_names(self.config.get("fields"))

    def _write_field_schema(self) -> tuple[dict, set]:
        """(properties, required) of the object the write tool receives as {{fields}}, from
        its input schema; ({}, set()) when the schema does not describe it."""
        r = self.mapping().get("write")
        path = _fields_path(r["args"]) if r else None
        if not path:
            return {}, set()
        sch = self._tool_schema("write")
        for key in path:
            props = sch.get("properties") if isinstance(sch, dict) else None
            sch = props.get(key) if isinstance(props, dict) else None
            if not isinstance(sch, dict):
                return {}, set()
        props = sch.get("properties") if isinstance(sch.get("properties"), dict) else {}
        req = set(sch.get("required") or []) if isinstance(sch.get("required"), list) else set()
        return props, req

    def _field_selection(self, fields: dict) -> tuple[list[str], dict]:
        allow = self._allowlist()
        if not allow:
            raise ConnectorError(
                "Set the connection's `fields` allowlist: an MCP server's tool schema does not "
                "say which fields are localized, so nothing is translated by guesswork."
            )
        props, _req = self._write_field_schema()
        selected: list[str] = []
        skipped: dict = {}
        for n in allow:
            p = props.get(n) if props else None
            if n in _SYSTEM_FIELDS:
                skipped[n] = "system/reserved field"
            elif props and p is None:
                skipped[n] = "not a field of the write tool's schema"
            elif isinstance(p, dict) and p.get("type") not in (None, "string"):
                skipped[n] = f"schema type {p.get('type')!r}, not text"
            elif isinstance(p, dict) and (
                p.get("x-localized") is False or p.get("localized") is False
            ):
                skipped[n] = "not localized (per the write tool's schema)"
            elif isinstance(fields.get(n), str) and fields.get(n).strip():
                selected.append(n)
            else:
                skipped[n] = "listed in `fields` but not a non-empty text value on this item"
        return selected, skipped

    def _title_field(self, names: list[str]) -> str | None:
        cfg = str(self.config.get("title_field") or "").strip()
        if cfg and cfg in names:
            return cfg
        for cand in ("title", "name"):
            if cand in names:
                return cand
        return None

    def extract(self, entry: dict):
        fields = entry.get("fields") or {}
        names, skipped = self._field_selection(fields)
        self.sel.record_selection(names, skipped)
        title_field = self._title_field(names)
        title_text = str(fields.get(title_field) or "") if title_field else ""
        body_names = [n for n in names if n != title_field]
        body_units = [_Unit(str(fields.get(n) or ""), n) for n in body_names]
        chash = _content_hash(*([title_text] + [str(fields.get(n) or "") for n in body_names]))
        return title_field, title_text, body_units, chash, body_names

    # ------------------------------------------------------ T2 client side
    def _get_item(self, aid: str, locale: str):
        try:
            data = self._call("get", {"id": aid, "locale": locale})
        except McpToolError as e:
            if _ABSENT_RE.search(e.text):
                return ABSENT
            raise
        item = _dig(data, self.mapping()["get_item_path"])
        if not isinstance(item, dict) or not item:
            return ABSENT
        got = item.get(self.mapping()["locale_field"])
        if isinstance(got, str) and got.strip() and got.strip().lower() != str(locale).lower():
            return ABSENT  # the CMS fell back to another locale: our variant does not exist
        return item

    def get_translation(self, record_id: str, locale: str):
        aid = _check_id(record_id)
        item = self._get_item(aid, locale)
        if item is ABSENT:
            self._existing.pop((aid, locale), None)
            return ABSENT
        self._existing[(aid, locale)] = item
        return item

    def _payload(self, fields: dict) -> tuple[dict, bool]:
        data: dict = {}
        had_real = False
        for k, v in (fields or {}).items():
            if k == _DRAFT_SENTINEL and isinstance(v, bool):
                continue  # the strategy's draft flag, never a CMS field
            if k.startswith(_ALIAS_PREFIX):
                k = k[len(_ALIAS_PREFIX) :]
            data[k] = v
            had_real = True
        return data, had_real

    def _write(self, record_id: str, locale: str, fields: dict, *, created: bool) -> WriteResult:
        aid = _check_id(record_id)
        data, had_real = self._payload(fields)
        if not had_real:
            return WriteResult(False, created, message=_HELD_BACK)
        if not created:
            # A schema-REQUIRED field we are not sending this run (held for review) keeps
            # the variant's OWN stored value, so the write is accepted without the source.
            _props, req = self._write_field_schema()
            current = self._existing.get((aid, locale)) or {}
            for n in req:
                if n not in data and isinstance(current.get(n), str):
                    data[n] = current[n]
        try:
            self._call("write", {"id": aid, "locale": locale, "fields": data}, idempotent=False)
        except McpToolError as e:
            return WriteResult(False, created, message=f"write tool error: {e.text}")
        except ConnectorError as e:
            return WriteResult(False, created, message=str(e)[:200])
        self._existing.pop((aid, locale), None)
        return WriteResult(
            True, created, target_id=aid, message="created" if created else "updated"
        )

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        return self._write(record_id, locale, fields, created=True)

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        return self._write(record_id, locale, fields, created=False)

    def publish(self, record_id: str, locale: str) -> None:
        self._call("publish", {"id": _check_id(record_id), "locale": locale}, idempotent=False)

    # -------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for entry in self.discover_articles():
            title_field, title_text, body_units, _h, _bn = self.extract(entry)
            label = (title_text or entry["id"])[:60]
            if title_field:
                out.append(PulledItem(entry["id"], title_field, title_text, label))
            for u in body_units:
                out.append(PulledItem(entry["id"], u.p_index, u.text, label))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("CMS via MCP writes per-locale variants - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


class _Unit:
    """A flow translation unit: .text + .p_index (the FIELD NAME)."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


# ===========================================================================
# Translate flow: thin hooks over the shared engine (flow.run_translate_flow).
# ===========================================================================
def _preflight(ctx):
    c = ctx.connector
    c._need("list")
    c._need("get")
    c._need("write")
    enabled = c.enabled_locales()
    src = c._source_locale().lower()
    return (set(enabled) if enabled is not None else None), {
        "source": src,
        "declared": enabled is not None,
    }


def _partition(locales, enabled, extra):
    src = (extra or {}).get("source", "")
    if enabled is None:  # the server declares no locale list: we cannot reject anything
        return [], [l for l in locales if l != src]  # noqa: E741
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _prepare(ctx, art):
    title_field, title_text, body_units, chash, body_names = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = art["id"]
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": title_text or None},
        doc_title=f"{(title_text or 'item')[:50]} ({aid})",
        extra={"title_field": title_field, "body_names": body_names},
    )


def _is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _stash(out: dict, name: str, value) -> None:
    out[(_ALIAS_PREFIX + name) if name == _DRAFT_SENTINEL else name] = value


def _build_payload(prep, loc, seg0_target, mapping):
    """Only translated text. A held title or body field is OMITTED (never the source)."""
    out: dict = {}
    tf = prep.extra["title_field"]
    if tf is not None and seg0_target is not None:
        _stash(out, tf, seg0_target)
    for name in prep.extra["body_names"]:
        if name in mapping:
            _stash(out, name, mapping[name])
    return out


def _on_write_ok(ctx, prep, loc):
    c = ctx.connector
    if not _bool(ctx.cfg.get("publish_after_write")) or not c.mapping().get("publish"):
        return
    if any(p.get("article_id") == prep.aid and p.get("locale") == loc for p in ctx.partial_held):
        return  # never publish a pair with a field still held for review
    try:
        c.publish(prep.aid, loc)
        c.published += 1
    except Exception as e:
        c.publish_errors.append({"article_id": prep.aid, "locale": loc, "error": str(e)[:200]})


def _is_held(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _compute_ok(ctx):
    if not ctx.usable:
        return False
    real = [f for f in ctx.failures if not _is_held(f)]
    return bool((ctx.created + ctx.updated) > 0 or not real)


def _build_message(ctx):
    c = ctx.connector
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} item variant(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} item(s) through MCP."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} item(s) with no listed text field.")
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) the server does not accept: "
            f"{', '.join(ctx.rejected)}."
        )
    if not ctx.extra.get("declared"):
        parts.append("The server declares no locale list, so target locales were not checked.")
    held = [f for f in ctx.failures if _is_held(f)]
    if held:
        parts.append(
            f"Held back {len(held)} (item, locale) pair(s) awaiting review (NEEDS_REVIEW): a "
            f"new variant is not created while a field is held. They will be retried next run."
        )
    if c.published or c.publish_errors:
        parts.append(f"Published {c.published} variant(s).")
    if c.publish_errors:
        parts.append(
            "Publish errors: "
            + "; ".join(
                f"{p['article_id']}/{p['locale']}: {p['error']}" for p in c.publish_errors[:3]
            )
        )
    sel = c.sel.selection_summary()
    if sel:
        parts.append(sel)
    real = [f for f in ctx.failures if not _is_held(f)]
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("item write(s)").strip())
    return " ".join(parts)


def _build_result(ctx):
    c = ctx.connector
    c.close()  # end the MCP session (best effort); the last hook the engine calls
    return {
        "entries": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len([f for f in ctx.failures if _is_held(f)]),
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "rejected": ctx.rejected,
        "published": c.published,
        "publish_errors": c.publish_errors,
        "fields_selected": c.sel.selected_sorted(),
        "fields_skipped": c.sel.skipped_dict(),
        "failures": [f for f in ctx.failures if not _is_held(f)],
    }


def mcp_cms_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate CMS items through the CMS's own MCP server (T2 over mapped tools)."""
    spec = flow.TranslateFlowSpec(
        kind="mcp_cms",
        no_conn_msg="No such CMS via MCP connection.",
        hash_key="mcp_cms_hashes",
        probe_label="MCP",
        preflight=_preflight,
        prepare=_prepare,
        is_suppressed=_is_suppressed,
        build_payload=_build_payload,
        compute_draft=lambda cfg: not _bool(cfg.get("publish_after_write")),
        compute_ok=_compute_ok,
        build_message=_build_message,
        build_result=_build_result,
        extra_counters=("empty_skipped",),
        partition=_partition,
        on_write_ok=_on_write_ok,
        held_prefix=_HELD_BACK_PREFIX,
    )
    return flow.run_translate_flow(
        spec,
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
