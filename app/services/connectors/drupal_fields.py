"""Drupal connector: pure field helpers (classification, text units, payload assembly).

Everything here is a pure function of a JSON:API `attributes` dict: no I/O, no connection
state, so it is unit-testable and the connector module stays small.

FIELD MODEL. A JSON:API entity resource carries its fields under `attributes`. The shapes
that matter for translation:

  plain string      "title": "Hello"                       one text unit
  formatted text    "body": {"value": "<p>..</p>", "format": "basic_html", "summary": "",
                             "processed": "<p>..</p>"}    HTML (or plain) units
  everything else   numbers, booleans, dates, links, path alias, lists, ...   NOT translated

Drupal's `processed` is a computed, read-only rendering of `value`: it is never extracted and
never written. `format` (the text format) is carried verbatim: dropping or changing it can
strip formatting or trip the site's filter sanitisation.

Which fields are TRANSLATABLE (not shared across languages) is a site configuration that core
JSON:API does not expose to a content token. So selection is by name and value shape, with an
operator `fields` allowlist that overrides it entirely (the recommended path for a known
schema). A wrongly selected shared field is not silently overwritten: the JSON:API
Multilingual module answers a write that names a non-translatable field with an error, and the
run reports it as a failed pair (live-tenant QA item MANDATORY-2).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re

# --- held-back marker (same contract as the other T2 connectors) -----------------------
HELD_BACK_PREFIX = "held-back:"
HELD_BACK = (
    HELD_BACK_PREFIX + " every translatable field was held for review (NEEDS_REVIEW); "
    "nothing was written and it will be retried next run"
)

# A pair whose existing translation is already PUBLISHED (live) is left alone unless the operator
# opted in (`edit_published`): a draft-mode run must never silently rewrite live content. The
# pair is reported (not hidden) under this prefix and is not a failure of the run.
LIVE_PREFIX = "live-translation:"

# Strategy-injected key (SubResourceStrategy adds `draft: bool` to every payload).
INJECTED_KEYS = frozenset({"draft"})

# Entity types this connector will touch. All four carry a `status` (published) base field
# and per-language translations. `user` (status = blocked!), config entities and paragraphs
# (translated through their host) are deliberately NOT supported.
ALLOWED_ENTITY_TYPES = frozenset({"node", "taxonomy_term", "block_content", "media"})

# JSON:API resource type = entity_type--bundle (machine names).
TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}--[a-z][a-z0-9_]{0,63}")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Drupal language ids: letters, digits, '-' and '_' (zh-hans, pt-br, x-klingon).
LANG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
PREFIX_RE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")

# Attribute names that are never translatable content (base fields and bookkeeping).
SYSTEM_ATTRS = frozenset(
    {
        "draft",  # the key SubResourceStrategy injects into every payload
        "langcode",
        "status",
        "default_langcode",
        "promote",
        "sticky",
        "created",
        "changed",
        "path",
        "uuid",
        "vid",
        "nid",
        "tid",
        "id",
        "type",
        "bundle",
        "uid",
        "weight",
        "parent",
        "moderation_state",
        "metatag",
        "menu_link",
        "publish_on",
        "unpublish_on",
        "reusable",
        "revision_timestamp",
        "revision_log",
        "revision_translation_affected",
    }
)
_SYSTEM_PREFIXES = ("drupal_internal__", "content_translation_", "revision_")

_ID_WORDS = frozenset({"slug", "uid", "uuid", "alias", "url", "uri", "email", "mail", "phone"})
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^(?:https?|ftp)://|^www\.", re.I)
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)

# Text formats whose `value` is plain text (no HTML markup): one unit, no HTML parse.
PLAIN_FORMATS = frozenset({"plain_text", "plain"})


class Unit:
    """A flow translation unit (the engine reads only .text and .p_index). p_index is the
    unit KEY ('title', 'body.value#0', 'body.summary'), so build_payload reinserts each
    translation exactly where it came from."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


def parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _words(name: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(name or "")}


def is_system_attr(name: str) -> bool:
    n = str(name)
    return n in SYSTEM_ATTRS or n.startswith(_SYSTEM_PREFIXES)


def is_formatted(value) -> bool:
    """A formatted-text field value: a dict with a string `value` and a `format` key."""
    return isinstance(value, dict) and isinstance(value.get("value"), str) and "format" in value


def classify(name: str, value) -> tuple[bool, str]:
    """(translate?, reason). Conservative: skip a system attribute, a non-text shape, an id /
    slug / url / email NAME, or an unambiguous machine VALUE (email, URL, ISO date)."""
    if is_system_attr(name):
        return False, "system field"
    if is_formatted(value):
        if not (value.get("value") or "").strip() and not (value.get("summary") or "").strip():
            return False, "empty"
        return True, "formatted text"
    if isinstance(value, dict):
        return False, "structured value (link/path/other) - not translated by this connector yet"
    if isinstance(value, list):
        return False, "multi-value field - not translated by this connector yet"
    if not isinstance(value, str):
        return False, f"non-text value ({type(value).__name__})"
    if not value.strip():
        return False, "empty"
    if _words(name) & _ID_WORDS or name.lower().endswith("_id") or name.lower() == "id":
        return False, f"field name looks non-translatable ({name})"
    if _EMAIL_RE.match(value):
        return False, "email-shaped value"
    if _URL_RE.match(value):
        return False, "URL-shaped value"
    if _DATETIME_RE.match(value):
        return False, "date/time-shaped value"
    return True, "text"


def pick_fields(attrs: dict, allow: list[str], exclude: set) -> tuple[list[str], dict]:
    """(selected_names, {skipped_name: reason}). The operator allowlist wins outright: a
    listed field is translated when it holds a non-empty text or formatted value, and a
    listed system attribute is refused with a visible reason."""
    attrs = attrs or {}
    selected: list[str] = []
    skipped: dict = {}
    if allow:
        for n in allow:
            v = attrs.get(n)
            if is_system_attr(n):
                skipped[n] = "system/reserved field - cannot be translated"
            elif is_formatted(v) or (isinstance(v, str) and v.strip()):
                selected.append(n)
            else:
                skipped[n] = "listed in `fields` but not a non-empty text field on this entity"
        return selected, skipped
    for k, v in attrs.items():
        if k in exclude:
            skipped[k] = "operator exclude_fields"
            continue
        ok, reason = classify(k, v)
        if ok:
            selected.append(k)
        elif not is_system_attr(k):  # keep system keys out of the report
            skipped[k] = reason
    return selected, skipped


# --- units ------------------------------------------------------------------------------
def _html_filter():
    from app.filters import get_filter

    return get_filter("html")


def text_parts(name: str, value) -> list[dict]:
    """The translatable parts of one selected field, each: {key, mode, raw, units}.
    mode 'text' = one unit keyed `<key>`; mode 'html' = one unit per HTML block keyed
    `<key>#<p_index>`. A plain string field is a single 'text' part keyed by its name."""
    if isinstance(value, str):
        return [{"key": name, "mode": "text", "raw": value, "units": [Unit(value, name)]}]
    fmt = str(value.get("format") or "")
    parts: list[dict] = []
    for sub in ("value", "summary"):
        raw = value.get(sub)
        if not isinstance(raw, str) or not raw.strip():
            continue
        key = f"{name}.{sub}"
        if fmt.lower() in PLAIN_FORMATS:
            parts.append({"key": key, "mode": "text", "raw": raw, "units": [Unit(raw, key)]})
            continue
        parsed = _html_filter().parse(raw.encode("utf-8"))
        units = [Unit(u.text, f"{key}#{u.p_index}") for u in parsed.units if (u.text or "").strip()]
        parts.append({"key": key, "mode": "html", "raw": raw, "units": units})
    return parts


def plan_field(name: str, value) -> dict:
    """One selected field's write plan: its parts and the original dict (for key mirroring)."""
    return {
        "name": name,
        "formatted": isinstance(value, dict),
        "orig": dict(value) if isinstance(value, dict) else None,
        "parts": text_parts(name, value),
    }


def field_payload(plan: dict, mapping: dict):
    """The attribute value to write for one planned field, or None when ANY of its non-empty
    units is held back (the whole field is then omitted so its stored translation survives:
    a formatted field is replaced as a whole, so a half-translated one must never be sent)."""
    built: dict = {}
    for part in plan["parts"]:
        units = [u for u in part["units"] if (u.text or "").strip()]
        if any(u.p_index not in mapping for u in units):
            return None
        if part["mode"] == "text":
            built[part["key"]] = mapping[units[0].p_index] if units else part["raw"]
        else:
            texts = {u.p_index.rsplit("#", 1)[1]: mapping[u.p_index] for u in units}
            built[part["key"]] = (
                _html_filter().build(part["raw"].encode("utf-8"), texts).decode("utf-8")
            )
    if not plan["formatted"]:
        return built.get(plan["name"])
    orig = plan["orig"] or {}
    out: dict = {}
    for sub in ("value", "summary"):
        if sub in orig:
            out[sub] = built.get(f"{plan['name']}.{sub}", orig.get(sub))
    out["format"] = orig.get("format")  # carried verbatim, never translated
    return out
