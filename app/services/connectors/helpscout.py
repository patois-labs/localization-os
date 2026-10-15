"""Help Scout Docs connector (T3: a separate article per language, tracked in an id map).

REBUILT 2026-10-10 (connector parity wave 3). Re-verified against the Docs API v1 reference
(developer.helpscout.com/docs-api, read 2026-10-10): HTTP Basic with the Docs API key as the
user name, base `https://docsapi.helpscout.net/v1`, 50 records per page (`pageSize` up to 100),
rate limit 2000 requests per 10 minutes per site (429 handled by the shared retry), list by `GET
/collections/{id}/articles`, create `POST /articles` (201 + Location), update `PUT
/articles/{id}` (200), `status` is `published` or `notpublished`.

TOPOLOGY. Help Scout Docs has NO native multi-language model: no locale field on a site,
collection or article, and the reference describes none (third-party tools such as Transifex
Live only overlay one site with JavaScript). The real-world workaround is one Docs SITE or one
COLLECTION per language. This connector supports both with one convention, a
`collection_map` {source collection id: {locale: target collection id}}: collection ids are
global across the account, so a target collection may sit in the same site (collection per
language) or in another Docs site (site per language, the way a translated public help center is
usually run). A (collection, locale) with no mapping is refused, never guessed. The connector
owns the source-to-target article id map (strategy.DocPerLocaleStrategy, reserve then fill, the
7-case reconciliation), so the source article and other languages are never overwritten and a
pre-existing article in a target collection is never touched (needs_link, adopt or ignore).

HELD UNITS (#640). A held (NEEDS_REVIEW) title or body unit is OMITTED, never replaced with
source text: no target article is created while any unit is held (it would be a mixed-language
article and the hash would hide it), an update re-sends the held part with its STORED value, a
partly written pair records no content hash and is retried after approval.

DRAFT MODE (`publish` off, the default). A target article that is already PUBLISHED is never
changed: it is skipped before machine translation is spent and reported as `live_kept`. New
targets are created `notpublished`. With `publish` on, a target is published only when its
SOURCE article is published (an unpublished source never produces a live translation). Note from
the API reference: changing `text` on a published article, or its status to published, deletes
an existing draft of that article.

related[] cross-links are remapped to the target-language article ids through the id map (an
unmapped id stays pointing at the source article, never an invented one). Change detection is
poll-only (Docs has no webhooks); updatedAt is metadata-tainted, so the per-(article, language)
content hash is the only gate. The Docs API key is distinct from the Inbox API OAuth
credentials. No bb-io code was read (no entry in THIRD_PARTY_NOTICES).
"""

from __future__ import annotations

import base64
import hashlib
import re

import httpx

from app.services.connectors import idmap
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FetchBudgetError,
    FieldSpec,
    PulledItem,
    _validate_target,
    fetch_cap,
    harvest_context_field,
    item_chars,
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

# Single global host + version (support_extra.md §2.2/§2.6 — CONFIRMED). No DC/region
# domain quirk (unlike Zoho), so the base is fixed.
API_BASE = "https://docsapi.helpscout.net/v1"
# HTTP Basic dummy password (support_extra.md §2.6: key in the USERNAME field, any
# non-empty password). "X" is the documented convention.
_DUMMY_PASSWORD = "X"  # noqa: S105 -- Help Scout basic-auth placeholder; key is the user  # pregate: allow-secret not a credential

# A Docs id (article, collection) is a short ASCII token. Anything with a dot, slash, percent,
# query or whitespace is refused before it can reach a URL path (defect taxonomy A).
_DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_HELD_BACK_PREFIX = "held-back:"
LIVE_KEPT_PREFIX = "live-kept:"
LIVE_KEPT_REASON = (
    "the target article is PUBLISHED in Help Scout Docs and this connection writes drafts, so the "
    "live text was left unchanged; set Publish translations = 1 to update published articles"
)


def _hs_id(value, what: str = "id") -> str:
    text = str(value if value is not None else "").strip()
    if not _DOC_ID.fullmatch(text) or not text.isascii():
        raise ConnectorError(f"Invalid Help Scout {what}: letters, digits, '-' and '_' only.")
    return text


def _is_published(art) -> bool:
    """Fail closed: an article whose status is not explicitly notpublished counts as live."""
    return isinstance(art, dict) and str(art.get("status") or "").strip().lower() != "notpublished"


def content_hash(*parts) -> str:
    """Stable per-article source-content hash for the poll-suppression gate. Covers
    the title, the raw HTML body, the slug, and the related[] id list (a change to
    ANY of those must re-run: related/slug affect the WRITTEN links, not just prose).
    Order-sensitive; SHA-256 hex (truncated)."""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (list, tuple)):
            p = "\x1e".join(str(x) for x in p)
        h.update((str(p) if p is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def validate_collection_map(cmap: dict) -> None:
    """Reject a structurally-invalid collection_map PARTITION loudly (MED-3), naming the
    offending ids (ASCII-safe). Two shapes corrupt the T3 lifecycle:
      (a) two DISTINCT locales share ONE target collection id — their articles land in
          the same collection and rule-6's slug/name probe matches the FIRST locale's
          article while building the SECOND, a false cross-locale needs_link collision.
      (b) a target collection id equals a SOURCE collection key — that target would be
          re-discovered as a source and its translations translated again (a loop).
    A target reused by the SAME locale from multiple source collections is allowed (one
    language, not a partition break). Enforced at BOTH the store create/update seam
    (web + CLI + api inherit it) and defensively at run start for pre-existing configs."""
    if not isinstance(cmap, dict):
        return
    source_keys = {_hs_id(k, "source collection id") for k in cmap}
    seen_target: dict[str, str] = {}  # target collection id -> the locale that claimed it
    for src, per in cmap.items():
        if not isinstance(per, dict):
            continue
        for locale, target in per.items():
            tgt = str(target).strip() if target else ""
            if not tgt:
                continue
            _hs_id(tgt, "target collection id")
            loc = str(locale).strip().lower()
            if tgt in source_keys:
                raise ConnectorError(
                    f"Help Scout collection_map is invalid: target collection {tgt!r} "
                    f"(locale {loc!r} of source collection {str(src)!r}) is also a SOURCE "
                    f"collection key. A target must never be a source, or its "
                    f"translations get re-discovered and re-translated."
                )
            prior_loc = seen_target.get(tgt)
            if prior_loc is not None and prior_loc != loc:
                raise ConnectorError(
                    f"Help Scout collection_map is invalid: target collection {tgt!r} is "
                    f"mapped by two locales ({prior_loc!r} and {loc!r}). Each locale needs "
                    f"its OWN target collection, or their articles collide."
                )
            seen_target[tgt] = loc


def validate_config(cfg: dict) -> None:
    """Validate a Help Scout config's collection_map at the store create/update seam and
    at run start (MED-3). Tolerates a dict or a JSON string (the web form stores config
    as strings). A blank or unparseable-as-JSON map is left to the runtime
    _collection_map() error (this guard only rejects an invalid PARTITION, not a missing
    or malformed map)."""
    raw = (cfg or {}).get("collection_map")
    if raw is None or raw == "":
        return
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception:
            return
    validate_collection_map(raw)


SPEC = ConnectorSpec(
    kind="helpscout",
    name="Help Scout Docs",
    icon="\U0001f4d8",  # blue book
    cat="Knowledge base / help center",
    blurb="Translate Help Scout Docs articles into any language. Help Scout has no "
    "native localization, so each translation is created as a separate article "
    "in a target-language COLLECTION you map (a collection per language, in the same "
    "Docs site or in a Docs site per language) and "
    "tracked in an internal id map, so the source article and other locales are "
    "never overwritten. Cross-links (related articles) are remapped to their "
    "translated counterparts.",
    config_fields=[
        FieldSpec(
            "collection_map",
            "Collection map (JSON)",
            "Collection-per-language map (Help Scout has no locale field). JSON "
            '{"<source collection id>": {"<locale>": "<target collection id>"}}. '
            "Keys are the discovery scope; an unmapped (collection, locale) is "
            "rejected, never guessed. A target collection may live in another Docs site.",
            required=True,
            placeholder='{"5f0a...": {"es": "6b1c...", "de-DE": "6b1d..."}}',
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the source articles "
            "(BCP-47, e.g. en). Never written as a target. Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locales to "
            "create (e.g. de-DE, fr, es). Each must appear in the collection "
            "map for the source collections you translate.",
            required=False,
            placeholder="de-DE, fr, es",
        ),
        FieldSpec(
            "publish",
            "Publish translations",
            "1 = publish target articles (only when the source article is published); "
            "0 (default) = create as drafts (notpublished) for review. A target that is "
            "already published is left unchanged and reported while this is 0.",
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
            "api_key",
            "Docs API key",
            "Your Help Scout Docs API key (Docs "
            "settings -> API), sent as HTTP Basic username. NOTE: distinct from "
            "the Mailbox/Inbox OAuth2 credentials.",
            secret=True,
            placeholder="your-docs-api-key",
        ),
    ],
    setup_steps=[
        "In Help Scout, open Docs -> Settings -> API and copy your Docs API key "
        "(distinct from the Mailbox/Inbox API).",
        "Create one target COLLECTION per language (e.g. 'Getting Started (es)') in "
        "the same Docs site, or a Docs site per language with the same collections in "
        "it. Help Scout has no locale field, so a collection (or site) is how you "
        "separate languages. Collection ids are in each collection's URL.",
        "Paste the API key above and fill the Collection map: for each source "
        "collection id, map each target locale to its target collection id.",
        "Set your Source locale (never translated) and Target locales.",
        "Help Scout Docs has no webhooks, so run on a schedule (Every N minutes) for "
        "change detection — the source-content hash keeps translations fresh.",
    ],
    other_side="Help Scout Docs change detection polls the mapped source collections "
    "and gates re-translation on a per-(article,locale) source-content hash "
    "(updatedAt bumps on any edit, so it never drives a watermark). Each "
    "translation is a separate article in the mapped target-language "
    "collection, tracked in the connector's id map; a pre-existing article "
    "in that collection is never overwritten (needs_link). related[] "
    "cross-links are remapped to their translated counterparts.",
    docs_url="https://developer.helpscout.com/docs-api/",
    supports_oauth=False,  # Docs API is Basic-key only; the OAuth2 API is a different product.
    archived=False,  # un-archived 2026-10-10 (connector parity rebuild); live QA is pending  # noqa: E501
)


class HelpScoutConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="helpscout",
        topology=Topology.T3,  # SECOND T3 consumer (document-per-locale)
        auth=(AuthKind.API_KEY,),
        api_version="v1",
        rich_text=RichText.HTML,  # body is HTML -> the existing html filter (no walker)
        regions="fixed",  # single global host docsapi.helpscout.net
        change_signals=ChangeSignals(
            webhook_sig=None,  # NO Docs webhooks -> poll-only, no verifier
            # CARDINAL RULE: updatedAt/lastPublishedAt bump on ANY edit (not content-
            # only), so they are NOT advanced as a watermark — the content hash is the
            # real gate (see the module docstring).
            poll_field="updatedAt",
            stale_flag=None,
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # T3 strategy wired with Help Scout's shapes — IDENTICAL wiring to Notion's,
        # because the reserve-then-fill + 7-case reconciliation lifecycle is entirely
        # in the vendor-neutral DocPerLocaleStrategy + idmap. This connector supplies
        # the four T3DocClient methods (below) + connection_id (set by build_fresh).
        self.strategy = DocPerLocaleStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )

    # --- config helpers ---
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en").strip().lower() or "en"

    def _publish(self) -> bool:
        return str(self.config.get("publish", "0")).strip().lower() in ("1", "true", "yes")

    def _collection_map(self) -> dict:
        """The {source_collection: {locale: target_collection}} mapping. Accepts a
        dict (tests / programmatic) OR a JSON string (the web form stores config as
        strings). A malformed value raises ConnectorError loudly rather than silently
        translating into a guessed collection."""
        raw = self.config.get("collection_map")
        if raw is None or raw == "":
            raise ConnectorError(
                "Help Scout collection_map is required — Help Scout has no locale "
                "field, so you must map each source collection + locale to a target "
                "collection."
            )
        if isinstance(raw, str):
            import json

            try:
                raw = json.loads(raw)
            except Exception as e:
                raise ConnectorError(f"Help Scout collection_map is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise ConnectorError(
                "Help Scout collection_map must be a JSON object "
                "{source_collection: {locale: target_collection}}."
            )
        return raw

    def _target_collection(self, source_collection: str, locale: str) -> str | None:
        """The target collection id for (source collection, locale), or None when the
        pair is not mapped (-> the create fails loudly; the pair is NEVER guessed into
        a default collection). Locale match is case-insensitive on the map keys so a
        'de-DE' map entry serves an internal 'de-de' locale."""
        try:
            cmap = self._collection_map()
        except ConnectorError:
            return None
        per = cmap.get(str(source_collection)) or {}
        if not isinstance(per, dict):
            return None
        loc = (locale or "").strip().lower()
        for k, v in per.items():
            if str(k).strip().lower() == loc:
                return _hs_id(v, "target collection id") if v else None
        return None

    def _source_collections(self) -> list[str]:
        """Discovery scope = the source collections (the collection_map keys)."""
        try:
            keys = [str(k) for k in self._collection_map().keys()]  # noqa: SIM118
        except ConnectorError:
            return []
        return [_hs_id(k, "source collection id") for k in keys]

    # --- auth / transport ---
    def _headers(self, write: bool = False) -> dict:
        key = (self.secret.get("api_key") or "").strip()
        if not key:
            raise ConnectorError("Help Scout Docs API key is required.")
        token = base64.b64encode(f"{key}:{_DUMMY_PASSWORD}".encode()).decode("ascii")
        h = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        _validate_target(API_BASE)  # SSRF guard (fixed public host; belt-and-braces)
        return API_BASE + path

    def _get(self, c, path, **kw):
        return self._request(c, "GET", self._url(path), headers=self._headers(), **kw)

    def _post(self, c, path, json_body, **kw):
        # POST /articles is non-idempotent (create) -> do NOT resend on 5xx/timeout; the
        # id map + reserve lock guard duplicate risk, not a blind retry.
        return self._request(
            c,
            "POST",
            self._url(path),
            idempotent=False,
            headers=self._headers(write=True),
            json=json_body,
            **kw,
        )

    def _put(self, c, path, json_body, **kw):
        # PUT /articles/{id} is a replace-in-place by id -> idempotent, safe to retry.
        return self._request(
            c,
            "PUT",
            self._url(path),
            idempotent=True,
            headers=self._headers(write=True),
            json=json_body,
            **kw,
        )

    # --- response unwrapping ([UNCERTAIN] envelope shapes; defensive both ways) ---
    @staticmethod
    def _unwrap_obj(body, *keys):
        """A single object from a Help Scout envelope: `{"article": {...}}` /
        `{"collection": {...}}`. [UNCERTAIN] exact wrapper key, so try each key then
        fall back to the body itself when it already looks like the object."""
        if not isinstance(body, dict):
            return None
        for k in keys:
            v = body.get(k)
            if isinstance(v, dict):
                return v
        # already-unwrapped (has an id) -> the object itself
        return body if body.get("id") is not None else None

    @staticmethod
    def _unwrap_list(body, *keys):
        """A list of objects from a paged Help Scout envelope:
        `{"articles": {"items": [...], "page": 1, "pages": N}}`. [UNCERTAIN] exact
        keys, so handle: wrapper->items, wrapper as a bare list, top-level items, or a
        bare list."""
        if isinstance(body, list):
            return body
        if not isinstance(body, dict):
            return []
        for k in keys:
            v = body.get(k)
            if isinstance(v, dict) and isinstance(v.get("items"), list):
                return v["items"]
            if isinstance(v, list):
                return v
        if isinstance(body.get("items"), list):
            return body["items"]
        return []

    @staticmethod
    def _pages(body, *keys):
        """Total page count from a paged envelope (default 1). Best-effort."""
        if not isinstance(body, dict):
            return 1
        for k in keys:
            v = body.get(k)
            if isinstance(v, dict) and v.get("pages"):
                try:
                    return int(v["pages"])
                except (TypeError, ValueError):
                    return 1
        try:
            return int(body.get("pages") or 1)
        except (TypeError, ValueError):
            return 1

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get(c, "/collections", params={"page": 1})
            if r.status_code in (401, 403):
                return False, (
                    f"Help Scout rejected the API key ({r.status_code}) — "
                    "check the Docs API key (not the Mailbox/Inbox key)."
                )
            if r.status_code != 200:
                return (
                    False,
                    f"Help Scout /collections returned {r.status_code}: {vendor_excerpt(r.text)}",
                )
            cols = self._unwrap_list(r.json(), "collections")
            try:
                mapped = self._source_collections()
            except ConnectorError:
                mapped = []
            n = len(cols)
            if not mapped:
                return True, (
                    f"Connected to Help Scout ({n} collection(s) visible). Add "
                    "a Collection map to start translating."
                )
            return True, (
                f"Connected to Help Scout ({n} collection(s) visible; "
                f"{len(mapped)} source collection(s) mapped)."
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: the account's collections. Best-effort."""
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, "/collections", params={"page": 1})
            if r.status_code != 200:
                return out
            for col in self._unwrap_list(r.json(), "collections"):
                if isinstance(col, dict) and col.get("id") is not None:
                    cid = str(col.get("id"))
                    name = col.get("name") or cid
                    out.append({"id": cid, "name": f"{name} ({cid})", "tables": []})
        except Exception:
            return out
        return out

    # --- discover: source articles ---
    def _list_collection_articles(self, c, collection_id: str, cap: int) -> list[dict]:
        """Article summaries in a source collection, page-paginated + bounded."""
        out: list[dict] = []
        page = 1
        collection_id = _hs_id(collection_id, "collection id")
        for _ in range(50):
            if len(out) >= cap:
                break
            r = self._get(c, f"/collections/{collection_id}/articles", params={"page": page})
            if r.status_code != 200:
                raise ConnectorError(
                    f"Help Scout list articles of collection {collection_id} -> "
                    f"{r.status_code}: {vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            items = self._unwrap_list(body, "articles")
            for a in items:
                if isinstance(a, dict) and a.get("id") is not None:
                    out.append(
                        {
                            "id": str(a.get("id")),
                            "collectionId": str(a.get("collectionId") or collection_id),
                            "name": a.get("name") or "",
                            # slug is carried through WHEN the list summary includes
                            # it (the rule-6 slug probe uses it); [UNCERTAIN] whether
                            # the list endpoint returns slug, so name is the fallback.
                            "slug": a.get("slug") or "",
                            "updatedAt": a.get("updatedAt") or "",
                        }
                    )
            if page >= self._pages(body, "articles") or not items:
                break
            page += 1
        return out

    def _fetch_article(self, c, article_id: str) -> dict | None:
        """The FULL article by id — the list endpoint may omit the HTML body/related,
        so write-back always fetches by id ([UNCERTAIN] whether list includes `text`;
        fetching by id is the documented-safe path). Returns the unwrapped article."""
        article_id = _hs_id(article_id, "article id")
        r = self._get(c, f"/articles/{article_id}")
        if r.status_code == 200:
            art = self._unwrap_obj(r.json(), "article")
            if isinstance(art, dict):
                return {
                    "id": str(art.get("id") or article_id),
                    "collectionId": str(art.get("collectionId") or ""),
                    "name": art.get("name") or "",
                    "text": art.get("text") or "",
                    "slug": art.get("slug") or "",
                    "related": [str(x) for x in (art.get("related") or [])],
                    "status": art.get("status") or "",
                    "hasDraft": bool(art.get("hasDraft")),
                    "updatedAt": art.get("updatedAt") or "",
                }
        return None

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source articles to translate. With explicit article_ids (retry path) each is
        fetched by id; otherwise every mapped source collection is listed."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        with self._client() as c:
            if article_ids:
                out = []
                scope = set(self._source_collections())
                for aid in list(article_ids)[:cap]:
                    art = self._fetch_article(c, _hs_id(aid, "article id"))
                    # Scope: a named article outside the mapped SOURCE collections is dropped like
                    # a missing one (it could be a target article or another team's page).
                    if art is not None:
                        self._charge_fetch(item_chars(art))  # #587: the full body was buffered
                        if art.get("collectionId") in scope:
                            out.append(art)
                return out
            out = []
            for col in self._source_collections():
                if len(out) >= cap:
                    break
                out.extend(self._list_collection_articles(c, col, cap - len(out)))
            return out[:cap]

    def show_article(self, article_id: str) -> dict | None:
        with self._client() as c:
            return self._fetch_article(c, str(article_id))

    def fetch_article_content(self, article_id: str) -> dict | None:
        """The full source article dict for the flow's prepare hook (id, collectionId,
        name, text HTML, slug, related, status)."""
        with self._client() as c:
            return self._fetch_article(c, str(article_id))

    # --- extract: title + HTML body units ---
    def extract_units(self, article_id: str, get_filter) -> tuple[list[dict], str]:
        """Article title (plain) + HTML body units, routed through the `html` filter.
        Returns (units, content_hash). Units are p_index-keyed: 'title' then body."""
        art = self.fetch_article_content(article_id) or {}
        title = art.get("name") or ""
        raw_body = art.get("text") or ""
        units = [{"p_index": "title", "text": title, "field": "title"}]
        for i, u in enumerate(get_filter("html").parse(raw_body.encode("utf-8")).units):
            units.append({"p_index": str(i), "text": u.text, "field": "body"})
        return units, content_hash(title, raw_body, art.get("slug") or "", art.get("related") or [])

    # --- write-back payload assembly (shared by create + fill) ---
    @staticmethod
    def _held(fields: dict) -> bool:
        """True when this pair carries a held (NEEDS_REVIEW / refused) title or body: that part
        is None in `fields` and is never replaced with source text (#640)."""
        f = fields or {}
        return bool(f.get("_no_translation") or f.get("title") is None or f.get("body") is None)

    def _target_status(self, fields: dict) -> str | None:
        """published only when this connection publishes AND the source article is published."""
        if self._publish() and (fields or {}).get("source_published"):
            return "published"
        return None

    def _related_for(self, locale: str, fields: dict) -> list:
        """related[] remapped to target-locale ids via the id map (unmapped ids stay pointing at
        the source article: idmap.remap_references). Every id is validated."""
        src = [_hs_id(x, "related article id") for x in ((fields or {}).get("related") or [])]
        if self.connection_id is None:
            return src
        return [
            _hs_id(x, "related article id")
            for x in idmap.remap_references(self.connection_id, src, locale)
        ]

    @staticmethod
    def _created_id(resp: httpx.Response) -> str | None:
        """The new article id from a create: the Location header (201 + Location with the new
        id, per the Docs API reference) or the JSON body, whichever is present."""
        loc = (resp.headers.get("Location") or "").strip()
        if loc:
            tail = loc.rstrip("/").rsplit("/", 1)[-1]
            if _DOC_ID.fullmatch(tail or ""):
                return tail
        try:
            art = HelpScoutConnector._unwrap_obj(resp.json(), "article")
        except Exception:
            art = None
        if isinstance(art, dict) and art.get("id") is not None:
            tail = str(art.get("id"))
            return tail if _DOC_ID.fullmatch(tail) else None
        return None

    # --- the four T3DocClient methods the DocPerLocaleStrategy drives ---
    def create_target(self, source_id: str, locale: str, fields: dict):
        """RESERVE AND FILL IN ONE CREATE: the target article is POSTed already carrying the full
        translated name and text (Help Scout requires both), so no reader ever sees an empty
        article. Returns (article_id | None, error).

        Refused, never guessed or degraded:
          - any held title or body unit (#640): a partial create would be a mixed-language article
            and its hash would hide that for good (the flow refuses first; this repeats it);
          - no target collection mapped for (source collection, locale).
        A crash between this POST landing and idmap.reserve() leaves a findable orphan that
        find_preexisting reclaims as needs_link next run rather than duplicating."""
        if self._held(fields):
            return None, f"{_HELD_BACK_PREFIX} not fully translated; article not created"
        source_collection = str((fields or {}).get("source_collection") or "")
        target_collection = self._target_collection(source_collection, locale)
        if not target_collection:
            return None, (
                f"no target collection mapped for source collection "
                f"{source_collection!r} / locale {locale} - add it to "
                f"collection_map (never guessed)"
            )
        payload = {
            "collectionId": target_collection,
            "name": fields["title"],
            "text": fields["body"],
            "status": self._target_status(fields) or "notpublished",
        }
        related = self._related_for(locale, fields)
        if related:
            payload["related"] = related
        try:
            with self._client() as c:
                r = self._post(c, "/articles", payload)
        except httpx.TransportError as e:
            return None, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201):
            aid = self._created_id(r)
            return (aid, None) if aid else (None, "create returned no id")
        return None, f"create article -> {r.status_code}: {vendor_excerpt(r.text)}"

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """Write the translated name/text onto the target article by id. The article is read first:
        a part that is held back (None in `fields`) is re-sent with its STORED value, never the
        source text (#640); content that already matches is not sent again (the one-shot create
        just wrote it). In draft mode a PUBLISHED target is never touched: it would put unreviewed
        machine text live (reported as live-kept, not a failure of the connection)."""
        tid = _hs_id(target_id, "article id")
        f = fields or {}
        if f.get("title") is None and f.get("body") is None:
            return WriteResult(
                False,
                False,
                target_id=tid,
                message=f"{_HELD_BACK_PREFIX} nothing translated this run; stored text kept",
            )
        locale = f.get("locale") or ""
        try:
            with self._client() as c:
                cur = self._fetch_article(c, tid)
                if cur is None:
                    return WriteResult(
                        False, False, target_id=tid, message="target article not readable"
                    )
                if not self._publish() and _is_published(cur):
                    return WriteResult(
                        False,
                        False,
                        target_id=tid,
                        message=f"{LIVE_KEPT_PREFIX} {LIVE_KEPT_REASON}",
                    )
                name = f["title"] if f.get("title") is not None else cur["name"]
                text = f["body"] if f.get("body") is not None else cur["text"]
                related = self._related_for(locale, f)
                # A held pair is never promoted to published (#640): it keeps its stored status.
                held = f.get("title") is None or f.get("body") is None
                status = None if held else self._target_status(f)
                same = (
                    name == cur["name"]
                    and text == cur["text"]
                    and (not related or related == cur["related"])
                    and (status is None or status == str(cur["status"]).lower())
                )
                if same:
                    return WriteResult(True, False, target_id=tid, message="unchanged")
                payload = {"name": name, "text": text}
                if related:
                    payload["related"] = related
                if status:
                    payload["status"] = status
                r = self._put(c, f"/articles/{tid}", payload)
        except httpx.TransportError as e:
            return WriteResult(False, False, target_id=tid, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return WriteResult(True, False, target_id=tid, message="filled")
        return WriteResult(
            False,
            False,
            target_id=tid,
            message=f"update article -> {r.status_code}: {vendor_excerpt(r.text)}",
        )

    def is_live_published(self, source_id: str, locale: str) -> bool:
        """Is the mapped target article for (source, locale) PUBLISHED? One map read and one GET;
        False when there is no target yet or it cannot be read."""
        if self.connection_id is None:
            return False
        row = idmap.get(self.connection_id, str(source_id), locale)
        if row is None or not row.target_id or row.status not in (idmap.DONE, idmap.BUILDING):
            return False
        with self._client() as c:
            return _is_published(self._fetch_article(c, row.target_id))

    def verify_target(self, target_id: str) -> bool:
        """Does the target article STILL EXIST (not deleted/404)? A fetch of the id
        (never a slug/name match - SPEC-t3 reconciliation rule: verify = fetch the id)."""
        tid = _hs_id(target_id, "article id")
        with self._client() as c:
            r = self._get(c, f"/articles/{tid}")
        if r.status_code in (404, 410):
            return False  # the only definite "gone" answers
        if r.status_code != 200:
            # Issue #444 (same class): 503 / 429 / 401 never means "deleted"; hold the pair.
            raise TargetUnverifiable(f"HTTP {r.status_code} verifying {tid}")
        return True

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        """RULE-6 collision probe: is there ALREADY an article in the target collection
        that our map never created (a pre-existing human translation), so we must NOT
        overwrite it? Returns its id (-> the strategy fails closed, needs_link) or None.

        The probe lists the target collection and matches by SLUG (the source slug we
        would reuse) then by NAME against our minted title AND the SOURCE title. Two
        MED-2 hardenings over the naive slug/minted-title match:
          - the list summary may OMIT `slug` ([UNCERTAIN]: support_extra.md §2.4). When
            the ENTIRE list lacks slug and we have a source slug to match, each candidate
            is fetched by id so a SAME-slug human translation is still caught. The
            candidate list is HARD-TRUNCATED to max_articles BEFORE this loop, so the
            by-id fetch count is bounded to max_articles (never one GET per in-page
            article — _list_collection_articles can overshoot the cap within a page).
          - name matching also compares the SOURCE title, so a human article that kept
            the source-language title (not our '[locale] ...' minted one) is caught, BUT
            only when the source title is UNIQUE among the run's source articles (see
            below).

        NAME MATCHING IS SKIPPED FOR NON-UNIQUE SOURCE TITLES: a source title shared by
        two or more source articles in scope is an unreliable identity key — a legit
        second same-titled source would false-collide with a human target (or the first
        source's own target, which carries the same minted title) and be permanently
        stuck at needs_link. So when `source_title_unique` is not set, this probe falls
        back to SLUG-ONLY matching, and a duplicate-titled source creates normally.

        DOCUMENTED RESIDUALS:
          - a human translation that LOCALIZED BOTH the slug AND the title shares no key
            with the source, so this probe returns None -> rule 1 mints a SEPARATE target
            article beside the human's. The human's article is never touched (we only
            ever write our own map's id), so the failure mode is a DUPLICATE, not a
            clobber. Detecting it would need a durable human<->source link Help Scout
            does not provide.
          - source-title matching is skipped for non-unique source titles, so a
            duplicate-titled source falls back to slug-only detection.
        Also reclaims a same-slug/name ORPHAN left by a crash between our own POST and
        idmap.reserve() (rule-1 orphan -> needs_link, no duplicate)."""
        source_collection = str((fields or {}).get("source_collection") or "")
        target_collection = self._target_collection(source_collection, locale)
        if not target_collection:
            return None  # unmapped pair -> create_target rejects it loudly; nothing to probe
        want_slug = ((fields or {}).get("slug") or "").strip().lower()
        # Name matching (our minted title AND the source title) is trustworthy ONLY when
        # the source title is unique among the run's source articles; otherwise fall back
        # to slug-only (see the docstring) so a same-titled second source creates.
        want_names: set = set()
        if (fields or {}).get("source_title_unique"):
            want_names = {
                n
                for n in (
                    ((fields or {}).get("title") or "").strip().lower(),  # our minted title
                    ((fields or {}).get("source_title") or "").strip().lower(),  # the source title
                )
                if n
            }
        try:
            with self._client() as c:
                cap = fetch_cap(self.config.get("max_articles"), 50)
                # NEW LOW: _list_collection_articles stops PAGINATING at the cap but does
                # not truncate WITHIN a page, so it can return more than `cap` items. Hard-
                # slice here so the by-id slug fetch loop below issues at most `cap` GETs
                # (else a first sync in the slug-omitting shape is N+1 on the whole page).
                arts = self._list_collection_articles(c, target_collection, cap)[:cap]
                if want_slug:
                    # If NO summary carries a slug, the list endpoint omits it
                    # ([UNCERTAIN] shape) — fetch each candidate by id (bounded) so a
                    # same-slug human article is still detected. If ANY summary has a
                    # slug, trust the summaries (avoid a fetch per article).
                    list_has_slug = any(str(a.get("slug") or "").strip() for a in arts)
                    for a in arts:
                        aslug = str(a.get("slug") or "").strip().lower()
                        if not aslug and not list_has_slug:
                            full = self._fetch_article(c, str(a.get("id")))
                            aslug = str((full or {}).get("slug") or "").strip().lower()
                        if aslug and aslug == want_slug:
                            return str(a.get("id"))
                if want_names:
                    for a in arts:
                        if str(a.get("name") or "").strip().lower() in want_names:
                            return str(a.get("id"))
        except ConnectorError:
            return None
        return None

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (article, unit) for the generic pull view. The rich flow
        is store.helpscout_translate (creates per-locale articles in mapped
        collections)."""
        from app.filters import get_filter

        out: list[PulledItem] = []
        self._reset_fetch_budget()
        for art in self.discover_articles():
            try:
                units, _h = self.extract_units(str(art.get("id")), get_filter)
            except ConnectorError as e:
                if isinstance(e, FetchBudgetError):
                    raise
                continue
            # #587 gate: each article is fetched in full here and its text stays in `out`
            # until the document is stored, so it counts against the run budget.
            self._charge_fetch(sum(len(u["text"]) for u in units))
            for u in units:
                out.append(
                    PulledItem(
                        external_id=str(art.get("id")),
                        field=f"{u['field']}:{u['p_index']}",
                        text=u["text"],
                        title=(art.get("name") or "")[:60],
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Help Scout creates a per-locale article per source article - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list the mapped source collections for the
        caller to diff against the persisted per-(article,locale) content hash. Returns
        (articles, 0) — updatedAt is metadata-tainted, so the hash in
        store.helpscout_translate is the gate; NO watermark is advanced."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow moved here from store.py (Wave 3 store.py decomposition).
# The per-connector hook bundle + helpscout_translate wrapper live with the
# connector; store.py keeps a thin delegator so store.helpscout_translate stays
# importable/monkeypatchable (jobs/CLI/webhook dispatch + verify suites).
# ===========================================================================
import logging

from app.services.connectors import flow

log = logging.getLogger("loc_tms.connectors")


# ------------------------- Help Scout Docs (T3) hooks ----------------------
# T3 (document-per-locale): Help Scout Docs has NO native i18n, so a "translation" is
# a SEPARATE article created in a mapped target-language COLLECTION and tracked in
# ConnectorIdMap via DocPerLocaleStrategy (SPEC-t3-notion-helpscout). This REUSES all
# of Notion's T3 machinery (the strategy owns reserve-then-fill + the 7-case
# reconciliation table); these hooks only prepare the HTML body extraction + assemble
# the translated payload. Body is HTML -> get_filter("html") (no walker, unlike
# Notion). Poll-first, so NO watermark (updatedAt is metadata-tainted — the content
# hash is the gate).
def _hs_preflight(ctx):
    # MED-3: defensively re-validate the collection_map partition at run start, so a
    # config written BEFORE the create/update guard existed still fails the run loudly
    # (a shared/looping target is never silently processed). Raised here, it is caught
    # by the flow's preflight try/except -> a clean per-run ERROR, no rows written.
    from app.services.connectors.helpscout import validate_config

    validate_config(ctx.cfg or {})
    # enabled = every target locale EXCEPT the source (Help Scout has no per-account
    # enabled-locale API; any locale is writable as a new article). The default
    # partition then rejects the source locale; the collection map rejects any
    # (source collection, locale) it does not map — enforced loudly at create time.
    src = (ctx.cfg.get("source_locale") or "en").strip().lower()
    enabled = {l for l in ctx.locales if l and l != src}  # noqa: E741
    return enabled, {}


def _hs_prepare(ctx, art):
    from app.filters import get_filter

    # The live-kept park hook (_hs_parked) needs this run's stored hashes.
    ctx.connector._run_hashes = ctx.hashes
    aid = str(art.get("id") or "")
    try:
        full = ctx.connector.fetch_article_content(aid)
    except Exception as e:
        return flow.SkipArticle("unreadable_skipped", error=f"{aid}: {type(e).__name__}: {e}")
    if full is None:
        return flow.SkipArticle("unreadable_skipped", error=f"{aid}: article not found")
    title = full.get("name") or ""
    raw_body = full.get("text") or ""
    if not title.strip() and not raw_body.strip():
        # Nothing translatable -> skip without creating an empty article.
        return flow.SkipArticle("empty_skipped")
    from app.services.connectors import idmap
    from app.services.connectors.helpscout import content_hash

    body_units = get_filter("html").parse(raw_body.encode("utf-8")).units
    related = full.get("related") or []
    # MED-1: fold the RESOLVED cross-reference state into the suppression hash. The base
    # parts (title/body/slug/related-source-ids) are byte-identical to before, so a
    # zero-references article's hash is unchanged (no mass re-translate on upgrade). Only
    # when the article HAS references do we append a digest of how each one currently
    # resolves across the target locales — so when a referent later reaches `done`, this
    # article's hash changes and it re-fills, healing the previously source-linked
    # related[] (content-hash suppression otherwise never revisits it).
    parts = [title, raw_body, full.get("slug") or "", related]
    if related:
        ref_digest = idmap.resolved_reference_digest(ctx.cid, related, ctx.usable)
        if ref_digest:
            parts.append(ref_digest)
    chash = content_hash(*parts)
    # NEW MED (source-title uniqueness guard): the rule-6 name probe may only use the
    # SOURCE title as a match key when it is UNIQUE among the run's source articles — a
    # non-unique title would false-collide a legit second same-titled source. Compute the
    # multiset of source titles once (cached on the per-run connector) and flag whether
    # THIS article's title is unique. Empty/absent titles are treated as unique (they add
    # no name key anyway). find_preexisting drops name matching when this is False.
    counts = getattr(ctx.connector, "_hs_src_title_counts", None)
    if counts is None:
        counts = {}
        for a in ctx.articles or []:
            nm = (a.get("name") or "").strip().lower()
            if nm:
                counts[nm] = counts.get(nm, 0) + 1
        ctx.connector._hs_src_title_counts = counts
    title_unique = (not title.strip()) or counts.get(title.strip().lower(), 0) <= 1
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": title},
        doc_title=f"{(title or 'article')[:50]} ({aid})",
        extra={
            "body_bytes": raw_body.encode("utf-8"),
            "source_title": title,
            "source_collection": str(full.get("collectionId") or ""),
            "slug": full.get("slug") or "",
            "related": full.get("related") or [],
            "source_title_unique": title_unique,
            "source_published": str(full.get("status") or "").strip().lower() == "published",
        },
    )


def _hs_is_suppressed(connector, aid, loc):
    # Reads the MAP (a `done` id-map row), never content (SPEC-t3 invariant). A
    # building/needs_link/orphaned row is NOT 'done', so it falls through to
    # write_target and the reconciliation table resumes/fails-closed/recreates.
    return connector.strategy.exists_target(connector, aid, loc)


def _hs_build_payload(prep, loc, seg0_target, mapping):
    """Only parts that carry a usable translation. A held (NEEDS_REVIEW / refused) title is None
    and so is the body when ANY of its text units is held (HTML cannot be half written), so the
    stored target is never overwritten with source-language text and a new one is never created
    mixed-language (defect taxonomy B, #640)."""
    from app.filters import get_filter

    ex = prep.extra
    body_held = any(
        str(u.p_index) not in mapping and (u.text or "").strip() for u in prep.body_units
    )
    body = None
    if not body_held:
        body = get_filter("html").build(ex["body_bytes"], mapping).decode("utf-8")
    # source_title: the rule-6 pre-existing-translation probe compares against BOTH the minted
    # title and the SOURCE title (a human article that kept the source-language title).
    return {
        "title": seg0_target,  # None = held back: never the source title
        "body": body,  # None = some unit held back: never a mixed-language body
        "locale": loc,
        "source_collection": ex["source_collection"],
        "slug": ex["slug"],
        "source_title": ex["source_title"],
        "related": ex["related"],
        "source_title_unique": ex.get("source_title_unique", False),
        "source_published": ex.get("source_published", False),
        "_no_translation": seg0_target is None and body is None,
    }


def _hs_parked(connector, aid, loc, chash):
    """Flow hook: in draft mode a pair whose mapped target article is already PUBLISHED is skipped
    BEFORE any MT is spent and reported as kept live (never written, no hash). fill_target repeats
    the check at write time (an explicit retry bypasses this hook)."""
    if connector._publish():
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


def _is_held(f) -> bool:
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _live_kept(ctx):
    return list(getattr(ctx.connector, "_live_kept", []) or []) + [
        f for f in ctx.failures if _is_live_kept_text(f.get("error"))
    ]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_live_kept_text(f.get("error")) and not _is_held(f)]


def _hs_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _hs_build_message(ctx):
    from app.services.connectors import idmap

    parts = [
        f"Created {ctx.created}, updated {ctx.updated} localized article(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) article(s).")
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} empty article(s).")
    if ctx.counters.get("unreadable_skipped"):
        parts.append(f"Skipped {ctx.counters['unreadable_skipped']} unreadable article(s).")
    held = [f for f in ctx.failures if _is_held(f)]
    if held:
        parts.append(
            f"Held back {len(held)} (article, locale) pair(s) awaiting review (NEEDS_REVIEW): a "
            f"held title or body is never replaced with source text, so nothing was created and a "
            f"stored translation keeps its text. They will be retried next run."
        )
    needs_link = idmap.status_counts(ctx.cid).get("needs_link", 0)
    if needs_link:
        parts.append(
            f"{needs_link} article(s) need a link decision (a pre-existing "
            f"translation was found - adopt or ignore it)."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) (source locale is never "
            f"a target): {', '.join(ctx.rejected)}."
        )
    live = _live_kept(ctx)
    if live:
        sample = ", ".join(f"{f['article_id']}/{f['locale']}" for f in live[:5])
        parts.append(
            f"Kept {len(live)} LIVE (published) article(s) unchanged: {sample}. This connection "
            "writes drafts, so new machine text is never written into a published article. Set "
            "Publish translations = 1 to update live articles."
        )
    real = _real_failures(ctx)
    errors = [e for e in ctx.errors if not _is_live_kept_text(e) and _HELD_BACK_PREFIX not in e]
    if real and errors:
        parts.append("Errors: " + "; ".join(errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())
        log.warning(
            "helpscout_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _hs_build_result(ctx):
    from app.services.connectors import idmap

    res = {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "unreadable_skipped": ctx.counters.get("unreadable_skipped", 0),
        "rejected": ctx.rejected,
        "failures": _real_failures(ctx),
        "id_map": idmap.status_counts(ctx.cid),
    }
    held = [f for f in ctx.failures if _is_held(f)]
    if held:
        res["held_back"] = len(held)
    if _live_kept(ctx):
        res["live_kept"] = _live_kept(ctx)
    return res


def helpscout_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Help Scout Docs articles into one or more locales, each written as a
    per-locale ARTICLE in the mapped target-language COLLECTION via the T3
    DocPerLocaleStrategy (reserve-then-fill + id-map reconciliation, so the source
    article and other locales are never overwritten and a partial build resumes rather
    than duplicating).

    Thin wrapper over flow.run_translate_flow. Help-Scout-specific behavior via hooks: HTML
    body extraction (get_filter("html")), collection-per-language mapping (an unmapped pair is
    rejected loudly at create), related[] cross-link remap, source-locale rejection,
    content-hash suppression, rule-6 needs_link surfacing, the #640 held-unit rules and the
    draft-mode live-kept guard. Poll-first, so NO watermark.
    only_pairs restricts the write loop to those (article_id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="helpscout",
        no_conn_msg="No such Help Scout connection.",
        hash_key="helpscout_hashes",
        probe_label="Help Scout",
        preflight=_hs_preflight,
        prepare=_hs_prepare,
        is_suppressed=_hs_is_suppressed,
        build_payload=_hs_build_payload,
        compute_draft=lambda cfg: (
            str(cfg.get("publish", "0")).strip().lower() not in ("1", "true", "yes")
        ),
        compute_ok=_hs_compute_ok,
        build_message=_hs_build_message,
        build_result=_hs_build_result,
        extra_counters=("empty_skipped", "unreadable_skipped", "parked_skipped"),
        held_prefix=_HELD_BACK_PREFIX,
        parked=_hs_parked,
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


def helpscout_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Operator resolution of a Help Scout needs_link row (SPEC-t3 rule 6 adopt-or-
    ignore): a pre-existing target article the id map never created. `action` is
    'adopt' (accept it as ours -> the next run UPDATES it) or 'ignore' (drop the row ->
    it re-flags). The one seam web + CLI + /api all call. Never overwrites content — it
    only edits the id map."""
    from app.services.connectors import idmap
    from app.services.connectors.store import _log, get

    conn = get(cid)
    if not conn or conn.kind != "helpscout":
        return False, "No such Help Scout connection.", {}
    ok, msg = idmap.resolve_needs_link(cid, str(source_id), target_locale, action)
    _log(cid, "resolve-link", ok, 0, msg, actor)
    return ok, msg, {"id_map": idmap.status_counts(cid)}
