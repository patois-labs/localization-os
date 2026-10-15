"""strapi: field-classification heuristics (review h3, tightened per n2)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

from app.services.connectors import (
    flow,
)

log = logging.getLogger("loc_tms.connectors")


API_VERSION = "v5"  # DEFAULT Strapi major (config-overridable)


_SUPPORTED_VERSIONS = ("v5", "v4")


_SYSTEM_FIELDS = frozenset(
    {
        "id",
        "documentId",
        "locale",
        "localizations",
        "locales",
        "createdAt",
        "updatedAt",
        "publishedAt",
        "createdBy",
        "updatedBy",
    }
)


_RESERVED_WRITE_KEYS = frozenset({"draft"})


_ALIAS_PREFIX = "\x00field:"


_TITLE_FALLBACK_KEY = "\x00title_fallback"


_HELD_BACK_PREFIX = "held-back:"


_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)


_NAME_SKIP_EXACT = frozenset(
    {
        "slug",
        "uid",
        "url",
        "link",
        "href",
        "email",
        "mail",
        "code",
        "key",
        "locale",
        "lang",
        "language",
        "permalink",
        "handle",
        "token",
        "password",
        "secret",
        "status",
        "state",
        "currency",
        "timezone",
        "tz",
        # slug/uid-ROLE names whose value can change a URL or clash on uniqueness if MT'd
        # (N2 swing-back — same corruption class as the original H3).
        "path",
        "alias",
        "identifier",
        "reference",
        "ref",
    }
)


_NAME_ID_TOKENS = frozenset({"slug", "uid"})


_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


_CAMEL_ID_RE = re.compile(r"[a-z0-9]Id$")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


_URL_RE = re.compile(r"^(?:https?|ftp)://|^www\.", re.I)


_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


_TIME_RE = re.compile(r"^\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?$")


# DEFAULT Strapi major (config-overridable)
# Strapi entry keys that are NEVER translatable content.
# Keys the shared SubResourceStrategy INJECTS into the write payload. A real Strapi
# content field of the same name is protected by aliasing (see _stash / _build_data),
# so it survives the injection and reaches Strapi (review M2).
# NUL-prefixed sentinels — never a valid Strapi field name, so they cannot collide
# with real content.
# WriteResult.message marker for a (entry, locale) whose every translatable field was
# held back (NEEDS_REVIEW) this run — written as ok=False so the flow records NO
# suppression hash, then accounted as `held_back` (not a real failure) in the report (N3).
# _HELD_BACK_PREFIX is the single source of truth for the marker: the full message and
# the _is_held_back / message-filter classifiers all key off it, so they cannot drift.
# --- field-classification heuristics (review H3, tightened per N2) -----------
# Field NAMES that are almost never free translatable text — matched as the WHOLE
# name only (an EXACT match), so a compound like `emailSubject`, `statusMessage` or
# `sourceCode` (all translatable copy) is NOT skipped by name.
# slug/uid-ROLE names whose value can change a URL or clash on uniqueness if MT'd
# (N2 swing-back — same corruption class as the original H3).
# camelCase / snake WORD components that mark an id/slug field even inside a compound
# (`metaSlug` -> {meta, slug}; `productUid` -> {product, uid}). Deliberately tiny: a
# looser set would eat `keyMessage`/`guide`/`guidance` (N2 — "guide" contains "uid"
# only as a substring, never as a word). Word-splitting is what distinguishes them.
def _name_words(name: str) -> set:
    """Lowercased word components of a field name (camelCase + snake/kebab split), so
    an id marker is matched as a WORD, never as an incidental substring (N2)."""
    return {w.lower() for w in _WORD_RE.findall(name or "")}


def _name_looks_id(name: str) -> bool:
    lname = name.lower()
    return (
        lname in _NAME_SKIP_EXACT
        or bool(_name_words(name) & _NAME_ID_TOKENS)
        or lname == "id"
        or lname.endswith("_id")
        or bool(_CAMEL_ID_RE.search(name))
    )


def _classify_field(name: str, value) -> tuple[bool, str]:
    """(translate?, reason). Conservative but not over-eager (N2): a field is skipped
    only when its NAME is an id/slug marker (exact or a whole word component) OR its
    VALUE is an unambiguous machine format (email / url / ISO date-time). Value SHAPE
    alone (a hyphenated string like `sign-up`, or a long lowercase word like
    `internationalization`) does NOT skip — that would eat ordinary UI copy. Fails
    safe: an un-caught non-text field round-trips its string harmlessly and is reported."""
    if name in _SYSTEM_FIELDS:
        return False, "system field"
    if isinstance(value, (list, dict)):
        return False, "rich content (blocks/JSON/component) - not translated by this connector yet"
    if not isinstance(value, str):
        return False, f"non-text value ({type(value).__name__})"
    if not value.strip():
        return False, "empty"
    if _name_looks_id(name):
        return False, f"field name looks non-translatable ({name})"
    if _EMAIL_RE.match(value):
        return False, "email-shaped value"
    if _URL_RE.match(value):
        return False, "URL-shaped value"
    if _DATETIME_RE.match(value) or _TIME_RE.match(value):
        return False, "date/time-shaped value"
    return True, "text"


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index).
    p_index is the FIELD NAME, so build_payload reinserts each translation into the
    exact field it came from."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _stash(out: dict, name: str, value) -> None:
    """Place a translated field into the write payload, ALIASING a name that collides
    with a strategy-injected reserved key ('draft') so the strategy's later injection
    cannot clobber a real content field (review M2). _build_data un-aliases it."""
    out[(_ALIAS_PREFIX + name) if name in _RESERVED_WRITE_KEYS else name] = value


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (no watermark): the content-hash gate is the change detector.
# ===========================================================================
def _strapi_preflight(ctx):
    enabled = set(ctx.connector.enabled_locales())
    return enabled, {"source": ctx.connector._source_locale().lower()}


def _strapi_partition(locales, enabled, extra):
    """usable = enabled targets minus the source locale; rejected = not enabled. The
    source locale is DROPPED (not rejected) — surfaced honestly in the message (L3)."""
    src = (extra or {}).get("source", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _strapi_prepare(ctx, art):
    title_field, title_text, body_units, chash, body_names = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = ctx.connector._aid(art)
    fields = art.get("fields") or {}
    disp = str(fields.get(title_field) or "") if title_field else ""
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={"article_id": aid, "title": disp or None, "content_type": art.get("_ct")},
        doc_title=f"{(disp or art.get('_ct') or 'entry')[:50]} ({aid})",
        extra={"title_field": title_field, "title_text": title_text, "body_names": body_names},
    )


def _strapi_is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _strapi_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one locale. A translated field is emitted under its real
    name (aliased if it is a reserved key). A HELD-BACK (NEEDS_REVIEW) title is NOT
    emitted as a real value — it rides a private fallback key that _build_data honours
    ONLY on CREATE (source seed), so an UPDATE keeps the current stored title (H2).
    Held-back body fields are omitted entirely (never write source as a stand-in)."""
    title_field = prep.extra["title_field"]
    out: dict = {}
    if title_field is not None:
        if seg0_target is not None:
            _stash(out, title_field, seg0_target)
        else:
            out[_TITLE_FALLBACK_KEY] = (title_field, prep.extra["title_text"])
    for name in prep.extra["body_names"]:
        if name in mapping:
            _stash(out, name, mapping[name])
    return out


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _held_back(ctx):
    """(entry, locale) pairs held back for review this run — surfaced as their OWN
    counter, never counted as created/updated and never a hard failure (review N3)."""
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]
