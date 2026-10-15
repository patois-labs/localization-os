"""strapi: part2

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

from urllib.parse import quote

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
from app.services.connectors.strapi_field_classification_heu import (
    _ALIAS_PREFIX,
    _HELD_BACK,
    _RESERVED_WRITE_KEYS,
    _SUPPORTED_VERSIONS,
    _SYSTEM_FIELDS,
    _TITLE_FALLBACK_KEY,
    API_VERSION,
    _classify_field,
    _parse_names,
    _Unit,
    log,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt
from app.services.vendor_dispatch_common import _endpoint_host

SPEC = ConnectorSpec(
    kind="strapi",
    name="Strapi",
    icon="🧩",
    cat="Headless CMS",
    blurb="Translate Strapi collection-type entries into any language using the i18n "
    "plugin. Each translation is written back as the entry's per-locale variant "
    "(via documentId on v5, or a localization on v4), so the source entry is never "
    "overwritten.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Base URL",
            "Your Strapi site root, e.g. "
            "https://cms.acme.com. Must be a publicly reachable host — loopback/"
            "internal targets (localhost, .internal/.local, private IPs) are refused "
            "by the SSRF guard. The connector appends /api.",
            placeholder="https://cms.acme.com",
        ),
        FieldSpec(
            "content_types",
            "Content type IDs",
            "Comma-separated plural API IDs of "
            "the collection types to translate — the plural form ending in 's' "
            "(e.g. 'articles, pages'), NOT the singular. May not contain ':' '/' "
            "'?' '#'.",
            placeholder="articles, pages",
        ),
        FieldSpec(
            "strapi_version",
            "Strapi version",
            "'v5' (default) or 'v4'. v5 keys "
            "locales by documentId and writes to the entry's DRAFT; v4 uses per-locale "
            "localizations and PUT updates the LIVE entry (no document draft).",
            required=False,
            placeholder="v5",
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "Optional allowlist of field names to "
            "translate (comma-separated). Blank = auto-discover text fields with "
            "conservative heuristics (slug/uid/email/url/date/enum values are "
            "excluded; the run report lists what was selected and skipped).",
            required=False,
            placeholder="title, body, description",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude fields",
            "Field names to NEVER translate "
            "(comma-separated). Applies only when the allowlist above is blank.",
            required=False,
            placeholder="slug, seoTitle",
        ),
        FieldSpec(
            "title_field",
            "Title field",
            "Field to use as the entry's display title (default: 'title' or 'name' when present).",
            required=False,
            placeholder="title",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The authoring locale to translate FROM (default: the i18n default locale).",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Strapi locale codes to "
            "create (must be enabled in Settings -> Internationalization).",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "max_entries",
            "Max entries",
            "Safety cap on entries scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_token",
            "API token",
            "Settings -> API Tokens -> Create new API Token "
            "with Read + Write (Full access or a custom role covering the content "
            "types). Sent as a Bearer token.",
            secret=True,
            placeholder="a long token",
        ),
    ],
    setup_steps=[
        "In Strapi, install/enable the Internationalization (i18n) plugin and add every "
        "target locale under Settings -> Internationalization.",
        "Enable Localization on each collection type you want to translate (Content-Type "
        "Builder -> Edit -> Advanced settings -> Enable localization).",
        "Create an API token (Settings -> API Tokens) with Read + Write access to those "
        "content types.",
        "Enter the Base URL (your site root, e.g. https://cms.acme.com), the plural content "
        "type IDs (e.g. 'articles'), the API token, and your target locales.",
        "Set the Strapi version to 'v4' if you run Strapi 4 (default is v5).",
        "Optional: if the token can read the Content-Type Builder, fields Strapi marks as "
        "not localized are skipped automatically. Otherwise set the `fields` allowlist to "
        "your localized text fields.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Strapi's updatedAt is the "
    "content-changed signal, and unchanged entries are suppressed by content hash. "
    "Writes go to the entry's DRAFT on v5 (never auto-published); a v4 PUT updates "
    "the live entry. Concurrent edits of the same field are last-write-wins.",
    docs_url="https://docs.strapi.io/dev-docs/plugins/i18n",
    supports_oauth=False,
)


class StrapiConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="strapi",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.NONE,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="updatedAt", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = SubResourceStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        self._locales_cache: list[dict] | None = None
        # Field-selection VISIBILITY (review H3): accumulated across extract() calls,
        # surfaced by build_message / build_result and logged loud for rich-content skips.
        self._sel_selected: set = set()
        self._sel_skipped: dict = {}
        self._sel_warned: set = set()
        # #638: Strapi's own per-field i18n flags. None = not fetched yet; after the first
        # attempt it holds {plural_name: {"type_localized": bool, "fields": {name: bool}}}
        # (empty when the schema could not be read: `_schema_state` says which).
        self._schema_cache: dict | None = None
        self._schema_state = ""  # "" (not tried) | "read" | "unreadable"

    # ---------------------------------------------------------------- version
    def _version(self) -> str:
        v = (self.config.get("strapi_version") or API_VERSION).strip().lower()
        if v not in _SUPPORTED_VERSIONS:
            raise ConnectorError(
                f"Unsupported Strapi version {v!r}; supported: {', '.join(_SUPPORTED_VERSIONS)}."
            )
        return v

    # ------------------------------------------------------------ auth / base
    def _base(self) -> str:
        raw = (self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "Strapi Base URL is required (your site root, e.g. https://cms.acme.com)."
            )
        root = _validate_target(raw).rstrip("/")
        if root.lower().endswith("/api"):
            root = root[:-4].rstrip("/")
        return f"{root}/api"

    def _headers(self, *, write: bool = False) -> dict:
        token = (self.secret.get("api_token") or "").strip()
        if not token:
            raise ConnectorError("Strapi API token is required.")
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if write:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _path(*segments) -> str:
        """A percent-encoded API path so a '/', '?', '#' or space in ANY segment
        (content type or id) cannot retarget the URL (review H1/L1). Segments are
        already validated (_validate_ct/_validate_docid); encoding is defence in depth."""
        return "/" + "/".join(quote(str(s), safe="") for s in segments)

    def _get(self, c, path, **kw):
        return self._request(c, "GET", self._base() + path, headers=self._headers(), **kw)

    # --------------------------------------------------------------- locales
    def _fetch_locales(self) -> list[dict]:
        if self._locales_cache is not None:
            return self._locales_cache
        with self._client() as c:
            r = self._get(c, "/i18n/locales")
        if r.status_code != 200:
            # #165: the raw upstream body is NOT echoed -- an operator-visible error is a
            # disclosure surface. Host + status is enough to diagnose, and the i18n hint
            # below is the actionable part of the original message, so keep it.
            # _endpoint_host (not urlsplit(...).hostname) because that returns None for a
            # blank or scheme-less value and would render a literal "None" to the operator.
            raise ConnectorError(
                f"Could not read Strapi locales from "
                f"{_endpoint_host(self.config.get('base_url') or '') or 'the configured host'} "
                f"({r.status_code}) — is the Internationalization (i18n) plugin installed "
                f"and enabled?"
            )
        body = r.json()
        locs = body if isinstance(body, list) else (body.get("data") or [])
        out = [x for x in locs if isinstance(x, dict) and x.get("code")]
        self._locales_cache = out
        return out

    def enabled_locales(self) -> list[str]:
        return [str(x["code"]).strip().lower() for x in self._fetch_locales()]

    def _locale_map(self) -> dict[str, str]:
        return {
            str(x["code"]).strip().lower(): str(x["code"]).strip() for x in self._fetch_locales()
        }

    def _resolve_locale(self, loc: str) -> str:
        return self._locale_map().get(str(loc).strip().lower(), str(loc).strip())

    def _source_locale(self) -> str:
        cfg = (self.config.get("source_locale") or "").strip()
        if cfg:
            return self._resolve_locale(cfg)
        for x in self._fetch_locales():
            if x.get("isDefault"):
                return str(x["code"]).strip()
        locs = self._fetch_locales()
        return str(locs[0]["code"]).strip() if locs else "en"

    # ---------------------------------------------------------- content types
    def _content_types(self) -> list[str]:
        raw = (self.config.get("content_types") or "").strip()
        cts = [x.strip() for x in raw.replace(";", ",").replace("\n", ",").split(",") if x.strip()]
        if not cts:
            raise ConnectorError(
                "At least one content type ID is required (the plural API ID, e.g. 'articles')."
            )
        for ct in cts:
            # Reject a ':' (would collide with the '<ct>:<id>' record-id scheme + the
            # strapi_hashes namespace), URL-structural chars, and '..'/dots-only (which
            # escape the /api path entirely, e.g. content_types='..' -> GET {base}?...)
            # (review L4/H1/N4).
            if (
                ".." in ct
                or set(ct) <= {"."}
                or any(ch in ct for ch in (":", "/", "?", "#"))
                or any(ch.isspace() for ch in ct)
            ):
                raise ConnectorError(
                    f"Invalid content type {ct!r}: may not contain ':', '/', '?', '#', "
                    f"'..' or be dots-only."
                )
        return cts

    def _validate_ct(self, ct: str, cts: list[str] | None = None) -> str:
        """Fail closed: a caller-named content type MUST be one this connection is
        configured for, else refuse (review H1 — playbook §3 scope law)."""
        cts = cts if cts is not None else self._content_types()
        c = str(ct).strip()
        if c not in cts:
            raise ConnectorError(
                f"Content type {ct!r} is not configured on this connection "
                f"({', '.join(cts)}) — refusing to target it."
            )
        return c

    @staticmethod
    def _validate_docid(docid: str) -> str:
        """Reject an id that could escape the /api/{ct}/ path or retarget the URL
        (review H1/L1). A real Strapi documentId / numeric id has none of these."""
        d = str(docid).strip()
        if not d:
            raise ConnectorError("Empty entry id.")
        if (
            set(d) <= {"."}
            or ".." in d
            or any(ch in d for ch in ("/", "?", "#", "\\"))
            or any(ch.isspace() for ch in d)
        ):
            raise ConnectorError(
                f"Unsafe entry id {docid!r}: may not contain '/', '?', '#', '..', "
                f"whitespace or be dots-only."
            )
        return d

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            locs = self._fetch_locales()
            return True, (f"Connected to Strapi ({self._version()}, {len(locs)} i18n locale(s)).")
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            return [
                {"id": ct, "name": f"{ct} (content type)", "tables": []}
                for ct in self._content_types()
            ]
        except Exception:
            return []

    # ------------------------------------------------------- entry normalize
    def _flatten(self, item: dict, ct: str) -> dict:
        item = item or {}
        if self._version() == "v4":
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            fields = {"id": item.get("id"), **attrs}
        else:
            fields = dict(item)
        return {
            "_ct": ct,
            "id": str(item.get("id") if item.get("id") is not None else fields.get("id") or ""),
            "documentId": fields.get("documentId"),
            "updatedAt": fields.get("updatedAt"),
            "locale": fields.get("locale"),
            "fields": fields,
        }

    @staticmethod
    def _docid(entry: dict) -> str:
        return str(entry.get("documentId") or entry.get("id") or "")

    def _aid(self, entry: dict) -> str:
        return f"{entry['_ct']}:{self._docid(entry)}"

    def _split_aid(self, record_id: str) -> tuple[str, str]:
        ct, sep, docid = str(record_id).partition(":")
        if not sep or not ct or not docid:
            raise ConnectorError(
                f"Malformed Strapi record id {record_id!r} (want '<contentType>:<id>')."
            )
        return self._validate_ct(ct), self._validate_docid(docid)

    # ----------------------------------------------------- read status (H4)
    def _read_params(self, locale: str | None) -> dict:
        """Query params for a READ/PROBE. v5 pins status=draft so reads target the same
        version the write lands on (Draft & Publish makes the default read PUBLISHED,
        which would 404 the probe forever). v4 has no document draft -> no status."""
        p: dict = {}
        if locale:
            p["locale"] = locale
        if self._version() != "v4":
            p["status"] = "draft"
        return p

    # ------------------------------------------------------------- discovery
    def _paginate(self, c, ct: str, params: dict, cap: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        params = dict(params or {})
        params.setdefault("pagination[pageSize]", min(100, max(1, cap)))
        while len(out) < cap:
            p = dict(params)
            p["pagination[page]"] = page
            r = self._get(c, self._path(ct), params=p)
            if r.status_code == 404:
                raise ConnectorError(
                    f"Content type {ct!r} not found (404). Check the plural API ID — it "
                    f"must end in 's' (e.g. 'articles', not 'article')."
                )
            if r.status_code != 200:
                raise ConnectorError(
                    f"Strapi list {ct} failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json()
            data = body.get("data") or []
            out.extend(self._keep_rows(data))
            meta = (body.get("meta") or {}).get("pagination") or {}
            page_count = int(meta.get("pageCount") or 1)
            if page >= page_count or not data:
                break
            page += 1
        return [self._flatten(x, ct) for x in out[:cap]]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source-locale entries to translate. Explicit article_ids ('<ct>:<docid>' or a
        bare docid for a single-content-type connection) win and are SCOPE-VALIDATED
        (review H1). Else each configured content type is fetched (newest-updated first)
        and the shared cap is filled ROUND-ROBIN across types (review N1) so the first
        content type can never starve the others — every type gets a fair rotation of
        the budget, and a short type spills its unused share to the rest."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        src = self._source_locale()
        cts = self._content_types()
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                for raw in list(article_ids)[:cap]:
                    ct, docid = self._resolve_explicit(str(raw), cts)
                    entry = self._fetch_entry(c, ct, docid, src)
                    if entry is not None:
                        out.append(self._charge_record(entry))
                return out
            params = {"sort[0]": "updatedAt:desc", **self._read_params(src)}
            per_type = [self._paginate(c, ct, dict(params), cap) for ct in cts]
            entries: list[dict] = []
            i = 0
            while len(entries) < cap and any(i < len(lst) for lst in per_type):
                for lst in per_type:
                    if i < len(lst):
                        entries.append(lst[i])
                        if len(entries) >= cap:
                            break
                i += 1
            return entries

    def _resolve_explicit(self, raw: str, cts: list[str]) -> tuple[str, str]:
        if ":" in raw:
            ct, _sep, docid = raw.partition(":")
            return self._validate_ct(ct.strip(), cts), self._validate_docid(docid)
        if len(cts) == 1:
            return cts[0], self._validate_docid(raw)
        raise ConnectorError(
            f"Ambiguous entry id {raw!r}: this connection has multiple content types, so "
            f"ids must be qualified as '<contentType>:<id>'."
        )

    def _fetch_entry(self, c, ct: str, docid: str, locale: str) -> dict | None:
        r = self._get(c, self._path(ct, docid), params=self._read_params(locale))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Strapi get {ct}/{docid} failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        data = (r.json() or {}).get("data")
        if not isinstance(data, dict):
            return None
        return self._flatten(data, ct)

    # ------------------------------------------------------------ i18n schema (#638)
    def _i18n_schema(self, ct: str | None) -> dict | None:
        """Strapi's own i18n setting for the content type `ct` (the plural API id), read once
        per connector from the content-type-builder API: {"type_localized": bool, "fields":
        {attribute: localized?}}. None when `ct` is unknown or the schema cannot be read
        (the token lacks content-type-builder read, an older Strapi, a proxy). Fail-soft:
        the caller then keeps the name/value heuristics and says so in the run message."""
        if not ct:
            return None
        if self._schema_cache is None:
            self._schema_cache = {}
            try:
                with self._client() as c:
                    r = self._get(c, "/content-type-builder/content-types")
                if r.status_code != 200:
                    raise ConnectorError(f"content-type-builder answered {r.status_code}")
                body = r.json()
                items = body.get("data") if isinstance(body, dict) else body
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    raise ConnectorError("unexpected content-type-builder response")
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    sch = it.get("schema") if isinstance(it.get("schema"), dict) else it
                    plural = str(sch.get("pluralName") or sch.get("collectionName") or "").strip()
                    attrs = sch.get("attributes")
                    if not plural or not isinstance(attrs, dict):
                        continue

                    def _loc(po):
                        i18n = (po or {}).get("i18n") if isinstance(po, dict) else None
                        v = i18n.get("localized") if isinstance(i18n, dict) else None
                        return v if isinstance(v, bool) else None

                    self._schema_cache[plural] = {
                        "type_localized": _loc(sch.get("pluginOptions")) is not False,
                        "fields": {
                            str(n): (_loc(a.get("pluginOptions")) is not False)
                            for n, a in attrs.items()
                            if isinstance(a, dict)
                        },
                    }
                self._schema_state = "read"
            except Exception as e:  # any failure means "cannot check", never a crash
                log.info(
                    "strapi: i18n schema unreadable (%s); using field heuristics", type(e).__name__
                )
                self._schema_cache = {}
                self._schema_state = "unreadable"
        return self._schema_cache.get(ct)

    # ------------------------------------------------------------ extraction
    def _field_selection(self, fields: dict, ct: str | None = None) -> tuple[list[str], dict]:
        """(translatable_names, {skipped_name: reason}). Allowlist wins; else heuristics.
        #638: whatever the pick, a field Strapi itself marks `localized: false` (or every
        field of a content type with localization off) is dropped with the reason
        'not localized in Strapi': its value is shared by all locales by design."""
        selected, skipped = self._pick_fields(fields)
        sch = self._i18n_schema(ct)
        if sch is not None:
            keep: list[str] = []
            for n in selected:
                if not sch["type_localized"] or sch["fields"].get(n) is False:
                    skipped[n] = "not localized in Strapi"
                else:
                    keep.append(n)
            selected = keep
        return selected, skipped

    def _pick_fields(self, fields: dict) -> tuple[list[str], dict]:
        fields = fields or {}
        allow = _parse_names(self.config.get("fields"))
        if allow:
            selected: list[str] = []
            skipped: dict = {}
            for n in allow:
                # An allowlisted SYSTEM field (updatedAt/documentId/...) is refused here
                # with a config-visible reason, NOT written — else every write 400s on
                # Strapi's strict `data` validator (review N6).
                if n in _SYSTEM_FIELDS:
                    skipped[n] = "system/reserved field — cannot be translated"
                elif isinstance(fields.get(n), str) and str(fields.get(n)).strip():
                    selected.append(n)
                else:
                    skipped[n] = "listed in `fields` but not a non-empty text field on this entry"
            return selected, skipped
        exclude = set(_parse_names(self.config.get("exclude_fields")))
        selected: list[str] = []
        skipped: dict = {}
        for k, v in fields.items():
            if k in exclude:
                skipped[k] = "operator exclude_fields"
                continue
            ok, reason = _classify_field(k, v)
            if ok:
                selected.append(k)
            elif k not in _SYSTEM_FIELDS:  # don't clutter the report with system keys
                skipped[k] = reason
        return selected, skipped

    def _record_selection(self, selected, skipped) -> None:
        self._sel_selected.update(selected)
        for n, r in skipped.items():
            self._sel_skipped.setdefault(n, r)
            if "rich content" in r and n not in self._sel_warned:
                self._sel_warned.add(n)
                log.warning(
                    "strapi: field %r skipped — %s. It is NOT being translated; "
                    "add it to the `fields` allowlist only if it holds plain text.",
                    n,
                    r,
                )

    def _translatable_field_names(self, fields: dict, ct: str | None = None) -> list[str]:
        selected, skipped = self._field_selection(fields, ct)
        self._record_selection(selected, skipped)
        return selected

    def _selection_summary(self) -> str:
        if not self._sel_selected and not self._sel_skipped:
            return ""
        parts = [f"Translatable field(s): {', '.join(sorted(self._sel_selected)) or '(none)'}."]
        if self._sel_skipped:
            items = sorted(self._sel_skipped.items())
            shown = "; ".join(f"{n} ({r})" for n, r in items[:8])
            more = "" if len(items) <= 8 else f"; +{len(items) - 8} more"
            parts.append(f"Skipped: {shown}{more}.")
        if self._schema_state == "unreadable" and not _parse_names(self.config.get("fields")):
            parts.append(
                "Could not read Strapi's content-type schema with this token, so its "
                "localized flags were not checked: fields were picked by name and value only. "
                "Set the connection's `fields` allowlist to the localized text fields."
            )
        return " ".join(parts)

    def _title_field(self, names: list[str]) -> str | None:
        cfg = (self.config.get("title_field") or "").strip()
        if cfg and cfg in names:
            return cfg
        for cand in ("title", "name"):
            if cand in names:
                return cand
        return None

    def extract(self, entry: dict) -> tuple[str | None, str, list[_Unit], str, list[str]]:
        fields = entry.get("fields") or {}
        names = self._translatable_field_names(fields, entry.get("_ct"))
        title_field = self._title_field(names)
        title_text = str(fields.get(title_field) or "") if title_field else ""
        body_names = [n for n in names if n != title_field]
        body_units = [_Unit(str(fields.get(n) or ""), n) for n in body_names]
        chash = _content_hash(*([title_text] + [str(fields.get(n) or "") for n in body_names]))
        return title_field, title_text, body_units, chash, body_names

    # ------------------------------------------------- write payload assembly
    def _build_data(self, fields: dict, *, create: bool) -> tuple[dict, bool]:
        """(data, had_real) — the clean `data` dict that reaches Strapi, and whether it
        carries at least one REAL translation (not just a source-title fallback).

        Strips the strategy-injected `draft` sentinel (a bool — a real text field named
        'draft' arrives aliased and survives, review M2), un-aliases reserved-name
        fields, and applies the CREATE-only source-title fallback (review H2). `had_real`
        is False when EVERY translatable field was held back (NEEDS_REVIEW) this run — the
        caller then writes NOTHING and reports it as held-back, so the suppression hash is
        never poisoned by a zero-real-content write (review N3)."""
        data: dict = {}
        had_real = False
        for k, v in (fields or {}).items():
            if k == _TITLE_FALLBACK_KEY:
                if create:
                    tf, src = v
                    data.setdefault(tf, src)  # source seed — NOT a real translation
                continue
            if k in _RESERVED_WRITE_KEYS and isinstance(v, bool):
                continue  # strategy-injected draft flag — never a real Strapi field value
            if k.startswith(_ALIAS_PREFIX):
                data[k[len(_ALIAS_PREFIX) :]] = v
            else:
                data[k] = v
            had_real = True
        return data, had_real

    # ------------------------------------------------- SubResourceClient (T2)
    def get_translation(self, record_id: str, locale: str):
        """EXISTENCE PROBE for the (entry, locale) variant — an explicit signal, never a
        content read. v5 probes the DRAFT (status=draft) so it matches where the write
        lands. v4: the master's `localizations` array."""
        ct, docid = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        with self._client() as c:
            if self._version() == "v4":
                loc_id = self._v4_localization_id(c, ct, docid, exact)
                return {"id": loc_id, "locale": exact} if loc_id else ABSENT
            r = self._get(c, self._path(ct, docid), params=self._read_params(exact))
        if r.status_code == 200:
            data = (r.json() or {}).get("data")
            return self._flatten(data, ct)["fields"] if isinstance(data, dict) else {}
        if r.status_code == 404:
            return ABSENT
        raise ConnectorError(
            f"Strapi locale probe {ct}/{docid}/{exact} -> {r.status_code}: {vendor_excerpt(r.text)}"
        )

    def _v4_localization_id(self, c, ct: str, master_id: str, exact_locale: str) -> str | None:
        r = self._get(
            c, self._path(ct, master_id), params={"populate[localizations][fields][0]": "locale"}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Strapi v4 localization probe {ct}/{master_id} -> {r.status_code}: "
                f"{vendor_excerpt(r.text)}"
            )
        data = (r.json() or {}).get("data") or {}
        attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else data
        locs = (
            (attrs.get("localizations") or {}).get("data")
            if isinstance(attrs.get("localizations"), dict)
            else attrs.get("localizations")
        ) or []
        for loc in locs:
            la = loc.get("attributes") if isinstance(loc.get("attributes"), dict) else loc
            if str((la or {}).get("locale") or "").strip() == exact_locale:
                return str(loc.get("id") or (la or {}).get("id") or "") or None
        return None

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """Create the missing locale variant with ONLY the translated fields (+ a
        source-title seed if the title was held back). v5: PUT /{ct}/{documentId}?locale=
        (upsert). v4: POST /{ct}/{masterId}/localizations (flat {locale, ...fields})."""
        ct, docid = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        data, had_real = self._build_data(fields, create=True)
        if not had_real:
            # Every translatable field was held back (NEEDS_REVIEW). Write NOTHING and
            # report held-back so the flow does NOT record the suppression hash — a later
            # healthy run (unchanged source) will re-attempt instead of skipping (N3).
            return WriteResult(False, True, message=_HELD_BACK)
        try:
            with self._client() as c:
                if self._version() == "v4":
                    r = self._request(
                        c,
                        "POST",
                        self._base() + self._path(ct, docid, "localizations"),
                        json={"locale": exact, **data},
                        idempotent=False,
                        headers=self._headers(write=True),
                    )
                else:
                    r = self._request(
                        c,
                        "PUT",
                        self._base() + self._path(ct, docid),
                        params={"locale": exact},
                        json={"data": data},
                        headers=self._headers(write=True),
                    )
        except httpx.TransportError as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return WriteResult(True, True, target_id=self._written_id(r, ct), message="created")
        return WriteResult(False, True, message=f"{r.status_code}: {vendor_excerpt(r.text)}")

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult:
        """Update the existing locale variant with ONLY the fields translated this run
        (review M1: Strapi's partial PUT keeps every field we do NOT send, so omitted /
        held-back fields keep their stored translation — that IS the protection, no
        read-current-merge, no relations in the body). v5 PUTs by documentId; v4 PUTs the
        localization by its own numeric id."""
        ct, docid = self._split_aid(record_id)
        exact = self._resolve_locale(locale)
        data, had_real = self._build_data(fields, create=False)
        if not had_real:
            # Every field held back (NEEDS_REVIEW): write NOTHING (the stored translation
            # stands via partial-PUT) and report held-back so the suppression hash is NOT
            # recorded and a later healthy run re-attempts (review N3 — NOT counted as an
            # 'updated' write, which would poison suppression and hide the held content).
            return WriteResult(False, False, message=_HELD_BACK)
        try:
            with self._client() as c:
                if self._version() == "v4":
                    loc_id = self._v4_localization_id(c, ct, docid, exact)
                    if not loc_id:  # raced away between probe and write -> create it
                        return self.create_translation(record_id, locale, fields)
                    r = self._request(
                        c,
                        "PUT",
                        self._base() + self._path(ct, loc_id),
                        json={"data": data},
                        headers=self._headers(write=True),
                    )
                else:
                    r = self._request(
                        c,
                        "PUT",
                        self._base() + self._path(ct, docid),
                        params={"locale": exact},
                        json={"data": data},
                        headers=self._headers(write=True),
                    )
        except httpx.TransportError as e:
            return WriteResult(False, False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return WriteResult(True, False, target_id=self._written_id(r, ct), message="updated")
        return WriteResult(False, False, message=f"{r.status_code}: {vendor_excerpt(r.text)}")

    def _written_id(self, r: httpx.Response, ct: str) -> str | None:
        try:
            data = (r.json() or {}).get("data")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        flat = self._flatten(data, ct)
        return flat.get("documentId") or flat.get("id") or None

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for entry in self.discover_articles():
            _tf, _tt, _bu, _h, _bn = self.extract(entry)
            fields = entry.get("fields") or {}
            aid = self._aid(entry)
            names = self._translatable_field_names(fields, entry.get("_ct"))
            label = str(fields.get(self._title_field(names) or "") or aid)[:60]
            for name in names:
                out.append(
                    PulledItem(
                        external_id=aid, field=name, text=str(fields.get(name) or ""), title=label
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Strapi writes per-locale entry variants — use 'Translate now'.")

    def detect_change(self, conn=None):
        return []
