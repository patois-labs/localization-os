"""Intercom Help Center connector — the FIRST T1 (field-level map) consumer of the
connector framework and the first NEW consumer of the shared translate-flow engine
(app/services/connectors/flow.py). Built from the vendor documentation.

Topology T1: an Article carries default-locale content at the TOP LEVEL
(`title`/`description`/`body`) PLUS a `translated_content` map keyed by locale.
Localizing locale X = writing X's key into that map. Write-back rides the shared
strategy.FieldMapStrategy (GET-merge-PUT the whole map — the framework's CARDINAL
T1/T3 rule, design §2); this module owns only the endpoints + auth + Intercom's
field shapes, locale-key mapping, and change-detection.

PROVENANCE: every endpoint below is [CONFIRMED] in intercom.md unless a comment
flags [UNCERTAIN] — those carry a guard + a documented fallback, never built
against as fact. The [UNCERTAIN] items surface in the connector's report for live
tenant QA.

FIVE HEADLINE GOTCHAS (intercom.md §7):
  (1) default_locale COUPLING. Top-level title/body ARE the default-locale content,
      NOT an "en row" inside translated_content. NEVER write a locale ==
      default_locale as a target (corrupts the source). Enforced twice: the flow's
      partition drops the CONFIGURED source locale globally, and the T1 strategy's
      write_target has a per-RECORD belt-and-suspenders on the article's own
      default_locale.
  (2) LOCALE-KEY EXACTNESS. Only the enumerated keys work; a bad key is SILENTLY
      ignored (no error). We map internal BCP-47 -> the exact Intercom key
      (_to_intercom_key): 'pt-br'->'pt-BR', 'zh-cn'->'zh-CN', Norwegian
      'nb'. An unmappable locale is rejected up front. REBUILD 2026-10-10: the current
      docs list 37 keys and no longer list 'de-form', so it is not offered any more
      (a formal-German request folds onto plain 'de').
  (3) MERGE-vs-REPLACE of the map is UNDOCUMENTED [UNCERTAIN]. We always GET-merge-
      PUT the full map (strategy.FieldMapStrategy) so we are correct either way.
  (4) PAID-PLAN GATING [UNCERTAIN tier]. The Articles API only returns data for
      workspaces with Help Center/Articles enabled. test()/discovery surface a clean
      "Articles product not reachable" rather than an opaque empty list.
  (5) REGION + VERSION pinning. Wrong regional base URL = data-not-found; unpinned
      version = payloads shift under you. Region (US/EU/AU) is a REQUIRED field and
      Intercom-Version is pinned per request.

REBUILD 2026-10-10 (un-archived, connector parity): the connector was archived 2026-08-06 only
because it was never live-tested. Brought to today's standard: API 2.16 pin, `author_id`
sent as an integer (the update endpoint's documented type), read-only fields stripped from
every block of the whole-map PUT, `help_center_id` applied WHILE paging (it used to filter
after the cap and starve), a held-back (NEEDS_REVIEW) body or title is OMITTED so the stored
translation survives (taxonomy B), an all-held pair writes nothing (taxonomy F), and the
shared #640 rules are wired (held_prefix). Review gate: in draft mode a PUBLISHED locale block
is never written (neither overwritten live with unreviewed text nor unpublished): the pair is
skipped before MT by the `parked` hook, refused again at write time, and reported as
`live_kept` in the run message and result. Live-tenant QA is pending.

CHANGE DETECTION: Intercom HAS article.* webhooks, but their signature scheme is
[UNCERTAIN] in the pulled docs, so this connector ships POLL-FIRST (Zoho precedent)
— we do NOT register a signature verifier (never guess a scheme). Polling gates on
content: Intercom exposes only `updated_at` (NOT a content-only `edited_at` like
Zendesk), so — honouring the CARDINAL edited_at-not-updated_at rule — we do NOT
advance a watermark off updated_at (it bumps on metadata too); the per-(article,
locale) source-content HASH is the real re-translate gate. See intercom.md §5.
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
from app.services.connectors.strategy import ABSENT, FieldMapStrategy, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt

API_VERSION = (
    "2.16"  # pinned stable Intercom-Version (the current docs default, 2026-10-10; unpinned drifts)
)

# Regional API base URLs (intercom.md §6.4). Region is a REQUIRED field: calling the
# wrong region silently returns data-not-found, so we pin the explicit base per
# connection rather than relying on api.intercom.io auto-routing.
_REGION_BASES = {
    "US": "https://api.intercom.io",
    "EU": "https://api.eu.intercom.io",
    "AU": "https://api.au.intercom.io",
}

# The EXACT `translated_content` locale keys Intercom accepts (intercom.md §1.4). A
# key outside this set is SILENTLY ignored on write, so we normalize/reject against
# it before every write-back.
SUPPORTED_LOCALES = (
    "ar",
    "bg",
    "bs",
    "ca",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "he",
    "hr",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "lt",
    "lv",
    "mn",
    "nb",
    "nl",
    "pl",
    "pt-BR",
    "pt",
    "ro",
    "ru",
    "sl",
    "sr",
    "sv",
    "tr",
    "vi",
    "zh-CN",
    "zh-TW",
)

_KEY_BY_LOWER = {k.lower(): k for k in SUPPORTED_LOCALES}

# Internal BCP-47 forms that don't fall out of a plain region-strip need an explicit
# alias to an exact Intercom key. Chinese is the sharp edge: 'zh' alone is NOT a
# valid Intercom key (only zh-CN / zh-TW), so a bare/Hans/Hant form must be pinned.
# Norwegian's key is 'nb'. [UNCERTAIN] nn->nb (Nynorsk has no distinct Intercom key;
# folded to nb rather than dropped — flagged for live QA).
_ALIAS = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hans-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hant-tw": "zh-TW",
    "pt-br": "pt-BR",
    "pt-pt": "pt",
    "nb": "nb",
    "nb-no": "nb",
    "no": "nb",
    "nn": "nb",
}


def _to_intercom_key(loc) -> str | None:
    """Map an internal locale (any case, region-qualified or not) to the EXACT
    Intercom `translated_content` key, or None when it can't be represented
    (gotcha #2 — a bad key is silently ignored, so an unmappable locale is REJECTED
    rather than written blind).

    Resolution order: exact alias -> exact supported key -> region-stripped alias ->
    region-stripped base language. e.g. 'de-DE'->'de', 'pt-br'->'pt-BR',
    'es-MX'->'es', 'zh-CN'->'zh-CN', 'de-form'->'de'."""
    if not loc:
        return None
    lc = str(loc).strip().lower()
    if not lc:
        return None
    if lc in _ALIAS:
        return _ALIAS[lc]
    if lc in _KEY_BY_LOWER:
        return _KEY_BY_LOWER[lc]
    base = lc.split("-")[0]
    if base in _ALIAS:
        return _ALIAS[base]
    if base in _KEY_BY_LOWER:
        return _KEY_BY_LOWER[base]
    return None


class _TextUnit:
    """A minimal translation unit (the flow reads only .text + .p_index). Used to
    carry the plain-text `description` field alongside the html filter's body units
    without routing it through the HTML skeleton — its p_index is the non-numeric
    sentinel '__desc__' so it can never collide with a body unit's integer index."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index):
        self.text = text
        self.p_index = p_index


DESC_PINDEX = "__desc__"


def _finalize_block(block: dict, *, draft: bool) -> dict:
    """Stamp the Intercom-required per-locale block fields AFTER the strategy's
    generic merge: the publish `state` (draft vs published, honouring the run's
    draft flag) and the `article_content` type discriminator. Kept here (not in the
    vendor-neutral strategy) so FieldMapStrategy stays Intercom-agnostic."""
    out = dict(block)
    if draft:
        # #465: draft mode only marks a NEWLY created block as a draft. An existing block
        # arrives here merged onto its current content and already carries its live `state`;
        # overwriting a published locale with "draft" would unpublish it from the Help Center.
        out.setdefault("state", "draft")
    else:
        out["state"] = "published"
    out.setdefault("type", "article_content")
    return out


SPEC = ConnectorSpec(
    kind="intercom",
    name="Intercom Help Center",
    icon="\U0001f4ac",  # speech balloon
    cat="Knowledge base / help center",
    blurb="Translate Intercom Help Center articles into any language. Each translation "
    "is written into the article's per-locale translated_content map (draft by "
    "default) via a GET-merge-PUT of the whole map, so the source article and "
    "other locales are never overwritten.",
    config_fields=[
        FieldSpec(
            "region",
            "Region",
            "Workspace region, pins the API base (US/EU/AU). "
            "The wrong region silently returns data-not-found.",
            required=True,
            placeholder="US",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The workspace default/authoring locale "
            "(the article's default_locale — its top-level content). Never written as a "
            "target. Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "author_id",
            "Localization author id",
            "Intercom teammate id stamped as author "
            "on written translations (must be a Help Center teammate). Blank = reuse the "
            "source article's author.",
            required=False,
            placeholder="123456",
        ),
        FieldSpec(
            "help_center_id",
            "Help Center id",
            "Limit discovery to one Help Center (optional).",
            required=False,
            placeholder="12345",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locales to write, mapped "
            "to Intercom's exact keys (pt-BR, zh-CN, nb…); an unmappable locale is "
            "rejected.",
            required=False,
            placeholder="de, fr, pt-BR",
        ),
        FieldSpec(
            "publish",
            "Publish after write",
            "'1' = write the translation with the "
            "'published' state; '0' = write a new locale as 'draft' and leave an "
            "existing locale's state as it is (default, safe). In draft mode a locale "
            "that is already published is never overwritten: it is reported as kept live.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "api_version",
            "API version",
            "Intercom-Version header to pin (default "
            f"{API_VERSION}). Do not use the Unstable channel in production.",
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
            "access_token",
            "Access token",
            "An Intercom Access Token (private/internal "
            "app) or OAuth token with read + write articles scopes. Sent as Bearer.",
            secret=True,
            placeholder="dG9rZW4t…",
        ),
    ],
    setup_steps=[
        "In the Intercom Developer Hub, create an app (or use your workspace's internal app) and "
        "grant the 'Read and list articles' + 'Read and write articles' scopes.",
        "Copy an Access Token for your workspace and paste it as the access token above.",
        "Set the Region to your workspace's region (US / EU / AU) — the wrong region returns no "
        "data.",
        "Set the Source locale to your workspace default/authoring language (e.g. 'en'); it is "
        "never written as a target.",
        "Optionally set a Localization author id (a Help Center teammate) — required by Intercom "
        "on each written translation; blank reuses the source article's author.",
        "Intercom has article.* webhooks, but this connector polls first — run on a schedule "
        "(Every N minutes on the connection page) so the source-content hash keeps translations "
        "fresh.",
    ],
    other_side="Intercom change detection polls the article list (updated_at desc) and gates "
    "re-translation on a per-locale source-content hash — updated_at bumps on "
    "metadata too, so it never drives a watermark. Webhook intake is a documented "
    "follow-up (signature scheme unverified).",
    docs_url="https://developers.intercom.com/docs/references/rest-api/api.intercom.io/articles",
    supports_oauth=False,  # access-token header path here; full OAuth authorize is a follow-up.
    archived=False,  # rebuilt 2026-10-10 (connector parity); live QA is pending (runbook)
)

# Fields of a stored per-locale block that Intercom computes. They are never sent back on
# the whole-map PUT (a write of a read-only key is rejected or ignored depending on version).
_READ_ONLY_BLOCK_KEYS = (
    "created_at",
    "updated_at",
    "url",
    "has_unpublished_changes",
    "draft_updated_at",
    "created_by_id",
    "updated_by_id",
)
HELD_BACK_PREFIX = "held-back:"
# Review gate 2026-10-10: a draft-mode run never writes into a locale block that is LIVE
# (state published). Writing it would put unreviewed machine text on the Help Center, and
# re-stamping it 'draft' would unpublish it (#465). The pair is left untouched and reported.
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the locale is PUBLISHED and this connection writes drafts, so the live text was left "
    "unchanged; turn on 'Publish after write' to update live translations, or unpublish it "
    "in Intercom and it is translated on the next run"
)

# Intercom article ids are numeric. An id reaches the URL path, so anything else (``..``,
# ``%2e%2e``, ``?``, ``#``, CR/LF, NUL, unicode digits) is refused before a request is built
# (taxonomy A). Compiled ASCII-only so a unicode digit cannot match.
_ARTICLE_ID_RE = re.compile(r"[0-9]{1,20}", re.ASCII)
_API_VERSION_RE = re.compile(r"(?:[0-9]{1,2}\.[0-9]{1,2}|Unstable|Preview)", re.ASCII)


def _safe_article_id(value) -> str:
    sid = str(value).strip()
    if not _ARTICLE_ID_RE.fullmatch(sid):
        raise ConnectorError(f"Refused article id {sid[:40]!r}: Intercom article ids are numeric.")
    return sid


def _author_value(v):
    """The update endpoint documents `author_id` as an integer; an all-digit id is sent as
    one. Anything else is passed through untouched so a bad value fails loudly at Intercom
    instead of being rewritten here."""
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return v


class _IntercomStrategy(FieldMapStrategy):
    """FieldMapStrategy plus the one T1 rule the stock class cannot know: a payload that
    carries NO translated field (every unit of the pair was held for review) must not
    create or touch a locale block. Reported as held-back, never as success, so no content
    hash is recorded and the pair is retried after approval (taxonomy B/F, #640)."""

    def write_target(self, client, record_id, target_locale, fields, *, draft=True):
        if not any((fields or {}).get(k) for k in ("title", "description", "body")):
            return WriteResult(
                False,
                False,
                message=f"{HELD_BACK_PREFIX} nothing in this pair is translated yet "
                "(NEEDS_REVIEW); nothing was written and it will be retried next run",
            )
        if draft and self.is_live(client, record_id, target_locale):
            return WriteResult(False, False, message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}")
        return super().write_target(client, record_id, target_locale, fields, draft=draft)

    def is_live(self, client, record_id, target_locale) -> bool:
        """Is this locale's block on the article PUBLISHED (live on the Help Center)?"""
        rec = client.get_record(str(record_id))
        if rec is ABSENT or not isinstance(rec, dict):
            return False
        block = (rec.get(self._map_key) or {}).get(self._key(target_locale))
        return isinstance(block, dict) and str(block.get("state") or "").lower() == "published"


class IntercomConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="intercom",
        topology=Topology.T1,  # FIRST T1 consumer (field-level map)
        auth=(AuthKind.API_KEY, AuthKind.OAUTH3LO),  # Bearer access token or OAuth
        api_version=API_VERSION,
        rich_text=RichText.HTML,  # body is HTML -> the shared html filter
        regions="fixed-region",  # US/EU/AU base-URL pin (the region primitive)
        change_signals=ChangeSignals(
            webhook_sig=None,  # webhooks EXIST but scheme UNCERTAIN -> poll-first
            # CARDINAL RULE: Intercom exposes only updated_at (bumps on metadata), NOT
            # a content-only edited_at. We record the field we poll but do NOT advance
            # a watermark off it — the source-content hash is the real gate.
            poll_field="updated_at",
            stale_flag=None,  # no per-locale stale flag -> hash is the gate
            thin_payload=True,
        ),  # (future) webhook events carry id only -> re-fetch
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # T1 strategy wired with Intercom's shapes. The strategy owns the generic
        # GET-merge-PUT + the key-presence create/update decision; this connector
        # supplies get_record/put_record (below) + the locale-key mapper + the
        # per-block finalizer.
        self.strategy = _IntercomStrategy(
            source_locale_of=lambda conn, rec: (
                (rec or {}).get("default_locale") or self._source_locale()
            ),
            locale_key=_to_intercom_key,
            finalize_block=_finalize_block,
            map_key="translated_content",
        )

    # --- auth / base url ---
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en").strip().lower() or "en"

    def _region(self) -> str:
        raw = (self.config.get("region") or "").strip().upper()
        if not raw:
            raise ConnectorError("Intercom region is required (US, EU, or AU).")
        if raw not in _REGION_BASES:
            raise ConnectorError(
                f"Invalid Intercom region {self.config.get('region')!r} — use US, EU, or AU."
            )
        return raw

    def _base(self) -> str:
        base = _REGION_BASES[self._region()]
        _validate_target(base)  # SSRF guard (base._validate_target) on every request path
        return base

    def _api_version(self) -> str:
        v = (self.config.get("api_version") or API_VERSION).strip() or API_VERSION
        if not _API_VERSION_RE.fullmatch(v):
            raise ConnectorError("Intercom API version must look like 2.16 (or Unstable).")
        return v

    def _auth_kwargs(self) -> dict:
        """Per-request auth + the pinned Intercom-Version header (intercom.md §6).
        Header: `Authorization: Bearer <access_token>` + `Intercom-Version: <x.y>`.
        A missing token is a hard config error, never a silent 4xx."""
        token = (self.secret.get("access_token") or "").strip()
        if not token:
            raise ConnectorError("Intercom access token is required.")
        if not token.isprintable() or " " in token:
            # httpx would echo an illegal header value in its error; never let the token out.
            raise ConnectorError("Intercom access token contains illegal characters.")
        return {
            "headers": {"Authorization": f"Bearer {token}", "Intercom-Version": self._api_version()}
        }

    def _get(self, c, path, **kw):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        return self._request(c, "GET", self._base() + path, headers=headers, **akw, **kw)

    def _write_kwargs(self) -> dict:
        akw = self._auth_kwargs()
        akw["headers"] = {"Content-Type": "application/json", **akw.pop("headers", {})}
        return akw

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                # A cheap authed Articles call that also proves the region + the
                # Articles product is reachable (gotcha #4).
                r = self._get(c, "/articles", params={"per_page": 1})
            if r.status_code == 200:
                total = (r.json() or {}).get("total_count")
                if total is None:
                    return True, "Connected to Intercom Help Center."
                return True, f"Connected to Intercom Help Center ({total} article(s))."
            if r.status_code in (401, 403):
                return False, (
                    f"Intercom rejected the token ({r.status_code}) — check the "
                    "access token, its article scopes, and that the region matches "
                    "your workspace."
                )
            if r.status_code == 404:
                return False, (
                    "Intercom Articles not reachable (404) — the workspace may not "
                    "have the Help Center / Articles product enabled."
                )
            return False, f"Intercom returned {r.status_code}: {vendor_excerpt(r.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: Help Center collections (id + name).
        Best-effort — returns [] if the token lacks the scope or the product is off."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, "/help_center/collections", params={"per_page": 50})
            if r.status_code != 200:
                return []
            for col in r.json().get("data") or []:
                cid = str(col.get("id") or "")
                name = col.get("name") or cid
                out.append({"id": cid, "name": f"{name} (collection {cid})", "tables": []})
        except Exception:
            return out
        return out

    # --- discover: articles ---
    def enabled_locales(self) -> list[str]:
        """Intercom's writable locale keys are a fixed enumerated set (intercom.md
        §1.4) — there is no per-workspace 'enabled locales' endpoint; a key outside
        the set is silently ignored. Return the enumerated set; the flow's partition
        maps + rejects a target that can't be represented."""
        return list(SUPPORTED_LOCALES)

    def _paginate_articles(self, c, cap: int, help_center_id: str = "") -> list[dict]:
        """Cursor-paginate GET /articles (sorted updated_at desc, intercom.md §2.3).
        Envelope: {data:[...], pages:{next:{starting_after}|null}}. Stops when `next`
        is absent. Dedupes by id (the cursor is stateless — mid-walk edits can
        duplicate rows, intercom.md gotcha #8). Bounded page count per run."""
        out: list[dict] = []
        seen: set = set()
        per_page = min(150, max(1, cap))
        starting_after = None
        for _ in range(50):  # bounded pages per run
            params: dict = {"per_page": per_page}
            if starting_after:
                params["starting_after"] = starting_after
            r = self._get(c, "/articles", params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Intercom list /articles failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            for row in self._keep_rows(body.get("data") or []):
                rid = str(row.get("id")) if isinstance(row, dict) else None
                if rid is None or rid in seen:
                    continue
                # Scope BEFORE the cap: filtering a capped list afterwards let other Help
                # Centers' articles fill the cap and hide the configured one (taxonomy E).
                if help_center_id and str(row.get("help_center_id") or "") != help_center_id:
                    continue
                seen.add(rid)
                out.append(row)
                if len(out) >= cap:
                    return out[:cap]
            pages = body.get("pages") or {}
            nxt = pages.get("next")
            # `next` is a dict {starting_after} on modern versions; some versions
            # return a bare URL string. We only follow the documented dict form —
            # parsing an opaque URL cursor is [UNCERTAIN], so we stop rather than
            # guess (a stop just means the safety cap governs; nothing is written wrong).
            if isinstance(nxt, dict) and nxt.get("starting_after"):
                starting_after = nxt.get("starting_after")
            else:
                break
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """List translatable source articles.

        Precedence mirrors the sibling connectors: explicit article_ids (webhook/
        retry path) win and are re-fetched in full by id; else the article list is
        walked (updated_at desc) and scoped to help_center_id when configured. List
        rows already carry the top-level source fields we extract (title/description/
        body), so the list is used directly — the full translated_content map (needed
        only for the existence check) is fetched lazily via get_record at write time.
        """
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cfg = self.config or {}
        with self._client() as c:
            if article_ids:
                out = []
                hc = (cfg.get("help_center_id") or "").strip()
                for aid in list(article_ids)[:cap]:
                    art = self._fetch_article(c, _safe_article_id(aid))
                    # A caller-supplied id must stay inside the configured Help Center: an
                    # article of another one answers like a missing one (scope check).
                    if art is not None and hc and str(art.get("help_center_id") or "") != hc:
                        art = None
                    if art is not None:
                        out.append(self._charge_record(art))
                return out
            rows = self._paginate_articles(c, cap, (cfg.get("help_center_id") or "").strip())
        return rows

    def _fetch_article(self, c, article_id: str) -> dict | None:
        """GET /articles/{id} -> the full Article dict (incl. translated_content), or
        None on 404. Sets id defensively."""
        r = self._get(c, f"/articles/{_safe_article_id(article_id)}")
        if r.status_code == 200:
            art = r.json()
            if isinstance(art, dict):
                art.setdefault("id", article_id)
                return art
        return None

    def show_article(self, article_id: str) -> dict | None:
        with self._client() as c:
            return self._fetch_article(c, str(article_id))

    # --- extract: translatable units from an article ---
    def extract_units(self, article: dict, html_filter) -> tuple[list[dict], str]:
        """Article title (plain, order 0) + description (plain) + body (HTML -> the
        shared html filter). Returns (units, content_hash). content_hash is the
        poll-suppression hash of title + description + raw body. Units are field-
        tagged for the generic pull view; the rich flow (store.intercom_translate)
        builds its own p_index-keyed unit list."""
        title = article.get("title") or ""
        description = article.get("description") or ""
        body = article.get("body") or ""
        units = [{"p_index": 0, "text": title, "field": "title"}]
        if description.strip():
            units.append({"p_index": DESC_PINDEX, "text": description, "field": "description"})
        for i, u in enumerate(html_filter.parse(body.encode("utf-8")).units):
            units.append({"p_index": 1 + i, "text": u.text, "field": "body"})
        return units, _content_hash(title, description, body)

    # --- write-back client (the FieldMapClient the T1 strategy drives) ---
    def get_record(self, record_id: str):
        """GET /articles/{id} -> the full Article dict (the strategy merges the target
        locale into its translated_content map), or strategy.ABSENT on 404."""
        with self._client() as c:
            r = self._get(c, f"/articles/{_safe_article_id(record_id)}")
        if r.status_code == 200:
            body = r.json()
            return body if isinstance(body, dict) else ABSENT
        if r.status_code == 404:
            return ABSENT
        raise ConnectorError(
            f"Intercom article {record_id} read -> {r.status_code}: {vendor_excerpt(r.text)}"
        )

    @staticmethod
    def _clean_container(container: dict) -> dict:
        """The whole map to PUT: every stored block keeps its writable fields only
        (read-only server fields stripped) and a numeric author id goes out as an integer."""
        out: dict = {}
        for key, blk in (container or {}).items():
            if not isinstance(blk, dict):
                continue
            nb = {k: v for k, v in blk.items() if k not in _READ_ONLY_BLOCK_KEYS}
            if "author_id" in nb:
                nb["author_id"] = _author_value(nb["author_id"])
            out[key] = nb
        return out

    def put_record(self, record_id: str, container: dict, *, created: bool) -> WriteResult:
        """PUT /articles/{id} with the merged translated_content map. Idempotent
        (replace-in-place of the whole map by id) -> retries on 5xx via base._request.
        A transient transport error lands as a clean per-pair failure, never an
        escaping exception (freshdesk parity)."""
        payload = {"translated_content": self._clean_container(container)}
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._base() + f"/articles/{_safe_article_id(record_id)}",
                    json=payload,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, created, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            # Read-back (taxonomy F): Intercom silently ignores a locale key the Help Center
            # does not have enabled. The PUT answers with the stored article, so a key we sent
            # that is missing from it was NOT stored: a failure, never a recorded success.
            try:
                stored = (r.json() or {}).get("translated_content")
            except ValueError:
                stored = None
            if isinstance(stored, dict):
                lost = [k for k in payload["translated_content"] if k not in stored]
                if lost:
                    return WriteResult(
                        False,
                        created,
                        message=f"Intercom did not store locale {', '.join(sorted(lost))} "
                        "(enable that language on the Help Center first)",
                    )
            return WriteResult(
                True, created, target_id=str(record_id), message="created" if created else "updated"
            )
        return WriteResult(False, created, message=f"{r.status_code}: {vendor_excerpt(r.text)}")

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, field-unit) for the generic pull view. The
        rich flow is store.intercom_translate (writes the translated_content map)."""
        from app.filters import get_filter

        html = get_filter("html")
        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h = self.extract_units(art, html)
            out.extend(self._pulled_items(units, str(art.get("id")), art.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Intercom writes the article's translated_content map — use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list source articles for the caller to diff
        against the persisted per-(article,locale) content hash. Returns
        (articles, 0) — Intercom has no content-only export cursor (updated_at is
        metadata-tainted, intercom.md §5), so the hash in store.intercom_translate is
        the actual change gate; there is deliberately NO watermark to advance."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow moved here from store.py (Wave 3 store.py decomposition).
# The per-connector hook bundle + intercom_translate wrapper live with the
# connector; store.py keeps a thin delegator so store.intercom_translate stays
# importable/monkeypatchable (jobs/CLI/webhook dispatch + verify suites).
# ===========================================================================
import logging

from app.services.connectors import flow

log = logging.getLogger("loc_tms.connectors")


# --------------------------- Intercom hooks --------------------------------
# The FIRST T1 flow: one Article carries a per-locale `translated_content` map;
# write-back is a GET-merge-PUT of the whole map via strategy.FieldMapStrategy.
# Poll-first (webhook scheme UNCERTAIN) + NO watermark (Intercom exposes only the
# metadata-tainted updated_at, never a content-only edited_at — the CARDINAL RULE),
# so the per-(article,locale) source-content HASH is the sole re-translate gate.
def _ic_preflight(ctx):
    # enabled = the target locales that map to a valid Intercom key; source_key is the
    # configured source locale's Intercom key (never written as a target, gotcha #1).
    from app.services.connectors.intercom import _to_intercom_key

    src_key = _to_intercom_key(ctx.cfg.get("source_locale") or "en")
    enabled = {l for l in ctx.locales if _to_intercom_key(l)}  # noqa: E741
    return enabled, {"source_key": src_key}


def _ic_partition(locales, enabled, extra):
    # Reject an unmappable locale (a bad Intercom key is silently ignored, gotcha #2)
    # AND the configured source locale (writing it corrupts the source, gotcha #1).
    from app.services.connectors.intercom import _to_intercom_key

    src_key = extra.get("source_key")
    rejected, usable = [], []
    for l in locales:  # noqa: E741
        key = _to_intercom_key(l)
        if not key or (src_key and key == src_key):
            rejected.append(l)
        else:
            usable.append(l)

    # D1: fold usable locales that resolve to the SAME Intercom key onto ONE writer.
    # Generic over the _ALIAS many-to-one map (e.g. both 'nn' and 'nb' fold to 'nb'):
    # two usable locales writing the same translated_content key clobber each other
    # (last-writer wins) while BOTH record a success hash -> one translation is lost
    # forever. Keep the canonical locale (whose own value already IS the Intercom
    # key); reject the colliders with a clear reason that surfaces in the run message
    # like any other rejection.
    def _is_canonical(loc, key):
        return str(loc).strip().lower() == str(key).strip().lower()

    winner: dict = {}  # intercom key -> the winning locale from `usable`
    for l in usable:  # noqa: E741
        key = _to_intercom_key(l)
        cur = winner.get(key)
        if cur is None or (_is_canonical(l, key) and not _is_canonical(cur, key)):
            winner[key] = l
    deduped = []
    for l in usable:  # noqa: E741
        key = _to_intercom_key(l)
        if winner.get(key) is l:
            deduped.append(l)
        else:
            rejected.append(f"{l} collides with {winner[key]} on Intercom locale key '{key}'")
    return rejected, deduped


def _ic_prepare(ctx, art):
    from app.filters import get_filter
    from app.services.connectors.intercom import DESC_PINDEX, _content_hash, _TextUnit

    html = get_filter("html")
    # The live-kept park hook (_ic_parked) needs this run's draft flag and stored hashes.
    ctx.connector._run_draft = ctx.draft
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id"))
    title = art.get("title") or ""
    description = art.get("description") or ""
    body = art.get("body") or ""
    if not (title.strip() or description.strip() or body.strip()):
        # Nothing translatable at all -> skip without a write (never flip an article
        # to empty content).
        return flow.SkipArticle("empty_skipped")
    chash = _content_hash(title, description, body)
    # description rides as a non-HTML unit at the sentinel p_index so it is translated
    # but never routed through the body HTML skeleton (build_payload pulls it back out).
    body_units = []
    if description.strip():
        body_units.append(_TextUnit(description, DESC_PINDEX))
    body_units.extend(html.parse(body.encode("utf-8")).units)
    # Intercom requires a valid teammate author on every written locale block
    # (gotcha #6): the configured localization author wins, else reuse the source
    # article's own author_id.
    author = (ctx.cfg.get("author_id") or "").strip() or str(art.get("author_id") or "")
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title,
        body_units=body_units,
        extra_skeleton={
            "article_id": aid,
            "title": title,
            "default_locale": art.get("default_locale"),
        },
        doc_title=f"{title[:50] or 'article'} ({aid})",
        extra={
            "html": html,
            "body_bytes": body.encode("utf-8"),
            "title": title,
            "description": description,
            "author_id": author,
        },
    )


def _ic_is_suppressed(connector, aid, loc):
    # exists in the article's translated_content map + hash unchanged -> suppress; a
    # MISSING locale is always written.
    return connector.strategy.exists_target(connector, aid, loc)


def _ic_build_payload(prep, loc, seg0_target, mapping):
    from app.services.connectors.intercom import DESC_PINDEX

    ex = prep.extra
    html = ex["html"]
    body_mapping = {k: v for k, v in mapping.items() if k != DESC_PINDEX}
    payload = {}
    if ex["author_id"]:
        payload["author_id"] = _author_value(ex["author_id"])
    # Taxonomy B: html.build leaves a held body unit as SOURCE text, and the whole-map PUT
    # would then overwrite a stored good translation of that paragraph with the source
    # language. If ANY body unit is held the whole body is omitted instead (the merge keeps
    # the stored body; a first-time create is refused earlier by the shared #640 gate).
    body_held = any(
        str(u.p_index) not in mapping and (u.text or "").strip()
        for u in prep.body_units
        if u.p_index != DESC_PINDEX
    )
    if not body_held:
        payload["body"] = html.build(ex["body_bytes"], body_mapping).decode("utf-8")
    # Title mirrors the `description` posture directly below, NOT the T2 flows'
    # source-title fallback: Intercom is T1 (GET-merge-PUT of the WHOLE
    # translated_content map; strategy.FieldMapStrategy.write_target does
    # `block.update(fields)` — a per-KEY merge onto the EXISTING block). Substituting
    # the source title when the title seg is NEEDS_REVIEW-excluded would overwrite a
    # previously-good target-locale title on UPDATE (the D2 description-defect class
    # reintroduced for title). Emit `title` ONLY when we have an actual translation;
    # omitting the key lets the merge preserve the existing target title on update,
    # and a brand-new block simply carries no title (same posture description takes).
    # The four T2 flows (zendesk/document360/freshdesk/zoho) are full-object PUTs with
    # no GET-merge, so they keep the source-title fallback — omission there would risk
    # nulling the title outright.
    if seg0_target is not None:
        payload["title"] = seg0_target
    # D2: emit `description` ONLY when we actually have a translation for it. When the
    # description segment is absent from `mapping` (NEEDS_REVIEW-excluded or empty),
    # OMIT the key entirely so the GET-merge-PUT preserves the existing target-locale
    # description instead of clobbering a previously-good target with source-language
    # text (the whole-map PUT would otherwise overwrite it). Never substitute the
    # source as a stand-in translation, including on the create path — an absent
    # description on a fresh block is cleaner than source text posing as a translation.
    t_desc = mapping.get(DESC_PINDEX)
    if t_desc is not None and t_desc.strip():
        payload["description"] = t_desc
    return payload


def _ic_parked(connector, aid, loc, chash):
    """Flow hook (review gate 2026-10-10): in draft mode a pair whose locale block is already
    PUBLISHED is skipped BEFORE any MT is spent and reported as kept live (never written).
    Only pairs that would otherwise be translated are probed (no stored hash for this
    source); the probe runs every such run, so unpublishing in Intercom or turning on
    'Publish after write' clears it with no stored state. The strategy repeats the check at
    write time (an explicit retry bypasses this hook)."""
    if not getattr(connector, "_run_draft", True):
        return False
    if (getattr(connector, "_run_hashes", None) or {}).get(f"{aid}:{loc}") == chash:
        return False  # unchanged source: the normal suppression path decides
    try:
        live = connector.strategy.is_live(connector, aid, loc)
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


def _is_held_back(f):
    return str((f or {}).get("error") or "").startswith(HELD_BACK_PREFIX)


def _is_live_kept(f):
    return str((f or {}).get("error") or "").startswith(LIVE_KEPT_PREFIX)


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _live_kept(ctx):
    """Pairs left untouched because their locale is live and the run is in draft mode:
    the ones the park hook skipped before MT plus any refused at write time."""
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept(f)
    ]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f) and not _is_live_kept(f)]


def _ic_compute_ok(ctx):
    # ALL-REJECTED (no target maps to a usable Intercom key / all == source) is a
    # misconfiguration -> fail loud. Held-back pairs (#640) are reported but are not a
    # failure of the connection: they are retried once approved.
    if ctx.rejected and not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _ic_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translation(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} empty article(s) (no translatable content)."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not mappable to an Intercom "
            f"key (or equal to the source): {', '.join(ctx.rejected)}."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (article, locale) pair(s) awaiting review (NEEDS_REVIEW): "
            "the held fields kept their stored value (or the locale was not created) and the "
            "pair will be re-attempted next run."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) translation(s) unchanged: {sample}. This "
            "connection writes drafts, so new machine text is never written into a published "
            "locale. Turn on 'Publish after write' to update live translations, or unpublish "
            "them in Intercom and they are translated on the next run."
        )
    real_errors = [e for e in ctx.errors if HELD_BACK_PREFIX not in e and LIVE_KEPT_PREFIX not in e]
    if real_errors:
        parts.append("Errors: " + "; ".join(real_errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())  # Cluster A: loud, per-run
        log.warning(
            "intercom_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _ic_build_result(ctx):
    return {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "failures": ctx.failures,
        "held_back": len(_held_back(ctx)),
    } | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})


def intercom_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Intercom Help Center articles into one or more locales, each written
    into the article's per-locale `translated_content` map via the T1
    FieldMapStrategy (GET-merge-PUT of the whole map, so the source and other locales
    are never overwritten).

    Thin wrapper over flow.run_translate_flow — the FIRST NEW consumer of the shared
    engine and the FIRST T1 flow. Intercom-specific behavior via hooks: BCP-47 ->
    exact-Intercom-key mapping with unmappable-locale + source-locale rejection
    (all-rejected = FAIL), title+description+body extraction (description carried as a
    non-HTML unit), content-hash suppression (a missing locale is always written),
    author-id stamping, and the publish toggle (draft by default). Poll-first, so NO
    watermark (updated_at is metadata-tainted — the content hash is the gate).
    only_pairs restricts the write loop to those (article_id, locale) pairs.
    """
    spec = flow.TranslateFlowSpec(
        kind="intercom",
        no_conn_msg="No such Intercom connection.",
        hash_key="intercom_hashes",
        probe_label="Intercom",
        preflight=_ic_preflight,
        prepare=_ic_prepare,
        is_suppressed=_ic_is_suppressed,
        build_payload=_ic_build_payload,
        compute_draft=lambda cfg: str(cfg.get("publish", "0")).strip() not in ("1", "true", "yes"),
        compute_ok=_ic_compute_ok,
        build_message=_ic_build_message,
        build_result=_ic_build_result,
        extra_counters=("empty_skipped", "parked_skipped"),
        partition=_ic_partition,
        held_prefix=HELD_BACK_PREFIX,
        parked=_ic_parked,
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
