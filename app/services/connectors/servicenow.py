"""ServiceNow Knowledge Management connector - a T3 (document-per-locale) consumer of the
connector framework (connector parity, factory NEW kind).

BUILT FROM SERVICENOW VENDOR DOCS ONLY. bb-io/ServiceNow (MIT, licence re-verified verbatim
2026-10-10) was deliberately NOT read, so there is no THIRD_PARTY_NOTICES entry.

TOPOLOGY - T3 with a NATIVE link, RE-DERIVED from the data model (not trusted from the
research summary): with the I18N Knowledge Management plugin (com.glideapp.knowledge.i18n2)
every language of an article is its OWN `kb_knowledge` row (own sys_id, number, workflow
state, version) carrying a `language` code and a `parent` reference to the base-language
article. There is no field-level or sub-resource localization. So write-back rides the
vendor-neutral `DocPerLocaleStrategy` + `idmap` reserve-then-fill + 7-case reconciliation
(the Notion / Help Scout / WordPress lifecycle). OUR id map stays the source of truth;
`parent` is (a) written at create so ServiceNow's own "Translated Versions" list shows the
sibling, and (b) the rule-6 corroboration probe (`find_preexisting`: a row with
parent=<source> and language=<target> that our map never created is flagged needs_link,
never overwritten).

API - REST Table API only (`/api/now/table/kb_knowledge`, GET list/get, POST insert, PATCH
partial update). Rate limits are per-instance admin rules answering 429 (+ Retry-After),
which `Connector._request` already backs off on. Auth: Basic (integration user) or OAuth
client credentials (`/oauth_token.do`, token held in memory only, minted again on a 401).

WORKFLOW STATES ARE RESPECTED (the part the Table API happily lets you ignore):
  * SOURCES: only `workflow_state=published` articles of the CONFIGURED knowledge base are
    read (a draft or retired article is not source-of-truth content).
  * CREATE: translations are inserted with an explicit `workflow_state=draft` and are never
    published by this connector; the operator publishes through ServiceNow's own workflow.
  * UPDATE: a target is edited in place only while it is `draft`. A target a human has
    submitted for review, published, retired or outdated is HELD (not edited): editing a
    live article through the Table API would bypass ServiceNow's versioning (the UI makes a
    new version; the API cannot). `update_published=1` opts in to editing PUBLISHED
    translations in place: a Table API PATCH to a published row goes LIVE at once, with no new
    version and no review (REVIEWED: community reports of instant publish on direct updates;
    the option is labelled as live editing). A held pair is PARKED (no MT re-spend every run)
    until the source hash changes or an operator retries it.

THE REVIEWED DEFECT CLASSES (CONNECTOR-BUILD-BLUEPRINT s3):
  A  every id is a bare 32-hex sys_id; knowledge_base / kb_category are 32-hex; language codes
     are a strict pattern; all of them are the ONLY strings that reach an encoded query, so
     `^`, `NQ`, `OR` injection is impossible. A sys_id outside the configured knowledge base
     answers exactly like a missing one.
  B  a withheld (NEEDS_REVIEW / refused) title or body is OMITTED from the PATCH entirely
     (the Table API PATCH is a partial update), so the stored translation survives; a held
     unit blocks CREATE altogether (flow #640), so no mixed-language article is ever made.
  C  probe, verify and write all address the same row by sys_id with workflow_state read
     back, so draft rows are visible; second run over unchanged source is fully skipped.
  D  non-HTML (`article_type=wiki`) articles are skipped LOUDLY with a count, never mangled.
  E  one knowledge base per connection, so a single cap; the test asserts the cap, not a
     distribution across containers.
  F  a pair written with a held unit records no content hash; a held-by-workflow pair is
     parked, never marked done.

DELIBERATELY OUT OF SCOPE: knowledge-article attachments, article versions/checkout, the
`kb_category` tree (a translation inherits the source's category sys_id), Translated-Versions
reconciliation beyond the rule-6 probe, and the sn_km_api Knowledge API. Live QA is
Needs live-tenant QA.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import quote

import httpx

from app.services.connectors import flow, harvest, idmap, webhooks  # noqa: F401
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
from app.services.connectors.strategy import DocPerLocaleStrategy, TargetUnverifiable, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "table-v2"
_TABLE = "kb_knowledge"
# Every sys_id that reaches a URL path or an encoded query: bare 32 hex, nothing else.
_SYSID_RE = re.compile(r"[0-9a-f]{32}")
# ServiceNow language ids: "en", "fr", "pt-BR", "zh_CN". Strict so it can never carry a `^`.
# Article numbers (KB0010001, custom prefixes allowed) are used as dict keys and, after this
# check, in one encoded query (explicit-id resolution): letters and digits only.
_NUM_RE = re.compile(r"[A-Za-z0-9]{3,32}")
_LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8}){0,2}")
_FIELDS = (
    "sys_id,number,short_description,text,language,parent,kb_knowledge_base,kb_category,"
    "workflow_state,article_type,sys_updated_on"
)
_PAGE = 50
# Workflow states a target may be edited in. Everything else is held (see module docstring).
_EDIT_STATES = frozenset({"draft"})
# "outdated" is a SUPERSEDED version row (not the live one): a PATCH to it succeeds and changes
# nothing a reader sees, so it is never an edit target, even with update_published=1.
_EDIT_STATES_PUBLISHED = frozenset({"draft", "published"})


def content_hash(*parts) -> str:
    """Stable per-article source-content hash (title + HTML body): the sole re-translate
    gate. sys_updated_on is touched by any edit or workflow move, so it drives nothing."""
    h = hashlib.sha256()
    for p in parts:
        h.update((str(p) if p is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def _ref_value(v) -> str:
    """A reference field is {"link","value"} by default and a bare sys_id with
    sysparm_exclude_reference_link=true; accept both, never crash on null."""
    if isinstance(v, dict):
        return str(v.get("value") or "").strip().lower()
    return str(v or "").strip().lower()


def _norm_lang(s) -> str:
    return str(s or "").strip().lower().replace("_", "-")


class _Unit:
    """Minimal flow translation unit (the engine reads only .text and .p_index)."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


SPEC = ConnectorSpec(
    kind="servicenow",
    name="ServiceNow",
    icon="\U0001f4d8",
    cat="Knowledge Base",
    blurb="Translate ServiceNow Knowledge Management articles into any language. Each "
    "translation is created as a separate kb_knowledge record in the target language, linked "
    "to the source article through ServiceNow's native Parent field, and left in Draft so "
    "your publish workflow still applies. Only published source articles in the configured "
    "knowledge base are read. Live QA pending - see the runbook.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Instance URL",
            "Your ServiceNow instance, e.g. https://dev12345.service-now.com. The connector "
            "calls /api/now/table on it.",
            placeholder="https://dev12345.service-now.com",
        ),
        FieldSpec(
            "knowledge_base",
            "Knowledge base sys_id",
            "The 32-character sys_id of the knowledge base to translate (open the knowledge "
            "base record, right-click the header, Copy sys_id). It is the scope fence: other "
            "knowledge bases are never read or written.",
            placeholder="0123456789abcdef0123456789abcdef",
        ),
        FieldSpec(
            "source_locale",
            "Source language",
            "Language code of the source articles, as ServiceNow stores it "
            "(kb_knowledge.language), e.g. en. Never written as a target. Blank = en.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target languages",
            "Comma-separated ServiceNow language codes to create, e.g. fr, de, es. Each must "
            "be an active language on the instance.",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "kb_category",
            "Category sys_id",
            "Optional: only translate articles in this category (sys_id).",
            required=False,
        ),
        FieldSpec(
            "update_published",
            "Edit LIVE (published) translations",
            "0 (default) = a translation that has been published is never edited (the pair "
            "is held; make a new version in ServiceNow). 1 = DANGER: overwrite published "
            "translations in place. The change goes live at once, with no new version and "
            "no review step. Only an admin can turn it on.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "max_articles",
            "Max articles",
            "Safety cap on source articles scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "username",
            "Username",
            "Integration user (Basic auth). Needs the snc_platform_rest_api_access and "
            "knowledge roles (knowledge_admin to write to a locked knowledge base).",
            required=False,
            secret=True,
            placeholder="svc_locos",
        ),
        FieldSpec(
            "password",
            "Password",
            "The integration user's password (Basic auth).",
            required=False,
            secret=True,
        ),
        FieldSpec(
            "client_id",
            "OAuth client ID",
            "Alternative to Basic: the Client ID of a System OAuth > Application Registry "
            "entry using the client credentials grant.",
            required=False,
            secret=True,
        ),
        FieldSpec(
            "client_secret",
            "OAuth client secret",
            "The Client Secret of that registry entry.",
            required=False,
            secret=True,
        ),
    ],
    setup_steps=[
        "Create an integration user (User Administration > Users) with the roles "
        "snc_platform_rest_api_access and knowledge (add knowledge_admin if your knowledge "
        "base restricts contributors). Give it a strong password.",
        "Install the I18N Knowledge Management plugin (com.glideapp.knowledge.i18n2) and "
        "activate each language you will translate into. This gives kb_knowledge its "
        "Language and Parent fields.",
        "Open your knowledge base record, copy its sys_id into Knowledge base sys_id, then "
        "enter the Instance URL, username and password here.",
        "Or, instead of a user, register an OAuth client (System OAuth > Application "
        "Registry > New inbound OAuth) with the client credentials grant and enter its "
        "Client ID and Secret.",
        "ServiceNow has no content webhook for this connector, so run on a schedule (Every "
        "N minutes); the source-content hash keeps translations fresh. Translations land in "
        "Draft: publish them through your normal workflow.",
    ],
    other_side="Change detection is poll-based and gated on a per-(article, language) "
    "source-content hash. Each translation is a separate Draft kb_knowledge record in the "
    "target language, linked to the source through the Parent field and tracked in the "
    "connector's id map. A translation that already exists in ServiceNow for a language is "
    "never overwritten (it is flagged for review), and a translation that has been "
    "published is never edited in place unless you opt in.",
    docs_url="https://www.servicenow.com/docs/bundle/zurich-api-reference/page/integrate/"
    "inbound-rest/concept/c_TableAPI.html",
    supports_oauth=False,
)


class ServiceNowConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="servicenow",
        topology=Topology.T3,
        auth=(AuthKind.BASIC, AuthKind.API_KEY),
        api_version=API_VERSION,
        rich_text=RichText.HTML,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="sys_updated_on", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = DocPerLocaleStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        self._token: str | None = None  # OAuth bearer, memory only, never logged
        self._schema: dict | None = None  # {"language": bool|None, "parent": bool|None}
        self._langs_cache: list | None = None
        # Per-run operator-visible accumulators (build_message / build_result).
        self._skips: dict = {}  # reason -> count (taxonomy D: every skip is reported)
        self._superseded: list = []  # targets whose human edits were overwritten
        self._link_unconfirmed: list = []  # created rows ServiceNow did not link by Parent
        self._parks: dict = {}  # "aid:loc" -> reason (held by workflow, parked)
        self._chash_by_aid: dict = {}
        self._written_delta: dict = {}
        self._created_hash: dict = {}  # target sys_id -> hash of what create already sent
        self._nl_parked = 0  # pairs skipped this run because a needs_link row is open
        self._num_delta: dict = {}  # article number -> first-seen source sys_id (new this run)
        self._live_by_ref: dict = {}  # stable ref -> sys_id of the live (Published) source row
        self._live_edited: list = []  # PUBLISHED translations PATCHed this run (went live)

    # ------------------------------------------------------------ config helpers
    def _hex_cfg(self, key: str, label: str, *, required: bool) -> str:
        raw = str(self.config.get(key) or "").strip().lower()
        if not raw:
            if required:
                raise ConnectorError(f"ServiceNow {label} is required (a 32-character sys_id).")
            return ""
        if not _SYSID_RE.fullmatch(raw):
            raise ConnectorError(f"Invalid {label} {raw[:40]!r}: must be a 32-character sys_id.")
        return raw

    def _kb(self) -> str:
        return self._hex_cfg("knowledge_base", "knowledge base", required=True)

    def _category(self) -> str:
        return self._hex_cfg("kb_category", "category", required=False)

    def _lang_cfg(self, value, label: str) -> str:
        v = str(value or "").strip()
        if not _LANG_RE.fullmatch(v):
            raise ConnectorError(f"Invalid {label} language code {v[:20]!r}.")
        return v

    def _source_locale(self) -> str:
        v = str(self.config.get("source_locale") or "").strip() or "en"
        return self._lang_cfg(v, "source").lower()

    def _update_published(self) -> bool:
        return str(self.config.get("update_published", "0")).strip().lower() in (
            "1",
            "true",
            "yes",
        )

    def _edit_states(self) -> frozenset:
        return _EDIT_STATES_PUBLISHED if self._update_published() else _EDIT_STATES

    def _split_id(self, raw) -> str:
        """Taxonomy A: a caller-supplied article id is a bare lowercase-or-uppercase 32-hex
        sys_id, nothing else (no path, query, whitespace, unicode, NUL, CR/LF)."""
        sid = str(raw or "").strip().lower()
        if not _SYSID_RE.fullmatch(sid):
            raise ConnectorError(
                f"Unsafe article id {str(raw)[:40]!r}: must be a 32-character sys_id."
            )
        return sid

    # -------------------------------------------------------------- auth / http
    def _base(self) -> str:
        raw = str(self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "ServiceNow Instance URL is required (e.g. https://dev12345.service-now.com)."
            )
        if not raw.lower().startswith("https://"):
            # Basic credentials / OAuth secrets go on this wire: never over cleartext http.
            raise ConnectorError("ServiceNow Instance URL must start with https://.")
        return _validate_target(raw).rstrip("/")

    def _table_url(self, *segments) -> str:
        tail = "/".join(quote(str(s), safe="") for s in (_TABLE, *segments))
        return f"{self._base()}/api/now/table/{tail}"

    def _auth_mode(self) -> str:
        if (self.secret.get("username") or "").strip() and (
            self.secret.get("password") or ""
        ).strip():
            return "basic"
        if (self.secret.get("client_id") or "").strip() and (
            self.secret.get("client_secret") or ""
        ).strip():
            return "oauth"
        raise ConnectorError(
            "ServiceNow credentials are required: a username and password, or an OAuth "
            "client ID and secret."
        )

    def _mint_token(self, c) -> str:
        r = self._request(
            c,
            "POST",
            f"{self._base()}/oauth_token.do",
            headers={"Accept": "application/json"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.secret.get("client_id", "").strip(),
                "client_secret": self.secret.get("client_secret", "").strip(),
            },
        )
        if r.status_code != 200:
            raise ConnectorError(
                f"ServiceNow rejected the OAuth client credentials ({r.status_code}); check the "
                "client ID and secret and that the client credentials grant is enabled."
            )
        try:
            tok = str((r.json() or {}).get("access_token") or "").strip()
        except ValueError:
            tok = ""
        if not tok:
            raise ConnectorError("ServiceNow OAuth token response carried no access_token.")
        self._token = tok
        return tok

    def _auth_headers(self, c) -> dict:
        if self._auth_mode() == "basic":
            import base64

            raw = f"{self.secret['username'].strip()}:{self.secret['password']}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {"Authorization": "Bearer " + (self._token or self._mint_token(c))}

    def _req(self, c, method: str, url: str, **kw):
        """One authed Table API call through the shared retrying base._request. An OAuth 401
        re-mints the token once (an expired bearer), nothing else retries auth."""
        idem = kw.pop("idempotent", True)
        for attempt in (0, 1):
            headers = {"Accept": "application/json", **self._auth_headers(c)}
            r = self._request(c, method, url, idempotent=idem, headers=headers, **kw)
            if r.status_code == 401 and self._auth_mode() == "oauth" and attempt == 0:
                self._token = None
                continue
            return r
        return r

    @staticmethod
    def _err(r) -> str:
        try:
            e = (r.json() or {}).get("error") or {}
            msg = e.get("message") or ""
        except (ValueError, AttributeError):
            msg = ""
        return vendor_excerpt(str(msg) or (r.text or ""))

    def _result(self, r, what: str):
        """Decode a Table API success body: {"result": ...}."""
        try:
            body = r.json()
        except ValueError as e:
            raise ConnectorError(f"ServiceNow {what} returned a non-JSON body.") from e
        return body.get("result") if isinstance(body, dict) else None

    def _fail(self, r, what: str) -> ConnectorError:
        if r.status_code == 401:
            return ConnectorError(
                f"ServiceNow rejected the credentials ({what}); check the user/password or "
                "OAuth client."
            )
        if r.status_code == 403:
            return ConnectorError(
                f"ServiceNow denied access ({what}): the integration user lacks the "
                "snc_platform_rest_api_access / knowledge role or an ACL blocks the table."
            )
        return ConnectorError(f"ServiceNow {what} -> {r.status_code}: {self._err(r)}")

    # --------------------------------------------------------- records / schema
    def _get_row(self, c, sid: str, fields: str = _FIELDS) -> dict | None:
        r = self._req(
            c,
            "GET",
            self._table_url(sid),
            params={"sysparm_fields": fields, "sysparm_exclude_reference_link": "true"},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise self._fail(r, f"get {sid[:8]}")
        res = self._result(r, "get")
        return res if isinstance(res, dict) else None

    def _paginate(self, c, query: str, cap: int, accept, fields: str = _FIELDS) -> list:
        """List kb_knowledge rows with sysparm_limit/offset until a short page. `accept`
        filters rows client-side; offset advances by RAW rows so a dropped row never shifts
        the window. Oversize rows are dropped by _keep_rows (budget) before buffering."""
        out: list = []
        offset = 0
        limit = _PAGE
        for _ in range(100000):
            if len(out) >= cap:
                break
            params = {
                "sysparm_query": query,
                "sysparm_fields": fields,
                "sysparm_limit": limit,
                "sysparm_offset": offset,
                "sysparm_exclude_reference_link": "true",
                "sysparm_display_value": "false",
                "sysparm_no_count": "true",
            }
            r = self._get_page(
                lambda **kw: self._req(c, "GET", self._table_url(), **kw),
                params=params,
                size_key="sysparm_limit",
            )
            limit = int(params["sysparm_limit"])
            if r.status_code != 200:
                raise self._fail(r, "list articles")
            rows = self._result(r, "list") or []
            if not isinstance(rows, list) or not rows:
                break
            offset += len(rows)
            out.extend(x for x in self._keep_rows(rows) if accept(x))
            if len(rows) < limit:
                break
        return out[:cap]

    def _check_schema(self, c) -> dict:
        """The Language / Parent fields only exist with the I18N plugin. The Table API
        silently ignores unknown fields on insert, so without this check we would create
        English-looking duplicates. Probe one row: an existing-but-empty field comes back as
        "", an absent one is omitted. No rows -> unknown (nothing to translate anyway)."""
        if self._schema is not None:
            return self._schema
        r = self._req(
            c,
            "GET",
            self._table_url(),
            params={
                "sysparm_query": f"kb_knowledge_base={self._kb()}",
                "sysparm_fields": "sys_id,language,parent",
                "sysparm_limit": 1,
                "sysparm_exclude_reference_link": "true",
            },
        )
        if r.status_code != 200:
            raise self._fail(r, "read knowledge articles")
        rows = self._result(r, "probe") or []
        if rows and isinstance(rows[0], dict):
            row = rows[0]
            if "language" not in row:
                raise ConnectorError(
                    "kb_knowledge has no Language field on this instance: install and activate "
                    "the I18N Knowledge Management plugin (com.glideapp.knowledge.i18n2) "
                    "before translating."
                )
            self._schema = {"language": True, "parent": "parent" in row}
        else:
            self._schema = {"language": None, "parent": None}
        return self._schema

    def link_mode(self) -> str:
        s = self._schema or {}
        if s.get("parent") is True:
            return "native"
        if s.get("parent") is False:
            return "absent"
        return "unknown"

    @staticmethod
    def link_label(mode: str) -> str:
        return {
            "native": "Parent link available - translations are linked to the source article.",
            "absent": "No Parent field on kb_knowledge - translations are tracked only by the "
            "connector's id map and are NOT linked to the source inside ServiceNow.",
            "unknown": "Parent link not verified (the knowledge base has no articles yet).",
        }.get(mode, mode)

    def _languages(self, c) -> list:
        if self._langs_cache is not None:
            return self._langs_cache
        out: list = []
        try:
            r = self._req(
                c,
                "GET",
                f"{self._base()}/api/now/table/sys_language",
                params={
                    "sysparm_query": "active=true",
                    "sysparm_fields": "id,name",
                    "sysparm_limit": 200,
                },
            )
            if r.status_code == 200:
                for x in self._result(r, "languages") or []:
                    i = str((x or {}).get("id") or "").strip()
                    if i and _LANG_RE.fullmatch(i):
                        out.append(i)
        except (httpx.TransportError, ConnectorError):
            out = []
        self._langs_cache = out
        return out

    def enabled_locales(self) -> list[str]:
        """Active ServiceNow language ids, in both '-' and '_' spellings, lowercased. []
        when the list is unreadable (the run preflight then treats targets as writable and
        says so)."""
        try:
            with self._client() as c:
                langs = self._languages(c)
        except (httpx.TransportError, ConnectorError):
            return []
        out: list = []
        for i in langs:
            for v in (i.lower(), i.lower().replace("-", "_"), _norm_lang(i)):
                if v not in out:
                    out.append(v)
        return out

    def _lang_code(self, locale: str) -> str:
        """The exact ServiceNow language id for an internal locale (case/separator-insensitive
        match against the active languages; the strict pattern otherwise)."""
        want = _norm_lang(locale)
        for i in self._langs_cache or []:
            if _norm_lang(i) == want:
                return i
        return self._lang_cfg(str(locale).strip(), "target")

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            kb = self._kb()
            with self._client() as c:
                r = self._req(
                    c,
                    "GET",
                    f"{self._base()}/api/now/table/kb_knowledge_base/{kb}",
                    params={
                        "sysparm_fields": "sys_id,title",
                        "sysparm_exclude_reference_link": "true",
                    },
                )
                if r.status_code == 404:
                    return False, (
                        "Connected, but the knowledge base was not found or is not readable "
                        "by this user (check the Knowledge base sys_id and the user's roles)."
                    )
                if r.status_code != 200:
                    return False, str(self._fail(r, "knowledge base lookup"))
                title = str((self._result(r, "kb") or {}).get("title") or "")
                self._check_schema(c)
            return True, (
                f"Connected to ServiceNow knowledge base {title[:60]!r}. "
                f"{self.link_label(self.link_mode())}"
            )
        except ConnectorError as e:
            return False, str(e)
        except httpx.TransportError as e:
            return False, f"ServiceNow unreachable: {type(e).__name__}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            kb = self._kb()
            with self._client() as c:
                r = self._req(
                    c,
                    "GET",
                    f"{self._base()}/api/now/table/kb_knowledge_base/{kb}",
                    params={
                        "sysparm_fields": "sys_id,title",
                        "sysparm_exclude_reference_link": "true",
                    },
                )
                if r.status_code != 200:
                    return []
                title = str((self._result(r, "kb") or {}).get("title") or kb)
            return [{"id": kb, "name": title, "tables": []}]
        except Exception:
            return []

    # ------------------------------------------------------------- discovery
    def _own_target_ids(self) -> set:
        if self.connection_id is None:
            return set()
        try:
            return {
                str(r.target_id).lower()
                for r in idmap.list_for_connection(self.connection_id)
                if r.target_id
            }
        except Exception:
            return set()

    def _skip(self, reason: str) -> None:
        self._skips[reason] = self._skips.get(reason, 0) + 1

    def _stable_ref(self, sid: str, number: str) -> str:
        """The source identity the id map is keyed by. ServiceNow KEEPS the article number but
        mints a NEW kb_knowledge row (new sys_id) every time a published article is edited
        into a new version, so the sys_id of the live row is NOT stable: keyed by it, every
        source edit would look like a brand-new article and get a second Draft translation.
        The first sys_id seen for a number is the identity from then on."""
        if not number or not _NUM_RE.fullmatch(number):
            return sid
        known = (self.config or {}).get("servicenow_numbers") or {}
        first = str(self._num_delta.get(number) or known.get(number) or "").strip().lower()
        if _SYSID_RE.fullmatch(first):
            return first
        self._num_delta[number] = sid
        return sid

    def _persist_numbers(self) -> None:
        if not self._num_delta or self.connection_id is None:
            return
        try:
            from app.services.connectors import store

            store.patch_config_nested(
                self.connection_id, {"servicenow_numbers": {"add": dict(self._num_delta)}}
            )
        except Exception:
            log.warning("servicenow connection %s: could not persist numbers", self.connection_id)

    def _current_published(self, c, row):
        """An explicit id may name a superseded version (an `outdated` row, e.g. the stable
        ref of an article that was re-versioned) or a Draft revision. Resolve it to the one
        Published row with the same number in the configured knowledge base, if any. Only a
        row already inside the knowledge base triggers the lookup (isolation)."""
        if not isinstance(row, dict):
            return row
        if str(row.get("workflow_state") or "").strip().lower() == "published":
            return row
        number = str(row.get("number") or "").strip()
        if _ref_value(row.get("kb_knowledge_base")) != self._kb() or not _NUM_RE.fullmatch(number):
            return row
        found = self._paginate(
            c,
            f"kb_knowledge_base={self._kb()}^number={number}^workflow_state=published",
            2,
            accept=lambda r: True,
        )
        return found[0] if found else row

    def _source_record(self, row, *, explicit: bool, own: set) -> dict | None:
        """A raw row -> our record, or None (counted in self._skips). The explicit-id path
        re-checks everything the list query filters server-side, and a row outside the
        configured knowledge base is indistinguishable from a missing one."""
        if not isinstance(row, dict):
            self._skip("not_found")
            return None
        sid = str(row.get("sys_id") or "").strip().lower()
        if not _SYSID_RE.fullmatch(sid):
            self._skip("not_found")
            return None
        if _ref_value(row.get("kb_knowledge_base")) != self._kb():
            self._skip("not_found")  # same bucket as a missing id (isolation)
            return None
        if sid in own:
            self._skip("own_translation")
            return None
        if _ref_value(row.get("parent")):
            self._skip("is_translation")  # a translated version is never a source
            return None
        lang = _norm_lang(row.get("language"))
        if lang and lang != _norm_lang(self._source_locale()):
            self._skip("other_language")
            return None
        # Defense in depth: re-checked on the LIST path too, so a server that binds `^OR`
        # differently than assumed (MANDATORY-3) can never feed a Draft or another category in.
        cat = self._category()
        if cat and _ref_value(row.get("kb_category")) != cat:
            self._skip("not_found")
            return None
        if str(row.get("workflow_state") or "").strip().lower() != "published":
            self._skip("unpublished")
            return None
        if str(row.get("article_type") or "html").strip().lower() == "wiki":
            self._skip("wiki_unsupported")
            return None
        number = str(row.get("number") or "").strip()
        ref = self._stable_ref(sid, number)
        self._live_by_ref[ref] = sid
        return {
            "ref": ref,
            "id": sid,
            "number": number,
            "title": str(row.get("short_description") or ""),
            "content": str(row.get("text") or ""),
            "lang": lang,
            "kb": self._kb(),
            "category": _ref_value(row.get("kb_category")),
            "state": str(row.get("workflow_state") or "").strip().lower(),
            "sys_updated_on": str(row.get("sys_updated_on") or ""),
        }

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Published source articles of the configured knowledge base (language = source or
        unset), minus translations and our own targets. Explicit ids are scope-validated
        (taxonomy A) and re-checked against the same rules."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        kb = self._kb()
        cat = self._category()
        src = self._source_locale()
        own = self._own_target_ids()
        with self._client() as c:
            self._check_schema(c)
            if article_ids:
                ids = [self._split_id(a) for a in list(article_ids)[:cap]]
                out = []
                for sid in ids:
                    row = self._current_published(c, self._get_row(c, sid))
                    rec = self._source_record(row, explicit=True, own=own)
                    if rec is not None:
                        out.append(self._charge_record(rec))
                self._persist_numbers()
                return out
            # The language clause is LAST so `^ORlanguageISEMPTY` ORs only that clause:
            # articles on a single-language instance have an empty language.
            query = f"kb_knowledge_base={kb}^workflow_state=published"
            if cat:
                query += f"^kb_category={cat}"
            query += f"^language={src}^ORlanguageISEMPTY^ORDERBYsys_id"
            rows = self._paginate(c, query, cap, accept=lambda r: True)
            out = []
            for row in rows:
                rec = self._source_record(row, explicit=False, own=own)
                if rec is not None:
                    out.append(rec)
                if len(out) >= cap:
                    break
            self._persist_numbers()
            return out

    def show_article(self, ref: str) -> dict | None:
        sid = self._split_id(ref)
        with self._client() as c:
            row = self._current_published(c, self._get_row(c, sid))
        return self._source_record(row, explicit=True, own=set()) if row else None

    # --------------------------------------------- write-back payload (see build_payload)
    @staticmethod
    def _has_translated_content(fields: dict) -> bool:
        return not bool((fields or {}).get("_no_translation"))

    @staticmethod
    def _row_hash(short_description, text) -> str:
        return content_hash(short_description or "", text or "")

    def _persist_written(self, sid: str, written_hash: str) -> None:
        self._written_delta[sid] = written_hash
        if self.connection_id is None:
            return
        try:
            from app.services.connectors import store

            store.patch_config_nested(
                self.connection_id, {"servicenow_written": {"add": {sid: written_hash}}}
            )
        except Exception:
            log.warning(
                "servicenow connection %s: could not persist written hash", self.connection_id
            )

    def _is_parked(self, aid, loc, chash) -> bool:
        parked = (self.config or {}).get("servicenow_parked") or {}
        return bool(chash) and parked.get(f"{aid}:{loc}") == chash

    # ----------------------------- health probe used by the suppression corroboration
    def target_health(self, target_id: str) -> str:
        """'gone' (404), 'blank' (a human emptied title and body) or 'ok'. A probe failure
        answers 'ok' so a garbage response SUPPRESSES (safe) instead of recreating."""
        try:
            sid = self._split_id(target_id)
        except ConnectorError:
            return "gone"
        try:
            with self._client() as c:
                row = self._get_row(c, sid)
        except (httpx.TransportError, ConnectorError, ValueError):
            return "ok"
        if row is None:
            return "gone"
        if (
            not str(row.get("short_description") or "").strip()
            and not str(row.get("text") or "").strip()
        ):
            return "blank"
        return "ok"

    # ------------------------------ the four T3DocClient methods the strategy drives
    def create_target(self, source_id: str, locale: str, fields: dict):
        """RESERVE: insert the target-language row as a DRAFT linked to the source by
        Parent. (id | None, error). A fully refused translation is never created."""
        if not self._has_translated_content(fields):
            return None, "all segments refused/flagged - article not created"
        title = (fields or {}).get("title")
        if title is None:
            return None, "title held for review - article not created"
        try:
            src = self._split_id(source_id)
            code = self._lang_code(locale)
        except ConnectorError as e:
            return None, str(e)
        body = {
            "kb_knowledge_base": self._kb(),
            "language": code,
            "parent": src,
            "short_description": title,
            "workflow_state": "draft",
        }
        cat = str((fields or {}).get("category") or "")
        if cat and _SYSID_RE.fullmatch(cat):
            body["kb_category"] = cat
        if (fields or {}).get("has_body") and (fields or {}).get("body") is not None:
            body["text"] = fields["body"]
        try:
            with self._client() as c:
                r = self._req(
                    c,
                    "POST",
                    self._table_url(),
                    idempotent=False,
                    params={"sysparm_exclude_reference_link": "true"},
                    json=body,
                )
        except httpx.TransportError as e:
            return None, f"{type(e).__name__}: {e}"
        if r.status_code not in (200, 201):
            return None, f"create article -> {r.status_code}: {self._err(r)}"
        res = self._result(r, "create")
        sid = str((res or {}).get("sys_id") or "").strip().lower() if isinstance(res, dict) else ""
        if not _SYSID_RE.fullmatch(sid):
            return None, "create returned no sys_id"
        if _ref_value(res.get("parent")) != src:
            self._link_unconfirmed.append(sid)
        self._created_hash[sid] = self._row_hash(title, body.get("text", ""))
        # What the server STORED (not the bytes we sent) is the baseline for spotting a later
        # human edit of this Draft.
        self._persist_written(
            sid,
            self._row_hash(
                res.get("short_description", title), res.get("text", body.get("text", ""))
            ),
        )
        return sid, None

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """PATCH the target (partial update: a held field is simply absent from the body, so
        its stored translation survives). Workflow-gated, scope-checked, human-edit honest."""
        tid = str(target_id)
        if not self._has_translated_content(fields):
            return WriteResult(
                False,
                False,
                target_id=tid,
                message="all segments refused/flagged - article not written",
            )
        try:
            sid = self._split_id(tid)
        except ConnectorError as e:
            return WriteResult(False, False, target_id=tid, message=str(e))
        title = (fields or {}).get("title")
        text = (fields or {}).get("body") if (fields or {}).get("has_body") else None
        body = {}
        if title is not None:
            body["short_description"] = title
        if text is not None:
            body["text"] = text
        if not body:
            return WriteResult(False, False, target_id=sid, message="nothing translated to write")
        # The create already sent exactly this: do not write it a second time.
        if self._created_hash.get(sid) == self._row_hash(title, text if text is not None else ""):
            return WriteResult(True, False, target_id=sid, message="filled")
        pair = f"{(fields or {}).get('source_id', '')}:{(fields or {}).get('locale', '')}"
        prior = (self.config.get("servicenow_written") or {}).get(sid)
        try:
            with self._client() as c:
                cur = self._get_row(c, sid)
                if cur is None:
                    return WriteResult(
                        False, False, target_id=sid, message=f"target {sid[:8]} not found"
                    )
                if _ref_value(cur.get("kb_knowledge_base")) != self._kb():
                    return WriteResult(
                        False,
                        False,
                        target_id=sid,
                        message="target is outside the configured knowledge base - not written",
                    )
                state = str(cur.get("workflow_state") or "").strip().lower()
                if state not in self._edit_states():
                    why = (
                        f"held: target {cur.get('number') or sid[:8]} is {state or 'unknown'}, "
                        "not Draft; it is not edited in place (edit it in ServiceNow, or set "
                        "'Edit LIVE (published) translations')"
                    )
                    if pair != ":":
                        self._parks[pair] = why
                    return WriteResult(False, False, target_id=sid, message=why)
                r = self._req(
                    c,
                    "PATCH",
                    self._table_url(sid),
                    params={"sysparm_exclude_reference_link": "true", "sysparm_fields": _FIELDS},
                    json=body,
                )
        except httpx.TransportError as e:
            return WriteResult(False, False, target_id=sid, message=f"{type(e).__name__}: {e}")
        if r.status_code != 200:
            return WriteResult(
                False,
                False,
                target_id=sid,
                message=f"update article -> {r.status_code}: {self._err(r)}",
            )
        if prior and self._row_hash(cur.get("short_description"), cur.get("text")) != prior:
            self._superseded.append(str(cur.get("number") or sid[:8]))
        if state != "draft":
            # update_published=1 on a Published row: this edit is LIVE now (gate 2026-10-10).
            self._live_edited.append(str(cur.get("number") or sid[:8]))
        res = self._result(r, "update")
        res = res if isinstance(res, dict) else {}
        stored = self._row_hash(
            res.get("short_description", cur.get("short_description")),
            res.get("text", cur.get("text")),
        )
        self._persist_written(sid, stored)
        return WriteResult(True, False, target_id=sid, message="filled")

    def verify_target(self, target_id: str) -> bool:
        """Does the target row still exist? A fetch by sys_id, any workflow state (draft
        rows included). Only a definite 404 is 'gone'; anything else unverifiable is
        TargetUnverifiable (#444), never a recreate."""
        try:
            sid = self._split_id(target_id)
        except ConnectorError:
            return False
        try:
            with self._client() as c:
                r = self._req(
                    c,
                    "GET",
                    self._table_url(sid),
                    params={
                        "sysparm_fields": "sys_id,workflow_state",
                        "sysparm_exclude_reference_link": "true",
                    },
                )
        except (httpx.TransportError, ConnectorError) as e:
            raise TargetUnverifiable(f"{type(e).__name__}: {e}") from e
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise TargetUnverifiable(f"HTTP {r.status_code} verifying {sid[:8]}")
        return True

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        """RULE-6 probe via the NATIVE link: a row with parent=<source> and the target
        language that our id map did not create (a human translation, a crash orphan, or a
        re-added connection). Returns its sys_id (-> needs_link, never overwritten) or None.
        The query is re-checked client-side (parent and language must match), so an instance
        that ignores an unknown `parent` clause cannot produce a false match."""
        try:
            src = self._split_id(source_id)
            code = self._lang_code(locale)
        except ConnectorError:
            return None
        own = self._own_target_ids()
        # The stable ref is the FIRST version's sys_id; a human translation made after the
        # source was re-versioned may name the live version as its Parent. Probe both, so
        # such a translation is flagged needs_link instead of getting a duplicate Draft.
        live = str(self._live_by_ref.get(src) or "")
        parents = [src] + ([live] if _SYSID_RE.fullmatch(live) and live != src else [])
        rows: list = []
        try:
            with self._client() as c:
                for p in parents:
                    rows += self._paginate(
                        c,
                        f"parent={p}^language={code}^kb_knowledge_base={self._kb()}",
                        20,
                        accept=lambda r: True,
                        fields="sys_id,language,parent,kb_knowledge_base",
                    )
        except (httpx.TransportError, ConnectorError, ValueError):
            return None
        for row in rows:
            sid = str(row.get("sys_id") or "").strip().lower()
            if (
                _SYSID_RE.fullmatch(sid)
                and sid not in parents
                and sid not in own
                and _ref_value(row.get("parent")) in parents
                and _norm_lang(row.get("language")) == _norm_lang(code)
                and _ref_value(row.get("kb_knowledge_base")) == self._kb()
            ):
                return sid
        return None

    # ------------------------------------------------- generic Connector surface
    def pull(self) -> list[PulledItem]:
        from app.filters import get_filter

        out: list[PulledItem] = []
        for rec in self.discover_articles():
            ref = rec["ref"]
            title = rec.get("title") or ""
            out.append(PulledItem(external_id=ref, field="title:0", text=title, title=title[:60]))
            for i, u in enumerate(
                get_filter("html").parse((rec.get("content") or "").encode("utf-8")).units
            ):
                out.append(
                    PulledItem(external_id=ref, field=f"body:{i}", text=u.text, title=title[:60])
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "ServiceNow creates one Draft article per language - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only: re-list the published sources for the caller to diff against the
        per-(article, language) content hash. No watermark is advanced."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow - thin hooks over the shared engine (flow.run_translate_flow).
# ===========================================================================
def _sn_preflight(ctx):
    conn = ctx.connector
    with conn._client() as c:
        conn._check_schema(c)
        conn._languages(c)
    src = _norm_lang(conn._source_locale())
    active = {_norm_lang(x) for x in conn._langs_cache or []}
    if active:
        enabled = {l for l in ctx.locales if _norm_lang(l) in active and _norm_lang(l) != src}  # noqa: E741
    else:
        enabled = {l for l in ctx.locales if _norm_lang(l) != src}  # noqa: E741
    return enabled, {"link_mode": conn.link_mode(), "langs_unverified": not active}


def _sn_prepare(ctx, art):
    from app.filters import get_filter

    ref = art["ref"]
    title = art.get("title") or ""
    body = art.get("content") or ""
    if not title.strip() and not body.strip():
        return flow.SkipArticle("empty_skipped")
    conn = ctx.connector
    units = get_filter("html").parse(body.encode("utf-8")).units
    chash = content_hash(title, body)
    conn._chash_by_aid[ref] = chash
    return flow.ArticlePrep(
        aid=ref,
        chash=chash,
        title_text=title,
        body_units=list(units),
        extra_skeleton={"article_id": ref, "title": title},
        doc_title=f"{(title or art.get('number') or 'article')[:50]} ({ref[:8]})",
        extra={
            "body_bytes": body.encode("utf-8"),
            "source_title": title,
            "has_body": bool(body.strip()),
            "category": art.get("category") or "",
            "source_id": ref,
        },
    )


def _sn_is_suppressed(connector, aid, loc):
    # A `done` map row is not trusted blindly: corroborate with a cheap live GET so an
    # externally deleted or blanked target is recreated/refilled (taxonomy C steady state).
    if not connector.strategy.exists_target(connector, aid, loc):
        return False
    cid = getattr(connector, "connection_id", None)
    row = idmap.get(cid, aid, loc) if cid is not None else None
    if row is None or not row.target_id:
        return False
    return connector.target_health(row.target_id) == "ok"


def _sn_parked(connector, aid, loc, chash):
    """Pairs a scheduled run must skip WITHOUT materializing or translating (no MT spend):
    a needs_link row waiting for an operator decision (re-attempting it would only re-fail),
    and a pair held by workflow for exactly this source hash. Neither records a content hash:
    a parked pair is not done."""
    cid = getattr(connector, "connection_id", None)
    if cid is not None:
        row = idmap.get(cid, aid, loc)
        if row is not None and row.status == idmap.NEEDS_LINK:
            connector._nl_parked += 1
            return True
    return connector._is_parked(aid, loc, chash)


def _sn_build_payload(prep, loc, seg0_target, mapping):
    """Taxonomy B: a held title or ANY held body unit yields None for that field, which the
    PATCH omits (the stored translation survives) and which the flow already refuses to
    CREATE with (#640). Never the source text standing in for a withheld translation."""
    from app.filters import get_filter

    ex = prep.extra
    held_body = any(
        str(u.p_index) not in mapping and (u.text or "").strip() for u in prep.body_units
    )
    body = None
    if ex["has_body"] and not held_body:
        body = get_filter("html").build(ex["body_bytes"], mapping).decode("utf-8")
    return {
        "title": seg0_target,
        "body": body,
        "has_body": ex["has_body"],
        "locale": loc,
        "category": ex.get("category", ""),
        "source_id": ex["source_id"],
        "_no_translation": seg0_target is None and not mapping,
    }


def _sn_held_by_workflow(ctx) -> int:
    return len(ctx.connector._parks)


def _sn_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    parked = ctx.counters.get("parked_skipped", 0)
    # A needs_link flag and a workflow hold are ATTENTION states the message names, not
    # connector errors: they must not turn the connection red on every scheduled run.
    real_failures = [
        f
        for f in ctx.failures
        if f"{f.get('article_id')}:{f.get('locale')}" not in ctx.connector._parks
        and not str(f.get("error") or "").startswith("needs_link")
    ]
    return bool(
        (ctx.created + ctx.updated) > 0
        or ((ctx.skipped + parked) > 0 and not real_failures)
        or (
            not real_failures
            and (ctx.created + ctx.updated + ctx.skipped + parked) == 0
            and ctx.usable
        )
    )


_SKIP_TEXT = {
    "wiki_unsupported": "{n} wiki-format article(s) skipped (only HTML articles are supported)",
    "unpublished": "{n} named article(s) skipped because they are not published",
    "is_translation": "{n} article(s) skipped because they are translated versions"
    " (they have a Parent)",
    "other_language": "{n} article(s) skipped because they are not in the source language",
    "own_translation": "{n} named id(s) skipped because they are this connection's own"
    " translations",
    "not_found": "{n} named id(s) not found in this knowledge base",
}


def _sn_build_message(ctx):
    conn = ctx.connector
    live = sorted(set(conn._live_edited))
    parts = [
        f"Created {ctx.created}, updated {ctx.updated - len(conn._live_edited)} Draft "
        f"article(s) across {len(ctx.usable)} language(s) from {len(ctx.articles)} article(s)."
    ]
    if live:
        # update_published=1: these edits are visible to readers now. Never folded into the
        # Draft count above.
        parts.append(
            f"LIVE: edited {len(conn._live_edited)} PUBLISHED translation(s) in place, visible "
            f"to readers now ({', '.join(live[:5])}{', ...' if len(live) > 5 else ''})."
        )
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) article(s).")
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} empty article(s).")
    for reason, tmpl in _SKIP_TEXT.items():
        if conn._skips.get(reason):
            parts.append(tmpl.format(n=conn._skips[reason]) + ".")
    held_wf = len(conn._parks) + ctx.counters.get("parked_skipped", 0) - conn._nl_parked
    if held_wf:
        parts.append(
            f"ATTENTION: {held_wf} pair(s) held because the translation is no longer Draft "
            f"(submitted, published or retired in ServiceNow); not edited in place and not "
            f"re-translated until the source changes or you retry."
        )
    needs_link = idmap.status_counts(ctx.cid).get("needs_link", 0)
    if needs_link:
        parts.append(
            f"{needs_link} article(s) need a link decision (a translation already exists in "
            f"ServiceNow - adopt or ignore it)."
        )
    if conn._superseded:
        sup = sorted(set(conn._superseded))
        parts.append(
            f"Superseded human edits on {len(sup)} Draft(s) ({', '.join(sup[:5])}) - re-translated "
            f"from the changed source."
        )
    if conn._link_unconfirmed and conn.link_mode() == "native":
        parts.append(
            f"Note: ServiceNow did not confirm the Parent link on {len(conn._link_unconfirmed)} "
            f"new article(s) (check the I18N plugin)."
        )
    mode = (ctx.extra or {}).get("link_mode") or conn.link_mode()
    parts.append(conn.link_label(mode))
    if (ctx.extra or {}).get("langs_unverified"):
        parts.append(
            "Note: the instance's active languages could not be read, so target languages"
            " were not checked."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) (the source language, or not an active "
            f"ServiceNow language): {', '.join(ctx.rejected)}."
        )
    if ctx.errors:
        parts.append("Errors: " + "; ".join(ctx.errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())
    return " ".join(parts)


def _persist_parks(ctx) -> None:
    """Park every pair held by workflow (source hash -> no MT re-spend while unchanged) and
    clear the park of any pair that wrote completely this run, in ONE atomic write."""
    from app.services.connectors import store

    conn = ctx.connector
    add_src, add_why = {}, {}
    for pair, why in conn._parks.items():
        aid = pair.rpartition(":")[0]
        ch = conn._chash_by_aid.get(aid)
        if ch:
            add_src[pair] = ch
            add_why[pair] = why[:300]
    remove = [k for k in ctx.hash_delta if k not in add_src]
    changes: dict = {}
    if add_src or remove:
        changes["servicenow_parked"] = {"add": add_src, "remove": remove}
        changes["servicenow_parked_why"] = {"add": add_why, "remove": remove}
    # A pair written with a held unit (a MIXED target) has its older stored hash dropped by
    # the shared flow (flow.py stage 9, #666).
    if changes:
        store.patch_config_nested(ctx.cid, changes)


def _sn_build_result(ctx):
    _persist_parks(ctx)
    conn = ctx.connector
    return {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "parked": ctx.counters.get("parked_skipped", 0),
        "held_by_workflow": sorted(conn._parks),
        "source_skips": dict(conn._skips),
        "superseded": sorted(set(conn._superseded)),
        "live_edited": len(conn._live_edited),
        "live_edited_articles": sorted(set(conn._live_edited)),
        "rejected": ctx.rejected,
        "failures": ctx.failures,
        "link_mode": (ctx.extra or {}).get("link_mode") or conn.link_mode(),
        "id_map": idmap.status_counts(ctx.cid),
    }


def servicenow_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate ServiceNow Knowledge articles into one or more languages, each written as
    a separate Draft kb_knowledge row linked to the source by Parent (T3 DocPerLocale
    strategy: reserve-then-fill + id-map reconciliation). Thin wrapper over
    flow.run_translate_flow; poll-only (no watermark), content-hash suppression.
    only_pairs restricts the write loop to exactly those (article, language) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="servicenow",
        no_conn_msg="No such ServiceNow connection.",
        hash_key="servicenow_hashes",
        probe_label="ServiceNow",
        preflight=_sn_preflight,
        prepare=_sn_prepare,
        is_suppressed=_sn_is_suppressed,
        build_payload=_sn_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_sn_compute_ok,
        build_message=_sn_build_message,
        build_result=_sn_build_result,
        extra_counters=("empty_skipped", "parked_skipped"),
        parked=_sn_parked,
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


def servicenow_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Operator resolution of a ServiceNow needs_link row (SPEC-t3 rule 6 adopt-or-ignore):
    a translation that already exists under the source's Parent but that the id map never
    created. 'adopt' makes the next run update it (while it is still Draft); 'ignore' drops
    the row (it re-flags). Only edits the id map, never content."""
    from app.services.connectors.store import _log, get

    conn = get(cid)
    if not conn or conn.kind != "servicenow":
        return False, "No such ServiceNow connection.", {}
    ok, msg = idmap.resolve_needs_link(cid, str(source_id), target_locale, action)
    _log(cid, "resolve-link", ok, 0, msg, actor)
    return ok, msg, {"id_map": idmap.status_counts(cid)}
