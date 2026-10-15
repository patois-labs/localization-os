"""Plytix PIM connector: a T1 (attribute map) consumer of the connector framework.

Built from Plytix vendor docs (apidocs.plytix.com, help.plytix.com) and the MIT-licensed
community API notes at github.com/Supplyline/plytix-mcp (docs/solutions/api-quirks/plytix-api.md)
for endpoint shapes only; no code was copied and no bb-io code was read (bb-io has no Plytix
repo). Live-tenant QA covers what the offline fake could only guess.

LOCALE MODEL (re-derived from the platform, not trusted from a label). Plytix has NO locale
dimension. A product is `sku`, `label` and a flat map of attribute label -> value. A translation
lives in a SEPARATE attribute per language (Plytix's own playbook: a text attribute such as
"Spanish description" next to "description"). So this is T1 with an operator-configured map
{source_attr: {locale: target_attr}}, the same shape as the Airtable field map. The whole
correlation lives inside the single product record: no sub-resource to probe (T2) and no
document-per-locale of our own to track (T3).

WRITE SHAPE. `PATCH /api/v2/products/{id}` with `{"attributes": {target: text}}`. The body is
built ONLY from this run's translated target attributes. The source attribute is NEVER in a write
body: the map is validated at load (no target may equal any source, targets are unique, system
fields are refused) and `build_values` re-checks it, so a mis-edited map cannot overwrite source
copy. Every target label must also be unambiguous under every alias Plytix might accept for an
attribute (label, display name, id; case, spaces and hyphens ignored), checked against the live
schema before any write.

MERGE CANARY (review gate). Public docs do not prove that this PATCH MERGES the named attributes
into the product; if it REPLACED the attribute set, one write would wipe every other attribute of
the customer's product. So until merge semantics are proven for a connection, the FIRST write of
every run is a canary: GET the whole product, PATCH, GET again and compare every attribute that
was not written (plus sku and label). Any attribute that had a value and lost it stops the run at
once (no further write, no further MT), the product is PATCHed back from the pre-write snapshot
and re-checked, the run reports a SAFETY STOP naming what was and was not restored, and the
connection records `replace_detected` so later runs refuse to write. A clean canary records
`merge_proven` on the connection (config key `plytix_patch_semantics`), after which runs skip
it. A canary that cannot verify (no other attribute with a value to watch, or the re-read fails)
fails closed.

HELD BACK (#640, taxonomy B/F). A unit with no usable translation (NEEDS_REVIEW, refusal, empty)
is OMITTED from the PATCH body; the stored target value is kept. An all-held pair writes nothing
and reports held-back (ok=False, no content hash). A pair is never CREATED while a unit is held
(the flow's create gate, `variant_exists`), so a half-filled language never gets hashed as done.

AUTH. An API key + API password are exchanged for a bearer token at
https://auth.plytix.com/auth/api/get-token (token lifetime 15 minutes; re-minted at 13 minutes and
once on a 401). Credentials and the token never appear in a log line, an error message or a
result. Both hosts are module constants (fixed-host kind), still run through `_validate_target`.

RATE LIMITS. Plytix enforces per-key windows (request count per 10 seconds plus an hourly cap per
plan) and answers 429. Every call goes through `Connector._request`, which backs off on 429/5xx.

SCOPE. Only the attributes named in the map are read or written; the optional `skus` setting
restricts the products in scope; a product id supplied by a caller is matched against a strict
pattern and, when `skus` is set, against that list (a foreign id answers like a missing one).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.services.connectors import flow, harvest, webhooks  # noqa: F401 (parity imports)
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
from app.services.connectors.base import content_hash as _content_hash
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

API_VERSION = "v2"
_API_ROOT = "https://pim.plytix.com"
_AUTH_URL = "https://auth.plytix.com/auth/api/get-token"
_TOKEN_REFRESH_AFTER = 13 * 60  # tokens live 15 minutes
_TEXT_TYPE = "TextAttribute"
_PAGE = 100  # Plytix max page_size
_MAX_MAPPED = 40  # search allows 50 requested attributes; keep headroom for label/modified
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABEL_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_LOCALE_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}")
_RESERVED = frozenset(
    {
        "id",
        "sku",
        "label",
        "gtin",
        "status",
        "created",
        "modified",
        "thumbnail",
        "categories",
        "assets",
        "relationships",
        "product_family_id",
        "product_type",
    }
)
_SEMANTICS_KEY = "plytix_patch_semantics"  # {"merge_proven": iso} / {"replace_detected": iso}
_SAFETY_PREFIX = "SAFETY STOP:"
_TOP_LEVEL_WATCHED = ("sku", "label")
_HELD_BACK_PREFIX = "held-back:"
_HELD_BACK = (
    _HELD_BACK_PREFIX + " every mapped attribute is awaiting review (NEEDS_REVIEW); "
    "nothing written, existing translation (if any) kept"
)


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _validate_id(value) -> str:
    """A Plytix product id reaches a URL path. Strict allowlist, fail closed (taxonomy A):
    no '.', '/', '?', '#', '%', whitespace, control characters or unicode."""
    v = value if isinstance(value, str) else str(value or "")
    if not _ID_RE.fullmatch(v):
        raise ConnectorError(
            f"Unsafe Plytix product id {value!r}: want letters, digits, '_' or '-'."
        )
    return v


def _present(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _has_value(v) -> bool:
    """Any stored value worth protecting (0 and False count; None, '', [] and {} do not)."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict, tuple)):
        return bool(v)
    return True


def _fold_alias(v) -> str:
    """One attribute alias, compared the way a lenient API might: ignoring case, with spaces
    and hyphens read as underscores (display name 'Description FR' ~ label 'description_fr')."""
    return re.sub(r"[\s-]+", "_", str(v or "").strip()).casefold()


class _Unit:
    """A minimal flow translation unit; p_index is the SOURCE attribute label."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


SPEC = ConnectorSpec(
    kind="plytix",
    name="Plytix",
    icon="🧩",
    cat="PIM",
    blurb="Translate Plytix PIM product text attributes into any language. Plytix has no locale "
    "dimension, so each language lives in its own attribute (for example description_fr); you "
    "map each source attribute to one target attribute per language, and each translation is "
    "written to that target attribute only. The source attribute is never written.",
    config_fields=[
        FieldSpec(
            "attribute_map",
            "Attribute map (JSON)",
            'Which source attribute goes to which per-language attribute, e.g. {"description": '
            '{"fr": "description_fr", "de": "description_de"}}. Use attribute LABELS (not display '
            "names). Create the target attributes in Plytix first. Every source needs the same "
            "languages.",
            placeholder='{"description": {"fr": "description_fr"}}',
        ),
        FieldSpec(
            "source_locale",
            "Source language",
            "The language the source attributes are written in, e.g. en. A target equal to it "
            "is skipped.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "skus",
            "Only these SKUs",
            "Optional comma-separated SKUs to restrict the run to. Blank = every product (up to "
            "the record cap).",
            required=False,
            placeholder="MUG-001, MUG-002",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated languages to write; each must appear in the attribute map.",
            required=False,
            placeholder="fr, de",
        ),
        FieldSpec(
            "max_entries",
            "Max records",
            "Safety cap on products scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_key", "API key", "From Plytix Admin, API tab.", secret=True, placeholder="API key"
        ),
        FieldSpec(
            "api_password",
            "API password",
            "The password shown next to the API key.",
            secret=True,
            placeholder="API password",
        ),
    ],
    setup_steps=[
        "In Plytix, create one text attribute per target language (for example description_fr) "
        "and add it to the product family.",
        "In accounts.plytix.com open the API tab, create an API key with a role that can read "
        "and edit products and attributes, and copy the key and password.",
        "Paste the key and password here, then fill in the Attribute map with your attribute "
        "labels.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Re-translation is gated on a "
    "per-(product, language) content hash plus a check that the target attribute is filled. "
    "A target attribute you edit by hand is kept while the source text is unchanged, but it is "
    "overwritten when the source text changes. Until Plytix is proven to keep a product's other "
    "attributes on a write, the first write of each run is checked: if anything else on that "
    "product was lost, the run stops, the product is put back and further writes are refused. "
    "Plytix has no webhook receiver in scope for v1.",
    docs_url="https://apidocs.plytix.com/",
    supports_oauth=False,
)


class PlytixAttributeStrategy(LocaleMappingStrategy):
    """T1 write-back for Plytix: one partial PATCH of the mapped target attributes. The stock
    FieldMapStrategy (a locale-keyed map inside one field) does not fit: Plytix has no locale
    map, each language is its own attribute."""

    topology = "T1"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True iff ANY mapped target attribute for this language is filled on the LIVE record
        (the same state a PATCH writes: Plytix has no draft/publish, taxonomy C)."""
        values = client.live_values(record_id)
        if values is ABSENT or not isinstance(values, dict):
            return False
        return client.any_target_filled(values, target_locale)

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        if not fields:
            # Every unit held back: write nothing, no hash (taxonomy B/F).
            return WriteResult(False, False, message=_HELD_BACK)
        lock = _record_write_lock_for(getattr(client, "connection_id", None), record_id)
        with lock:
            existed = client.snapshot_target_filled(record_id, target_locale)
            values = client.build_values(fields, target_locale)
            if not values:
                return WriteResult(
                    False, not existed, message="no writable target attribute for this language"
                )
            ok, message = client.guarded_patch(record_id, values)
            if ok:
                return WriteResult(
                    True,
                    not existed,
                    target_id=str(record_id),
                    message="created" if not existed else "updated",
                )
            return WriteResult(False, not existed, message=message)


class PlytixConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="plytix",
        topology=Topology.T1,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.NONE,
        regions=_API_ROOT,
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="modified", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = PlytixAttributeStrategy()
        self._token: str | None = None
        self._token_at = 0.0
        self._map_cache: dict | None = None
        self._schema_cache: dict | None = None
        self._snapshot: dict = {}
        self._alias_rows: list[tuple[str, str, str]] = []  # (label, name, id) per attribute
        self._canary_lock = threading.Lock()
        self._canary_passed = False
        self.safety_stop: dict | None = None
        self.sel = SelectionTracker(
            noun="attribute", label="plytix", loud_markers=("is not plain text",)
        )

    # ----------------------------------------------------------------- config
    def _base(self) -> str:
        """The fixed API root (a module constant; no config field can repoint it)."""
        return _validate_target(_API_ROOT)

    def attribute_map(self) -> dict[str, dict[str, str]]:
        """{source_attr: {locale(lowercase): target_attr}}, validated (taxonomy A applies to
        operator config too). Fail closed: any problem refuses the whole map."""
        if self._map_cache is not None:
            return self._map_cache
        raw = self.config.get("attribute_map")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                raise ConnectorError("Plytix Attribute map is required (JSON).")
            try:
                raw = json.loads(raw)
            except ValueError as e:
                raise ConnectorError(f"Plytix Attribute map is not valid JSON: {e.msg}.") from None
        if not isinstance(raw, dict) or not raw:
            raise ConnectorError(
                'Plytix Attribute map must be a JSON object like {"description": '
                '{"fr": "description_fr"}}.'
            )
        out: dict[str, dict[str, str]] = {}
        targets: set[str] = set()
        folded_sources: set[str] = set()  # case-insensitive: Description == description
        locale_sets: list[frozenset] = []
        for src, per in raw.items():
            if (
                not isinstance(src, str)
                or not _LABEL_RE.fullmatch(src)
                or src.casefold() in _RESERVED
            ):
                raise ConnectorError(f"Plytix Attribute map: bad source attribute label {src!r}.")
            if src.casefold() in folded_sources:
                raise ConnectorError(
                    f"Plytix Attribute map: source {src!r} repeats another source (labels are "
                    "compared ignoring case)."
                )
            folded_sources.add(src.casefold())
            if not isinstance(per, dict) or not per:
                raise ConnectorError(
                    f"Plytix Attribute map: {src!r} needs a language -> target object."
                )
            row: dict[str, str] = {}
            for loc, tgt in per.items():
                if not isinstance(loc, str) or not _LOCALE_RE.fullmatch(loc.strip()):
                    raise ConnectorError(f"Plytix Attribute map: bad language code {loc!r}.")
                if (
                    not isinstance(tgt, str)
                    or not _LABEL_RE.fullmatch(tgt)
                    or tgt.casefold() in _RESERVED
                ):
                    raise ConnectorError(
                        f"Plytix Attribute map: bad target label {tgt!r} for {src!r}/{loc}."
                    )
                key = loc.strip().lower()
                if key in row:
                    raise ConnectorError(
                        f"Plytix Attribute map: language {loc!r} repeated for {src!r}."
                    )
                if tgt.casefold() in targets:
                    raise ConnectorError(
                        f"Plytix Attribute map: target {tgt!r} is used twice (one source each)."
                    )
                targets.add(tgt.casefold())
                row[key] = tgt
            out[src] = row
            locale_sets.append(frozenset(row))
        overlap = targets & folded_sources
        if overlap:
            raise ConnectorError(
                "Plytix Attribute map: "
                + ", ".join(sorted(overlap))
                + " is both a source and a target; a source attribute is never written."
            )
        if len(set(locale_sets)) != 1:
            raise ConnectorError(
                "Plytix Attribute map: every source attribute must map the same languages."
            )
        if len(out) + len(targets) > _MAX_MAPPED:
            raise ConnectorError(
                f"Plytix Attribute map names too many attributes (max {_MAX_MAPPED})."
            )
        self._map_cache = out
        return out

    def map_locales(self) -> list[str]:
        m = self.attribute_map()
        return sorted(next(iter(m.values())))

    def _source_locale(self) -> str:
        return str(self.config.get("source_locale") or "").strip().lower()

    def _sku_scope(self) -> list[str]:
        return _parse_names(self.config.get("skus"))

    def _all_labels(self) -> list[str]:
        m = self.attribute_map()
        return sorted(set(m) | {t for per in m.values() for t in per.values()})

    # ------------------------------------------------------------------- http
    def _mint_token(self, c) -> str:
        key = (self.secret.get("api_key") or "").strip()
        pw = (self.secret.get("api_password") or "").strip()
        if not key or not pw:
            raise ConnectorError("Plytix API key and API password are required.")
        _validate_target(_AUTH_URL)
        r = self._request(
            c,
            "POST",
            _AUTH_URL,
            json={"api_key": key, "api_password": pw},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if r.status_code in (400, 401, 403):
            raise ConnectorError(
                f"Plytix rejected the API key/password (HTTP {r.status_code}). Check both in "
                "Admin > API."
            )
        if r.status_code != 200:
            raise ConnectorError(f"Plytix token request failed (HTTP {r.status_code}).")
        try:
            tok = ((r.json() or {}).get("data") or [{}])[0].get("access_token")
        except (ValueError, AttributeError, IndexError, TypeError):
            tok = None
        if not tok or not isinstance(tok, str):
            raise ConnectorError("Plytix token response carried no access_token.")
        self._token, self._token_at = tok, time.monotonic()
        return tok

    def _bearer(self, c) -> str:
        if self._token and (time.monotonic() - self._token_at) < _TOKEN_REFRESH_AFTER:
            return self._token
        return self._mint_token(c)

    def _api(self, c, method: str, path: str, **kw) -> httpx.Response:
        _validate_target(_API_ROOT)
        url = _API_ROOT + path
        for attempt in (0, 1):
            headers = {
                "Authorization": f"Bearer {self._bearer(c)}",
                "Accept": "application/json",
            }
            if method in ("POST", "PATCH", "PUT"):
                headers["Content-Type"] = "application/json"
            r = self._request(c, method, url, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:
                self._token = None  # expired mid-run: mint once more
                continue
            return r
        return r  # pragma: no cover

    @staticmethod
    def _data(r: httpx.Response) -> list:
        try:
            body = r.json()
        except ValueError:
            return []
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    def _fail(self, what: str, r: httpx.Response) -> ConnectorError:
        if r.status_code in (401, 403):
            return ConnectorError(
                f"Plytix refused {what} ({r.status_code}): the API key's role needs access to it."
            )
        return ConnectorError(f"Plytix {what} failed ({r.status_code}): {vendor_excerpt(r.text)}")

    # ----------------------------------------------------------------- schema
    def _attribute_schema(self) -> dict:
        """{label: type_class} for every product attribute (v1 attribute search)."""
        if self._schema_cache is not None:
            return self._schema_cache
        schema: dict = {}
        with self._client() as c:
            for page in range(1, 51):
                r = self._api(
                    c,
                    "POST",
                    "/api/v1/attributes/product/search",
                    json={
                        "attributes": ["label", "name", "type_class"],
                        "pagination": {"page": page, "page_size": _PAGE},
                    },
                )
                if r.status_code != 200:
                    raise self._fail("the attribute list", r)
                rows = self._data(r)
                for a in rows:
                    if isinstance(a, dict) and a.get("label"):
                        schema[str(a["label"])] = str(a.get("type_class") or "")
                        self._alias_rows.append(
                            (str(a["label"]), str(a.get("name") or ""), str(a.get("id") or ""))
                        )
                if len(rows) < _PAGE:
                    break
        self._schema_cache = schema
        return schema

    def validate_map_against_schema(self) -> dict[str, str]:
        """Fail loud on config the live schema contradicts; return {source: skip reason} for
        sources that are real but not plain text (reported, not written)."""
        schema = self._attribute_schema()
        if not schema:
            raise ConnectorError(
                "Plytix returned no product attributes: check the key's role can read them."
            )
        m = self.attribute_map()
        problems: list[str] = []
        skipped: dict[str, str] = {}
        for src, per in m.items():
            stype = schema.get(src)
            if stype is None:
                problems.append(f"source attribute {src!r} does not exist")
                continue
            if stype != _TEXT_TYPE:
                skipped[src] = f"attribute type {stype} is not plain text (only {_TEXT_TYPE})"
            for loc, tgt in per.items():
                ttype = schema.get(tgt)
                if ttype is None:
                    problems.append(f"target attribute {tgt!r} ({loc}) does not exist")
                elif ttype != _TEXT_TYPE:
                    problems.append(
                        f"target attribute {tgt!r} ({loc}) is {ttype}, not {_TEXT_TYPE}"
                    )
        problems.extend(self._alias_problems(m))
        if problems:
            raise ConnectorError(
                "Plytix Attribute map does not match your account: " + "; ".join(problems) + "."
            )
        return skipped

    def _alias_problems(self, m: dict) -> list[str]:
        """A target label must name exactly ONE attribute under every alias Plytix might accept
        for an attribute: its label, its display name and its id, ignoring case and reading
        spaces or hyphens as underscores. A target that is also an alias of any OTHER attribute
        (above all a SOURCE attribute's display name or id) is refused, so no lenient resolution
        on the Plytix side can turn a target write into a write of the source or of any other
        attribute."""
        aliases: dict[str, set[str]] = {}
        for label, name, aid in self._alias_rows:
            for a in (label, name, aid):
                if a:
                    aliases.setdefault(_fold_alias(a), set()).add(label)
        problems: list[str] = []
        sources = set(m)
        for per in m.values():
            for loc, tgt in per.items():
                others = sorted(aliases.get(_fold_alias(tgt), set()) - {tgt})
                if not others:
                    continue
                what = (
                    "the SOURCE attribute"
                    if any(o in sources for o in others)
                    else "another attribute"
                )
                problems.append(
                    f"target {tgt!r} ({loc}) is also a label, name or id of {what} "
                    f"{', '.join(repr(o) for o in others)}; use an unambiguous target label"
                )
        return problems

    # ------------------------------------------------------------ test / list
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                self._mint_token(c)
            schema = self._attribute_schema()
            note = ""
            if str(self.config.get("attribute_map") or "").strip():
                self.validate_map_against_schema()
                note = " Attribute map checked."
            return True, f"Connected to Plytix ({len(schema)} product attribute(s)).{note}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        return [{"id": "products", "name": "products", "tables": []}]

    # -------------------------------------------------------------- discovery
    @staticmethod
    def _cell(row: dict, label: str):
        attrs = row.get("attributes")
        if isinstance(attrs, dict) and label in attrs:
            return attrs[label]
        return row.get(f"attributes.{label}")

    def _normalize(self, row: dict) -> dict | None:
        pid = row.get("id")
        if not isinstance(pid, str) or not _ID_RE.fullmatch(pid):
            return None
        values = {lb: self._cell(row, lb) for lb in self._all_labels()}
        sku = row.get("sku")
        scope = self._sku_scope()
        if scope and sku not in scope:
            return None
        self._snapshot[pid] = values
        return {
            "id": pid,
            "aid": pid,
            "sku": sku or "",
            "values": values,
            "updated": row.get("modified"),
        }

    def _get_row(self, c, pid: str) -> dict | None:
        """The WHOLE product as Plytix returns it (every attribute), or None when missing."""
        r = self._api(c, "GET", "/api/v2/products/" + quote(pid, safe=""))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise self._fail(f"product {pid}", r)
        rows = self._data(r)
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        if row is None or row.get("id") != pid:
            return None
        return row

    def _fetch_one(self, c, pid: str) -> dict | None:
        row = self._get_row(c, pid)
        return None if row is None else self._normalize(row)

    def _search_all(self, c, cap: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        size = min(_PAGE, max(1, cap))
        cols = ["label", "modified"] + [f"attributes.{lb}" for lb in self._all_labels()]
        body: dict = {"attributes": cols}
        scope = self._sku_scope()
        if scope:
            body["filters"] = [[{"field": "sku", "operator": "in", "value": scope}]]
        while len(out) < cap and page <= 1000:
            body["pagination"] = {"page": page, "page_size": size}
            r = self._api(c, "POST", "/api/v2/products/search", json=body)
            if r.status_code != 200:
                raise self._fail("the product search", r)
            rows = [x for x in self._data(r) if isinstance(x, dict)]
            for row in self._keep_rows(rows):
                rec = self._normalize(row)
                if rec is not None:
                    out.append(rec)
            if len(rows) < size:
                break
            page += 1
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Explicit ids win and are validated (taxonomy A); else products are listed with only
        the mapped attribute columns requested, honouring the optional SKU scope."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        self.attribute_map()
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                for raw in list(article_ids)[:cap]:
                    pid = _validate_id(raw)
                    rec = self._fetch_one(c, pid)
                    if rec is not None:
                        out.append(self._charge_record(rec))
                return out
            return self._search_all(c, cap)

    # -------------------------------------------------------------- selection
    def _field_selection(self, values: dict, skipped_types: dict | None = None):
        selected: list[tuple] = []
        skipped: dict = {}
        for src in sorted(self.attribute_map()):
            if skipped_types and src in skipped_types:
                skipped[src] = skipped_types[src]
                continue
            val = (values or {}).get(src)
            if not _present(val):
                skipped[src] = "no non-empty source value on this product"
                continue
            selected.append((src, val))
        return selected, skipped

    def extract(self, article: dict):
        selected, skipped = self._field_selection(
            article.get("values") or {}, getattr(self, "_type_skips", None)
        )
        self.sel.record_selection([a for a, _ in selected], skipped)
        units = [_Unit(text, attr) for attr, text in selected]
        return units, _content_hash(*[t for _, t in selected]), [a for a, _ in selected]

    # ------------------------------------------------- client the strategy drives
    def live_values(self, record_id: str):
        pid = _validate_id(record_id)
        with self._client() as c:
            rec = self._fetch_one(c, pid)
        return ABSENT if rec is None else rec["values"]

    def _targets_for(self, loc: str) -> dict[str, str]:
        key = str(loc).strip().lower()
        return {src: per[key] for src, per in self.attribute_map().items() if key in per}

    def any_target_filled(self, values: dict, loc: str) -> bool:
        return any(_present((values or {}).get(t)) for t in self._targets_for(loc).values())

    def all_targets_filled(self, values: dict, loc: str) -> bool:
        wanted = [t for s, t in self._targets_for(loc).items() if _present((values or {}).get(s))]
        return bool(wanted) and all(_present((values or {}).get(t)) for t in wanted)

    def snapshot_target_filled(self, record_id: str, loc: str) -> bool:
        return self.any_target_filled(self._snapshot.get(str(record_id)) or {}, loc)

    def build_values(self, fields: dict, loc: str) -> dict:
        """{target_attr: text} for this run's translated units only. Defence in depth: refuses
        to emit a source attribute or a label outside the validated map."""
        m = self.attribute_map()
        out: dict = {}
        for src, text in (fields or {}).items():
            tgt = (m.get(src) or {}).get(str(loc).strip().lower())
            if not tgt or tgt.casefold() in {k.casefold() for k in m} or not _present(text):
                continue
            out[tgt] = text
        return out

    def patch_record(self, record_id: str, values: dict) -> tuple[bool, str]:
        pid = _validate_id(record_id)
        try:
            with self._client() as c:
                r = self._api(
                    c,
                    "PATCH",
                    "/api/v2/products/" + quote(pid, safe=""),
                    json={"attributes": values},
                )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201, 204):
            return True, ""
        return False, f"{r.status_code}: {vendor_excerpt(r.text)}"

    # ------------------------------------------------------------ merge canary
    def _semantics(self) -> dict:
        st = self.config.get(_SEMANTICS_KEY)
        return st if isinstance(st, dict) else {}

    def replace_detected(self) -> bool:
        return bool(self._semantics().get("replace_detected"))

    def needs_canary(self) -> bool:
        return not self._canary_passed and not self._semantics().get("merge_proven")

    def _persist_semantics(self, state: dict) -> None:
        cfg_state = dict(self._semantics())
        cfg_state.update(state)
        self.config[_SEMANTICS_KEY] = cfg_state
        cid = getattr(self, "connection_id", None)
        if cid is None:
            return
        from app.services.connectors import store  # lazy: store imports this module

        try:
            store.patch_config_merge(cid, _SEMANTICS_KEY, state)
        except Exception:
            log.warning(
                "plytix: connection %s could not record %s", cid, sorted(state), exc_info=True
            )

    def _read_row(self, pid: str) -> dict | None:
        try:
            with self._client() as c:
                return self._get_row(c, pid)
        except (ConnectorError, httpx.TransportError):
            return None

    @staticmethod
    def _attrs(row: dict | None) -> dict:
        a = (row or {}).get("attributes")
        return a if isinstance(a, dict) else {}

    def guarded_patch(self, record_id: str, values: dict) -> tuple[bool, str]:
        """EVERY write goes through here. After a safety stop nothing more is written; until
        merge semantics are proven, the write is a canary (see the module docstring)."""
        if self.safety_stop is not None:
            return False, self.safety_stop["message"]
        if not self.needs_canary():
            return self.patch_record(record_id, values)
        with self._canary_lock:
            if self.safety_stop is not None:
                return False, self.safety_stop["message"]
            if not self.needs_canary():
                return self.patch_record(record_id, values)
            return self._canary_patch(record_id, values)

    def _canary_patch(self, record_id: str, values: dict) -> tuple[bool, str]:
        pid = _validate_id(record_id)
        pre = self._read_row(pid)
        if pre is None:
            return False, (
                f"safety check: could not read product {pid} before the first write of this "
                "run; nothing written"
            )
        pre_attrs = self._attrs(pre)
        written = {str(k).casefold() for k in values}
        watched = {
            k: v for k, v in pre_attrs.items() if str(k).casefold() not in written and _has_value(v)
        }
        if not watched:
            return False, (
                f"safety check: product {pid} shows no other attribute with a value, so the "
                "first write could not be verified; nothing written"
            )
        ok, message = self.patch_record(pid, values)
        post = self._read_row(pid)
        if post is None:
            return self._stop(
                pid,
                pre,
                values,
                lost={},
                changed=[],
                why="the product could not be re-read afterwards, so there is no way to tell "
                "whether Plytix kept its other attributes",
            )
        post_attrs = self._attrs(post)
        lost = {k: v for k, v in watched.items() if not _has_value(post_attrs.get(k))}
        changed = sorted(k for k, v in watched.items() if k not in lost and post_attrs.get(k) != v)
        for f in _TOP_LEVEL_WATCHED:
            if not _has_value(pre.get(f)):
                continue
            if not _has_value(post.get(f)):
                lost[f"[{f}]"] = pre.get(f)
            elif post.get(f) != pre.get(f):
                changed.append(f"[{f}]")
        if lost:
            return self._stop(
                pid,
                pre,
                values,
                lost=lost,
                changed=changed,
                why=f"Plytix dropped {len(lost)} attribute(s) the write did not name (this PATCH "
                "REPLACES the attribute set instead of merging)",
            )
        if changed:
            return self._stop(
                pid,
                pre,
                values,
                lost={},
                changed=changed,
                why="value(s) the write did not name changed during it",
            )
        if ok and all(post_attrs.get(k) == v for k, v in values.items()):
            self._canary_passed = True
            self._persist_semantics(
                {"merge_proven": datetime.now(UTC).isoformat(timespec="seconds"), "product": pid}
            )
        return ok, message

    def _restore(self, pid: str, pre: dict, values: dict, lost: dict) -> tuple[list, dict]:
        """PATCH the product back from the pre-write snapshot: every attribute that had a value
        plus this write's translations, so the result is right under MERGE and under REPLACE
        semantics alike. Re-read and report what came back and what did not."""
        written = {str(k).casefold() for k in values}
        body = {
            k: v
            for k, v in self._attrs(pre).items()
            if _has_value(v) and str(k).casefold() not in written
        }
        body.update(values)
        req: dict = {"attributes": body}
        if "[label]" in lost:
            req["label"] = pre.get("label")
        try:
            with self._client() as c:
                self._api(c, "PATCH", "/api/v2/products/" + quote(pid, safe=""), json=req)
        except (ConnectorError, httpx.TransportError):
            pass
        after = self._read_row(pid) or {}
        after_attrs = self._attrs(after)
        restored: list[str] = []
        not_restored: dict = {}
        for k, v in lost.items():
            now = after.get(k[1:-1]) if k.startswith("[") else after_attrs.get(k)
            if _has_value(now):
                restored.append(k)
            else:
                not_restored[k] = v
        return restored, not_restored

    def _stop(self, pid, pre, values, *, lost: dict, changed: list, why: str):
        restored: list = []
        not_restored: dict = {}
        if lost:
            restored, not_restored = self._restore(pid, pre, values, lost)
            self._persist_semantics(
                {
                    "replace_detected": datetime.now(UTC).isoformat(timespec="seconds"),
                    "product": pid,
                }
            )
        sku = pre.get("sku") or pid
        parts = [
            f"{_SAFETY_PREFIX} the first write of this run (product {sku}) was checked: {why}. "
            "The run stopped there and nothing else was written."
        ]
        if lost:
            parts.append(f"Lost: {', '.join(sorted(lost))}.")
            if restored:
                parts.append(f"Restored from the pre-write copy: {', '.join(sorted(restored))}.")
            if not_restored:
                parts.append(
                    "NOT restored, restore these by hand (their old values are in the run "
                    f"result): {', '.join(sorted(not_restored))}."
                )
            parts.append(
                "Writes are now refused for this connection until Plytix confirms that PATCH "
                "merges attributes; contact support."
            )
        if changed:
            parts.append(f"Changed: {', '.join(sorted(set(changed)))} (left as they are).")
        if not lost and not changed:
            parts.append(f"Check product {sku} in Plytix by hand.")
        message = " ".join(parts)
        self.safety_stop = {
            "product": pid,
            "sku": pre.get("sku") or "",
            "lost": sorted(lost),
            "restored": sorted(restored),
            "not_restored": not_restored,
            "changed": sorted(set(changed)),
            "message": message,
        }
        log.error(
            "plytix: connection %s SAFETY STOP on product %s: lost=%s restored=%s "
            "not_restored=%s changed=%s",
            getattr(self, "connection_id", None),
            pid,
            sorted(lost),
            sorted(restored),
            sorted(not_restored),
            sorted(set(changed)),
        )
        return False, message

    # ---------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for art in self.discover_articles():
            selected, _sk = self._field_selection(art.get("values") or {})
            label = f"product {art.get('sku') or art['id']}"[:60]
            for attr, text in selected:
                out.append(PulledItem(external_id=art["aid"], field=attr, text=text, title=label))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Plytix writes per-language attributes - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow: thin hooks over the shared engine (flow.run_translate_flow).
# ===========================================================================
def _plytix_preflight(ctx):
    conn = ctx.connector
    if conn.replace_detected():
        raise ConnectorError(
            f"{_SAFETY_PREFIX} an earlier run found that Plytix replaced a product's attribute "
            "set on write (see that run's result). Writes are refused for this connection until "
            "Plytix confirms that PATCH merges attributes; contact support."
        )
    enabled = set(conn.map_locales())
    conn._type_skips = conn.validate_map_against_schema()
    return enabled, {"source": conn._source_locale()}


def _plytix_partition(locales, enabled, extra):
    src = (extra or {}).get("source", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _plytix_prepare(ctx, art):
    if ctx.connector.safety_stop is not None:
        return flow.SkipArticle("halted_skipped")  # stopped: no more MT, no more writes
    units, chash, attrs = ctx.connector.extract(art)
    if not units:
        return flow.SkipArticle("empty_skipped")
    aid = art["aid"]
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text="",
        body_units=units,
        extra_skeleton={"article_id": aid, "title": None},
        doc_title=f"product {art.get('sku') or aid}"[:60],
        extra={"body_attrs": attrs, "connector": ctx.connector},
    )


def _plytix_is_suppressed(connector, aid, loc):
    # Taxonomy C: the live record is the state a PATCH writes; suppressed only when EVERY mapped
    # target (for sources that have text) is filled, so a half-written language is retried.
    values = connector.live_values(aid)
    if values is ABSENT:
        return False
    return connector.all_targets_filled(values, loc)


def _plytix_variant_exists(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _plytix_build_payload(prep, loc, seg0_target, mapping):
    """{source_attr: translated text} for units actually translated. A held unit is OMITTED
    (never source-seeded), so Plytix keeps the stored target value (taxonomy B)."""
    out = {a: mapping[a] for a in prep.extra["body_attrs"] if a in mapping}
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
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _plytix_compute_ok(ctx):
    if ctx.connector.safety_stop is not None:
        return False
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _plytix_build_message(ctx):
    stop = ctx.connector.safety_stop
    parts = ([stop["message"]] if stop is not None else []) + [
        f"Created {ctx.created}, updated {ctx.updated} per-language write(s) (each a partial "
        f"attribute PATCH) across {len(ctx.usable)} language(s), {len(ctx.articles)} product(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(f"Skipped {ctx.counters['empty_skipped']} product(s) with no source text.")
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) not in the attribute map: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source language ({', '.join(dropped)})."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (product, language) pair(s) awaiting review (NEEDS_REVIEW) - "
            "nothing written; they will be re-attempted next run."
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
        parts.append(ctx.budget.message("product write(s)").strip())
        log.warning(
            "plytix_translate: connection %s hit MAX_WRITES_PER_RUN, stopped early", ctx.cid
        )
    return " ".join(parts)


def _plytix_build_result(ctx):
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
            {"article_id": a, "locale": lc, "held_fields": sorted(h.keys())}
            for (a, lc), h in ctx.connector.sel.held_back_pairs().items()
        ],
        "fields_selected": ctx.connector.sel.selected_sorted(),
        "fields_skipped": ctx.connector.sel.skipped_dict(),
        "failures": _real_failures(ctx),
        "halted_skipped": ctx.counters["halted_skipped"],
        "safety_stop": ctx.connector.safety_stop,
    }


def plytix_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Plytix product text attributes into one or more languages, each written to its
    mapped per-language target attribute with a partial PATCH (T1 attribute map). Thin wrapper
    over flow.run_translate_flow; poll-only with content-hash suppression."""
    spec = flow.TranslateFlowSpec(
        kind="plytix",
        no_conn_msg="No such Plytix connection.",
        hash_key="plytix_hashes",
        probe_label="Plytix",
        preflight=_plytix_preflight,
        prepare=_plytix_prepare,
        is_suppressed=_plytix_is_suppressed,
        build_payload=_plytix_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_plytix_compute_ok,
        build_message=_plytix_build_message,
        build_result=_plytix_build_result,
        held_prefix=_HELD_BACK_PREFIX,
        report_held_updates=False,
        variant_exists=_plytix_variant_exists,
        extra_counters=("empty_skipped", "halted_skipped"),
        partition=_plytix_partition,
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
