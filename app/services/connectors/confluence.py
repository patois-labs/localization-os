"""Confluence connector, Confluence Cloud REST API v2 (pages) with email + API token or OAuth 3LO.

REBUILD (connector parity, 2026-10-10). The archived 2026-07 connector called the v1 content
endpoints (`/wiki/rest/api/content...`, deprecated by Atlassian in favour of v2) and ran its
own bespoke translate loop in store_part5. This module is the current-standard version:

  * Topology T3 (a page per language). Confluence has NO native localization (Marketplace
    apps only), so a translation is a CHILD page of the source titled per `title_pattern`
    (default "[{loc}] {title}") and the connector owns the source->target id map
    (`strategy.DocPerLocaleStrategy` + `idmap`: reserve-then-fill, 7-case reconciliation,
    needs_link fail-closed when a page of that title already exists and our map never made
    it). This replaces the old `confluence_children` title-matching heuristics.
  * Write-back rides the shared flow engine (`flow.run_translate_flow`): WriteBudget, egress
    guard, content-hash suppression, NEEDS_REVIEW exclusion and the #640 held-back rules
    exist once there. Confluence only supplies hooks.
  * v2 for everything that has a v2 form: spaces (`GET /spaces?keys=`), pages (`GET/POST
    /pages`, `GET/PUT /pages/{id}`), space listing (`/spaces/{id}/pages`), children
    (`/pages/{id}/direct-children`), ancestors and labels. v2 has NO CQL, so the optional
    `cql` scope still goes through the v1 search endpoint (`/rest/api/search`), which returns
    ids only; the pages themselves are then read through v2.
  * Auth is unchanged (basic email + API token, or the Atlassian 3LO gateway). Gateway paths:
    v2 is `/ex/confluence/{cloudid}/wiki/api/v2` and v1 search is `/ex/confluence/{cloudid}
    /rest/api` (NO `/wiki`, .claude/rules/connectors.md). The v2 gateway prefix is the single
    constant `_GW_V2` and is MANDATORY-1 in the live runbook, because it is the one path the
    docs do not state.

Held-back (#640): a title or body unit without a usable translation never ships. A new child
page is not created while any unit is held (flow stage 9); an UPDATE writes only the part that
IS translated and keeps the stored value of the held part (the page is read first, so the
stored title or body is sent back unchanged); no content hash is recorded, so the pair is
retried after approval.

EXISTING CONNECTIONS keep working with no data migration: the child page ids the old loop
recorded in `confluence_children` are adopted into the id map as `done` rows the first time a
pair is seen (only recorded ids are adopted, a same-title page nobody recorded is NOT adopted:
it surfaces as needs_link). Config keys are unchanged. Review gate: an adopted pair's legacy
`confluence_hashes` entry is INVALIDATED (the old loop could leave source-language text in a
child it created while a unit was held, and still recorded the hash), so each adopted page is
rewritten exactly once (`legacy_resync` in the run result, one-time MT spend).

A new child page is created in ONE request already carrying the translated title and body
(never an empty published page); the strategy's fill step is then a no-op.

ACCEPTED-BY-DESIGN: if a human hand-edits a machine-created child page, the next run's
content-hash suppression (keyed on the SOURCE page) will not see that drift and a later
source edit overwrites it. Clear the `confluence_hashes` entry to force a re-sync.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.connectors import flow, oauth, webhooks
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    fetch_cap,
    harvest_context_field,
    reject_unwrappable_query,
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
from app.services.connectors.strategy import DocPerLocaleStrategy, TargetUnverifiable, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

_HELD_BACK_PREFIX = "held-back:"
# Review gate: shown in the run result, the connection status and `connector test` when an
# OAuth token lacks the v2 granular scopes (every Confluence OAuth connection made before the
# Connector parity rebuild). Token-mode (email + API token) connections never see it.
_OAUTH_RECONNECT_MSG = (
    "Confluence rejected this connection's OAuth token ({code}). Confluence now uses the v2 "
    "API, which needs new permissions: open this connection and press Reconnect with OAuth "
    "once to grant them (an Atlassian admin must first enable read:page, write:page, "
    "read:space, read:label, read:hierarchical-content and read:content.metadata for "
    "Confluence on the Atlassian app). Nothing was written."
)
_PAGE_ID_RE = re.compile(r"^[0-9]{1,20}$")
_SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_~.\-]{1,255}$")
_GW_V2 = "/wiki/api/v2"  # MANDATORY-1 (runbook): confirm on a live OAuth connection
_LIST_LIMIT = 100
_MAX_ANCESTORS = 5
_MAX_LIST_PAGES = 200  # hard bound on cursor loops

SPEC = ConnectorSpec(
    kind="confluence",
    name="Confluence",
    icon="📖",
    cat="Knowledge base / help center",
    blurb="Translate Confluence pages (help articles, runbooks, policies) into any language. "
    "Each translation is created as a child page, so the original stays intact.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Site URL",
            "Your Atlassian site, e.g. https://your-team.atlassian.net.",
            placeholder="https://your-team.atlassian.net",
        ),
        FieldSpec(
            "space_key",
            "Space key",
            "Default space to translate (optional if you pass page ids). "
            "Also the SCOPE: when set, translation is confined to this space and outside page ids "
            "are skipped. Blank = any space the token can reach. "
            "Only an admin can change it once the connection holds a saved credential.",
            required=False,
            placeholder="HELP",
        ),
        FieldSpec(
            "cql",
            "CQL filter",
            "Optional Confluence Query Language to pick pages. Also the "
            "SCOPE for free-form queries: a caller-supplied CQL is ANDed against this, so a "
            "request can never match pages outside it. "
            "Only an admin can change it once the connection holds a saved credential.",
            required=False,
            placeholder="type=page AND label='translate'",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the pages. Never written as a target.",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated languages to create.",
            required=False,
            placeholder="es-MX, de-DE, fr-FR",
        ),
        FieldSpec(
            "title_pattern",
            "Translated title",
            "{loc} = locale, {title} = translated title. Must contain text besides {title} "
            "(Confluence titles are unique per space, and the pattern is the collision key).",
            required=False,
            placeholder="[{loc}] {title}",
        ),
        FieldSpec(
            "max_pages", "Max pages", "Safety cap per run.", required=False, placeholder="25"
        ),
        # SPEC-context-metadata Phase 3B: Confluence is on the unified harvest mechanism.
        # `harvest_context` is the canonical flag (space name + ancestor chain ->
        # source.section, labels, title into the <translation_context> block).
        # `context_brief` is kept as a DEPRECATED alias: an existing connection with
        # context_brief=0 stays off. On by default.
        harvest_context_field(),
        FieldSpec(
            "context_brief",
            "Inject page context (deprecated)",
            "Deprecated alias of 'Harvest source context', kept so connections that "
            "disabled it stay disabled. Prefer 'Harvest source context'.",
            required=False,
            placeholder="1",
        ),
    ],
    secret_fields=[
        FieldSpec(
            "email",
            "Atlassian account email",
            "The account that owns the API token.",
            secret=False,
            placeholder="you@company.com",
        ),
        FieldSpec(
            "api_token",
            "API token",
            "Created at id.atlassian.com/manage-profile/security/api-tokens.",
            secret=True,
            placeholder="ATATT3xFfGF0…",
        ),
        FieldSpec(
            "webhook_secret",
            "Webhook signing secret",
            "Shared HMAC secret verifying "
            "inbound page-updated webhooks. Set the SAME value on the sender. "
            "(Non-Forge Confluence webhooks are limited.)",
            secret=True,
            required=False,
            placeholder="a long random string",
        ),
    ],
    setup_steps=[
        "Create an API token at id.atlassian.com, Security, API tokens (the same token works for Jira).",  # noqa: E501
        "Set the Site URL to your Atlassian base, e.g. https://your-team.atlassian.net.",
        "Set a Space key (its pages will be translated) or pass specific page ids via the webhook.",
        "List target locales (e.g. es-MX, de-DE). Each page gets one translated child page per locale.",  # noqa: E501
        "The account behind the token needs permission to view pages and to add pages in that space.",  # noqa: E501
    ],
    other_side="A 'Translate this page' button can be added in Confluence via a Forge app "
    "(deferred, needs Localization OS reachable from Atlassian's cloud).",
    docs_url="https://developer.atlassian.com/cloud/confluence/rest/v2/intro/",
    supports_oauth=True,
    archived=False,
)


def _valid_page_id(v) -> str:
    s = str(v if v is not None else "").strip()
    if not _PAGE_ID_RE.match(s):
        raise ConnectorError(f"Not a Confluence page id: {s[:40]!r} (digits only).")
    return s


def _valid_space_key(v) -> str:
    s = str(v if v is not None else "").strip()
    if not _SPACE_KEY_RE.match(s):
        raise ConnectorError(f"Not a Confluence space key: {s[:40]!r}.")
    return s


def _cursor_of(body) -> str | None:
    """The `cursor` query value of a v2 `_links.next`, or None. Only the cursor is taken from
    the link: we never follow a server-supplied URL (it could name another host)."""
    nxt = ((body or {}).get("_links") or {}).get("next")
    if not nxt:
        return None
    try:
        vals = parse_qs(urlparse(str(nxt)).query).get("cursor")
    except Exception:
        return None
    return vals[0] if vals else None


class ConfluenceConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="confluence",
        topology=Topology.T3,
        auth=(AuthKind.API_KEY, AuthKind.OAUTH3LO),
        api_version="v2",
        rich_text=RichText.HTML,  # storage-format XHTML through the html filter
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig="hmac-sha256",
            poll_field="version.number",
            stale_flag=None,
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = DocPerLocaleStrategy(source_locale_of=lambda conn, rec: self._source())
        self._space_cache: dict = {}
        self._title_cache: dict = {}
        self._display: dict = {}  # lower-case locale -> the form the operator configured

    # --- config helpers ---
    def _source(self) -> str:
        return (self.config.get("source_locale") or "").strip().lower()

    def _is_oauth(self) -> bool:
        return self.secret.get("auth_mode") == "oauth"

    def _site(self) -> str:
        b = (self.config.get("base_url") or "").strip().rstrip("/")
        if not b:
            raise ConnectorError("Confluence site URL is required.")
        site = b[:-5] if b.endswith("/wiki") else b  # tolerate a pasted /wiki URL
        _validate_target(site)  # SSRF guard on every request path (see base._validate_target)
        return site

    def _gateway(self) -> str:
        cloudid = ((self.secret.get("oauth") or {}).get("cloudid") or "").strip()
        if not oauth.valid_cloudid(cloudid):
            raise ConnectorError("Missing or invalid Atlassian cloudid, reconnect this connection.")
        return f"{oauth.ATLASSIAN_API_HOST}/ex/confluence/{cloudid}"

    def _api(self) -> str:
        """The v1 REST root (used only for CQL search). OAuth: the gateway has NO /wiki here."""
        if self._is_oauth():
            return self._gateway() + "/rest/api"
        return self._site() + "/wiki/rest/api"

    def _v2(self) -> str:
        if self._is_oauth():
            return self._gateway() + _GW_V2
        return self._site() + "/wiki/api/v2"

    def _title_pattern(self) -> str:
        pat = (self.config.get("title_pattern") or "[{loc}] {title}").strip() or "[{loc}] {title}"
        if "{title}" not in pat:
            raise ConnectorError("Translated title pattern must contain {title}.")
        return pat

    def _affixes(self, loc: str) -> tuple[str, str]:
        pre, _, post = self._title_pattern().replace("{loc}", self._shown(loc)).partition("{title}")
        return pre, post

    def _shown(self, loc: str) -> str:
        return self._display.get((loc or "").lower(), loc)

    def _target_title(self, loc: str, translated_title: str) -> str:
        pre, post = self._affixes(loc)
        if not (pre.strip() or post.strip()):
            raise ConnectorError(
                "The translated title pattern needs text besides {title} (for example "
                "'[{loc}] {title}'): Confluence page titles are unique per space."
            )
        return f"{pre}{translated_title or ''}{post}"

    def _looks_like_target(self, title: str, locales) -> bool:
        t = (title or "").strip().lower()
        for loc in locales:
            try:
                pre, post = self._affixes(loc)
            except ConnectorError:
                return False
            pre, post = pre.strip().lower(), post.strip().lower()
            if (
                (pre or post)
                and t.startswith(pre)
                and t.endswith(post)
                and len(t) > len(pre + post)
            ):
                return True
        return False

    # --- transport ---
    def _auth(self):
        email = (self.secret.get("email") or "").strip()
        token = (self.secret.get("api_token") or "").strip()
        if not email or not token:
            raise ConnectorError("Confluence email and API token are required.")
        return (email, token)

    def _auth_kwargs(self) -> dict:
        """Per-request auth kwargs, mode-aware: token mode -> httpx basic auth; oauth mode ->
        a Bearer header with the fresh access token."""
        if self._is_oauth():
            tok = ((self.secret.get("oauth") or {}).get("access_token") or "").strip()
            if not tok:
                raise ConnectorError(
                    "Missing Atlassian OAuth access token, reconnect this connection."
                )
            return {"headers": {"Authorization": f"Bearer {tok}"}}
        return {"auth": self._auth()}

    def _call(self, c, method, url, *, params=None, body=None, idempotent=True):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        kw: dict = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kw["json"] = body
        if params is not None:
            kw["params"] = params
        r = self._request(c, method, url, headers=headers, idempotent=idempotent, **akw, **kw)
        if self._is_oauth() and (
            r.status_code == 401 or (r.status_code == 403 and "scope" in (r.text or "").lower())
        ):
            # Review gate: an OAuth connection made before the v2 rebuild holds only the classic
            # scopes, and v2 answers 401 "scope does not match". Say what to do, never a bare
            # 401. A plain 403 (a restricted page) is NOT this and is left to the caller.
            raise ConnectorError(_OAUTH_RECONNECT_MSG.format(code=r.status_code))
        return r

    def _get2(self, c, path, params=None):
        return self._call(c, "GET", self._v2() + path, params=params)

    # --- operations ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get2(c, "/spaces", {"limit": 1})
            if r.status_code == 200:
                return True, "Connected to Confluence (v2 API, spaces readable)."
            if r.status_code in (401, 403) and self._is_oauth():
                return False, _OAUTH_RECONNECT_MSG.format(code=r.status_code)
            return False, f"Confluence returned {r.status_code}: {vendor_excerpt(r.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get2(c, "/spaces", {"limit": 50})
                if r.status_code != 200:
                    return []
                for sp in r.json().get("results", []):
                    out.append(
                        {
                            "id": sp.get("key"),
                            "name": f"{sp.get('name')} ({sp.get('key')})",
                            "tables": [],
                        }
                    )
        except Exception:
            return out
        return out

    # --- spaces / pages (v2) ---
    def _space_by_key(self, c, key: str) -> dict:
        key = _valid_space_key(key)
        if key in self._space_cache:
            return self._space_cache[key]
        r = self._get2(c, "/spaces", {"keys": key, "limit": 1})
        if r.status_code != 200:
            raise ConnectorError(
                f"Confluence space lookup failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        for sp in r.json().get("results") or []:
            if str(sp.get("key") or "").lower() == key.lower():
                info = {"id": str(sp.get("id")), "key": sp.get("key"), "name": sp.get("name") or ""}
                self._space_cache[key] = info
                return info
        raise ConnectorError(f"Space {key!r} was not found or is not visible to this token.")

    def _space_by_id(self, c, sid: str) -> dict:
        sid = _valid_page_id(sid)
        for info in self._space_cache.values():
            if info["id"] == sid:
                return info
        r = self._get2(c, f"/spaces/{sid}")
        if r.status_code != 200:
            return {"id": sid, "key": "", "name": ""}
        d = r.json() or {}
        info = {"id": sid, "key": d.get("key") or "", "name": d.get("name") or ""}
        if info["key"]:
            self._space_cache[info["key"]] = info
        return info

    @staticmethod
    def _page(d: dict) -> dict:
        labels = [
            (lb.get("name") or "")
            for lb in (((d.get("labels") or {}).get("results")) or [])
            if isinstance(lb, dict)
        ]
        body = (d.get("body") or {}).get("storage") or {}
        return {
            "id": str(d.get("id") or ""),
            "title": d.get("title") or "",
            "space_id": str(d.get("spaceId") or ""),
            "parent_id": str(d.get("parentId") or ""),
            "status": d.get("status") or "current",
            "version": ((d.get("version") or {}).get("number")),
            "labels": [x for x in labels if x],
            "body": body.get("value") or "",
        }

    def _fetch_page(self, c, pid: str) -> dict | None:
        pid = _valid_page_id(pid)
        r = self._get2(c, f"/pages/{pid}", {"body-format": "storage", "include-labels": "true"})
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Confluence page {pid} fetch failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        pg = self._page(r.json() or {})
        return pg if pg["status"] == "current" else None

    def _cql_ids(self, c, cql: str, cap: int) -> list[str]:
        ids: list[str] = []
        start = 0
        for _ in range(_MAX_LIST_PAGES):
            if len(ids) >= cap:
                break
            params = {"cql": cql, "limit": min(cap - len(ids), 50), "start": start}
            r = self._get_page(
                lambda **kw: self._call(c, "GET", self._api() + "/search", **kw),
                params=params,
                size_key="limit",
            )
            if r.status_code != 200:
                raise ConnectorError(
                    f"Confluence CQL search failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            rows = body.get("results") or []
            for row in rows:
                ct = (row.get("content") or {}) if isinstance(row, dict) else {}
                if ct.get("type") == "page" and str(ct.get("id") or "").strip():
                    pid = str(ct["id"]).strip()
                    if _PAGE_ID_RE.match(pid) and pid not in ids:
                        ids.append(pid)
            if not rows or not (body.get("_links") or {}).get("next"):
                break
            start += len(rows)
        return ids[:cap]

    def _our_target_ids(self) -> set:
        out: set = set()
        cid = self.connection_id
        if cid is not None:
            from app.services.connectors import idmap

            try:
                out |= {str(r.target_id) for r in idmap.list_for_connection(cid) if r.target_id}
            except Exception:
                log.warning("confluence: could not read the id map for target exclusion")
        out |= {str(v) for v in (self.config.get("confluence_children") or {}).values() if v}
        return out

    def discover_articles(
        self,
        cap: int | None = None,
        article_ids: list | None = None,
        space_key=None,
        cql=None,
    ) -> list[dict]:
        """Source pages to translate, confined to the connection's declared scope.

        The connection's OWN space_key / cql are the SCOPE (B2 scope-escape fix, kept): a
        caller-supplied cql is ANDed to it after reject_unwrappable_query (parens and quotes
        refused), a caller space_key is ignored when a scope space exists, and page ids outside
        the scope space are dropped. A cql-only scope refuses page ids (no per-id test).
        Page ids and space keys are validated before they reach a URL. Our own translated child
        pages (id map, recorded legacy ids, or a title that renders from the pattern) are never
        returned as sources."""
        cap = fetch_cap(cap or self.config.get("max_pages"), 25)
        scope_space = (self.config.get("space_key") or "").strip()
        scope_cql = (self.config.get("cql") or "").strip()
        cql = (cql or "").strip() or None
        locs = list(self._display.values()) or [
            x.strip() for x in str(self.config.get("target_locales") or "").split(",") if x.strip()
        ]
        skip_ids = self._our_target_ids()
        out: list[dict] = []

        def _keep(pg) -> bool:
            if pg["id"] in skip_ids:
                return False
            return not (locs and self._looks_like_target(pg["title"], locs))

        with self._client() as c:
            scope_sid = self._space_by_key(c, scope_space)["id"] if scope_space else ""
            if article_ids:
                if scope_cql and not scope_space:
                    raise ConnectorError(
                        "This connection's scope is a CQL filter with no space key, so "
                        "specific page ids can't be scope-checked. Translate by CQL/space "
                        "instead, or add a space key to the connection."
                    )
                for pid in [_valid_page_id(p) for p in list(article_ids)[:cap]]:
                    pg = self._fetch_page(c, pid)
                    if pg is None or (scope_sid and pg["space_id"] != scope_sid):
                        continue
                    self._charge_record(pg)
                    if _keep(pg):
                        out.append(pg)
                return out
            scope_clause = scope_cql or (f'space = "{scope_space}"' if scope_space else "")
            if cql or scope_cql:
                if cql and scope_clause:
                    eff = f"({reject_unwrappable_query(cql, lang='CQL')}) AND ({scope_clause})"
                else:
                    eff = cql or scope_cql
                for pid in self._cql_ids(c, eff, cap + min(len(skip_ids), 200)):
                    pg = self._fetch_page(c, pid)
                    # a scope CQL wins over a scope space inside the query, so the space is
                    # also enforced here on the fetched page (both declared = both hold)
                    if pg is not None and not (scope_sid and pg["space_id"] != scope_sid):
                        self._charge_record(pg)
                        if _keep(pg):
                            out.append(pg)
                    if len(out) >= cap:
                        break
                return out[:cap]
            eff_space = scope_space or (space_key or "").strip()
            if not eff_space:
                raise ConnectorError("Provide a space key, a CQL filter, or page ids.")
            sid = scope_sid or self._space_by_key(c, eff_space)["id"]
            cursor = None
            limit = min(_LIST_LIMIT, max(cap, 1))
            for _ in range(_MAX_LIST_PAGES):
                params = {"status": "current", "body-format": "storage", "limit": limit}
                if cursor:
                    params["cursor"] = cursor
                r = self._get_page(
                    lambda **kw: self._call(c, "GET", self._v2() + f"/spaces/{sid}/pages", **kw),
                    params=params,
                    size_key="limit",
                )
                limit = int(params["limit"])  # stay at the halved size if a page was too large
                if r.status_code != 200:
                    raise ConnectorError(
                        f"Confluence fetch failed ({r.status_code}): {vendor_excerpt(r.text)}"
                    )
                body = r.json() or {}
                rows = self._keep_rows(body.get("results") or [])
                for d in rows:
                    pg = self._page(d)
                    if pg["status"] == "current" and _keep(pg):
                        out.append(pg)
                cursor = _cursor_of(body)
                if not cursor or len(out) >= cap:
                    break
        return out[:cap]

    def show_article(self, article_id: str) -> dict | None:
        with self._client() as c:
            return self._fetch_page(c, article_id)

    # --- context harvest (extra reads, only when harvest is on) ---
    def page_context(self, pg: dict) -> dict:
        """Space name + nearest ancestor titles + label names for the source-context brief."""
        out = {"space": "", "ancestors": [], "labels": pg.get("labels") or []}
        with self._client() as c:
            try:
                sp = self._space_by_id(c, pg["space_id"]) if pg.get("space_id") else {}
                out["space"] = sp.get("name") or sp.get("key") or ""
                r = self._get2(c, f"/pages/{pg['id']}/ancestors", {"limit": 25})
                if r.status_code == 200:
                    ids = [str(a.get("id")) for a in (r.json() or {}).get("results") or []]
                    titles = []
                    for aid in [i for i in ids if _PAGE_ID_RE.match(i)][-_MAX_ANCESTORS:]:
                        if aid not in self._title_cache:
                            ar = self._get2(c, f"/pages/{aid}")
                            self._title_cache[aid] = (
                                (ar.json() or {}).get("title", "") if ar.status_code == 200 else ""
                            )
                        titles.append(self._title_cache[aid])
                    out["ancestors"] = [t for t in titles if t]
            except (ConnectorError, httpx.HTTPError):
                log.warning("confluence: context harvest for page %s skipped", pg.get("id"))
        return out

    # --- T3 write-back client (what DocPerLocaleStrategy drives) ---
    def create_target(self, source_id: str, locale: str, fields: dict):
        """RESERVE AND FILL IN ONE CREATE (review gate, connector parity): the child page is POSTed
        already carrying the translated title AND body, so a reader never sees an empty or
        half-built published page. The flow never creates while any unit is held (#640), and
        this refuses too if the title or body is missing, so a create is always complete. The
        strategy's fill step that follows is a no-op when the stored page already matches
        (see fill_target); if it fails, the page that exists is still the full translation."""
        f = fields or {}
        if f.get("_no_translation") or f.get("title") is None or f.get("body") is None:
            return None, f"{_HELD_BACK_PREFIX} not fully translated; page not created"
        title = self._target_title(locale, f["title"])
        payload = {
            "spaceId": str(f.get("space_id") or ""),
            "status": "current",
            "title": title,
            "parentId": _valid_page_id(source_id),
            "body": {"representation": "storage", "value": f["body"]},
        }
        if not payload["spaceId"]:
            return None, "source page has no space id"
        try:
            with self._client() as c:
                # Non-idempotent create: never resent on 5xx or a dropped connection.
                r = self._call(c, "POST", self._v2() + "/pages", body=payload, idempotent=False)
        except httpx.TransportError as e:
            return None, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201):
            pid = (r.json() or {}).get("id")
            return (str(pid) if pid else None), (None if pid else "create returned no id")
        return None, f"create page -> {r.status_code}: {vendor_excerpt(r.text)}"

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """Write the translated title and/or body onto the target by id. The page is read
        first: a part that is held back (None in `fields`) keeps its STORED value, never the
        source text (#640). A 409 (someone saved in between) refetches the version and retries
        once. Never raises."""
        tid = _valid_page_id(target_id)
        f = fields or {}
        if f.get("title") is None and f.get("body") is None:
            return WriteResult(
                False,
                False,
                target_id=tid,
                message=f"{_HELD_BACK_PREFIX} nothing translated this run; stored page untouched",
            )
        try:
            with self._client() as c:
                for attempt in (0, 1):
                    cur = self._fetch_page(c, tid)
                    if cur is None:
                        return WriteResult(
                            False, False, target_id=tid, message="target page not readable"
                        )
                    title = (
                        self._target_title(f.get("locale") or "", f["title"])
                        if f.get("title") is not None
                        else cur["title"]
                    )
                    body = f["body"] if f.get("body") is not None else cur["body"]
                    if attempt == 0 and title == cur["title"] and body == cur["body"]:
                        # Already exactly this content (the one-shot create just wrote it):
                        # no second version, nothing to send.
                        return WriteResult(True, False, target_id=tid, message="unchanged")
                    payload = {
                        "id": tid,
                        "status": "current",
                        "title": title,
                        "body": {"representation": "storage", "value": body},
                        "version": {"number": int(cur["version"] or 0) + 1},
                    }
                    # PUT with a version guard: a blind resend after a 5xx would risk a spurious
                    # 409, so the base retries only 429 and this loop owns the 409 refetch.
                    r = self._call(
                        c, "PUT", self._v2() + f"/pages/{tid}", body=payload, idempotent=False
                    )
                    if r.status_code == 409 and attempt == 0:
                        continue
                    if r.status_code in (200, 201):
                        return WriteResult(True, False, target_id=tid, message="filled")
                    return WriteResult(
                        False,
                        False,
                        target_id=tid,
                        message=f"{r.status_code}: {vendor_excerpt(r.text)}",
                    )
        except httpx.TransportError as e:
            return WriteResult(False, False, target_id=tid, message=f"{type(e).__name__}: {e}")
        return WriteResult(False, False, target_id=tid, message="version conflict")

    def verify_target(self, target_id: str) -> bool:
        """Does the target page still exist? A fetch of the id (never a title match). Only a
        definite 404/410 or a trashed page is "gone"; anything else unclear holds the pair."""
        tid = _valid_page_id(target_id)
        with self._client() as c:
            r = self._get2(c, f"/pages/{tid}")
        if r.status_code in (404, 410):
            return False
        if r.status_code != 200:
            raise TargetUnverifiable(f"HTTP {r.status_code} verifying {tid}")
        st = (r.json() or {}).get("status")
        if st in ("trashed", "deleted"):
            return False
        if st == "archived":
            raise TargetUnverifiable(f"page {tid} is archived; restore or delete it, then retry")
        return True

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        """Rule-6 collision probe: a page our map never made that would collide. Checks the
        source's direct children for the exact title or our locale tag, and the whole space for
        the exact title (Confluence titles are unique per space). Returns its id or None."""
        if (fields or {}).get("_no_translation"):
            return None  # nothing will be created, so there is nothing to collide with
        want = self._target_title(locale, (fields or {}).get("title") or "").strip()
        pre, post = (x.strip().lower() for x in self._affixes(locale))
        try:
            with self._client() as c:
                cursor = None
                for _ in range(_MAX_LIST_PAGES):
                    params = {"limit": _LIST_LIMIT}
                    if cursor:
                        params["cursor"] = cursor
                    r = self._get2(c, f"/pages/{_valid_page_id(source_id)}/direct-children", params)
                    if r.status_code != 200:
                        break
                    body = r.json() or {}
                    for ch in body.get("results") or []:
                        t = (ch.get("title") or "").strip()
                        tl = t.lower()
                        if t == want or (
                            (pre or post)
                            and tl.startswith(pre)
                            and tl.endswith(post)
                            and len(tl) > len(pre + post)
                        ):
                            return str(ch.get("id"))
                    cursor = _cursor_of(body)
                    if not cursor:
                        break
                sid = str((fields or {}).get("space_id") or "")
                if sid and _PAGE_ID_RE.match(sid):
                    r = self._get2(c, "/pages", {"space-id": sid, "title": want, "limit": 5})
                    if r.status_code == 200:
                        for pg in (r.json() or {}).get("results") or []:
                            if (pg.get("title") or "").strip() == want:
                                return str(pg.get("id"))
        except (ConnectorError, httpx.HTTPError):
            return None
        return None

    # --- generic Connector surface ---
    def pull(self) -> list[PulledItem]:
        """Generic-framework pull: one item per page (the storage body). The real
        translation flow is confluence_translate (creates child pages)."""
        self._reset_fetch_budget()
        return [
            PulledItem(external_id=p["id"], field="body", text=p["body"], title=p["title"])
            for p in self.discover_articles()
        ]

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Confluence writes translated child pages, use 'Translate now'.")


# ------------------------- Confluence (T3) flow hooks ----------------------
def _cf_ctx_on(cfg: dict) -> bool:
    from app.services.connectors import harvest

    if "harvest_context" in cfg:
        return harvest.harvest_enabled(cfg)
    return str(cfg.get("context_brief", "1")).strip().lower() not in ("0", "false", "no")


def _cf_preflight(ctx, raw_locales):
    from app.filters import get_filter  # noqa: F401  (fail early if the filter is missing)

    c = ctx.connector
    c._display = {str(l).strip().lower(): str(l).strip() for l in raw_locales}  # noqa: E741
    if not _cf_ctx_on(ctx.cfg):
        # the deprecated context_brief=0 alias must keep context off for the flow's harvest too
        ctx.cfg = {**ctx.cfg, "harvest_context": "0"}
    src = c._source()
    return {l for l in ctx.locales if l and l != src}, {}  # noqa: E741


# Persisted in place of a legacy hash when a pair is adopted: it never equals a real content
# hash, so the pair is NOT suppressed until one corrective write succeeds (then the real hash
# replaces it). Survives a failed or budget-trimmed write, so the rewrite is retried.
_LEGACY_RESYNC = "legacy-resync"


def _cf_adopt_legacy(ctx, aid: str) -> None:
    """Existing connections: adopt the child ids the old loop recorded in `confluence_children`
    as `done` id-map rows, and lower-case-alias the old mixed-case hash keys. Recorded ids only.

    Review gate (connector parity): the OLD loop wrote held (NEEDS_REVIEW / untranslated) units as
    SOURCE-language text into the child pages it created AND recorded their hash, so keeping
    that hash would leave those pages silently in the source language for good, and which
    pages are affected cannot be told from the hashes. So an adopted pair's legacy hash is
    INVALIDATED: every adopted page gets exactly one corrective rewrite through the new flow
    (the #640 rules apply: a still-held unit keeps the page's stored part and the pair stays
    pending). One-time MT spend, reported as `legacy_resync` in the run result. Confluence
    keeps page history, so the prior version of each rewritten page stays restorable."""
    from app.services.connectors import idmap

    legacy = ctx.extra.get("_legacy")
    if legacy is None:
        cfg = ctx.conn.config or {}
        kids = {str(k).lower(): str(v) for k, v in (cfg.get("confluence_children") or {}).items()}
        hs = {str(k).lower(): v for k, v in (cfg.get("confluence_hashes") or {}).items()}
        legacy = ctx.extra["_legacy"] = (kids, hs)
    kids, hs = legacy
    for loc in ctx.usable:
        key = f"{aid}:{loc}".lower()
        if key in hs and f"{aid}:{loc}" not in ctx.hashes:
            ctx.hashes[f"{aid}:{loc}"] = hs[key]
        if key in kids and idmap.get(ctx.cid, aid, loc) is None:
            idmap.reserve(ctx.cid, aid, loc, kids[key], status=idmap.DONE)
            ctx.hashes[f"{aid}:{loc}"] = _LEGACY_RESYNC
            ctx.hash_delta[f"{aid}:{loc}"] = _LEGACY_RESYNC
            ctx.counters["legacy_resync"] += 1


def _cf_prepare(ctx, art):
    from app.filters import get_filter

    aid = str(art.get("id") or "")
    _cf_adopt_legacy(ctx, aid)
    body = art.get("body") or ""
    title = art.get("title") or ""
    units = get_filter("html").parse(body.encode("utf-8")).units
    if not title.strip() and not units:
        return flow.SkipArticle("empty_skipped")
    harvest_extra: dict = {}
    if _cf_ctx_on(ctx.cfg):
        pc = ctx.connector.page_context(art)
        harvest_extra = {"space": pc["space"], "ancestors": pc["ancestors"], "labels": pc["labels"]}
    space_key = ""
    try:
        with ctx.connector._client() as c:
            space_key = ctx.connector._space_by_id(c, art.get("space_id") or "0").get("key") or ""
    except Exception:
        space_key = ""
    return flow.ArticlePrep(
        aid=aid,
        chash=_content_hash(title, body),  # same input as the archived loop: old hashes stay valid
        title_text=title,
        body_units=units,
        extra_skeleton={"page_id": aid, "title": title, "space": space_key},
        doc_title=f"{(title or 'page')[:50]} ({aid})",
        extra={"body": body, "space_id": art.get("space_id") or ""},
        harvest=harvest_extra,
    )


def _cf_is_suppressed(connector, aid, loc):
    # Reads the MAP (a `done` row), never content. Anything else falls through to write_target.
    return connector.strategy.exists_target(connector, aid, loc)


def _cf_build_payload(prep, loc, seg0_target, mapping):
    from app.filters import get_filter

    real = [u for u in prep.body_units if (u.text or "").strip()]
    complete = all(str(u.p_index) in mapping for u in real)
    body = None
    if complete:
        body = get_filter("html").build(prep.extra["body"].encode("utf-8"), mapping).decode("utf-8")
    return {
        "title": seg0_target,  # None = held back: never the source title
        "body": body,  # None = some unit held back: never a mixed-language body
        "locale": loc,
        "space_id": prep.extra["space_id"],
        "_no_translation": seg0_target is None and body is None,
    }


def _cf_is_held(f) -> bool:
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _cf_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = [f for f in ctx.failures if not _cf_is_held(f)]
    return bool((ctx.created + ctx.updated) > 0 or not real)


def _cf_build_message(ctx):
    from app.services.connectors import idmap

    wrote = ctx.created + ctx.updated
    parts = [
        f"Wrote {wrote} translated page(s) ({ctx.created} created, {ctx.updated} updated) in "
        f"{len(ctx.usable)} language(s) from {len(ctx.articles)} source page(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged/already-translated page/locale pair(s).")
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} empty page(s).")
    if ctx.counters.get("legacy_resync"):
        parts.append(
            f"Adopted {ctx.counters['legacy_resync']} translated page(s) made by the previous "
            f"Confluence connector; each is rewritten once (one-time translation cost) so no "
            f"page keeps source-language text the old connector left in it."
        )
    held = [f for f in ctx.failures if _cf_is_held(f)]
    if held:
        parts.append(
            f"Held back {len(held)} (page, locale) pair(s) awaiting review (NEEDS_REVIEW): a new "
            f"child page is not created while a unit is held. They will be retried next run."
        )
    needs_link = idmap.status_counts(ctx.cid).get("needs_link", 0)
    if needs_link:
        parts.append(
            f"{needs_link} page(s) need a link decision (a page with that title already exists, "
            f"adopt or ignore it)."
        )
    if ctx.rejected:
        parts.append(f"Rejected {len(ctx.rejected)} locale(s): {', '.join(ctx.rejected)}.")
    real = [f for f in ctx.failures if not _cf_is_held(f)]
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("page write(s)").strip())
        log.warning(
            "confluence_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _cf_build_result(ctx):
    from app.services.connectors import idmap

    return {
        "pages": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "legacy_resync": ctx.counters.get("legacy_resync", 0),
        "held_back": len([f for f in ctx.failures if _cf_is_held(f)]),
        "rejected": ctx.rejected,
        "failures": [f for f in ctx.failures if not _cf_is_held(f)],
        "id_map": idmap.status_counts(ctx.cid),
    }


def confluence_translate(
    cid: int,
    page_ids: list | None = None,
    space_key: str | None = None,
    cql: str | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    article_ids: list | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Confluence pages into one or more languages, one child page per (page, locale),
    through the shared flow engine and the T3 id map. `page_ids` / `article_ids` pick pages by
    id; `space_key` / `cql` narrow within the connection's declared scope (never widen it)."""
    from app.services.connectors import store

    ids = page_ids or article_ids
    raw = store.effective_locales(store.get(cid), target_locales) if store.get(cid) else []

    def _pre(ctx):
        return _cf_preflight(ctx, raw)

    spec = flow.TranslateFlowSpec(
        kind="confluence",
        no_conn_msg="No such Confluence connection.",
        hash_key="confluence_hashes",
        probe_label="Confluence",
        preflight=_pre,
        prepare=_cf_prepare,
        is_suppressed=_cf_is_suppressed,
        build_payload=_cf_build_payload,
        compute_draft=lambda cfg: False,
        compute_ok=_cf_compute_ok,
        build_message=_cf_build_message,
        build_result=_cf_build_result,
        extra_counters=("empty_skipped", "legacy_resync"),
        held_prefix=_HELD_BACK_PREFIX,
        # #666: legacy mixed-case hash keys alias the pair (see _cf_adopt_legacy).
        hash_drop_casefold=True,
        discover=lambda connector, article_ids: connector.discover_articles(
            article_ids=article_ids, space_key=space_key, cql=cql
        ),
    )
    return flow.run_translate_flow(
        spec,
        cid,
        article_ids=ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def confluence_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Operator resolution of a needs_link row: 'adopt' (the existing page is ours, the next run
    updates it) or 'ignore' (drop the row). Only edits the id map, never page content."""
    from app.services.connectors import idmap
    from app.services.connectors.store import _log, get

    conn = get(cid)
    if not conn or conn.kind != "confluence":
        return False, "No such Confluence connection.", {}
    ok, msg = idmap.resolve_needs_link(cid, str(source_id), target_locale, action)
    _log(cid, "resolve-link", ok, 0, msg, actor)
    return ok, msg, {"id_map": idmap.status_counts(cid)}


# --- register the connector's webhook verifier + payload extractors -------------
# [UNCERTAIN, capability guard]: Confluence Cloud does not sign webhooks for non-Forge apps out
# of the box. This makes the generalized intake (POST /webhooks/connectors/{id}) ACCEPT a
# page-updated event and re-fetch/enqueue; the SENDER that HMAC-signs the body must be a Forge
# app or an Atlassian automation rule you control. HMAC-SHA256(body, secret), HEX digest, in
# `X-Hub-Signature-256`, `sha256=` prefix stripped.
webhooks.register_verifier(
    "confluence",
    webhooks.hmac_sha256_verifier("X-Hub-Signature-256", prefix="sha256=", hex_digest=True),
    event_id=lambda body_json, headers: _confluence_event_key(body_json),
)


def _confluence_page_ids(body_json) -> list[str]:
    """Page ids a (verified) Confluence webhook says changed. THIN: we only take the id, the
    job re-fetches the page body before translating. Defensive across the shapes a Forge app /
    automation rule might send: {page:{id}}, {content:{id}}, top-level {pageId}/{id}, or a
    list under {pages:[...]}."""
    body_json = body_json or {}
    ids: list[str] = []

    def _add(v):
        s = str(v or "").strip()
        if s and s not in ids:
            ids.append(s)

    for key in ("page", "content"):
        node = body_json.get(key)
        if isinstance(node, dict):
            _add(node.get("id"))
    _add(body_json.get("pageId"))
    if not ids:  # top-level id ONLY when no nested content node claimed one
        _add(body_json.get("id"))
    for node in body_json.get("pages") or []:
        if isinstance(node, dict):
            _add(node.get("id"))
    return ids


def _confluence_event_key(body_json) -> str | None:
    """(page_id, version) composite for dedup, but ONLY when we actually have a version (F2:
    a bare page id would collapse distinct edits onto one event id and drop the second).
    None falls through to a SHA-256 of the raw body in webhooks.dedup_key."""
    ids = _confluence_page_ids(body_json)
    body_json = body_json or {}
    node = body_json.get("page") or body_json.get("content") or {}
    ver = ""
    if isinstance(node, dict):
        ver = str(((node.get("version") or {}).get("number")) or node.get("version") or "")
    if ids and ver:
        return f"{ids[0]}:{ver}"
    return None


webhooks.register_changed_ids("confluence", _confluence_page_ids)
