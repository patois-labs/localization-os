"""contentful: part2

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from app.filters import contentful_rich_text as rich_text
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    PulledItem,
    _validate_target,
    fetch_cap,
)
from app.services.connectors.base import (
    content_hash as _content_hash,
)
from app.services.connectors.contentful_part1 import (
    _REGION_BASES,
    _REMOTE_REGEX_TIMEOUT_S,
    _SYMBOL_MAX,
    _TRANSLATABLE_TYPES,
    API_VERSION,
    CMA_CONTENT_TYPE,
    SPEC,
    ContentfulEntryStrategy,
    _compile_js_regex,
    _name_looks_id,
    _parse_names,
    _PutOutcome,
    _rich_key,
    _Unit,
    _validate_segment,
    _value_is_machine,
    log,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.strategy import (
    ABSENT,
)
from app.services.connectors.vendor_text import vendor_excerpt


class ContentfulConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="contentful",
        topology=Topology.T1,  # field-level map (fields.{id}.{locale})
        auth=(AuthKind.API_KEY,),  # CMA personal access token (Bearer)
        api_version=API_VERSION,
        rich_text=RichText.CONTENTFUL_RICH_TEXT,  # Rich Text walker, #648
        regions="fixed-region",  # US/EU host pin
        change_signals=ChangeSignals(
            webhook_sig=None,  # webhooks EXIST but scheme not verified -> poll-first
            poll_field="sys.updatedAt",  # metadata-tainted -> NO watermark; hash is the gate
            stale_flag=None,
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = ContentfulEntryStrategy()
        self._locales_cache: list[dict] | None = None
        self._schema_cache: dict[str, dict] = {}
        # Field-selection visibility (taxonomy D): accumulated across extract() calls,
        # surfaced by build_message / build_result and logged loud for RichText skips.
        self._sel_selected: set = set()
        self._sel_skipped: dict = {}
        self._sel_warned: set = set()
        # Per-run INCOMPLETE markers (review H2/M1): {(entry_id, locale): {field: reason}}
        # for pairs where SOME fields were written but others were held back (NEEDS_REVIEW
        # or a CMA-validation failure). Surfaced by build_message / build_result so the
        # operator sees exactly what still needs attention; the flow records NO content
        # hash for such a pair (#665, like #640), so the next run retries it.
        self._incomplete: dict = {}
        # Per-run VALIDATION-BLOCK markers (review F1a/F2): {(entry_id, locale): {what:
        # reason}} for pairs that could NOT be written at all because every candidate
        # value fails validation, or a pre-existing echoed value violates a validation.
        # These are DETERMINISTIC, so build_result persists them durably (hash + block
        # set) and the scheduled path (_ctf_is_suppressed) skips them WITHOUT re-spending
        # MT. `_chash_by_aid` carries each processed entry's source hash so build_result
        # can persist the block keyed on it.
        self._blocked: dict = {}
        self._chash_by_aid: dict = {}
        # Rich Text source documents per entry id ({fid: document}), set by extract() and
        # read by _ctf_prepare so the flow can rebuild each field from its translated units.
        self._rich_by_aid: dict = {}
        # {(entry_id, locale): {field: reason}} for fields held back by a CMA VALIDATION on
        # a pair that was otherwise written (#665). Deterministic for an unchanged source,
        # so _persist_blocks parks such a pair (no MT re-spend) without recording a hash.
        self._vheld: dict = {}

    def _record_vheld(self, entry_id, locale, held: dict) -> None:
        if held:
            self._vheld.setdefault((str(entry_id), str(locale)), {}).update(held)

    def _is_parked(self, entry_id, locale, chash) -> bool:
        """True when a prior run parked this pair for exactly this source hash (a CMA
        validation hold, #665). A parked pair is skipped by scheduled runs WITHOUT any MT,
        and is not done: no content hash exists for it."""
        parked = (self.config or {}).get("contentful_blocked_src") or {}
        return bool(chash) and parked.get(f"{entry_id}:{locale}") == chash

    def _record_incomplete(self, entry_id, locale, held: dict) -> None:
        """Merge held-field reasons for an (entry, locale) pair (review H2/M1)."""
        if not held:
            return
        self._incomplete.setdefault((str(entry_id), str(locale)), {}).update(held)

    def _record_blocked(self, entry_id, locale, reasons: dict) -> None:
        """Record a DETERMINISTIC validation block for an (entry, locale) pair — nothing
        was written and re-translating is pure waste until the source or the field
        validation changes (review F1a/F2)."""
        if not reasons:
            return
        self._blocked.setdefault((str(entry_id), str(locale)), {}).update(reasons)

    def _is_validation_blocked(self, entry_id, locale) -> bool:
        """True when a prior run durably recorded this (entry, locale) as validation-
        blocked (review F1a/F2). Consulted by _ctf_is_suppressed ONLY after the flow's
        content-hash gate has confirmed the source is UNCHANGED, so a source edit (hash
        change) bypasses this and re-attempts; an explicit retry (only_pairs) makes the
        flow skip the whole suppression check for the pair, so it re-attempts too."""
        blocked = (self.config or {}).get("contentful_blocked") or {}
        return f"{entry_id}:{locale}" in blocked

    # ------------------------------------------------------------ auth / base
    def _region(self) -> str:
        raw = (self.config.get("region") or "US").strip().upper() or "US"
        if raw not in _REGION_BASES:
            raise ConnectorError(
                f"Invalid Contentful region {self.config.get('region')!r} - use US or EU."
            )
        return raw

    def _host(self) -> str:
        host = _REGION_BASES[self._region()]
        _validate_target(host)  # SSRF guard on every request path
        return host

    def _space(self) -> str:
        return _validate_segment(self.config.get("space_id"), "Space ID")

    def _env(self) -> str:
        raw = (self.config.get("environment") or "master").strip() or "master"
        return _validate_segment(raw, "environment")

    def _headers(self, *, write: bool = False) -> dict:
        token = (self.secret.get("cma_token") or "").strip()
        if not token:
            raise ConnectorError("Contentful Management API token is required.")
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, application/vnd.contentful.management.v1+json",
        }
        if write:
            h["Content-Type"] = CMA_CONTENT_TYPE
        return h

    def _base_path(self) -> str:
        """/spaces/{space}/environments/{env} — segments validated + percent-encoded."""
        return f"/spaces/{quote(self._space(), safe='')}/environments/{quote(self._env(), safe='')}"

    @staticmethod
    def _seg(value) -> str:
        """Percent-encode a single (already-validated) path segment (defence in depth)."""
        return quote(str(value), safe="")

    def _get(self, c, path, **kw):
        return self._request(c, "GET", self._host() + path, headers=self._headers(), **kw)

    # --------------------------------------------------------------- locales
    def _fetch_locales(self) -> list[dict]:
        if self._locales_cache is not None:
            return self._locales_cache
        with self._client() as c:
            r = self._get(c, self._base_path() + "/locales")
        if r.status_code == 401 or r.status_code == 403:
            raise ConnectorError(
                f"Contentful rejected the token ({r.status_code}) — check the Management "
                f"API token and that it has access to space {self.config.get('space_id')!r}."
            )
        if r.status_code == 404:
            raise ConnectorError(
                f"Contentful space/environment not found (404) - check the Space ID and "
                f"environment. {vendor_excerpt(r.text)}"
            )
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read Contentful locales ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        items = (r.json() or {}).get("items") or []
        out = [x for x in items if isinstance(x, dict) and x.get("code")]
        self._locales_cache = out
        return out

    def enabled_locales(self) -> list[str]:
        return [str(x["code"]).strip().lower() for x in self._fetch_locales()]

    def _locale_map(self) -> dict[str, str]:
        return {
            str(x["code"]).strip().lower(): str(x["code"]).strip() for x in self._fetch_locales()
        }

    def resolve_locale(self, loc: str) -> str:
        """Lower-cased flow locale -> the space's EXACT locale code casing (de-de ->
        de-DE). Identity when unknown (the partition rejects unknown targets first)."""
        return self._locale_map().get(str(loc).strip().lower(), str(loc).strip())

    def _source_locale(self) -> str:
        cfg = (self.config.get("source_locale") or "").strip()
        if cfg:
            return self.resolve_locale(cfg)
        for x in self._fetch_locales():
            if x.get("default"):
                return str(x["code"]).strip()
        locs = self._fetch_locales()
        return str(locs[0]["code"]).strip() if locs else "en-US"

    # ---------------------------------------------------------- content types
    def _content_types(self) -> list[str]:
        raw = (self.config.get("content_types") or "").strip()
        cts = [x.strip() for x in raw.replace(";", ",").replace("\n", ",").split(",") if x.strip()]
        if not cts:
            raise ConnectorError("At least one content type ID is required (e.g. 'blogPost').")
        return [_validate_segment(ct, "content type") for ct in cts]

    def _validate_ct(self, ct: str, cts: list[str] | None = None) -> str:
        """Fail closed: a content type must be one this connection is configured for
        (defect taxonomy A / Strapi H1 — playbook §3 scope law)."""
        cts = cts if cts is not None else self._content_types()
        c = _validate_segment(ct, "content type")
        if c not in cts:
            raise ConnectorError(
                f"Content type {ct!r} is not configured on this connection "
                f"({', '.join(cts)}) - refusing to target it."
            )
        return c

    def _schema(self, ct: str) -> dict:
        """The content type's field schema: {"display": displayField, "fields":
        {fieldId: {"type": str, "localized": bool, "validations": list}}}. Cached per
        content type. The CMA token can read this (unlike Strapi's content-token
        limitation), so selection is schema-EXACT, not heuristic. `validations` is
        RETAINED (review H2) so a length/regex/enum-violating translation is held back
        per field instead of 422-ing the whole entry."""
        ct = self._validate_ct(ct)
        cached = self._schema_cache.get(ct)
        if cached is not None:
            return cached
        with self._client() as c:
            r = self._get(c, self._base_path() + "/content_types/" + self._seg(ct))
        if r.status_code == 404:
            raise ConnectorError(
                f"Content type {ct!r} not found (404) in space {self.config.get('space_id')!r}."
            )
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read content type {ct!r} ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        body = r.json() or {}
        fdefs: dict = {}
        for f in body.get("fields") or []:
            fid = f.get("id")
            if not fid:
                continue
            fdefs[str(fid)] = {
                "type": str(f.get("type") or ""),
                "localized": bool(f.get("localized")),
                "validations": list(f.get("validations") or []),
            }
        schema = {"display": body.get("displayField"), "fields": fdefs}
        self._schema_cache[ct] = schema
        return schema

    def localizable_field_ids(self, ct: str) -> set:
        """Field IDs we are ALLOWED to write to a target locale: localized AND a
        translatable text type. The write overlay guards on this set (defence in
        depth on top of selection)."""
        return {
            fid
            for fid, d in self._schema(ct)["fields"].items()
            if d["localized"] and d["type"] in _TRANSLATABLE_TYPES
        }

    def validate_value(self, ct: str, fid: str, value: str) -> str | None:
        """Return an operator-readable reason when `value` would FAIL Contentful's
        field validations (so a whole-entry PUT would 422), else None (review H2). Covers
        the Symbol 256 hard cap and the field's own size (min/max length), regexp (with
        JS-flag mapping, review F3) and `in` (enum) validations.

        FAILS OPEN on anything it cannot evaluate faithfully (review F3/F4): an
        unrecognised validation SHAPE ({"size": 5}, a non-dict element, a regexp with a
        JS-only flag or an unparseable pattern) is IGNORED rather than crashing or
        producing a false hold-back — a false hold-back is the harm, and the live CMA
        422s loudly for a genuinely invalid value (now classified as a validation block).
        A remote regexp that is over-long or that exceeds the per-match timeout fails
        open the same way, with a warning naming the content type + field, never the
        value (red-team audit 2026-10-03 finding 01)."""
        if not isinstance(value, str):
            return None  # a Rich Text document: validated by the CMA, not modelled here
        d = self._schema(ct)["fields"].get(fid) or {}
        n = len(value or "")
        if d.get("type") == "Symbol" and n > _SYMBOL_MAX:
            return f"exceeds the Symbol {_SYMBOL_MAX}-character limit ({n} chars)"
        for v in d.get("validations") or []:
            if not isinstance(v, dict):
                continue  # F4: a non-dict validation element -> ignore (fail open)
            size = v.get("size")
            if isinstance(size, dict):
                mx, mn = size.get("max"), size.get("min")
                if isinstance(mx, int) and n > mx:
                    return f"exceeds max length {mx} ({n} chars)"
                if isinstance(mn, int) and n < mn:
                    return f"below min length {mn} ({n} chars)"
            rx = v.get("regexp")
            if isinstance(rx, dict) and isinstance(rx.get("pattern"), str):
                cre = _compile_js_regex(
                    rx["pattern"], rx.get("flags"), where=f"content type {ct} field {fid}"
                )
                if cre is not None:
                    try:
                        # Red-team audit 2026-10-03 finding 01: the pattern is remote-
                        # supplied text; the regex-module timeout bounds what one match
                        # can cost a sync-worker thread.
                        hit = cre.search(value or "", timeout=_REMOTE_REGEX_TIMEOUT_S)
                    except TimeoutError:
                        log.warning(
                            "contentful regexp validation on content type %s field %s "
                            "exceeded the %.2fs match limit "
                            "(validation skipped, value accepted)",
                            ct,
                            fid,
                            _REMOTE_REGEX_TIMEOUT_S,
                        )
                        hit = True
                    if not hit:
                        return "does not match the field's required pattern"
            allowed = v.get("in")
            if isinstance(allowed, list) and value not in allowed:
                return "not one of the field's allowed values"
        return None

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            locs = self._fetch_locales()
            return True, (
                f"Connected to Contentful (space {self.config.get('space_id')}, "
                f"env {self._env()}, {len(locs)} locale(s))."
            )
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
        sys = item.get("sys") or {}
        return {
            "_ct": ct,
            "id": str(sys.get("id") or ""),
            "version": sys.get("version"),
            "updatedAt": sys.get("updatedAt"),
            "fields": item.get("fields") or {},
            "metadata": item.get("metadata"),
        }

    @staticmethod
    def _entry_ct(item: dict) -> str:
        sys = (item or {}).get("sys") or {}
        return str(((sys.get("contentType") or {}).get("sys") or {}).get("id") or "")

    # --------------------------------------------------------- discovery
    def _paginate(self, c, ct: str, cap: int) -> list[dict]:
        out: list[dict] = []
        skip = 0
        page_size = min(100, max(1, cap))
        while len(out) < cap:
            params = {
                "content_type": ct,
                "order": "-sys.updatedAt",
                "skip": skip,
                "limit": page_size,
            }
            r = self._get(c, self._base_path() + "/entries", params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Contentful list entries for {ct!r} failed ({r.status_code}): "
                    f"{vendor_excerpt(r.text)}"
                )
            body = r.json() or {}
            items = body.get("items") or []
            out.extend(self._flatten(x, ct) for x in self._keep_rows(items))
            total = int(body.get("total") or 0)
            skip += page_size
            if not items or skip >= total:
                break
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source entries to translate. Explicit article_ids win and are SCOPE-VALIDATED
        (their entry id is sanitised and their content type must be configured, taxonomy
        A/H1). Else each configured content type is listed (newest-updated first) and the
        shared cap is filled ROUND-ROBIN across types so the first type can never starve
        the others (defect taxonomy E)."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        cts = self._content_types()
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                for raw in list(article_ids)[:cap]:
                    entry = self._fetch_entry_scoped(c, str(raw), cts)
                    if entry is not None:
                        out.append(self._charge_record(entry))
                return out
            per_type = [self._paginate(c, ct, cap) for ct in cts]
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

    def _fetch_entry_scoped(self, c, entry_id: str, cts: list[str]) -> dict | None:
        """GET one entry by id, then SCOPE-CHECK its content type against the
        configured set (an entry id alone does not carry the content type, so the
        check happens after the fetch — fail closed on a non-configured type)."""
        eid = _validate_segment(entry_id, "entry id")
        r = self._get(c, self._base_path() + "/entries/" + self._seg(eid))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Contentful get entry {eid!r} failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        item = r.json() or {}
        ct = self._entry_ct(item)
        if ct not in cts:
            raise ConnectorError(
                f"Entry {eid!r} is content type {ct!r}, which is not configured on this "
                f"connection ({', '.join(cts)}) - refusing to target it."
            )
        return self._flatten(item, ct)

    # ------------------------------------------------------------ selection
    def translated_field_ids(self, ct: str, fields: dict) -> set:
        """Field IDs this connection TRANSLATES on an entry of content type `ct`, judged
        from the entry's own `fields` map (#647). Same gates and allowlist/exclude/name
        rules as `_field_selection`, but a field with no source text is still in scope
        (it is the field's role, not this entry's current value, that matters), so a
        translated title an editor emptied is recognised as missing. Localized slugs,
        excluded fields and machine-format values are NOT in scope: a target-locale value
        on one of those must never make a locale look already translated."""
        fdefs = self._schema(ct)["fields"]
        allow = _parse_names(self.config.get("fields"))
        exclude = set(_parse_names(self.config.get("exclude_fields")))
        src = self._source_locale()
        out: set = set()
        for fid in allow if allow else list(fdefs.keys()):
            d = fdefs.get(fid)
            if d is None or not d["localized"] or d["type"] not in _TRANSLATABLE_TYPES:
                continue
            if not allow:
                if fid in exclude or _name_looks_id(fid):
                    continue
                val = (fields.get(fid) or {}).get(src)
                if isinstance(val, str) and val.strip() and _value_is_machine(val):
                    continue
            out.add(fid)
        return out

    def _field_selection(self, entry: dict) -> tuple[list[str], dict]:
        """(translatable_field_ids, {skipped_id: reason}). Schema-driven. In AUTO mode
        (no `fields` allowlist) a localized Symbol/Text is NOT assumed to be translatable
        copy: the Strapi-ported name/value heuristic (review H1) additionally skips
        id/slug-named fields and machine-format values (localized slugs, canonical URLs,
        emails and dates are normal Contentful modelling). The explicit `fields`
        allowlist BYPASSES the heuristic (operator override) but still respects the
        localized + text-type gates."""
        ct = entry.get("_ct") or ""
        schema = self._schema(ct)
        fdefs = schema["fields"]
        fields = entry.get("fields") or {}
        src = self._source_locale()
        allow = _parse_names(self.config.get("fields"))
        exclude = set(_parse_names(self.config.get("exclude_fields")))
        selected: list[str] = []
        skipped: dict = {}

        def _source_value(fid: str) -> str | None:
            val = (fields.get(fid) or {}).get(src)
            if (fdefs.get(fid) or {}).get("type") == "RichText":
                # A Rich Text document counts when it holds at least one translatable block.
                return "rich text" if rich_text.extract(val) else None
            return val if (isinstance(val, str) and val.strip()) else None

        def _gate(fid: str) -> tuple[bool, str]:
            """The schema gate every field must pass (allowlist AND auto)."""
            d = fdefs.get(fid)
            if d is None:
                return False, "not a field on this content type"
            if not d["localized"]:
                return False, (
                    "field is not localized in Contentful (cannot receive a target locale)"
                )
            t = d["type"]
            if t not in _TRANSLATABLE_TYPES:
                return False, f"field type {t} is not translatable text"
            return True, "text"

        names = allow if allow else list(fdefs.keys())
        for fid in names:
            if not allow and fid in exclude:
                skipped[fid] = "operator exclude_fields"
                continue
            ok, reason = _gate(fid)
            if not ok:
                skipped[fid] = reason
                continue
            val = _source_value(fid)
            if val is None:
                skipped[fid] = "no non-empty source-locale text on this entry"
                continue
            # AUTO-mode heuristic (H1): skip id/slug NAMES and machine-format VALUES so a
            # localized slug/url/email/date is not blindly MT'd. Skipped for the allowlist
            # (operator override).
            if not allow:
                if _name_looks_id(fid):
                    skipped[fid] = f"field name looks non-translatable ({fid})"
                    continue
                mach = (
                    None
                    if (fdefs.get(fid) or {}).get("type") == "RichText"
                    else _value_is_machine(val)
                )
                if mach:
                    skipped[fid] = mach
                    continue
            selected.append(fid)
        return selected, skipped

    def _record_selection(self, selected, skipped) -> None:
        self._sel_selected.update(selected)
        for n, r in skipped.items():
            self._sel_skipped.setdefault(n, r)
            if "rich text" in r and n not in self._sel_warned:
                self._sel_warned.add(n)
                log.warning(
                    "contentful: field %r skipped - %s. It is NOT being translated; "
                    "RichText translation is a documented later build.",
                    n,
                    r,
                )

    def _translatable_field_names(self, entry: dict) -> list[str]:
        selected, skipped = self._field_selection(entry)
        self._record_selection(selected, skipped)
        return selected

    def _selection_summary(self) -> str:
        if not self._sel_selected and not self._sel_skipped:
            return ""
        parts = [f"Translatable field(s): {', '.join(sorted(self._sel_selected)) or '(none)'}."]
        if self._sel_skipped:
            # Surface the DEFERRED-TYPE / structural skips (rich text, non-text,
            # non-localized) FIRST so the taxonomy-D "loud skip" is never truncated
            # away behind alphabetical id/slug/value skips (LOW).
            def _rank(item):
                _n, r = item
                loud = "rich text" in r or "not translatable text" in r or "not localized" in r
                return (0 if loud else 1, _n)

            items = sorted(self._sel_skipped.items(), key=_rank)
            shown = "; ".join(f"{n} ({r})" for n, r in items[:8])
            more = "" if len(items) <= 8 else f"; +{len(items) - 8} more"
            parts.append(f"Skipped: {shown}{more}.")
        return " ".join(parts)

    def _title_field(self, ct: str, names: list[str]) -> str | None:
        cfg = (self.config.get("title_field") or "").strip()
        if cfg and cfg in names:
            return cfg
        display = self._schema(ct)["display"]
        if display and display in names:
            return display
        for cand in ("title", "name"):
            if cand in names:
                return cand
        return None

    def extract(self, entry: dict):
        ct = entry.get("_ct") or ""
        fields = entry.get("fields") or {}
        src = self._source_locale()
        names = self._translatable_field_names(entry)
        fdefs = self._schema(ct)["fields"]
        rich_names = [n for n in names if (fdefs.get(n) or {}).get("type") == "RichText"]
        plain = [n for n in names if n not in rich_names]
        title_field = self._title_field(ct, plain)

        def _src_text(fid: str) -> str:
            return str((fields.get(fid) or {}).get(src) or "")

        title_text = _src_text(title_field) if title_field else ""
        body_names = [n for n in plain if n != title_field]
        body_units = [_Unit(_src_text(n), n) for n in body_names]
        hash_parts = [title_text] + [_src_text(n) for n in body_names]
        # Rich Text (#648): one unit per text block, keyed "<field>#<n>". The source
        # document joins the content hash (canonical JSON) only when there is one, so an
        # entry without Rich Text keeps the hash it had before this feature.
        rich: dict = {}
        for n in rich_names:
            tree = (fields.get(n) or {}).get(src)
            units = rich_text.extract(tree)
            if not units:
                continue
            rich[n] = tree
            body_units.extend(_Unit(u.text, _rich_key(n, u.p_index)) for u in units)
            hash_parts.append(
                json.dumps(tree, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
        self._rich_by_aid[str(entry.get("id") or "")] = rich
        chash = _content_hash(*hash_parts)
        return title_field, title_text, body_units, chash, body_names

    # ------------------------------------- client the T1 strategy drives
    def entry_snapshot(self, entry_id: str):
        """{"fields", "version", "ct", "metadata"} for the entry, or strategy.ABSENT
        on 404. The content type is re-scope-checked (defence in depth) so a write can
        never target an entry outside the configured content types."""
        eid = _validate_segment(entry_id, "entry id")
        with self._client() as c:
            r = self._get(c, self._base_path() + "/entries/" + self._seg(eid))
        if r.status_code == 404:
            return ABSENT
        if r.status_code != 200:
            raise ConnectorError(
                f"Contentful get entry {eid!r} -> {r.status_code}: {vendor_excerpt(r.text)}"
            )
        item = r.json() or {}
        ct = self._validate_ct(self._entry_ct(item))
        sys = item.get("sys") or {}
        return {
            "fields": item.get("fields") or {},
            "version": sys.get("version"),
            "ct": ct,
            "metadata": item.get("metadata"),
        }

    def write_entry(self, entry_id: str, fields: dict, version, metadata) -> _PutOutcome:
        """PUT the whole entry with the X-Contentful-Version lock. Idempotent by id +
        version (a retried PUT after a 5xx that landed re-arrives with a stale version
        -> 409 -> the strategy refetches), so it retries on 5xx via base._request."""
        eid = _validate_segment(entry_id, "entry id")
        body: dict = {"fields": fields}
        if metadata is not None:
            body["metadata"] = metadata  # preserve tags/taxonomy (never clobber)
        headers = self._headers(write=True)
        if version is not None:
            headers["X-Contentful-Version"] = str(version)
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PUT",
                    self._host() + self._base_path() + "/entries/" + self._seg(eid),
                    json=body,
                    headers=headers,
                )
        except httpx.TransportError as e:
            return _PutOutcome(False, False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201):
            return _PutOutcome(True, False, status=r.status_code)
        if r.status_code == 409:
            # A VersionMismatch 409 never carries rate-limit headers (those ride the
            # 429, which base._request already retries), so no wait is read here: the
            # caller falls back to its bounded exponential backoff (#650).
            return _PutOutcome(
                False,
                True,
                message=f"409 version conflict: {vendor_excerpt(r.text)}",
                status=409,
            )
        return _PutOutcome(
            False, False, message=f"{r.status_code}: {vendor_excerpt(r.text)}", status=r.status_code
        )

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for entry in self.discover_articles():
            fdefs = self._schema(entry.get("_ct") or "")["fields"]
            # Rich Text is a document tree, not a string: it is translated by the flow
            # (extract), never pulled as flat text.
            names = [
                n
                for n in self._translatable_field_names(entry)
                if (fdefs.get(n) or {}).get("type") != "RichText"
            ]
            fields = entry.get("fields") or {}
            src = self._source_locale()
            aid = entry["id"]
            title_field = self._title_field(entry.get("_ct") or "", names)
            label = str((fields.get(title_field or "") or {}).get(src) or aid)[:60]
            for name in names:
                out.append(
                    PulledItem(
                        external_id=aid,
                        field=name,
                        text=str((fields.get(name) or {}).get(src) or ""),
                        title=label,
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Contentful writes per-locale field keys - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []
