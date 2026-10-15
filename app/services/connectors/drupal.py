"""Drupal connector: a T2 consumer of the connector framework.

TOPOLOGY - T2. A Drupal content entity (node, taxonomy term, custom block, media item) is ONE
record that holds many per-language translations of its fields; the SOURCE SYSTEM owns the
correlation (the same UUID, a `langcode` per translation). That is the T2 shape, so write-back
rides the shared `strategy.SubResourceStrategy` (existence probe -> create or update).

INTEGRATION PATH - core JSON:API + the contrib `jsonapi_multilingual` module. Core JSON:API
alone cannot add a translation to an existing entity (POST only creates an entity in a
non-default language) and addresses a translation only through URL language negotiation. The
contrib module adds per-translation GET / POST / PATCH over the normal resource URLs, chosen by
a `?langCode=xx` query parameter:

    GET   /jsonapi/node/article/{uuid}?langCode=fr    404 if that translation does not exist
    POST  /jsonapi/node/article/{uuid}?langCode=fr    creates it (409 if it exists)
    PATCH /jsonapi/node/article/{uuid}?langCode=fr    updates only that translation

The other route (a TMGMT translator plugin, which is what the bb-io Drupal app drives) needs a
custom Drupal module on the customer site and is a different product shape; it is out of scope.

THE ONE WAY THIS GOES WRONG, AND THE GUARDS. On a site WITHOUT the module, core JSON:API
ignores `langCode`, so a PATCH meant for the French translation would overwrite the SOURCE
entity. Every write is therefore preceded by an existence probe that must PROVE the module is
active: the strict read of translation X answers 404 (absent) or a resource whose `langcode`
IS X. A 200 in any other language means the module is missing and the connector raises before
anything is written (`get_translation`). The same probe refuses a target that is the entity's
own default language. Discovery runs the probe once up front (`_ensure_multilingual`) so a
missing module is reported before any machine translation is spent.

WRITE MODEL. JSON:API PATCH replaces only the attributes it names, so a partial body keeps every
field we do not send; that is what protects an existing human translation of a held-back field
(taxonomy B). A formatted-text field (`{value, format, summary}`) is replaced as a WHOLE, so a
half-translated one is never sent: if any unit of it is held, the entire field is omitted. A new
translation is created UNPUBLISHED (`status: false`) and never published by this connector.

HELD-BACK FIELDS (#640). The flow engine refuses to create a translation while any of its units
is held (`held_prefix`), because Drupal seeds a new translation with the SOURCE values of every
translatable field not sent, and the content hash would hide that for good. A pair with a held
unit never records a hash, so the next run retries once the segment is approved.

EXISTENCE PROBE (taxonomy C). Reads and writes target the same state: the latest default
revision. A new translation is read back right after the POST; if it is not visible (content
moderation parks it in a non-default revision) the pair is reported as failed, never done.

AUTH. One of: Simple OAuth client credentials (client id + secret; the token is minted in
process and cached, no refresh token exists, so this adds no second refresh path), HTTP Basic
(core basic_auth), or a pasted Bearer token. No credential is logged or echoed.

CHANGE DETECTION - polling with content-hash suppression (Drupal core has no webhooks).

PROVENANCE: the vendor request shapes are built from Drupal's public documentation. The
bb-io/Drupal app (c) 2023 Blackbird, MIT) was studied for retry and HTML-error-page handling
only. See THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import base64
import hashlib
import json as _json
import logging
import re
import threading
import time
from urllib.parse import quote

import httpx

from app.services.connectors import flow, harvest, webhooks  # noqa: F401 (parity imports)
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
from app.services.connectors.drupal_fields import (
    ALLOWED_ENTITY_TYPES,
    HELD_BACK,
    HELD_BACK_PREFIX,
    INJECTED_KEYS,
    LANG_RE,
    LIVE_PREFIX,
    PREFIX_RE,
    TYPE_RE,
    UUID_RE,
    Unit,
    field_payload,
    parse_names,
    pick_fields,
    plan_field,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult
from app.services.connectors.tracking import SelectionTracker
from app.services.vendor_dispatch_common import _endpoint_host

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "jsonapi"
_JSONAPI = "application/vnd.api+json"
_PAGE = 50  # Drupal's hard JSON:API page cap
_MAX_PAGES = 200  # a server that always says "next" cannot hold a run forever
_TITLE_CANDIDATES = ("title", "name", "info")  # node / term / custom block label

_NO_MODULE = (
    "The Drupal site did not honour langCode={loc}: the JSON:API Multilingual module "
    "(drupal/jsonapi_multilingual) is not installed or not enabled, so a translation cannot be "
    "addressed on its own. Nothing was written."
)

_NO_VIEW = (
    "Drupal answered 403 for the {loc} translation: the API user may not VIEW it (an unpublished "
    "translation needs 'view any unpublished content', see the setup steps). Nothing was written."
)

# Raised on every later call once a write was seen to land in the WRONG language: the run stops
# writing at once (one bad write is the most this connector can ever do per run).
_TRIPPED = (
    "SOURCE OVERWRITE SUSPECTED: a write meant for {loc} was answered in language {got!r}, so the "
    "JSON:API Multilingual module was not in effect for that request and the SOURCE entity "
    "{rid} may have been changed. All further writes in this run are refused. Restore the "
    "previous revision in Drupal (Revisions tab) and check the module is installed and enabled."
)

# Simple OAuth client-credentials tokens, cached per (site, client, secret hash). No refresh
# token exists in this grant, so there is no refresh path to keep single-flight.
_TOKEN_CACHE: dict = {}
_TOKEN_LOCK = threading.Lock()
_TOKEN_MARGIN = 30.0
_SAFE_ERROR_RE = re.compile(r"^[a-z_]{1,40}$")


def _now() -> float:
    return time.monotonic()


SPEC = ConnectorSpec(
    kind="drupal",
    name="Drupal",
    icon="💧",
    cat="CMS",
    blurb="EXPERIMENTAL: depends on the alpha-stage JSON:API Multilingual contrib module "
    "(1.0.0-alpha5). Translate Drupal content (nodes, taxonomy terms, blocks) into any language "
    "using core Content Translation and that module. Each translation is written back as the "
    "entity's own translation in that language and saved unpublished; the connector refuses to "
    "write unless the module is proven active.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Site URL",
            "Your Drupal site root, e.g. https://www.example.edu. Must be a publicly reachable "
            "host: loopback and internal targets are refused by the SSRF guard unless the "
            "operator allowlists a private address. The connector appends /jsonapi.",
            placeholder="https://www.example.edu",
        ),
        FieldSpec(
            "resource_types",
            "Resource types",
            "Comma-separated JSON:API resource types to translate, written entity--bundle "
            "(node--article, node--page, taxonomy_term--tags, block_content--basic, "
            "media--document). Users and config entities are not supported.",
            placeholder="node--article, node--page",
        ),
        FieldSpec(
            "source_locale",
            "Source language",
            "The Drupal language code to translate FROM (default: en). Only entities whose "
            "original language is this one are translated.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target languages",
            "Comma-separated Drupal language codes to create (each must be added under "
            "Configuration -> Languages and enabled for the content type).",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "Optional allowlist of field names to translate (comma-separated). Blank = "
            "auto-discover text and formatted-text fields by name and value shape (the run "
            "report lists what was selected and skipped). Set it for a known schema: Drupal "
            "does not tell a content token which fields are translatable.",
            required=False,
            placeholder="title, body, field_summary",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude fields",
            "Field names to NEVER translate (comma-separated). Applies only when the "
            "allowlist above is blank.",
            required=False,
            placeholder="field_internal_code",
        ),
        FieldSpec(
            "edit_published",
            "Edit live translations",
            "'1' = also rewrite a translation that is already PUBLISHED when the source "
            "changed. '0' (default) = a published translation is left alone and reported, so a "
            "run can never silently change live content. Translations this connector created "
            "are unpublished, so they are always updated.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "title_field",
            "Title field",
            "Field used as the display title (default: title, name or info when present).",
            required=False,
            placeholder="title",
        ),
        FieldSpec(
            "username",
            "Username (Basic auth)",
            "Only for HTTP Basic authentication (core basic_auth). Leave blank for OAuth or "
            "a token.",
            required=False,
            placeholder="locos-bot",
        ),
        FieldSpec(
            "oauth_scope",
            "OAuth scope",
            "Optional Simple OAuth scope for the client-credentials grant.",
            required=False,
            placeholder="content_editor",
        ),
        FieldSpec(
            "api_prefix",
            "JSON:API path prefix",
            "Only if the site changed it (JSON:API Extras). Default: jsonapi.",
            required=False,
            placeholder="jsonapi",
        ),
        FieldSpec(
            "max_entries",
            "Max entities",
            "Safety cap on entities scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "client_id",
            "OAuth client id",
            "Simple OAuth consumer UUID (client credentials grant). Use with the secret below.",
            required=False,
            secret=True,
            placeholder="consumer uuid",
        ),
        FieldSpec(
            "client_secret",
            "OAuth client secret",
            "The consumer's secret.",
            required=False,
            secret=True,
            placeholder="a long secret",
        ),
        FieldSpec(
            "password",
            "Password (Basic auth)",
            "The API user's password (with the username above).",
            required=False,
            secret=True,
            placeholder="password",
        ),
        FieldSpec(
            "access_token",
            "Bearer token",
            "A pre-issued Bearer token (for example from a long-lived key module). "
            "Simple OAuth tokens expire in minutes, so prefer the client id and secret.",
            required=False,
            secret=True,
            placeholder="a long token",
        ),
    ],
    setup_steps=[
        "In Drupal, enable the Language, Content Translation and JSON:API modules; add every "
        "target language under Configuration -> Regional and language -> Languages.",
        "Under Configuration -> Regional and language -> Content language and translation, "
        "tick each content type and mark its text fields translatable.",
        "Install the JSON:API Multilingual module (composer require "
        "drupal/jsonapi_multilingual, then enable it) and set JSON:API to accept write "
        "operations (Configuration -> Web services -> JSON:API). EXPERIMENTAL: that module is an "
        "alpha release (1.0.0-alpha5); pin its exact version in composer.lock and never disable "
        "or upgrade it while a run is going. If a write ever lands on the source language, the "
        "run stops and names it: restore the previous revision from the entity's Revisions tab.",
        "Create an API user (or a Simple OAuth consumer whose user has the role) with these "
        "permissions and NOT 'bypass node access': access content; edit any <type> content for "
        "each type; 'translate any entity', 'create content translations' and 'update content "
        "translations'; 'administer node published status' (needed to save a new translation "
        "unpublished); and 'view any "
        "unpublished content' (enable the core Content Moderation module for that permission; "
        "it needs no workflow). For Basic auth, enable the HTTP Basic Authentication module.",
        "Enter the Site URL, the resource types (entity--bundle, e.g. node--article), the "
        "credentials and your target languages.",
    ],
    other_side="Experimental connector (alpha-dependent: JSON:API Multilingual 1.0.0-alpha5). "
    "Change detection is poll-based: run on a schedule. Unchanged entities are "
    "suppressed by content hash. A new translation is created UNPUBLISHED and never "
    "published by this connector. A translation that is already published is left alone and "
    "reported unless 'Edit live translations' is on. Concurrent edits of the same field are "
    "last-write-wins.",
    docs_url="https://www.drupal.org/docs/core-modules-and-themes/core-modules/jsonapi-module/translations",
    supports_oauth=False,
)


class DrupalConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="drupal",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY, AuthKind.BASIC),
        api_version=API_VERSION,
        rich_text=RichText.HTML,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="changed", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = SubResourceStrategy(source_locale_of=lambda conn, rec: self._source())
        # Field-selection visibility (taxonomy D). Structural skips (links, lists) are loud.
        self.sel = SelectionTracker(
            noun="field", label="drupal", loud_markers=("structured value", "multi-value")
        )
        self._lang_cache: list[str] | None = None
        self._lang_state = ""  # "" (not tried) | "read" | "configured" (list unreadable)
        self._ml_state: str | None = None  # None | "present" | "missing" | "unknown"
        self.other_language_skipped = 0  # entities whose original language is not the source
        self._tripped: str | None = None  # set once a write landed in the wrong language

    # ------------------------------------------------------------------ config
    def _site_root(self) -> str:
        raw = (self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "Drupal Site URL is required (your site root, e.g. https://www.example.edu)."
            )
        return _validate_target(raw).rstrip("/")

    def _prefix(self) -> str:
        p = (self.config.get("api_prefix") or "jsonapi").strip().strip("/")
        if not PREFIX_RE.fullmatch(p) or ".." in p:
            raise ConnectorError(f"Invalid JSON:API path prefix {p!r}.")
        return p

    def _base(self) -> str:
        root = self._site_root()
        p = self._prefix()
        if root.lower().endswith("/" + p.lower()):
            root = root[: -(len(p) + 1)].rstrip("/")
        return f"{root}/{p}"

    @staticmethod
    def _quote(*segments) -> str:
        return "/" + "/".join(quote(str(s), safe="") for s in segments)

    def _path(self, rtype: str, uuid: str | None = None) -> str:
        entity, _sep, bundle = rtype.partition("--")
        return self._quote(*(entity, bundle) if uuid is None else (entity, bundle, uuid))

    def _resource_types(self) -> list[str]:
        types = parse_names(self.config.get("resource_types"))
        if not types:
            raise ConnectorError(
                "At least one resource type is required, written entity--bundle "
                "(for example node--article)."
            )
        for t in types:
            if not TYPE_RE.fullmatch(t):
                raise ConnectorError(
                    f"Invalid resource type {t!r}: write it as entity--bundle with lowercase "
                    f"letters, digits and underscores (for example node--article)."
                )
            if t.partition("--")[0] not in ALLOWED_ENTITY_TYPES:
                raise ConnectorError(
                    f"Resource type {t!r} is not supported: only "
                    f"{', '.join(sorted(ALLOWED_ENTITY_TYPES))} entities can be translated."
                )
        return types

    def _validate_type(self, rtype: str, types: list[str] | None = None) -> str:
        """Fail closed: a caller-named type MUST be one this connection is configured for."""
        types = types if types is not None else self._resource_types()
        t = str(rtype).strip()
        if t not in types:
            raise ConnectorError(
                f"Resource type {rtype!r} is not configured on this connection "
                f"({', '.join(types)}) - refusing to target it."
            )
        return t

    @staticmethod
    def _validate_uuid(uuid: str) -> str:
        u = str(uuid).strip()
        if not UUID_RE.fullmatch(u):
            raise ConnectorError(f"Unsafe entity id {uuid!r}: must be a Drupal UUID.")
        return u.lower()

    def _split_aid(self, record_id: str) -> tuple[str, str]:
        t, sep, u = str(record_id).partition(":")
        if not sep or not t or not u:
            raise ConnectorError(
                f"Malformed Drupal record id {record_id!r} (want '<type>:<uuid>')."
            )
        return self._validate_type(t), self._validate_uuid(u)

    def _resolve_explicit(self, raw: str, types: list[str]) -> tuple[str, str]:
        if ":" in raw:
            t, _sep, u = raw.partition(":")
            return self._validate_type(t.strip(), types), self._validate_uuid(u)
        if len(types) == 1:
            return types[0], self._validate_uuid(raw)
        raise ConnectorError(
            f"Ambiguous entity id {raw!r}: this connection has several resource types, so ids "
            f"must be written '<type>:<uuid>'."
        )

    # -------------------------------------------------------------------- auth
    def _auth_mode(self) -> str:
        s = self.secret
        cid = (s.get("client_id") or "").strip()
        csec = (s.get("client_secret") or "").strip()
        user = (self.config.get("username") or "").strip()
        pwd = s.get("password") or ""
        tok = (s.get("access_token") or "").strip()
        if cid or csec:
            if not (cid and csec):
                raise ConnectorError("Drupal OAuth needs both the client id and the client secret.")
            return "oauth"
        if user or pwd:
            if not (user and pwd):
                raise ConnectorError("Drupal Basic auth needs both the username and the password.")
            return "basic"
        if tok:
            return "bearer"
        raise ConnectorError(
            "Drupal credentials are required: an OAuth client id and secret, a username and "
            "password (Basic auth), or a Bearer token."
        )

    def _token_key(self) -> tuple:
        sec = (self.secret.get("client_secret") or "").strip().encode("utf-8")
        return (
            self._site_root(),
            (self.secret.get("client_id") or "").strip(),
            hashlib.sha256(sec).hexdigest(),
            (self.config.get("oauth_scope") or "").strip(),  # a token is only as wide as its scope
        )

    def _invalidate_token(self) -> None:
        with _TOKEN_LOCK:
            _TOKEN_CACHE.pop(self._token_key(), None)

    def _mint_token(self) -> tuple[str, float]:
        """Client-credentials grant against Simple OAuth's /oauth/token. Only a short error
        code is ever surfaced (never the upstream body), and a code that carries the client id
        or secret is dropped, so a proxy echoing the form cannot leak into an error."""
        cid = (self.secret.get("client_id") or "").strip()
        csec = (self.secret.get("client_secret") or "").strip()
        form = {"grant_type": "client_credentials", "client_id": cid, "client_secret": csec}
        scope = (self.config.get("oauth_scope") or "").strip()
        if scope:
            form["scope"] = scope
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                self._site_root() + "/oauth/token",
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                idempotent=False,
            )
        body: dict = {}
        try:
            parsed = r.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
        if r.status_code != 200:
            code = str(body.get("error") or "").strip()
            if not _SAFE_ERROR_RE.match(code) or any(v and v in code for v in (cid, csec)):
                code = ""
            raise ConnectorError(
                f"Drupal refused the OAuth client id / secret (HTTP {r.status_code}"
                + (f", {code}" if code else "")
                + "). Check the consumer, that it allows the client credentials grant, and "
                "that it has a user assigned."
            )
        token = str(body.get("access_token") or "").strip()
        if not token:
            raise ConnectorError("Drupal token response had no access_token.")
        try:
            ttl = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            ttl = 0.0
        return token, (ttl if ttl > 0 else 300.0)

    def _oauth_token(self) -> str:
        key = self._token_key()
        with _TOKEN_LOCK:
            hit = _TOKEN_CACHE.get(key)
            if hit and hit[1] - _TOKEN_MARGIN > _now():
                return hit[0]
        token, ttl = self._mint_token()
        with _TOKEN_LOCK:
            _TOKEN_CACHE[key] = (token, _now() + ttl)
        return token

    def _authorization(self) -> str:
        mode = self._auth_mode()
        if mode == "oauth":
            return f"Bearer {self._oauth_token()}"
        if mode == "basic":
            raw = (
                f"{(self.config.get('username') or '').strip()}:{self.secret.get('password') or ''}"
            )
            return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return f"Bearer {(self.secret.get('access_token') or '').strip()}"

    def _call(self, c, method, path, *, params=None, body=None, idempotent=True):
        """One authenticated JSON:API call through base._request (retry, backoff, size and
        deadline bounds). A 401 on an OAuth connection drops the cached token and retries ONCE
        (a 401 means the request was not processed, so resending a write is safe)."""
        url = self._base() + path
        content = _json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (0, 1):
            headers = {"Accept": _JSONAPI, "Authorization": self._authorization()}
            if method == "GET":
                # a probe must be answered by the origin, never by a shared cache
                headers["Cache-Control"] = "no-cache"
                headers["Pragma"] = "no-cache"
            if content is not None:
                headers["Content-Type"] = _JSONAPI
            r = self._request(
                c,
                method,
                url,
                params=params,
                content=content,
                headers=headers,
                idempotent=idempotent,
            )
            if r.status_code == 401 and attempt == 0 and self._auth_mode() == "oauth":
                self._invalidate_token()
                continue
            return r
        return r

    # ---------------------------------------------------------------- responses
    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            body = r.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _err(r: httpx.Response) -> str:
        """A short, bounded error summary: status plus the JSON:API error title/detail. The raw
        body is never echoed (an HTML login page or a proxy page could carry anything)."""
        text = ""
        try:
            errs = (r.json() or {}).get("errors")
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                text = str(errs[0].get("detail") or errs[0].get("title") or "")
        except Exception:
            text = ""
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:160]
        return f"{r.status_code}: {text}" if text else str(r.status_code)

    def _flatten(self, row, rtype: str, *, strict: bool = False) -> dict | None:
        """strict (every probe that gates a write): the resource MUST name its type."""
        if not isinstance(row, dict):
            return None
        attrs = row.get("attributes")
        uuid = str(row.get("id") or "").strip()
        if not isinstance(attrs, dict) or not UUID_RE.fullmatch(uuid):
            return None
        if row.get("type") not in ((rtype,) if strict else (None, rtype)):
            return None  # an included/other resource, never ours to write
        return {"_type": rtype, "uuid": uuid.lower(), "attrs": attrs}

    def _aid(self, ent: dict) -> str:
        return f"{ent['_type']}:{ent['uuid']}"

    # --------------------------------------------------------------- languages
    def _languages(self) -> list[str]:
        """Enabled language ids. Reading them needs the 'administer languages' permission; a
        content-only API user gets a 403, so fall back to the operator-configured set."""
        if self._lang_cache is not None:
            return self._lang_cache
        ids: list[str] = []
        with self._client() as c:
            r = self._call(
                c,
                "GET",
                self._quote("configurable_language", "configurable_language"),
                params={"page[limit]": _PAGE},
            )
        if r.status_code == 200:
            for row in self._json(r).get("data") or []:
                a = (row or {}).get("attributes") or {}
                lid = str(a.get("drupal_internal__id") or "").strip()
                if lid and not a.get("locked") and LANG_RE.fullmatch(lid):
                    ids.append(lid)
            self._lang_state = "read"
        else:
            self._lang_state = "configured"
        if self._lang_state != "read" or not ids:
            self._lang_state = "configured"
            ids = [self._cfg_source()] + [
                x for x in parse_names(self.config.get("target_locales")) if LANG_RE.fullmatch(x)
            ]
        self._lang_cache = list(dict.fromkeys(ids))
        return self._lang_cache

    def enabled_locales(self) -> list[str]:
        return [x.lower() for x in self._languages()]

    def _cfg_source(self) -> str:
        s = (self.config.get("source_locale") or "").strip() or "en"
        if not LANG_RE.fullmatch(s):
            raise ConnectorError(f"Invalid source language code {s!r}.")
        return s

    def _resolve_locale(self, loc: str) -> str:
        s = str(loc).strip()
        exact = {x.lower(): x for x in self._languages()}.get(s.lower(), s)
        if not LANG_RE.fullmatch(exact):
            raise ConnectorError(f"Invalid language code {loc!r}.")
        return exact

    def _source(self) -> str:
        return self._resolve_locale(self._cfg_source())

    # -------------------------------------------------------------- test / list
    def _probe_type(self, c, rtype: str, src: str):
        r = self._call(c, "GET", self._path(rtype), params={"page[limit]": 1, "langCode": src})
        return r

    def test(self) -> tuple[bool, str]:
        try:
            types = self._resource_types()
            self._auth_mode()
            sample = None
            with self._client() as c:
                for t in types:
                    r = self._probe_type(c, t, self._source())
                    if r.status_code == 404:
                        return False, (
                            f"Resource type {t!r} was not found (404). Check the "
                            f"entity--bundle name and that JSON:API is enabled."
                        )
                    if r.status_code in (401, 403):
                        return False, (
                            f"Drupal refused access to {t} ({r.status_code}). Check the "
                            f"credentials and that the user may view that content."
                        )
                    if r.status_code != 200:
                        return False, f"Drupal answered {self._err(r)} for {t}."
                    if sample is None:
                        for row in self._json(r).get("data") or []:
                            sample = self._flatten(row, t)
                            if sample:
                                break
            langs = self._languages()
            state = self.multilingual_state(sample)
            host = _endpoint_host(self.config.get("base_url") or "") or "the site"
            if state == "missing":
                return False, _NO_MODULE.format(loc="a target language")
            tail = (
                "JSON:API Multilingual detected."
                if state == "present"
                else "Could not verify JSON:API Multilingual yet (no sample content or no "
                "target language); it is checked again before any write."
            )
            note = (
                ""
                if self._lang_state == "read"
                else " Language list not readable with this user; using the configured languages."
            )
            return True, (
                f"Connected to Drupal at {host} ({len(types)} resource type(s), "
                f"{len(langs)} language(s)). {tail}{note}"
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            return [
                {"id": t, "name": f"{t} (resource type)", "tables": []}
                for t in self._resource_types()
            ]
        except Exception:
            return []

    # ----------------------------------------------- JSON:API Multilingual guard
    def multilingual_state(self, sample: dict | None) -> str:
        """'present' | 'missing' | 'unknown'. Reads one real entity in a non-source language
        and checks what the site did with `langCode`: 404 or a resource in THAT language proves
        the module; a resource in any other language proves it absent."""
        if self._ml_state is not None:
            return self._ml_state
        if not sample:
            return "unknown"
        src = self._source().lower()
        cfg = [x.lower() for x in parse_names(self.config.get("target_locales"))]
        pool = [x for x in cfg if x != src] or [x for x in self.enabled_locales() if x != src]
        if not pool:
            return "unknown"
        target = self._resolve_locale(pool[0])
        with self._client() as c:
            r = self._call(
                c,
                "GET",
                self._path(sample["_type"], sample["uuid"]),
                params={"langCode": target},
            )
        state = "unknown"
        if r.status_code == 404:
            # only the module's strict-read 404 lists the translations; a bare 404 (deleted
            # entity, WAF, proxy) proves nothing. Diagnostic only: writes re-prove it themselves.
            errs = self._json(r).get("errors")
            meta = (errs[0].get("meta") if isinstance(errs, list) and errs else None) or {}
            if isinstance(meta, dict) and "availableTranslations" in meta:
                state = "present"
        elif r.status_code == 200:
            ent = self._flatten(self._json(r).get("data"), sample["_type"])
            if ent is not None:
                got = str(ent["attrs"].get("langcode") or "").lower()
                state = "present" if got == target.lower() else "missing"
        if state != "unknown":
            self._ml_state = state
        return state

    def _ensure_multilingual(self, sample: dict | None) -> None:
        if self.multilingual_state(sample) == "missing":
            raise ConnectorError(_NO_MODULE.format(loc="a target language"))

    # ---------------------------------------------------------------- discovery
    def _is_source(self, ent: dict, src: str) -> bool:
        a = ent["attrs"]
        return (
            str(a.get("langcode") or "").lower() == src.lower()
            and a.get("default_langcode") is not False
        )

    def _list_type(self, c, rtype: str, src: str, cap: int) -> list[dict]:
        out: list[dict] = []
        seen: set = set()
        offset = 0
        for _page in range(_MAX_PAGES):
            if len(out) >= cap:
                break
            r = self._call(
                c,
                "GET",
                self._path(rtype),
                params={
                    "page[limit]": _PAGE,
                    "page[offset]": offset,
                    "sort": "-changed",
                    "langCode": src,
                },
            )
            if r.status_code == 404:
                raise ConnectorError(
                    f"Resource type {rtype!r} not found (404). Check the entity--bundle name "
                    f"and that JSON:API is enabled for it."
                )
            if r.status_code != 200:
                raise ConnectorError(f"Drupal list {rtype} failed ({self._err(r)}).")
            body = self._json(r)
            data = body.get("data") or []
            fresh = 0
            for row in self._keep_rows(data):
                ent = self._flatten(row, rtype)
                if ent is None or ent["uuid"] in seen:
                    continue
                seen.add(ent["uuid"])
                fresh += 1
                if self._is_source(ent, src):
                    out.append(ent)
                else:
                    self.other_language_skipped += 1
            links = body.get("links")
            more = (
                bool((links or {}).get("next")) if isinstance(links, dict) else len(data) >= _PAGE
            )
            if not data or not more or fresh == 0:
                break
            offset += len(data)
        return out[:cap]

    def _fetch_entity(self, c, rtype: str, uuid: str, src: str) -> dict | None:
        r = self._call(c, "GET", self._path(rtype, uuid), params={"langCode": src})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(f"Drupal get {rtype}/{uuid} failed ({self._err(r)}).")
        return self._flatten(self._json(r).get("data"), rtype)

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source-language entities to translate. Explicit article_ids ('<type>:<uuid>', or a
        bare uuid on a single-type connection) win and are SCOPE-VALIDATED (taxonomy A). Else
        each configured type is listed (newest-changed first) and the shared cap is filled
        ROUND-ROBIN so the first type can never starve the others (taxonomy E)."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        src = self._source()
        types = self._resource_types()
        self.other_language_skipped = 0
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                for raw in list(article_ids)[:cap]:
                    t, u = self._resolve_explicit(str(raw), types)
                    ent = self._fetch_entity(c, t, u, src)
                    if ent is None:
                        continue
                    if not self._is_source(ent, src):
                        self.other_language_skipped += 1
                        continue
                    out.append(self._charge_record(ent))
            else:
                per_type = [self._list_type(c, t, src, cap) for t in types]
                out = []
                i = 0
                while len(out) < cap and any(i < len(lst) for lst in per_type):
                    for lst in per_type:
                        if i < len(lst) and len(out) < cap:
                            out.append(lst[i])
                    i += 1
        self._ensure_multilingual(out[0] if out else None)
        return out

    # --------------------------------------------------------------- extraction
    def _field_selection(self, attrs: dict) -> tuple[list[str], dict]:
        return pick_fields(
            attrs,
            parse_names(self.config.get("fields")),
            set(parse_names(self.config.get("exclude_fields"))),
        )

    def _title_field(self, attrs: dict, names: list[str]) -> str | None:
        cfg = (self.config.get("title_field") or "").strip()
        if cfg and cfg in names and isinstance(attrs.get(cfg), str):
            return cfg
        for cand in _TITLE_CANDIDATES:
            if cand in names and isinstance(attrs.get(cand), str):
                return cand
        return None

    def extract(self, ent: dict) -> tuple[str | None, str, list[Unit], str, list[dict]]:
        attrs = ent.get("attrs") or {}
        names, skipped = self._field_selection(attrs)
        self.sel.record_selection(names, skipped)
        title_field = self._title_field(attrs, names)
        title_text = str(attrs.get(title_field) or "") if title_field else ""
        plans = [plan_field(n, attrs[n]) for n in sorted(names) if n != title_field]
        body_units = [u for p in plans for part in p["parts"] for u in part["units"]]
        chash = _content_hash(title_text, *[f"{u.p_index}\x1e{u.text}" for u in body_units])
        return title_field, title_text, body_units, chash, plans

    # ------------------------------------------------------ SubResourceClient (T2)
    def edit_published(self) -> bool:
        return str(self.config.get("edit_published", "0")).strip().lower() in ("1", "true", "yes")

    def _check_tripped(self) -> None:
        if self._tripped:
            raise ConnectorError(self._tripped)

    def _trip(self, record_id: str, locale: str, got: str) -> str:
        """A write was seen to land in the wrong language. Stop every later write of this run
        and return the (loud) message. Nothing here can undo the write: Drupal keeps the
        previous revision, which is the recovery path named in the message."""
        msg = _TRIPPED.format(loc=locale, got=got or "unknown", rid=record_id)
        self._tripped = msg
        self._ml_state = "missing"
        log.error(
            "drupal: write for %s/%s answered in language %r; run halted", record_id, locale, got
        )
        return msg

    def get_translation(self, record_id: str, locale: str):
        """EXISTENCE PROBE for the (entity, language) translation: an explicit 404 is ABSENT,
        never a content read. Raises (so nothing is written) when the answer is not provably
        the requested translation: wrong language = module missing; default language = source.

        FAIL CLOSED: the resource must name its own type and id, carry `langcode` equal to the
        requested language and an explicit `default_langcode: false`; a response a shared cache
        served (Age header) is not a proof. update/create re-run this IMMEDIATELY before they
        write, so the proof is never older than one round trip."""
        self._check_tripped()
        t, u = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        with self._client() as c:
            r = self._call(c, "GET", self._path(t, u), params={"langCode": exact})
        if r.status_code == 404:
            return ABSENT
        if r.status_code == 403:
            raise ConnectorError(_NO_VIEW.format(loc=exact))
        if r.status_code != 200:
            raise ConnectorError(f"Drupal translation probe {t}/{u}/{exact} -> {self._err(r)}")
        if str(r.headers.get("age") or "0").strip() not in ("", "0"):
            raise ConnectorError(
                f"Drupal translation probe {t}/{u}/{exact} was answered by a shared cache (Age "
                f"header); a cached answer cannot prove the module is active. Nothing was written."
            )
        ent = self._flatten(self._json(r).get("data"), t, strict=True)
        if ent is None or ent["uuid"] != u:
            raise ConnectorError(f"Drupal translation probe {t}/{u}/{exact}: unexpected response.")
        attrs = ent["attrs"]
        if str(attrs.get("langcode") or "").lower() != exact.lower():
            raise ConnectorError(_NO_MODULE.format(loc=exact))
        if attrs.get("default_langcode") is not False:
            raise ConnectorError(
                f"{exact} is this entity's original language (or the site did not say otherwise); "
                f"the connector never writes the source language."
            )
        return attrs

    @staticmethod
    def _build_attrs(fields: dict) -> tuple[dict, bool]:
        """(attributes, had_real): the clean attribute dict (the strategy-injected `draft`
        bool stripped) and whether it carries at least one real translation."""
        attrs = {
            k: v
            for k, v in (fields or {}).items()
            if not (k in INJECTED_KEYS and isinstance(v, bool))
        }
        return attrs, bool(attrs)

    def _verify_write(self, r: httpx.Response, record_id: str, locale: str) -> str | None:
        """None when the write PROVABLY landed in the requested language, else an error message.
        Never trusts a missing answer: a 204, an empty body or a body without `langcode` is
        verified by a fresh strict read. A mismatch trips the run (see _trip)."""
        _t, u = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        if r.status_code == 405:
            return (
                "405: the site refused the write; JSON:API is probably in read-only mode "
                "(Configuration -> Web services -> JSON:API)"
            )
        if r.status_code not in (200, 201, 204):
            return self._err(r)
        data = self._json(r).get("data") if (r.content or b"").strip() else None
        attrs = data.get("attributes") if isinstance(data, dict) else None
        got = str((attrs or {}).get("langcode") or "") if isinstance(attrs, dict) else ""
        if got:
            if (
                got.lower() != exact.lower()
                or str(data.get("id") or "").lower() != u
                or attrs.get("default_langcode") is True
            ):
                # a default-translation answer means the SOURCE entity took the write (its
                # language was even relabelled), whatever langcode it now shows
                return self._trip(record_id, exact, got)
            return None
        try:  # unverifiable answer: read the translation back before believing the write
            if self.get_translation(record_id, locale) is ABSENT:
                return f"the write to {exact} could not be confirmed (translation not readable)"
        except ConnectorError as e:
            return self._trip(record_id, exact, "unverified") + f" ({e})"
        return None

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """Create the missing translation with ONLY the translated fields. Created UNPUBLISHED
        (`status: false`), then read back to prove it is visible on the default revision."""
        t, u = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        attrs, had_real = self._build_attrs(fields)
        if not had_real:
            return WriteResult(False, True, message=HELD_BACK)
        body = {
            "data": {
                "type": t,
                "id": u,
                "attributes": {**attrs, "langcode": exact, "status": False},
            }
        }
        try:
            # fresh proof right before the write: module active, and the translation is absent
            if self.get_translation(record_id, locale) is not ABSENT:
                return WriteResult(
                    False, True, message=f"{exact} appeared meanwhile; not created over it"
                )
            with self._client() as c:
                r = self._call(
                    c,
                    "POST",
                    self._path(t, u),
                    params={"langCode": exact},
                    body=body,
                    idempotent=False,
                )
            bad = self._verify_write(r, record_id, locale)
            if bad:
                return WriteResult(False, True, message=bad)
            made = self.get_translation(record_id, locale)
            if made is ABSENT:
                return WriteResult(
                    False,
                    True,
                    message=f"created {exact} but it is not readable on the default revision "
                    f"(content moderation?); not marked done",
                )
            if made.get("status") is True:
                return WriteResult(
                    False,
                    True,
                    target_id=u,
                    message=f"created {exact} but the site PUBLISHED it despite status=false "
                    f"(workflow default state?); unpublish it in Drupal. Not marked done",
                )
        except (httpx.TransportError, ConnectorError) as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        return WriteResult(True, True, target_id=u, message="created")

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """PATCH only the fields translated this run. JSON:API leaves every attribute a PATCH
        does not name untouched, so a held-back field keeps its stored translation. A translation
        that is already PUBLISHED (live) is not rewritten unless `edit_published` is on."""
        t, u = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        attrs, had_real = self._build_attrs(fields)
        if not had_real:
            return WriteResult(False, False, message=HELD_BACK)
        # `langcode` is sent on purpose (review gate 2026-10-10). With the module it is validated
        # against ?langCode= (a mismatch is a 422) and is a no-op on the translation. On a site
        # where the module vanished after the probe, core JSON:API PATCHes the DEFAULT
        # translation: setting its langcode to a language the entity already has throws in
        # ContentEntityBase::onChange ("A translation already exists"), or is a 403 without
        # field edit access, BEFORE save(). Either way nothing lands on the source.
        body = {"data": {"type": t, "id": u, "attributes": {**attrs, "langcode": exact}}}
        try:
            # fresh proof right before the write (the PATCH is the only call that could hit the
            # SOURCE entity if the module were missing)
            cur = self.get_translation(record_id, locale)
            if cur is ABSENT:
                return WriteResult(False, False, message=f"{exact} no longer exists; not updated")
            if cur.get("status") is not False and not self.edit_published():
                return WriteResult(
                    False,
                    False,
                    message=f"{LIVE_PREFIX} the {exact} translation is published (live) and "
                    f"was left alone; turn on 'Edit live translations' to rewrite it",
                )
            with self._client() as c:
                r = self._call(c, "PATCH", self._path(t, u), params={"langCode": exact}, body=body)
        except (httpx.TransportError, ConnectorError) as e:
            return WriteResult(False, False, message=f"{type(e).__name__}: {e}")
        bad = self._verify_write(r, record_id, locale)
        if bad:
            return WriteResult(False, False, message=bad)
        return WriteResult(True, False, target_id=u, message="updated")

    # --------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for ent in self.discover_articles():
            tf, title_text, body_units, _h, _p = self.extract(ent)
            aid = self._aid(ent)
            label = (title_text or aid)[:60]
            if tf and title_text:
                out.append(PulledItem(external_id=aid, field=tf, text=title_text, title=label))
            for u in body_units:
                out.append(PulledItem(external_id=aid, field=u.p_index, text=u.text, title=label))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Drupal writes per-language translations - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow: thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (no watermark): the content-hash gate is the change detector.
# ===========================================================================
def _drupal_preflight(ctx):
    return set(ctx.connector.enabled_locales()), {"source": ctx.connector._source().lower()}


def _drupal_partition(locales, enabled, extra):
    """usable = enabled targets minus the source language; rejected = not enabled. The source is
    DROPPED (not rejected) and reported honestly in the message."""
    src = (extra or {}).get("source", "")
    rejected = [x for x in locales if x not in enabled]
    usable = [x for x in locales if x in enabled and x != src]
    return rejected, usable


def _drupal_prepare(ctx, art):
    if getattr(ctx.connector, "_tripped", None):
        # a write already landed in the wrong language: no more MT spend, no more writes
        return flow.SkipArticle("halted_skipped")
    tf, title_text, body_units, chash, plans = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = ctx.connector._aid(art)
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={
            "article_id": aid,
            "title": title_text or None,
            "content_type": art.get("_type"),
        },
        doc_title=f"{(title_text or art.get('_type') or 'entity')[:50]} ({aid})",
        extra={"title_field": tf, "title_text": title_text, "plans": plans},
    )


def _drupal_is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _drupal_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one language: ONLY what was translated. A held-back title is
    omitted (never the source), and a formatted field is sent whole or not at all."""
    out: dict = {}
    tf = prep.extra["title_field"]
    if tf is not None and seg0_target is not None:
        out[tf] = seg0_target
    for plan in prep.extra["plans"]:
        v = field_payload(plan, mapping)
        if v is not None:
            out[plan["name"]] = v
    return out


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(HELD_BACK_PREFIX)


def _is_live(f):
    return str((f or {}).get("error", "")).startswith(LIVE_PREFIX)


def _live_kept(ctx) -> list:
    """Published translations left untouched this run, as {article_id, locale, error} rows like
    the other connectors' `live_kept`: the pairs parked before any MT spend plus any refused at
    the write by the same rule (an explicit retry bypasses the park hook)."""
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live(f)
    ]


def _live_left_alone(ctx) -> int:
    return len(_live_kept(ctx))


def _drupal_parked(connector, aid, loc, chash):
    """True = leave this pair alone BEFORE any machine translation is spent: its translation is
    already published (live) and the operator did not opt in to rewriting it. Unchanged pairs
    are left to the normal hash suppression. A probe error is not an answer: the write-time
    guard in update_translation decides."""
    try:
        if connector.edit_published():
            return False
        if (connector.config.get("drupal_hashes") or {}).get(f"{aid}:{loc}") == chash:
            return False
        cur = connector.get_translation(aid, loc)
    except Exception:
        return False
    live = cur is not ABSENT and cur.get("status") is not False
    if live:
        if not isinstance(getattr(connector, "_live_kept", None), list):
            connector._live_kept = []
        connector._live_kept.append(
            {
                "article_id": str(aid),
                "locale": str(loc),
                "error": f"{LIVE_PREFIX} the translation is published (live) and was left alone",
            }
        )
    return live


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f) and not _is_live(f)]


def _dropped_as_source(ctx):
    return [x for x in ctx.locales if x not in ctx.usable and x not in ctx.rejected]


def _drupal_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _drupal_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translation(s) across "
        f"{len(ctx.usable)} language(s) from {len(ctx.articles)} entity(ies)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} entity(ies) with no translatable fields."
        )
    other = getattr(ctx.connector, "other_language_skipped", 0)
    if other:
        parts.append(
            f"Skipped {other} entity(ies) whose original language is not the source language."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) translation(s) unchanged: {sample}. Turn on "
            f"'Edit live translations' to rewrite them."
        )
    tripped = getattr(ctx.connector, "_tripped", None)
    if tripped:
        halted = ctx.counters["halted_skipped"]
        parts.insert(
            0,
            "RUN HALTED: "
            + tripped
            + (f" {halted} later entity(ies) were not attempted." if halted else ""),
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) not enabled in Drupal: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source language "
            f"({', '.join(dropped)}) - configure different target languages."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (entity, language) pair(s) awaiting review "
            f"(NEEDS_REVIEW): a new translation is not created while any of its fields is "
            f"held, and an existing one keeps the held field's stored value. They will be "
            f"re-attempted next run."
        )
    sel = ctx.connector.sel.selection_summary()
    if sel:
        parts.append(sel)
    if getattr(ctx.connector, "_lang_state", "") == "configured":
        parts.append(
            "The site's language list was not readable with this user, so the configured "
            "languages were used unchecked."
        )
    real = _real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("translation write(s)").strip())
        log.warning(
            "drupal_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _drupal_build_result(ctx):
    return {
        "entities": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "live_left_alone": _live_left_alone(ctx),
        "halted_skipped": ctx.counters["halted_skipped"],
        "empty_skipped": ctx.counters["empty_skipped"],
        "other_language_skipped": getattr(ctx.connector, "other_language_skipped", 0),
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "fields_selected": ctx.connector.sel.selected_sorted(),
        "fields_skipped": ctx.connector.sel.skipped_dict(),
        "failures": _real_failures(ctx),
    } | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})


def drupal_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Drupal entities into one or more languages, each written back as the entity's
    own translation via the T2 SubResourceStrategy over JSON:API Multilingual. Thin wrapper over
    flow.run_translate_flow; poll-only with content-hash suppression and an empty-entity skip.
    only_pairs restricts the write loop to exactly those ('<type>:<uuid>', language) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="drupal",
        no_conn_msg="No such Drupal connection.",
        hash_key="drupal_hashes",
        probe_label="Drupal",
        preflight=_drupal_preflight,
        prepare=_drupal_prepare,
        is_suppressed=_drupal_is_suppressed,
        build_payload=_drupal_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_drupal_compute_ok,
        build_message=_drupal_build_message,
        build_result=_drupal_build_result,
        extra_counters=("empty_skipped", "parked_skipped", "halted_skipped"),
        parked=_drupal_parked,
        partition=_drupal_partition,
        held_prefix=HELD_BACK_PREFIX,
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
