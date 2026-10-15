"""Sanity connector - supports BOTH Sanity localization models (see sanity_strategy.py).

Documented in: docs/architecture/connectors.md

WHY BOTH MODELS. Sanity has no built-in localization. The two community-standard plugins store a
translation in different places (a language item inside the same document, or a separate linked
document), many customers run one or the other, and a connector that picks one silently serves
half of them. The shared work (auth, scoped discovery, field selection, Portable Text, held-back
rules, hash suppression, the draft-only write) is identical; only the final write differs, so the
cost of supporting both is one extra strategy. The model is a per-connection setting (`i18n_model`,
default `field`); a project that mixes both on the SAME document type picks the one that holds its
translations.

TOPOLOGY. Field model = T1 (SanityFieldStrategy, delta patches with `ifRevisionID`). Document
model = T3 (SanityDocStrategy over the stock DocPerLocaleStrategy and the id map). The capability
card declares T1 (the default); the document model is the T3 variant of the same connector.

API (all calls use the project host `https://<projectId>.api.sanity.io`, never the CDN host, which
serves stale and published-only data):
    POST /<ver>/data/query/<dataset>?perspective=raw   {"query": GROQ, "params": {...}}
    POST /<ver>/data/mutate/<dataset>                  {"mutations": [...]}
Every id and parameter travels inside the JSON body; none is ever interpolated into a URL or into
GROQ text (the only text spliced into GROQ is the validated `language` field name).

DRAFT STEADY STATE (taxonomy C). Reads use `perspective=raw` and overlay the draft over the
published document; writes go to `drafts.<id>`, created from the published document first when
only that exists. The existence probe reads the same overlay, so the second run over an unchanged
source is fully suppressed. Nothing is ever published.

SCOPE (taxonomy A). `project_id`, `dataset`, `api_version`, document types and the language field
are validated against strict patterns; a caller-supplied document id must match the id pattern and
the fetched document's `_type` must be one of the configured types (the check runs after the fetch
because an id does not carry its type); remote `_key` values are validated before they enter a
patch path.

SELECTION (taxonomy D). Field model: only internationalized-array fields whose source-language
item holds text or Portable Text. Document model: top-level text and Portable Text fields. In auto
mode id-like names, machine-shaped values (email, URL, date) and, for the document model, enum-like
single tokens are skipped; the `fields` allowlist bypasses those heuristics. Every skip is reported.

PORTABLE TEXT. app/filters/sanity_portable_text.py walks a block array like the Contentful Rich
Text walker: one unit per text block, marks and links kept as paired placeholders, images and
custom blocks copied verbatim. A Portable Text field is all-or-nothing.

NOT IN v1. Webhooks (poll-first, like Contentful), nested objects and references inside documents
(only top-level fields are translated), Content Releases (`versions.*` documents are ignored),
and publishing.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.filters import sanity_portable_text as pt
from app.services.connectors import flow, harvest, idmap, webhooks  # noqa: F401
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
from app.services.connectors.contentful_part1 import _name_looks_id, _parse_names, _value_is_machine
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.sanity_strategy import (
    API_VERSION,
    EXISTING_PREFIX,
    FIELD_RE,
    HELD_BACK_PREFIX,
    SOURCE_KEY,
    SYSTEM_KEYS,
    Outcome,
    SanityDocStrategy,
    SanityFieldStrategy,
    copy_content,
    find_item,
    is_i18n_array,
    item_lang,
    metadata_doc_id,
    published_id,
    target_doc_id,
)
from app.services.connectors.strategy import ABSENT, WriteResult
from app.services.connectors.tracking import SelectionTracker
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_DATASET_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_VERSION_RE = re.compile(r"v(?:\d{4}-\d{2}-\d{2}|X)")
_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_DOCID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}")
_LANG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_LANGFIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_ENUMISH_RE = re.compile(r"[a-z0-9_-]{1,32}")
_RESERVED_TYPE_PREFIXES = ("translation.", "system.", "sanity.", "drafts.", "versions.")
_PAGE = 100
_MAX_PAGES = 50


SPEC = ConnectorSpec(
    kind="sanity",
    name="Sanity",
    icon="🧱",
    cat="Headless CMS",
    blurb="Translate Sanity documents into any language. Supports both localization models: "
    "field-level (sanity-plugin-internationalized-array: each translation is a new language "
    "item inside the same document) and document-level (@sanity/document-internationalization: "
    "each translation is its own document linked by a translation.metadata document). Writes "
    "land on the draft and never publish. Portable Text is translated block by block with "
    "links and marks kept.",
    config_fields=[
        FieldSpec(
            "project_id",
            "Project ID",
            "Your Sanity project ID (sanity.io/manage). Lowercase letters, digits, hyphens only.",
            placeholder="abc12xyz",
        ),
        FieldSpec(
            "dataset",
            "Dataset",
            "The dataset to translate (default 'production').",
            required=False,
            placeholder="production",
        ),
        FieldSpec(
            "i18n_model",
            "Localization model",
            "'field' for sanity-plugin-internationalized-array (default) or 'document' for "
            "@sanity/document-internationalization.",
            required=False,
            placeholder="field",
        ),
        FieldSpec(
            "document_types",
            "Document types",
            "Comma-separated schema type names to translate, e.g. 'post, page'.",
            placeholder="post, page",
        ),
        FieldSpec(
            "languages",
            "Languages",
            "Comma-separated language ids configured in your Studio. Targets outside this list "
            "are rejected.",
            placeholder="en, fr, de",
        ),
        FieldSpec(
            "source_locale",
            "Source language",
            "The language id to translate from (default: the first language).",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target languages",
            "Comma-separated language ids to write.",
            required=False,
            placeholder="fr, de",
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "Optional allowlist of field names to translate (comma-separated). Blank = "
            "auto-select; the run report lists what was selected and skipped.",
            required=False,
            placeholder="title, body",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude fields",
            "Field names to NEVER translate (comma-separated). Applies only when the allowlist "
            "above is blank.",
            required=False,
            placeholder="slug",
        ),
        FieldSpec(
            "language_field",
            "Language field (document model)",
            "Name of the field holding a document's language (default 'language').",
            required=False,
            placeholder="language",
        ),
        FieldSpec(
            "max_entries",
            "Max documents",
            "Safety cap on documents scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_token",
            "API token",
            "A Sanity API token with Editor permission (sanity.io/manage > API > Tokens). "
            "Sent as a Bearer token.",
            secret=True,
            placeholder="sk...",
        ),
    ],
    setup_steps=[
        "In sanity.io/manage open your project and note the Project ID and the dataset.",
        "Create an API token with Editor permission (API > Tokens > Add API token).",
        "Decide the model: field-level (internationalized-array plugin) or document-level "
        "(document-internationalization plugin).",
        "Enter the Project ID, the document types, the Studio's language ids and the token.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Sanity's _updatedAt bumps on "
    "any edit, so unchanged documents are suppressed by a per-(document, language) content "
    "hash. Writes go to the draft with a revision check and are never published; publish in "
    "the Studio when ready.",
    docs_url="https://www.sanity.io/docs/localization",
    supports_oauth=False,
)


class _Unit:
    """A flow translation unit (the engine reads only .text and .p_index)."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


def _rich_key(fid: str, idx: int) -> str:
    return f"{fid}#{idx}"


class SanityConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="sanity",
        topology=Topology.T1,  # field model; the document model is the T3 variant
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.PORTABLE_TEXT,
        regions="fixed-region",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="_updatedAt", stale_flag=None, thin_payload=True
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.sel = SelectionTracker(
            noun="field",
            label="sanity",
            loud_markers=("not an internationalized array", "unsupported value"),
        )
        self._rich_by_aid: dict = {}
        self._meta_cache: dict = {}
        try:
            is_doc = self._model() == "document"
        except ConnectorError:
            is_doc = False  # an invalid model is refused at first use (_model), not at build
        self.strategy = SanityDocStrategy() if is_doc else SanityFieldStrategy()

    # ------------------------------------------------------------- config
    def _model(self) -> str:
        raw = str(self.config.get("i18n_model") or "field").strip().lower() or "field"
        if raw not in ("field", "document"):
            raise ConnectorError(f"Invalid localization model {raw!r} - use 'field' or 'document'.")
        return raw

    def _project(self) -> str:
        p = str(self.config.get("project_id") or "").strip()
        if not _PROJECT_RE.fullmatch(p):
            raise ConnectorError(
                f"Invalid Sanity project ID {p[:40]!r}: lowercase letters, digits and hyphens only."
            )
        return p

    def _dataset(self) -> str:
        d = str(self.config.get("dataset") or "production").strip() or "production"
        if not _DATASET_RE.fullmatch(d):
            raise ConnectorError(f"Invalid Sanity dataset {d[:40]!r}.")
        return d

    def _version(self) -> str:
        v = str(self.config.get("api_version") or API_VERSION).strip() or API_VERSION
        if not _VERSION_RE.fullmatch(v):
            raise ConnectorError(f"Invalid Sanity API version {v[:20]!r} (e.g. {API_VERSION}).")
        return v

    def _host(self) -> str:
        host = f"https://{self._project()}.api.sanity.io"
        _validate_target(host)  # SSRF guard on every request path
        return host

    def _types(self) -> list[str]:
        out = []
        for t in _parse_names(self.config.get("document_types")):
            if not _TYPE_RE.fullmatch(t) or t.startswith(_RESERVED_TYPE_PREFIXES) or ".." in t:
                raise ConnectorError(f"Unsafe or reserved document type {t[:40]!r}.")
            out.append(t)
        if not out:
            raise ConnectorError("At least one document type is required (e.g. 'post').")
        return out

    def _lang_field(self) -> str:
        f = str(self.config.get("language_field") or "language").strip() or "language"
        if not _LANGFIELD_RE.fullmatch(f):
            raise ConnectorError(f"Invalid language field name {f[:40]!r}.")
        return f

    def _languages(self) -> list[str]:
        langs = _parse_names(self.config.get("languages"))
        if not langs:
            raise ConnectorError("At least one language id is required (e.g. 'en, fr').")
        for lg in langs:
            if not _LANG_RE.fullmatch(lg):
                raise ConnectorError(f"Unsafe language id {lg[:40]!r}.")
        return langs

    def enabled_locales(self) -> list[str]:
        return [x.lower() for x in self._languages()]

    def resolve_locale(self, loc: str) -> str:
        """Lower-cased flow locale -> the configured language id with its exact case."""
        m = {x.lower(): x for x in self._languages()}
        return m.get(str(loc).strip().lower(), str(loc).strip())

    def _source_locale(self) -> str:
        cfg = str(self.config.get("source_locale") or "").strip()
        return self.resolve_locale(cfg) if cfg else self._languages()[0]

    def source_language(self) -> str:
        return self._source_locale()

    # --------------------------------------------------------------- HTTP
    def _headers(self) -> dict:
        tok = str(self.secret.get("api_token") or "").strip()
        if not tok:
            raise ConnectorError("A Sanity API token is required.")
        if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in tok):
            raise ConnectorError("The Sanity API token has characters a header cannot carry.")
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def _base(self) -> str:
        return f"{self._host()}/{self._version()}/data"

    def _query(self, groq: str, params: dict | None = None):
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                f"{self._base()}/query/{self._dataset()}",
                params={"perspective": "raw"},
                json={"query": groq, "params": params or {}},
                headers=self._headers(),
            )
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"Sanity rejected the token ({r.status_code}) - check the API token, its "
                f"permissions and that it belongs to project {self.config.get('project_id')!r}."
            )
        if r.status_code == 404:
            raise ConnectorError(
                f"Sanity project or dataset not found (404) - check the Project ID and dataset. "
                f"{vendor_excerpt(r.text)}"
            )
        if r.status_code != 200:
            raise ConnectorError(f"Sanity query failed ({r.status_code}): {vendor_excerpt(r.text)}")
        try:
            return (r.json() or {}).get("result")
        except ValueError as e:
            raise ConnectorError("Sanity answered with a body that is not JSON.") from e

    def mutate(self, mutations: list) -> Outcome:
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "POST",
                    f"{self._base()}/mutate/{self._dataset()}",
                    params={"visibility": "sync"},
                    json={"mutations": mutations},
                    headers=self._headers(),
                )
        except httpx.TransportError as e:
            return Outcome(False, message=f"{type(e).__name__}: {e}")
        if r.status_code == 200:
            return Outcome(True, status=200)
        text = vendor_excerpt(r.text)
        err = {}
        try:
            body = r.json()
            err = body.get("error") if isinstance(body, dict) else {}
            err = err if isinstance(err, dict) else {}
        except ValueError:
            pass
        if r.status_code == 409 or err.get("type") == "transactionConsistencyError":
            return Outcome(False, True, f"revision conflict: {text}", r.status_code)
        return Outcome(False, False, f"{r.status_code}: {text}", r.status_code)

    # ------------------------------------------------------------ documents
    def _check_id(self, raw) -> str:
        s = published_id(str(raw or "").strip())
        if not _DOCID_RE.fullmatch(s):
            raise ConnectorError(f"Unsafe document id {str(raw)[:40]!r}.")
        return s

    def snapshot(self, doc_id: str):
        """The effective document (draft over published) plus draft facts, or ABSENT. The type is
        scope-checked here so a write can never reach a document outside the configured types."""
        pid = self._check_id(doc_id)
        rows = self._query(
            "*[_id in [$p, $d]]",
            {"p": pid, "d": "drafts." + pid},
        )
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        draft = next((r for r in rows if r.get("_id") == "drafts." + pid), None)
        pub = next((r for r in rows if r.get("_id") == pid), None)
        doc = draft or pub
        if doc is None:
            return ABSENT
        types = self._types()
        if doc.get("_type") not in types:
            raise ConnectorError(
                f"Document {pid!r} is type {doc.get('_type')!r}, which is not configured on "
                f"this connection ({', '.join(types)}) - refusing to target it."
            )
        return {
            "doc": doc,
            "type": doc.get("_type"),
            "has_draft": draft is not None,
            "draft_rev": (draft or {}).get("_rev"),
            "draft_id": "drafts." + pid,
            "published": pub,
        }

    def ensure_draft(self, doc_id: str) -> None:
        """Create drafts.<id> from the published document when no draft exists (idempotent)."""
        pid = self._check_id(doc_id)
        snap = self.snapshot(pid)
        if snap is ABSENT or snap["has_draft"] or not snap["published"]:
            return
        body = copy_content(snap["published"])
        body["_id"] = "drafts." + pid
        out = self.mutate([{"createIfNotExists": body}])
        if not out.ok:
            raise ConnectorError(f"Could not create the draft of {pid}: {out.message}")

    # --------------------------------------------------------------- test
    def test(self) -> tuple[bool, str]:
        try:
            types = self._types()
            self._languages()
            n = self._query("count(*[_type in $t && !(_id in path('versions.**'))])", {"t": types})
            return True, (
                f"Connected to Sanity (project {self.config.get('project_id')}, dataset "
                f"{self._dataset()}, {self._model()} model): {n} document(s) of type "
                f"{', '.join(types)}."
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:200]}"

    def containers(self) -> list[dict]:
        try:
            return [{"id": t, "name": f"{t} (document type)", "tables": []} for t in self._types()]
        except Exception:
            return []

    # ---------------------------------------------------------- discovery
    def _ids_for_type(self, t: str, cap: int) -> list[str]:
        """Base ids (newest edited first) of one type, drafts and published folded together."""
        lf = self._lang_field()
        flt = "_type == $t && !(_id in path('versions.**'))"
        params: dict = {"t": t}
        if self._model() == "document":
            flt += f" && {lf} == $src"
            params["src"] = self._source_locale()
        out: list[str] = []
        seen: set = set()
        for page in range(_MAX_PAGES):
            lo, hi = page * _PAGE, (page + 1) * _PAGE
            groq = f"*[{flt}] | order(_updatedAt desc, _id) [{lo}...{hi}]._id"
            rows = self._query(groq, params) or []
            for i in rows:
                if not isinstance(i, str):
                    continue
                pid = published_id(i)
                if pid not in seen and _DOCID_RE.fullmatch(pid):
                    seen.add(pid)
                    out.append(pid)
            if len(out) >= cap or len(rows) < _PAGE:
                break
        return out[:cap]

    def _load(self, ids: list[str]) -> dict:
        """{base id: snapshot-like dict} for the given base ids (both variants, one query)."""
        want = []
        for i in ids:
            want += [i, "drafts." + i]
        rows = self._query("*[_id in $ids]", {"ids": want}) or []
        by: dict = {}
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("_id"), str):
                by.setdefault(published_id(r["_id"]), {})[
                    "draft" if r["_id"].startswith("drafts.") else "pub"
                ] = r
        return by

    def _flatten(self, doc: dict) -> dict:
        return {
            "id": published_id(doc["_id"]),
            "_type": doc.get("_type"),
            "updatedAt": doc.get("_updatedAt"),
            "doc": doc,
        }

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source documents to translate. Explicit ids win and are SCOPE-VALIDATED (id pattern, then
        the fetched type must be configured). Else each configured type is listed newest-edited
        first and the shared cap is filled ROUND-ROBIN across types (taxonomy E)."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        types = self._types()
        if article_ids:
            out = []
            for raw in list(article_ids)[:cap]:
                snap = self.snapshot(self._check_id(raw))
                if snap is ABSENT:
                    continue
                if self._model() == "document":
                    got = snap["doc"].get(self._lang_field())
                    if got != self._source_locale():
                        raise ConnectorError(
                            f"Document {raw!r} has language {got!r}, not the source language "
                            f"{self._source_locale()!r} - only source documents are translated."
                        )
                out.append(self._charge_record(self._flatten(snap["doc"])))
            return out
        per_type = [self._ids_for_type(t, cap) for t in types]
        order: list[str] = []
        i = 0
        while len(order) < cap and any(i < len(lst) for lst in per_type):
            for lst in per_type:
                if i < len(lst) and len(order) < cap:
                    order.append(lst[i])
            i += 1
        loaded = self._load(order)
        out = []
        for pid in order:
            v = loaded.get(pid) or {}
            doc = v.get("draft") or v.get("pub")
            if doc is None or doc.get("_type") not in types:
                continue
            out.extend(self._keep_rows([self._flatten(doc)]))
        return out

    # ---------------------------------------------------------- selection
    def _select(self, doc: dict, *, scope_only: bool = False):
        """(plain {name: text}, rich {name: blocks}, skipped {name: reason}) for one document.
        scope_only counts a field whose source value is currently empty (it is the field's role,
        not this document's current value, that decides whether it is a translated field)."""
        model = self._model()
        src = self._source_locale()
        allow = _parse_names(self.config.get("fields"))
        exclude = set(_parse_names(self.config.get("exclude_fields")))
        lf = self._lang_field()
        plain: dict = {}
        rich: dict = {}
        skipped: dict = {}
        names = allow if allow else [k for k in doc if not k.startswith("_") and k != lf]
        for name in names:
            if not allow and name in exclude:
                skipped[name] = "operator exclude_fields"
                continue
            if not FIELD_RE.fullmatch(name):
                # a name that is not a plain identifier would be read as a path by `set`
                skipped[name] = "field name is not a plain identifier"
                continue
            if name not in doc or name.startswith("_") or name == lf:
                skipped[name] = "not a translatable field on this document"
                continue
            val = doc[name]
            if model == "field":
                if not is_i18n_array(val):
                    if isinstance(val, str) or pt.is_portable_text(val) or allow:
                        skipped[name] = (
                            "not an internationalized array (the field model translates "
                            "internationalized-array fields only)"
                        )
                    continue
                item = find_item(val, src)
                sv = item.get("value") if item else None
                if item is None:
                    skipped[name] = f"no {src!r} item to translate from"
                    continue
            else:
                sv = val
            if isinstance(sv, str):
                if not sv.strip() and not scope_only:
                    skipped[name] = "no non-empty source text"
                    continue
                if not allow:
                    if _name_looks_id(name):
                        skipped[name] = f"field name looks non-translatable ({name})"
                        continue
                    mach = _value_is_machine(sv)
                    if mach:
                        skipped[name] = mach
                        continue
                    if model == "document" and _ENUMISH_RE.fullmatch(sv.strip()):
                        skipped[name] = "enum-like value (a single lowercase token)"
                        continue
                plain[name] = sv
            elif pt.is_portable_text(sv):
                if not pt.extract(sv) and not scope_only:
                    skipped[name] = "no translatable text in the Portable Text"
                    continue
                if not allow and _name_looks_id(name):
                    skipped[name] = f"field name looks non-translatable ({name})"
                    continue
                rich[name] = sv
            else:
                skipped[name] = (
                    f"unsupported value (a {type(sv).__name__}, not text or Portable Text)"
                )
        return plain, rich, skipped

    def translated_field_ids(self, doc: dict) -> set:
        """Field names this connection translates on `doc` (field model existence scope)."""
        plain, rich, _sk = self._select(doc, scope_only=True)
        return set(plain) | set(rich)

    def _title_field(self, names: list[str]) -> str | None:
        cfg = str(self.config.get("title_field") or "").strip()
        if cfg and cfg in names:
            return cfg
        for cand in ("title", "name"):
            if cand in names:
                return cand
        return None

    def extract(self, entry: dict):
        doc = entry.get("doc") or {}
        plain, rich, skipped = self._select(doc)
        self.sel.record_selection(list(plain) + list(rich), skipped)
        title_field = self._title_field(list(plain))
        title_text = plain.get(title_field, "") if title_field else ""
        body_names = [n for n in plain if n != title_field]
        body_units = [_Unit(plain[n], n) for n in body_names]
        hash_parts = [title_text] + [plain[n] for n in body_names]
        rich_kept: dict = {}
        for n, blocks in rich.items():
            units = pt.extract(blocks)
            if not units:
                continue
            rich_kept[n] = blocks
            body_units.extend(_Unit(u.text, _rich_key(n, u.p_index)) for u in units)
            hash_parts.append(json.dumps(blocks, sort_keys=True, ensure_ascii=False))
        self._rich_by_aid[str(entry.get("id") or "")] = rich_kept
        return title_field, title_text, body_units, _content_hash(*hash_parts), body_names

    # --------------------------------- T3 client (document model) side
    def _lang_exact(self, loc: str) -> str:
        return self.resolve_locale(loc)

    def metadata(self, source_id: str, *, fresh: bool = False):
        """{"doc": translation.metadata doc, "entries": {language: referenced published id}} for
        the metadata document that lists `source_id`, or None."""
        pid = self._check_id(source_id)
        if not fresh and pid in self._meta_cache:
            return self._meta_cache[pid]
        rows = self._query(
            "*[_type == 'translation.metadata' && $ref in translations[].value._ref]"
            "{_id, _rev, translations}",
            {"ref": pid},
        )
        found = None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            entries = {}
            for e in row.get("translations") or []:
                lang = item_lang(e)
                ref = (e.get("value") or {}).get("_ref") if isinstance(e, dict) else None
                if lang and isinstance(ref, str):
                    entries[lang] = published_id(ref)
            if pid in entries.values():
                found = {"doc": row, "entries": entries}
                break
        self._meta_cache[pid] = found
        return found

    def foreign_translation(self, source_id: str, locale: str) -> str | None:
        """The id of a translation Sanity already links for (source, language) that this
        connection does not own, else None."""
        lang = self._lang_exact(locale)
        meta = self.metadata(source_id, fresh=True)
        ref = (meta or {}).get("entries", {}).get(lang)
        if not ref:
            return None
        return None if self._owned(source_id, locale, ref) else ref

    def foreign_cached(self, source_id: str, locale: str) -> bool:
        """The pre-MT check (flow `parked` hook): same rule, one metadata read per document."""
        lang = self._lang_exact(locale)
        meta = self.metadata(source_id)
        ref = (meta or {}).get("entries", {}).get(lang)
        return bool(ref) and not self._owned(source_id, locale, ref)

    def _owned(self, source_id: str, locale: str, ref: str) -> bool:
        if ref == target_doc_id(source_id, self._lang_exact(locale)):
            return True
        cid = getattr(self, "connection_id", None)
        row = idmap.get(cid, str(source_id), locale) if cid is not None else None
        return bool(row and row.target_id and published_id(row.target_id) == ref)

    def _meta_entry(self, style: dict, lang: str, ref: str, doc_type: str) -> dict:
        legacy = style.get("legacy", False)
        e = {
            "_key": lang if legacy else target_doc_id(ref, lang)[5:17],
            "_type": style.get("type") or "internationalizedArrayReferenceValue",
            "value": {
                "_type": "reference",
                "_ref": ref,
                "_weak": True,
                "_strengthenOnPublish": {"type": doc_type},
            },
        }
        if not legacy:
            e["language"] = lang
        return e

    def _link(self, source_id: str, doc_type: str, lang: str, target_pid: str) -> str:
        """Link the translation into the source's translation.metadata document (creating it when
        there is none). Returns "" or an error message. Never replaces an existing entry."""
        spid = self._check_id(source_id)
        for _attempt in range(3):
            meta = self.metadata(spid, fresh=True)
            if meta is None:
                src_lang = self._source_locale()
                doc = {
                    "_id": metadata_doc_id(spid),
                    "_type": "translation.metadata",
                    "schemaTypes": [doc_type],
                    "translations": [
                        self._meta_entry({}, src_lang, spid, doc_type),
                        self._meta_entry({}, lang, target_pid, doc_type),
                    ],
                }
                out = self.mutate([{"createIfNotExists": doc}])
                self._meta_cache.pop(spid, None)
                if not out.ok:
                    return out.message
                # createIfNotExists is a no-op when the id exists: confirm the link landed.
                again = self.metadata(spid, fresh=True)
                if again and again["entries"].get(lang) == target_pid:
                    return ""
                continue
            cur = meta["entries"].get(lang)
            if cur == target_pid:
                return ""
            if cur:
                return f"{lang} is already linked to {cur}"
            first = (meta["doc"].get("translations") or [{}])[0]
            style = {"legacy": "language" not in first, "type": first.get("_type")}
            entry = self._meta_entry(style, lang, target_pid, doc_type)
            out = self.mutate(
                [
                    {
                        "patch": {
                            "id": meta["doc"]["_id"],
                            "ifRevisionID": meta["doc"].get("_rev"),
                            "insert": {"after": "translations[-1]", "items": [entry]},
                        }
                    }
                ]
            )
            self._meta_cache.pop(spid, None)
            if out.ok:
                return ""
            if not out.conflict:
                return out.message
        return "could not link the translation (concurrent edits to the metadata document)"

    def create_target(self, source_id: str, locale: str, fields: dict):
        """Reserve step: a near-empty draft (type + language only) under the deterministic id,
        linked into the translation metadata. The body is filled by fill_target."""
        lang = self._lang_exact(locale)
        snap = self.snapshot(source_id)
        if snap is ABSENT:
            return None, f"source document {source_id} not found"
        tpid = target_doc_id(source_id, lang)
        tsnap = self.snapshot(tpid)  # a wrong-type document under this id is refused here
        if tsnap is ABSENT:
            shell = {"_id": "drafts." + tpid, "_type": snap["type"], self._lang_field(): lang}
            out = self.mutate([{"createIfNotExists": shell}])
            if not out.ok:
                return None, f"could not create the translation draft: {out.message}"
        elif not tsnap["has_draft"]:
            # Review: the translation is already PUBLISHED (e.g. the id map was lost). Never lay
            # an empty shell over it (fill would rebuild the draft from the SOURCE document): copy
            # the published translation into its draft, so the fill only sets translated fields.
            self.ensure_draft(tpid)
        err = self._link(source_id, snap["type"], lang, tpid)
        if err:
            return None, f"could not link the translation: {err}"
        return tpid, ""

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        fields = dict(fields or {})
        src = fields.pop(SOURCE_KEY, None)
        if not src or not fields:
            return WriteResult(False, False, target_id=target_id, message="nothing to fill")
        lf = self._lang_field()
        for _attempt in range(3):
            tsnap = self.snapshot(target_id)
            if tsnap is ABSENT:
                return WriteResult(False, False, target_id=target_id, message="target not found")
            if not tsnap["has_draft"]:
                self.ensure_draft(target_id)
                tsnap = self.snapshot(target_id)
                if tsnap is ABSENT or not tsnap["has_draft"]:
                    return WriteResult(False, False, target_id=target_id, message="no draft")
            tdoc = tsnap["doc"]
            lang = tdoc.get(lf)
            if not isinstance(lang, str) or not lang:
                return WriteResult(
                    False, False, target_id=target_id, message="target has no language"
                )
            content = [k for k in tdoc if k not in SYSTEM_KEYS and k not in ("_type", lf)]
            pub = tsnap.get("published")
            pub_content = bool(pub) and any(
                k not in SYSTEM_KEYS and k not in ("_type", lf) for k in pub
            )
            if not content and pub_content:
                # an empty shell draft over a published translation: rebuild the draft from the
                # PUBLISHED translation (never from the source), then set only translated fields
                body = copy_content(pub)
                body.update(fields)
                body[lf] = lang
                body["_id"] = tsnap["draft_id"]
                out = self.mutate([{"createOrReplace": body}])
                created = False
            elif not content:
                # BUILD: the reserved shell is still empty -> copy the source's other fields (as the
                # Studio's duplicate does) and overlay the translated ones in ONE replace.
                ssnap = self.snapshot(src)
                if ssnap is ABSENT:
                    return WriteResult(False, True, target_id=target_id, message="source not found")
                body = copy_content(ssnap["doc"])
                body.update(fields)
                body[lf] = lang
                body["_id"] = tsnap["draft_id"]
                out = self.mutate([{"createOrReplace": body}])
                created = True
            else:
                # UPDATE: only the translated fields, guarded by the draft revision; held fields
                # are absent from `fields`, so their stored translation is untouched.
                out = self.mutate(
                    [
                        {
                            "patch": {
                                "id": tsnap["draft_id"],
                                "ifRevisionID": tsnap["draft_rev"],
                                "set": fields,
                            }
                        }
                    ]
                )
                created = False
            if out.ok:
                if pub:
                    self.note_live_kept(src, lang)
                return WriteResult(True, created, target_id=target_id, message="filled")
            if not out.conflict:
                return WriteResult(False, created, target_id=target_id, message=out.message)
        return WriteResult(
            False, False, target_id=target_id, message="revision conflict (concurrent edit)"
        )

    def note_live_kept(self, source_id: str, locale: str) -> None:
        """Record a pair whose translation is already PUBLISHED: this run wrote the draft only, the
        live version is unchanged until an editor publishes it in the Studio (run result)."""
        if not isinstance(getattr(self, "_live_kept", None), list):
            self._live_kept = []
        pair = {"article_id": str(source_id), "locale": str(locale).lower()}
        if pair not in self._live_kept:
            self._live_kept.append(pair)

    def verify_target(self, target_id: str) -> bool:
        from app.services.connectors.strategy import TargetUnverifiable

        try:
            pid = self._check_id(target_id)
            n = self._query("count(*[_id in [$p, $d]])", {"p": pid, "d": "drafts." + pid})
        except Exception as e:
            raise TargetUnverifiable(str(e)) from e
        if not isinstance(n, int):
            raise TargetUnverifiable("unexpected count result")
        return n > 0

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        return None  # rule 6 is enforced statelessly by SanityDocStrategy.write_target

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for entry in self.discover_articles():
            plain, _rich, _sk = self._select(entry["doc"])
            tf = self._title_field(list(plain))
            label = (plain.get(tf, "") or entry["id"])[:60] if tf else entry["id"]
            for name, text in plain.items():
                out.append(PulledItem(entry["id"], name, text, label))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Sanity writes per-language content - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow - thin hooks over the shared engine (flow.run_translate_flow).
# ===========================================================================
_GUARD_REASON = "Portable Text placeholders did not round-trip; the stored value was kept"


def _preflight(ctx):
    enabled = set(ctx.connector.enabled_locales())
    src = ctx.connector._source_locale().lower()
    if src not in enabled:
        raise ConnectorError(
            f"Source language {src!r} is not in this connection's languages "
            f"({', '.join(sorted(enabled)) or 'none'}). Add it, or fix source_locale."
        )
    return enabled, {"source": src}


def _partition(locales, enabled, extra):
    src = (extra or {}).get("source", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _prepare(ctx, art):
    title_field, title_text, body_units, chash, body_names = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = art["id"]
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={
            "article_id": aid,
            "title": title_text or None,
            "doc_type": art.get("_type"),
        },
        doc_title=f"{(title_text or art.get('_type') or 'document')[:50]} ({aid})",
        extra={
            "title_field": title_field,
            "body_names": body_names,
            "rich": dict(ctx.connector._rich_by_aid.get(aid) or {}),
            "model": ctx.connector._model(),
        },
    )


def _is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _parked(connector, aid, loc, chash):
    """Document model: a translation Sanity links that this connection does not own is skipped
    BEFORE any machine translation is spent (rule 6). Not done: no hash is recorded."""
    return connector._model() == "document" and connector.foreign_cached(aid, loc)


def _rich_rebuild(prep, mapping) -> tuple:
    docs: dict = {}
    held: dict = {}
    for fid, blocks in (prep.extra.get("rich") or {}).items():
        units = pt.extract(blocks)
        trans = {u.p_index: mapping.get(_rich_key(fid, u.p_index)) for u in units}
        if any(v is None for v in trans.values()):
            held[fid] = ("awaiting review (NEEDS_REVIEW)", False)
            continue
        doc, _bad = pt.rebuild(blocks, trans)
        if doc is None:
            held[fid] = (_GUARD_REASON, True)
            continue
        docs[fid] = doc
    return docs, held


def _extra_held(prep, loc, mapping):
    _docs, held = _rich_rebuild(prep, mapping)
    names = [f"{fid} (placeholders lost)" for fid, (_r, guard) in sorted(held.items()) if guard]
    if names:
        log.warning(
            "sanity: Portable Text field(s) %s failed the placeholder guard on %s/%s; the stored "
            "value is kept and the pair is retried next run",
            ", ".join(n.split(" ")[0] for n in names),
            prep.aid,
            loc,
        )
    return names


def _build_payload(prep, loc, seg0_target, mapping):
    """{field: translated value} for the fields translated THIS run. A held-back field is OMITTED,
    never source-seeded, so the delta write keeps the stored translation (taxonomy B)."""
    out: dict = {}
    tf = prep.extra["title_field"]
    if tf is not None and seg0_target is not None:
        out[tf] = seg0_target
    for name in prep.extra["body_names"]:
        if name in mapping:
            out[name] = mapping[name]
    docs, _held = _rich_rebuild(prep, mapping)
    out.update(docs)
    if prep.extra.get("model") == "document":
        out[SOURCE_KEY] = prep.aid
    return out


def _is_held(f):
    return str((f or {}).get("error", "")).startswith(HELD_BACK_PREFIX)


def _is_existing(f):
    return str((f or {}).get("error", "")).startswith(EXISTING_PREFIX)


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held(f) and not _is_existing(f)]


def _existing(ctx):
    return [f for f in ctx.failures if _is_existing(f)]


def _compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    pk = ctx.counters["parked_skipped"]
    return bool(
        (ctx.created + ctx.updated) > 0
        or ((ctx.skipped + pk) > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped + pk) == 0)
    )


def _live_kept(ctx):
    """Pairs whose translation is already published: written to the draft, live text unchanged."""
    return list(getattr(ctx.connector, "_live_kept", None) or [])


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} language version(s) across "
        f"{len(ctx.usable)} language(s) from {len(ctx.articles)} document(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} document(s) with no translatable fields."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) not in this connection's languages: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source language "
            f"({', '.join(dropped)}) - configure different target languages."
        )
    held = [f for f in ctx.failures if _is_held(f)]
    if held:
        parts.append(
            f"Held back {len(held)} (document, language) pair(s) awaiting review (NEEDS_REVIEW): "
            f"held fields keep their stored value and the pair is retried next run."
        )
    foreign = ctx.counters["parked_skipped"] + len(_existing(ctx))
    if foreign:
        parts.append(
            f"Left {foreign} (document, language) pair(s) untouched: Sanity already links a "
            f"translation that this connection did not create."
        )
    live = _live_kept(ctx)
    if live:
        parts.append(
            f"{len(live)} translation(s) were already published in Sanity: the new text is on "
            f"the draft only and the live (published) version is unchanged until an editor "
            f"reviews and publishes it in the Studio."
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
        parts.append(ctx.budget.message("document write(s)").strip())
    return " ".join(parts)


def _build_result(ctx):
    return {
        "entries": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len([f for f in ctx.failures if _is_held(f)]),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "existing_untouched": ctx.counters["parked_skipped"] + len(_existing(ctx)),
        "fields_selected": ctx.connector.sel.selected_sorted(),
        "fields_skipped": ctx.connector.sel.skipped_dict(),
        "failures": _real_failures(ctx),
    } | ({"live_kept": _live_kept(ctx)} if _live_kept(ctx) else {})


def sanity_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Sanity documents into one or more languages. Field model: a new language item in
    each internationalized-array field of the SAME document (delta patch, revision-guarded).
    Document model: a linked per-language document (reserve-then-fill with the id map). Both write
    drafts only. Thin wrapper over flow.run_translate_flow; poll-only with content-hash
    suppression. only_pairs restricts the write loop to exactly those (document, language) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="sanity",
        no_conn_msg="No such Sanity connection.",
        hash_key="sanity_hashes",
        probe_label="Sanity",
        preflight=_preflight,
        prepare=_prepare,
        is_suppressed=_is_suppressed,
        build_payload=_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_compute_ok,
        build_message=_build_message,
        build_result=_build_result,
        held_prefix=HELD_BACK_PREFIX,
        extra_held=_extra_held,
        extra_counters=("empty_skipped", "parked_skipped"),
        parked=_parked,
        partition=_partition,
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
