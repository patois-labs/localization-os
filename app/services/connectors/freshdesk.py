"""Freshdesk (Freshworks) Solutions / Knowledge-base connector — the FOURTH T2
consumer of the connector framework (built from the vendor documentation).

Topology T2: every Solution ARTICLE owns a per-language translation addressed by a
`{lang_code}` in the URL path — `GET/POST/PUT /api/v2/solutions/articles/{art_id}/
{lang}` IS the entire localization mechanism (the master article is never
overwritten). That write-back rides the shared strategy.SubResourceStrategy, exactly
like Zendesk/Document360; this module owns only the endpoints + auth + the
Freshdesk-specific field shapes and change-detection rules.

PROVENANCE: every endpoint below is [CONFIRMED] in freshdesk.md unless a comment
flags [UNCERTAIN] — those carry a guard + a documented fallback, never built
against as fact.

FOUR HEADLINE CONSTRAINTS (per the brief):

  (1) POLL-ONLY change detection. Freshdesk Solutions have NO webhooks AND NO
      `updated_since` filter (freshdesk.md §5) → a full-tree walk
      (categories → folders → articles, paginated) diffing each master article's
      `updated_at`. The PRIMARY suppression is a per-article source content-hash
      stored per (article_id, lang) key via store.patch_config_merge (the exact
      Document360 pattern); `updated_at` is only a coarse gate. State is persisted
      per-connection via patch_config_merge (concurrency-safe, REQUIRED).

  (2) IMMUTABLE PRIMARY LANGUAGE. Freshdesk's master language is chosen when
      multilingual is enabled and CANNOT change. The connector READS the portal's
      primary language from the API (`primary_language`) and treats it as the
      source — it never assumes `en`. Attempting to translate INTO the primary code
      is nonsensical and is dropped.

  (3) PLAN-GATED MULTILINGUAL (Pro/Enterprise). On lower plans the `{lang_code}`
      translation endpoints don't function. A 4xx there surfaces CLEANLY (a clear
      per-article "upgrade required / plan-gated" error, run NOT green), never a
      crash.

  (4) MASTER MUST EXIST AND BE PUBLISHED before translations. You cannot attach a
      translation to a missing master, and a translation of a DRAFT master orphans
      (freshdesk.md gotcha #3). The flow guards: master present → master published
      → then POST/PUT translation; otherwise a clear per-article error.

STATUS: `status` 1 = draft, 2 = published (freshdesk.md §4). Translations land as
DRAFT (status 1) by default so machine/AI output is reviewed; a `publish` toggle
flips them to published (status 2), mirroring Zendesk/Document360's draft flag.

REBUILD 2026-10-10 (un-archived, connector parity): archived 2026-08-06 only because it was never
live-tested. Brought to today's standard: caller-supplied article ids and language codes are
validated before they reach a URL and scope-checked against the configured category/folder
(taxonomy A); the vendor's own language-code casing (ru-RU) is written, not the lowercased
internal form; a held-back (NEEDS_REVIEW) title or body is NOT replaced by source text on an
update - the stored translation field is kept - and an all-held pair writes nothing
(taxonomy B/F, #640, held_prefix wired); in draft mode a PUBLISHED translation is never
written at all (review gate: neither unpublished nor overwritten live with unreviewed text): the
pair is skipped before MT by the `parked` hook, refused again at write time, and reported as
`live_kept` in the run message and result; article discovery is spread across folders
instead of the first folder taking the whole max_articles cap (taxonomy E).

DEFERRALS (documented, out of scope for v1):
  - Category & folder `name`/`description` translation (a SEPARATE translatable
    axis, freshdesk.md gotcha #5). v1 ships ARTICLE translation only; the folder
    tree is walked for discovery but its container strings are left in source.
  - `seo_data` (`meta_title`/`meta_description`/`meta_keywords`) — the exact key
    spelling is [UNCERTAIN] (freshdesk.md §1) so v1 does NOT write seo_data;
    writing an unverified key risks a 400 or a silently-wrong field. Deferred.
  - Nested sub-folder completeness edge cases (freshdesk.md gotcha #7) — the walk
    is a real bounded BFS over subfolder ids (cycle-safe via a seen-set), so
    sub-sub-folders and deeper are discovered; a subfolders 4xx on any one node
    is tolerated best-effort without abandoning the rest of the tree.
"""

from __future__ import annotations

import base64
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
from app.services.connectors.vendor_text import vendor_excerpt

API_VERSION = "v2"  # Solutions API version pin (unpinned payloads shift under you)

# status enum (freshdesk.md §4): 1 = draft, 2 = published.
STATUS_DRAFT = 1
STATUS_PUBLISHED = 2

# Folder/subfolder discovery cap: generous and INDEPENDENT of the per-run
# max_articles safety cap (reviewer finding — a KB with more folders than
# max_articles must not have its remaining folders permanently hidden just
# because article accumulation is capped low). Matches _paginate's own hard
# page bound (50 pages * up to 100/page).
FOLDER_LIST_CAP = 5000
HELD_BACK_PREFIX = "held-back:"
# Review gate 2026-10-10: a draft-mode run never writes into a translation that is LIVE
# (published). Freshdesk has no separate draft of a published translation in the API we use:
# a PUT with status 2 puts unreviewed machine text on the customer's site, and status 1
# unpublishes it. Both are wrong, so the pair is left untouched and reported (live-kept).
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the translation is PUBLISHED and this connection writes drafts, so the live text was "
    "left unchanged; turn on 'Publish after write' to update live translations, or "
    "unpublish it in Freshdesk and it is translated on the next run"
)

# Ids and language codes are interpolated into URL paths: plain id tokens (Freshdesk's are
# numeric; letters, digits, '-' and '_' are allowed so a fixture id never matters) and
# BCP-47-ish codes only. ASCII; ``..``, ``/``, ``%``, ``?``, ``#``, CR/LF, NUL and unicode
# digits can never match.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}", re.ASCII)
_LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", re.ASCII)


def _safe_id(value, what: str = "id") -> str:
    sid = str(value).strip()
    if not _ID_RE.fullmatch(sid):
        raise ConnectorError(f"Refused {what} {sid[:40]!r}: not a plain Freshdesk id.")
    return sid


def _safe_lang(value) -> str:
    code = str(value).strip()
    if not _LANG_RE.fullmatch(code):
        raise ConnectorError(f"Refused language code {code[:40]!r}.")
    return code


SPEC = ConnectorSpec(
    kind="freshdesk",
    name="Freshdesk Solutions",
    icon="\U0001f9be",  # mechanical arm (Freshworks-ish); any stable glyph
    cat="Knowledge base / help center",
    blurb="Translate Freshdesk Solutions (knowledge-base) articles into any language. "
    "Each translation is written back as the article's per-language version "
    "(GET/POST/PUT /api/v2/solutions/articles/{id}/{lang}), so the master "
    "article is never overwritten. Translations land as drafts by default.",
    config_fields=[
        FieldSpec(
            "domain",
            "Freshdesk domain",
            "Your Freshdesk domain — the 'acme' in acme.freshdesk.com (or paste the full host).",
            placeholder="acme",
        ),
        FieldSpec(
            "category_id",
            "Category id",
            "Limit discovery to one Solution category (optional; blank = walk all categories).",
            required=False,
            placeholder="80000012345",
        ),
        FieldSpec(
            "folder_id",
            "Folder id",
            "Limit discovery to one folder (optional; blank = all folders in scope).",
            required=False,
            placeholder="80000067890",
        ),
        FieldSpec(
            "source_lang",
            "Source lang override",
            "USUALLY BLANK — the portal's "
            "immutable primary language is auto-detected as the source. Set only to "
            "override the detected code.",
            required=False,
            placeholder="(auto)",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Freshdesk "
            "supported language codes to write (must be enabled + Pro/Enterprise).",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "publish",
            "Publish after write",
            "'1' = publish the translated "
            "version (status 2) after writing; '0' = leave it a draft (status 1, "
            "default, safe, mirrors the Zendesk/Document360 draft flag). In draft "
            "mode a translation that is already published is never overwritten: it "
            "is reported as kept live.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "max_articles",
            "Max articles",
            "Safety cap per run (full-tree poll walk — keep modest for large KBs).",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_key",
            "API key",
            "Freshdesk → Profile settings → Your API key. "
            "Sent as HTTP Basic username (password 'X'). Requests act AS that agent "
            "— use a dedicated KB-write agent.",
            secret=True,
            placeholder="your-api-key",
        ),
    ],
    setup_steps=[
        "In Freshdesk → Profile settings, copy Your API key (use a dedicated "
        "integration agent with knowledge-base write scope).",
        "Enter your domain (the 'acme' in acme.freshdesk.com) and paste the API key.",
        "Multilingual Solutions require a Pro or Enterprise plan — enable your target "
        "languages under Admin → Knowledge base → Languages first.",
        "The portal's PRIMARY language is the source and is read automatically (it is "
        "immutable) — leave Source lang blank unless you must override it.",
        "Freshdesk Solutions have NO webhooks — change detection is a poll-based "
        "full-tree walk, so run on a schedule (Every N minutes on the connection page) "
        "to keep translations fresh.",
    ],
    other_side="Freshdesk Solutions have no webhooks and no updated_since filter; "
    "change detection is a full-tree poll walk diffing each article's "
    "updated_at plus a per-article source content hash (the primary "
    "suppression).",
    docs_url="https://developers.freshdesk.com/api/",
    supports_oauth=False,  # API-key HTTP Basic is the supported server-to-server path
    archived=False,  # rebuilt 2026-10-10 (connector parity); live QA is pending (runbook)
)


def _is_published(article: dict) -> bool:
    """A master article is published when its `status` == 2 (freshdesk.md §4).
    A missing/unparseable status is treated as NOT published (conservative — the
    master-published precondition must be positively met, gotcha #3)."""
    try:
        return int(article.get("status")) == STATUS_PUBLISHED
    except (TypeError, ValueError):
        return False


class FreshdeskConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="freshdesk",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.HTML,  # article `description` is HTML -> html filter
        regions="subdomain",  # per-connection {domain}.freshdesk.com
        change_signals=ChangeSignals(
            webhook_sig=None,  # NO webhook for Solutions — poll only (§5)
            poll_field="updated_at",  # coarse gate; the content hash is primary
            stale_flag=None,  # no per-locale stale flag exposed for Solutions
            thin_payload=False,
        ),  # poll pulls the full article; no re-fetch dance
        anti_loop_label=False,  # no labels; the content-hash watermark is the guard
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # T2 strategy wired with Freshdesk's discovery hooks. The strategy owns the
        # 404-vs-200 create/update decision over the connector's get/create/update
        # translation methods below.
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: (
                (rec or {}).get("source_lang") or self.primary_language()
            )
        )

    # --- auth / base url ---
    def _base(self) -> str:
        from app.services.connectors.tenant_host import tenant_host

        base = "https://" + tenant_host("freshdesk", self.config)
        _validate_target(base)  # SSRF guard (base._validate_target) on every request path
        return base

    def _auth_kwargs(self) -> dict:
        """Per-request auth: HTTP Basic with the API key as username and the literal
        'X' as password (freshdesk.md §6): Authorization: Basic base64('<api_key>:X')."""
        key = (self.secret.get("api_key") or "").strip()
        if not key:
            raise ConnectorError("Freshdesk API key is required.")
        raw = f"{key}:X".encode()
        return {"headers": {"Authorization": "Basic " + base64.b64encode(raw).decode()}}

    def _get(self, c, path, **kw):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        return self._request(c, "GET", self._base() + path, headers=headers, **akw, **kw)

    def _write_kwargs(self) -> dict:
        akw = self._auth_kwargs()
        akw["headers"] = {"Content-Type": "application/json", **akw.pop("headers", {})}
        return akw

    def _sol(self, path: str) -> str:
        return f"/api/{API_VERSION}/solutions{path}"

    def _wire_lang(self, locale) -> str:
        """The vendor's own spelling of a language code. The flow lowercases locales, but
        Freshdesk lists some as 'ru-RU' / 'pt-BR'; the path must carry the listed form.
        Unknown codes keep the caller's spelling. Always validated before it reaches a URL."""
        lc = str(locale).strip().lower()
        if getattr(self, "_lang_map", None) is None:
            self._lang_map = {}
            langs = self._helpdesk_settings().get("supported_languages")
            for lang in langs if isinstance(langs, list) else []:
                code = str((lang.get("code") if isinstance(lang, dict) else lang) or "").strip()
                if code:
                    self._lang_map[code.lower()] = code
        return _safe_lang(self._lang_map.get(lc, str(locale).strip()))

    def _settings(self, path: str = "") -> str:
        return f"/api/{API_VERSION}/settings{path}"

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                # /settings/helpdesk carries primary_language + supported_languages;
                # it also proves auth. A list-categories fallback covers tenants that
                # 404 the settings route.
                r = self._get(c, self._settings("/helpdesk"))
                if r.status_code == 200:
                    body = r.json() or {}
                    prim = body.get("primary_language") or "(unknown)"
                    langs = body.get("supported_languages") or []
                    return True, (
                        f"Connected to Freshdesk (primary language {prim}; "
                        f"{len(langs)} supported language(s))."
                    )
                if r.status_code in (401, 403):
                    return False, f"Freshdesk rejected the API key ({r.status_code})."
                # settings route missing on this tenant -> prove auth via categories.
                r2 = self._get(c, self._sol("/categories"))
                if r2.status_code == 200:
                    cats = r2.json() or []
                    n = len(cats) if isinstance(cats, list) else 0
                    return True, f"Connected to Freshdesk ({n} solution categor(ies))."
                if r2.status_code in (401, 403):
                    return False, f"Freshdesk rejected the API key ({r2.status_code})."
                return False, f"Freshdesk returned {r2.status_code}: {vendor_excerpt(r2.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: Solution categories (id + name). Best-effort."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, self._sol("/categories"))
            if r.status_code != 200:
                return []
            for cat in r.json() or []:
                cid = str(cat.get("id") or "")
                name = cat.get("name") or cid
                out.append({"id": cid, "name": f"{name} (category {cid})", "tables": []})
        except Exception:
            return out
        return out

    # --- discover: portal primary language + supported languages ---
    def _helpdesk_settings(self) -> dict:
        """GET /api/v2/settings/helpdesk -> the portal's language settings, or {} if
        the route is unavailable. Carries `primary_language` (the immutable master /
        source) and `supported_languages` (the enabled secondary set)."""
        with self._client() as c:
            r = self._get(c, self._settings("/helpdesk"))
        if r.status_code == 200:
            body = r.json()
            return body if isinstance(body, dict) else {}
        # 401/403 are genuine auth failures worth surfacing; other codes (esp. 404 on
        # a tenant without the settings route) fall back to {} so primary_language /
        # enabled_locales can use their configured/guarded defaults instead of hard-
        # blocking. [UNCERTAIN] exact settings route shape -> guarded, not assumed.
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"Freshdesk rejected the API key reading portal settings ({r.status_code})."
            )
        return {}

    def primary_language(self) -> str:
        """The portal's PRIMARY (master) language — the immutable source (constraint
        #2). Read from the API's `primary_language`; NEVER assumed to be 'en'. A
        config `source_lang` override wins (for a tenant whose settings route is
        unavailable or misreported). Lowercased; falls back to 'en' only as a last
        resort when neither the API nor config yields one (guarded, documented)."""
        override = (self.config.get("source_lang") or "").strip().lower()
        if override:
            return override
        prim = (self._helpdesk_settings().get("primary_language") or "").strip().lower()
        return prim or "en"

    def enabled_locales(self) -> list[str]:
        """The portal's SUPPORTED (secondary) language codes — a target must be in
        this set or the write is dropped. Read from the API's `supported_languages`
        (freshdesk.md §1). Lowercased for comparison; the connector's write-back uses
        the config's own casing.

        Guarded fallback: if the settings route yields no supported set (unavailable
        route on some tenants — [UNCERTAIN]), fall back to the configured target
        locales + the primary so a misrouted-but-otherwise-fine tenant is not hard-
        blocked. The plan-gate 4xx on an actual translation write is the real,
        clean failure surface (constraint #3), not this discovery guess."""
        s = self._helpdesk_settings()
        langs = s.get("supported_languages")
        out: list[str] = []
        if isinstance(langs, list):
            for lang in langs:
                code = (lang.get("code") if isinstance(lang, dict) else lang) or ""
                code = str(code).strip().lower()
                if code:
                    out.append(code)
        if out:
            # include the primary too (some tenants list it under supported)
            return sorted(set(out))
        cfg_targets = [
            x.strip().lower()
            for x in (self.config.get("target_locales") or "").replace(";", ",").split(",")
            if x.strip()
        ]
        return sorted(set(cfg_targets + [self.primary_language()])) or [self.primary_language()]  # noqa: RUF005

    # --- discover: the full-tree article walk (poll-only) ---
    def _paginate(self, c, path: str, cap: int) -> list[dict]:
        """Page through a Freshdesk Solutions list (?page=n 1-based, ?per_page<=100;
        default 30). Stop when a page returns fewer than per_page (freshdesk.md §2).
        A non-200 after the first page is treated as end-of-list ONLY when we already
        have results; a non-200 on page 1 raises (a genuine bad-path/auth failure,
        never silently 'zero results')."""
        out: list[dict] = []
        per_page = min(100, max(1, cap))
        page = 1
        for _ in range(50):  # hard bound on pages per run
            r = self._get(c, path, params={"page": page, "per_page": per_page})
            if r.status_code != 200:
                if page > 1 and out and 400 <= r.status_code < 500:
                    break
                raise ConnectorError(
                    f"Freshdesk list {path} failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            data = r.json() or []
            if not isinstance(data, list):
                data = (
                    data.get("articles") or data.get("items") or []
                    if isinstance(data, dict)
                    else []
                )
            out.extend(self._keep_rows(data))
            if len(out) >= cap or len(data) < per_page:
                break
            page += 1
        return out[:cap]

    def _folder_ids_in_category(self, c, cat_id: str) -> list[str]:
        """Folder ids under a category, walked via a real bounded BFS over nested
        subfolders (sub-sub-folders and deeper — June-2022 nesting is common in
        real KBs, freshdesk.md gotcha #7). Deep-nesting quirks are documented, not
        engineered against — a subfolders 4xx on any node is tolerated best-effort
        (some tenants/plans lack the route) without abandoning the rest of the walk.

        Folder discovery is capped by FOLDER_LIST_CAP, NOT the caller's `cap`
        (max_articles) — a KB with more folders than max_articles must still have
        every folder discovered; max_articles only bounds ARTICLE accumulation
        downstream (reviewer finding). The BFS is cycle-safe via a seen-set (a
        folder is only ever queued/visited once, however it's reached) and bounded
        by the same page hard-bound _paginate already enforces per list call, plus
        an overall node cap so a pathological/cyclic tree can't run away."""
        ids: list[str] = []
        seen: set[str] = set()
        queue: list[str] = []
        cat_id = _safe_id(cat_id, "category id")
        for f in self._paginate(c, self._sol(f"/categories/{cat_id}/folders"), FOLDER_LIST_CAP):
            fid = str(f.get("id") or "")
            if fid and fid not in seen:
                seen.add(fid)
                ids.append(fid)
                queue.append(fid)
        # BFS over subfolders: process the queue FIFO, tolerate a 4xx per-node.
        while queue and len(seen) < FOLDER_LIST_CAP:
            fid = _safe_id(queue.pop(0), "folder id")
            try:
                subs = self._paginate(c, self._sol(f"/folders/{fid}/subfolders"), FOLDER_LIST_CAP)
            except ConnectorError:
                continue
            for sf in subs:
                sfid = str(sf.get("id") or "")
                if sfid and sfid not in seen:
                    seen.add(sfid)
                    ids.append(sfid)
                    queue.append(sfid)
        return ids

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """List translatable MASTER (primary-language) articles via a full-tree walk.

        Precedence mirrors the sibling connectors: explicit article_ids (retry path)
        win; else folder_id scope; else category_id scope; else every category's
        folder tree. Each returned article is the MASTER
        (GET /solutions/articles/{id}) — the source-language content."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        cfg = self.config or {}
        with self._client() as c:
            if article_ids:
                out = []
                for aid in list(article_ids)[:cap]:
                    art = self._fetch_article(c, _safe_id(aid, "article id"))
                    # A caller-supplied id must stay inside the configured folder/category
                    # (fail closed: an article that does not name its place is out of scope).
                    if art is not None and not self._in_scope(art):
                        art = None
                    if art is not None:
                        out.append(self._charge_record(art))
                return out
            folder_ids: list[str] = []
            if (cfg.get("folder_id") or "").strip():
                folder_ids = [_safe_id(cfg["folder_id"], "folder id")]
            elif (cfg.get("category_id") or "").strip():
                folder_ids = self._folder_ids_in_category(c, cfg["category_id"].strip())
            else:
                for cat in self._get(c, self._sol("/categories")).json() or []:
                    catid = str(cat.get("id") or "")
                    if catid:
                        folder_ids.extend(self._folder_ids_in_category(c, catid))
            # List every folder's article ids first, then take them ROUND-ROBIN up to the cap
            # so the first folder cannot consume the whole max_articles budget run after run
            # (taxonomy E). Each chosen master is re-fetched by id for the full shape.
            per_folder: list[list[str]] = []
            for fid in folder_ids:
                ids = [
                    str(row.get("id") or "")
                    for row in self._paginate(c, self._sol(f"/folders/{fid}/articles"), cap)
                ]
                per_folder.append([i for i in ids if i])
            seen: set[str] = set()
            chosen: list[str] = []
            depth = 0
            while len(chosen) < cap and any(depth < len(ids) for ids in per_folder):
                for ids in per_folder:
                    if depth < len(ids) and ids[depth] not in seen and len(chosen) < cap:
                        seen.add(ids[depth])
                        chosen.append(ids[depth])
                depth += 1
            out = []
            for aid in chosen:
                art = self._fetch_article(c, _safe_id(aid, "article id"))
                if art is not None:
                    out.append(self._charge_record(art))
        return out[:cap]

    def _in_scope(self, art: dict) -> bool:
        """Is this article inside the configured folder / category? Unconfigured = anything
        the key can read. A configured scope the article does not positively match fails."""
        cfg = self.config or {}
        for key, field in (("folder_id", "folder_id"), ("category_id", "category_id")):
            want = (cfg.get(key) or "").strip()
            if want and str(art.get(field) or "") != want:
                return False
        return True

    def _fetch_article(self, c, article_id: str) -> dict | None:
        """GET /solutions/articles/{id} -> the master article dict, or None on 404."""
        r = self._get(c, self._sol(f"/articles/{_safe_id(article_id, 'article id')}"))
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
    def extract_units(self, article: dict, get_filter) -> tuple[list[dict], str]:
        """Article title (plain) + body `description` (HTML -> the shared html filter).
        Returns (units, content_hash). Units are p_index-keyed: 0 = title, 1+ = body
        text nodes. content_hash is the per-source-content poll-suppression hash.

        seo_data is NOT extracted in v1 ([UNCERTAIN] key spelling — deferred)."""
        title = article.get("title") or ""
        body = article.get("description") or ""
        body_units = get_filter("html").parse(body.encode("utf-8")).units
        units = [{"p_index": 0, "text": title, "field": "title"}]
        for i, u in enumerate(body_units):
            units.append({"p_index": 1 + i, "text": u.text, "field": "body"})
        return units, _content_hash(title, body)

    # --- write-back client (the SubResourceClient the strategy drives) ---
    def get_translation(self, record_id: str, locale: str):
        """GET /solutions/articles/{id}/{lang} -> the translation dict, or
        strategy.ABSENT on 404 (translation absent → needs creation, §2). This is the
        EXPLICIT existence probe the create-vs-update decision needs — NEVER a content
        read (guards a silent master-language fallback)."""
        path = self._sol(f"/articles/{_safe_id(record_id, 'article id')}/{self._wire_lang(locale)}")
        with self._client() as c:
            r = self._get(c, path)
        if r.status_code == 200:
            body = r.json()
            return body if isinstance(body, dict) and body else ABSENT
        if r.status_code == 404:
            return ABSENT
        raise ConnectorError(
            f"Freshdesk translation probe {record_id}/{locale} -> {r.status_code}: "
            f"{vendor_excerpt(r.text)}"
        )

    def is_live_published(self, record_id: str, locale: str) -> bool:
        """Is the stored (record, locale) translation PUBLISHED? One GET, no content read."""
        existing = self.get_translation(record_id, locale)
        return isinstance(existing, dict) and _is_published(existing)

    @staticmethod
    def _held_result(created: bool) -> WriteResult:
        return WriteResult(
            False,
            created,
            message=f"{HELD_BACK_PREFIX} the title or body of this pair is not translated yet "
            "(NEEDS_REVIEW); nothing was written and it will be retried next run",
        )

    @staticmethod
    def _status_for(fields: dict, existing) -> int:
        """Publish flag on -> published. Draft mode: a NEW translation is a draft, an
        EXISTING one keeps its status (an update must never unpublish a live translation;
        a published one never gets here in draft mode, update_translation refuses it)."""
        if not fields.get("draft", True):
            return STATUS_PUBLISHED
        if isinstance(existing, dict):
            try:
                return int(existing.get("status"))
            except (TypeError, ValueError):
                return STATUS_DRAFT
        return STATUS_DRAFT

    def _write_payload(self, fields: dict, existing=None) -> dict | None:
        """Build the POST/PUT body for a translation write. `fields` carries the already-
        translated {title, body} where a value of None means HELD BACK (NEEDS_REVIEW). A held
        field takes the STORED translation's value (never source text); with nothing stored to
        keep there is no payload (None). seo_data is intentionally omitted (v1)."""
        title, body = fields.get("title"), fields.get("body")
        if isinstance(existing, dict):
            if title is None:
                title = existing.get("title")
            if body is None:
                body = existing.get("description")
        if title is None or body is None:
            return None
        return {"title": title, "description": body, "status": self._status_for(fields, existing)}

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """POST a new translation at /solutions/articles/{id}/{lang}. Non-idempotent
        create -> retry only on 429 (base._request idempotent=False). A 4xx here on a
        plan-gated tenant (multilingual not available) surfaces as (False, msg), not a
        crash (constraint #3). A pair with a held field is never created (#640)."""
        payload = self._write_payload(fields)
        if payload is None:
            return self._held_result(True)
        path = self._sol(f"/articles/{_safe_id(record_id, 'article id')}/{self._wire_lang(locale)}")
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "POST",
                    self._base() + path,
                    json=payload,
                    idempotent=False,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            tid = ""
            try:
                tid = str((r.json() or {}).get("id") or record_id)
            except Exception:
                tid = str(record_id)
            return WriteResult(True, True, target_id=tid, message="created")
        return WriteResult(False, True, message=self._http_error(r))

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """PUT an existing translation (idempotent per (article, lang) — retries on
        5xx). Held fields and the status are merged from the stored translation, so the
        write is correct whether the PUT is a partial or a full replace."""
        if fields.get("title") is None and fields.get("body") is None:
            return self._held_result(False)  # nothing translated: do not rewrite the old text
        existing = self.get_translation(record_id, locale)
        if fields.get("draft", True) and isinstance(existing, dict) and _is_published(existing):
            # Draft mode never writes into a LIVE translation (see LIVE_KEPT_PREFIX).
            return WriteResult(False, False, message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}")
        payload = self._write_payload(fields, existing)
        if payload is None:
            return self._held_result(False)
        path = self._sol(f"/articles/{_safe_id(record_id, 'article id')}/{self._wire_lang(locale)}")
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._base() + path,
                    json=payload,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return WriteResult(False, False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            tid = ""
            try:
                tid = str((r.json() or {}).get("id") or record_id)
            except Exception:
                tid = str(record_id)
            return WriteResult(True, False, target_id=tid, message="updated")
        return WriteResult(False, False, message=self._http_error(r))

    @staticmethod
    def _http_error(r) -> str:
        return f"{r.status_code}: {vendor_excerpt(r.text)}"

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, field-unit) for the generic pull view. The
        rich flow is store.freshdesk_translate (writes per-language versions)."""
        from app.filters import get_filter

        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _h = self.extract_units(art, get_filter)
            out.extend(self._pulled_items(units, str(art.get("id")), art.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Freshdesk writes per-language article versions — use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list master articles and return them with a
        per-article content key the caller diffs against the persisted watermark hash.
        Returns (articles, 0) — Freshdesk Solutions have no monotonic export cursor
        and no updated_since filter (§5); the per-article content hash in
        store.freshdesk_translate is the actual change gate."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow moved here from store.py (Wave 3 store.py decomposition).
# The per-connector hook bundle + freshdesk_translate wrapper live with the
# connector; store.py keeps a thin delegator so store.freshdesk_translate stays
# importable/monkeypatchable (jobs/CLI/webhook dispatch + verify suites).
# ===========================================================================
import logging

from app.services.connectors import flow

log = logging.getLogger("loc_tms.connectors")


# --------------------------- Freshdesk hooks -------------------------------
def _fd_preflight(ctx):
    primary = ctx.connector.primary_language()
    enabled = set(ctx.connector.enabled_locales())
    return enabled, {"primary": primary}


def _fd_partition(locales, enabled, extra):
    primary = extra["primary"]
    # Drop the immutable primary language (translating INTO the source is nonsensical,
    # constraint #2) and any target not in the portal's supported set.
    rejected = [l for l in locales if l == primary or l not in enabled]  # noqa: E741
    usable = [l for l in locales if l != primary and l in enabled]  # noqa: E741
    return rejected, usable


def _fd_prepare(ctx, art):
    from app.filters import get_filter
    from app.services.connectors.freshdesk import _is_published

    # The live-kept park hook (_fd_parked) needs this run's draft flag and stored hashes.
    ctx.connector._run_draft = ctx.draft
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id"))
    raw_title = art.get("title") or ""
    raw_body = art.get("description") or ""
    # MASTER-PUBLISHED precondition (constraint #4): a translation of a draft master
    # orphans, so skip an unpublished master with a clear error.
    if not _is_published(art):
        return flow.SkipArticle(
            "unpublished_skipped",
            f"{aid}: master not published (status != 2) — publish the "
            f"master article before translating.",
        )
    if not raw_body.strip() and not raw_title.strip():
        return flow.SkipArticle("empty_skipped")
    _units, chash = ctx.connector.extract_units(art, get_filter)
    body_units = get_filter("html").parse(raw_body.encode("utf-8")).units
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=raw_title,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": raw_title},
        doc_title=f"{raw_title[:50] or 'article'} ({aid})",
        extra={"body_bytes": raw_body.encode("utf-8"), "raw_title": raw_title},
    )


def _fd_is_suppressed(connector, aid, loc):
    from app.services.connectors.strategy import ABSENT

    # No per-locale stale flag for Solutions: existence + hash IS the gate.
    return connector.get_translation(aid, loc) is not ABSENT


def _fd_build_payload(prep, loc, seg0_target, mapping):
    from app.filters import get_filter

    # None = HELD BACK. Never the source text as a stand-in: an update would overwrite a
    # stored good translation with the source language (taxonomy B); the connector keeps the
    # stored value, and refuses to create a pair that has a held field (#640).
    raw_title = prep.extra["raw_title"]
    if seg0_target is not None:
        t_title = seg0_target
    elif not raw_title.strip():
        t_title = ""  # a source with no title has nothing to hold back
    else:
        t_title = None
    body_held = any(
        str(u.p_index) not in mapping and (u.text or "").strip() for u in prep.body_units
    )
    t_body = (
        None
        if body_held
        else get_filter("html").build(prep.extra["body_bytes"], mapping).decode("utf-8")
    )
    return {"title": t_title, "body": t_body}


def _fd_parked(connector, aid, loc, chash):
    """Flow hook (review gate 2026-10-10): in draft mode a pair whose translation is already
    PUBLISHED is skipped BEFORE any MT is spent and reported as kept live (never written).
    Only pairs that would otherwise be translated are probed (no stored hash for this
    source); the probe runs every such run, so unpublishing in Freshdesk or turning on
    'Publish after write' clears it with no stored state. update_translation repeats the
    check at write time (an explicit retry bypasses this hook)."""
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


def _is_held_back(f):
    return str((f or {}).get("error") or "").startswith(HELD_BACK_PREFIX)


def _is_live_kept(f):
    return str((f or {}).get("error") or "").startswith(LIVE_KEPT_PREFIX)


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _live_kept(ctx):
    """Pairs left untouched because their translation is live and the run is in draft mode:
    the ones the park hook skipped before MT plus any refused at write time."""
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept(f)
    ]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f) and not _is_live_kept(f)]


def _fd_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    real = _real_failures(ctx)
    if real or ctx.counters["unpublished_skipped"]:
        # A plan-gate 4xx (constraint #3) or unpublished master (constraint #4) means
        # the run did real work incompletely — green only if SOMETHING landed AND there
        # were no hard failures. Held-back pairs (#640) are reported, not failures.
        return (
            (ctx.created + ctx.updated) > 0 and not real and not ctx.counters["unpublished_skipped"]
        )
    return bool(
        (ctx.created + ctx.updated) > 0
        or ctx.skipped > 0
        or ((ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _fd_build_message(ctx):
    primary = ctx.extra.get("primary")
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} article version(s) across "
        f"{len(ctx.usable)} language(s) from {len(ctx.articles)} article(s) "
        f"(primary/source: {primary})."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["unpublished_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['unpublished_skipped']} unpublished master "
            f"article(s) (publish the master first)."
        )
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} empty article(s) (no translatable content)."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) not supported / equal to the "
            f"primary: {', '.join(ctx.rejected)}."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (article, language) pair(s) awaiting review (NEEDS_REVIEW): "
            "the held field kept its stored translation (or the language version was not "
            "created) and the pair will be re-attempted next run."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) translation(s) unchanged: {sample}. This "
            "connection writes drafts, so new machine text is never written into a published "
            "translation. Turn on 'Publish after write' to update live translations, or "
            "unpublish them in Freshdesk and they are translated on the next run."
        )
    real_errors = [e for e in ctx.errors if HELD_BACK_PREFIX not in e and LIVE_KEPT_PREFIX not in e]
    if real_errors:
        parts.append("Errors: " + "; ".join(real_errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())  # Cluster A: loud, per-run
        log.warning(
            "freshdesk_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _fd_build_result(ctx):
    return (
        {
            "articles": len(ctx.articles),
            "locales": ctx.usable,
            "created": ctx.created,
            "updated": ctx.updated,
            "skipped": ctx.skipped,
            "empty_skipped": ctx.counters["empty_skipped"],
            "unpublished_skipped": ctx.counters["unpublished_skipped"],
            "primary": ctx.extra.get("primary"),
            "rejected": ctx.rejected,
            "failures": ctx.failures,
        }
        | ({"held_back": len(_held_back(ctx))} if _held_back(ctx) else {})
        | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})
    )


def freshdesk_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Freshdesk Solutions articles into one or more language codes, each
    written back as the article's per-language VERSION via the T2 SubResourceStrategy.

    Thin wrapper over flow.run_translate_flow. Freshdesk-specific behavior via hooks:
    IMMUTABLE PRIMARY LANGUAGE read from the API + dropped as a target, supported-
    language rejection (all-rejected = FAIL), MASTER-MUST-BE-PUBLISHED skip, plan-gate
    4xx surfaced as a per-pair failure, and poll-only content-hash suppression (no
    stale flag -> existence + hash is the gate). NO watermark. The egress guard is
    checked against the RAW effective locales (egress_raw_effective). only_pairs
    restricts the write loop to those (article_id, lang) pairs.
    """
    spec = flow.TranslateFlowSpec(
        kind="freshdesk",
        no_conn_msg="No such Freshdesk connection.",
        hash_key="fd_hashes",
        probe_label="Freshdesk",
        preflight=_fd_preflight,
        prepare=_fd_prepare,
        is_suppressed=_fd_is_suppressed,
        build_payload=_fd_build_payload,
        compute_draft=lambda cfg: str(cfg.get("publish", "0")).strip() not in ("1", "true", "yes"),
        compute_ok=_fd_compute_ok,
        build_message=_fd_build_message,
        build_result=_fd_build_result,
        extra_counters=("empty_skipped", "unpublished_skipped", "parked_skipped"),
        partition=_fd_partition,
        egress_raw_effective=True,
        held_prefix=HELD_BACK_PREFIX,
        parked=_fd_parked,
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
