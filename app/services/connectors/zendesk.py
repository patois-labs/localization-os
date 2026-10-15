"""Zendesk Guide (Help Center) connector — the T2 reference consumer of the
connector framework.

Topology T2: every Article/Section/Category owns a `translations` sub-resource
addressed by locale; translate = POST-create or PUT-update the translation object
at /{id}/translations/{locale}. That write-back rides the shared
strategy.SubResourceStrategy; this module owns only the endpoints + auth + the
Zendesk-specific field shapes and change-detection rules.

PROVENANCE: every endpoint below is [CONFIRMED] in zendesk.md unless a comment
flags [UNCERTAIN] — those are guarded (a guard + a documented fallback), never
built against as fact.

CARDINAL CHANGE-DETECTION RULE: re-translate off `edited_at` (content changed &
published), NEVER `updated_at` (bumps on comments/votes). Enforced in
select_changed / the incremental watermark.

ANTI-LOOP (two layers, per the brief):
  (1) label — on write-back, remove the trigger label / add a `translated` label
      (configurable) so the article isn't re-picked.
  (2) content-hash — a per-field source-content hash stored per unit; skip
      re-translation when the source content hash is unchanged even if edited_at
      moved. This guards the [UNCERTAIN] "do label edits bump edited_at?" risk so
      the connector can't self-trigger a loop.
"""

from __future__ import annotations

import base64
import re

import httpx

from app.filters.base import InputLimitError
from app.services.connectors import webhooks
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    fetch_cap,
    harvest_context_field,
    item_char_limit,
    item_chars,
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
from app.services.connectors.vendor_text import vendor_excerpt

API_VERSION = "v2"  # Help Center API version pin (design §3.6 — unpinned drifts)

_NUMERIC_ID = re.compile(r"[0-9]{1,20}")
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
# Marker build_payload leaves on a payload whose held (NEEDS_REVIEW) field was omitted, so
# the client does not claim the stale flag is cleared and never writes a source stand-in.
_HELD_KEY = "_held"
# Gate 2026-10-10 (same rule as Freshdesk/Intercom): in draft mode a PUBLISHED translation is
# never written (not unpublished, not overwritten live with unreviewed text).
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the translation is PUBLISHED in Guide and this connection writes drafts, so the live "
    "text was left unchanged; set Write as draft = 0 to update published translations"
)


def _is_published(tr) -> bool:
    # Fail closed: a translation whose draft flag is not explicitly True counts as live.
    return isinstance(tr, dict) and tr.get("draft") is not True


def _zd_id(value, what: str = "id") -> str:
    """A Help Center id is digits only. Anything else (`..`, `%2e`, `?`, `#`, CR/LF, NUL,
    unicode digits) is refused before it can reach a URL path (defect taxonomy A)."""
    text = str(value if value is not None else "").strip()
    if not _NUMERIC_ID.fullmatch(text) or not text.isascii():
        raise ConnectorError(f"Invalid Zendesk {what}: expected digits only.")
    return text


def _zd_locale(value) -> str:
    text = str(value if value is not None else "").strip()
    if not _LOCALE.fullmatch(text) or not text.isascii():
        raise ConnectorError("Invalid Zendesk locale code.")
    return text


SPEC = ConnectorSpec(
    kind="zendesk",
    name="Zendesk Guide",
    icon="🎫",
    cat="Knowledge base / help center",
    blurb="Translate Zendesk Guide (Help Center) articles, sections, and categories into "
    "any language. Each translation is written back as a per-locale translation object "
    "(draft by default), so the source article is never overwritten.",
    config_fields=[
        FieldSpec(
            "subdomain",
            "Zendesk subdomain",
            "Your Help Center subdomain — the 'acme' in acme.zendesk.com.",
            placeholder="acme",
        ),
        FieldSpec(
            "label_names",
            "Label filter",
            "Only translate articles carrying ALL of these "
            "labels (comma-separated, case-sensitive, max 10). Blank = all.",
            required=False,
            placeholder="needs-translation",
        ),
        FieldSpec(
            "section_id",
            "Section id",
            "Limit to one section (optional).",
            required=False,
            placeholder="360001234567",
        ),
        FieldSpec(
            "category_id",
            "Category id",
            "Limit to one category (optional).",
            required=False,
            placeholder="360007654321",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Guide default/authoring language.",
            required=False,
            placeholder="en-us",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Guide locales to create (must be enabled in Guide).",
            required=False,
            placeholder="de, fr, es",
        ),
        FieldSpec(
            "draft",
            "Write as draft",
            "'1' = push translations as unpublished drafts "
            "(default, safe; a translation already published in Guide is left unchanged "
            "and reported); '0' = publish immediately.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "translated_label",
            "Translated label",
            "Label added to an article after a "
            "successful write-back (anti-loop). Blank = don't add one.",
            required=False,
            placeholder="translated",
        ),
        FieldSpec(
            "remove_trigger_label",
            "Remove trigger label",
            "'1' = remove the FIRST label "
            "filter label after write-back so the article isn't re-picked.",
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
            "email",
            "Agent email",
            "The Zendesk agent/admin email that owns the API token.",
            secret=False,
            placeholder="you@acme.com",
        ),
        FieldSpec(
            "api_token",
            "API token",
            "Admin Center → Apps and integrations → APIs → Zendesk API → add an API token.",
            secret=True,
            placeholder="abcd1234…",
        ),
        FieldSpec(
            "webhook_secret",
            "Webhook signing secret",
            "Shared secret for the article-event "
            "webhook (used to verify inbound deliveries). Set the SAME value on the Zendesk "
            "webhook.",
            secret=True,
            required=False,
            placeholder="a long random string",
        ),
    ],
    setup_steps=[
        "In Admin Center → Apps and integrations → APIs → Zendesk API, enable Token access and add an API token.",  # noqa: E501
        "Enter your subdomain (the 'acme' in acme.zendesk.com), the agent email, and the token.",
        "In Guide → Admin → Localization, enable every target locale you want to create translations for.",  # noqa: E501
        "Optionally label the articles to translate (e.g. 'needs-translation') and set that as the Label filter.",  # noqa: E501
        "For push-triggered sync, create a Zendesk article.published webhook pointing at the intake URL "  # noqa: E501
        "shown on the connection page, with the same signing secret.",
    ],
    other_side="Change detection combines an article.published webhook (low latency), a nightly "
    "incremental-export sweep (edited_at watermark), and the outdated_locales stale flags.",
    docs_url="https://developer.zendesk.com/api-reference/help_center/help-center-api/translations/",
    supports_oauth=False,  # API-token Basic is first-class; OAuth is a documented follow-up.
    archived=False,  # un-archived 2026-10-10 (connector parity rebuild); live QA is pending  # noqa: E501
)


class ZendeskConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="zendesk",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY, AuthKind.OAUTH3LO),
        api_version=API_VERSION,
        rich_text=RichText.HTML,
        regions="subdomain",
        change_signals=ChangeSignals(
            webhook_sig="hmac_sha256_b64",  # X-Zendesk-Webhook-Signature (base64)
            poll_field="edited_at",  # CARDINAL: content signal, not updated_at
            stale_flag="outdated_locales",  # Zendesk's own stale signal
            thin_payload=True,
        ),  # events carry id only -> re-fetch
        anti_loop_label=True,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # T2 strategy wired with Zendesk's source-locale hook. The strategy owns the
        # 404-vs-200 create/update decision; this connector supplies the client
        # (get_translation/create_translation/update_translation below).
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: (rec or {}).get("source_locale", "")
        )
        # Context-harvest section id->name cache (SPEC-context-metadata Phase 3B):
        # populated lazily by section_name(), so a pull resolves each DISTINCT section
        # with at most ONE GET (a section-scoped connection = one fetch total), never
        # per-article.
        self._section_names: dict[str, str] = {}

    # --- auth / base url ---
    def _is_oauth(self) -> bool:
        return self.secret.get("auth_mode") == "oauth"

    def _base(self) -> str:
        from app.services.connectors.tenant_host import tenant_host

        base = "https://" + tenant_host("zendesk", self.config)
        _validate_target(base)  # SSRF guard (base._validate_target) on every request path
        return base

    def _auth_kwargs(self) -> dict:
        """Per-request auth kwargs. API-token Basic is the first-class path:
        credential string is `{email}/token:{api_token}` base64-encoded into a
        Basic header (zendesk.md §6). OAuth Bearer is supported if a v2 OAuth
        secret is present (optional; see supports_oauth=False on the spec)."""
        if self._is_oauth():
            tok = ((self.secret.get("oauth") or {}).get("access_token") or "").strip()
            if not tok:
                raise ConnectorError(
                    "Missing Zendesk OAuth access token — reconnect this connection."
                )
            return {"headers": {"Authorization": f"Bearer {tok}"}}
        email = (self.secret.get("email") or "").strip()
        token = (self.secret.get("api_token") or "").strip()
        if not email or not token:
            raise ConnectorError("Zendesk agent email and API token are required.")
        raw = f"{email}/token:{token}".encode()
        return {"headers": {"Authorization": "Basic " + base64.b64encode(raw).decode()}}

    def _get(self, c, path, **kw):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        return self._request(c, "GET", self._base() + path, headers=headers, **akw, **kw)

    def _write_kwargs(self) -> dict:
        akw = self._auth_kwargs()
        akw["headers"] = {"Content-Type": "application/json", **akw.pop("headers", {})}
        return akw

    def _hc(self, path: str) -> str:
        return f"/api/{API_VERSION}/help_center{path}"

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get(c, self._hc("/locales"))
            if r.status_code == 200:
                locs = r.json().get("locales", [])
                return True, f"Connected to Zendesk Guide ({len(locs)} enabled locale(s))."
            return False, f"Zendesk returned {r.status_code}: {vendor_excerpt(r.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: Guide categories (id + name). Best-effort."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, self._hc("/categories"), params={"page[size]": 100})
                if r.status_code != 200:
                    return []
                for cat in r.json().get("categories", []):
                    out.append(
                        {
                            "id": str(cat.get("id")),
                            "name": f"{cat.get('name')} (category {cat.get('id')})",
                            "tables": [],
                        }
                    )
        except Exception:
            return out
        return out

    # --- discover: enabled locales + articles ---
    def enabled_locales(self) -> list[str]:
        """GET /help_center/locales — the Guide's enabled locales. A target must be
        in this set or the write is rejected (zendesk.md gotcha #1)."""
        with self._client() as c:
            r = self._get(c, self._hc("/locales"))
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read Guide locales ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        body = r.json()
        # The endpoint returns {"locales": ["en-us","de",...], "default_locale": "en-us"}.
        locs = body.get("locales") or []
        return [str(x).strip().lower() for x in locs if str(x).strip()]

    def _paginate(self, c, path: str, key: str, params: dict, cap: int) -> list[dict]:
        """Cursor-paginate a standard Help Center list (page[size]<=100, follow
        meta.after_cursor until has_more == false). Capped."""
        out: list[dict] = []
        params = dict(params or {})
        params.setdefault("page[size]", min(100, cap))
        after = None
        while len(out) < cap:
            p = dict(params)
            if after:
                p["page[after]"] = after
            r = self._get(c, path, params=p)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Zendesk list {path} failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json()
            out.extend(self._keep_rows(body.get(key, [])))
            meta = body.get("meta") or {}
            if not meta.get("has_more") or not meta.get("after_cursor"):
                break
            after = meta.get("after_cursor")
        return out[:cap]

    def _label_filter(self) -> list[str]:
        """Parsed label_names filter: case-SENSITIVE (Zendesk requirement), AND
        semantics, capped at 10 (the API max)."""
        raw = (self.config.get("label_names") or "").strip()
        if not raw:
            return []
        labels = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
        return labels[:10]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """List translatable articles, scoped by section/category + label filter.

        Precedence mirrors the existing connectors' scoping: explicit article_ids
        (webhook / retry path) win; else section_id; else category_id; else all.
        The label_names filter (AND, case-sensitive, <=10) narrows articles."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cfg = self.config or {}
        params: dict = {"sort_by": "edited_at", "sort_order": "desc"}
        labels = self._label_filter()
        if labels:
            params["label_names"] = ",".join(labels)  # case-sensitive per spec
        with self._client() as c:
            if article_ids:
                # Explicit ids (webhook re-fetch / retry) are NOT label-filtered
                # (review finding): the anti-loop config strips the trigger label
                # on the first translate, so a later content-edit webhook for that
                # article would be silently dropped if the filter re-applied. The
                # event/retry NAMES the article — that IS the selection.
                out = []
                for aid in list(article_ids)[:cap]:
                    aid = _zd_id(aid, "article id")  # fail closed: no path escape
                    r = self._get(c, self._hc(f"/articles/{aid}"))
                    if r.status_code == 200:
                        art = r.json().get("article")
                        # Scope check: a caller-named article outside the connection's
                        # configured section/category is dropped, same as a missing one.
                        if art and _zd_scope_ok(cfg, art):
                            out.append(self._charge_record(art))
                return out
            if (cfg.get("section_id") or "").strip():
                path = self._hc(f"/sections/{_zd_id(cfg['section_id'], 'section id')}/articles")
            elif (cfg.get("category_id") or "").strip():
                path = self._hc(f"/categories/{_zd_id(cfg['category_id'], 'category id')}/articles")
            else:
                path = self._hc("/articles")
            arts = self._paginate(c, path, "articles", params, cap)
        # label_names is not honored on the section/category-scoped list endpoints
        # (only on /articles), so post-filter to keep AND semantics uniform.
        return [a for a in arts if self._label_ok(a, labels)]

    @staticmethod
    def _label_ok(article: dict, labels: list[str]) -> bool:
        """AND, case-sensitive: the article must carry every filter label."""
        if not labels:
            return True
        have = set(article.get("label_names") or [])
        return all(lbl in have for lbl in labels)

    def section_name(self, section_id) -> str | None:
        """Resolve a Help Center section id -> its NAME for context harvest
        (SPEC-context-metadata Phase 3B). Cached per connector instance
        (`_section_names`) so a pull resolves each DISTINCT section with at most ONE
        GET, never per-article. Best-effort: any non-200 / transport error caches ""
        (a negative result) so a broken/permission-trimmed section id is not re-fetched
        and never fails the run — harvest simply omits section for that article.

        [UNCERTAIN — needs live-tenant QA]: verify against a real Guide tenant that GET
        /api/v2/help_center/sections/{id} returns {"section": {"name": ...}} in the
        default locale (older Guide sites expose the locale-scoped
        /help_center/{locale}/sections/{id} shape instead — if so this returns None and
        harvest degrades to labels+title, never a wrong value)."""
        sid = str(section_id or "").strip()
        if not sid or not _NUMERIC_ID.fullmatch(sid):
            return None
        if sid in self._section_names:
            return self._section_names[sid] or None
        name = ""
        try:
            with self._client() as c:
                r = self._get(c, self._hc(f"/sections/{sid}"))
            if r.status_code == 200:
                name = ((r.json() or {}).get("section") or {}).get("name") or ""
        except Exception:
            name = ""
        self._section_names[sid] = name
        return name or None

    def show_article(self, article_id: str) -> dict | None:
        """Re-fetch one article in full (webhook payloads are THIN — id only — so
        the connector NEVER trusts payload content; it re-reads here)."""
        article_id = _zd_id(article_id, "article id")
        with self._client() as c:
            r = self._get(c, self._hc(f"/articles/{article_id}"))
        return r.json().get("article") if r.status_code == 200 else None

    # --- incremental export watermark backstop ---
    def incremental_articles(self, start_time: int) -> tuple[list[dict], int]:
        """GET /incremental/articles?start_time= — returns (articles, end_time).

        Time-window pagination (NOT cursor): chain the returned end_time as the
        next run's start_time. Up to 1000/page; agent/admin auth only. The caller
        stores end_time as the per-connection watermark."""
        arts: list[dict] = []
        st = int(start_time)
        end_time = st
        with self._client() as c:
            # Follow next_page (time-window) a bounded number of times per run.
            for _ in range(50):
                r = self._get(c, self._hc("/incremental/articles"), params={"start_time": st})
                if r.status_code != 200:
                    raise ConnectorError(
                        f"Zendesk incremental export failed ({r.status_code}): "
                        f"{vendor_excerpt(r.text)}"
                    )
                body = r.json()
                # #579 gate: an oversize changed article is never buffered. It is kept as a
                # small stub (its scalar fields only) so the flow's edited_at gate and scope
                # check still apply to it; _zd_prepare then refuses the stub per record and
                # the flow holds the watermark behind it, like any other over-limit record.
                limit = item_char_limit()
                for row in body.get("articles", []):
                    n = item_chars(row)
                    if n > limit and isinstance(row, dict):
                        row = {
                            k: v
                            for k, v in row.items()
                            if isinstance(v, (str, int, float)) and len(str(v)) <= 64
                        }
                        row["_oversize_chars"] = n
                        n = item_chars(row)
                    # #587: what the export keeps counts against the run's character budget.
                    self._charge_fetch(n)
                    arts.append(row)
                end_time = int(body.get("end_time") or st)
                nxt = body.get("next_page")
                # Zendesk signals "caught up" by count < 1000 / next_page repeating
                # the same start_time. Stop when there's no advance.
                if not nxt or end_time <= st:
                    break
                st = end_time
        return arts, end_time

    # --- extract: translatable units from an article ---
    def extract_units(self, article: dict, html_filter) -> tuple[list[dict], str]:
        """Article title (plain) + body (HTML -> the shared html filter). Returns
        (units, content_hash). Units are p_index-keyed: 0 = title, 1+ = body text
        nodes. content_hash is the per-source-content anti-loop hash."""
        title = article.get("title") or ""
        body = article.get("body") or ""
        body_units = html_filter.parse(body.encode("utf-8")).units
        units = [{"p_index": 0, "text": title, "field": "title"}]
        for i, u in enumerate(body_units):
            units.append({"p_index": 1 + i, "text": u.text, "field": "body"})
        return units, _content_hash(title, body)

    # --- write-back client (the SubResourceClient the strategy drives) ---
    def get_translation(self, record_id: str, locale: str):
        """GET /articles/{id}/translations/{locale} -> the translation dict, or
        strategy.ABSENT on 404. This is the EXPLICIT existence probe (never a
        content read) the create-vs-update decision needs."""
        record_id, locale = _zd_id(record_id, "article id"), _zd_locale(locale)
        with self._client() as c:
            r = self._get(c, self._hc(f"/articles/{record_id}/translations/{locale}"))
        if r.status_code == 200:
            return r.json().get("translation") or {}
        if r.status_code == 404:
            return ABSENT
        raise ConnectorError(
            f"Zendesk translation probe {record_id}/{locale} -> {r.status_code}: "
            f"{vendor_excerpt(r.text)}"
        )

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """POST a new translation object. Non-idempotent create -> retry only on
        429 (base._request idempotent=False)."""
        record_id, locale = _zd_id(record_id, "article id"), _zd_locale(locale)
        fields = dict(fields)
        fields.pop(_HELD_KEY, None)
        if not (fields.get("title") or "").strip():
            # Nothing usable (title is REQUIRED by Zendesk): every field is held for review.
            # Never create an empty or source-language variant (#640).
            return WriteResult(
                False, True, message="held-back: no translated title to create the locale with"
            )
        payload = {"translation": {"locale": locale, **fields}}
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "POST",
                    self._base() + self._hc(f"/articles/{record_id}/translations"),
                    json=payload,
                    idempotent=False,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            tid = str((r.json().get("translation") or {}).get("id") or "")
            return WriteResult(True, True, target_id=tid, message="created")
        return WriteResult(False, True, message=f"{r.status_code}: {vendor_excerpt(r.text)}")

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """PUT an existing translation (idempotent per locale — retries on 5xx).
        Also clears `outdated` so the locale stops showing as stale (zendesk.md
        §4; [UNCERTAIN] whether Zendesk auto-clears on PUT — we set it explicitly)."""
        record_id, locale = _zd_id(record_id, "article id"), _zd_locale(locale)
        body = dict(fields)
        held = bool(body.pop(_HELD_KEY, False))
        if "title" not in body and "body" not in body:
            return WriteResult(
                False, False, message="held-back: nothing translated to write; stored value kept"
            )
        # Gate (2026-10-10): read the stored translation before any PUT that could touch live
        # text or drop a field. Zendesk does not document whether a translation PUT merges or
        # replaces, nor what draft:true does to a PUBLISHED translation (unpublish it, or
        # change live text), so the write must be safe under every reading:
        #  - draft mode never touches a published translation (it would either unpublish the
        #    live page or put unreviewed machine text live); the pair fails visibly instead;
        #  - a held field is re-sent with its STORED value (never source text), so a
        #    replace-semantics PUT cannot blank it;
        #  - a partly held pair never publishes a stored draft.
        draft = body.get("draft")
        if draft is True or held or "title" not in body or "body" not in body:
            current = self.get_translation(record_id, locale)
            if current is ABSENT:
                return WriteResult(
                    False, False, message="translation disappeared before update; not written"
                )
            if draft is True and _is_published(current):
                return WriteResult(False, False, message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}")
            for key in ("title", "body"):
                if key not in body and current.get(key) is not None:
                    body[key] = current[key]
            if held and current.get("draft") is True:
                body["draft"] = True
        if not held:
            # A fully refreshed translation is no longer stale. A pair with a held field is
            # still partly stale, so the flag is left alone.
            body.setdefault("outdated", False)
        payload = {"translation": body}
        # Idempotent PUT retries a transient transport error but base._request re-raises
        # a persistent one — catch it here (parity with create_translation above, which
        # already did; the create-vs-update asymmetry fixed in freshdesk's client) so a
        # timed-out update is a clean WriteResult failure, never an escaping exception.
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._base() + self._hc(f"/articles/{record_id}/translations/{locale}"),
                    json=payload,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            tid = str((r.json().get("translation") or {}).get("id") or "")
            return WriteResult(True, False, target_id=tid, message="updated")
        return WriteResult(False, False, message=f"{r.status_code}: {vendor_excerpt(r.text)}")

    def is_live_published(self, record_id: str, locale: str) -> bool:
        """Is the stored (record, locale) translation PUBLISHED? One GET, no content read."""
        return _is_published(self.get_translation(record_id, locale))

    # --- anti-loop label write-back (article label_names is edited on the ARTICLE) ---
    def apply_labels(self, article: dict) -> tuple[bool, str]:
        """After a successful write-back, mutate the article's label_names:
        optionally remove the FIRST filter label (so it isn't re-picked) and/or add
        a `translated` label. Idempotent — a no-op set is skipped. This is a PUT on
        the ARTICLE, not a translation (zendesk.md gotcha #4)."""
        cfg = self.config or {}
        add = (cfg.get("translated_label") or "").strip()
        remove_trigger = str(cfg.get("remove_trigger_label") or "").strip() in ("1", "true", "yes")
        labels = list(article.get("label_names") or [])
        new = list(labels)
        if remove_trigger:
            flt = self._label_filter()
            if flt and flt[0] in new:
                new.remove(flt[0])
        if add and add not in new:
            new.append(add)
        if new == labels:
            return True, "no label change"
        payload = {"article": {"label_names": new}}
        aid = _zd_id(article.get("id"), "article id")
        with self._client() as c:
            r = self._request(
                c,
                "PUT",
                self._base() + self._hc(f"/articles/{aid}"),
                json=payload,
                **self._write_kwargs(),
            )
        ok = r.status_code in (200, 201)
        return ok, "labels updated" if ok else f"{r.status_code}: {vendor_excerpt(r.text)}"

    # --- generic Connector surface (pull/push) — used by the store pull/push path ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, field-unit) for the generic pull view. The
        rich flow is store.zendesk_translate (writes translation objects)."""
        from app.filters import get_filter

        html = get_filter("html")
        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h = self.extract_units(art, html)
            out.extend(self._pulled_items(units, str(art["id"]), art.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Zendesk writes per-locale translation objects — use 'Translate now'.")

    def detect_change(self, conn=None, watermark: int = 0):
        """Incremental-export poll backstop: changed articles since `watermark`
        (epoch), plus the new end_time watermark. edited_at gating happens in the
        caller (store.zendesk_translate) — the cardinal edited_at-not-updated_at
        rule."""
        return self.incremental_articles(start_time=watermark)


# --- register the connector's webhook verifier + payload extractors -------------
# Zendesk signs timestamp + body with HMAC-SHA256, encoded as base64.
def _verify_zendesk(secret: str, body: bytes, headers: dict) -> bool:
    """Zendesk signs timestamp + body; require freshness within five minutes of UTC."""
    from datetime import datetime, timezone

    normalized = {k.lower(): v for k, v in (headers or {}).items()}
    timestamp = normalized.get("x-zendesk-webhook-signature-timestamp", "")
    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if sent.tzinfo is None:
            return False
        if abs((datetime.now(timezone.utc) - sent).total_seconds()) > 300:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    verify = webhooks.hmac_sha256_verifier("X-Zendesk-Webhook-Signature", hex_digest=False)
    return verify(secret, timestamp.encode("utf-8") + body, headers)


webhooks.register_verifier(
    "zendesk",
    _verify_zendesk,
)


def _zendesk_changed_ids(body_json) -> list[str]:
    """Article ids from a (verified) article-event payload. THIN: the event/detail
    carries the article id; we return it and the job re-fetches the article."""
    body_json = body_json or {}
    ids = []
    detail = body_json.get("detail") or {}
    if detail.get("id"):
        ids.append(str(detail["id"]))
    event = body_json.get("event") or {}
    # Some article events put the id under event; take it too (deduped by caller).
    if event.get("id") and str(event["id"]) not in ids:
        ids.append(str(event["id"]))
    return ids


webhooks.register_changed_ids("zendesk", _zendesk_changed_ids)


# ===========================================================================
# Translate flow moved here from store.py (Wave 3 store.py decomposition).
# The per-connector hook bundle + zendesk_translate wrapper live with the
# connector; store.py keeps a thin delegator so store.zendesk_translate stays
# importable/monkeypatchable (jobs/CLI/webhook dispatch + verify suites).
# ===========================================================================
import logging

from app.services.connectors import flow, harvest

log = logging.getLogger("loc_tms.connectors")


# --------------------------- Zendesk hooks ---------------------------------
def _zd_preflight(ctx):
    return set(ctx.connector.enabled_locales()), {}


def _zd_detect_change(connector, watermark):
    inc_arts, _end = connector.detect_change(watermark=watermark)
    return inc_arts


def _zd_scope_ok(cfg, art):
    sec = (cfg.get("section_id") or "").strip()
    cat = (cfg.get("category_id") or "").strip()
    if sec and str(art.get("section_id") or "") != sec:
        return False
    if cat and str(art.get("category_id") or "") != cat:  # noqa: SIM103
        return False
    return True


def _zd_prepare(ctx, art):
    from app.filters import get_filter

    html = get_filter("html")
    # The live-kept park hook (_zd_parked) needs this run's draft flag and stored hashes.
    ctx.connector._run_draft = ctx.draft
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id"))
    if art.get("_oversize_chars"):
        # #579 gate: an incremental-export article that was too heavy to buffer.
        raise InputLimitError(
            f"Record {aid} is {art['_oversize_chars']} characters, over the fetch limit of "
            f"{item_char_limit()} (10 x MAX_SEGMENT_CHARS). It was not fetched into memory "
            "and no translation was started for it."
        )
    _units, chash = ctx.connector.extract_units(art, html)
    body = art.get("body") or ""
    body_units = html.parse(body.encode("utf-8")).units
    # Context harvest (Phase 3B): resolve the article's section id -> its NAME, gated on
    # harvest_context so a disabled connection adds NO fetch. section_name is cached on
    # the connector instance, so this resolves each distinct section ONCE per pull (a
    # section-scoped connection = one GET), never per-article. Kept OFF extra_skeleton
    # (persisted-shape parity); harvest.build_flow merges `harvest` in.
    section = None
    if harvest.harvest_enabled(ctx.cfg):
        section = ctx.connector.section_name(art.get("section_id"))
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=art.get("title") or "",
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": art.get("title")},
        doc_title=f"{(art.get('title') or 'article')[:50]} ({aid})",
        extra={"html": html, "body_bytes": body.encode("utf-8"), "title": art.get("title") or ""},
        # Phase 3A: label_names is already fetched as a NAME list (zendesk.py) ->
        # source.labels. Phase 3B: the resolved section NAME -> source.section.
        harvest={"labels": art.get("label_names") or [], "section": section},
    )


def _zd_is_suppressed(connector, aid, loc):
    # exists in Guide + hash unchanged -> suppress (a MISSING locale is written).
    return connector.strategy.exists_target(connector, aid, loc)


def _zd_build_payload(prep, loc, seg0_target, mapping):
    """Only fields that carry a usable translation. A held (NEEDS_REVIEW / refusal) title is
    OMITTED, and so is the body when ANY of its text units is held (the HTML body cannot be
    half-written: html.build would put source text in the held spots), so an existing stored
    translation is never overwritten with source-language text (defect taxonomy B, #640)."""
    html = prep.extra["html"]
    payload: dict = {}
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
        payload["body"] = html.build(prep.extra["body_bytes"], mapping).decode("utf-8")
    if held:
        payload[_HELD_KEY] = True
    return payload


def _zd_compute_draft(cfg):
    return str(cfg.get("draft", "1")).strip() not in ("0", "false", "no")


def _zd_parked(connector, aid, loc, chash):
    """Flow hook (review gate 2026-10-10, Freshdesk/Intercom parity): in draft mode a pair whose
    translation is already PUBLISHED is skipped BEFORE any MT is spent and reported as kept
    live (never written, no hash). update_translation repeats the check at write time (an
    explicit retry bypasses this hook)."""
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
    """Pairs left untouched because their translation is live and the run is in draft mode:
    the ones the park hook skipped before MT plus any refused at write time."""
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept_text(f.get("error"))
    ]


def _real_errors(ctx):
    return [e for e in ctx.errors if not _is_live_kept_text(e)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_live_kept_text(f.get("error"))]


def _zd_post_article(ctx, art):
    # anti-loop labels: only after at least one locale landed for this article.
    aid = str(art.get("id"))
    # A held or failed locale must stay retryable: removing the trigger label would drop the
    # article from the label-filtered discovery and the held pair would never be retried (#640).
    if any(
        str(row.get("article_id")) == aid
        for rows in (ctx.failures, ctx.partial_held, ctx.held_create, _live_kept(ctx))
        for row in rows
    ):
        return
    try:
        ok, why = ctx.connector.apply_labels(art)
        if not ok:
            # Surfaced, not fatal: the translations are written; the content-hash gate still
            # stops a re-translate loop even when the label could not be changed.
            ctx.errors.append(f"{aid}/labels: {why}")
    except Exception as e:
        ctx.errors.append(f"{aid}/labels: {type(e).__name__}: {e}")


def _zd_compute_ok(ctx):
    # ALL-REJECTED (no target enabled in Guide) is a misconfiguration -> fail loud.
    if ctx.rejected and not ctx.usable:
        return False
    # A live-kept pair (draft mode, published translation) is reported, not a failure.
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


def _zd_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translation(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not enabled in Guide: "
            f"{', '.join(ctx.rejected)}."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) translation(s) unchanged: {sample}. This "
            "connection writes drafts, so new machine text is never written into a published "
            "translation. Set Write as draft = 0 to update live translations."
        )
    errors = _real_errors(ctx)
    if errors:
        parts.append("Errors: " + "; ".join(errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())  # Cluster A: loud, per-run
        log.warning(
            "zendesk_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _zd_build_result(ctx):
    return {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "rejected": ctx.rejected,
        "watermark": ctx.max_edited,
        "failures": ctx.failures,
    } | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})


def zendesk_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Zendesk Guide articles into one or more locales, each written back
    as a per-locale TRANSLATION OBJECT via the T2 SubResourceStrategy.

    Thin wrapper over flow.run_translate_flow. Zendesk-specific behavior preserved
    via the spec hooks below: enabled-locale rejection (all-rejected = FAIL),
    content-hash suppression (missing locale always written), anti-loop labels, and
    the edited_at watermark + incremental-export backstop with the CARDINAL RULE
    (edited_at > watermark) and the FINDING-1 clamp (both owned by the engine now).
    only_pairs restricts the write loop to exactly those (article_id, locale) pairs.
    """
    from app.services.connectors.store import _epoch

    spec = flow.TranslateFlowSpec(
        kind="zendesk",
        no_conn_msg="No such Zendesk connection.",
        hash_key="zendesk_hashes",
        probe_label="Zendesk",
        preflight=_zd_preflight,
        prepare=_zd_prepare,
        is_suppressed=_zd_is_suppressed,
        build_payload=_zd_build_payload,
        compute_draft=_zd_compute_draft,
        compute_ok=_zd_compute_ok,
        build_message=_zd_build_message,
        build_result=_zd_build_result,
        watermark_key="zendesk_watermark",
        detect_change=_zd_detect_change,
        scope_ok=_zd_scope_ok,
        article_epoch=lambda a: _epoch(a.get("edited_at")),
        post_article=_zd_post_article,
        extra_counters=("parked_skipped",),
        parked=_zd_parked,
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
