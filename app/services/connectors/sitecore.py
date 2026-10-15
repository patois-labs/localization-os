"""Sitecore XM Cloud (SitecoreAI) connector - a T2 consumer of the connector framework.

TOPOLOGY - T2, re-derived from Sitecore's real model. A content item has ONE identity (its
item id) and every language holds its own numbered VERSIONS of the field values. The pair
(item, language) is therefore a per-locale sub-resource of one record, owned by the source
system: there is no locale map on one payload to merge (not T1) and no second document of
ours to track (not T3). Write-back rides the stock `strategy.SubResourceStrategy`: probe
"does (item, language) have a version >= 1" (an explicit signal, never a content read), then
create (`addItemVersion`, then `updateItem` on the new version) or update (`updateItem` on the
latest existing version) through the Authoring GraphQL API. addItemVersion takes no field
values, so a create is two calls: when the updateItem fails, the version just added is
deleted again (`deleteItemVersion`, confirmed by a re-probe), because an existing empty
language version would switch off Sitecore's language fallback for site visitors.

AUTH. An XM Cloud automation client (OAuth client credentials) is exchanged at the fixed host
auth.sitecorecloud.io for a short-lived bearer token, minted lazily per connector instance and
re-minted once on a 401. There is no refresh token and no stored token, so nothing here is a
second OAuth refresh path. Client id and secret are BOTH stored secrets and the CM host
(`base_url`) is the pinned credential destination (store_part2), so a config edit that moves
the host drops the credential. The CM host must be a Sitecore-hosted *.sitecorecloud.io
name (custom hostnames are an Experience Edge delivery feature, not a CM one), so neither the
token nor the client secret can be steered to another host.

SCHEMA (fail closed). addItemVersion, deleteItemVersion and the template-field `versioning`
enum are inferred from reference clients, not Sitecore's docs, so the live schema is
introspected once per connector instance before any discovery or write; a mismatch stops
the run with the list of differences and nothing is written.

SCOPE (taxonomy A). Everything is confined to one configured content root path under
/sitecore/content. Item ids are strict GUIDs sent only as GraphQL VARIABLES (the reference
connector builds queries by string concatenation, which this port deliberately does not
copy); an explicit id must also resolve to a path under the root, fail closed.

SHARED FIELDS (taxonomy B, the data-loss class). A Sitecore field flagged shared holds ONE
value for every language, so writing a "translation" into it would overwrite the SOURCE
language. Shared fields, and fields whose sharing the API did not state, are never selected,
not even when the operator allowlists them.

FIELD SELECTION (taxonomy D) is schema-driven: the item query returns each field's template
field type, so only Single-Line Text, Multi-Line Text and Rich Text are candidates; images,
links, checkboxes, droplists and the rest are skipped and REPORTED with a reason. Rich Text
goes through the shared html filter and is written all-or-nothing per field.

HELD BACK (taxonomy B/F, #640). A withheld (NEEDS_REVIEW) field is omitted from the update,
so the stored value survives. A new language version is not created while any unit is held,
and a pair written with a unit held records no content hash.

VERSION AND STATE (taxonomy C). Probe, update and creation all address the same state: the
master database, the latest version in the target language. Nothing is ever published; a
workflow or check-in rule on the item may still refuse a write and is reported per pair.

PROVENANCE: endpoint shapes (token exchange, authoring endpoint, item/search queries,
addItemVersion + updateItem) are reference-and-reimplemented from bb-io/SitecoreGraphQl
(copyright 2023 Blackbird, MIT, verified verbatim 2026-10-10). See
THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from app.services.connectors import fieldmap
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
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.sitecore_flow import (
    FIELD_PREFIX,
    HELD_BACK,
    canonical_guid,
    parse_names,
    sitecore_translate,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult
from app.services.connectors.tracking import SelectionTracker
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "v1"
GQL_PATH = "/sitecore/api/authoring/graphql/v1"
AUTH_URL = "https://auth.sitecorecloud.io/oauth/token"
AUTH_AUDIENCE = "https://api.sitecorecloud.io"
CONTENT_PREFIX = "/sitecore/content/"
_PAGE_SIZE = 50
# `Item.fields` is a paged connection (default page 50), so ask for a bigger page and compare
# totalCount: a short answer is reported, never silently treated as the whole item.
_FIELDS_PAGE = 100

__all__ = ["SPEC", "SitecoreConnector", "sitecore_translate"]

# template-field type (lower-cased typeKey, else type) -> fieldmap kind. Anything else is
# reported as skipped, never guessed at.
_TYPE_KIND = {
    "single-line text": "string",
    "multi-line text": "textarea",
    "rich text": "html",
}

_URL_RE = re.compile(r"^(?:https?://|mailto:|tel:)\S+$", re.I)
_GUID_VALUE_RE = re.compile(r"^\{?[0-9A-Fa-f]{8}(?:-?[0-9A-Fa-f]{4}){3}-?[0-9A-Fa-f]{12}\}?$")
_NUMERIC_RE = re.compile(r"^[\d\s.,:/+-]+$")

_ITEM_FIELDS = (
    "itemId name path version language { name } "
    f"fields(ownFields: false, excludeStandardFields: true, first: {_FIELDS_PAGE}) "
    "{ totalCount nodes { name value templateField { type versioning } } }"
)

Q_LANGUAGES = "query SitecoreLanguages { languages { nodes { name iso displayName } } }"
Q_ITEM_BY_PATH = (
    "query SitecoreItemByPath($path: String!) "
    '{ item(where: {database: "master", path: $path}) { itemId name path } }'
)
Q_ITEM_STATE = (
    "query SitecoreItemState($id: ID!, $lang: String!) "
    '{ item(where: {database: "master", itemId: $id, language: $lang}) '
    "{ itemId name path version language { name } } }"
)
Q_ITEM = (
    "query SitecoreItem($id: ID!, $lang: String!) "
    '{ item(where: {database: "master", itemId: $id, language: $lang}) '
    "{ " + _ITEM_FIELDS + " } }"
)
# The root id is a validated GUID (canonical_guid) and the only value inlined; the language,
# page index and page size travel as variables.
Q_SEARCH = (
    "query SitecoreSearch($lang: String!, $pageIndex: Int, $pageSize: Int) "
    "{ search(query: {language: $lang, latestVersionOnly: true, "
    "paging: {pageIndex: $pageIndex, pageSize: $pageSize}, "
    'filterStatement: {criteria: [{field: "_path", criteriaType: SEARCH, operator: MUST, '
    'value: "%s"}]}}) '
    "{ totalCount results { itemId path version innerItem { " + _ITEM_FIELDS + " } } } }"
)
M_ADD_VERSION = (
    "mutation SitecoreAddVersion($input: AddItemVersionInput!) "
    "{ addItemVersion(input: $input) { item { itemId version } } }"
)
M_UPDATE_ITEM = (
    "mutation SitecoreUpdateItem($input: UpdateItemInput!) "
    "{ updateItem(input: $input) { item { itemId version } } }"
)
# Cleanup after a failed first write (see create_translation). The payload shape differs
# between the public reference clients (`successful` vs `item`), so only `__typename` is
# selected and success is judged by re-probing the item, never by the payload.
M_DELETE_VERSION = (
    "mutation SitecoreDeleteVersion($input: DeleteItemVersionInput!) "
    "{ deleteItemVersion(input: $input) { __typename } }"
)

# addItemVersion, deleteItemVersion and the `versioning` enum are NOT in Sitecore's public
# docs; their shapes come from MIT reference clients and a third-party decompile. Before the
# first discovery or write, the live schema is introspected and anything that does not match
# what this connector sends or reads stops the run (fail closed, nothing written).
_INPUT_SHAPE = "{ inputFields { name defaultValue type { kind } } }"
Q_SCHEMA = (
    "query SitecoreSchemaCheck { "
    "__schema { mutationType { fields { name } } } "
    'tf: __type(name: "ItemTemplateField") { fields { name type { kind name '
    "enumValues { name } ofType { kind name enumValues { name } } } } } "
    f'addv: __type(name: "AddItemVersionInput") {_INPUT_SHAPE} '
    f'delv: __type(name: "DeleteItemVersionInput") {_INPUT_SHAPE} '
    f'upd: __type(name: "UpdateItemInput") {_INPUT_SHAPE} }}'
)
_SCHEMA_MUTATIONS = ("addItemVersion", "updateItem", "deleteItemVersion")
_SCHEMA_INPUTS = {
    "addv": ("AddItemVersionInput", {"itemId", "language"}),
    "delv": ("DeleteItemVersionInput", {"itemId", "language", "version"}),
    "upd": ("UpdateItemInput", {"itemId", "language", "version", "fields"}),
}
_VERSIONING_VALUES = {"SHARED", "UNVERSIONED", "VERSIONED"}

# XM Cloud CM (authoring) instances are Sitecore-hosted under sitecorecloud.io, and the
# token minted at auth.sitecorecloud.io (audience api.sitecorecloud.io) is only meant for
# them. Sitecore's custom-hostname feature covers Experience Edge delivery, not the CM, so
# no other host is accepted: the bearer token can never be sent anywhere else.
CM_HOST_SUFFIX = ".sitecorecloud.io"
_NON_CM_HOSTS = {
    "sitecorecloud.io",
    "auth.sitecorecloud.io",
    "api.sitecorecloud.io",
    "edge.sitecorecloud.io",
    "edge-platform.sitecorecloud.io",
}
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

SPEC = ConnectorSpec(
    kind="sitecore",
    name="Sitecore",
    icon="🧱",
    cat="DXP / CMS",
    blurb="Translate Sitecore XM Cloud (SitecoreAI) content items into any language. Each "
    "translation is written as a language version of the same item through the Authoring "
    "GraphQL API, so the source language version is never overwritten and nothing is "
    "published.",
    config_fields=[
        FieldSpec(
            "base_url",
            "CM host URL",
            "Your XM Cloud Content Management (authoring) host, e.g. "
            "https://xmc-acme-site-env.sitecorecloud.io. Must be an https host under "
            "sitecorecloud.io. The connector appends /sitecore/api/authoring/graphql/v1.",
            placeholder="https://xmc-acme-site-env.sitecorecloud.io",
        ),
        FieldSpec(
            "root_path",
            "Content root path",
            "Only items at or under this path are read or written. Must start with "
            "/sitecore/content/, e.g. /sitecore/content/Acme/Site/Home.",
            placeholder="/sitecore/content/Acme/Site/Home",
        ),
        FieldSpec(
            "source_locale",
            "Source language",
            "The Sitecore language to translate FROM (default en).",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target languages",
            "Comma-separated Sitecore language names that already exist under "
            "/sitecore/system/Languages.",
            required=False,
            placeholder="fr-FR, de-DE",
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "Optional allowlist of field names. Blank = every Single-Line Text, Multi-Line "
            "Text and Rich Text field that is not shared across languages. A shared field is "
            "never translated, even when listed here.",
            required=False,
            placeholder="Title, Text",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude fields",
            "Field names to never translate (comma-separated).",
            required=False,
            placeholder="Navigation Title",
        ),
        FieldSpec(
            "title_field",
            "Title field",
            "Field used as the display title (default Title, then Headline).",
            required=False,
            placeholder="Title",
        ),
        FieldSpec(
            "max_items",
            "Max items",
            "Safety cap on items scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "client_id",
            "Client ID",
            "From the XM Cloud Deploy app: Credentials -> Environment -> Create credentials "
            "-> Automation.",
            secret=True,
            placeholder="automation client id",
        ),
        FieldSpec(
            "client_secret",
            "Client secret",
            "Shown once when the automation credential is created.",
            secret=True,
            placeholder="automation client secret",
        ),
    ],
    setup_steps=[
        "In the XM Cloud Deploy app, open your project environment, go to Credentials and "
        "create an Automation credential. Copy the client ID and secret.",
        "Make sure each target language exists in Sitecore (Content Editor -> "
        "/sitecore/system/Languages) and is enabled for the site.",
        "Enter the CM host URL, the content root path to translate (under "
        "/sitecore/content/), the source and target languages, and the client ID and secret.",
        "Check that the fields you want translated are not marked Shared on their template: "
        "a shared field has one value for every language and is skipped.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Unchanged items are "
    "suppressed by content hash. Writes go to the master database and are never published; "
    "publishing is a separate Sitecore step. Fields shared across all languages are never "
    "written, and fields that are not text (images, links, checkboxes) are not copied to a "
    "new language version.",
    docs_url="https://doc.sitecore.com/xmc/en/developers/xm-cloud/"
    "sitecore-authoring-and-management-graphql-api.html",
    supports_oauth=False,
)


class SitecoreConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="sitecore",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.HTML,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="updatedDate", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = SubResourceStrategy(source_locale_of=lambda conn, rec: self.source_locale())
        self.sel = SelectionTracker(
            noun="field", label="sitecore", loud_markers=("sharing unknown",)
        )
        self._tok: str | None = None
        self._lang_cache: list[str] | None = None
        self._root_cache: tuple[str, str] | None = None
        self._schema_checked = False

    # ------------------------------------------------------------------ config
    def _base(self) -> str:
        raw = (self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "Sitecore CM host URL is required (e.g. https://xmc-acme-site-env.sitecorecloud.io)."
            )
        parts = urlsplit(raw)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path.strip("/")
        ):
            raise ConnectorError(
                "The Sitecore CM host URL must be a plain https origin "
                "(https://host), with no path, query or credentials."
            )
        host = parts.hostname.lower()
        try:
            port = parts.port
        except ValueError:
            port = -1
        labels = host[: -len(CM_HOST_SUFFIX)].split(".") if host.endswith(CM_HOST_SUFFIX) else []
        if (
            not labels
            or host in _NON_CM_HOSTS
            or port not in (None, 443)
            or not all(_HOST_LABEL_RE.match(lb) for lb in labels)
        ):
            raise ConnectorError(
                "The Sitecore CM host must be your XM Cloud authoring host under "
                "sitecorecloud.io (e.g. https://xmc-acme-site-env.sitecorecloud.io); other "
                "hosts are refused so the Sitecore token is never sent anywhere else."
            )
        return _validate_target(f"https://{host}").rstrip("/")

    def _endpoint(self) -> str:
        return self._base() + GQL_PATH

    def _root_path_cfg(self) -> str:
        raw = (self.config.get("root_path") or "").strip()
        if not raw:
            raise ConnectorError(
                "Content root path is required (e.g. /sitecore/content/Acme/Site/Home)."
            )
        path = raw.rstrip("/")
        segments = path.split("/")
        if (
            not path.lower().startswith(CONTENT_PREFIX)
            or len(path) <= len(CONTENT_PREFIX)
            or "" in segments[1:]
            or any(s in (".", "..") or ".." in s for s in segments)
            or any(ord(ch) < 32 or ch in "\\?#{}" for ch in path)
        ):
            raise ConnectorError(
                f"Invalid content root path {raw!r}: it must start with {CONTENT_PREFIX}, name "
                "an item below it, and contain no empty or '..' segments, backslash, '?', '#', "
                "braces or control characters."
            )
        return path

    # -------------------------------------------------------------------- auth
    def _mint_token(self) -> str:
        client_id = (self.secret.get("client_id") or "").strip()
        client_secret = (self.secret.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise ConnectorError("Sitecore needs an automation client ID and client secret.")
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                AUTH_URL,
                json={
                    "audience": AUTH_AUDIENCE,
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        if r.status_code != 200:
            # #165: the upstream body is not echoed into an operator-visible error.
            raise ConnectorError(
                f"Sitecore token endpoint returned {r.status_code}: check the automation "
                "client ID and secret."
            )
        try:
            token = str((r.json() or {}).get("access_token") or "").strip()
        except ValueError:
            token = ""
        if not token:
            raise ConnectorError("Sitecore token response had no access_token.")
        return token

    def _token(self) -> str:
        if self._tok is None:
            self._tok = self._mint_token()
        return self._tok

    def _gql(
        self,
        operation: str,
        query: str,
        variables: dict | None = None,
        *,
        idempotent: bool = True,
        _retry: bool = True,
    ) -> dict:
        """One Authoring GraphQL call. Values only ever travel as variables. A 401 re-mints
        the token once (there is no refresh token). Returns the `data` object."""
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                self._endpoint(),
                json={"query": query, "operationName": operation, "variables": variables or {}},
                headers=headers,
                idempotent=idempotent,
            )
        if r.status_code == 401 and _retry:
            self._tok = None
            return self._gql(operation, query, variables, idempotent=idempotent, _retry=False)
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"Sitecore refused {operation} ({r.status_code}): the automation client may lack "
                "access to the Authoring API or this environment."
            )
        if r.status_code != 200:
            raise ConnectorError(f"Sitecore {operation} failed ({r.status_code}).")
        try:
            body = r.json()
        except ValueError:
            raise ConnectorError(f"Sitecore {operation} returned a non-JSON response.") from None
        if not isinstance(body, dict):
            raise ConnectorError(f"Sitecore {operation} returned an unexpected response.")
        errors = body.get("errors")
        if errors:
            msgs = "; ".join(
                str((e or {}).get("message") or e)[:160] if isinstance(e, dict) else str(e)[:160]
                for e in errors[:3]
            )
            raise ConnectorError(f"Sitecore {operation} error: {vendor_excerpt(msgs)}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ConnectorError(f"Sitecore {operation} returned no data.")
        return data

    # ----------------------------------------------------------- schema (fail closed)
    @staticmethod
    def _schema_problems(data: dict) -> list[str]:
        """What in the live Authoring schema does not match this connector, [] when it all
        matches. Pure, so the rules are testable without a transport."""
        problems: list[str] = []
        mt = ((data.get("__schema") or {}).get("mutationType") or {}).get("fields") or []
        have = {str((f or {}).get("name") or "") for f in mt if isinstance(f, dict)}
        for name in _SCHEMA_MUTATIONS:
            if name not in have:
                problems.append(f"mutation {name} is missing")
        tf = data.get("tf")
        tf_fields = {
            str(f.get("name")): f
            for f in ((tf or {}).get("fields") or [])
            if isinstance(f, dict) and f.get("name")
        }
        if not tf:
            problems.append("type ItemTemplateField is missing")
        else:
            if "type" not in tf_fields:
                problems.append("ItemTemplateField.type is missing")
            ver = tf_fields.get("versioning")
            if ver is None:
                problems.append("ItemTemplateField.versioning is missing")
            else:
                t = ver.get("type") or {}
                while isinstance(t, dict) and t.get("kind") == "NON_NULL":
                    t = t.get("ofType") or {}
                values = {str((v or {}).get("name") or "") for v in (t.get("enumValues") or [])}
                if t.get("kind") != "ENUM" or not values >= _VERSIONING_VALUES:
                    problems.append(
                        "ItemTemplateField.versioning is not the SHARED / UNVERSIONED / "
                        "VERSIONED enum"
                    )
        for alias, (tname, needed) in _SCHEMA_INPUTS.items():
            node = data.get(alias)
            if not node:
                problems.append(f"input {tname} is missing")
                continue
            fields = [f for f in (node.get("inputFields") or []) if isinstance(f, dict)]
            names = {str(f.get("name") or "") for f in fields}
            for n in sorted(needed - names):
                problems.append(f"{tname}.{n} is missing")
            for f in fields:
                n = str(f.get("name") or "")
                required = (f.get("type") or {}).get("kind") == "NON_NULL"
                if n not in needed and required and f.get("defaultValue") is None:
                    problems.append(f"{tname}.{n} is required but not sent by this connector")
        return problems

    def check_schema(self) -> None:
        """Introspect the live Authoring schema once per connector instance; raise (nothing
        read or written yet) when it does not match the inferred shapes."""
        if self._schema_checked:
            return
        try:
            data = self._gql("SitecoreSchemaCheck", Q_SCHEMA)
        except ConnectorError as e:
            raise ConnectorError(
                "Sitecore schema check failed, so nothing was read or written: the Authoring "
                f"API did not answer schema introspection ({e})."
            ) from None
        problems = self._schema_problems(data)
        if problems:
            raise ConnectorError(
                "Sitecore schema check failed, so nothing was read or written: this "
                "environment's Authoring API does not match what the connector expects ("
                + "; ".join(problems[:6])
                + "). See the Sitecore live QA runbook, MANDATORY-1."
            )
        self._schema_checked = True

    # ---------------------------------------------------------------- languages
    def _languages(self) -> list[str]:
        if self._lang_cache is None:
            data = self._gql("SitecoreLanguages", Q_LANGUAGES)
            nodes = ((data.get("languages") or {}).get("nodes")) or []
            self._lang_cache = [
                str(n["name"]).strip()
                for n in nodes
                if isinstance(n, dict) and str(n.get("name") or "").strip()
            ]
        return self._lang_cache

    def enabled_locales(self) -> list[str]:
        return [n.lower() for n in self._languages()]

    def resolve_locale(self, loc: str) -> str:
        """The exact Sitecore language name for a (possibly lower-cased) locale."""
        want = str(loc).strip().lower()
        for n in self._languages():
            if n.lower() == want:
                return n
        return str(loc).strip()

    def source_locale(self) -> str:
        cfg = (self.config.get("source_locale") or "").strip() or "en"
        exact = self.resolve_locale(cfg)
        if exact.lower() not in self.enabled_locales():
            raise ConnectorError(f"Source language {cfg!r} does not exist in this Sitecore.")
        return exact

    # --------------------------------------------------------------- scope (A)
    def root(self) -> tuple[str, str]:
        """(root item id, canonical root path). Resolved once; fails closed."""
        if self._root_cache is None:
            path = self._root_path_cfg()
            data = self._gql("SitecoreItemByPath", Q_ITEM_BY_PATH, {"path": path})
            item = data.get("item")
            rid = canonical_guid(str((item or {}).get("itemId") or ""))
            rpath = str((item or {}).get("path") or "").rstrip("/")
            if not item or not rid:
                raise ConnectorError(f"Content root {path!r} was not found in Sitecore.")
            if not rpath.lower().startswith(CONTENT_PREFIX):
                raise ConnectorError(f"Content root {path!r} resolved outside {CONTENT_PREFIX}.")
            self._root_cache = (rid, rpath)
        return self._root_cache

    def in_scope(self, path: str) -> bool:
        _rid, rpath = self.root()
        p = str(path or "").rstrip("/").lower()
        r = rpath.lower()
        return p == r or p.startswith(r + "/")

    @staticmethod
    def _validate_id(raw) -> str:
        guid = canonical_guid(raw)
        if guid is None:
            raise ConnectorError(
                f"Unsafe item id {str(raw)[:60]!r}: want exactly one Sitecore item GUID."
            )
        return guid

    # ------------------------------------------------------------- test / scope
    def test(self) -> tuple[bool, str]:
        try:
            langs = self._languages()
            self.check_schema()
            _rid, rpath = self.root()
            return True, (
                f"Connected to Sitecore XM Cloud ({len(langs)} language(s)); content root {rpath}."
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            p = self._root_path_cfg()
        except Exception:
            return []
        return [{"id": p, "name": f"{p} (content root)", "tables": []}]

    # ---------------------------------------------------------------- discovery
    @staticmethod
    def _flatten(item: dict) -> dict | None:
        """A normalized item dict, or None when it lacks a trusted id or any version."""
        if not isinstance(item, dict):
            return None
        guid = canonical_guid(str(item.get("itemId") or ""))
        try:
            version = int(item.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if guid is None or version < 1:
            return None
        nodes = ((item.get("fields") or {}).get("nodes")) or []
        fields = []
        for n in nodes:
            if not isinstance(n, dict) or not n.get("name"):
                continue
            tf = n.get("templateField") if isinstance(n.get("templateField"), dict) else {}
            # The Authoring schema states sharing ONCE, as the `versioning` enum on the
            # template field (VERSIONED | UNVERSIONED | SHARED), not as two booleans. Anything
            # else (missing, null, unknown word) stays None = "sharing unknown" = not written.
            ver = str(tf.get("versioning") or "").strip().upper()
            shared = {"SHARED": True, "UNVERSIONED": False, "VERSIONED": False}.get(ver)
            fields.append(
                {
                    "name": str(n["name"]),
                    "value": n.get("value") if isinstance(n.get("value"), str) else "",
                    "type": str(tf.get("type") or "").strip().lower(),
                    "shared": shared,
                    "unversioned": ver == "UNVERSIONED" if ver else None,
                }
            )
        try:
            total = int(((item.get("fields") or {}).get("totalCount")) or 0)
        except (TypeError, ValueError):
            total = 0
        return {
            "id": guid,
            "name": str(item.get("name") or ""),
            "path": str(item.get("path") or ""),
            "version": version,
            "language": str(((item.get("language") or {}).get("name")) or ""),
            "fields": fields,
            "fields_total": total,
        }

    def _fetch_item(self, guid: str, lang: str) -> dict | None:
        data = self._gql("SitecoreItem", Q_ITEM, {"id": guid, "lang": lang})
        return self._flatten(data.get("item"))

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source-language items to translate. Explicit ids are validated as GUIDs up front
        (any bad one refuses the whole request, before a request leaves the process), then
        each must resolve under the content root. Otherwise the content root is searched."""
        cap = fetch_cap(cap or self.config.get("max_items"), 50)
        self.check_schema()
        src = self.source_locale()
        root_id, root_path = self.root()
        if article_ids:
            ids = []
            for raw in list(article_ids)[:cap]:
                guid = self._validate_id(raw)
                if guid not in ids:
                    ids.append(guid)
            out: list[dict] = []
            for guid in ids:
                item = self._fetch_item(guid, src)
                if item is None:
                    continue
                if not self.in_scope(item["path"]):
                    raise ConnectorError(
                        f"Item {guid} is outside this connection's content root ({root_path}); "
                        "refusing to target it."
                    )
                out.append(self._charge_record(item))
            return out
        out = []
        page = 0
        query = Q_SEARCH % root_id
        while len(out) < cap:
            data = self._gql(
                "SitecoreSearch",
                query,
                {"lang": src, "pageIndex": page, "pageSize": min(_PAGE_SIZE, cap - len(out))},
            )
            search = data.get("search") or {}
            rows = search.get("results") or []
            if not rows:
                break
            for row in self._keep_rows(rows):
                item = self._flatten((row or {}).get("innerItem") or {})
                if item is None or not self.in_scope(item["path"]):
                    continue
                out.append(item)
            page += 1
            try:
                total = int(search.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            if page * _PAGE_SIZE >= total:
                break
        return out[:cap]

    # --------------------------------------------------------- field selection (D)
    def _field_selection(self, fields: list[dict]) -> tuple[list[dict], dict]:
        allow = parse_names(self.config.get("fields"))
        exclude = set(parse_names(self.config.get("exclude_fields")))
        selected: list[dict] = []
        skipped: dict = {}
        present = set()
        for f in fields:
            name = f["name"]
            present.add(name)
            if name.startswith("__"):
                continue
            if allow and name not in allow:
                continue
            if name in exclude and not allow:
                skipped[name] = "operator exclude_fields"
                continue
            if f.get("shared") is True:
                skipped[name] = "shared across all languages (writing it would change the source)"
                continue
            if f.get("shared") is not False:
                skipped[name] = "sharing unknown: the API did not say, so it is not written"
                continue
            kind = _TYPE_KIND.get(f.get("type") or "")
            if kind is None:
                skipped[name] = (
                    f"field type {f.get('type') or 'unknown'!r} is not translatable text"
                )
                continue
            value = f.get("value") or ""
            if not value.strip():
                continue
            if kind != "html":
                v = value.strip()
                if _GUID_VALUE_RE.match(v) or _URL_RE.match(v) or _NUMERIC_RE.match(v):
                    skipped[name] = "id, URL or number shaped value"
                    continue
            selected.append({"name": name, "type": kind, "label": name})
        for n in allow:
            if n not in present:
                skipped[n] = "listed in `fields` but not a field on this item"
        return selected, skipped

    def _title_field(self, specs: list[dict]) -> str | None:
        plain = {s["name"].lower(): s["name"] for s in specs if s["type"] != "html"}
        cfg = (self.config.get("title_field") or "").strip().lower()
        for cand in (cfg, "title", "headline", "heading"):
            if cand and cand in plain:
                return plain[cand]
        return None

    def extract(self, art: dict) -> fieldmap.Extraction:
        from app.filters import get_filter

        specs, skipped = self._field_selection(art.get("fields") or [])
        total = int(art.get("fields_total") or 0)
        if total > len(art.get("fields") or []):
            skipped["(other fields)"] = (
                f"the item has {total} fields but only {len(art.get('fields') or [])} were "
                "returned; the rest are not translated"
            )
        self.sel.record_selection([s["name"] for s in specs], skipped)
        record = {f["name"]: f.get("value") or "" for f in art.get("fields") or []}
        tf = self._title_field(specs)
        return fieldmap.extract_record(record, specs, get_filter, title_field=tf or "")

    def selection_summary(self) -> str:
        return self.sel.selection_summary()

    # ---------------------------------------------------- SubResourceClient (T2)
    def _item_state(self, guid: str, lang: str) -> dict | None:
        """{path, version} of (item, language), None when the item does not exist. Refuses an
        item outside the content root, so no write can reach it (taxonomy A)."""
        data = self._gql("SitecoreItemState", Q_ITEM_STATE, {"id": guid, "lang": lang})
        item = data.get("item")
        if not isinstance(item, dict):
            return None
        path = str(item.get("path") or "")
        if not self.in_scope(path):
            raise ConnectorError(
                f"Item {guid} is outside this connection's content root; refusing to target it."
            )
        try:
            version = int(item.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        return {"path": path, "version": version}

    def get_translation(self, record_id: str, locale: str):
        """EXISTENCE PROBE for (item, language): an explicit version count, never a content
        read. A language with no version yet answers version 0 and is ABSENT."""
        guid = self._validate_id(record_id)
        st = self._item_state(guid, self.resolve_locale(locale))
        if st is None or st["version"] < 1:
            return ABSENT
        return {"version": st["version"]}

    @staticmethod
    def _build_input(fields: dict) -> tuple[list[dict], bool]:
        """The updateItem field list. The strategy-injected `draft` flag (a bool, no prefix)
        is dropped; every real field arrives under FIELD_PREFIX."""
        out = []
        for k, v in (fields or {}).items():
            if not isinstance(k, str) or not k.startswith(FIELD_PREFIX):
                continue
            out.append({"name": k[len(FIELD_PREFIX) :], "value": v, "reset": False})
        return out, bool(out)

    def _write_fields(
        self, guid: str, lang: str, version: int, items: list[dict], *, created: bool
    ) -> WriteResult:
        try:
            data = self._gql(
                "SitecoreUpdateItem",
                M_UPDATE_ITEM,
                {"input": {"itemId": guid, "language": lang, "version": version, "fields": items}},
            )
        except (ConnectorError, httpx.TransportError) as e:
            return WriteResult(False, created, message=f"{type(e).__name__}: {e}"[:300])
        if not isinstance(((data.get("updateItem") or {}).get("item")), dict):
            return WriteResult(False, created, message="updateItem returned no item")
        return WriteResult(
            True, created, target_id=guid, message="created" if created else "updated"
        )

    def _undo_new_version(self, guid: str, lang: str, version: int, why: str) -> WriteResult:
        """The first version in `lang` was added but its fields were not written. An existing
        language version, even an empty one, switches off Sitecore's item-level language
        fallback, so once published visitors would see a blank page instead of the fallback.
        Delete exactly the version this run added, then confirm by re-probing master."""
        if version != 1:
            # The language had no version at the probe, so ours is version 1; a higher number
            # means another writer raced in, and a version we did not create is never deleted.
            return WriteResult(
                False,
                True,
                message=(
                    f"language version {version} was added but its fields were not written "
                    f"and it was left in place (another writer added versions meanwhile): {why}"
                )[:300],
            )
        err = ""
        try:
            self._gql(
                "SitecoreDeleteVersion",
                M_DELETE_VERSION,
                {"input": {"itemId": guid, "language": lang, "version": version}},
            )
        except (ConnectorError, httpx.TransportError) as e:
            err = f" ({type(e).__name__}: {e})"
        try:
            after = self._item_state(guid, lang)
            gone = after is not None and after["version"] < 1
        except (ConnectorError, httpx.TransportError):
            gone = False
        if gone:
            return WriteResult(
                False,
                True,
                message=(
                    "the new language version was removed again after its fields failed to "
                    f"write, so the language fallback still applies; will retry: {why}"
                )[:300],
            )
        log.warning("sitecore: empty language version left on %s %s v%s", guid, lang, version)
        return WriteResult(
            False,
            True,
            message=(
                f"EMPTY language version {version} left in master and could not be removed"
                f"{err[:80]}: delete it or rerun before publishing, or visitors lose the "
                f"language fallback. Write error: {why}"
            )[:400],
        )

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """Add the first version in the language, then write ONLY the translated fields."""
        self.check_schema()
        guid = self._validate_id(record_id)
        lang = self.resolve_locale(locale)
        items, had_real = self._build_input(fields)
        if not had_real:
            return WriteResult(False, True, message=HELD_BACK)
        st = self._item_state(guid, lang)
        if st is None:
            return WriteResult(False, True, message=f"item {guid} was not found")
        if st["version"] >= 1:  # raced: another writer created it between probe and write
            return self._write_fields(guid, lang, st["version"], items, created=False)
        try:
            data = self._gql(
                "SitecoreAddVersion",
                M_ADD_VERSION,
                {"input": {"itemId": guid, "language": lang}},
                idempotent=False,
            )
        except (ConnectorError, httpx.TransportError) as e:
            msg = f"{type(e).__name__}: {e}"
            if isinstance(e, httpx.TransportError):
                # The add may have landed before the connection dropped. A version that
                # appeared is not provably ours, so it is reported, never deleted.
                try:
                    after = self._item_state(guid, lang)
                except (ConnectorError, httpx.TransportError):
                    after = None
                if after is None or after["version"] >= 1:
                    msg = (
                        "addItemVersion may have added an EMPTY language version before the "
                        "connection failed; check the item in Content Editor before "
                        f"publishing (a rerun fills it): {msg}"
                    )
            return WriteResult(False, True, message=msg[:400])
        item = (data.get("addItemVersion") or {}).get("item")
        try:
            version = int((item or {}).get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version < 1:
            again = self._item_state(guid, lang)
            version = again["version"] if again else 0
        if version < 1:
            return WriteResult(False, True, message="addItemVersion did not return a version")
        res = self._write_fields(guid, lang, version, items, created=True)
        if not res.ok:
            return self._undo_new_version(guid, lang, version, res.message or "write failed")
        return res

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """Write ONLY the fields translated this run onto the latest existing version; a
        field not in the list (held back, empty, shared) keeps its stored value."""
        self.check_schema()
        guid = self._validate_id(record_id)
        lang = self.resolve_locale(locale)
        items, had_real = self._build_input(fields)
        if not had_real:
            return WriteResult(False, False, message=HELD_BACK)
        st = self._item_state(guid, lang)
        if st is None:
            return WriteResult(False, False, message=f"item {guid} was not found")
        if st["version"] < 1:  # the version vanished between probe and write
            return self.create_translation(record_id, locale, fields)
        return self._write_fields(guid, lang, st["version"], items, created=False)

    # --------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for art in self.discover_articles():
            ex = self.extract(art)
            label = (ex.title_text.strip() or art.get("name") or art["id"])[:60]
            if ex.title_field is not None and ex.title_text.strip():
                out.append(
                    PulledItem(
                        external_id=art["id"], field=ex.title_field, text=ex.title_text, title=label
                    )
                )
            for u in ex.body_units:
                out.append(
                    PulledItem(
                        external_id=art["id"], field=str(u.p_index), text=u.text, title=label
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Sitecore writes language versions - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []
