"""akeneo: part1

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.services.connectors import (
    flow,
)
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
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _record_write_lock_for,
)
from app.services.connectors.tracking import SelectionTracker
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")


API_VERSION = "v1"  # /api/rest/v1 — a real URL path segment


_API_PREFIX = "/api/rest/v1"


_ENDPOINT = {"product": "products-uuid", "product_model": "product-models"}


_ID_FIELD = {"product": "uuid", "product_model": "code"}


_CONTENT_TYPES = tuple(_ENDPOINT)  # ("product", "product_model")


_TRANSLATABLE_TYPES = frozenset({"pim_catalog_text", "pim_catalog_textarea"})


_HELD_BACK_PREFIX = "held-back:"


_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable attribute is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)


# /api/rest/v1 — a real URL path segment
# The two content types v1 covers, and their REST endpoint + identifier field. This is the
# FIXED allowlist the caller-supplied content-type half of an article id is validated against
# (defect taxonomy A / Strapi H1) — a value outside it is refused, fail-closed.
# ("product", "product_model")
# The Akeneo attribute types that carry translatable plain text. Everything else (identifier,
# metric, price_collection, boolean, number, date, simpleselect, multiselect, image, file,
# reference_entity, ...) is skipped + reported. Selection is EXACT on this set, not a heuristic.
# WriteResult.message marker for a (record, locale) whose every translatable attribute was
# held back (NEEDS_REVIEW) this run — written as ok=False so the flow records NO suppression
# hash, then accounted as `held_back` (not a real failure) in the report (taxonomy B/F).
def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _validate_segment(value, what: str) -> str:
    """Reject an id/code that could escape or retarget the /api/rest/v1 path (defect
    taxonomy A). A real Akeneo uuid / product-model code is [A-Za-z0-9._-]-ish and contains
    NONE of these. Rejects '/', '?', '#', '\\', whitespace, control chars (NUL/CR/LF/DEL),
    '..' and dots-only — so a traversal, a query/fragment injector or a CRLF cannot reach the
    wire. Percent-encoding at the call site is defence in depth on top of this."""
    v = str(value or "").strip()
    if not v:
        raise ConnectorError(f"{what} is required.")
    if (
        set(v) <= {"."}
        or ".." in v
        or any(ch in v for ch in ("/", "?", "#", "\\"))
        or any(ch.isspace() for ch in v)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)
    ):
        raise ConnectorError(
            f"Unsafe {what} {value!r}: may not contain '/', '?', '#', '..', whitespace, "
            f"control characters or be dots-only."
        )
    return v


@dataclass
class _PatchOutcome:
    """Result of one values PATCH: ok (2xx/204) or a terminal failure carrying a message +
    the HTTP status (so a caller can classify a 422 validation refusal distinctly)."""

    ok: bool
    message: str = ""
    status: int | None = None


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index). p_index is
    the ATTRIBUTE CODE, so build_payload reinserts each translation into the exact attribute
    it came from."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


SPEC = ConnectorSpec(
    kind="akeneo",
    name="Akeneo",
    icon="🗂️",  # card index dividers (matches the factory spec)
    cat="PIM",
    blurb="Translate Akeneo PIM product and product-model attribute values into any "
    "language. Each translation is written back as a per-locale attribute value on the "
    "SAME record via a partial PATCH merge, so other locales, scopes, and untouched "
    "attributes are never overwritten.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Instance URL",
            "Your Akeneo PIM base URL, e.g. "
            "https://my-pim.cloud.akeneo.com. The connector appends /api/rest/v1. May "
            "not point at a private/loopback address.",
            placeholder="https://my-pim.cloud.akeneo.com",
        ),
        FieldSpec(
            "content_types",
            "Content types",
            "Comma-separated: 'product' and/or 'product_model'. Blank = both.",
            required=False,
            placeholder="product, product_model",
        ),
        FieldSpec(
            "channel",
            "Channel (scope)",
            "Optional Akeneo channel code used as the "
            "scope for scopable attributes. Required only if you translate scopable "
            "attributes.",
            required=False,
            placeholder="ecommerce",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The authoring Akeneo locale to translate "
            "FROM (e.g. en_US). REQUIRED — Akeneo has no default locale.",
            required=False,
            placeholder="en_US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Akeneo locale codes (e.g. fr_FR, de_DE) enabled on the instance.",
            required=False,
            placeholder="fr_FR, de_DE",
        ),
        FieldSpec(
            "max_entries",
            "Max records",
            "Safety cap on records scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "access_token",
            "Access token",
            "An Akeneo API access token (Bearer). For "
            "v1, paste a token minted via your Akeneo connection; the OAuth Connect-Apps "
            "flow is not required.",
            secret=True,
            placeholder="a long token",
        ),
    ],
    setup_steps=[
        "In Akeneo, go to Connect -> Connection settings and create a connection to obtain "
        "API credentials.",
        "Mint an access token for that connection (Connect Apps OAuth, or the API connection "
        "credentials) and paste the Bearer token here.",
        "Ensure every target locale is enabled under Settings -> Locales, set the Source "
        "locale (e.g. en_US), and note your channel code if you translate scopable attributes.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Akeneo's 'updated' search "
    "filter is the content-changed signal, re-verified by a per-(record, locale) "
    "content hash so unchanged records are suppressed. Writes are a partial PATCH "
    "merge (other locales/scopes/attributes are never touched) and land live (no "
    "draft/publish in Community Edition). Akeneo has no webhook receiver in scope "
    "for v1.",
    docs_url="https://api.akeneo.com/documentation/introduction.html",
    supports_oauth=False,
)


# card index dividers (matches the factory spec)
class AkeneoProductStrategy(LocaleMappingStrategy):
    """T1 write-back tailored to Akeneo's SHAPE, not the stock FieldMapStrategy (see the
    module docstring for why the stock class does not fit). Vendor-neutral in the same spirit
    as the shared strategies: it drives a small client protocol the connector implements
    (resolve_locale / record_values / snapshot_locale_present / build_values / patch_record).

    write_target is a SURGICAL PARTIAL PATCH with **zero read-before-write for the merge**:
    the values dict is built ENTIRELY from THIS run's translated attributes, and Akeneo's
    server-side per-(attribute, locale, scope) merge is what preserves every other locale,
    scope and attribute. The created-vs-updated flag comes from the DISCOVERY snapshot already
    in hand (no extra network read), never from a merge read. An empty payload (every
    attribute held back) writes NOTHING and reports held-back (taxonomy B/F).
    """

    topology = "T1"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True iff the target locale is already present on ANY translatable attribute of the
        record — an explicit value-presence signal, never a content comparison. Reads the LIVE
        record (the same state a PATCH writes, so a second run over unchanged source is
        suppressed — taxonomy C). A missing/absent record is always (re-)written."""
        exact = client.resolve_locale(target_locale)
        values = client.record_values(record_id)
        if values is ABSENT or not isinstance(values, dict):
            return False
        return client.locale_present(values, exact)

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        # `draft` is accepted for the framework signature but ignored: Akeneo CE has no
        # draft/publish — a PATCH is immediately live.
        if not fields:
            # Every translatable attribute held back (NEEDS_REVIEW). Write NOTHING and report
            # held-back so the flow records NO suppression hash and a later healthy run
            # re-attempts (taxonomy F). Nothing is source-seeded (taxonomy B).
            return WriteResult(False, False, message=_HELD_BACK)
        exact = client.resolve_locale(target_locale)
        # DEFENCE-IN-DEPTH lock only (NOT a read-merge-write serialization). Akeneo's write is
        # a single blind PATCH whose per-triple merge is server-side, so two in-process writers
        # of DIFFERENT locales of the same record cannot lose each other's work even without a
        # lock. We keep the shared D1 record lock anyway (per digest §2's recommendation) as a
        # guard against a future write-side regression; same single-process caveat as the
        # shared strategies. A true lost update needs two writers on the IDENTICAL
        # (attribute, locale, scope) triple — a narrower window than the whole-map-PUT platforms.
        lock = _record_write_lock_for(getattr(client, "connection_id", None), record_id)
        with lock:
            # created-vs-updated from the DISCOVERY snapshot already fetched this run — a purely
            # local lookup, so write_target stays read-free for the merge.
            existed = client.snapshot_locale_present(record_id, exact)
            values = client.build_values(fields, exact)
            if not values:
                # Selection guarantees writable attributes; an empty values dict here would be a
                # config/mapping bug (e.g. a scopable attribute with no channel slipped through).
                return WriteResult(
                    False, not existed, message="no writable attribute values for this locale"
                )
            out = client.patch_record(record_id, values)
            if out.ok:
                return WriteResult(
                    True,
                    not existed,
                    target_id=str(record_id),
                    message="created" if not existed else "updated",
                )
            return WriteResult(False, not existed, message=out.message)


class AkeneoConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="akeneo",
        topology=Topology.T1,  # field-level map (values[attr] = [{locale,scope,data}])
        auth=(AuthKind.API_KEY,),  # a pasted Bearer access token
        api_version=API_VERSION,
        rich_text=RichText.NONE,  # scalar text/textarea values; no structured AST
        regions="base_url",  # instance URL is fully operator-supplied
        change_signals=ChangeSignals(
            webhook_sig=None,  # no webhook receiver in scope for v1
            poll_field="updated",  # metadata-tainted -> NO watermark; hash is the gate
            stale_flag=None,
            thin_payload=False,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = AkeneoProductStrategy()
        self._locales_cache: list[dict] | None = None
        self._schema_cache: dict | None = None
        # Per-record DISCOVERY snapshot of the `values` payload (keyed by aid) — used ONLY to
        # classify created-vs-updated without a second read; never merged into a write body.
        self._snapshot: dict = {}
        # Shared field-selection + held-back bookkeeping (taxonomy D). Loud-warn ONCE on a
        # localizable-but-non-text attribute (a select/media/date deferred type) so the skip is
        # never silent.
        self.sel = SelectionTracker(
            noun="attribute", label="akeneo", loud_markers=("is not translatable text",)
        )

    # ------------------------------------------------------------ auth / base
    def _base(self) -> str:
        raw = (self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "Akeneo Instance URL is required (e.g. https://my-pim.cloud.akeneo.com)."
            )
        root = _validate_target(raw).rstrip("/")
        if root.lower().endswith(_API_PREFIX):
            root = root[: -len(_API_PREFIX)].rstrip("/")
        return f"{root}{_API_PREFIX}"

    def _headers(self, *, write: bool = False) -> dict:
        token = (self.secret.get("access_token") or "").strip()
        if not token:
            raise ConnectorError("Akeneo access token is required.")
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _get(self, c, path, **kw):
        return self._request(c, "GET", self._base() + path, headers=self._headers(), **kw)

    def _channel(self) -> str:
        return (self.config.get("channel") or "").strip()

    def _channel_codes(self) -> set:
        """The channel codes defined on the instance (GET /channels), for the L2 channel-
        existence preflight. A typo'd channel would otherwise surface as per-pair 422s at
        write time; preflight fails loud instead."""
        with self._client() as c:
            items = self._paginate_raw(c, "/channels")
        return {str(x.get("code")) for x in items if x.get("code")}

    # --------------------------------------------------------------- locales
    def _paginate_raw(self, c, path: str, limit: int = 100) -> list[dict]:
        """Walk a page/limit list endpoint (locales, attributes) returning every item from the
        `_embedded.items` envelope. with_count=false so a large catalog is not counted."""
        out: list[dict] = []
        page = 1
        while True:
            r = self._get(c, path, params={"page": page, "limit": limit, "with_count": "false"})
            if r.status_code == 401 or r.status_code == 403:
                raise ConnectorError(
                    f"Akeneo rejected the token ({r.status_code}) reading {path} — check the "
                    f"access token and its scope."
                )
            if r.status_code != 200:
                raise ConnectorError(
                    f"Akeneo {path} failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            items = ((r.json() or {}).get("_embedded") or {}).get("items") or []
            out.extend(x for x in items if isinstance(x, dict))
            if len(items) < limit:
                break
            page += 1
        return out

    def _fetch_locales(self) -> list[dict]:
        if self._locales_cache is not None:
            return self._locales_cache
        with self._client() as c:
            items = self._paginate_raw(c, "/locales")
        # Only ENABLED locales can receive values (the reference filters Enabled==true).
        out = [x for x in items if x.get("code") and x.get("enabled", True)]
        self._locales_cache = out
        return out

    def enabled_locales(self) -> list[str]:
        return [str(x["code"]).strip().lower() for x in self._fetch_locales()]

    def _locale_map(self) -> dict[str, str]:
        return {
            str(x["code"]).strip().lower(): str(x["code"]).strip() for x in self._fetch_locales()
        }

    def resolve_locale(self, loc: str) -> str:
        """Lower-cased flow locale -> the instance's EXACT locale code (fr_fr -> fr_FR).
        Identity when unknown (the partition rejects unknown targets first)."""
        return self._locale_map().get(str(loc).strip().lower(), str(loc).strip())

    def _source_locale(self) -> str:
        cfg = (self.config.get("source_locale") or "").strip()
        if not cfg:
            raise ConnectorError(
                "Akeneo has no default locale — set the connection's Source locale (e.g. "
                "en_US) to the authoring locale you translate FROM."
            )
        return self.resolve_locale(cfg)

    # ---------------------------------------------------------- attribute schema
    def _attribute_schema(self) -> dict:
        """{attr_code: {"type": str, "localizable": bool, "scopable": bool}} for every
        attribute on the instance. Cached. Selection is schema-EXACT off this (the token can
        read /attributes), not a name/value heuristic."""
        if self._schema_cache is not None:
            return self._schema_cache
        with self._client() as c:
            items = self._paginate_raw(c, "/attributes")
        schema: dict = {}
        for a in items:
            code = a.get("code")
            if not code:
                continue
            schema[str(code)] = {
                "type": str(a.get("type") or ""),
                "localizable": bool(a.get("localizable")),
                "scopable": bool(a.get("scopable")),
            }
        self._schema_cache = schema
        return schema

    # ---------------------------------------------------------- content types
    def _content_types(self) -> list[str]:
        raw = (self.config.get("content_types") or "").strip()
        if not raw:
            return list(_CONTENT_TYPES)
        cts = _parse_names(raw)
        out: list[str] = []
        for ct in cts:
            c = ct.strip()
            if c not in _CONTENT_TYPES:
                raise ConnectorError(
                    f"Unknown Akeneo content type {ct!r}: must be one of "
                    f"{', '.join(_CONTENT_TYPES)}."
                )
            if c not in out:
                out.append(c)
        return out

    def _validate_ct(self, ct: str, cts: list[str] | None = None) -> str:
        """Fail closed (defect taxonomy A / Strapi H1): a caller-named content type must be in
        the FIXED {product, product_model} allowlist AND configured on this connection. A value
        that is merely a PREFIX of a valid type (e.g. 'prod') is refused, not matched."""
        c = str(ct).strip()
        if c not in _CONTENT_TYPES:
            raise ConnectorError(
                f"Unknown Akeneo content type {ct!r}: must be exactly one of "
                f"{', '.join(_CONTENT_TYPES)}."
            )
        cts = cts if cts is not None else self._content_types()
        if c not in cts:
            raise ConnectorError(
                f"Content type {ct!r} is not configured on this connection "
                f"({', '.join(cts)}) - refusing to target it."
            )
        return c

    def _endpoint(self, ct: str) -> str:
        return _ENDPOINT[ct]

    def _split_aid(self, record_id: str) -> tuple[str, str]:
        """('<contentType>:<uuidOrCode>') -> (validated ct, validated id). The content-type
        half is allowlist-validated (taxonomy A); the id half is segment-validated + is
        percent-encoded at every call site."""
        ct, sep, ident = str(record_id).partition(":")
        if not sep or not ct or not ident:
            raise ConnectorError(
                f"Malformed Akeneo record id {record_id!r} (want '<contentType>:<uuidOrCode>')."
            )
        return self._validate_ct(ct), _validate_segment(ident, "record id")

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            locs = self._fetch_locales()
            return True, f"Connected to Akeneo ({len(locs)} enabled locale(s))."
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

    # ------------------------------------------------------------- discovery
    def _normalize(self, ct: str, item: dict) -> dict:
        item = item or {}
        ident = str(item.get(_ID_FIELD[ct]) or "")
        values = item.get("values") or {}
        aid = f"{ct}:{ident}"
        if ident:
            self._snapshot[aid] = values  # discovery snapshot for the created-vs-updated flag
        return {
            "_ct": ct,
            "id": ident,
            "aid": aid,
            "values": values,
            "updated": item.get("updated"),
        }

    def _paginate(self, c, ct: str, cap: int) -> list[dict]:
        endpoint = self._endpoint(ct)
        out: list[dict] = []
        page = 1
        limit = min(100, max(1, cap))
        while len(out) < cap:
            r = self._get(
                c, "/" + endpoint, params={"page": page, "limit": limit, "with_count": "false"}
            )
            if r.status_code != 200:
                raise ConnectorError(
                    f"Akeneo list {endpoint} failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            items = ((r.json() or {}).get("_embedded") or {}).get("items") or []
            if not items:
                break
            out.extend(self._normalize(ct, it) for it in self._keep_rows(items))
            if len(items) < limit:
                break
            page += 1
        return out[:cap]

    def _fetch_one(self, c, ct: str, ident: str) -> dict | None:
        endpoint = self._endpoint(ct)
        r = self._get(c, "/" + endpoint + "/" + quote(ident, safe=""))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(
                f"Akeneo get {endpoint}/{ident} failed ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        return self._normalize(ct, r.json() or {})

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source records to translate. Explicit article_ids ('<contentType>:<uuidOrCode>')
        win and are SCOPE-VALIDATED (the content-type half against the {product,
        product_model} allowlist + this connection's configured set, the id half segment-
        validated — taxonomy A). Else each configured content type is listed and the shared
        `cap` is filled ROUND-ROBIN across types, so products can never starve product models
        (taxonomy E)."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        cts = self._content_types()
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                for raw in list(article_ids)[:cap]:
                    ct, ident = self._split_aid(str(raw))  # validates ct + id, fail-closed
                    rec = self._fetch_one(c, ct, ident)
                    if rec is not None:
                        out.append(self._charge_record(rec))
                return out
            per_type = [self._paginate(c, ct, cap) for ct in cts]
            records: list[dict] = []
            i = 0
            while len(records) < cap and any(i < len(lst) for lst in per_type):
                for lst in per_type:
                    if i < len(lst):
                        records.append(lst[i])
                        if len(records) >= cap:
                            break
                i += 1
            return records

    # ------------------------------------------------------------ selection
    def _source_value(self, triples, src: str, scope) -> str | None:
        """The source-locale string value for one attribute's triple list, at (locale=src,
        scope=scope). scope is None for a non-scopable attribute (its triple carries
        scope:null) and the configured channel for a scopable one. Returns the data only when
        it is a non-empty string (a metric/price/select data is a dict/list/code and never
        matches a translatable-type attribute anyway)."""
        for t in triples or []:
            if str(t.get("locale") or "").strip() != src:
                continue
            tscope = t.get("scope")
            if scope is None:
                if tscope not in (None, ""):
                    continue
            elif str(tscope or "").strip() != scope:
                continue
            data = t.get("data")
            if isinstance(data, str) and data.strip():
                return data
        return None

    def _field_selection(self, values: dict) -> tuple[list[tuple], dict]:
        """([(attr, source_text), ...], {skipped_attr: reason}). SCHEMA-DRIVEN off /attributes:
        a candidate is a localizable pim_catalog_text/textarea attribute with a non-empty
        source-locale value. Everything else is skipped WITH a reason (taxonomy D)."""
        schema = self._attribute_schema()
        channel = self._channel()
        src = self._source_locale()
        selected: list[tuple] = []
        skipped: dict = {}
        for attr in sorted(values or {}):
            meta = schema.get(attr)
            if meta is None:
                skipped[attr] = "attribute not found in the Akeneo attribute schema"
                continue
            atype = meta["type"]
            if not meta["localizable"]:
                skipped[attr] = f"attribute is not localizable in Akeneo (type {atype})"
                continue
            if atype not in _TRANSLATABLE_TYPES:
                # localizable but a select/media/date/number/etc: its VALUE is not free text.
                # Loud once (option-label localization is a deferred, separate unit).
                skipped[attr] = (
                    f"attribute type {atype} is not translatable text (only "
                    f"pim_catalog_text/textarea; option-label localization is a "
                    f"deferred build)"
                )
                continue
            if meta["scopable"] and not channel:
                skipped[attr] = (
                    "scopable attribute but no channel is configured on the "
                    "connection - set the Channel field to translate it"
                )
                continue
            scope = channel if meta["scopable"] else None
            val = self._source_value(values.get(attr) or [], src, scope)
            if not val:
                skipped[attr] = "no non-empty source-locale value on this record"
                continue
            selected.append((attr, val))
        return selected, skipped

    def extract(self, article: dict):
        """(body_units, chash, body_attrs). No flat title exists on an Akeneo record (the
        identifier-attribute analogue needs per-catalog resolution, deferred), so every
        translatable attribute is a body unit (order 1+); there is no title (order 0)."""
        values = article.get("values") or {}
        selected, skipped = self._field_selection(values)
        self.sel.record_selection([a for a, _ in selected], skipped)
        body_units = [_Unit(text, attr) for attr, text in selected]
        body_attrs = [attr for attr, _ in selected]
        chash = _content_hash(*[text for _, text in selected])
        return body_units, chash, body_attrs

    # ------------------------------------- client the T1 strategy drives
    def record_values(self, record_id: str):
        """The LIVE `values` dict for the record (a fresh GET), or strategy.ABSENT on 404.
        Used ONLY by the existence probe — never to build a write body."""
        ct, ident = self._split_aid(record_id)
        with self._client() as c:
            rec = self._fetch_one(c, ct, ident)
        if rec is None:
            return ABSENT
        return rec.get("values") or {}

    def locale_present(self, values: dict, exact_locale: str) -> bool:
        """True iff the exact locale appears on ANY translatable attribute's value triples in
        `values`. Consulted by exists_target (a live read) — an explicit presence signal."""
        schema = self._attribute_schema()
        for attr, triples in (values or {}).items():
            meta = schema.get(attr)
            if not meta or not meta["localizable"] or meta["type"] not in _TRANSLATABLE_TYPES:
                continue
            for t in triples or []:
                if str(t.get("locale") or "").strip() == exact_locale:
                    return True
        return False

    def snapshot_locale_present(self, record_id: str, exact_locale: str) -> bool:
        """created-vs-updated helper: was the locale already present in THIS run's DISCOVERY
        snapshot (fetched before any write)? Purely local — no network read — so write_target
        stays read-free for the merge."""
        values = self._snapshot.get(str(record_id))
        if not values:
            return False
        return self.locale_present(values, exact_locale)

    def build_values(self, fields: dict, exact_locale: str) -> dict:
        """The minimal Akeneo values dict {attr: [{locale, scope, data}]} for this run's
        translated attributes ONLY. A scopable attribute carries the configured channel as its
        scope; a non-scopable one carries scope:null. Built ENTIRELY from `fields` (no read):
        the server-side per-triple merge preserves every other locale/scope/attribute."""
        schema = self._attribute_schema()
        channel = self._channel()
        out: dict = {}
        for attr, text in (fields or {}).items():
            meta = schema.get(attr)
            if meta is None:
                continue  # selection guaranteed presence; defensive skip
            scope = channel if meta["scopable"] else None
            if meta["scopable"] and not scope:
                continue  # scopable w/o channel (selection excluded it)
            out[attr] = [
                {
                    "locale": exact_locale if meta["localizable"] else None,
                    "scope": scope,
                    "data": text,
                }
            ]
        return out

    def patch_record(self, record_id: str, values: dict) -> _PatchOutcome:
        """PATCH the minimal values dict to the record. NO read-before-write. Idempotent
        (a re-sent identical triple set yields the same server state), so base._request may
        safely retry it on 5xx/429."""
        ct, ident = self._split_aid(record_id)
        endpoint = self._endpoint(ct)
        body = {"values": values}
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "PATCH",
                    self._base() + "/" + endpoint + "/" + quote(ident, safe=""),
                    json=body,
                    headers=self._headers(write=True),
                )
        except httpx.TransportError as e:
            return _PatchOutcome(False, message=f"{type(e).__name__}: {e}")
        if r.status_code in (200, 201, 204):
            return _PatchOutcome(True, status=r.status_code)
        return _PatchOutcome(
            False, message=f"{r.status_code}: {vendor_excerpt(r.text)}", status=r.status_code
        )

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for art in self.discover_articles():
            selected, _sk = self._field_selection(art.get("values") or {})
            aid = art["aid"]
            label = f"{art.get('_ct')} {art.get('id')}"[:60]
            for attr, text in selected:
                out.append(PulledItem(external_id=aid, field=attr, text=text, title=label))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Akeneo writes per-locale attribute values - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (no watermark): the content-hash gate is the change detector.
# ===========================================================================
def _akeneo_preflight(ctx):
    conn = ctx.connector
    enabled = set(conn.enabled_locales())
    src = conn._source_locale().lower()
    # A source locale not enabled on the instance would silently yield "no source value" on
    # every record and stay CONNECTED forever with no stated cause. Fail LOUD (Contentful M4).
    if src not in enabled:
        raise ConnectorError(
            f"Source locale {src!r} is not enabled on this Akeneo instance (enabled: "
            f"{', '.join(sorted(enabled)) or 'none'}). Set Source locale to an enabled locale."
        )
    # M1: PRE-LOAD + VALIDATE the attribute schema HERE (inside stage-3's try), so a token
    # that can read locales/products but NOT catalog structure fails as a clean (False, msg)
    # instead of an uncaught ConnectorError escaping the per-article prepare() loop. It is
    # cached, so extract() reuses it (no per-article re-fetch). An UNREADABLE schema
    # propagates the API 401/403 (via _paginate_raw); a 200-but-EMPTY structure list while
    # products exist is an ERROR STATE (an empty schema silently skips every real attribute
    # and returns a green zero-write run), NOT a benign no-op. A NON-empty schema with no
    # localizable text attribute is DISTINCT and stays benign (records skip with clear
    # per-attribute reasons) — only a truly empty structure list fails here.
    schema = conn._attribute_schema()
    if not schema:
        raise ConnectorError(
            "Could not load the Akeneo attribute schema: GET /attributes returned no "
            "attribute definitions. A catalog with products to translate always defines "
            "attributes - check the access token's rights to read catalog structure."
        )
    # L2: a configured channel that does not exist would surface as per-pair 422s at write
    # time; confirm it exists at preflight and fail loud otherwise.
    channel = conn._channel()
    if channel:
        codes = conn._channel_codes()
        if channel not in codes:
            raise ConnectorError(
                f"Channel {channel!r} is not defined on this Akeneo instance (channels: "
                f"{', '.join(sorted(codes)) or 'none'}). Fix the Channel config or clear it."
            )
    return enabled, {"source": src}


def _akeneo_partition(locales, enabled, extra):
    """usable = enabled targets minus the source locale; rejected = not enabled. The source
    locale is DROPPED (not rejected) - surfaced honestly in the message."""
    src = (extra or {}).get("source", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _akeneo_prepare(ctx, art):
    body_units, chash, body_attrs = ctx.connector.extract(art)
    if not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = art["aid"]
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text="",
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": None, "content_type": art.get("_ct")},
        doc_title=f"{art.get('_ct')} {art.get('id')}"[:60],
        extra={"body_attrs": body_attrs, "connector": ctx.connector},
    )


def _akeneo_is_suppressed(connector, aid, loc):
    # TAXONOMY C: the probe reads the LIVE record (the same state a PATCH writes, no
    # draft/publish split in CE), so a second run over unchanged source is fully skipped.
    return connector.strategy.exists_target(connector, aid, loc)


def _akeneo_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one locale: {attr_code: translatedValue} for the attributes
    actually translated this run. A HELD-BACK (NEEDS_REVIEW) attribute is OMITTED entirely
    (never source-seeded), so Akeneo's partial PATCH keeps its existing stored value; when the
    dict is empty the strategy writes NOTHING (taxonomy B/F). There is no title (order 0), so
    seg0_target is unused."""
    out: dict = {}
    for attr in prep.extra["body_attrs"]:
        if attr in mapping:
            out[attr] = mapping[attr]
    # M1 visibility: attributes that WERE candidates but got held back this run, recorded ONLY
    # on a PARTIAL write (out non-empty). An all-held write returns {} and stays the taxonomy-F
    # held_back path (no hash, re-attempted), so it must not be double-counted as incomplete.
    if out:
        held = {
            a: "awaiting review (NEEDS_REVIEW)" for a in prep.extra["body_attrs"] if a not in out
        }
        connector = prep.extra.get("connector")
        if held and connector is not None:
            connector.sel.record_held_back(prep.aid, loc, held)
    return out


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _held_back(ctx):
    """(record, locale) pairs held back for review this run - their OWN counter, never counted
    as created/updated and never a hard failure (taxonomy B/F)."""
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _akeneo_compute_ok(ctx):
    if not ctx.usable:
        return False  # nothing translatable (all rejected and/or all source-equal)
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _akeneo_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} per-locale write(s) (each a "
        f"partial attribute-value PATCH) across {len(ctx.usable)} locale(s) from "
        f"{len(ctx.articles)} record(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} record(s) with no translatable attributes."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not enabled in Akeneo: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source locale "
            f"({', '.join(dropped)}) - configure different target locales."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (record, locale) pair(s) awaiting review "
            f"(NEEDS_REVIEW) - nothing written; they will be re-attempted next run."
        )
    sel = ctx.connector.sel.selection_summary()
    if sel:
        parts.append(sel)
    real = _real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("record write(s)").strip())
        log.warning(
            "akeneo_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _akeneo_build_result(ctx):
    return {
        "records": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "incomplete": [
            {"article_id": a, "locale": l, "held_fields": sorted(h.keys())}
            for (a, l), h in ctx.connector.sel.held_back_pairs().items()  # noqa: E741
        ],
        "fields_selected": ctx.connector.sel.selected_sorted(),
        "fields_skipped": ctx.connector.sel.skipped_dict(),
        "failures": _real_failures(ctx),
    }
