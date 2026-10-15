"""Document360 knowledge-base connector (T2, rebuilt and un-archived 2026-10-10).

Topology T2: every article owns a per-language version addressed by a `langCode` in the URL
path. `PUT /v2/Articles/{articleId}/{langCode}` writes it; there is no duplicate-object
gymnastics. The write-back rides the shared strategy.SubResourceStrategy, like Zendesk; this
module owns the endpoints, auth and the Document360 field shapes.

Built from the CURRENT vendor docs only (no third-party connector code was read):
  base hosts      https://apihub.document360.io (global), apihub.us.document360.io (US),
                  apihub.ca.document360.io (v3 intro page lists it)   [apidocs.document360.com]
  auth            `api_token` request header (v2); rate limit 60 req/min (Business), 100
                  (Enterprise/Trial) per token, 429 + X-RateLimit-Reset  [rate-limiting]
  workspaces      GET /v2/ProjectVersions
  languages       GET /v2/Language/{projectVersionId}  -> language_code per enabled language
  article list    GET /v2/ProjectVersions/{pv}/articles?langCode&page&hitsPerPage (flat)
  category tree   GET /v2/ProjectVersions/{pv}/categories (nested child_categories + articles)
  read            GET /v2/Articles/{id}/{langCode}?isForDisplay&isPublished&appendSASToken
  write           PUT /v2/Articles/{id}/{langCode}  PARTIAL update; fields title, content,
                  html_content (deprecated), category_id, hidden, version_number,
                  translation_option ("0" none, "1" needs translation, "2" translated), source,
                  order. No `publish` field (the body rejects unknown properties).
  fork            PUT /v2/Articles/{id}/fork {version_number, user_id, lang_code} -> new draft
  publish         POST /v2/Articles/{id}/{langCode}/publish {user_id, version_number,
                  publish_message}: a separate call; the draft becomes the published version.

WHAT THE ARCHIVED VERSION GOT WRONG (found by this rebuild): host `.com` instead of `.io`; the
languages route; the category/version article routes; publish via PUT with no body (it is a POST
that needs user_id and version_number); a `publish` key in the PUT body; a hard-coded
`translation_option=2` even for a partly translated article.

GOTCHAS:

  (1) EDITOR TYPE. `content_type` (markdown / wysiwyg / block) decides the filter: Markdown ->
      `md`, WYSIWYG -> `html`. Block-editor articles have no Markdown or HTML body and are
      skipped. The body is always written to `content` (the documented field for every editor;
      `html_content` is deprecated).

  (2) MERGE CODES. Snippet, variable and glossary merge codes are `{{...}}` tokens. They are
      shielded with private-use sentinels before segmenting and restored after; a code that did
      not round-trip holds the pair (template integrity).

  (3) NEVER A SOURCE STAND-IN (#640). The PUT is a partial update, so a held title or body is
      simply left out and keeps its stored value; the body is omitted when ANY of its units is
      held. A language with no real version (a missing one answers 400/404 or returns the
      default-language fallback) is created only with BOTH title and body translated.

  (4) PUBLISHED TRANSLATIONS ARE NEVER EDITED IN PLACE. A version is edited only when it is a
      draft. When the language's latest version is the published one, the connector forks a new
      draft from it (needs the team account id) and writes into that, so the live text stays
      until a person publishes the draft; with no team account id the pair fails as
      "live-kept". The run result reports forked drafts and live-kept pairs.

CHANGE DETECTION: no webhook, poll only. A content hash of the source per (article, langCode),
plus the target's translation_option (needs-translation re-enqueues). State is persisted per
connection via store.patch_config_merge.
"""

from __future__ import annotations

import re

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
from app.services.connectors.base import (
    content_hash as _content_hash,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult
from app.services.connectors.template_integrity import MERGE_SPANS, template_spans_match
from app.services.connectors.vendor_text import vendor_excerpt

API_VERSION = "v2"  # article routes are under /v2/Articles (v3 exists; v2 is still documented)

# translation_option enum (docs §1.1): 0=None, 1=Needs translation, 2=Translated.
TRANSLATION_OPTION_TRANSLATED = 2

# --- merge-code shielding --------------------------------------------------------
# Document360 snippets/variables/glossary render as {{...}} tokens. The EXACT
# delimiter set is [UNCERTAIN] (docs §1.3 flags it), so we take the CONSERVATIVE
# {{...}} form only — the documented, universal case — and shield nothing else.
# A token is non-greedy and must not itself contain braces, so adjacent codes
# ({{a}}{{b}}) each shield independently. Round-trip tested (see verify_document360).
_MERGE_CODE = MERGE_SPANS
# Private-use-area sentinels: characters no natural-language MT output will emit or
# translate, so a shielded code round-trips byte-identical.  <idx> .
_SHIELD_OPEN = ""
_SHIELD_CLOSE = ""
# Defensive guard: PUA codepoints "never appear in real content" is a strong but not
# absolute assumption -- source text COULD legitimately (or accidentally) already
# contain a raw U+E000/U+E001 character, which would collide with our sentinels and
# make unshield_codes misparse a "<idx>" boundary that was never ours, silently
# corrupting the write-back (proven corruption per review). Since these codepoints
# have no defined meaning outside private agreements and Document360 bodies are
# prose/markup (never legitimately needing them), shield_codes strips any
# pre-existing raw occurrence from the source BEFORE substitution. Accepted edge:
# if a tenant genuinely relied on a raw PUA char for something else, it is silently
# dropped rather than causing corruption -- a documented trade-off, not a
# round-trip guarantee for that (extremely unlikely) case.
_RAW_SENTINEL_RE = re.compile(f"[{_SHIELD_OPEN}{_SHIELD_CLOSE}]")


def shield_codes(text: str) -> tuple[str, list[str]]:
    """Replace each {{merge_code}} with a sentinel placeholder; return the shielded
    text + the ordered list of the ORIGINAL codes (index = order of appearance).
    The MT engine never sees a merge code, so it cannot mangle one. Idempotent-safe:
    the sentinels use private-use-area codepoints that never appear in real content
    -- and any pre-existing raw occurrence of one of those codepoints in the SOURCE
    is stripped first, so it can never collide with (and falsely trigger) a restore
    in unshield_codes."""
    text = _RAW_SENTINEL_RE.sub("", text or "")
    codes: list[str] = []

    def _sub(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"{_SHIELD_OPEN}{len(codes) - 1}{_SHIELD_CLOSE}"

    return _MERGE_CODE.sub(_sub, text), codes


def unshield_codes(text: str, codes: list[str]) -> str:
    """Inverse of shield_codes: restore every sentinel to its original merge code.
    An out-of-range/garbled sentinel (an MT engine that somehow dropped/reordered a
    placeholder) is left as-is rather than crashing — the write still lands and the
    damage is visible, not a stack trace. Byte-identical for a faithful engine."""
    if not codes:
        return text or ""

    def _restore(m: re.Match) -> str:
        try:
            return codes[int(m.group(1))]
        except (ValueError, IndexError):
            return m.group(0)

    return re.sub(f"{_SHIELD_OPEN}(\\d+){_SHIELD_CLOSE}", _restore, text or "")


# --- write-side egress guard -----------------------------------------------------
# unshield_codes() is total (never raises) but a MANGLING MT engine can still hand
# back text only PARTIALLY restorable: it may drop one half of a sentinel pair (a
# SPLIT sentinel — e.g. the close delimiter vanishes, leaving a bare "<open>digits"
# with no close) or emit an out-of-range index that unshield_codes deliberately
# leaves verbatim (see its docstring). "Left as-is" is right INSIDE the codec (never
# crash), but at the WRITE boundary that leftover is a live corruption risk: a raw
# U+E000/U+E001 glyph, or a bare digit stripped of its delimiters, landing in a real
# Document360 tenant cell — visible garbage a human/customer reads. So immediately
# before a decoded body reaches the PUT payload, strip every residual shield artifact
# — INCLUDING the digits of a half-open sentinel, not just its delimiter — so a
# mangled merge code degrades to a clean deletion, never a glyph and never a stray
# number. Purely COSMETIC net for damage unshield_codes already flagged as
# unrestorable; a faithful engine leaves nothing for it to match (idempotent no-op).
#
# Matches, in order of specificity: a well-formed "<open>digits<close>" pair (an
# out-of-range index left verbatim by unshield_codes), then a half-open
# "<open>digits" or "digits<close>" (a split sentinel that lost one delimiter), then
# a bare lone delimiter with no adjacent digits (both halves lost). Mirrors
# airtable._strip_write_egress_debris exactly.
_RESIDUAL_SENTINEL_RE = re.compile(
    f"{_SHIELD_OPEN}\\d+{_SHIELD_CLOSE}|{_SHIELD_OPEN}\\d+|\\d+{_SHIELD_CLOSE}"
)


def strip_write_egress_debris(text: str) -> str:
    """Remove any leftover merge-code shield artifacts before a decoded body reaches
    a write payload. Idempotent no-op on a faithful round-trip.

    Order matters: a residual sentinel — whole OR half-open — must be stripped as one
    unit (delimiter(s) AND its digits together) FIRST; stripping only the delimiter
    codepoint afterward would leave the digit behind as a stray number in the written
    text. Only then do we mop up any remaining lone delimiter (both halves lost, no
    digits adjacent)."""
    text = _RESIDUAL_SENTINEL_RE.sub("", text or "")
    return _RAW_SENTINEL_RE.sub("", text)


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LANG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
# Marker build_payload leaves on a payload whose held (NEEDS_REVIEW) field was omitted, so the
# client does not mark the version Translated and never writes a source-language stand-in (#640).
_HELD_KEY = "_held"


def _d360_id(value, what: str = "id") -> str:
    """A Document360 id (article, project version, category, team account) is a short token of
    letters, digits, `-` and `_`. Anything else (`..`, `%2e`, `/`, `?`, `#`, CR/LF, NUL, unicode
    look-alikes) is refused before it can reach a URL path (defect taxonomy A)."""
    text = str(value if value is not None else "").strip()
    if not _ID.fullmatch(text) or not text.isascii():
        raise ConnectorError(f"Invalid Document360 {what}.")
    return text


def _d360_lang(value) -> str:
    text = str(value if value is not None else "").strip()
    if not _LANG.fullmatch(text) or not text.isascii():
        raise ConnectorError("Invalid Document360 langCode.")
    return text


SPEC = ConnectorSpec(
    kind="document360",
    name="Document360",
    icon="\U0001f4d8",  # closed book
    cat="Knowledge base / help center",
    blurb="Translate Document360 knowledge-base articles into any language. Each "
    "translation is written back as the article's per-language version "
    "(PUT /v2/Articles/{id}/{langCode}), so the source article is never "
    "overwritten. Markdown and WYSIWYG articles are both handled.",
    config_fields=[
        FieldSpec(
            "project_version_id",
            "Project version id",
            "The workspace (project version) to translate. Required. Settings, Knowledge base "
            "portal, Project versions shows it, or pick one with Test connection.",
            required=True,
            placeholder="a1b2c3d4-...",
        ),
        FieldSpec(
            "category_id",
            "Category id",
            "Limit to one category and its sub-categories (optional).",
            required=False,
            placeholder="e5f6g7h8-...",
        ),
        FieldSpec(
            "source_lang",
            "Source langCode",
            "The authoring language code (Document360 langCode, e.g. 'en'). Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Document360 langCodes to write (must be enabled in the project).",
            required=False,
            placeholder="de, fr, es",
        ),
        FieldSpec(
            "region",
            "Data region",
            "Where your Document360 project is hosted: 'global' (apihub.document360.io, the "
            "default), 'us' (apihub.us.document360.io) or 'ca'.",
            required=False,
            placeholder="global",
        ),
        FieldSpec(
            "user_id",
            "Team account id",
            "A Document360 team account id (Settings, Users and permissions). Needed to start a "
            "new draft of a translation that is already published, and to publish. Without it, "
            "a published translation is left unchanged and reported.",
            required=False,
            placeholder="f11efc6f-...",
        ),
        FieldSpec(
            "publish",
            "Publish after write",
            "'1' = publish the translated version after writing it; '0' = leave it as a draft "
            "(default, safe; a translation already published is never edited in place: a new "
            "draft is started from it, or it is left unchanged and reported).",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "max_articles", "Max articles", "Safety cap per run.", required=False, placeholder="50"
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_token",
            "API token",
            "Document360, Settings, Knowledge base portal, API tokens. The token is sent in "
            "the api_token request header. Needs an API-enabled plan (a 14-day trial has it).",
            secret=True,
            placeholder="your-api-token",
        ),
    ],
    setup_steps=[
        "In Document360 go to Settings, Knowledge base portal, API tokens and generate a "
        "token (an API-enabled plan or the 14-day trial).",
        "Paste the token as the API token above, and set the Data region if your project is "
        "hosted in the US or Canada.",
        "Enter the project version id of the workspace to translate (Test connection lists it).",
        "Enter the target langCodes (they must be enabled languages in the project). "
        "Optionally limit to one category.",
        "Optionally enter a team account id so a translation that is already published can "
        "get a new draft (or be published when Publish is on).",
        "Document360 has no webhooks, so change detection is poll-based: run on a schedule "
        "(Every N minutes on the connection page) to keep translations fresh.",
    ],
    other_side="Document360 has no webhooks; change detection compares a content hash of the "
    "source per (article, langCode) plus the translation_option stale signal.",
    docs_url="https://apidocs.document360.com/v2-api/apidocs/update-an-article",
    supports_oauth=False,  # api_token header only
    archived=False,  # un-archived 2026-10-10 (connector parity rebuild); live QA is pending  # noqa: E501
)

# Regional API hosts. v2 pages list the global and US hosts; the Canada host is named on the v3
# introduction page only (live check in the runbook). The region is a closed set, so a
# hostile config value can never choose the host a stored token is sent to.
_HOSTS = {
    "global": "apihub.document360.io",
    "us": "apihub.us.document360.io",
    "ca": "apihub.ca.document360.io",
}

# Version `status`: the docs show both a string enum and the integers 0 (draft) / 3 (published).
_STATUS_PUBLISHED = ("published", "3")
_STATUS_DRAFT = ("draft", "0")
# translation_option: request strings "0"/"1"/"2"; the response enum also names them.
_OPTION_NEEDS_WORK = ("0", "1", "none", "needtranslation")

# A draft-mode run never writes into a PUBLISHED translation (same convention as Freshdesk and
# Intercom): with a team account id a draft is forked from it; without one it is left untouched
# and reported as live-kept, skipped before any machine translation is paid for.
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the translation is PUBLISHED and this connection writes drafts with no Team account id to "
    "start a new draft from it, so the live text was left unchanged; set the Team account id "
    "(a draft is then started from the published text) or turn on 'Publish after write'"
)


def _editor_of(article: dict) -> str:
    """Editor type of an article: 'markdown' -> Markdown filter; 'html' -> HTML filter;
    'block' -> advanced (block) editor, which has no Markdown/HTML body and is not translated.

    The documented `content_type` (markdown / wysiwyg / block, or 0 / 1 / 2) decides when
    present. A payload without it falls back to which body field is populated."""
    ct = str(article.get("content_type") if article.get("content_type") is not None else "")
    ct = ct.strip().lower()
    if ct in ("block", "2"):
        return "block"
    if ct in ("wysiwyg", "1"):
        return "html"
    if ct in ("markdown", "0"):
        return "markdown"
    if (article.get("content") or "").strip():
        return "markdown"
    if (article.get("html_content") or "").strip():
        return "html"
    return "markdown"


def _body_of(article: dict, editor: str) -> str:
    """The authored body text. A WYSIWYG article may carry its HTML in `html_content`
    (older shape) or in `content`; the documented `content` field wins when both exist."""
    if editor == "block":
        return ""
    if editor == "markdown":
        return article.get("content") or ""
    return article.get("content") or article.get("html_content") or ""


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Document360Strategy(SubResourceStrategy):
    """Refuse failed merge-code fidelity before probing or writing the remote."""

    def write_target(self, client, record_id, target_locale, fields, *, draft=True):
        if fields.get("_template_hold"):
            return WriteResult(False, False, message=fields["_template_hold"])
        return super().write_target(client, record_id, target_locale, fields, draft=draft)


class Document360Connector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="document360",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        # Rich text is PER-ARTICLE (Markdown or HTML) - declare the common case;
        # extract routes per article via _editor_of.
        rich_text=RichText.MARKDOWN,
        regions="fixed-region",  # global / US / CA host pin
        change_signals=ChangeSignals(
            webhook_sig=None,  # NO webhook - poll only
            poll_field="version_number",  # content signal: article version bump
            stale_flag="translation_option",  # 0/1 = needs work; 2 = translated
            thin_payload=False,
        ),
        anti_loop_label=False,  # no labels; the content-hash watermark is the guard
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = Document360Strategy(
            source_locale_of=lambda conn, rec: (rec or {}).get("source_lang") or self._source_lang()
        )
        # lowercased langCode -> the code exactly as the project spells it (pt-BR).
        self._lang_wire: dict = {}
        # (article, langCode) -> notes the write left for the run result (forked draft, ...).
        self._notes: dict = {}

    # --- auth / base url ---
    def _source_lang(self) -> str:
        return (self.config.get("source_lang") or "en").strip().lower() or "en"

    def _host(self) -> str:
        region = str(self.config.get("region") or "global").strip().lower() or "global"
        host = _HOSTS.get(region)
        if host is None:
            raise ConnectorError("Document360 region must be one of: global, us, ca.")
        return host

    def _base(self) -> str:
        base = "https://" + self._host()
        _validate_target(base)  # SSRF guard on every request path (fixed hosts, but uniform)
        return base

    def _pv(self) -> str:
        pv = str(self.config.get("project_version_id") or "").strip()
        if not pv:
            raise ConnectorError("Document360 project version id is required.")
        return _d360_id(pv, "project version id")

    def _user(self) -> str:
        uid = str(self.config.get("user_id") or "").strip()
        return _d360_id(uid, "team account id") if uid else ""

    def _auth_kwargs(self) -> dict:
        """Per-request auth: the token in the `api_token` header. No Basic/Bearer."""
        token = (self.secret.get("api_token") or "").strip()
        if not token:
            raise ConnectorError("Document360 API token is required.")
        return {"headers": {"api_token": token}}

    def _get(self, c, path, **kw):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        return self._request(c, "GET", self._base() + path, headers=headers, **akw, **kw)

    def _write_kwargs(self) -> dict:
        akw = self._auth_kwargs()
        akw["headers"] = {"Content-Type": "application/json", **akw.pop("headers", {})}
        return akw

    def _v2(self, path: str) -> str:
        return f"/{API_VERSION}{path}"

    @staticmethod
    def _unwrap(body):
        """Document360 wraps payloads in {success, data}. Return `data` when present,
        else the body itself (tolerant of either shape / test doubles)."""
        if isinstance(body, dict) and "data" in body:
            return body.get("data")
        return body

    @staticmethod
    def _json(r):
        try:
            return r.json()
        except Exception:
            return None

    @staticmethod
    def _errors_text(r) -> str:
        """The vendor's error descriptions (or the raw text) of a failed response, lowercased."""
        body = Document360Connector._json(r)
        parts = []
        if isinstance(body, dict):
            for e in body.get("errors") or []:
                if isinstance(e, dict):
                    parts.append(str(e.get("description") or ""))
                else:
                    parts.append(str(e))
        return (" ".join(parts) or r.text[:200]).lower()

    def _wire_lang(self, locale: str) -> str:
        code = _d360_lang(locale)
        return self._lang_wire.get(code.lower(), code)

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get(c, self._v2("/ProjectVersions"))
            if r.status_code == 200:
                data = self._unwrap(r.json()) or []
                rows = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
                pv = str(self.config.get("project_version_id") or "").strip()
                if pv and pv not in {str(d.get("id")) for d in rows}:
                    return False, (
                        "Connected to Document360, but the project version id is not in this "
                        "project. Versions: "
                        + ", ".join(
                            f"{d.get('version_code_name') or d.get('id')} ({d.get('id')})"
                            for d in rows[:5]
                        )
                    )
                return True, f"Connected to Document360 ({len(rows)} project version(s))."
            if r.status_code in (401, 403):
                return False, (
                    f"Document360 rejected the API token ({r.status_code}). Check the token and "
                    "the Data region (global, us or ca)."
                )
            return False, f"Document360 returned {r.status_code}: {vendor_excerpt(r.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: project versions (id + name). Best-effort."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, self._v2("/ProjectVersions"))
            if r.status_code != 200:
                return []
            for pv in self._unwrap(r.json()) or []:
                pid = str(pv.get("id") or "")
                name = pv.get("version_code_name") or pid
                out.append({"id": pid, "name": f"{name} (version {pid})", "tables": []})
        except Exception:
            return out
        return out

    # --- discover: enabled languages + articles ---
    def enabled_locales(self) -> list[str]:
        """GET /v2/Language/{projectVersionId} - the langCodes enabled for the workspace
        (`language_code`). A target must be in this set or the write is rejected. Lowercased
        for comparison; the exact spelling is remembered for the wire. Fails closed: if the list
        cannot be read, nothing is written."""
        with self._client() as c:
            r = self._get(c, self._v2(f"/Language/{self._pv()}"))
        if r.status_code != 200:
            raise ConnectorError(
                f"Document360 languages for the project version failed ({r.status_code}): "
                f"{vendor_excerpt(r.text)}"
            )
        out = []
        for lang in self._unwrap(r.json()) or []:
            code = (lang.get("language_code") if isinstance(lang, dict) else lang) or ""
            code = str(code).strip()
            if code and _LANG.fullmatch(code):
                self._lang_wire[code.lower()] = code
                out.append(code.lower())
        return out

    def _list_articles(self, c, cap: int) -> list[dict]:
        """GET /v2/ProjectVersions/{pv}/articles, 0-based pages of 100 while pagination.has_more.
        The listing is flat (no category on the rows)."""
        out: list[dict] = []
        path = self._v2(f"/ProjectVersions/{self._pv()}/articles")
        for page in range(50):  # hard bound on pages per run
            params = {"langCode": self._source_lang(), "page": page, "hitsPerPage": 100}
            r = self._get(c, path, params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Document360 article list failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json()
            data = self._unwrap(body) or []
            if isinstance(data, dict):
                data = data.get("articles") or data.get("items") or []
            out.extend(self._keep_rows(data))
            pag = body.get("pagination") if isinstance(body, dict) else None
            # The docs show `pagination: null` in an example: a full page with no pagination
            # object still means "maybe more", so keep paging rather than truncate silently.
            more = pag.get("has_more") if isinstance(pag, dict) else len(data) >= 100
            if len(out) >= cap or not more:
                break
        return out[:cap]

    def _category_articles(self, c, category_id: str, cap: int | None = None) -> list[dict]:
        """Article rows of ONE category and its sub-categories, from the category tree
        (GET /v2/ProjectVersions/{pv}/categories). The category must exist in the configured
        project version or the run fails."""
        r = self._get(
            c,
            self._v2(f"/ProjectVersions/{self._pv()}/categories"),
            params={"langCode": self._source_lang()},
        )
        if r.status_code != 200:
            raise ConnectorError(
                f"Document360 category tree failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        data = self._unwrap(r.json()) or []

        def find(nodes):
            for n in nodes or []:
                if not isinstance(n, dict):
                    continue
                if str(n.get("id")) == category_id:
                    return n
                hit = find(n.get("child_categories"))
                if hit is not None:
                    return hit
            return None

        root = find(data)
        if root is None:
            raise ConnectorError("Document360 category id was not found in the project version.")
        rows: list[dict] = []
        stack = [root]
        while stack:
            node = stack.pop(0)
            rows.extend(a for a in node.get("articles") or [] if isinstance(a, dict))
            stack.extend(n for n in node.get("child_categories") or [] if isinstance(n, dict))
            if cap is not None and len(rows) >= cap:
                break
        return rows[:cap] if cap is not None else rows

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """List translatable source-language articles of the configured project version (and
        category). Explicit article_ids (retry path) are fetched by id and kept only when they
        sit inside the same scope; an id outside it is dropped like a missing one. Each returned
        article is the raw authored source version (no isForDisplay)."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cfg = self.config or {}
        category = str(cfg.get("category_id") or "").strip()
        if category:
            category = _d360_id(category, "category id")
        pv = self._pv()
        src = self._source_lang()
        with self._client() as c:
            if article_ids:
                ids = [_d360_id(a, "article id") for a in list(article_ids)[:cap]]
                in_category = None
                if category:
                    in_category = {str(a.get("id")) for a in self._category_articles(c, category)}
                out = []
                for aid in ids:
                    if in_category is not None and aid not in in_category:
                        continue
                    art = self._fetch_article(c, aid, src)
                    if art is not None and self._in_project_version(art, pv):
                        out.append(self._charge_record(art))
                return out
            if category:
                listing = self._keep_rows(self._category_articles(c, category, cap))
            else:
                listing = self._list_articles(c, cap)
            out = []
            for row in listing:
                aid = str(row.get("id") or row.get("article_id") or "")
                if not aid:
                    continue
                art = self._fetch_article(c, _d360_id(aid, "article id"), src)
                if art is not None and self._in_project_version(art, pv):
                    out.append(self._charge_record(art))
        return out

    @staticmethod
    def _in_project_version(article: dict, pv: str) -> bool:
        """Scope check on a fetched article: it must belong to the configured project version.
        An article that does not say which one fails closed."""
        return str(article.get("project_version_id") or "") == pv

    def _fetch_article(self, c, article_id: str, lang: str) -> dict | None:
        """GET /v2/Articles/{id}/{lang}?isPublished=true&appendSASToken=false: the raw authored
        source (no merge codes resolved, no storage tokens appended to file URLs). Returns the
        unwrapped article dict, or None when the article is not found."""
        lang = self._wire_lang(lang)
        r = self._get(
            c,
            self._v2(f"/Articles/{article_id}/{lang}"),
            params={"isForDisplay": "false", "isPublished": "true", "appendSASToken": "false"},
        )
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and body.get("success") is False:
                return None
            art = self._unwrap(body)
            if isinstance(art, dict) and art:
                art.setdefault("id", article_id)
                return art
            return None
        if r.status_code == 404 or (r.status_code == 400 and "not found" in self._errors_text(r)):
            return None
        raise ConnectorError(
            f"Document360 article {article_id} fetch failed ({r.status_code}): "
            f"{vendor_excerpt(r.text)}"
        )

    def show_article(self, article_id: str, lang: str | None = None) -> dict | None:
        aid = _d360_id(article_id, "article id")
        with self._client() as c:
            art = self._fetch_article(c, aid, (lang or self._source_lang()))
        return art if art is not None and self._in_project_version(art, self._pv()) else None

    # --- extract: translatable units from an article (editor-type branching) ---
    def _body_filter_name(self, article: dict) -> str:
        return "md" if _editor_of(article) == "markdown" else "html"

    def extract_units(self, article: dict, get_filter) -> tuple[list[dict], str, str]:
        """Article title (plain) + body, routed through the editor-appropriate filter
        (Markdown -> `md`, WYSIWYG -> `html`). Merge codes are SHIELDED before segmentation so
        MT never touches them.

        Returns (units, content_hash, editor). Units are p_index-keyed: 0 = title, 1+ = body
        text nodes. content_hash is the poll-suppression hash of the RAW (unshielded) source
        body + title."""
        title = article.get("title") or ""
        editor = _editor_of(article)
        raw_body = _body_of(article, editor)
        shielded_body, _codes = shield_codes(raw_body)
        shielded_title, _tcodes = shield_codes(title)
        filt = get_filter(self._body_filter_name(article))
        body_units = filt.parse(shielded_body.encode("utf-8")).units
        units = [{"p_index": 0, "text": shielded_title, "field": "title"}]
        for i, u in enumerate(body_units):
            units.append({"p_index": 1 + i, "text": u.text, "field": "body"})
        return units, _content_hash(title, raw_body), editor

    # --- write-back client (the SubResourceClient the strategy drives) ---
    def get_translation(self, record_id: str, locale: str):
        """GET /v2/Articles/{id}/{langCode}?isPublished=false -> the language version's LATEST
        draft-or-published state, or ABSENT when the article has no real version in that
        language: a 404, a 400 "article not found", a null-data envelope, or content that is
        only the default-language fallback (`is_fall_back_content`). Never a content read for
        the source (guards a silent source-language fallback)."""
        record_id = _d360_id(record_id, "article id")
        locale = self._wire_lang(locale)
        with self._client() as c:
            r = self._get(
                c,
                self._v2(f"/Articles/{record_id}/{locale}"),
                params={"isForDisplay": "false", "isPublished": "false", "appendSASToken": "false"},
            )
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and body.get("success") is False:
                return ABSENT
            data = self._unwrap(body)
            if not isinstance(data, dict) or not data:
                return ABSENT
            if data.get("is_fall_back_content"):
                return ABSENT
            return data
        if r.status_code == 404:
            return ABSENT
        if r.status_code == 400 and "not found" in self._errors_text(r):
            return ABSENT
        raise ConnectorError(
            f"Document360 translation probe {record_id}/{locale} -> {r.status_code}: "
            f"{vendor_excerpt(r.text)}"
        )

    @staticmethod
    def _is_live(current: dict) -> bool:
        """True when the language version's LATEST version is the published one, so a content
        write to it would change the live text. A newer draft beside a published version is not
        live."""
        status = str(current.get("status") if current.get("status") is not None else "")
        status = status.strip().lower()
        if status in _STATUS_PUBLISHED:
            return True
        if status in _STATUS_DRAFT:
            return False
        latest, public = (
            _as_int(current.get("latest_version")),
            _as_int(current.get("public_version")),
        )
        if public is not None and latest is not None:
            return latest == public
        # Neither signal is readable: a draft cannot be proven, so fail closed (treat as live;
        # the caller forks a draft or reports it live-kept) rather than edit a version in place.
        return True

    def _note(self, record_id, locale, kind: str) -> None:
        self._notes.setdefault((str(record_id), str(locale).lower()), []).append(kind)

    def pop_notes(self, record_id, locale) -> list:
        return self._notes.pop((str(record_id), str(locale).lower()), [])

    def _write_payload(self, fields: dict, version: int | None, held: bool) -> dict:
        """Build the PUT body. Partial update: only the fields that carry a usable translation
        are sent, so a held field keeps its stored value. `content` is the documented body field
        for every editor type (`html_content` is deprecated). translation_option "2" (Translated)
        is set only when nothing was held. `publish` is not a PUT field."""
        payload: dict = {}
        if "title" in fields:
            payload["title"] = fields["title"] or ""
        if "body" in fields:
            payload["content"] = fields["body"] or ""
        if not held:
            payload["translation_option"] = str(TRANSLATION_OPTION_TRANSLATED)
        if version is not None:
            payload["version_number"] = version
        return payload

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """The language has no real version yet: PUT creates it (create and update are the same
        call in Document360). Both a translated title and body are required: a version made with
        one of them missing would keep the source-language text there (#640)."""
        fields = dict(fields)
        fields.pop(_HELD_KEY, None)
        if "title" not in fields or "body" not in fields:
            return WriteResult(
                False,
                True,
                message="held-back: no complete translation to create the language with",
            )
        return self._write_version(record_id, locale, fields, None, held=False, created=True)

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        body = dict(fields)
        held = bool(body.pop(_HELD_KEY, False))
        if "title" not in body and "body" not in body:
            return WriteResult(
                False, False, message="held-back: nothing translated to write; stored value kept"
            )
        current = self.get_translation(record_id, locale)
        if current is ABSENT:
            return WriteResult(
                False, False, message="translation disappeared before update; not written"
            )
        return self._write_version(record_id, locale, body, current, held=held, created=False)

    def _write_version(
        self, record_id: str, locale: str, fields: dict, current, *, held: bool, created: bool
    ) -> WriteResult:
        """Write one language version, never changing a PUBLISHED translation in place unless the
        connection publishes.

        A language version that is published (its latest version is the live one) is not edited:
        a new draft is forked from it (needs the team account id) and the machine text goes into
        that draft, so the live text stays as it was until someone publishes the draft. Without
        a team account id the pair is reported as live-kept and nothing is written.
        With Publish on, the written version is published only when no unit was held."""
        record_id = _d360_id(record_id, "article id")
        wire = self._wire_lang(locale)
        draft = not _truthy(self.config.get("publish", "0"))
        user_id = self._user()
        if not draft and not user_id:
            return WriteResult(
                False, created, message="Publish is on but no team account id is configured"
            )
        version = None
        if current is not None:
            version = _as_int(current.get("latest_version"))
            if version is None:
                version = _as_int(current.get("version_number"))
            if self._is_live(current):
                if not user_id:
                    return WriteResult(
                        False,
                        created,
                        message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}",
                    )
                fork = self._fork(record_id, wire, version, user_id)
                if isinstance(fork, str):
                    return WriteResult(False, created, message=fork)
                version = fork
                self._note(record_id, locale, "forked")
        payload = self._write_payload(fields, version, held)
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._base() + self._v2(f"/Articles/{record_id}/{wire}"),
                    json=payload,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, created, message=f"{type(e).__name__}: {e}")
        body = self._json(r)
        if r.status_code not in (200, 201) or (
            isinstance(body, dict) and body.get("success") is False
        ):
            return WriteResult(False, created, message=f"{r.status_code}: {vendor_excerpt(r.text)}")
        data = self._unwrap(body) if isinstance(body, dict) else {}
        data = data if isinstance(data, dict) else {}
        tid = str(data.get("id") or record_id)
        if not draft and not held:
            written = _as_int(data.get("latest_version")) or version
            if written is None:
                again = self.get_translation(record_id, locale)
                written = _as_int(again.get("latest_version")) if again is not ABSENT else None
            ok, msg = self.publish_translation(record_id, locale, written, user_id)
            if not ok:
                self._note(record_id, locale, f"publish-failed: {msg}")
        elif not draft and held:
            self._note(record_id, locale, "held-unpublished")
        return WriteResult(
            True, created, target_id=tid, message="created" if created else "updated"
        )

    def _fork(self, record_id: str, wire: str, version, user_id: str):
        """PUT /v2/Articles/{id}/fork: start a new draft from the published version. Returns the
        new draft's version number, or an error string. Not idempotent (each call makes a version),
        so it is never resent after a 5xx."""
        if version is None:
            return "could not tell which version is published; not changed"
        payload = {"version_number": version, "user_id": user_id, "lang_code": wire}
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._base() + self._v2(f"/Articles/{record_id}/fork"),
                    json=payload,
                    idempotent=False,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return f"fork failed: {type(e).__name__}: {e}"
        body = self._json(r)
        data = self._unwrap(body) if isinstance(body, dict) else None
        new = _as_int(data.get("version_number")) if isinstance(data, dict) else None
        if r.status_code not in (200, 201) or new is None:
            return f"fork failed ({r.status_code}): {vendor_excerpt(r.text)}"
        return new

    def publish_translation(
        self, record_id: str, locale: str, version_number, user_id: str
    ) -> tuple[bool, str]:
        """POST /v2/Articles/{id}/{langCode}/publish {user_id, version_number}: make the draft
        version the published one for this (article, language) pair - a SEPARATE call from the
        content write. A publish failure does not undo the content write, which stays a draft."""
        record_id = _d360_id(record_id, "article id")
        if version_number is None:
            return False, "no version number to publish"
        payload = {
            "user_id": user_id,
            "version_number": int(version_number),
            "publish_message": "Published by the translation connector",
        }
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                self._base() + self._v2(f"/Articles/{record_id}/{self._wire_lang(locale)}/publish"),
                json=payload,
                **self._write_kwargs(),
            )
        body = self._json(r)
        if r.status_code in (200, 201, 204) and not (
            isinstance(body, dict) and body.get("success") is False
        ):
            return True, "published"
        if r.status_code == 400 and "already been published" in self._errors_text(r):
            return True, "already published"
        return False, f"{r.status_code}: {vendor_excerpt(r.text)}"

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, field-unit) for the generic pull view. The
        rich flow is store.document360_translate (writes per-language versions)."""
        from app.filters import get_filter

        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h, _editor = self.extract_units(art, get_filter)
            out.extend(self._pulled_items(units, str(art.get("id")), art.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Document360 writes per-language article versions - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list source articles and return them; the content-hash
        diff in the translate flow is the actual change gate (Document360 has no export cursor)."""
        return self.discover_articles(), 0


def _truthy(value) -> bool:
    return str(value if value is not None else "").strip().lower() in ("1", "true", "yes")


# ===========================================================================
# Translate flow (hook bundle + wrapper live with the connector; store.py keeps a thin
# delegator so store.document360_translate stays importable/monkeypatchable).
# ===========================================================================
import logging

from app.services.connectors import flow

log = logging.getLogger("loc_tms.connectors")


# ------------------------- Document360 hooks -------------------------------
def _d360_preflight(ctx):
    # Fail the run BEFORE any machine translation is paid for when the connection cannot
    # finish: no project version, or Publish on without the team account id it needs.
    ctx.connector._pv()
    if _d360_publish(ctx.cfg) and not str(ctx.cfg.get("user_id") or "").strip():
        raise ConnectorError(
            "Publish is on but no Team account id is configured; Document360 needs one to publish."
        )
    return set(ctx.connector.enabled_locales()), {}


def _d360_publish(cfg):
    return _truthy(cfg.get("publish", "0"))


def _d360_prepare(ctx, art):
    from app.filters import get_filter

    # The live-kept park hook (_d360_parked) needs this run's draft flag and stored hashes.
    ctx.connector._run_draft = ctx.draft
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id"))
    editor = _editor_of(art)
    if editor == "block":
        # The advanced (block) editor has no Markdown or HTML body to round-trip.
        return flow.SkipArticle("unsupported_skipped")
    status = str(art.get("status") if art.get("status") is not None else "").strip().lower()
    if status in _STATUS_DRAFT and not art.get("public_version"):
        # Never published: the source is still a draft, so it is not translated yet.
        return flow.SkipArticle("unpublished_skipped")
    _units, chash, editor = ctx.connector.extract_units(art, get_filter)
    raw_body = _body_of(art, editor)
    if not raw_body.strip():
        # Empty-body article: no translation units; writing back an empty body with a
        # GUESSED editor type risks flipping an article's type -> skip entirely.
        return flow.SkipArticle("empty_skipped")
    body_filter = get_filter("md" if editor == "markdown" else "html")
    shielded_body, body_codes = shield_codes(raw_body)
    raw_title = art.get("title") or ""
    shielded_title, title_codes = shield_codes(raw_title)
    body_units = body_filter.parse(shielded_body.encode("utf-8")).units
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=shielded_title,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": raw_title, "editor": editor},
        doc_title=f"{raw_title[:50] or 'article'} ({aid})",
        extra={
            "body_filter": body_filter,
            "shielded_body_bytes": shielded_body.encode("utf-8"),
            "body_codes": body_codes,
            "title_codes": title_codes,
            "shielded_title": shielded_title,
            "editor": editor,
        },
    )


def _d360_is_suppressed(connector, aid, loc):
    target = connector.get_translation(aid, loc)
    if target is ABSENT:
        return False
    opt = target.get("translation_option") if isinstance(target, dict) else None
    if opt is None:
        # A MISSING field suppresses (missing == translated, the conservative choice).
        return True
    # Needs-work values (0 / 1) re-enqueue; Translated (2) and In Progress (3) are left alone.
    return str(opt).strip().lower() not in _OPTION_NEEDS_WORK


def _title_text(prep) -> str:
    text = getattr(prep, "title_text", None)
    return text if text is not None else prep.extra.get("shielded_title", "")


def _d360_build_payload(prep, loc, seg0_target, mapping):
    """Only fields that carry a usable translation (defect taxonomy B, #640). A held
    (NEEDS_REVIEW / refusal) title is OMITTED, and so is the body when ANY of its text units is
    held (the body cannot be half-written: build() would put source text in the held spots), so
    a stored translation is never overwritten with source-language text."""
    ex = prep.extra
    payload: dict = {}
    held = False
    if seg0_target is not None:
        t_title = strip_write_egress_debris(unshield_codes(seg0_target, ex["title_codes"]))
        payload["title"] = t_title
    elif (_title_text(prep) or "").strip():
        held = True
    else:
        payload["title"] = ""
    body_held = any(
        str(u.p_index) not in mapping and (u.text or "").strip()
        for u in getattr(prep, "body_units", None) or []
    )
    if body_held:
        held = True
    else:
        shielded_out = ex["body_filter"].build(ex["shielded_body_bytes"], mapping).decode("utf-8")
        payload["body"] = strip_write_egress_debris(unshield_codes(shielded_out, ex["body_codes"]))
    for field, source, target in (
        ("title", unshield_codes(ex["shielded_title"], ex["title_codes"]), payload.get("title")),
        (
            "body",
            unshield_codes(ex["shielded_body_bytes"].decode("utf-8"), ex["body_codes"]),
            payload.get("body"),
        ),
    ):
        if target is None:
            continue
        if not template_spans_match(source, target, _MERGE_CODE):
            # The flow records this per-pair failure in the run message and SyncLog;
            # no strategy write or successful hash update occurs.
            return {
                "_template_hold": (
                    f"Document360 template integrity failed in {field}: a merge code was "
                    "missing, added, changed, or duplicated - not written; "
                    "preserve source codes and retry"
                )
            }
    if held:
        payload[_HELD_KEY] = True
    return payload


def _d360_on_write_ok(ctx, prep, loc):
    """Surface what the write did beyond the content: a draft forked from a published
    translation (the live text is unchanged) and publish outcomes (separate call)."""
    aid = prep.aid
    for note in ctx.connector.pop_notes(aid, loc):
        if note == "forked":
            ctx.counters["forked_drafts"] += 1
        elif note == "held-unpublished":
            ctx.errors.append(
                f"{aid}/{loc}/publish: not published, a field is held for review; the written "
                "version stays a draft"
            )
        elif note.startswith("publish-failed"):
            ctx.errors.append(f"{aid}/{loc}/{note.replace('-failed', '')}")


def _d360_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    failures = [f for f in ctx.failures if not _is_live_kept(f)]  # kept live is deliberate
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not failures)
        or (not failures and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _is_live_kept(f) -> bool:
    return str((f or {}).get("error") or "").startswith(LIVE_KEPT_PREFIX)


def _live_kept(ctx) -> list:
    """Pairs left untouched because their translation is live and no draft can be started:
    the ones the park hook skipped before MT plus any refused at write time."""
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept(f)
    ]


def _d360_parked(connector, aid, loc, chash):
    """Flow hook: in draft mode with no team account id, a pair whose translation is already
    PUBLISHED is skipped BEFORE any MT is spent and reported as live-kept (never written, no
    hash, so it is retried once the account id is set or the translation is unpublished).
    Pairs with an unchanged source are left to the normal suppression path."""
    if not getattr(connector, "_run_draft", True) or connector._user():
        return False
    if (getattr(connector, "_run_hashes", None) or {}).get(f"{aid}:{loc}") == chash:
        return False
    try:
        current = connector.get_translation(aid, loc)
    except Exception:
        return False  # probe failed: the write-time guard still refuses a live target
    if current is ABSENT or not connector._is_live(current):
        return False
    if not isinstance(getattr(connector, "_live_kept", None), list):
        connector._live_kept = []
    connector._live_kept.append(
        {
            "article_id": str(aid),
            "locale": str(loc),
            "error": f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}",
        }
    )
    return True


def _d360_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} article version(s) across "
        f"{len(ctx.usable)} langCode(s) from {len(ctx.articles)} article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (version-hash) langCode(s).")
    for key, text in (
        ("empty_skipped", "empty-body article(s) (no translatable content)"),
        ("unpublished_skipped", "article(s) that are not published yet"),
        ("unsupported_skipped", "article(s) in the advanced (block) editor"),
    ):
        if ctx.counters[key]:
            parts.append(f"Skipped {ctx.counters[key]} {text}.")
    if ctx.counters["forked_drafts"]:
        parts.append(
            f"{ctx.counters['forked_drafts']} translation(s) were already published: the "
            "machine text went into a new draft and the published text is unchanged."
        )
    kept = _live_kept(ctx)
    if kept:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in kept[:5])
        parts.append(
            f"Kept {len(kept)} LIVE (published) translation(s) unchanged: {sample}. This "
            "connection writes drafts and has no Team account id to start a draft from the "
            "published text. Set the Team account id, or turn on 'Publish after write'."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} langCode(s) not enabled in the project: "
            f"{', '.join(ctx.rejected)}."
        )
    real_errors = [e for e in ctx.errors if LIVE_KEPT_PREFIX not in e]
    if real_errors:
        parts.append("Errors: " + "; ".join(real_errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())  # Cluster A: loud, per-run
        log.warning(
            "document360_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _d360_build_result(ctx):
    # The counters added by the rebuild appear only when non-zero, so a plain run keeps the
    # result shape the flow-parity goldens froze.
    extra = {
        k: ctx.counters[k]
        for k in ("unpublished_skipped", "unsupported_skipped", "forked_drafts")
        if ctx.counters[k]
    }
    return (
        {
            "articles": len(ctx.articles),
            "locales": ctx.usable,
            "created": ctx.created,
            "updated": ctx.updated,
            "skipped": ctx.skipped,
            "empty_skipped": ctx.counters["empty_skipped"],
            "rejected": ctx.rejected,
            "failures": ctx.failures,
        }
        | extra
        | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})
    )


def document360_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Document360 KB articles into one or more langCodes, each written
    back as the article's per-language VERSION via the T2 SubResourceStrategy.

    Thin wrapper over flow.run_translate_flow. Document360-specific behavior via the
    spec hooks: EDITOR-TYPE BRANCHING (markdown/html filter), MERGE-CODE SHIELDING,
    empty-body / unpublished / block-editor skips, translation_option (stale-flag)-aware
    suppression, held units never written (#640), a published translation never edited in
    place (a draft is forked, or the pair is reported), and the publish toggle (a separate
    call). Poll-only (no webhook), so NO watermark. only_pairs restricts the write loop to
    those (article_id, langCode) pairs.
    """
    spec = flow.TranslateFlowSpec(
        kind="document360",
        no_conn_msg="No such Document360 connection.",
        hash_key="d360_hashes",
        probe_label="Document360",
        preflight=_d360_preflight,
        prepare=_d360_prepare,
        is_suppressed=_d360_is_suppressed,
        build_payload=_d360_build_payload,
        compute_draft=lambda cfg: not _d360_publish(cfg),
        compute_ok=_d360_compute_ok,
        build_message=_d360_build_message,
        build_result=_d360_build_result,
        extra_counters=(
            "empty_skipped",
            "unpublished_skipped",
            "unsupported_skipped",
            "forked_drafts",
            "parked_skipped",
        ),
        parked=_d360_parked,
        on_write_ok=_d360_on_write_ok,
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
