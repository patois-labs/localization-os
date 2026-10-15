"""Zoho Desk knowledge-base connector (T2: a per-locale TRANSLATION object per article).

REBUILT 2026-10-10 (connector parity wave 3) against the vendor's own OpenAPI spec
(github.com/zoho/zohodesk-oas, v1.0/Article.json + ArticleTranslation.json, read 2026-10-10).
The archived version had never run against a real tenant and was wrong in ways the spec shows:

  * a translation is EDITED with PATCH /api/v1/articles/{articleId}/translations/{locale}
    (the old code sent PUT, which the API does not offer) and CREATED with POST
    /api/v1/articles/{articleId}/translations;
  * the body field is `answer` (the old `content` guess is gone);
  * create REQUIRES `authorId`, `locale`, `title`, `answer` and `status`;
  * there is NO departmentId filter on the article list (the department scope is now a client-side
    check on each article), and `limit` caps at 50 with `from` as a 1-based index;
  * the PATCH body is a true partial update (every field optional), so a held field is simply
    left out and keeps its stored value.

AUTH. Zoho OAuth 2.0 "Self Client" (API console): the operator generates a one-time grant code,
exchanges it ONCE for a refresh token (which never expires, until revoked), and gives this
connection `client_id`, `client_secret` and `refresh_token`. The connector mints a 1 hour access
token from `https://accounts.zoho.<dc>/oauth/v2/token` (grant_type=refresh_token) and sends
`Authorization: Zoho-oauthtoken <token>`. Deliberate exception to "one refresh path": the generic
single-flight refresh in store_part2 exists for ROTATING refresh tokens stored as an oauth-mode
secret; a Zoho refresh token does not rotate, so a refresh can never invalidate another one. Minting
is still serialized in-process and cached (Zoho allows only 10 access tokens per refresh token per
10 minutes). A pasted short-lived `access_token` still works for quick tests and for connections
made before the rebuild.

DATA CENTRES. The API host is one of the Zoho Desk hosts below (the operator picks the one that
matches the org; a wrong one answers 401 or empty). The accounts (token) host is DERIVED from the
chosen Desk host, never typed, and both are pinned to this allowlist so a credential can only ever
be sent to a real Zoho host. `orgId` is a mandatory header on every call.

HELD UNITS (#640). A held (NEEDS_REVIEW) title or body unit is OMITTED from the write, never
replaced with the source text. A body with ANY held unit is omitted whole (HTML cannot be half
written). A translation is not created while a unit is held (the flow refuses), a partly written
pair gets no content hash and stays OUTDATED so the next run retries it.

DRAFT MODE never changes a live translation: with `publish` off, a translation that is already
Published is skipped BEFORE machine translation is spent and reported as `live_kept`.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from urllib.parse import urlsplit

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

API_VERSION = "v1"  # Desk KB routes are under /api/v1

# Desk host -> the accounts host of the same data centre. The six verified against
# https://accounts.zoho.com/oauth/serverinfo (2026-10-10) are com, eu, in, com.au, jp, ca; the Desk
# hosts of sa, uk, ae, sg, com.cn and zohohq.in follow the same pattern; the 10-10 review saw
# every Desk host answer an unauthenticated GET with a Zoho 401 JSON, and every accounts host but
# com.cn is listed by serverinfo (live sign-in per data center is still a runbook step).
_DESK_TO_ACCOUNTS = {
    "desk.zoho.com": "accounts.zoho.com",
    "desk.zoho.eu": "accounts.zoho.eu",
    "desk.zoho.in": "accounts.zoho.in",
    "desk.zoho.com.au": "accounts.zoho.com.au",
    "desk.zoho.jp": "accounts.zoho.jp",
    "desk.zohocloud.ca": "accounts.zohocloud.ca",
    "desk.zoho.sa": "accounts.zoho.sa",
    "desk.zoho.uk": "accounts.zoho.uk",
    "desk.zoho.ae": "accounts.zoho.ae",
    "desk.zoho.sg": "accounts.zoho.sg",
    "desk.zoho.com.cn": "accounts.zoho.com.cn",
    # India enterprise data center ("inec" in serverinfo); Desk answers 401 like the rest.
    "desk.zohohq.in": "accounts.zohohq.in",
}

STATUS_DRAFT = "Draft"
STATUS_PUBLISHED = "Published"
_DRAFTISH = frozenset({"draft", "review", "unpublished", "auto-translate failed"})
_PUBLISHED = frozenset({"published", "publish"})

# translationState values that mean "needs work" (spec enum: OUTDATED, UP-TO-DATE, AUTO-TRANSLATE
# FAILED). Anything else, including a missing field, counts as up to date and suppresses.
_STALE_STATES = frozenset({"outdated", "autotranslatefailed"})

_NUMERIC_ID = re.compile(r"[0-9]{1,20}")
# The spec's locale pattern: 2-4 letters plus an optional 2-letter region.
_LOCALE = re.compile(r"[A-Za-z]{2,4}(?:[-_][A-Za-z]{2})?")
# Marker build_payload leaves on a payload whose held field was omitted.
_HELD_KEY = "_held"
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the translation is PUBLISHED in Zoho Desk and this connection writes drafts, so the live "
    "text was left unchanged; set Publish after write = 1 to update published translations"
)


def _is_stale_state(value) -> bool:
    """True when a translationState is an explicit 'needs work' marker. Missing/unknown/up to
    date -> False (suppress: the conservative choice, no re-translate-forever loop)."""
    if value is None:
        return False
    v = re.sub(r"[^a-z]", "", str(value).lower())
    return v in _STALE_STATES


def _is_published(tr) -> bool:
    """Fail closed: a translation whose status is not explicitly a draft-like state is live."""
    if not isinstance(tr, dict):
        return False
    return str(tr.get("status") or "").strip().lower() not in _DRAFTISH


def _zo_id(value, what: str = "id") -> str:
    """A Zoho Desk id is digits only. Anything else (`..`, `%2e`, `?`, `#`, CR/LF, unicode digits)
    is refused before it can reach a URL path or header (defect taxonomy A)."""
    text = str(value if value is not None else "").strip()
    if not _NUMERIC_ID.fullmatch(text) or not text.isascii():
        raise ConnectorError(f"Invalid Zoho Desk {what}: expected digits only.")
    return text


def _zo_locale(value) -> str:
    text = str(value if value is not None else "").strip()
    if not _LOCALE.fullmatch(text) or not text.isascii():
        raise ConnectorError("Invalid Zoho Desk locale code.")
    return text


def _scope_ok(cfg: dict, art: dict) -> bool:
    """Is this article inside the connection's configured department / category? Fails closed
    when a configured scope cannot be read from the article. Applies to listed and by-id ones."""
    cfg = cfg or {}
    dept = str(cfg.get("department_id") or "").strip()
    if dept and str(art.get("departmentId") or "") != dept:
        return False
    cat = str(cfg.get("category_id") or "").strip()
    return not (cat and str(art.get("categoryId") or "") != cat)


SPEC = ConnectorSpec(
    kind="zoho",
    name="Zoho Desk",
    icon="\U0001f5c3",  # card index box (knowledge base)
    cat="Knowledge base / help center",
    blurb="Translate Zoho Desk knowledge-base articles into any language. Each "
    "translation is written back as the article's per-locale translation "
    "(POST/PATCH /api/v1/articles/{id}/translations), so the source "
    "article is never overwritten. Draft by default.",
    config_fields=[
        FieldSpec(
            "api_domain",
            "Desk host (data center)",
            "The Zoho Desk host of your data center: desk.zoho.com (US), desk.zoho.eu (EU), "
            "desk.zoho.in (India), desk.zoho.com.au (Australia), desk.zoho.jp (Japan), "
            "desk.zohocloud.ca (Canada). The sign-in host is derived from it.",
            required=True,
            placeholder="desk.zoho.com",
        ),
        FieldSpec(
            "org_id",
            "Organization id (orgId)",
            "The Zoho Desk organization id (digits), sent as the mandatory orgId header.",
            required=True,
            placeholder="600000000000",
        ),
        FieldSpec(
            "department_id",
            "Department id",
            "Only translate articles of this department (optional; checked on every article).",
            required=False,
            placeholder="900000000000",
        ),
        FieldSpec(
            "category_id",
            "Category id",
            "Only translate articles directly in this KB category (optional; checked on every "
            "article, including ones named by id).",
            required=False,
            placeholder="800000000000",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The authoring locale code (e.g. 'en'). Articles in another language are skipped. "
            "Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Zoho locale codes to write (must be enabled for the KB).",
            required=False,
            placeholder="de, fr, es",
        ),
        FieldSpec(
            "author_id",
            "Author agent id",
            "Zoho requires an author on a new translation. Blank = the source article's author.",
            required=False,
            placeholder="4000000000059",
        ),
        FieldSpec(
            "publish",
            "Publish after write",
            "'1' = write translations as Published (only when the source article is Published); "
            "'0' = leave them Draft (default). A translation that is already Published is left "
            "unchanged and reported while this is '0'.",
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
            "client_id",
            "OAuth client id",
            "From the Zoho API console, Self Client, Client Secret tab.",
            required=False,
            secret=True,
            placeholder="1000.ABCDEF",
        ),
        FieldSpec(
            "client_secret",
            "OAuth client secret",
            "From the same Self Client page.",
            required=False,
            secret=True,
            placeholder="client secret",
        ),
        FieldSpec(
            "refresh_token",
            "OAuth refresh token",
            "Exchange the one-time grant code once (steps below); the refresh token does not "
            "expire until revoked.",
            required=False,
            secret=True,
            placeholder="1000.abcd...",
        ),
        FieldSpec(
            "access_token",
            "Access token (optional, short-lived)",
            "Only for a quick test: a pasted access token lasts 1 hour. Leave blank when the "
            "refresh token above is set.",
            required=False,
            secret=True,
            placeholder="1000.abcd...",
        ),
    ],
    setup_steps=[
        "In the Zoho API console (api-console.zoho.com, sign in with the data-center site that "
        "matches your org) choose Self Client, then Generate Code with the scopes "
        "Desk.articles.READ,Desk.articles.CREATE,Desk.articles.UPDATE and a 10 minute lifetime.",
        "Exchange that one-time code for a refresh token (POST /oauth/v2/token on your accounts "
        "host, grant_type=authorization_code) and paste client id, client secret and refresh "
        "token above. The grant code itself is never stored.",
        "Set the Desk host to your data center (desk.zoho.eu for the EU, and so on). The "
        "sign-in host is derived from it.",
        "Set the Organization id (Setup, Developer Space, API in Zoho Desk) and the target "
        "locales (they must be enabled for the KB).",
        "Zoho Desk has webhooks, but this connector polls: run on a schedule so modifiedTime and "
        "the translationState flag keep translations fresh.",
    ],
    other_side="Zoho Desk change detection polls modifiedTime + the per-translation "
    "translationState stale flag (webhook intake is a documented follow-up). "
    "Every call routes to the pinned Zoho host of your data center.",
    docs_url="https://desk.zoho.com/DeskAPIDocument",
    supports_oauth=False,  # Self Client refresh token; the browser authorize flow is a follow-up.
    archived=False,  # un-archived 2026-10-10 (connector parity rebuild); live QA is pending  # noqa: E501
)


# Process-wide access-token cache: Zoho allows 10 access tokens per refresh token per 10 minutes,
# and every run builds a fresh connector. Key carries only a HASH of the refresh token.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict = {}
_TOKEN_SKEW = 120
# Zoho: 10 mints per refresh token per 10 minutes, 10 active access tokens per refresh token (the
# 11th invalidates the oldest). A 401 may force one re-mint, but never inside this window, so a
# call that is refused for another reason cannot burn the mint budget in a loop.
_REMINT_MIN_AGE = 60


def _clock() -> float:
    return time.time()


def _error_code(resp: httpx.Response) -> str:
    """The token endpoint's `error` code (a short word), never the body: the body can echo input."""
    try:
        code = str((resp.json() or {}).get("error") or "")
    except Exception:
        code = ""
    return code if re.fullmatch(r"[A-Za-z0-9_ .-]{1,40}", code) else "unknown"


def _http_err(r: httpx.Response, limit: int = 140) -> str:
    """'status: body' for an error answer; when the body names the organization, say that the
    orgId is the problem. The official docs send orgId as a header, the OpenAPI file lists it as a
    query parameter: the header is sent, and an orgId complaint is reported as such instead of
    as a generic failure (runbook MANDATORY check)."""
    text = (r.text or "")[:limit]
    if r.status_code in (400, 401, 403, 404, 422) and re.search(
        r"org(anization)?[ _-]?id", text, re.I
    ):
        return (
            f"{r.status_code}: Zoho Desk did not accept the organization id (orgId header): check "
            f"the Organization id on the connection. Zoho said: {text}"
        )
    return f"{r.status_code}: {text}"


class ZohoConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="zoho",
        topology=Topology.T2,
        auth=(AuthKind.OAUTH3LO,),  # Self Client refresh token -> Zoho-oauthtoken access token
        api_version=API_VERSION,
        rich_text=RichText.HTML,
        regions="dynamic-from-token",  # per-connection Desk host, pinned to the Zoho allowlist
        change_signals=ChangeSignals(
            webhook_sig=None,  # webhooks EXIST but we POLL first (no verifier wired)
            poll_field="modifiedTime",
            stale_flag="translationState",
            thin_payload=False,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: (
                (rec or {}).get("source_locale") or self._source_locale()
            )
        )

    # --- config ---
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en").strip().lower() or "en"

    def _org_id(self) -> str:
        oid = (self.config.get("org_id") or "").strip()
        if not oid:
            raise ConnectorError("Zoho Desk organization id (orgId) is required.")
        return _zo_id(oid, "organization id")

    def _desk_host(self) -> str:
        """The pinned Desk host. Accepts a bare host or an https:// URL (http:// is upgraded),
        refuses a path, port, userinfo or any host outside the Zoho allowlist."""
        raw = (self.config.get("api_domain") or "").strip()
        if not raw:
            raise ConnectorError(
                "Zoho Desk host (api_domain) is required, e.g. desk.zoho.com or desk.zoho.eu."
            )
        raw = raw.rstrip("/")
        if raw.startswith("http://"):
            raw = "https://" + raw[len("http://") :]
        elif not raw.startswith("https://"):
            raw = "https://" + raw
        parts = urlsplit(raw)
        if parts.path.strip("/") or parts.query or parts.fragment:
            raise ConnectorError(
                "Zoho Desk host (api_domain) must be a host, not a URL with a path "
                "- e.g. desk.zoho.eu, not desk.zoho.eu/api/v1/articles."
            )
        try:
            port = parts.port
        except ValueError:
            port = -1
        host = (parts.hostname or "").lower()
        if parts.username or parts.password or port not in (None, 443) or not parts.hostname:
            raise ConnectorError("Zoho Desk host (api_domain) must be a plain host name.")
        if host not in _DESK_TO_ACCOUNTS:
            raise ConnectorError(
                f"Zoho Desk host must be one of: {', '.join(sorted(_DESK_TO_ACCOUNTS))}."
            )
        return host

    def _base(self) -> str:
        """https://<pinned desk host>, validated by the shared SSRF guard on every call."""
        return _validate_target("https://" + self._desk_host())

    def _accounts_base(self) -> str:
        return _validate_target("https://" + _DESK_TO_ACCOUNTS[self._desk_host()])

    # --- auth ---
    def _token_key(self) -> tuple:
        rt = (self.secret.get("refresh_token") or "").strip()
        return (
            self._desk_host(),
            (self.secret.get("client_id") or "").strip(),
            hashlib.sha256(rt.encode("utf-8")).hexdigest()[:24],
        )

    def _mint_token(self, c) -> str:
        """Exchange the (non-rotating) refresh token for a 1 hour access token. Serialized and
        cached in-process. The secret values go only in the POST body; errors name Zoho's error
        code, never the body, the client id or any token."""
        s = self.secret
        cid, csec, rtok = (
            (s.get("client_id") or "").strip(),
            (s.get("client_secret") or "").strip(),
            (s.get("refresh_token") or "").strip(),
        )
        if not (cid and csec and rtok):
            raise ConnectorError(
                "Zoho Desk needs the OAuth client id, client secret and refresh token "
                "(or a pasted access token for a quick test)."
            )
        key = self._token_key()
        with _TOKEN_LOCK:
            hit = _TOKEN_CACHE.get(key)
            if hit and hit[1] - _TOKEN_SKEW > _clock():
                return hit[0]
            r = self._request(
                c,
                "POST",
                self._accounts_base() + "/oauth/v2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cid,
                    "client_secret": csec,
                    "refresh_token": rtok,
                },
                headers={"Accept": "application/json"},
                idempotent=False,  # a resent mint would burn a token: retry only on 429
            )
            token, expires_in = "", 3600
            if r.status_code == 200:
                try:
                    body = r.json() or {}
                except Exception:
                    body = {}
                token = str(body.get("access_token") or "")
                try:
                    expires_in = int(body.get("expires_in") or 3600)
                except (TypeError, ValueError):
                    expires_in = 3600
            if not token or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
                # Zoho answers some token errors with HTTP 200 and {"error": "invalid_code"}.
                raise ConnectorError(
                    f"Zoho refused the refresh token ({r.status_code}, {_error_code(r)}): check "
                    "the client id, client secret, refresh token and that the Desk host is in the "
                    "same data center as the Self Client."
                )
            _TOKEN_CACHE[key] = (token, _clock() + expires_in, _clock())
            return token

    def _forget_token(self, used: str | None) -> bool:
        """Drop the cached token after a 401, but only if it is still the one that was refused (a
        concurrent run may already have minted a fresh one) and it is older than the re-mint
        window. True when a fresh mint is worth trying."""
        with _TOKEN_LOCK:
            key = self._token_key()
            hit = _TOKEN_CACHE.get(key)
            if hit is None:
                return True
            if used is not None and hit[0] != used:
                return True  # someone already replaced it: just retry with the new one
            if _clock() - hit[2] < _REMINT_MIN_AGE:
                return False
            _TOKEN_CACHE.pop(key, None)
            return True

    def _uses_refresh(self) -> bool:
        s = self.secret
        return any(
            (s.get(k) or "").strip() for k in ("refresh_token", "client_id", "client_secret")
        )

    def _headers(self, c, *, write: bool = False) -> dict:
        if self._uses_refresh():
            token = self._mint_token(c)
            self._used_token = token
        else:
            token = (self.secret.get("access_token") or "").strip()
            if not token:
                raise ConnectorError("Zoho Desk OAuth credentials are required.")
            if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
                raise ConnectorError("Zoho Desk access token is not valid.")
        h = {
            "Authorization": f"Zoho-oauthtoken {token}",
            "orgId": self._org_id(),
            "Accept": "application/json",
        }
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _call(self, c, method: str, path: str, *, idempotent: bool = True, **kw):
        """One authed call. A 401 on a minted token (revoked or expired early) drops the cached
        token and retries once with a fresh one."""
        write = method in ("POST", "PATCH", "PUT")
        for attempt in (0, 1):
            r = self._request(
                c,
                method,
                self._base() + path,
                idempotent=idempotent,
                headers=self._headers(c, write=write),
                **kw,
            )
            # A 401 means the request was not processed, so even a create is safe to resend.
            if (
                r.status_code == 401
                and attempt == 0
                and self._uses_refresh()
                and self._forget_token(getattr(self, "_used_token", None))
            ):
                continue
            return r
        return r

    def _get(self, c, path, **kw):
        return self._call(c, "GET", path, **kw)

    def _v1(self, path: str) -> str:
        return f"/api/{API_VERSION}{path}"

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get(c, self._v1("/articles"), params={"limit": 1})
            if r.status_code == 200:
                return True, "Connected to Zoho Desk (KB articles reachable)."
            if r.status_code in (401, 403):
                return False, (
                    f"Zoho Desk rejected the credentials/orgId ({r.status_code}) - check the "
                    "token scopes (Desk.articles.*), the orgId, and that the Desk host matches "
                    "your data center."
                )
            return False, f"Zoho Desk returned {_http_err(r, 160)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: KB root categories (id + name). Best-effort."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, self._v1("/kbRootCategories"), params={"limit": 50})
            if r.status_code != 200:
                return []
            for cat in r.json().get("data") or []:
                cid = str(cat.get("id") or "")
                if not _NUMERIC_ID.fullmatch(cid):
                    continue
                name = cat.get("name") or cid
                out.append({"id": cid, "name": f"{name} (KB {cid})", "tables": []})
        except Exception:
            return out
        return out

    # --- discover: enabled locales + articles ---
    def enabled_locales(self, effective: list | None = None) -> list[str]:
        """The locale set a write may target. Zoho exposes no OAuth-reachable list of the KB's
        enabled languages (GET /api/v1/languages needs a browser session), so enabled is the
        configured and effective locales plus the source; a locale the KB lacks is refused by Zoho
        at write time and reported per pair. Locales that are not well-formed codes are dropped
        here so they can never reach a URL."""
        cfg_targets = [
            x.strip().lower()
            for x in (self.config.get("target_locales") or "").replace(";", ",").split(",")
            if x.strip()
        ]
        eff = [str(x).strip().lower() for x in (effective or []) if str(x).strip()]
        found = sorted(set(cfg_targets + eff + [self._source_locale()]))
        ok = [x for x in found if _LOCALE.fullmatch(x) and x.isascii()]
        return ok or [self._source_locale()]

    def _paginate(self, c, path: str, params: dict, cap: int) -> list[dict]:
        """Page through a Zoho Desk list: `from` (1-based index) + `limit` (hard cap 50). Stops on
        a short or empty page, or when a page adds no new ids (a server that ignores `from`)."""
        out: list[dict] = []
        seen_ids: set = set()
        params = dict(params or {})
        page_size = min(50, cap)
        frm = 1
        for _ in range(50):  # bounded pages per run
            p = dict(params, **{"from": frm, "limit": page_size})
            r = self._get(c, path, params=p)
            if r.status_code != 200:
                if frm > 1 and out and 400 <= r.status_code < 500:
                    break
                raise ConnectorError(f"Zoho Desk list {path} failed ({_http_err(r, 120)})")
            data = r.json().get("data") or []
            new_rows = []
            for row in data:
                rid = str(row.get("id")) if isinstance(row, dict) else None
                if rid is not None and rid in seen_ids:
                    continue
                if rid is not None:
                    seen_ids.add(rid)
                new_rows.append(row)
            if data and not new_rows:
                break
            out.extend(self._keep_rows(new_rows))
            if len(out) >= cap or len(data) < page_size:
                break
            frm += page_size
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source articles, scoped by the connection's department/category. Explicit article_ids
        (the retry path) are digits-only and must sit inside the same scope: an article outside it
        is dropped like a missing one. Each listed row is re-fetched in full so extract sees the
        body."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cfg = self.config or {}
        with self._client() as c:
            if article_ids:
                out = []
                for aid in list(article_ids)[:cap]:
                    art = self._fetch_article(c, _zo_id(aid, "article id"))
                    if art is not None and _scope_ok(cfg, art):
                        out.append(self._charge_record(art))
                return out
            params: dict = {}
            cat = (cfg.get("category_id") or "").strip()
            if cat:
                params["categoryId"] = _zo_id(cat, "category id")
            dept = (cfg.get("department_id") or "").strip()
            if dept:
                _zo_id(dept, "department id")
            listing = self._paginate(c, self._v1("/articles"), params, cap)
            out = []
            for row in listing:
                aid = str(row.get("id") or "")
                if not aid:
                    continue
                art = self._fetch_article(c, _zo_id(aid, "article id"))
                if art is not None and _scope_ok(cfg, art):
                    out.append(self._charge_record(art))
        return out

    def _fetch_article(self, c, article_id: str) -> dict | None:
        """GET /api/v1/articles/{id} -> the full article dict, or None on 404."""
        article_id = _zo_id(article_id, "article id")
        r = self._get(c, self._v1(f"/articles/{article_id}"))
        if r.status_code == 200:
            art = r.json()
            if isinstance(art, dict):
                art.setdefault("id", article_id)
                return art
        return None

    def show_article(self, article_id: str) -> dict | None:
        with self._client() as c:
            return self._fetch_article(c, str(article_id))

    # --- extract ---
    def extract_units(self, article: dict, html_filter) -> tuple[list[dict], str, str]:
        """Title (plain) + `answer` body (HTML, shared html filter). Returns (units, content_hash,
        body_key); body_key is always `answer` (the documented key). Unit 0 = title, 1+ = body."""
        title = article.get("title") or ""
        body = article.get("answer") or ""
        body_units = html_filter.parse(body.encode("utf-8")).units
        units = [{"p_index": 0, "text": title, "field": "title"}]
        for i, u in enumerate(body_units):
            units.append({"p_index": 1 + i, "text": u.text, "field": "body"})
        return units, _content_hash(title, body), "answer"

    # --- write-back client (the SubResourceClient the strategy drives) ---
    def get_translation(self, record_id: str, locale: str):
        """GET /api/v1/articles/{id}/translations/{locale} -> the translation dict, or ABSENT on
        404. The EXPLICIT existence probe (never a content read) the create-vs-update decision
        needs."""
        record_id, locale = _zo_id(record_id, "article id"), _zo_locale(locale)
        with self._client() as c:
            r = self._get(c, self._v1(f"/articles/{record_id}/translations/{locale}"))
        if r.status_code == 200:
            body = r.json()
            return body if isinstance(body, dict) and body else ABSENT
        if r.status_code == 404:
            return ABSENT
        raise ConnectorError(f"Zoho translation probe {record_id}/{locale} -> {_http_err(r, 120)}")

    def is_live_published(self, record_id: str, locale: str) -> bool:
        """Is the stored (article, locale) translation Published? One GET, no content read."""
        return _is_published(self.get_translation(record_id, locale))

    @staticmethod
    def _ok_id(resp: httpx.Response, fallback: str) -> str:
        try:
            return str((resp.json() or {}).get("id") or fallback)
        except Exception:
            return str(fallback)

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """POST /articles/{id}/translations. Zoho requires locale, title, answer, status and
        authorId, so a create with no usable title or body (held for review) is refused (#640):
        never an empty or source-language translation. Non-idempotent: retried on 429 only."""
        record_id, locale = _zo_id(record_id, "article id"), _zo_locale(locale)
        f = dict(fields)
        f.pop(_HELD_KEY, None)
        title, answer = (f.get("title") or "").strip(), f.get("body") or ""
        if not title or not str(answer).strip():
            return WriteResult(
                False, True, message="held-back: no translated title and body to create the locale"
            )
        author = str(f.get("author_id") or "").strip()
        if not author:
            return WriteResult(
                False,
                True,
                message="no author for the new translation: set Author agent id on the connection",
            )
        try:
            author = _zo_id(author, "author id")
        except ConnectorError as e:
            return WriteResult(False, True, message=str(e))
        payload = {
            "locale": locale,
            "title": f["title"],
            "answer": answer,
            "authorId": author,
            "status": (
                STATUS_PUBLISHED
                if (not f.get("draft", True) and f.get("source_published"))
                else STATUS_DRAFT
            ),
            "translationState": "UP-TO-DATE",
        }
        try:
            with self._client() as c:
                r = self._call(
                    c,
                    "POST",
                    self._v1(f"/articles/{record_id}/translations"),
                    json=payload,
                    idempotent=False,
                )
        except httpx.TransportError as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return WriteResult(True, True, target_id=self._ok_id(r, record_id), message="created")
        return WriteResult(False, True, message=_http_err(r))

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """PATCH /articles/{id}/translations/{locale}: a true partial update, so a held field is
        left out and keeps its stored value. In draft mode the stored translation is read first and
        a PUBLISHED one is never touched (it would put unreviewed machine text live). A partly held
        pair does not clear the OUTDATED flag."""
        record_id, locale = _zo_id(record_id, "article id"), _zo_locale(locale)
        f = dict(fields)
        held = bool(f.pop(_HELD_KEY, False))
        body: dict = {}
        if f.get("title") is not None:
            body["title"] = f["title"]
        if f.get("body") is not None:
            body["answer"] = f["body"]
        if not body:
            return WriteResult(
                False, False, message="held-back: nothing translated to write; stored value kept"
            )
        draft = f.get("draft", True)
        if draft:
            current = self.get_translation(record_id, locale)
            if current is ABSENT:
                return WriteResult(
                    False, False, message="translation disappeared before update; not written"
                )
            if _is_published(current):
                return WriteResult(False, False, message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}")
        elif f.get("source_published") and not held:
            # Publish mode promotes a translation only when every unit is translated: a held pair
            # keeps its stored status, so a part-stale translation never goes live (#640).
            body["status"] = STATUS_PUBLISHED
        if not held:
            body["translationState"] = "UP-TO-DATE"
        try:
            with self._client() as c:
                r = self._call(
                    c, "PATCH", self._v1(f"/articles/{record_id}/translations/{locale}"), json=body
                )
        except httpx.TransportError as e:
            return WriteResult(False, False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return WriteResult(True, False, target_id=self._ok_id(r, record_id), message="updated")
        return WriteResult(False, False, message=_http_err(r))

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        from app.filters import get_filter

        html = get_filter("html")
        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h, _bk = self.extract_units(art, html)
            out.extend(self._pulled_items(units, str(art.get("id")), art.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Zoho Desk writes per-locale translation objects - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only: re-list source articles; the per-pair hash + translationState decide."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow (moved here from store.py in the Wave 3 decomposition).
# ===========================================================================
import logging

from app.services.connectors import flow

log = logging.getLogger("loc_tms.connectors")


def _zo_preflight(ctx):
    from app.services.connectors.store import effective_locales

    own_locales = [l.strip().lower() for l in effective_locales(ctx.conn)]  # noqa: E741
    return set(ctx.connector.enabled_locales(own_locales)), {}


def _zo_prepare(ctx, art):
    from app.filters import get_filter

    html = get_filter("html")
    # The live-kept park hook (_zo_parked) needs this run's draft flag and stored hashes.
    ctx.connector._run_draft = ctx.draft
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id"))
    src = ctx.connector._source_locale()
    art_locale = str(art.get("locale") or "").strip().lower()
    if art_locale and art_locale != src:
        return flow.SkipArticle("other_locale_skipped")
    _units, chash, _bk = ctx.connector.extract_units(art, html)
    raw_body = art.get("answer") or ""
    if not raw_body.strip():
        return flow.SkipArticle("empty_skipped")
    body_units = html.parse(raw_body.encode("utf-8")).units
    raw_title = art.get("title") or ""
    author = str(
        (ctx.cfg or {}).get("author_id") or art.get("authorId") or art.get("ownerId") or ""
    ).strip()
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=raw_title,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": raw_title, "body_key": "answer"},
        doc_title=f"{raw_title[:50] or 'article'} ({aid})",
        extra={
            "html": html,
            "body_bytes": raw_body.encode("utf-8"),
            "raw_title": raw_title,
            "author_id": author,
            "source_published": str(art.get("status") or "").strip().lower() in _PUBLISHED,
        },
    )


def _zo_is_suppressed(connector, aid, loc):
    target = connector.get_translation(aid, loc)
    if target is ABSENT:
        return False
    state = target.get("translationState") if isinstance(target, dict) else None
    # translationState PREFERRED over the hash: an explicit outdated marker re-enqueues.
    return not _is_stale_state(state)


def _zo_build_payload(prep, loc, seg0_target, mapping):
    """Only fields that carry a usable translation. A held title is OMITTED, and so is the body
    when ANY of its text units is held (the HTML cannot be half written), so a stored translation
    is never overwritten with source-language text (defect taxonomy B, #640)."""
    ex = prep.extra
    payload: dict = {
        "author_id": ex.get("author_id", ""),
        "source_published": bool(ex.get("source_published")),
    }
    held = False
    if seg0_target is not None:
        payload["title"] = seg0_target
    elif (getattr(prep, "title_text", "") or "").strip():
        held = True
    body_units = getattr(prep, "body_units", None) or []
    body_held = any(str(u.p_index) not in mapping and (u.text or "").strip() for u in body_units)
    if body_held:
        held = True
    elif seg0_target is not None or mapping or not body_units:
        payload["body"] = ex["html"].build(ex["body_bytes"], mapping).decode("utf-8")
    if held:
        payload[_HELD_KEY] = True
    return payload


def _zo_parked(connector, aid, loc, chash):
    """Flow hook: in draft mode a pair whose translation is already PUBLISHED is skipped BEFORE
    any MT is spent and reported as kept live (never written, no hash). update_translation repeats
    the check at write time (an explicit retry bypasses this hook)."""
    if not getattr(connector, "_run_draft", True):
        return False
    if (getattr(connector, "_run_hashes", None) or {}).get(f"{aid}:{loc}") == chash:
        return False  # unchanged source: the normal suppression path decides
    try:
        live = connector.is_live_published(aid, loc)
    except Exception:
        return False  # probe failed: the write-time guard still refuses a live target
    if live:
        if not isinstance(getattr(connector, "_live_kept", None), list):
            connector._live_kept = []
        connector._live_kept.append(
            {
                "article_id": str(aid),
                "locale": str(loc),
                "error": f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}",
            }
        )
    return live


def _is_live_kept_text(text) -> bool:
    return LIVE_KEPT_PREFIX in str(text or "")


def _live_kept(ctx):
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept_text(f.get("error"))
    ]


def _real_errors(ctx):
    return [e for e in ctx.errors if not _is_live_kept_text(e)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_live_kept_text(f.get("error"))]


def _zo_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    errors, failures = _real_errors(ctx), _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not errors)
        or (
            not errors
            and not failures
            and (ctx.created + ctx.updated + ctx.skipped) == 0
            and ctx.usable
        )
    )


def _zo_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translation(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash/translationState) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} empty-body article(s) "
            f"(no translatable content)."
        )
    if ctx.counters["other_locale_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['other_locale_skipped']} article(s) not written in the "
            f"source locale ({ctx.connector._source_locale()})."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not enabled for the KB: "
            f"{', '.join(ctx.rejected)}."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) translation(s) unchanged: {sample}. This "
            "connection writes drafts, so new machine text is never written into a published "
            "translation. Set Publish after write = 1 to update live translations."
        )
    errors = _real_errors(ctx)
    if errors:
        parts.append("Errors: " + "; ".join(errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())
        log.warning(
            "zoho_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _zo_build_result(ctx):
    res = {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "failures": ctx.failures,
    }
    if ctx.counters["other_locale_skipped"]:
        res["other_locale_skipped"] = ctx.counters["other_locale_skipped"]
    if _live_kept(ctx):
        res["live_kept"] = _live_kept(ctx)
    return res


def zoho_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Zoho Desk KB articles into one or more locales, each written back as a
    per-locale TRANSLATION object via the T2 SubResourceStrategy (thin wrapper over
    flow.run_translate_flow). Zoho-specific hooks: locale pre-filter, empty-body and
    other-language skips, translationState-preferred suppression, the publish toggle, the
    #640 held-unit rules and the draft-mode live-kept guard. Poll-first, so NO watermark.
    only_pairs restricts the write loop to those (article_id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="zoho",
        no_conn_msg="No such Zoho Desk connection.",
        hash_key="zoho_hashes",
        probe_label="Zoho",
        preflight=_zo_preflight,
        prepare=_zo_prepare,
        is_suppressed=_zo_is_suppressed,
        build_payload=_zo_build_payload,
        compute_draft=lambda cfg: str(cfg.get("publish", "0")).strip() not in ("1", "true", "yes"),
        compute_ok=_zo_compute_ok,
        build_message=_zo_build_message,
        build_result=_zo_build_result,
        extra_counters=("empty_skipped", "other_locale_skipped", "parked_skipped"),
        parked=_zo_parked,
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
