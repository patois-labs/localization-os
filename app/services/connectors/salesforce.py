"""Salesforce Lightning Knowledge connector — the FIELD-MAPPING GENERALIZER build
(built from the vendor documentation).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports API_VERSION, _LOGIN_SANDBOX, _LOGIN_PROD, _JWT_GRANT,
_SF_ID_RE, _soql_lit, log, _sf_preflight, _sf_partition, _sf_prepare, _sf_is_suppressed,
_sf_build_payload, _sf_compute_ok, _sf_build_result, salesforce_rediscover_fields,
_sf_build_message, salesforce_translate from app.services.connectors.salesforce_common,
app.services.connectors.salesforce_part2.

Topology T2: a logical article is a set of `Knowledge__kav` version records that
share one `KnowledgeArticleId`; the master carries `IsMasterLanguage=true` and each
LANGUAGE is a separate translation version linked by that same KnowledgeArticleId.
Localizing locale X = creating/updating X's translation version. Write-back rides
the shared strategy.SubResourceStrategy (the SAME T2 seam as Zendesk/Zoho/
Freshdesk/Document360), with the create/update methods carrying Salesforce's
managed translation LIFECYCLE. This module owns only auth, endpoints, the field
shapes, and locale mapping; the reusable per-record-type extraction contract lives
in fieldmap.py (spec step 4 — the generalizer is deliberately NOT in this class).

PROVENANCE: every endpoint is [CONFIRMED] in salesforce_knowledge.md unless a
comment flags [UNCERTAIN] — those carry a guard + a documented fallback and are
listed in the connector's report for live-tenant QA. Several developer.salesforce
object-reference pages were JS-rendered, so exact field spellings / response shapes
of the managed translation actions are verify-in-sandbox.

FIVE HEADLINE GOTCHAS (research §7):

  (1) EXPLICIT LOCALE MAPPING IS MANDATORY (the #1 trap). Salesforce `Language`
      uses its OWN curated codes (`en_US`, `zh_CN`, `fr` — underscore, NOT BCP-47).
      We enumerate the org's ACTIVE Knowledge languages (GET knowledgeManagement/
      settings) and resolve each internal BCP-47 target against that active set;
      an unmapped/inactive locale is REJECTED (never guessed), because a wrong guess
      writes the wrong or an inactive language. The internal (BCP-47) form is the
      wire locale the flow threads; the connector maps it to the SF code only at the
      API boundary (the zendesk casing precedent — canonical rows vs wire codes).

  (2) RECORD-TYPE FIELD VARIANCE. Translatable body/custom fields differ per
      RecordTypeId; NEVER hardcode a body field name. Fields are DISCOVERED via
      Describe (a "rediscover fields" action) and persisted in Connection.config as
      sf_field_map; extraction routes each field by translatable type (fieldmap.py).

  (3) DRAFT/PUBLISH IS A MANAGED LIFECYCLE, not a free field write. You canNOT
      PATCH PublishStatus='Online' on the sObject. Per locale:
      submit-for-translation -> PATCH the Draft field values -> set-complete ->
      publish (managed action). Each step checks the prior; a mid-lifecycle failure
      leaves a recoverable Draft (never a corrupt/partial publish) and lands as a
      per-pair WriteResult failure (surfaced for review / retry_failed).

  (4) MASTER-LANGUAGE COUPLING. A translation is meaningless without its master; we
      translate FROM the published master (PublishStatus='Online' AND
      IsMasterLanguage=true) and correlate by KnowledgeArticleId (stable across
      languages).

  (5) HEADLESS AUTH, TWO MODES (2026-10 rebuild). New orgs can no longer create
      classic Connected Apps by default (Salesforce steers new integrations to
      External Client Apps), so a connection takes EITHER the OAuth2 client
      credentials flow (consumer key + consumer secret + the org's My Domain URL as
      the login URL; no certificate, the app's "run as" user acts) OR the JWT
      Bearer flow (consumer key + RSA private key + integration username).
      JWT-BEARER, HEADLESS: server-to-server, no user, no scopes-in-JWT.
      We sign the assertion, POST /services/oauth2/token, and TRUST
      the returned `instance_url` as the REST base (never hardcode a host — the Zoho
      api_domain discipline). No refresh token: re-mint on 401 (the Jira
      re-mint-before-write pattern).

CHANGE DETECTION: poll-first (research §5) + per-(article,locale) content-hash
suppression (the zoho/freshdesk gate). We deliberately declare NO watermark key
(the zoho/intercom precedent): Salesforce `LastModifiedDate` bumps on republish /
metadata too, so — honouring the CARDINAL edited_at-not-updated_at rule — the
source-content HASH is the sole re-translate gate, never a LastModifiedDate
watermark. CDC support for Knowledge__kav is [UNCERTAIN]; poll-only ships first.

FIRST BUILD TARGETS A SANDBOX (test.salesforce.com) — never a live prod org.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re
import time

import httpx

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
from app.services.connectors.fieldmap import extract_record, select_translatable_fields
from app.services.connectors.salesforce_common import (
    _JWT_GRANT,
    _LOGIN_PROD,
    _LOGIN_SANDBOX,
    _NEXT_URL_RE,
    _SF_ID_RE,
    API_VERSION,
    HELD_MARKER,
    _err_code,
    _sf_build_payload,
    _sf_build_result,
    _sf_compute_ok,
    _sf_is_suppressed,
    _sf_partition,
    _sf_preflight,
    _sf_prepare,
    _soql_lit,
    salesforce_rediscover_fields,
)
from app.services.connectors.salesforce_part2 import _sf_build_message, salesforce_translate
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult

__all__ = [
    "API_VERSION",
    "_JWT_GRANT",
    "_LOGIN_PROD",
    "_LOGIN_SANDBOX",
    "_SF_ID_RE",
    "_sf_build_message",
    "_sf_build_payload",
    "_sf_build_result",
    "_sf_compute_ok",
    "_sf_is_suppressed",
    "_sf_partition",
    "_sf_preflight",
    "_sf_prepare",
    "_soql_lit",
    "flow",
    "harvest",
    "re",
    "salesforce_rediscover_fields",
    "salesforce_translate",
]


SPEC = ConnectorSpec(
    kind="salesforce",
    name="Salesforce Knowledge",
    icon="☁",  # cloud
    cat="Knowledge base / help center",
    blurb="Translate Salesforce Lightning Knowledge articles into your target "
    "languages. Each language is written as the article's own translation "
    "version (submit-for-translation -> fill the Draft -> set-complete -> "
    "publish), correlated by KnowledgeArticleId, so the master article and "
    "other languages are never overwritten. Draft by default. Translatable "
    "fields are discovered per record type via Describe (rediscover fields).",
    config_fields=[
        FieldSpec(
            "login_url",
            "Login URL",
            "Salesforce OAuth login host. JWT mode: test.salesforce.com "
            "(sandbox), login.salesforce.com (production AND free Developer Edition) or "
            "your My Domain. Client-credentials mode: your My Domain URL "
            "(e.g. myorg.my.salesforce.com). Blank = sandbox.",
            required=False,
            placeholder="test.salesforce.com",
        ),
        FieldSpec(
            "username",
            "Integration username",
            "JWT mode only: the Salesforce username the JWT acts as "
            "(pre-authorized on the app, with a Knowledge User license + Manage "
            "Articles perms). Not used in client-credentials mode (the app's run-as user acts).",
            required=False,
            placeholder="svc@example.com.sandbox",
        ),
        FieldSpec(
            "record_type_id",
            "Record type id",
            "Limit discovery to one "
            "Knowledge record type (optional; blank lists every published "
            "master article).",
            required=False,
            placeholder="012xx0000000ABC",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The master/authoring locale "
            "(BCP-47, e.g. en-US). Never written as a target. Blank = en-US.",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated BCP-47 locales to write "
            "(must be ACTIVE Knowledge languages; an inactive/unmapped locale is rejected).",
            required=False,
            placeholder="de-DE, fr, zh-CN",
        ),
        FieldSpec(
            "publish",
            "Publish after write",
            "'1' = publish each translation "
            "(Draft -> Online via the managed action); '0' = leave it Draft "
            "(default, safe).",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "refresh_online",
            "Edit live translations",
            "'1' = when a translation is already Online (live) and the source changed, take it "
            "back to Draft and rewrite it. '0' (default) = leave live translations alone and "
            "report the pair as needing a manual edit.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "api_version",
            "API version",
            f"Salesforce REST API version to pin (default {API_VERSION}).",
            required=False,
            placeholder=API_VERSION,
        ),
        FieldSpec(
            "max_articles", "Max articles", "Safety cap per run.", required=False, placeholder="50"
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "client_id",
            "Connected-app consumer key",
            "The External Client App's (or connected app's) OAuth consumer key; "
            "the JWT issuer (iss) or the client-credentials client id.",
            secret=True,
            placeholder="3MVG9…",
        ),
        FieldSpec(
            "client_secret",
            "Consumer secret (client-credentials mode)",
            "The app's OAuth consumer secret. Fill this INSTEAD of the RSA private key "
            "to use the client-credentials flow (no certificate needed).",
            secret=True,
            required=False,
        ),
        FieldSpec(
            "private_key",
            "RSA private key (PEM, JWT mode)",
            "JWT mode only: the RSA private key (PEM) whose cert "
            "is uploaded to the app; signs the JWT assertion. Stored "
            "Fernet-encrypted.",
            secret=True,
            required=False,
            placeholder="-----BEGIN RSA PRIVATE KEY-----\n…",
        ),
    ],
    setup_steps=[
        "In Salesforce Setup, enable Lightning Knowledge (Knowledge Settings), add your "
        "languages under Multiple Languages and mark them Active, and give your user the "
        "Knowledge User checkbox.",
        "Create an External Client App (Setup > External Client App Manager; classic "
        "Connected Apps are off by default in new orgs). Enable OAuth with the 'api' scope, "
        "then EITHER turn on the Client Credentials flow and set a 'run as' user, OR turn on "
        "the JWT Bearer flow and upload an X.509 certificate whose RSA key you keep.",
        "Give the run-as (or JWT) user a Knowledge User license, the Manage Articles "
        "permission, translation rights and the record-type and data-category access it needs.",
        "Paste the consumer key plus EITHER the consumer secret (client credentials; set Login "
        "URL to your My Domain host) OR the RSA private key and the integration username (JWT; "
        "Login URL test.salesforce.com for a sandbox, login.salesforce.com for production or a "
        "free Developer Edition org).",
        "Click 'Rediscover fields' on the connection page to Describe the translatable "
        "fields per record type, then run on a schedule (Salesforce Knowledge has no "
        "confirmed webhook for Knowledge__kav, so change detection is poll-first).",
    ],
    other_side="Salesforce change detection polls published master articles and "
    "gates re-translation on a per-(article,locale) source-content hash "
    "(LastModifiedDate bumps on republish/metadata, so it never drives a "
    "watermark). Translatable fields are discovered per record type via "
    "Describe (rediscover fields). CDC for Knowledge__kav is unverified.",
    docs_url="https://developer.salesforce.com/docs/atlas.en-us.knowledge_dev.meta/knowledge_dev/",
    supports_oauth=False,  # JWT-bearer (server-to-server); no interactive authorize leg.
    # Un-archived in the connector parity rebuild (the §6b gate dropped once the connector met the
    # current standard; hermetic tests green, live QA is pending).
)


class SalesforceConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="salesforce",
        topology=Topology.T2,
        auth=(
            AuthKind.JWT,
            AuthKind.API_KEY,
        ),  # JWT Bearer or client credentials (key+secret); no 3LO leg
        api_version=API_VERSION,
        rich_text=RichText.HTML,  # Rich Text Area bodies -> the shared html filter
        regions="dynamic-from-token",  # instance_url from the token (never hardcoded)
        change_signals=ChangeSignals(
            webhook_sig=None,  # no confirmed Knowledge__kav webhook -> poll-first
            poll_field="LastModifiedDate",  # content signal (but NOT advanced as a watermark)
            stale_flag=None,  # no clean per-locale stale flag -> hash is the gate
            thin_payload=False,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # T2 strategy wired with Salesforce's discovery hooks. The strategy owns the
        # ABSENT-vs-present create/update decision + the D1 striped write lock; this
        # connector supplies get/create/update_translation (with the managed
        # lifecycle) below.
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        # per-instance caches (the connector is built once per run and reused for
        # every write, so the token + active-language fetches happen at most once).
        self._tok: tuple[str, str] | None = None  # (access_token, instance_url)
        self._active: set | None = None  # active SF Knowledge language codes
        self._rt_names: dict | None = None  # RecordTypeId -> Name (harvest, Phase 3B)

    # --- config helpers ---
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en-US").strip() or "en-US"

    def _api_version(self) -> str:
        return (self.config.get("api_version") or API_VERSION).strip() or API_VERSION

    def _login_url(self) -> str:
        raw = (self.config.get("login_url") or "").strip() or _LOGIN_SANDBOX
        raw = raw.rstrip("/")
        if raw.startswith("http://"):
            raw = "https://" + raw[len("http://") :]
        elif not raw.startswith("https://"):
            raw = "https://" + raw
        return _validate_target(raw)  # SSRF guard

    # --- locale mapping (research §7.1/7.2 — the MANDATORY explicit table) ---
    @staticmethod
    def _sf_to_loc(sf_code: str) -> str:
        """Salesforce Language code -> internal BCP-47 (underscore -> hyphen).
        en_US -> en-US, zh_CN -> zh-CN, fr -> fr."""
        return str(sf_code or "").replace("_", "-")

    def sf_lang_for(self, loc: str) -> str | None:
        """Resolve an internal BCP-47 target locale to the org's ACTIVE Salesforce
        Knowledge language code, or None when it is not an active/mappable language
        (-> the flow REJECTS it, never guesses — a wrong guess writes the wrong or an
        inactive language). Matches case-insensitively on the exact code first
        (en-US -> en_US), then the base language (en-US -> en)."""
        want = str(loc or "").strip().replace("-", "_").lower()
        if not want:
            return None
        active = self.active_languages()
        by_lower = {c.lower(): c for c in active}
        if want in by_lower:
            return by_lower[want]
        base = want.split("_")[0]
        if base in by_lower:
            return by_lower[base]
        return None

    def _require_sf_lang(self, loc: str) -> str:
        sf = self.sf_lang_for(loc)
        if not sf:
            raise ConnectorError(
                f"Locale {loc!r} is not an active Salesforce Knowledge language in "
                f"this org (active: {sorted(self.active_languages()) or 'none'})."
            )
        return sf

    # --- auth: JWT Bearer -> access_token + instance_url ---
    def _mint_token(self) -> tuple[str, str]:
        """Exchange credentials for an access token. Returns (access_token,
        instance_url). Two modes: client credentials (consumer key + secret, the
        app's run-as user acts) when a consumer secret is stored and no private key;
        else JWT Bearer. Trusts the org's own instance_url as the REST base (never
        hardcode a host)."""
        client_id = (self.secret.get("client_id") or "").strip()
        client_secret = (self.secret.get("client_secret") or "").strip()
        private_key = (self.secret.get("private_key") or "").strip()
        login = self._login_url()
        if client_secret and not private_key:
            if not client_id:
                raise ConnectorError("Salesforce client-credentials auth needs the consumer key.")
            form = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        else:
            import jwt  # PyJWT (already a dependency for OIDC id_token validation)

            username = (self.config.get("username") or "").strip()
            if not client_id or not username or not private_key:
                raise ConnectorError(
                    "Salesforce auth needs the consumer key plus EITHER the consumer "
                    "secret (client credentials) OR an integration username and an RSA "
                    "private key (JWT)."
                )
            claims = {
                "iss": client_id,
                "sub": username,
                "aud": login,
                "exp": int(time.time()) + 300,
            }
            try:
                assertion = jwt.encode(claims, private_key, algorithm="RS256")
            except Exception as e:
                raise ConnectorError(
                    f"Could not sign the Salesforce JWT -- check the RSA "
                    f"private key (PEM): {type(e).__name__}"
                ) from e
            form = {"grant_type": _JWT_GRANT, "assertion": assertion}
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                login + "/services/oauth2/token",
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                idempotent=False,
            )
        if r.status_code != 200:
            # #165: do not echo the raw upstream body into an operator-visible error.
            raise ConnectorError(f"Salesforce token endpoint returned {r.status_code}.")
        body = r.json() if r.content else {}
        token = (body.get("access_token") or "").strip()
        instance = (body.get("instance_url") or "").strip()
        if not token or not instance:
            raise ConnectorError("Salesforce token response missing access_token / instance_url.")
        return token, _validate_target(instance.rstrip("/"))  # SSRF guard on instance_url

    def _auth(self) -> tuple[str, str]:
        """Cached (access_token, instance_url); mints on first use."""
        if self._tok is None:
            self._tok = self._mint_token()
        return self._tok

    def _authed(
        self, method: str, path: str, *, idempotent: bool = True, _retry: bool = True, **kw
    ) -> httpx.Response:
        """A REST call to the org's instance, Bearer-authed. On a 401 (token expired
        mid-run — there is no refresh token) we RE-MINT once and retry (the Jira
        re-mint pattern). `path` is instance-relative (starts with /services/...)."""
        token, instance = self._auth()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        headers.update(kw.pop("headers", {}))
        with self._client() as c:
            r = self._request(
                c, method, instance + path, idempotent=idempotent, headers=headers, **kw
            )
        if r.status_code == 401 and _retry:
            self._tok = None  # force a re-mint
            return self._authed(method, path, idempotent=idempotent, _retry=False, **kw)
        return r

    def _data(self, path: str) -> str:
        return f"/services/data/{self._api_version()}{path}"

    # --- SOQL query transport ---
    def _query(self, soql: str, cap: int | None = None) -> list[dict]:
        """Run a SOQL query, following nextRecordsUrl pagination up to a bounded
        number of pages / an optional record cap."""
        out: list[dict] = []
        r = self._authed("GET", self._data("/query/"), params={"q": soql})
        for _ in range(50):  # bounded pages per run
            if r.status_code != 200:
                raise ConnectorError(f"Salesforce SOQL failed ({r.status_code}): {_err_code(r)}")
            body = r.json() or {}
            for row in self._keep_rows(body.get("records") or []):
                out.append(row)
                if cap and len(out) >= cap:
                    return out[:cap]
            nxt = body.get("nextRecordsUrl")
            if body.get("done") or not nxt:
                break
            # The cursor is instance-relative. Anything else (absolute URL, other path,
            # traversal) would let a hostile response steer an authed call: fail closed.
            if not _NEXT_URL_RE.match(str(nxt)):
                raise ConnectorError("Salesforce returned an unexpected nextRecordsUrl.")
            r = self._authed("GET", nxt)  # nextRecordsUrl is already instance-relative
        return out[:cap] if cap else out

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            self._auth()  # mint the token (proves JWT + connected-app pre-auth)
            r = self._authed("GET", self._data("/limits/"))
            if r.status_code == 200:
                langs = sorted(self.active_languages())
                return True, (
                    "Connected to Salesforce Knowledge"
                    + (
                        f" ({len(langs)} active language(s): "
                        f"{', '.join(langs[:8])}"
                        f"{'…' if len(langs) > 8 else ''})."
                        if langs
                        else " (no active Knowledge languages found)."
                    )
                )
            if r.status_code in (401, 403):
                return False, (
                    f"Salesforce rejected the request ({r.status_code}) — "
                    "check the connected-app pre-authorization + the "
                    "integration user's Knowledge / API access."
                )
            return False, f"Salesforce returned {r.status_code}: {_err_code(r)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: Knowledge record types (id + name).
        Best-effort — [] if Describe is unreachable."""
        out: list[dict] = []
        try:
            for rt in self._record_types():
                out.append(
                    {"id": rt["id"], "name": f"{rt['name']} (record type {rt['id']})", "tables": []}
                )
        except Exception:
            return out
        return out

    def _record_types(self) -> list[dict]:
        """Active, non-master Knowledge record types from the sObject describe."""
        r = self._authed("GET", self._data("/sobjects/Knowledge__kav/describe"))
        if r.status_code != 200:
            return []
        infos = (r.json() or {}).get("recordTypeInfos") or []
        out = []
        for info in infos:
            rtid = str(info.get("recordTypeId") or "")
            if not rtid or info.get("master") or info.get("available") is False:
                continue
            out.append({"id": rtid, "name": info.get("name") or rtid})
        return out

    def record_type_name(self, record_type_id) -> str | None:
        """Resolve a RecordTypeId -> its human RecordType Name for context harvest
        (SPEC-context-metadata Phase 3B -> content_kind). Built ONCE per connector
        instance from the Knowledge__kav describe's recordTypeInfos and cached
        (`_rt_names`), so a pull resolves every article's record type with NO
        per-article API call. Unlike _record_types (which filters to active,
        non-master pickable scopes), this maps EVERY record type — including the
        master — so any article's RecordTypeId resolves. Best-effort: a describe
        failure caches an empty map (harvest omits content_kind, never a wrong value).

        [UNCERTAIN — needs live-tenant QA]: confirm against a real org that
        recordTypeInfos carries `name` (the label) for the article's RecordTypeId; the
        DeveloperName is used as a fallback only."""
        rtid = str(record_type_id or "").strip()
        if not rtid:
            return None
        if self._rt_names is None:
            self._rt_names = {}
            try:
                r = self._authed("GET", self._data("/sobjects/Knowledge__kav/describe"))
                if r.status_code == 200:
                    for info in (r.json() or {}).get("recordTypeInfos") or []:
                        rid = str(info.get("recordTypeId") or "")
                        if rid:
                            self._rt_names[rid] = (
                                info.get("name") or info.get("developerName") or ""
                            )
            except Exception:
                self._rt_names = {}
        return self._rt_names.get(rtid) or None

    # --- discover: active languages (the MANDATORY locale gate) ---
    def active_languages(self) -> set:
        """The org's ACTIVE Knowledge languages (SF codes), from GET
        knowledgeManagement/settings (research §7.2). Cached per instance. Only
        active languages can receive translations, so this drives the target
        rejection. Handles both documented shapes defensively [UNCERTAIN which the
        tenant emits]: a `languages` LIST of {name/language/code, active} OR a MAP of
        {code: {active}}. On any failure returns an empty set (every target is then
        rejected loudly rather than guessed)."""
        if self._active is not None:
            return self._active
        active: set = set()
        try:
            r = self._authed("GET", self._data("/knowledgeManagement/settings"))
            if r.status_code == 200:
                body = r.json() or {}
                langs = body.get("languages")
                if isinstance(langs, dict):
                    for code, meta in langs.items():
                        if (isinstance(meta, dict) and meta.get("active")) or meta is True:
                            active.add(str(code))
                elif isinstance(langs, list):
                    for item in langs:
                        if not isinstance(item, dict):
                            continue
                        if item.get("active"):
                            code = (
                                item.get("name") or item.get("language") or item.get("code") or ""
                            )
                            if code:
                                active.add(str(code))
                # Always include the org default language if present (a master's own
                # language is active by definition).
                dflt = body.get("defaultLanguage")
                if dflt:
                    active.add(str(dflt))
        except Exception:
            active = set()
        self._active = active
        return active

    # --- discover: the field map (Describe -> the generalizer) ---
    def _describe_fields_raw(self) -> list[dict]:
        r = self._authed("GET", self._data("/sobjects/Knowledge__kav/describe"))
        if r.status_code != 200:
            raise ConnectorError(f"Salesforce Describe failed ({r.status_code}): {_err_code(r)}")
        return (r.json() or {}).get("fields") or []

    def discover_field_map(self) -> dict:
        """Describe Knowledge__kav, pick the translatable fields (fieldmap.
        select_translatable_fields — never hardcoded), and key them per record type.

        [UNCERTAIN — live-tenant QA] Salesforce record types gate a field's LAYOUT,
        not its existence on the object, and describeLayouts parsing is version-
        sensitive/JS-rendered, so we map EVERY record type to the object-level
        translatable set (a field not populated on a given article is simply empty
        and skipped at extraction). Per-layout narrowing is a documented future
        refinement. A "" bucket covers articles whose RecordTypeId is blank."""
        translatable = select_translatable_fields(self._describe_fields_raw())
        fmap: dict = {"": list(translatable)}
        for rt in self._record_types():
            fmap[rt["id"]] = list(translatable)
        return fmap

    def _field_map(self) -> dict:
        return dict((self.config or {}).get("sf_field_map") or {})

    def _fields_for(self, record_type_id) -> list[dict]:
        """The persisted translatable field specs for an article's record type,
        falling back to the "" bucket, then to just Title (a standard field that is
        always translatable) so a never-rediscovered connection still localizes
        titles rather than failing."""
        fmap = self._field_map()
        rtid = str(record_type_id or "")
        specs = fmap.get(rtid) or fmap.get("") or []
        if not specs:
            return [{"name": "Title", "type": "string", "label": "Title"}]
        return specs

    def _select_columns(self) -> list[str]:
        """The SOQL SELECT column set: the standard correlation/lifecycle columns +
        the UNION of every translatable field across all record types (all live on
        Knowledge__kav, so the union is safely queryable)."""
        cols = [
            "Id",
            "KnowledgeArticleId",
            "ArticleNumber",
            "Title",
            "Language",
            "PublishStatus",
            "IsMasterLanguage",
            "RecordTypeId",
            "LastModifiedDate",
        ]
        seen = {c.lower() for c in cols}
        for specs in self._field_map().values():
            for f in specs or []:
                name = f.get("name")
                if name and name.lower() not in seen:
                    cols.append(name)
                    seen.add(name.lower())
        return cols

    # --- discover: master articles to translate ---
    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Published master versions to translate: SELECT ... FROM Knowledge__kav
        WHERE PublishStatus='Online' AND IsMasterLanguage=true (research §2). When
        `article_ids` (KnowledgeArticleIds, from retry/webhook) is given, scope to
        them; else optionally to the configured record type."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cols = ", ".join(self._select_columns())
        where = ["PublishStatus = 'Online'", "IsMasterLanguage = true"]
        if article_ids:
            ids = [a for a in (str(x).strip() for x in article_ids) if _SF_ID_RE.match(a)]
            if not ids:
                return []
            joined = ", ".join(f"'{_soql_lit(i)}'" for i in ids)
            where.append(f"KnowledgeArticleId IN ({joined})")
        else:
            rt = (self.config.get("record_type_id") or "").strip()
            if rt and _SF_ID_RE.match(rt):
                where.append(f"RecordTypeId = '{_soql_lit(rt)}'")
        soql = (
            f"SELECT {cols} FROM Knowledge__kav WHERE " + " AND ".join(where) + f" LIMIT {int(cap)}"  # noqa: S608 -- SOQL; ids regex-checked + _soql_lit
        )
        return self._query(soql, cap=cap)

    def show_article(self, article_id: str) -> dict | None:
        rows = self.discover_articles(article_ids=[article_id])
        return rows[0] if rows else None

    # --- extract: translatable units (routes through the generalizer) ---
    def extract_units(self, article: dict, get_filter) -> tuple[list[dict], str]:
        """Per-field translation units for the generic pull view + the poll-
        suppression content hash, via fieldmap.extract_record on this article's
        record-type field specs. `get_filter` is app.filters.get_filter (the shared
        html filter is reused unchanged for rich-text fields)."""
        specs = self._fields_for(article.get("RecordTypeId"))
        ex = extract_record(article, specs, get_filter)
        units: list[dict] = []
        if ex.title_field is not None:
            units.append({"p_index": 0, "text": ex.title_text, "field": ex.title_field})
        for i, u in enumerate(ex.body_units):
            units.append({"p_index": 1 + i, "text": u.text, "field": str(u.p_index)})
        return units, ex.content_hash

    # --- write-back client (the SubResourceClient the T2 strategy drives) ---
    def get_translation(self, record_id: str, locale: str):
        """The EXPLICIT existence probe (not a content read) the create-vs-update
        decision needs: the translation version for (KnowledgeArticleId, SF language)
        -- a language with no version yet returns ABSENT.

        Taxonomy C: the write lands on the DRAFT, so the probe reads the Draft first,
        then the Online version. Two queries because SOQL on Knowledge__kav REQUIRES a
        PublishStatus filter (an unfiltered query is MALFORMED_QUERY). A Draft wins
        when both exist (it is the one the lifecycle edits)."""
        sf_lang = self._require_sf_lang(locale)
        if not _SF_ID_RE.match(str(record_id)):
            raise ConnectorError(f"Invalid KnowledgeArticleId {record_id!r}.")
        for status in ("Draft", "Online"):
            soql = (
                f"SELECT Id, PublishStatus, IsMasterLanguage, Language, VersionNumber "  # noqa: S608 -- SOQL; values via _soql_lit
                f"FROM Knowledge__kav WHERE KnowledgeArticleId = '{_soql_lit(record_id)}' "
                f"AND PublishStatus = '{status}' "
                f"AND Language = '{_soql_lit(sf_lang)}' AND IsMasterLanguage = false "
                f"ORDER BY VersionNumber DESC LIMIT 1"
            )
            rows = self._query(soql, cap=1)
            if rows:
                return rows[0]
        return ABSENT

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """No existing translation version for this language -> run the full managed
        lifecycle from a fresh submit-for-translation."""
        return self._lifecycle(record_id, locale, fields, existing=None, created=True)

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """A translation version already exists -> reuse its Draft (or re-submit to
        refresh an Online one to a new Draft), then fill / complete / publish."""
        try:
            existing = self.get_translation(record_id, locale)
        except ConnectorError as e:
            return WriteResult(False, False, message=str(e))
        existing = existing if existing is not ABSENT else None
        return self._lifecycle(record_id, locale, fields, existing=existing, created=False)

    # --- the managed translation lifecycle (research §4 Path A) ---
    def _lifecycle(
        self, record_id: str, locale: str, fields: dict, *, existing, created: bool
    ) -> WriteResult:
        """submit-for-translation -> PATCH the Draft field values -> set-complete ->
        publish (unless draft). Each step checks the prior; a mid-lifecycle failure
        returns a per-pair WriteResult failure and leaves a recoverable Draft (never
        a corrupt/partial publish) — the unit is then surfaced for review /
        retry_failed. NOTE: PublishStatus is NEVER PATCHed directly (gotcha #3)."""
        draft = bool(fields.get("draft", True))
        held = bool((fields or {}).get(HELD_MARKER))
        payload = {k: v for k, v in (fields or {}).items() if k not in ("draft", HELD_MARKER)}
        if not payload:
            # Review gate 2026-10-10: nothing translated (every unit held for review, #640).
            # A fresh submit-for-translation Draft is a SOURCE-language copy, so creating
            # one, or completing/publishing an existing one, would ship source text.
            return WriteResult(
                False,
                created,
                message="held-back: no translated field to write (awaiting review); not written",
            )
        try:
            sf_lang = self._require_sf_lang(locale)
        except ConnectorError as e:
            return WriteResult(False, created, message=str(e))

        # Step 1: ensure a Draft translation version exists.
        version_id, err = self._ensure_draft_version(record_id, sf_lang, existing)
        if err:
            return WriteResult(False, created, message=err)
        if not version_id:
            return WriteResult(
                False, created, message="no Draft translation version after submit-for-translation"
            )

        # Step 2: PATCH the translated field values into the Draft version.
        if payload:  # always true here (empty payload refused above); kept as a guard
            ok, msg = self._patch_version(version_id, payload)
            if not ok:
                if existing is None:
                    # A Draft made by THIS call still holds the SOURCE-language copy. Leaving
                    # it would make the next run see "variant exists", write only the usable
                    # fields and (publish=1) publish the held ones in the source language.
                    self._discard_fresh_draft(version_id)
                return WriteResult(
                    False,
                    created,
                    target_id=version_id,
                    message=f"Draft field PATCH failed: {msg} (Draft left for review)",
                )

        # Review gate 2026-10-10 (#640): some unit is held for review. The usable fields are in
        # the Draft, the held ones kept their stored value; never mark it complete or
        # publish it. The flow records no hash, so the next run finishes the pair.
        if held:
            return WriteResult(
                True, created, target_id=version_id, message="updated (held units left in Draft)"
            )

        # Step 3: mark the translation complete (moves it toward publishable).
        ok, msg = self._set_complete(version_id)
        if not ok:
            return WriteResult(
                False,
                created,
                target_id=version_id,
                message=f"set-complete failed: {msg} (Draft left for review)",
            )

        # Step 4: publish via the MANAGED action (Draft -> Online), unless draft mode.
        if not draft:
            ok, msg = self._publish(record_id, version_id)
            if not ok:
                return WriteResult(
                    False,
                    created,
                    target_id=version_id,
                    message=f"publish failed: {msg} (Draft left for review)",
                )

        return WriteResult(
            True, created, target_id=version_id, message="created" if created else "updated"
        )

    def _refresh_online(self) -> bool:
        return str((self.config or {}).get("refresh_online", "0")).strip() in ("1", "true", "yes")

    def _ensure_draft_version(self, record_id: str, sf_lang: str, existing):
        """Return (draft_version_id, error). Reuse an existing Draft version directly.
        An ONLINE translation is live content: it is only edited (through a Draft copy) when the
        operator opted in (`refresh_online`; the live version stays Online), otherwise the
        pair fails with a clear reason and the live translation is untouched. No
        translation yet -> submit for translation and locate the created Draft."""
        status = str((existing or {}).get("PublishStatus") or "")
        if existing and status == "Draft" and existing.get("Id"):
            return str(existing["Id"]), None
        if existing and existing.get("Id") and status == "Online":
            if not self._refresh_online():
                return None, (
                    "translation is Online (live); not overwritten. Set 'refresh_online' to 1 "
                    "to let the connector edit it, or edit it in Salesforce"
                )
            vid = str(existing["Id"])
            ok, msg = self._create_draft_from_online(vid)
            if not ok:
                return None, f"edit online translation failed: {msg}"
            draft_id = self._draft_version_id(record_id, sf_lang)
            if not draft_id:
                return None, "edit online translation: no Draft found after create-draft"
            return draft_id, None
        vid, err = self._submit_for_translation(record_id, sf_lang)
        if err:
            return None, err
        if not vid:
            vid = self._draft_version_id(record_id, sf_lang)
        return vid, None

    def _submit_for_translation(self, record_id: str, sf_lang: str):
        """POST knowledgeManagement/articleVersions/translations {articleId,
        assignments:[{language}]} — creates the Draft translation version (research
        §4 Path A step 1, v43+ assignments[] shape). Returns (version_id_or_None,
        error_or_None). [UNCERTAIN] the exact success response shape — it may return
        the new version id or a bare 200/204, so we read a best-effort id and fall
        back to a SOQL lookup (_draft_version_id) when absent."""
        body = {"articleId": record_id, "assignments": [{"language": sf_lang}]}
        r = self._authed(
            "POST",
            self._data("/knowledgeManagement/articleVersions/translations"),
            idempotent=False,
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code not in (200, 201, 204):
            return None, f"submit-for-translation -> {r.status_code}: {_err_code(r)}"
        vid = None
        try:
            data = r.json() if r.content else {}
            if isinstance(data, dict):
                vid = data.get("id") or data.get("versionId") or data.get("Id")
            elif isinstance(data, list) and data and isinstance(data[0], dict):
                vid = data[0].get("id") or data[0].get("versionId")
        except Exception:
            vid = None
        vid = str(vid) if vid else None
        if vid and not _SF_ID_RE.match(vid):
            return None, "submit-for-translation returned an unexpected version id"
        return vid, None

    def _draft_version_id(self, record_id: str, sf_lang: str) -> str | None:
        """SOQL the Draft translation version id for (article, language) — the
        fallback when submit-for-translation didn't return one."""
        soql = (
            f"SELECT Id FROM Knowledge__kav WHERE "  # noqa: S608 -- SOQL; values via _soql_lit
            f"KnowledgeArticleId = '{_soql_lit(record_id)}' "
            f"AND Language = '{_soql_lit(sf_lang)}' AND PublishStatus = 'Draft' "
            f"AND IsMasterLanguage = false ORDER BY VersionNumber DESC LIMIT 1"
        )
        rows = self._query(soql, cap=1)
        vid = str(rows[0]["Id"]) if rows and rows[0].get("Id") else None
        return vid if vid and _SF_ID_RE.match(vid) else None

    def _patch_version(self, version_id: str, payload: dict):
        """PATCH /sobjects/Knowledge__kav/{versionId} with the translated field
        values (standard sObject update on the Draft version). Idempotent (replace-
        in-place by id) -> retries on 5xx via base._request."""
        if not _SF_ID_RE.match(str(version_id)):
            return False, "invalid version id"
        try:
            r = self._authed(
                "PATCH",
                self._data(f"/sobjects/Knowledge__kav/{version_id}"),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201, 204):
            return True, "ok"
        return False, f"{r.status_code}: {_err_code(r)}"

    def _set_complete(self, version_id: str):
        """PATCH knowledgeManagement/articleVersions/translations/{versionId}
        {"complete":"true"} — returns 204 (research §4 Path A step 3)."""
        if not _SF_ID_RE.match(str(version_id)):
            return False, "invalid version id"
        try:
            r = self._authed(
                "PATCH",
                self._data(f"/knowledgeManagement/articleVersions/translations/{version_id}"),
                json={"complete": "true"},
                headers={"Content-Type": "application/json"},
            )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201, 204):
            return True, "ok"
        return False, f"{r.status_code}: {_err_code(r)}"

    def _invoke_action(self, name: str, inputs: dict):
        """POST /actions/standard/<name> {"inputs":[inputs]} (Lightning Knowledge
        invocable actions, API 44.0+; Salesforce Knowledge Developer Guide, "Invocable
        Actions for Lightning Knowledge"). The action answers HTTP 200 even when it
        FAILED, so isSuccess is checked. Returns (ok, message); the message carries
        error codes only. Not idempotent-retried on 5xx (a publish may have landed)."""
        try:
            r = self._authed(
                "POST",
                self._data(f"/actions/standard/{name}"),
                idempotent=False,
                json={"inputs": [inputs]},
                headers={"Content-Type": "application/json"},
            )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code not in (200, 201):
            return False, f"{r.status_code}: {_err_code(r)}"
        try:
            body = r.json()
        except Exception:
            return False, "unreadable action response"
        rows = body if isinstance(body, list) else [body]
        if rows and all(isinstance(x, dict) and x.get("isSuccess") is True for x in rows):
            return True, "ok"
        codes = []
        for x in rows:
            if isinstance(x, dict):
                for e in x.get("errors") or []:
                    if isinstance(e, dict):
                        codes.append(str(e.get("statusCode") or e.get("errorCode") or "")[:60])
        return False, ",".join(c for c in codes if c)[:120] or "action reported failure"

    def _create_draft_from_online(self, version_id: str):
        """createDraftFromOnlineKnowledgeArticle EDIT_AS_DRAFT_TRANSLATION with
        unpublish=false: the live translation STAYS ONLINE while a Draft copy is
        created (a failure later in the lifecycle never takes live content offline).
        The action does not return the new Draft id; the caller SOQLs it."""
        if not _SF_ID_RE.match(str(version_id)):
            return False, "invalid version id"
        return self._invoke_action(
            "createDraftFromOnlineKnowledgeArticle",
            {
                "action": "EDIT_AS_DRAFT_TRANSLATION",
                "unpublish": False,
                "articleVersionId": version_id,
            },
        )

    def _discard_fresh_draft(self, version_id: str) -> None:
        """Best-effort delete of a Draft this call just created (deleteKnowledgeArticles).
        Only ever called for a version id that submit-for-translation returned in the same
        call, never for an existing translation."""
        if _SF_ID_RE.match(str(version_id)):
            self._invoke_action("deleteKnowledgeArticles", {"articleVersionIdList": [version_id]})

    def _publish(self, record_id: str, version_id: str):
        """Publish the translation Draft -> Online with the DOCUMENTED invocable action
        publishKnowledgeArticles pubAction=PUBLISH_TRANSLATION. (The old POST to
        masterVersions published the MASTER. A PATCH of translations/{id}
        publishStatus=Online is NOT documented: the translations resource documents
        only complete/incomplete and publishStatus=draft.) A failure is a per-pair
        failure with the Draft intact and recoverable. Publish is OFF by default."""
        if not _SF_ID_RE.match(str(version_id)):
            return False, "invalid version id"
        return self._invoke_action(
            "publishKnowledgeArticles",
            {"articleVersionIdList": [version_id], "pubAction": "PUBLISH_TRANSLATION"},
        )

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, field-unit) for the generic pull view. The
        rich flow is store.salesforce_translate (writes per-locale translation
        versions)."""
        from app.filters import get_filter

        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h = self.extract_units(art, get_filter)
            for u in units:
                out.append(
                    PulledItem(
                        external_id=str(art.get("KnowledgeArticleId")),
                        field=f"{u['field']}:{u['p_index']}",
                        text=u["text"],
                        title=(art.get("Title") or "")[:60],
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Salesforce Knowledge writes per-locale translation versions — use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list published master articles for the
        caller to diff against the persisted per-(article,locale) content hash.
        Returns (articles, 0) — NO watermark is advanced (LastModifiedDate bumps on
        republish/metadata; the source-content hash in store.salesforce_translate is
        the actual gate — the zoho/intercom precedent)."""
        return self.discover_articles(), 0


import logging

from app.services.connectors import flow, harvest

log = logging.getLogger("loc_tms.connectors")
