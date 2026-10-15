"""Language catalog + enabled-languages registry (P2, item 1).

Two tiers (RESEARCH_DIGEST item 1, Smartling/Phrase model):

  * THE CATALOG  — a curated BCP-47 list shipped as a static resource
    (``app/resources/languages.json``), loaded once and cached. It is the UNIVERSE
    of codes the app knows about (name + optional endonym). Editing the JSON changes
    the catalog with NO migration — it is a resource file, like ``default.srx``.

  * THE ENABLED SUBSET — a single ``enabled_locales`` Setting (JSON list), default =
    the digest's top-30. This is install-global config (exactly what ``Setting`` is
    for), so it reuses ``config apply``/``config get``. It is a UI-OFFERING FILTER,
    not a hard gate: the project picker + language-set editor offer the enabled
    subset by default (with a "show all" escape hatch), but ``is_valid`` accepts ANY
    catalog code even when it isn't enabled (hiding a language never breaks a project
    already using it — grandfathering).

This module is the ONE source of truth the web routes, the CLI, and the project
picker all share (the parity rule): ``all_locales`` / ``enabled_locales`` /
``set_enabled`` / ``name_for`` / ``is_valid`` / ``label_for``. ``html_lang`` and
``text_direction`` (a11y T5) turn any stored code into the HTML ``lang`` / ``dir`` of the
text written in it; the web layer's ``langattrs`` filter and GET /api/languages use them.

VALIDATION SEMANTICS (MASTER_PLAN P2 risk (h) + item 5): ``is_valid`` normalizes
(case-insensitive, underscore->hyphen) before matching, so a legacy freeform code
like ``es_ES`` or ``EN-us`` validates. It is deliberately lenient on shape — only
CLEARLY-unknown codes are rejected — and callers only validate NEW input (existing
``TargetLocale.locale`` rows are never re-validated).

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

import functools
import json
import re
import threading
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "resources" / "languages.json"

# The digest's top-30 default enabled set (RESEARCH_DIGEST item 1, verbatim order).
# Also the fallback when the enabled_locales Setting is unset/blank/corrupt.
DEFAULT_ENABLED = [
    "en-US",
    "en-GB",
    "zh-Hans",
    "zh-Hant",
    "es-ES",
    "es-419",
    "hi-IN",
    "ar",
    "pt-BR",
    "pt-PT",
    "fr-FR",
    "fr-CA",
    "ja-JP",
    "ru-RU",
    "de-DE",
    "id-ID",
    "ko-KR",
    "it-IT",
    "tr-TR",
    "pl-PL",
    "nl-NL",
    "vi-VN",
    "th-TH",
    "uk-UA",
    "bn-BD",
    "ur-PK",
    "fa-IR",
    "ro-RO",
    "sv-SE",
    "nb-NO",
]

# Cached catalog: the ordered list of {code,name,native?} dicts + a normalized
# lookup index. Loaded once (the JSON never changes at runtime).
_lock = threading.Lock()
_catalog: list[dict] | None = None
_index: dict[str, dict] | None = None


def _normalize(code) -> str:
    """Canonical comparison key for a locale code: trimmed, underscores->hyphens,
    lower-cased. So ``es_ES``, ``ES-es`` and ``es-ES`` all compare equal (BCP-47 is
    case-insensitive; we only store hyphens). Returns "" for blank/None."""
    return str(code or "").strip().replace("_", "-").lower()


def normalize(code) -> str:
    """Public comparison key for a locale code (see _normalize). Callers that dedup
    a NEW locale against EXISTING TargetLocale rows use this so a canonical new code
    (``es-ES``) is recognised as already-present when the stored row is a legacy /
    non-canonical spelling of the same locale (``ES-es`` / ``es_ES``) — those rows
    are grandfathered (never re-validated), and a connector's free-text config can
    seed them, so a case/underscore-only difference must NOT fork a project into two
    TargetLocale rows for one locale (P2 risk (h) / item 5)."""
    return _normalize(code)


def _load() -> tuple[list[dict], dict[str, dict]]:
    global _catalog, _index
    with _lock:
        if _catalog is not None and _index is not None:
            return _catalog, _index
        try:
            data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
            langs = data.get("languages", []) if isinstance(data, dict) else []
        except Exception:
            langs = []  # fall back to an empty catalog (validation then leans on
            #                 the enabled list / grandfathering).
        catalog, index = [], {}
        for row in langs:
            code = str(row.get("code", "")).strip()
            if not code:
                continue
            entry = {
                "code": code,
                "name": row.get("name") or code,
                "native": row.get("native") or "",
            }
            catalog.append(entry)
            index.setdefault(_normalize(code), entry)
        _catalog, _index = catalog, index
        return _catalog, _index


def all_locales() -> list[dict]:
    """The full catalog as ordered {code, name, native} dicts (a fresh copy)."""
    catalog, _ = _load()
    return [dict(x) for x in catalog]


def all_codes() -> list[str]:
    """Just the catalog's codes, in catalog order."""
    catalog, _ = _load()
    return [x["code"] for x in catalog]


def _canonical(code) -> str | None:
    """The catalog's canonical code for `code` (matching case-insensitively /
    underscore-normalized), or None when the code isn't in the catalog."""
    _, index = _load()
    hit = index.get(_normalize(code))
    return hit["code"] if hit else None


def canonical(code) -> str | None:
    """PUBLIC: the catalog's canonical spelling for `code` (case-insensitive /
    underscore-normalized match), or None when the code isn't in the catalog. The
    active-pairs seam (customers.confined_target_rows) canonicalizes a stored pair's
    target through this so a legacy spelling still matches its catalog row."""
    return _canonical(code)


def is_valid(code) -> bool:
    """True when `code` is a known catalog code (normalized match). This is the
    validation predicate used at every point locales ENTER (project create, add-
    locale, language sets, CLI) — it accepts ANY catalog code even if not currently
    enabled (the enabled list is a UI-offering filter, not a hard gate). Blank is
    NOT valid (callers skip blanks before validating)."""
    if not str(code or "").strip():
        return False
    return _canonical(code) is not None


def name_for(code) -> str:
    """The catalog English name for `code`, or the code itself when it isn't in the
    catalog (legacy/freeform codes display as-is — display-only fallback)."""
    _, index = _load()
    hit = index.get(_normalize(code))
    return hit["name"] if hit else str(code or "")


def label_for(code) -> str:
    """A UI label: "English name (code)" for a catalog code, else the raw code.
    Legacy codes not in the catalog render as just the code."""
    _, index = _load()
    hit = index.get(_normalize(code))
    if not hit:
        return str(code or "")
    return f"{hit['name']} ({hit['code']})"


# --- enabled subset (the enabled_locales Setting) ----------------------------
def enabled_locales() -> list[str]:
    """The install's enabled locale codes (the UI-offering subset). Reads the
    ``enabled_locales`` Setting (a JSON list); falls back to DEFAULT_ENABLED when
    unset / blank / unparseable. De-duplicated, order preserved."""
    from app.services import settings

    raw = settings.get("enabled_locales")
    codes: list[str] | None = None
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                codes = [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            codes = None
    if not codes:
        codes = list(DEFAULT_ENABLED)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def rows_for(codes) -> list[dict]:
    """{code, name, native} rows for an EXPLICIT list of codes, in the given order
    (de-duped on the normalized key, first spelling wins). A catalog code is
    canonicalized + named; a code not in the catalog renders with the code as its
    own name (mirrors enabled_language_rows' legacy handling). This is the shared
    row-builder the langpicker `enabled` slot uses — enabled_language_rows() is just
    rows_for(enabled_locales()), and the active-pairs seam feeds it a customer's
    confined target codes so the picker offers exactly those by default."""
    out, seen = [], set()
    for code in codes or []:
        canon = _canonical(code)
        key = _normalize(canon or code)
        if not key or key in seen:
            continue
        seen.add(key)
        if canon:
            out.append({"code": canon, "name": name_for(canon), "native": _index_native(canon)})
        else:
            out.append({"code": code, "name": code, "native": ""})
    return out


def enabled_language_rows() -> list[dict]:
    """The enabled subset as {code, name, native} rows, for the picker. A code that
    is enabled but not in the catalog (e.g. a hand-added legacy code) still renders
    with the code as its name."""
    return rows_for(enabled_locales())


def _index_native(code) -> str:
    _, index = _load()
    hit = index.get(_normalize(code))
    # Always a string: a catalog row may omit `native` (or carry JSON null), which
    # would otherwise surface as None and render as the literal "None" in the
    # picker's data-label search haystack.
    return (hit.get("native") or "") if hit else ""


def set_enabled(codes, actor: str = "system") -> list[str]:
    """Set the enabled subset. `codes` is a list (or comma/space string) of catalog
    codes. Unknown codes are DROPPED (canonicalized against the catalog first) so the
    stored list is always clean; duplicates collapse. Persists the ``enabled_locales``
    Setting (JSON) and audits ``languages.enabled``. Returns the stored codes.

    An empty result is allowed (an admin may disable everything — the picker then
    offers only the "show all" escape hatch), but is unusual."""
    from app.services import audit, settings

    if isinstance(codes, str):
        codes = codes.replace(",", " ").split()
    clean, seen = [], set()
    for c in codes or []:
        canon = _canonical(c)
        if canon and canon not in seen:
            seen.add(canon)
            clean.append(canon)
    settings.set_many({"enabled_locales": json.dumps(clean)})
    audit.log(
        actor=actor or "system",
        action="languages.enabled",
        detail=f"{len(clean)} locale(s) enabled",
    )
    return clean


# --- HTML language of parts (a11y T5, WCAG 3.1.2) -------------------------------
# `html_lang` and `text_direction` turn a STORED locale code into the value of an HTML
# `lang` / `dir` attribute on the element that holds source or target text. They are
# display helpers, not validation: a code need not be in the catalog (a legacy
# TargetLocale row such as `es_ES` or `EN-us` is grandfathered and must still be
# tagged), but it must be a well-formed BCP-47 tag, and anything else (blank, a dash
# placeholder, `*`, junk) returns "" so the caller emits no attribute at all. Wrong
# `lang` is worse than none, and neither function may ever raise: a template calls
# them on every row, and a garbage legacy code must never 500 a page.

_LANG_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")

# Scripts written right to left (ISO 15924). When a tag carries a script subtag, the
# script alone decides the direction (`ku-Arab` rtl, `ug-Latn` ltr, `sd-Deva` ltr).
_RTL_SCRIPTS = frozenset(
    {"arab", "hebr", "syrc", "thaa", "nkoo", "adlm", "rohg", "samr", "mand", "mend", "yezi"}
)

# Base languages whose DEFAULT script is right to left (used only when the tag has no
# script subtag). `ku` (Kurmanji, Latin) and `pa` (Gurmukhi) are deliberately absent:
# their Arabic-script forms are `ku-Arab` / `ckb` and `pa-Arab` / `pnb`.
_RTL_LANGUAGES = frozenset(
    {
        "ar",
        "arc",
        "azb",
        "ckb",
        "dv",
        "fa",
        "glk",
        "he",
        "iw",
        "ji",
        "ks",
        "lrc",
        "mzn",
        "nqo",
        "pnb",
        "prs",
        "ps",
        "sd",
        "syr",
        "ug",
        "ur",
        "yi",
    }
)


def _as_text(code) -> str | None:
    """`code` as a stripped string, or None when it cannot be one. Never raises (an
    object whose __str__ fails is treated as unusable)."""
    if code is None:
        return ""
    try:
        return str(code).strip()
    except Exception:
        return None


@functools.lru_cache(maxsize=1024)
def _html_lang_cached(text: str) -> str:
    tag = text.replace("_", "-")
    if len(tag) > 64 or not _LANG_TAG_RE.match(tag):
        return ""
    parts = tag.split("-")
    out = [parts[0].lower()]
    extension = False  # after a singleton (x-, u-, t-) subtags are not script/region
    for sub in parts[1:]:
        if len(sub) == 1:
            extension = True
            out.append(sub.lower())
        elif extension:
            out.append(sub.lower())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub.title())  # script: zh-Hant, sr-Latn
        elif (len(sub) == 2 and sub.isalpha()) or (len(sub) == 3 and sub.isdigit()):
            out.append(sub.upper())  # region: es-ES, es-419
        else:
            out.append(sub.lower())  # variant
    return "-".join(out)


def html_lang(code) -> str:
    """The HTML `lang` value for a stored locale code, or "" when there is none usable.

    Underscores become hyphens and case is canonicalized (language lower, Script title,
    REGION upper), so a legacy `es_ES` gives `es-ES` and `EN-us` gives `en-US`. Blank,
    None, a dash placeholder, `*` and anything that is not a well-formed BCP-47 tag
    (quotes, angle brackets, spaces) give "". Never raises."""
    text = _as_text(code)
    if not text:
        return ""
    return _html_lang_cached(text)


@functools.lru_cache(maxsize=1024)
def _direction_cached(tag: str) -> str:
    parts = tag.split("-")
    for sub in parts[1:]:
        if len(sub) == 1:
            break  # extensions never carry the script
        if len(sub) == 4 and sub.isalpha():
            return "rtl" if sub.lower() in _RTL_SCRIPTS else "ltr"
    return "rtl" if parts[0] in _RTL_LANGUAGES else "ltr"


def text_direction(code) -> str:
    """ "rtl" or "ltr" for a stored locale code, or "" when `html_lang` finds no usable
    tag. A script subtag decides (`ku-Arab`, `pa-Arab`, `az-Arab` rtl; `sd-Deva`,
    `ug-Latn` ltr); without one the base language does (`ar`, `he`, `fa-IR`, `ur` rtl;
    `ku` ltr). Never raises."""
    tag = html_lang(code)
    if not tag:
        return ""
    return _direction_cached(tag)


def validate_codes(codes) -> tuple[list[str], list[str]]:
    """Split `codes` (list or comma/space string) into (valid, invalid) against the
    catalog, canonicalizing the valid ones. Blanks are ignored. Order preserved,
    duplicates within `valid` collapsed. Used by the create/add-locale/langset paths
    to reject garbage with a clear error while accepting disabled-but-valid codes."""
    if isinstance(codes, str):
        codes = codes.replace(",", " ").split()
    valid, invalid, seen = [], [], set()
    for c in codes or []:
        c = str(c).strip()
        if not c:
            continue
        canon = _canonical(c)
        if canon is None:
            invalid.append(c)
        elif canon not in seen:
            seen.add(canon)
            valid.append(canon)
    return valid, invalid
