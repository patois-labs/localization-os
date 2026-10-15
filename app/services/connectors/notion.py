"""Notion connector: T3 (document-per-locale), rebuilt on the 2026-03-11 API (connector parity).

Topology T3: Notion has NO native localization, so a "translation" is a page the
connector creates and links itself, and the connector OWNS the source->target page id
map (ConnectorIdMap, via strategy.DocPerLocaleStrategy). A target page is created as a
CHILD page of its source page, titled per `title_pattern` (default "[{loc}] {title}").

WHY child pages and not a "Language" property on a database. A real customer's
documentation lives either as a page tree (wiki, handbook, help center) or as a database
of pages. A child page needs no schema: it works under a plain page AND under a database
row, it leaves the customer's database untouched (no new Language column, no duplicate
rows in views, rollups, relations, formulas or automations), and it is what people do by
hand when they translate a Notion page. The one cost, that a translation is not a row of
the same view, is the customer's to accept; the id map keeps it addressable.

Scope. The configured root is a PAGE (its child pages are the sources; `source_depth` > 1
also takes pages nested below them) or a DATABASE (its rows are the sources, read through
the data-source query endpoint). Nothing outside that subtree is read or written: ids are
validated and an explicit page id is checked against the root's ancestry.

API VERSION (verified against developers.notion.com on 2026-10-10):
  - Notion-Version is pinned to 2026-03-11 (reference/versioning: "the most recent").
  - 2025-09-03 split databases into data sources: a database is read with
    GET /v1/databases/{id} (its `data_sources` array), its rows with
    POST /v1/data_sources/{id}/query.
  - 2026-03-11: `archived` became `in_trash` (requests and responses), append-children takes
    `position` instead of `after` (we only append at the end, the default), and the
    `transcription` block is `meeting_notes` (neither can be created through the API).
  - This connector creates pages only under PAGE parents (child pages), so the data-source
    parent shape never appears in a request.

#640 (held-back units): a unit with no usable translation never ships source text. No
target page is created while any unit is held (the flow engine gates it and create_target
refuses it again). An update of an existing target keeps a held title and, when any body
unit is held, the whole stored body (the body is replaced as a unit, so a partly
translated body cannot be written without source text in it). No content hash is recorded,
so the pair is retried after approval.

Gotchas kept from the first build (docs_platforms.md section 3.7): no idempotency and no
transactions (reserve the target id BEFORE filling, write-then-swap the body); rich_text
runs are translated one by one so annotations, links and mentions survive (the 2000-char
cap splits a run into identically formatted runs); the block tree is rebuilt one nesting
level per request; the integration only sees pages that were SHARED with it.

CHANGE DETECTION: poll-only. `last_edited_time` bumps on any edit, so no watermark: the
per-(page,locale) source-content HASH is the only re-translate gate. No webhook verifier is
registered (never wire a verifier on a guessed signature scheme).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse

import httpx

from app.filters.base import InputLimitError
from app.services.connectors import flow, notion_blocks
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
from app.services.connectors.strategy import DocPerLocaleStrategy, TargetUnverifiable, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

API_BASE = "https://api.notion.com/v1"
# Pinned Notion-Version: the newest (reference/versioning). `api_version` in the connection
# config may pin another dated version (validated: YYYY-MM-DD, ASCII). Reads here accept both
# `in_trash` and the older `archived`. Versions before 2025-09-03 cannot read a database root
# (no data sources); a page root works on any version.
NOTION_VERSION = "2026-03-11"
_DATA_SOURCES_SINCE = "2025-09-03"
_VERSION_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_APPEND_BATCH = 100  # Notion children append cap per request
_MAX_TREE_BLOCKS = 2000  # safety cap on blocks read/written per page
_MAX_TREE_DEPTH = 30  # nesting guard for the recursive read
_MAX_SOURCE_DEPTH = 5
_MAX_LIST_REQUESTS = 200  # bound on listing requests per discovery

# The rule-6 collision probe (find_preexisting) matches a child page's leading "[{loc}] "
# tag: the exact bracketed locale tag, case insensitive, followed by a space or end of
# string. "[de-de]" never matches a "[de]" target or vice versa.
_LOCALE_TAG_RE = re.compile(r"^\[([^\]]*)\](?:\s|$)")

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX32_RE = re.compile(r"[0-9a-f]{32}")


def canon_id(raw) -> str | None:
    """Canonical dashed UUID for a Notion page / database / data-source id, or None.

    Accepts the 32-hex form, the dashed form, or a notion.so / notion.site URL whose path
    ends in the 32-hex id. ASCII only (a `\\d` class would take Arabic-Indic digits), so an
    id can never smuggle a path segment or a query string into a request."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 2048:
        return None
    if "/" in s or "?" in s:
        try:
            u = urllib.parse.urlsplit(s if "://" in s else "https://" + s)
        except ValueError:
            return None
        host = (u.hostname or "").lower()
        if not (host == "notion.so" or host.endswith((".notion.so", ".notion.site"))):
            return None
        tail = u.path.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "")
        m = re.search(r"([0-9a-f]{32})$", tail)
        if not m:
            return None
        s = m.group(1)
    s = s.lower()
    if _HEX32_RE.fullmatch(s):
        s = f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"
    return s if _UUID_RE.fullmatch(s) else None


def _is_trashed(obj) -> bool:
    """in_trash (2026-03-11), is_archived / archived (older shapes): any of them = gone."""
    return isinstance(obj, dict) and bool(
        obj.get("in_trash") or obj.get("is_archived") or obj.get("archived")
    )


def _rich_text_plain(rich_text) -> str:
    """Flatten a Notion rich_text array to plain text (run contents joined)."""
    if not isinstance(rich_text, list):
        return ""
    out = []
    for el in rich_text:
        if isinstance(el, dict):
            txt = (el.get("text") or {}).get("content")
            out.append(txt if isinstance(txt, str) else (el.get("plain_text") or ""))
    return "".join(out)


def _page_title(page: dict) -> str:
    """The title text of a Notion page object: the property whose type is 'title'."""
    props = (page or {}).get("properties") or {}
    for _name, val in props.items():
        if isinstance(val, dict) and val.get("type") == "title":
            return _rich_text_plain(val.get("title"))
    # A child_page block carries the title directly.
    cp = (page or {}).get("child_page")
    if isinstance(cp, dict) and isinstance(cp.get("title"), str):
        return cp["title"]
    return ""


def _has_translated_content(fields: dict) -> bool:
    """False when a write-back payload carries ZERO translated content (every segment was
    refused / NEEDS_REVIEW this run). _nt_build_payload stamps `_no_translation`."""
    return not bool((fields or {}).get("_no_translation"))


def _held(fields: dict) -> bool:
    """True when the payload has a held (NEEDS_REVIEW / empty) title or body unit (#640)."""
    return bool((fields or {}).get("_held_title") or (fields or {}).get("_held_body"))


def _title_property_value(text: str) -> dict:
    """The page-property VALUE object for the title property (create + update)."""
    return {"title": [{"type": "text", "text": {"content": (text or "")[:2000]}}]}


def _error_ids(resp, key: str) -> list:
    """Ids a 5xx answer says were committed anyway (`committed_resource_id` for a page,
    `committed_child_ids` for appended blocks; docs: reference/request-limits, "Writes that
    return 503"). Where in the body they sit is not shown there, so the top level and
    `additional_data` are both read. Only well-formed ids are returned."""
    try:
        body = resp.json()
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    extra = body.get("additional_data")
    found = body.get(key)
    if found is None and isinstance(extra, dict):
        found = extra.get(key)
    if isinstance(found, str):
        found = [found]
    out = []
    for x in found if isinstance(found, list) else []:
        cid = canon_id(x) if isinstance(x, str) else None
        if cid:
            out.append(cid)
    return out


SPEC = ConnectorSpec(
    kind="notion",
    name="Notion",
    icon="\U0001f5d2",  # spiral notepad
    cat="Knowledge base / help center",
    blurb="Translate Notion pages into any language. Notion has no native "
    "localization, so each translation is created as a CHILD page of the "
    "source (titled '[locale] Title') and tracked in an internal id map, so "
    "the source page and other locales are never overwritten. Works under a "
    "page or a database; formatting, links and mentions are kept. A partial "
    "build resumes instead of duplicating. Notion has NO draft state: every "
    "write is live at once, and a translation page edited by hand in Notion is "
    "never overwritten (it is reported instead).",
    config_fields=[
        FieldSpec(
            "parent_page_id",
            "Root page or database id",
            "The Notion page whose child pages are your SOURCE pages, or a database whose "
            "rows are. The id (32 hex characters) or the page URL. Share the integration "
            "with it; each translation is created as a child of its source. Nothing "
            "outside this page or database is read or written.",
            required=True,
            placeholder="1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the source pages (BCP-47, e.g. en). Never written as a target. "
            "Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locales to create (e.g. de-DE, fr, es-MX).",
            required=False,
            placeholder="de-DE, fr, es-MX",
        ),
        FieldSpec(
            "title_pattern",
            "Translated title",
            "{loc} = locale, {title} = translated title (default '[{loc}] {title}'). Must "
            "contain {loc}. Also the collision key: a matching pre-existing page is never "
            "overwritten.",
            required=False,
            placeholder="[{loc}] {title}",
        ),
        FieldSpec(
            "source_depth",
            "Source depth",
            "How many page levels below a root PAGE are source pages (1 = its direct "
            "children, the default; at most 5). A database root always uses its rows.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "api_version",
            "Notion-Version",
            f"Notion API version to pin (default {NOTION_VERSION}).",
            required=False,
            placeholder=NOTION_VERSION,
        ),
        FieldSpec(
            "max_pages", "Max pages", "Safety cap per run.", required=False, placeholder="25"
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "token",
            "Integration token",
            "A Notion internal integration token, sent as Bearer. Create it at "
            "notion.so/profile/integrations and SHARE your root page with it.",
            secret=True,
            placeholder="ntn_...",
        ),
    ],
    setup_steps=[
        "Create an internal integration at notion.so/profile/integrations (capabilities: "
        "read, update and insert content) and copy its token (starts with 'ntn_').",
        "Open your root page in Notion, click ... -> Connections -> add your integration "
        "(this SHARES it, and its sub-pages: without this every API call answers 404).",
        "Paste the integration token above and set the Root page or database id to that "
        "page (the id in its URL, 32 hex characters).",
        "Set your Source locale (never translated) and Target locales.",
        "Notion has no confirmed webhook here, so run on a schedule (Every N minutes) "
        "for change detection: the source-content hash keeps translations fresh.",
    ],
    other_side="Notion change detection polls the root's source pages and gates "
    "re-translation on a per-(page,locale) source-content hash "
    "(last_edited_time bumps on any edit, so it never drives a watermark). "
    "Each translation is a child page tracked in the connector's id map; "
    "a pre-existing same-title child is never overwritten (needs_link), and a "
    "translation page changed in Notion after our last write is reported, not "
    "overwritten, and skipped without MT until the source changes (move it to the "
    "trash to have it re-created). Writes are live: there is no draft or publish "
    "step. An update adds the new body before it removes the old one, so a page is "
    "never empty; readers may see both for a moment.",
    docs_url="https://developers.notion.com/reference",
    supports_oauth=False,  # internal-token path here; OAuth 2.1 arrives with mcp_cms.
    archived=False,  # rebuilt on the 2026-03-11 API (connector parity); live QA is pending
)


class NotionConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="notion",
        topology=Topology.T3,  # document-per-locale
        auth=(AuthKind.API_KEY,),
        api_version=NOTION_VERSION,
        rich_text=RichText.NOTION_BLOCKS,  # recursive block tree of rich_text[] (notion_blocks.py)
        regions="fixed",  # single global host api.notion.com
        change_signals=ChangeSignals(
            webhook_sig=None,  # poll-only, no verifier
            # last_edited_time bumps on ANY edit (not content-only), so it is recorded but
            # NEVER advanced as a watermark: the content hash is the real gate.
            poll_field="last_edited_time",
            stale_flag=None,
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = DocPerLocaleStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        self._root_info: tuple | None = None
        self._target_id_set: set | None = None
        self._fill_mutated = False
        # Pages THIS connector created (create_target). A first fill that fails on one of
        # them trashes it, so readers never meet an empty "[loc] ..." page (the strategy's
        # rule 5 then recreates it on the next run).
        self._fresh_ids: set = set()
        # Per-run notes the flow reports (blocks not copied, mentions kept as text, ...):
        # a WriteResult message on success never reaches the operator, this list does.
        self.write_notes: list = []

    # --- config helpers ---
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en").strip().lower() or "en"

    def _api_version(self) -> str:
        v = str(self.config.get("api_version") or "").strip()
        return v if _VERSION_RE.fullmatch(v) else NOTION_VERSION

    def _root_id(self) -> str:
        raw = self.config.get("parent_page_id")
        if not isinstance(raw, str) or not raw.strip():
            raise ConnectorError("Notion root page id is required.")
        rid = canon_id(raw)
        if not rid:
            raise ConnectorError(
                "The Notion root id is not valid: use the 32-character id of the page or "
                "database (or its notion.so URL)."
            )
        return rid

    def _source_depth(self) -> int:
        try:
            n = int(str(self.config.get("source_depth") or 1).strip())
        except ValueError:
            return 1
        return max(1, min(_MAX_SOURCE_DEPTH, n))

    def _title_pattern(self) -> str:
        pat = (self.config.get("title_pattern") or "").strip()
        # a pattern without {loc} would give every locale the same title and make the
        # collision probe flag the second locale of every page: use the default instead.
        return pat if "{loc}" in pat else "[{loc}] {title}"

    def _target_title(self, loc: str, translated_title: str) -> str:
        # plain substitution, never str.format (a pattern like {title.__class__} must not run)
        return (
            self._title_pattern()
            .replace("{loc}", str(loc or ""))
            .replace("{title}", str(translated_title or ""))
        )

    def _configured_tags(self) -> set:
        raw = str(self.config.get("target_locales") or "")
        tags = {x.strip().lower() for x in re.split(r"[,\s;]+", raw) if x.strip()}
        tags.add(self._source_locale())
        return tags

    def _looks_translated(self, title: str) -> bool:
        """A page titled with one of OUR locale tags is a translation, never a source."""
        m = _LOCALE_TAG_RE.match((title or "").strip())
        return bool(m and m.group(1).strip().lower() in self._configured_tags())

    def _our_target_ids(self) -> set:
        if self._target_id_set is None:
            ids: set = set()
            cid = getattr(self, "connection_id", None)
            if cid is not None:
                from app.services.connectors import idmap

                for row in idmap.list_for_connection(cid):
                    t = canon_id(row.target_id or "")
                    if t:
                        ids.add(t)
            self._target_id_set = ids
        return self._target_id_set

    # --- auth / transport ---
    def _headers(self, write: bool = False) -> dict:
        token = (self.secret.get("token") or "").strip()
        if not token:
            raise ConnectorError("Notion integration token is required.")
        h = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self._api_version(),
            "Accept": "application/json",
        }
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        _validate_target(API_BASE)  # SSRF guard (fixed public host; belt-and-braces)
        return API_BASE + path

    def _get(self, c, path, **kw):
        return self._request(c, "GET", self._url(path), headers=self._headers(), **kw)

    def _query(self, c, path, json_body, **kw):
        # a data-source query is a POST that only READS: safe to resend on 5xx.
        return self._request(
            c,
            "POST",
            self._url(path),
            idempotent=True,
            headers=self._headers(write=True),
            json=json_body,
            **kw,
        )

    def _post(self, c, path, json_body, **kw):
        # POST /pages is non-idempotent (create) -> never resend on 5xx/timeout; the id map
        # and the reserve lock guard against duplicates, not a blind retry.
        return self._request(
            c,
            "POST",
            self._url(path),
            idempotent=False,
            headers=self._headers(write=True),
            json=json_body,
            **kw,
        )

    def _patch(self, c, path, json_body, *, idempotent=True, **kw):
        return self._request(
            c,
            "PATCH",
            self._url(path),
            idempotent=idempotent,
            headers=self._headers(write=True),
            json=json_body,
            **kw,
        )

    def _delete(self, c, path, **kw):
        return self._request(c, "DELETE", self._url(path), headers=self._headers(), **kw)

    # --- root: page or database ---
    def _root(self, c) -> tuple:
        """(kind, root_id, data_source_ids) for the configured root; kind is 'page' or
        'database'. Cached per connector instance. Raises ConnectorError with an operator
        message when the token is wrong or the root is not shared."""
        if self._root_info is not None:
            return self._root_info
        rid = self._root_id()
        r = self._get(c, f"/pages/{rid}")
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"Notion rejected the token or its capabilities ({r.status_code}): check the "
                "integration token and that it may read content."
            )
        if r.status_code == 200 and not _is_trashed(r.json()):
            self._root_info = ("page", rid, [])
            return self._root_info
        if r.status_code == 200:
            raise ConnectorError("The Notion root page is in the trash.")
        if r.status_code in (400, 404):
            d = self._get(c, f"/databases/{rid}")
            if d.status_code == 200:
                body = d.json() or {}
                if _is_trashed(body):
                    raise ConnectorError("The Notion root database is in the trash.")
                if self._api_version() < _DATA_SOURCES_SINCE:
                    raise ConnectorError(
                        f"A database root needs Notion-Version {_DATA_SOURCES_SINCE} or "
                        "later (data sources); clear the Notion-Version field."
                    )
                ds = [
                    x
                    for x in (
                        canon_id(str(s.get("id") or "")) for s in body.get("data_sources") or []
                    )
                    if x
                ]
                self._root_info = ("database", rid, ds)
                return self._root_info
            if d.status_code in (401, 403):
                raise ConnectorError(f"Notion refused the database read ({d.status_code}).")
            raise ConnectorError(
                "The integration cannot see the Notion root. Open the page or database -> "
                "Connections -> add this integration (shared with 0 pages)."
            )
        raise ConnectorError(
            f"Notion returned {r.status_code} for the root: {vendor_excerpt(r.text)}"
        )

    # --- operations: test / containers ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                who = self._get(c, "/users/me")
                if who.status_code in (401, 403):
                    return False, (
                        f"Notion rejected the token ({who.status_code}) - "
                        "check the integration token."
                    )
                if who.status_code != 200:
                    return (
                        False,
                        f"Notion /users/me returned {who.status_code}: {vendor_excerpt(who.text)}",
                    )
                if not (self.config.get("parent_page_id") or "").strip():
                    return True, (
                        "Connected to Notion (token valid). Set a Root page or database "
                        "id and share the integration with it to translate."
                    )
                kind, _rid, _ds = self._root(c)
                n = len(self._list_sources(c, 100))
            pat_note = ""
            raw_pat = (self.config.get("title_pattern") or "").strip()
            if raw_pat and "{loc}" not in raw_pat:
                pat_note = " The Translated title has no {loc}, so the default pattern is used."
            if n == 0:
                return True, (
                    f"Connected and shared (root {kind}, API {self._api_version()}), but it has "
                    "0 source pages to translate (shared with 0 pages). Add pages under it, "
                    "or point at a different root." + pat_note
                )
            return True, (
                f"Connected to Notion (API {self._api_version()}, root {kind} shared; "
                f"{n} source page(s) found)." + pat_note
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """Pickable scopes for the UI: the root's source pages. Best-effort."""
        out: list[dict] = []
        try:
            if not (self.config.get("parent_page_id") or "").strip():
                return out
            with self._client() as c:
                for pg in self._list_sources(c, 100):
                    pid = str(pg.get("id") or "")
                    out.append(
                        {"id": pid, "name": f"{pg.get('title') or pid} ({pid})", "tables": []}
                    )
        except Exception:
            return out
        return out

    # --- discover: source pages ---
    def _list_child_pages(self, c, parent_id: str, cap: int | None = None) -> list[dict]:
        """Direct child_page blocks under `parent_id` (trashed ones skipped),
        cursor-paginated + bounded. With `cap` (#579) it stops requesting pages as soon as
        it holds more than `cap` pages, so a parent with a million children is never listed
        in full."""
        out: list[dict] = []
        cursor = None
        for _ in range(50):
            if cap is not None and len(out) > cap:
                break
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self._get(c, f"/blocks/{parent_id}/children", params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Notion list children of {parent_id} -> {r.status_code}: "
                    f"{vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            for b in body.get("results") or []:
                if isinstance(b, dict) and b.get("type") == "child_page" and not _is_trashed(b):
                    out.append(
                        {
                            "id": str(b.get("id") or ""),
                            "title": (b.get("child_page") or {}).get("title") or "",
                            "last_edited_time": b.get("last_edited_time") or "",
                        }
                    )
                    if cap is not None and len(out) > cap:
                        break
            if body.get("has_more") and body.get("next_cursor"):
                cursor = body.get("next_cursor")
            else:
                break
        return out

    def _list_db_rows(self, c, ds_ids: list, cap: int | None) -> list[dict]:
        """The rows (pages) of every data source of the root database, via the data-source
        query endpoint (2025-09-03+); trashed rows are not returned by the API."""
        out: list[dict] = []
        for ds in ds_ids:
            cursor = None
            for _ in range(50):
                if cap is not None and len(out) > cap:
                    return out
                body = {"page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                r = self._query(c, f"/data_sources/{ds}/query", body)
                if r.status_code != 200:
                    raise ConnectorError(
                        f"Notion query of data source {ds} -> {r.status_code}: "
                        f"{vendor_excerpt(r.text)}"
                    )
                data = r.json() or {}
                for row in data.get("results") or []:
                    if (
                        isinstance(row, dict)
                        and row.get("object") == "page"
                        and not _is_trashed(row)
                    ):
                        out.append(
                            {
                                "id": str(row.get("id") or ""),
                                "title": _page_title(row),
                                "last_edited_time": row.get("last_edited_time") or "",
                            }
                        )
                if data.get("has_more") and data.get("next_cursor"):
                    cursor = data.get("next_cursor")
                else:
                    break
        return out

    def _list_sources(self, c, cap: int | None) -> list[dict]:
        """Every source page under the root: its child pages (down to `source_depth`
        levels), or the rows of a database root. Pages that are OUR translations (an id in
        the id map, or a title carrying one of our locale tags) are never sources."""
        kind, rid, ds = self._root(c)
        if kind == "database":
            rows = self._list_db_rows(c, ds, cap)
            return [r for r in rows if not self._is_ours(r)]
        out: list[dict] = []
        frontier = [rid]
        calls = 0
        for _level in range(self._source_depth()):
            nxt: list = []
            for pid in frontier:
                calls += 1
                if calls > _MAX_LIST_REQUESTS:
                    return out
                room = None if cap is None else max(cap - len(out), 0)
                for ch in self._list_child_pages(c, pid, room):
                    if self._is_ours(ch):
                        continue
                    out.append(ch)
                    nxt.append(ch["id"])
                    if cap is not None and len(out) > cap:
                        return out
            frontier = nxt
            if not frontier:
                break
        return out

    def _is_ours(self, page: dict) -> bool:
        pid = canon_id(str(page.get("id") or ""))
        return bool(
            (pid and pid in self._our_target_ids())
            or self._looks_translated(page.get("title") or "")
        )

    def _fetch_page(self, c, page_id: str) -> dict | None:
        """The page object trimmed to what discovery needs (None for a missing, unshared or
        trashed page)."""
        r = self._get(c, f"/pages/{page_id}")
        if r.status_code in (400, 404, 410):
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Notion read page {page_id} -> {r.status_code}: {vendor_excerpt(r.text)}"
            )
        body = r.json()
        if not isinstance(body, dict) or _is_trashed(body):
            return None
        return {
            "id": str(body.get("id") or page_id),
            "title": _page_title(body),
            "last_edited_time": body.get("last_edited_time") or "",
            "parent": body.get("parent") if isinstance(body.get("parent"), dict) else {},
        }

    def _in_scope(self, c, page: dict) -> bool:
        """Is this page one of the root's source pages? Walks the parent chain (at most
        `source_depth` hops for a page root; one hop for a database root). A page whose
        parent is a block, the workspace or anything else is out of scope: it fails closed."""
        kind, rid, ds = self._root(c)
        if self._is_ours(page):
            return False
        cur = page
        hops = 1 if kind == "database" else self._source_depth()
        for hop in range(hops):
            par = cur.get("parent") or {}
            ptype = par.get("type")
            if kind == "database":
                db = canon_id(str(par.get("database_id") or ""))
                dsid = canon_id(str(par.get("data_source_id") or ""))
                return bool(db == rid or (dsid and dsid in ds))
            if ptype != "page_id":
                return False
            parent_id = canon_id(str(par.get("page_id") or ""))
            if parent_id == rid:
                return True
            if not parent_id or hop + 1 >= hops:
                return False
            nxt = self._fetch_page(c, parent_id)
            if nxt is None:
                return False
            cur = nxt
        return False

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source pages to translate. With explicit article_ids (retry path) each id is
        validated, fetched and checked against the root's subtree: an id outside it answers
        exactly like a missing one. Otherwise the root's sources are listed (our own target
        pages are never re-discovered as sources)."""
        cap = fetch_cap(cap or self.config.get("max_pages"), 25)
        with self._client() as c:
            if article_ids:
                ids = []
                for raw in list(article_ids)[:cap]:
                    pid = canon_id(str(raw))
                    if not pid:
                        raise ConnectorError(f"Not a valid Notion page id: {str(raw)[:40]!r}.")
                    ids.append(pid)
                out = []
                for pid in ids:
                    pg = self._fetch_page(c, pid)
                    if pg is not None and self._in_scope(c, pg):
                        out.append({k: v for k, v in pg.items() if k != "parent"})
                return out
            return self._list_sources(c, cap)[:cap]

    def show_article(self, article_id: str) -> dict | None:
        pid = canon_id(str(article_id))
        if not pid:
            return None
        with self._client() as c:
            pg = self._fetch_page(c, pid)
            if pg is None or not self._in_scope(c, pg):
                return None
            return {k: v for k, v in pg.items() if k != "parent"}

    # --- extract: the block tree + units ---
    @staticmethod
    def _too_many_blocks(block_id: str) -> str:
        return (
            f"Notion page {block_id} has more than {_MAX_TREE_BLOCKS} blocks, the most one "
            "page may hold. It was not translated, so no half-read page is published."
        )

    def _fetch_blocks(self, c, block_id: str, budget: list, depth: int = 0) -> list:
        """Recursively read a block's children (populating nested `children`), so the
        walker sees the full tree. `budget` caps total blocks read per page."""
        if depth > _MAX_TREE_DEPTH:
            raise InputLimitError(
                f"Notion page nesting under {block_id} is deeper than {_MAX_TREE_DEPTH} levels."
            )
        out: list = []
        cursor = None
        for _ in range(50):
            if budget[0] <= 0:
                if len(budget) > 1:
                    # #579 gate: more blocks remain past the block budget. Stopping here would
                    # translate the page half read; refuse it whole.
                    raise InputLimitError(self._too_many_blocks(block_id))
                break
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self._get(c, f"/blocks/{block_id}/children", params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Notion read blocks of {block_id} -> {r.status_code}: {vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            for b in body.get("results") or []:
                if not isinstance(b, dict) or _is_trashed(b):
                    continue
                # Skip sub-pages entirely: a child_page/child_database block is a SEPARATE
                # page (our per-locale TARGET pages ARE child_page blocks of the source), not
                # translatable body. They are discovered via _list_child_pages, never walked.
                if b.get("type") in ("child_page", "child_database"):
                    continue
                budget[0] -= 1
                if len(budget) > 1 and budget[0] < 0:
                    raise InputLimitError(self._too_many_blocks(block_id))
                if len(budget) > 1:
                    # #579: bound the page's total size before the tree is buffered; a page
                    # over the limit is refused whole, never translated half read.
                    budget[1] -= item_chars(b)
                    if budget[1] < 0:
                        raise InputLimitError(
                            f"Notion page {block_id} is larger than {item_char_limit()} "
                            "characters (10 x MAX_SEGMENT_CHARS). It was not read into "
                            "memory and no translation was started for it."
                        )
                if b.get("has_children"):
                    kids = self._fetch_blocks(c, str(b.get("id")), budget, depth + 1)
                    if kids:
                        typ = b.get("type")
                        if isinstance(b.get(typ), dict):
                            b[typ]["children"] = kids
                out.append(b)
            if body.get("has_more") and body.get("next_cursor"):
                cursor = body.get("next_cursor")
            else:
                break
        return out

    def fetch_page_content(self, page_id: str) -> tuple[str, list, str]:
        """Return (title, block_tree, content_hash) for a source page: the input to the
        flow's prepare hook. content_hash covers the title + every translatable run so an
        unchanged page is suppressed and any edit re-translates."""
        pid = canon_id(str(page_id))
        if not pid:
            raise ConnectorError(f"Not a valid Notion page id: {str(page_id)[:40]!r}.")
        with self._client() as c:
            pg = self._fetch_page(c, pid)
            title = (pg or {}).get("title") or ""
            blocks = self._fetch_blocks(c, pid, [_MAX_TREE_BLOCKS, item_char_limit()])
        units = notion_blocks.extract(blocks).units
        chash = _content_hash(title, *[u.text for u in units])
        return title, blocks, chash

    def extract_units(self, page_id: str) -> tuple[list[dict], str]:
        title, blocks, chash = self.fetch_page_content(page_id)
        units = [{"p_index": "title", "text": title, "field": "title"}]
        for u in notion_blocks.extract(blocks).units:
            units.append({"p_index": u.p_index, "text": u.text, "field": "body"})
        return units, chash

    # --- write-back client (the T3DocClient the DocPerLocaleStrategy drives) ---
    def create_target(self, source_id: str, locale: str, fields: dict):
        """RESERVE: create the target page as a CHILD of the source page, titled per the
        pattern. The body is NOT written here (that is fill_target) so a crash right after
        leaves a findable, near-empty partial. Returns (page_id | None, error).

        Never creates while a unit is held (#640: a page made with a unit missing stays
        that way) or when every segment was refused (an untranslated clone wearing a
        translation label); the pair fails and retry_failed picks it up."""
        if not _has_translated_content(fields):
            return None, "all segments refused/flagged - page not created"
        if _held(fields):
            return None, "held-back: unit(s) awaiting review - page not created"
        sid = canon_id(str(source_id))
        if not sid:
            return None, "invalid source page id"
        try:  # a body Notion would refuse whole must not leave an empty page behind
            for b in (fields or {}).get("blocks") or []:
                _writeable_block(b, skipped=[])
        except BlockLimitError as e:
            return None, str(e)
        title = self._target_title(locale, (fields or {}).get("title") or "")
        payload = {
            "parent": {"page_id": sid},
            "properties": {"title": _title_property_value(title)},
        }
        try:
            with self._client() as c:
                r = self._post(c, "/pages", payload)
                if r.status_code in (200, 201):
                    pid = (r.json() or {}).get("id")
                    if pid:
                        self._fresh_ids.add(str(pid))
                    return (str(pid) if pid else None), (None if pid else "create returned no id")
                if r.status_code >= 500:
                    # the page may have been saved anyway (request-limits: "Writes that return
                    # 503"); adopt it only when the answer names it AND it sits under the source.
                    for cand in _error_ids(r, "committed_resource_id"):
                        pg = self._fetch_page(c, cand)
                        par = canon_id(str(((pg or {}).get("parent") or {}).get("page_id") or ""))
                        if pg is not None and par == sid:
                            self._fresh_ids.add(str(cand))
                            return cand, None
        except httpx.TransportError as e:
            return None, f"{type(e).__name__}: {e}"
        return None, f"create page -> {r.status_code}: {vendor_excerpt(r.text)}"

    # --- hand-edit guard (Notion has no draft state: every write is live) ---
    def _stored_print(self, tid: str) -> str:
        cid = getattr(self, "connection_id", None)
        if cid is None:
            return ""
        from app.services.connectors.store import get

        conn = get(cid)
        prints = (conn.config or {}).get("notion_target_prints") if conn else None
        v = prints.get(tid) if isinstance(prints, dict) else ""
        return v if isinstance(v, str) else ""

    def _store_print(self, tid: str, fp: str) -> None:
        cid = getattr(self, "connection_id", None)
        if cid is None:
            return
        from app.services.connectors import store

        store.patch_config_merge(cid, "notion_target_prints", {tid: fp})

    def _target_print(self, c, tid: str) -> str:
        """Fingerprint of what the target page holds NOW (title + body text, formatting,
        links, structure), read back from Notion, never computed from what we meant to
        write: whatever Notion normalised is normalised on both sides of the comparison."""
        pg = self._fetch_page(c, tid)
        if pg is None:
            raise ConnectorError(f"target page {tid} cannot be read")
        blocks = self._fetch_blocks(c, tid, [_MAX_TREE_BLOCKS])
        return _fingerprint(pg.get("title") or "", blocks)

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """Write the target (title + body), but never over a page a human changed.

        Notion has no draft or publish state: a write is live the moment it lands. After
        every complete write the page is read back and its fingerprint is stored
        (`notion_target_prints`). Before the next overwrite the page is read again; when it
        no longer matches, someone edited the translation in Notion, so it is NOT
        overwritten, the pair is reported as failed and PARKED for this source hash
        (`notion_edited_src`: later runs skip it without MT until the source changes or the
        page is trashed).

        WRITE-AHEAD: the seal is dropped BEFORE the first write, because from then on the
        page may hold old and new text together. A run killed midway therefore leaves an
        unsealed page that the next run rewrites whole (it converges), instead of a mixed
        page mistaken for a hand edit and left that way. When the write fails and the page
        reads back exactly as sealed (nothing landed, or the rollback undid it), the seal is
        restored."""
        tid = str(target_id)
        if not _has_translated_content(fields):
            return self._fill_target(tid, fields)
        expected = self._stored_print(tid)
        if expected:
            try:
                with self._client() as c:
                    now = self._target_print(c, tid)
            except (ConnectorError, InputLimitError, httpx.TransportError) as e:
                return WriteResult(
                    False,
                    False,
                    target_id=tid,
                    message=f"could not read the translation before updating it ({e}); "
                    "nothing was changed",
                )
            if now != expected:
                self._park(tid, (fields or {}).get("_src_hash") or "")
                return WriteResult(
                    False,
                    False,
                    target_id=tid,
                    message="edited-in-notion: this translation page was changed in Notion "
                    "after the last write, so it was NOT overwritten. Review it; to have it "
                    "re-created, move it to the Notion trash (your edited copy stays there). "
                    "Until then later runs skip it without translating it again, unless the "
                    "source page changes.",
                )
            try:
                self._store_print(tid, "")  # write-ahead: the page is about to be partly ours
            except Exception as e:
                return WriteResult(
                    False,
                    False,
                    target_id=tid,
                    message=f"could not record the write intent ({type(e).__name__}); "
                    "nothing was changed",
                )
        self._fill_mutated = False
        res = self._fill_target(tid, fields)
        try:
            if res.ok:
                with self._client() as c:
                    self._store_print(tid, self._target_print(c, tid))
                self._park(tid, "")
            elif expected:
                with self._client() as c:
                    if not self._fill_mutated or self._target_print(c, tid) == expected:
                        self._store_print(tid, expected)  # nothing visible changed: re-seal
        except Exception:
            # the page stays unsealed: the next run rewrites it whole (never a mixed page
            # left in place), which is the safe direction for a page that is partly ours.
            log.warning("notion: could not record the fingerprint of %s", tid)
        return res

    def _park(self, tid: str, src_hash: str) -> None:
        """Record (or clear, with "") the source hash a hand-edited target is parked for."""
        cid = getattr(self, "connection_id", None)
        if cid is None:
            return
        from app.services.connectors import store

        conn = store.get(cid)
        cur = ((conn.config or {}).get("notion_edited_src") or {}) if conn else {}
        if (cur.get(tid) if isinstance(cur, dict) else "") == src_hash:
            return
        try:
            store.patch_config_merge(cid, "notion_edited_src", {tid: src_hash})
        except Exception:
            log.warning("notion: could not record the parked state of %s", tid)

    def is_parked(self, source_id: str, locale: str, chash: str) -> bool:
        """flow `parked` hook: True while the pair's target was found edited in Notion for
        exactly this source hash and the page still exists. A scheduled run then skips it
        without MT or writes (no paid loop over a page we will refuse to overwrite). A
        trashed page or a changed source un-parks it; an unverifiable page stays parked."""
        cid = getattr(self, "connection_id", None)
        if cid is None or not chash:
            return False
        from app.services.connectors import idmap, store

        row = idmap.get(cid, str(source_id), locale)
        if not row or row.status != idmap.DONE or not row.target_id:
            return False
        conn = store.get(cid)
        parked = ((conn.config or {}).get("notion_edited_src") or {}) if conn else {}
        if not isinstance(parked, dict) or parked.get(str(row.target_id)) != chash:
            return False
        try:
            return bool(self.verify_target(str(row.target_id)))
        except Exception:
            return True  # cannot tell: keep it parked (nothing written, nothing spent)

    def _fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """Write the target's body + title. WRITE-THEN-SWAP (so a resume/update is
        idempotent and the old body survives a refused append, #466), appending the
        translated block tree ONE level at a time under each created parent. Returns a
        per-pair WriteResult (never an escaping exception).

        When every segment was refused, nothing is touched (an existing translation is never
        replaced with source text). #640: a held title is not rewritten, and when ANY body
        unit is held the stored body stays exactly as it is."""
        if not _has_translated_content(fields):
            return WriteResult(
                False,
                False,
                target_id=str(target_id),
                message="all segments refused/flagged - page not created",
            )
        loc = (fields or {}).get("locale") or ""
        title = self._target_title(loc, (fields or {}).get("title") or "")
        blocks = (fields or {}).get("blocks") or []
        tid = str(target_id)
        skipped: list = []
        title_held = bool((fields or {}).get("_held_title"))
        body_held = bool((fields or {}).get("_held_body"))
        try:
            with self._client() as c:
                if not body_held:
                    # #466: WRITE-THEN-SWAP. Notion can refuse an append (a block shape it will
                    # not create), so the existing body is never cleared before the new body is
                    # accepted: remember the current body, append the new tree after it, and
                    # only on success trash the remembered blocks. A refused append rolls back
                    # what it created and leaves the old body (or the empty new page) as it was.
                    old_ids, err = self._child_block_ids(c, tid)
                    if err:
                        return WriteResult(False, False, target_id=tid, message=err)
                    created_top: list = []
                    self._fill_mutated = True
                    try:
                        err = self._append_tree(
                            c,
                            tid,
                            blocks,
                            [_MAX_TREE_BLOCKS],
                            created_top=created_top,
                            skipped=skipped,
                        )
                    except httpx.TransportError as e:
                        # a network drop mid-sequence: the first batches may already be on the
                        # page, so roll them back before reporting, exactly like a refusal.
                        err = f"{type(e).__name__}: {e}"
                    if err:
                        self._rollback_blocks(c, created_top)
                        if not old_ids and tid in self._fresh_ids:
                            # a page created this run with no body yet: never leave it live
                            # and empty. Trashed, the strategy recreates it next run (rule 5).
                            err += self._trash_fresh(c, tid)
                        return WriteResult(False, False, target_id=tid, message=err)
                    for n, bid in enumerate(old_ids):
                        try:
                            dr = self._delete(c, f"/blocks/{bid}")
                            failed = dr.status_code not in (200, 201, 404, 410)
                            why = f"replace body: trash old block -> {dr.status_code}"
                        except httpx.TransportError as e:
                            failed, why = (
                                True,
                                f"replace body: trash old block: {type(e).__name__}",
                            )
                        if failed:
                            if n == 0:
                                # nothing of the old body is gone yet: undo the new one so the
                                # page still shows exactly one body.
                                self._rollback_blocks(c, created_top)
                            else:
                                why += (
                                    f" ({n} of {len(old_ids)} old blocks already trashed; the "
                                    "page shows the old and the new text together until the "
                                    "next run rewrites it)"
                                )
                            return WriteResult(False, False, target_id=tid, message=why)
                if not title_held:
                    # title last (idempotent replace-in-place by id), once the body landed
                    self._fill_mutated = True
                    tr = self._patch(
                        c,
                        f"/pages/{tid}",
                        {"properties": {"title": _title_property_value(title)}},
                    )
                    if tr.status_code not in (200, 201):
                        return WriteResult(
                            False,
                            False,
                            target_id=tid,
                            message=f"title update -> {tr.status_code}: {vendor_excerpt(tr.text)}",
                        )
        except httpx.TransportError as e:
            return WriteResult(False, False, target_id=tid, message=f"{type(e).__name__}: {e}")
        notes = []
        dropped = [x for x in skipped if not x.startswith("mention:")]
        mentions = [x[8:] for x in skipped if x.startswith("mention:")]
        if dropped:
            kinds = sorted(set(dropped))
            notes.append(f"{len(dropped)} block(s) not copied ({', '.join(kinds)})")
        if mentions:
            kinds = ", ".join(sorted(set(mentions)))
            notes.append(f"{len(mentions)} mention(s) kept as plain text ({kinds})")
        for n in notes:
            self.write_notes.append(f"{loc or tid}: {n}")
        if body_held or title_held:
            notes.append("held unit(s) kept as stored")
        return WriteResult(
            True,
            False,
            target_id=tid,
            message="filled" + ("; " + "; ".join(notes) if notes else ""),
        )

    def _child_block_ids(self, c, page_id: str) -> tuple[list, str | None]:
        """The ids of a page's current child blocks (cursor-paginated, bounded). Returns
        (ids, error): an unreadable body is an ERROR, never "empty", so a fill that cannot
        see the old body does not append beside it. Child pages are not body: they are never
        returned (the fill must not trash a page that lives under the target)."""
        ids: list = []
        cursor = None
        for _ in range(50):
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self._get(c, f"/blocks/{page_id}/children", params=params)
            if r.status_code != 200:
                return [], f"read current body -> {r.status_code}: {vendor_excerpt(r.text)}"
            body = r.json() or {}
            for b in body.get("results") or []:
                if not isinstance(b, dict) or b.get("type") in ("child_page", "child_database"):
                    continue
                if b.get("id"):
                    ids.append(str(b["id"]))
            if body.get("has_more") and body.get("next_cursor"):
                cursor = body.get("next_cursor")
            else:
                break
        return ids, None

    def _trash_fresh(self, c, tid: str) -> str:
        try:
            r = self._patch(c, f"/pages/{tid}", {"in_trash": True})
            if r.status_code in (200, 201):
                self._fresh_ids.discard(tid)
                return "; the new, still empty page was moved to the trash"
        except httpx.TransportError:
            pass
        log.warning("notion: could not trash the empty new page %s", tid)
        return "; the new page is still empty (it could not be moved to the trash)"

    def _rollback_blocks(self, c, block_ids: list) -> None:
        """Best-effort trash of the blocks a refused append already created (their nested
        children go with them). A leftover is swept by the next fill, which snapshots the
        whole body as "old" before it writes."""
        for bid in block_ids:
            try:
                self._delete(c, f"/blocks/{bid}")
            except Exception:  # rollback must never mask the real error
                log.warning("notion: rollback of block %s failed", bid)

    def _append_tree(
        self,
        c,
        parent_id: str,
        blocks: list,
        budget: list,
        *,
        created_top: list | None = None,
        skipped: list | None = None,
    ) -> str | None:
        """Append `blocks` under `parent_id`, ONE nesting level per request, except that
        table / column_list / column blocks carry their children INLINE (Notion refuses to
        create them any other way, #466). `created_top` collects the ids this call created at
        the top level so the caller can roll back. Returns an error string or None."""
        if not isinstance(blocks, list) or not blocks:
            return None
        payloads, child_lists = [], []
        for b in blocks:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            try:
                payload, kids = _writeable_block(b, skipped=skipped)
            except BlockLimitError as e:
                return str(e)
            if payload is None:
                continue
            payloads.append(payload)
            child_lists.append(kids)
        created_ids: list = []
        for batch in _batches(payloads):
            r = self._patch(
                c, f"/blocks/{parent_id}/children", {"children": batch}, idempotent=False
            )  # append is non-idempotent
            if r.status_code not in (200, 201):
                if r.status_code >= 500 and created_top is not None:
                    # the blocks may have been saved anyway: roll those back too.
                    created_top.extend(_error_ids(r, "committed_child_ids"))
                return f"append blocks -> {r.status_code}: {vendor_excerpt(r.text)}"
            new_ids = [
                str(x.get("id"))
                for x in (r.json() or {}).get("results") or []
                if isinstance(x, dict) and x.get("id")
            ]
            created_ids.extend(new_ids)
            if created_top is not None:
                created_top.extend(new_ids)
        # recurse into children under each created block. The nested blocks live under a
        # created top-level block, so a rollback of that block removes them too.
        for created_id, kids in zip(created_ids, child_lists, strict=False):
            if kids and created_id:
                err = self._append_tree(c, created_id, kids, budget, skipped=skipped)
                if err:
                    return err
        return None

    def verify_target(self, target_id: str) -> bool:
        """Does the target page STILL EXIST (not deleted / in the trash)? A fetch of the id
        (never a title/content match: SPEC-t3 reconciliation rule)."""
        with self._client() as c:
            r = self._get(c, f"/pages/{target_id}")
        if r.status_code in (404, 410):
            return False  # the only definite "gone" answers
        if r.status_code != 200:
            # Issue #444 (same class): 503 / 429 / 401 never means "deleted"; hold the pair.
            raise TargetUnverifiable(f"HTTP {r.status_code} verifying {target_id}")
        return not _is_trashed(r.json() or {})

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        """RULE-6 collision probe: is there ALREADY a child page of the source titled like
        our target ('[loc] title') that our map never created? Returns its id (the strategy
        then fails closed with needs_link) or None.

        Exact-title match is the primary probe. A crash between POST /pages and the id-map
        reserve leaves an orphan page with NO map row; if the computed title then drifts on a
        later run (a title edit, non-deterministic MT) the exact probe misses it and a SECOND
        child page would be minted. So after an exact-title miss, any child whose title
        carries OUR locale's bracketed tag ("[{loc}] ") also counts, whatever follows the
        tag. The tag match is exact (case-insensitive), so "[de]" never collides with a
        "[de-de]" target."""
        want = self._target_title(locale, (fields or {}).get("title") or "")
        try:
            with self._client() as c:
                children = self._list_child_pages(c, str(source_id))
        except ConnectorError:
            return None
        want_norm = want.strip()
        for ch in children:
            if (ch.get("title") or "").strip() == want_norm:
                return ch.get("id")
        loc_norm = (locale or "").strip().lower()
        for ch in children:
            title = (ch.get("title") or "").strip()
            m = _LOCALE_TAG_RE.match(title)
            if m and m.group(1).strip().lower() == loc_norm:
                return ch.get("id")
        return None

    # --- generic Connector surface (pull/push) ---
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (page, unit) for the generic pull view. The rich flow is
        store.notion_translate (creates per-locale child pages)."""
        out: list[PulledItem] = []
        self._reset_fetch_budget()
        # #587: pages that could not be read are listed (id, title, reason) so the pull door
        # can tell the operator, instead of leaving them out of the document unannounced.
        self.skipped_pages = []
        for pg in self.discover_articles():
            try:
                units, _h = self.extract_units(str(pg.get("id")))
            except ConnectorError as e:
                if isinstance(e, FetchBudgetError):
                    raise
                self.skipped_pages.append(
                    {
                        "id": str(pg.get("id") or ""),
                        "title": pg.get("title") or "",
                        "error": str(e)[:200],
                    }
                )
                continue
            # #587: every page's text stays in `out` until the document is stored.
            self._charge_fetch(sum(len(u["text"]) for u in units))
            out.extend(self._pulled_items(units, str(pg.get("id")), pg.get("title") or ""))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Notion creates a per-locale child page per source page - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list the root's source pages for the caller to
        diff against the persisted per-(page,locale) content hash. Returns (pages, 0):
        last_edited_time is metadata-tainted (bumps on any edit), so the hash in
        notion_translate is the actual gate; NO watermark is advanced."""
        return self.discover_articles(), 0


def _payload_weight(p) -> tuple:
    """(block elements, bytes) of one block payload including its inline children."""
    n = 1
    typ = p.get(p.get("type")) if isinstance(p, dict) else None
    for ch in (typ or {}).get("children") or []:
        n += _payload_weight(ch)[0]
    return n, len(json.dumps(p, ensure_ascii=False))


def _batches(payloads: list):
    """Split payloads into append requests of at most 100 top-level blocks, 900 block
    elements (inline children count) and 400 KB (request-limits: 100 / 1000 / 500 KB)."""
    batch: list = []
    blocks_n = size = 0
    for p in payloads:
        n, nbytes = _payload_weight(p)
        if batch and (
            len(batch) >= _APPEND_BATCH
            or blocks_n + n > _MAX_PAYLOAD_BLOCKS
            or size + nbytes > _MAX_PAYLOAD_BYTES
        ):
            yield batch
            batch, blocks_n, size = [], 0, 0
        batch.append(p)
        blocks_n += n
        size += nbytes
    if batch:
        yield batch


# Block types the API can create (reference/patch-block-children, "Block types you can
# create", checked 2026-10-10). Anything else read from a page is converted or left out and
# reported; it is never sent (the API would refuse the whole append).
_CREATABLE_TYPES = frozenset(
    {
        "embed", "bookmark", "image", "video", "pdf", "file", "audio", "code", "equation",
        "divider", "breadcrumb", "tab", "table_of_contents", "link_to_page", "table",
        "table_row", "column_list", "column", "heading_1", "heading_2", "heading_3",
        "heading_4", "paragraph", "bulleted_list_item", "numbered_list_item", "quote",
        "to_do", "toggle", "callout", "synced_block",
    }
)  # fmt: skip
# Blocks Notion only accepts with their children inline in the create request (#466).
_INLINE_CHILD_TYPES = frozenset({"table", "column_list", "column"})
# Blocks that can hold an uploaded (Notion-hosted) file; the API refuses to create one.
_FILE_BLOCK_TYPES = frozenset({"image", "video", "pdf", "file", "audio"})
_RICH_ARRAY_KEYS = ("rich_text", "caption")
_MAX_ARRAY = 100  # request-limits: arrays of blocks / rich text, 100 elements
_MAX_PAYLOAD_BLOCKS = 900  # request-limits: 1000 block elements per request (margin kept)
_MAX_PAYLOAD_BYTES = 400_000  # request-limits: 500 KB per request (margin kept)


def _fp_value(v, *, in_run: bool = False):
    """A block / run value with only what a reader sees and a human can change: ids of
    the block itself, timestamps, authors, trash flags and signed file URLs are left out;
    a user object is reduced to its id; a mention keeps its target, not the text Notion
    renders for it NOW (a renamed page or user is not a hand edit of our translation)."""
    if isinstance(v, list):
        return [_fp_value(x, in_run=in_run) for x in v]
    if not isinstance(v, dict):
        return v
    if v.get("object") == "user":
        return {"user": str(v.get("id") or "")}
    out = {}
    for k, x in v.items():
        if k in _FP_VOLATILE:
            continue
        if k == "url" and "expiry_time" in v:
            continue  # a Notion-hosted file's signed URL changes on every read
        if in_run and v.get("type") == "mention" and k in ("plain_text", "href"):
            continue
        out[k] = _fp_value(x, in_run=in_run or k in _RICH_ARRAY_KEYS or k == "cells")
    return out


_FP_VOLATILE = frozenset(
    {
        "created_time", "last_edited_time", "created_by", "last_edited_by",
        "has_children", "parent", "archived", "in_trash", "is_archived", "request_id",
        "expiry_time",
    }
)  # fmt: skip


def _fp_blocks(blocks) -> list:
    out = []
    for b in blocks if isinstance(blocks, list) else []:
        t = b.get("type") if isinstance(b, dict) else None
        typ = dict(b.get(t)) if t and isinstance(b.get(t), dict) else {}
        kids = typ.pop("children", None)
        out.append([t, _fp_value(typ), _fp_blocks(kids)])
    return out


def _fingerprint(title: str, blocks) -> str:
    """Stable digest of a page's title + body: every block type, its text, annotations,
    links, colours, settings (code language, checked, url, ...), mention targets and the
    order and nesting of blocks. Ids, timestamps, signed file URLs and the live rendering
    of mentions are left out, so only an edit to the page itself changes it."""
    raw = json.dumps([title, _fp_blocks(blocks)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hosted_file(block: dict, ntype: str) -> bool:
    """A Notion-hosted file (`type: "file"` / `file_upload`) cannot be created through the
    API and its URL is a short-lived signed link, so it is not copied (#466)."""
    if ntype not in _FILE_BLOCK_TYPES:
        return False
    typ = block.get(ntype)
    return isinstance(typ, dict) and typ.get("type") in ("file", "file_upload")


def _clean_run(el, notes: list | None = None):
    """A rich_text run reduced to its WRITE shape: type, the type's own object and the
    annotations. Response-only fields (plain_text, href, a user mention's full profile) are
    dropped; the text, its annotations and its link are kept exactly. A mention the API
    cannot be given back (link_mention, template_mention, unknown) is kept as plain text
    with its link, so no words are lost."""
    if not isinstance(el, dict):
        return None
    t = el.get("type")
    ann = el.get("annotations") if isinstance(el.get("annotations"), dict) else None
    out: dict = {}
    if t == "text" and isinstance((el.get("text") or {}).get("content"), str):
        tx = el["text"]
        text = {"content": tx["content"]}
        link = tx.get("link")
        if isinstance(link, dict) and isinstance(link.get("url"), str) and link["url"]:
            text["link"] = {"url": link["url"]}
        out = {"type": "text", "text": text}
    elif t == "equation" and isinstance((el.get("equation") or {}).get("expression"), str):
        out = {"type": "equation", "equation": {"expression": el["equation"]["expression"]}}
    elif t == "mention" and isinstance(el.get("mention"), dict):
        m = el["mention"]
        mt = m.get("type")
        body = None
        if mt in ("page", "database", "user") and isinstance(m.get(mt), dict) and m[mt].get("id"):
            body = {"type": mt, mt: {"id": m[mt]["id"]}}
        elif mt == "date" and isinstance(m.get("date"), dict) and m["date"].get("start"):
            body = {"type": "date", "date": dict(m["date"])}
        elif (
            mt == "link_preview"
            and isinstance(m.get("link_preview"), dict)
            and m["link_preview"].get("url")
        ):
            body = {"type": "link_preview", "link_preview": {"url": m["link_preview"]["url"]}}
        if body is not None:
            out = {"type": "mention", "mention": body}
    if not out:
        # unknown run shape: keep the visible words (and the link) as text, and SAY so
        # (a mention that becomes plain text is a change readers can see)
        if t == "mention" and notes is not None:
            mt = (
                (el.get("mention") or {}).get("type") if isinstance(el.get("mention"), dict) else ""
            )
            notes.append(f"mention:{mt or 'unknown'}")
        plain = el.get("plain_text")
        if not isinstance(plain, str) or not plain:
            return None
        text = {"content": plain[:2000]}
        if isinstance(el.get("href"), str) and el["href"]:
            text["link"] = {"url": el["href"]}
        out = {"type": "text", "text": text}
    if ann is not None:
        out["annotations"] = ann
    return out


class BlockLimitError(ValueError):
    """A block the API would refuse whole (request-limits: 100 elements per array)."""


def _clean_rich_arrays(typ: dict, notes: list | None = None) -> None:
    for key in _RICH_ARRAY_KEYS:
        arr = typ.get(key)
        if isinstance(arr, list):
            typ[key] = [r for r in (_clean_run(x, notes) for x in arr) if r is not None]
            if len(typ[key]) > _MAX_ARRAY:
                raise BlockLimitError(
                    f"a block has {len(typ[key])} rich-text runs; Notion accepts at most "
                    f"{_MAX_ARRAY} per block, so the page was not written"
                )
    cells = typ.get("cells")
    if isinstance(cells, list):
        typ["cells"] = [
            [r for r in (_clean_run(x, notes) for x in cell) if r is not None]
            if isinstance(cell, list)
            else []
            for cell in cells
        ]
        if any(len(cell) > _MAX_ARRAY for cell in typ["cells"]):
            raise BlockLimitError(
                f"a table cell has more than {_MAX_ARRAY} rich-text runs; Notion accepts at "
                "most that many, so the page was not written"
            )


def _writeable_block(block, *, skipped: list | None = None, inline: bool = False) -> tuple:
    """Strip a read Notion block down to the write shape and split off its children.
    Returns (payload | None, children_list_to_append_later). Drops read-only fields
    (id, timestamps, has_children, in_trash, parent, ...); keeps the block type and its
    type-object (rich_text, color, language, is_toggleable, ...), with the rich text reduced
    to its write shape (_clean_run).

    A block type the API cannot create is converted (link_preview -> bookmark) or left out
    and its type name appended to `skipped`. A synced-block duplicate is written as an
    original holding the translated content (a duplicate cannot hold its own text, and
    pointing it back at the source block would show source-language text).

    table / column_list / column (and everything beneath them) keep their children inline in
    the payload, because Notion rejects them created empty and filled later."""
    if not isinstance(block, dict):
        return None, []
    ntype = block.get("type")
    typ = block.get(ntype)
    if not ntype or not isinstance(typ, dict):
        return None, []
    if ntype == "link_preview" and typ.get("url"):
        return {"type": "bookmark", "bookmark": {"url": typ["url"]}}, []
    if ntype not in _CREATABLE_TYPES or _is_hosted_file(block, ntype):
        if skipped is not None:
            skipped.append(ntype)
        return None, []
    typ = dict(typ)
    children = typ.pop("children", None)
    kids = children if isinstance(children, list) else []
    _clean_rich_arrays(typ, skipped)
    icon = typ.get("icon")
    if isinstance(icon, dict) and icon.get("type") in ("file", "file_upload"):
        typ.pop("icon", None)  # a hosted icon is a short-lived signed link
    if ntype == "synced_block":
        typ["synced_from"] = None
    if ntype in _INLINE_CHILD_TYPES or inline:
        inline_kids = []
        for k in kids:
            kp, _ = _writeable_block(k, skipped=skipped, inline=True)
            if kp is not None:
                inline_kids.append(kp)
        rest: list = []
        if len(inline_kids) > _MAX_ARRAY:
            if ntype != "table":
                raise BlockLimitError(
                    f"a {ntype} holds {len(inline_kids)} blocks; Notion accepts at most "
                    f"{_MAX_ARRAY} per array, so the page was not written"
                )
            # a long table: the first 100 rows go inline, the rest are appended to the table
            inline_kids, rest = inline_kids[:_MAX_ARRAY], inline_kids[_MAX_ARRAY:]
            if inline:
                raise BlockLimitError(
                    "a table inside a column holds more than 100 rows; Notion accepts at "
                    "most 100 per array, so the page was not written"
                )
        if inline_kids:
            typ["children"] = inline_kids
        elif ntype in _INLINE_CHILD_TYPES:
            return None, []  # an empty table / column would be refused: drop it
        return {"type": ntype, ntype: typ}, rest
    return {"type": ntype, ntype: typ}, kids


# ===========================================================================
# Translate flow (hooks) for the shared engine. store.py keeps a thin delegator so
# store.notion_translate stays importable/monkeypatchable (jobs/CLI/dispatch + suites).
# ===========================================================================


# ------------------------- Notion (T3) hooks -------------------------------
# Write-back rides the flow engine via the DocPerLocaleStrategy: write_target does
# reserve-then-fill + the 7-case reconciliation table against the id map. These hooks only
# prepare the block-tree extraction and assemble the translated-blocks payload. Poll-first,
# so NO watermark (last_edited_time is metadata-tainted: the content hash is the gate).
def _nt_preflight(ctx):
    # enabled = every target locale EXCEPT the source (Notion has no per-workspace
    # enabled-locale API; any locale is writable as a new child page). The default
    # partition then rejects the source locale (never translate a page into its own
    # language: it would duplicate the source).
    src = (ctx.cfg.get("source_locale") or "en").strip().lower()
    enabled = {x for x in ctx.locales if x and x != src}
    return enabled, {}


def _nt_prepare(ctx, art):
    aid = str(art.get("id") or "")
    try:
        title, blocks, chash = ctx.connector.fetch_page_content(aid)
    except Exception as e:
        return flow.SkipArticle("unreadable_skipped", error=f"{aid}: {type(e).__name__}: {e}")
    units = notion_blocks.extract(blocks).units
    if not (title or "").strip() and not units:
        # Nothing translatable at all -> skip without creating an empty page.
        return flow.SkipArticle("empty_skipped")
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title,
        body_units=units,
        extra_skeleton={"page_id": aid, "title": title},
        doc_title=f"{(title or 'page')[:50]} ({aid})",
        extra={"blocks": blocks, "source_title": title},
    )


def _nt_is_suppressed(connector, aid, loc):
    # Reads the MAP (a `done` id-map row), never content (SPEC-t3 invariant). A
    # building/needs_link/orphaned row is NOT 'done', so it falls through to write_target
    # and the reconciliation table resumes / fails closed / recreates.
    return connector.strategy.exists_target(connector, aid, loc)


def _nt_build_payload(prep, loc, seg0_target, mapping):
    # Title: the NEEDS_REVIEW-excluded seg0 yields None -> fall back to the SOURCE title only
    # as a placeholder; `_held_title` tells the connector never to write it (#640).
    title = seg0_target if seg0_target is not None else prep.extra["source_title"]
    translated = notion_blocks.reinsert(prep.extra["blocks"], mapping)
    # Structure guard: reinsert deep-clones and only rewrites run text, so this always holds;
    # if a mutation ever broke it, keep the SOURCE tree rather than a structurally corrupt one.
    if not notion_blocks.structure_ok(prep.extra["blocks"], translated):
        translated = prep.extra["blocks"]
    title_held = seg0_target is None and bool((prep.title_text or "").strip())
    body_held = any(
        str(u.p_index) not in mapping and (u.text or "").strip() for u in prep.body_units
    )
    # seg0 None AND an empty mapping: the ENTIRE translation collapsed, so `title` and
    # `translated` are just the source content wearing the "[loc]" label.
    no_translation = seg0_target is None and not mapping
    return {
        "title": title,
        "blocks": translated,
        "locale": loc,
        "_no_translation": no_translation,
        "_held_title": title_held,
        "_held_body": body_held,
        "_src_hash": prep.chash,  # a hand-edited target is parked for exactly this source
    }


def _nt_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not ctx.failures)
        or (not ctx.failures and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _nt_build_message(ctx):
    from app.services.connectors import idmap

    parts = [
        f"Created {ctx.created}, updated {ctx.updated} localized page(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} page(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) page(s).")
    if ctx.created or ctx.updated:
        parts.append("Notion has no draft state: written pages are live now.")
    notes = list(getattr(ctx.connector, "write_notes", []) or [])
    if notes:
        parts.append("Not carried over: " + "; ".join(notes[:5]) + ".")
    edited = _nt_edited_count(ctx)
    if edited:
        parts.append(
            f"{edited} translation page(s) were edited in Notion and NOT overwritten "
            "(move one to the Notion trash to have it re-created)."
        )
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} empty page(s).")
    if ctx.counters.get("unreadable_skipped"):
        parts.append(f"Skipped {ctx.counters['unreadable_skipped']} unreadable page(s).")
    needs_link = idmap.status_counts(ctx.cid).get("needs_link", 0)
    if needs_link:
        parts.append(
            f"{needs_link} page(s) need a link decision (a pre-existing "
            f"translation was found: adopt or ignore it)."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) (source locale is never "
            f"a target): {', '.join(ctx.rejected)}."
        )
    if ctx.errors:
        parts.append("Errors: " + "; ".join(ctx.errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("page write(s)").strip())
        log.warning(
            "notion_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _nt_edited_count(ctx) -> int:
    fresh = [f for f in ctx.failures if "edited-in-notion" in str(f.get("error"))]
    return len(fresh) + ctx.counters.get("parked_skipped", 0)


def _nt_parked(connector, aid, loc, chash):
    return connector.is_parked(aid, loc, chash)


def _nt_build_result(ctx):
    from app.services.connectors import idmap

    return {
        "pages": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "unreadable_skipped": ctx.counters.get("unreadable_skipped", 0),
        "rejected": ctx.rejected,
        "failures": ctx.failures,
        "not_carried_over": list(getattr(ctx.connector, "write_notes", []) or []),
        "edited_in_notion": _nt_edited_count(ctx),
        "id_map": idmap.status_counts(ctx.cid),
    }


def notion_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Notion pages into one or more locales, each written as a per-locale
    CHILD page of the source via the T3 DocPerLocaleStrategy (reserve-then-fill + id-map
    reconciliation, so the source page and other locales are never overwritten and a
    partial build resumes rather than duplicating).

    Thin wrapper over flow.run_translate_flow. Notion-specific behavior via hooks:
    block-tree extraction (notion_blocks) and translated-blocks assembly, source-locale
    rejection, content-hash suppression (a MISSING/partial localized page is always
    (re)built), rule-6 needs_link surfacing and the #640 held-back payload flags.
    Poll-first, so NO watermark. only_pairs restricts the write loop to those
    (page_id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="notion",
        no_conn_msg="No such Notion connection.",
        hash_key="notion_hashes",
        probe_label="Notion",
        preflight=_nt_preflight,
        prepare=_nt_prepare,
        is_suppressed=_nt_is_suppressed,
        build_payload=_nt_build_payload,
        compute_draft=lambda cfg: True,  # Notion has no draft/publish; unused by the T3 write
        compute_ok=_nt_compute_ok,
        build_message=_nt_build_message,
        build_result=_nt_build_result,
        extra_counters=("empty_skipped", "unreadable_skipped", "parked_skipped"),
        parked=_nt_parked,
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


def notion_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Operator resolution of a Notion needs_link row (SPEC-t3 rule 6 adopt-or-ignore):
    a pre-existing target page the id map never created. `action` is 'adopt' (accept
    it as ours -> the next run UPDATES it) or 'ignore' (drop the row -> it re-flags).
    The one seam web + CLI + /api all call. Never overwrites content: it only edits
    the id map."""
    from app.services.connectors import idmap
    from app.services.connectors.store import _log, get

    conn = get(cid)
    if not conn or conn.kind != "notion":
        return False, "No such Notion connection.", {}
    ok, msg = idmap.resolve_needs_link(cid, str(source_id), target_locale, action)
    _log(cid, "resolve-link", ok, 0, msg, actor)
    return ok, msg, {"id_map": idmap.status_counts(cid)}
