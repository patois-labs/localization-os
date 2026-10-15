"""Per-record-type field-mapping generalizer (SPEC-salesforce-knowledge.md — the
build's POINT).

Unlike Zendesk/Zoho/Freshdesk (a FIXED title+body shape), a field-rich source
system's translatable fields VARY per record type and must be discovered at
runtime from the system's own schema (Salesforce Describe). Getting this contract
right once generalizes it to every future field-rich connector (Contentful T1,
...), so it lives here — a vendor-neutral shared module — NOT inside the
Salesforce class (spec step 4).

The three-step contract:

  1. select_translatable_fields(describe_fields) -> the updateable, non-calculated
     string / textarea / rich-text(HTML) fields, each normalized to
     {name, type, label} where `type` is one of 'string' | 'textarea' | 'html'.
     NEVER hardcode a body field name — an org renames/adds fields, and a formula
     / calculated field must be excluded (it is read-only and derived).

  2. the connector persists that list in Connection.config (a "rediscover fields"
     action), keyed per record type — persistence is the connector's job, not
     this module's.

  3. extract_record(record, field_specs, get_filter) -> an Extraction that carries
     the flow-engine units (title = order 0, every other field's unit(s) = order
     1+) PLUS the state to rebuild a per-FIELD translated payload. A 'html' field
     routes through the shared html filter (many units); a 'string'/'textarea'
     field is ONE plain unit. Field name <-> unit index is recorded so write-back
     reinserts each translation into the SAME field.

The flow engine (flow.py) only knows `title_text` + `body_units` (each with
`.text` + `.p_index`) + a `build_payload(seg0_target, mapping)`. This module
namespaces every field's unit p_index (`Name` for a plain field, `Name#j` for the
j-th html sub-unit of a field) so multiple html fields never collide, and
Extraction.build_payload reassembles the exact per-field dict the connector writes
back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.connectors.base import content_hash

# Standard Knowledge__kav string fields that are updateable but are NOT translatable
# content (research salesforce_knowledge.md §3 "Do NOT translate"): identifiers,
# workflow/status, the URL slug (SF docs: "You do not need to translate URL
# fields"), and version/visibility bookkeeping. Excluded even though Describe marks
# them string/updateable. Title + Summary + custom rich-text/text fields remain.
_EXCLUDE_NAMES = frozenset(
    {
        "Id",
        "KnowledgeArticleId",
        "MasterVersionId",
        "RecordTypeId",
        "ArticleNumber",
        "Language",
        "PublishStatus",
        "IsMasterLanguage",
        "IsLatestVersion",
        "VersionNumber",
        "UrlName",
        "IsVisibleInApp",
        "IsVisibleInPkb",
        "IsVisibleInCsp",
        "IsVisibleInPrm",
        "SourceId",
        "OwnerId",
        "CreatedById",
        "LastModifiedById",
    }
)

# Salesforce Describe `type` values that carry translatable plain text. A rich-text
# area is `type == "textarea"` WITH htmlFormatted=true / extraTypeInfo=richtextarea
# (detected separately by _is_rich); a non-rich textarea/string is plain text.
_PLAIN_TYPES = frozenset({"string", "textarea"})


def _is_rich(f: dict) -> bool:
    """True when a Describe field is a Rich Text Area (HTML body). Salesforce marks
    these with `htmlFormatted: true` and/or `extraTypeInfo: "richtextarea"`; some
    describe shapes also surface `type: "richtextarea"`/`"html"`. Any one signal
    counts (defensive — the exact flag set is version-sensitive; [UNCERTAIN] which
    the tenant emits, so we accept all documented signals)."""
    if not isinstance(f, dict):
        return False
    if f.get("htmlFormatted") is True:
        return True
    if str(f.get("extraTypeInfo") or "").strip().lower() == "richtextarea":
        return True
    return str(f.get("type") or "").strip().lower() in ("richtextarea", "html")


def select_translatable_fields(describe_fields, *, extra_exclude=()) -> list[dict]:
    """Filter a Describe field list to the translatable payload (spec step 1).

    A field qualifies iff: it is `updateable` AND NOT `calculated` (formula/rollup
    fields are read-only, derived — never write to them) AND either it is a rich
    text area (-> normalized type 'html') OR its type is string/textarea (-> that
    type) AND its API name is not a known-non-content standard field (_EXCLUDE_NAMES)
    nor in the caller's extra_exclude. Returns [{name, type, label}] with `type`
    normalized to 'string' | 'textarea' | 'html' (the routing key extract_record
    uses). Order is preserved from the Describe response."""
    excl = _EXCLUDE_NAMES | {str(x) for x in extra_exclude}
    out: list[dict] = []
    for f in describe_fields or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not name or name in excl:
            continue
        if not f.get("updateable"):
            continue
        if f.get("calculated"):
            continue
        if _is_rich(f):
            norm = "html"
        elif str(f.get("type") or "").strip().lower() in _PLAIN_TYPES:
            norm = str(f.get("type")).strip().lower()
        else:
            continue
        out.append({"name": name, "type": norm, "label": f.get("label") or name})
    return out


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index).
    p_index is a namespaced STRING sentinel unique across all fields of the record,
    so two html fields' sub-units can never collide."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


@dataclass
class Extraction:
    """The result of extract_record: the flow-engine view (title_text + body_units)
    plus the per-field rebuild state. `is_empty` is True when nothing translatable
    was found (the connector then skips the article without a write)."""

    title_field: str | None
    title_text: str
    body_units: list = field(default_factory=list)
    content_hash: str = ""
    # per field name -> rebuild state:
    #   plain: {"type":"string|textarea", "pindex": "<name>"}
    #   html : {"type":"html", "bytes": b"<raw html>", "filter": <html filter>,
    #           "subs": [(pindex_str, orig_int_index), ...]}
    _fields: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.title_text.strip() and not self.body_units

    def build_payload(self, seg0_target, mapping: dict, *, omit_held: bool = False) -> dict:
        """Reassemble the per-FIELD translated payload {FieldName: value} from the
        flow's title translation (`seg0_target`) + the body-unit `mapping`
        ({str(p_index): translated_text}, NEEDS_REVIEW / empty units already excluded
        by the engine).

        - Title field: uses seg0_target when present, else the source title (parity
          with every sibling's build_payload title handling).
        - html field: rebuilds via the html filter with the field's own sub-mapping
          {str(orig_index): translated}; a missing sub-unit keeps its SOURCE text
          (filter.build leaves un-provided indices untouched — the standard
          NEEDS_REVIEW behavior), so the field always round-trips valid HTML.
        - plain field: emits the field ONLY when a translation is present; a
          NEEDS_REVIEW / empty plain field is OMITTED so the Draft keeps its existing
          value rather than having SOURCE text written as a fake translation
          (intercom D2 precedent — never write source as a stand-in).

        omit_held=True (#640, the Salesforce gate 2026-10-10): a title with no translation
        and an html field with ANY untranslated sub-unit are OMITTED too, so no source
        text is ever written into an existing translation (the field keeps its stored
        value). An all-held record then yields {}."""
        out: dict = {}
        if self.title_field is not None:
            if seg0_target is not None:
                out[self.title_field] = seg0_target
            elif not omit_held or not self.title_text.strip():
                out[self.title_field] = self.title_text
        for name, st in self._fields.items():
            if st["type"] == "html":
                sub = {}
                for pidx, orig in st["subs"]:
                    if pidx in mapping:
                        sub[str(orig)] = mapping[pidx]
                if omit_held and len(sub) < len(st["subs"]):
                    continue
                out[name] = st["filter"].build(st["bytes"], sub).decode("utf-8")
            else:
                pidx = st["pindex"]
                if pidx in mapping:
                    out[name] = mapping[pidx]
                # else omit (NEEDS_REVIEW / empty) — never write source as a stand-in.
        return out


def extract_record(
    record: dict, field_specs, get_filter, *, title_field: str = "Title"
) -> Extraction:
    """Extract one field-rich record's translatable units (spec step 3).

    `field_specs` is the persisted [{name,type,label}] for THIS record's type. The
    `title_field` (default 'Title') becomes flow order 0 (seg0); every other field
    becomes order 1+ body unit(s) — a plain field is one unit, a html field is its
    html-filter units, each with a namespaced p_index. `get_filter` is the
    app.filters.get_filter callable (only fetched for html fields, so a plain-only
    record never touches the html filter).

    Title is ALWAYS represented (even if absent from field_specs) so a record with
    only a Title still translates — Title is a standard Knowledge__kav field."""
    record = record or {}
    specs = list(field_specs or [])
    have_title = any(s.get("name") == title_field for s in specs)
    title_text = str(record.get(title_field) or "")

    ex = Extraction(
        title_field=(title_field if (have_title or title_text) else None), title_text=title_text
    )
    hash_parts = [title_text]
    _html = None

    for spec in specs:
        name = spec.get("name")
        if not name or name == title_field:
            continue
        ftype = spec.get("type")
        value = str(record.get(name) or "")
        if ftype == "html":
            if _html is None:
                _html = get_filter("html")
            raw = value.encode("utf-8")
            subs = []
            for u in _html.parse(raw).units:
                pidx = f"{name}#{u.p_index}"
                ex.body_units.append(_Unit(u.text, pidx))
                subs.append((pidx, u.p_index))
            ex._fields[name] = {"type": "html", "bytes": raw, "filter": _html, "subs": subs}
            hash_parts.append(value)
        else:
            # plain string/textarea: one unit, only when it carries content.
            if value.strip():
                ex.body_units.append(_Unit(value, name))
                ex._fields[name] = {"type": ftype, "pindex": name}
                hash_parts.append(value)

    ex.content_hash = content_hash(*hash_parts)
    return ex
