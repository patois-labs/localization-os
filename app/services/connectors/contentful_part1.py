"""contentful: part1

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass

import regex

from app.filters import contentful_rich_text as rich_text
from app.services.connectors import base, flow
from app.services.connectors.base import (
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    harvest_context_field,
)
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _record_write_lock_for,
)

log = logging.getLogger("loc_tms.connectors")


API_VERSION = "CMA-v1"  # Contentful Content Management API (pinned semantics)


CMA_CONTENT_TYPE = "application/vnd.contentful.management.v1+json"


_REGION_BASES = {
    "US": "https://api.contentful.com",
    "EU": "https://api.eu.contentful.com",
}


_TRANSLATABLE_TYPES = frozenset({"Symbol", "Text", "RichText"})


_SYMBOL_MAX = 256


_MAX_VERSION_RETRIES = 4


_MAX_TOTAL_CONFLICT_WAIT = 60.0


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


# Contentful Content Management API (pinned semantics)
# Regional CMA hosts (Contentful EU data residency). Region pins the base host — the
# wrong host silently returns data-not-found, so it is a first-class field.
# The field types this connector translates (Symbol, Text and, since #648, RichText
# through app/filters/contentful_rich_text.py). Everything else (Object, Location,
# Boolean, Integer, Number, Date, Link, Array) is skipped + reported.
# Contentful's hard Symbol (short text) length limit — enforced pre-write so a length-
# expanding translation (German/Finnish on a ~200-char English Symbol) is held back per
# field instead of 422-ing the WHOLE entry PUT and discarding the good translations (H2).
# Bounded refetch-remerge-retry on X-Contentful-Version conflicts (409). Exhausted ->
# abort the pair (no hash recorded), so the next run retries. Never clobbers.
# Explicit ceiling on the CUMULATIVE 409-backoff wait for ONE pair (review F6/round-3
# finding 7). Set BELOW the natural worst case (3 sleeps x MAX_SLEEP=30 = 90s) so it is a
# REAL bound: a hostile/looping X-Contentful-RateLimit-Reset stream stops backing off
# after 60s and lets the remaining attempts run immediately. This bounds ONE pair; a run
# with N conflicting pairs can still spend up to ~60N s of backoff across the run.
# --- auto-selection skip heuristics (review H1) ------------------------------
# Ported from the already-reviewed Strapi skip layer (strapi.py:108-162): a LOCALIZED
# Symbol is NOT automatically translatable copy — Contentful users localize slugs,
# canonical URLs, codes and dates. Auto-selection therefore skips a field when its NAME
# is an id/slug marker OR its VALUE is an unambiguous machine format. The explicit
# `fields` allowlist bypasses these heuristics (operator override), still respecting the
# localized + text-type gates. Value SHAPE alone (a hyphenated word) does NOT skip.
def _name_words(name: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(name or "")}


def _name_looks_id(name: str) -> bool:
    lname = (name or "").lower()
    return (
        lname in _NAME_SKIP_EXACT
        or bool(_name_words(name) & _NAME_ID_TOKENS)
        or lname == "id"
        or lname.endswith("_id")
        or bool(_CAMEL_ID_RE.search(name or ""))
    )


# Remote-supplied regexp guards (red-team audit 2026-10-03, finding 01). A field's
# `regexp` validation is attacker-controllable text from the remote space, executed on
# a sync-worker thread during every validate_value. stdlib `re` has no match timeout
# (a (a+)+$-class pattern grows ~4x per +2 input chars and never returns); the `regex`
# module does, so remote patterns run through it with a HARD per-match wall-clock cap,
# plus a raw pattern-length ceiling applied before compiling.
_REMOTE_REGEX_TIMEOUT_S = 0.2


_MAX_REMOTE_REGEX_PATTERN_LEN = 1000


def _compile_js_regex(pattern: str, flags, where: str = ""):
    """Compile a Contentful (JS) regexp validation into a `regex` pattern, mapping the JS
    flags we can honour faithfully (i->IGNORECASE, m->MULTILINE, s->DOTALL) and IGNORING
    the ones with no faithful Python equivalent (u is Python3's default; g/y are stateful
    match modes, irrelevant to a `search`). Returns the compiled pattern, or None when it
    is over-long or cannot be compiled — the caller then FAILS OPEN (accepts) rather than
    false-holding a value the CMA would accept (review F3). Every rejection logs a
    warning naming `where` (the content type + field) — never the value. The caller runs
    matches with timeout=_REMOTE_REGEX_TIMEOUT_S and treats a timeout the same way."""
    if len(pattern) > _MAX_REMOTE_REGEX_PATTERN_LEN:
        log.warning(
            "contentful regexp validation on %s rejected: %d chars exceeds the %d-char "
            "pattern limit (validation skipped, value accepted)",
            where or "a remote field",
            len(pattern),
            _MAX_REMOTE_REGEX_PATTERN_LEN,
        )
        return None
    py_flags = 0
    for ch in str(flags or ""):
        if ch == "i":
            py_flags |= regex.IGNORECASE
        elif ch == "m":
            py_flags |= regex.MULTILINE
        elif ch == "s":
            py_flags |= regex.DOTALL
        # u / g / y and anything else: no faithful Python equivalent -> ignore
    try:
        return regex.compile(pattern, py_flags)
    except regex.error:
        log.warning(
            "contentful regexp validation on %s is not a valid pattern "
            "(validation skipped, value accepted)",
            where or "a remote field",
        )
        return None  # unparseable pattern -> fail open


def _value_is_machine(value: str) -> str | None:
    """Return a skip reason when the source VALUE is an unambiguous machine format
    (email / url / ISO date-time / time), else None. Value shape alone (hyphenated UI
    copy) is NOT a machine format."""
    v = (value or "").strip()
    if _EMAIL_RE.match(v):
        return "email-shaped value"
    if _URL_RE.match(v):
        return "URL-shaped value"
    if _DATETIME_RE.match(v) or _TIME_RE.match(v):
        return "date/time-shaped value"
    return None


_HELD_BACK_PREFIX = "held-back:"


_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)


_VBLOCK_PREFIX = "validation-blocked:"


# WriteResult.message marker for an (entry, locale) whose every translatable field was
# held back (NEEDS_REVIEW) this run: written as ok=False so the flow records NO
# suppression hash (taxonomy F), then accounted as `held_back` (not a real failure).
# WriteResult.message marker for a DETERMINISTIC validation block (review F1a/F2): either
# every candidate field's translation fails CMA validation, or a PRE-EXISTING echoed value
# violates a (later-added) validation so the whole entry PUT would 422. Re-translating is
# pure waste (the outcome is deterministic for the same source), so — unlike a hard
# failure — a blocked pair is recorded durably (content-hash keyed) and the scheduled path
# SKIPS it WITHOUT spending MT. A source edit (hash change) or retry_failed re-attempts it.
class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index).
    p_index is the FIELD ID, so build_payload reinserts each translation into the
    exact field it came from."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


@dataclass
class _PutOutcome:
    """Result of one CMA entry PUT: ok (2xx), conflict (409 version mismatch), or a
    terminal failure carrying a message. retry_after carries the server's requested
    wait on a conflict, if a client ever supplies one (Contentful sends none on a 409);
    status is the HTTP status (so a terminal 422 can be classified as a deterministic
    validation block rather than a generic retryable failure, review F2)."""

    ok: bool
    conflict: bool
    message: str = ""
    retry_after: float | None = None
    status: int | None = None


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _validate_segment(value: str, what: str) -> str:
    """Reject an operator/caller string that could escape or retarget the CMA URL
    (defect taxonomy A). A real Contentful space id / environment id / entry id /
    content-type id is `[A-Za-z0-9._-]`-ish and contains NONE of these. Rejects
    '/', '?', '#', '\\', whitespace, '..' and dots-only — so `..`, `%2e%2e`
    (already decoded by the caller), a path separator, a query/fragment injector,
    or a CRLF cannot reach the wire. Percent-encoding at the call site is defence in
    depth on top of this."""
    v = str(value or "").strip()
    if not v:
        raise ConnectorError(f"{what} is required.")
    if (
        set(v) <= {"."}
        or ".." in v
        or any(ch in v for ch in ("/", "?", "#", "\\"))
        or any(ch.isspace() for ch in v)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)
    ):  # NUL / CR / LF / DEL etc.
        raise ConnectorError(
            f"Unsafe {what} {value!r}: may not contain '/', '?', '#', '..', "
            f"whitespace, control characters or be dots-only."
        )
    return v


SPEC = ConnectorSpec(
    kind="contentful",
    name="Contentful",
    icon="\U0001f4e6",  # package
    cat="Headless CMS",
    blurb="Translate Contentful entries into any language. Each translation is written "
    "back as a new locale key on the SAME entry (fields.{id}.{locale}) via a "
    "version-locked read-merge-write, so the source locale, other locales and "
    "non-translated fields are never overwritten. Writes update the entry (draft) "
    "and never auto-publish.",
    config_fields=[
        FieldSpec(
            "space_id",
            "Space ID",
            "Your Contentful Space ID (Settings -> General "
            "settings). May not contain '/', '?', '#', '..' or whitespace.",
            placeholder="abc123xyz",
        ),
        FieldSpec(
            "environment",
            "Environment",
            "The environment to target (default 'master'). All reads and writes are scoped to it.",
            required=False,
            placeholder="master",
        ),
        FieldSpec(
            "region",
            "Data region",
            "'US' (api.contentful.com, default) or 'EU' "
            "(api.eu.contentful.com). The wrong region silently returns no data.",
            required=False,
            placeholder="US",
        ),
        FieldSpec(
            "content_types",
            "Content type IDs",
            "Comma-separated content-type IDs "
            "(the API identifiers) to translate, e.g. 'blogPost, page'. May not "
            "contain '/', '?', '#', '..' or whitespace.",
            placeholder="blogPost, page",
        ),
        FieldSpec(
            "fields",
            "Translatable fields",
            "Optional allowlist of field IDs to "
            "translate (comma-separated). Blank = auto-select every LOCALIZED "
            "short/long text (Symbol/Text) and Rich Text field; the run report lists what was "
            "selected and skipped (non-localized and non-text fields are excluded). "
            "Rich Text fields are translated block by block, keeping bold/italic, links "
            "and embedded entries and assets.",
            required=False,
            placeholder="title, summary",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude fields",
            "Field IDs to NEVER translate "
            "(comma-separated). Applies only when the allowlist above is blank.",
            required=False,
            placeholder="slug, internalNote",
        ),
        FieldSpec(
            "title_field",
            "Title field",
            "Field ID to use as the entry's display "
            "title (default: the content type's display field, or 'title'/'name').",
            required=False,
            placeholder="title",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The authoring locale to translate FROM (default: the space's default locale).",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Contentful locale "
            "codes to write (must be enabled on the space/environment).",
            required=False,
            placeholder="de-DE, fr, es",
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
            "cma_token",
            "Management API token",
            "A Content Management API token "
            "(Settings -> API keys -> Content management tokens -> Generate personal "
            "token) with access to the space. Sent as a Bearer token.",
            secret=True,
            placeholder="CFPAT-...",
        ),
    ],
    setup_steps=[
        "In Contentful, note your Space ID (Settings -> General settings) and the "
        "environment you want to translate (default 'master').",
        "Enable your target locales under Settings -> Locales (each must be enabled on the "
        "environment you target).",
        "On each content type, mark the text fields you want translated as localized "
        "(field settings -> 'Enable localization of this field').",
        "Create a Content Management API token (Settings -> API keys -> Content management "
        "tokens -> Generate personal token) and paste it as the Management API token.",
        "Enter the Space ID, the content-type IDs (e.g. 'blogPost'), the token, and your "
        "target locales. Set the Data region to 'EU' only if your space is on the EU "
        "residency host.",
    ],
    other_side="Change detection is poll-based: run on a schedule. Contentful's updatedAt "
    "bumps on any change, so unchanged entries are suppressed by a per-(entry, "
    "locale) content hash rather than a timestamp watermark. Writes update the "
    "entry (its draft) with an X-Contentful-Version lock and never auto-publish; "
    "publish each locale in Contentful when ready. Concurrent external edits "
    "during a write are handled by version-conflict retry, then abort-and-retry-"
    "next-run (never a clobber).",
    docs_url="https://www.contentful.com/developers/docs/references/content-management-api/",
    supports_oauth=False,
)


# package
class ContentfulEntryStrategy(LocaleMappingStrategy):
    """T1 write-back tailored to Contentful's per-field locale maps + optimistic
    concurrency (see the module docstring for why the generic FieldMapStrategy does
    not fit). Vendor-neutral in the same spirit as the shared strategies: it drives a
    small client protocol the connector implements. These are connector methods called
    ACROSS the strategy/connector boundary (kept on the connector because they need its
    HTTP client + schema cache); the leading underscore marks connector-internal, not
    "private from the strategy":

        connection_id                         -> keys the shared D1 record lock
        resolve_locale(locale) -> exact code   (lower-cased flow locale -> wire case)
        entry_snapshot(entry_id) -> {"fields", "version", "ct", "metadata"} | ABSENT
        localizable_field_ids(ct) -> set        (localized AND translatable-type)
        validate_value(ct, fid, value) -> reason|None  (pre-write CMA validation, H2/F2/F3)
        write_entry(entry_id, fields, version, metadata) -> _PutOutcome (carries .status)
        _record_incomplete(entry_id, locale, {field: reason})  (partial write, H2/M1)
        _record_blocked(entry_id, locale, {what: reason})      (deterministic block, F1a/F2)
        _record_vheld(entry_id, locale, {field: reason})       (validation hold, #665)

    write_target overlays ONLY the translated delta onto a freshly-read snapshot and
    PUTs with the read version; on 409 it re-reads and re-applies the delta (bounded, with
    a cumulative-wait ceiling), so it never re-sends a stale view of the other
    locales/fields. Values are validated pre-wire (delta AND echoed pre-existing values);
    a per-field failure is held back (incomplete), an all-field or pre-existing failure is
    a deterministic BLOCK (no MT re-spend), and a non-localizable-only payload is a loud
    per-pair failure.
    """

    topology = "T1"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True iff the target locale key is present on ANY field THIS CONNECTION
        TRANSLATES, an explicit key-presence signal, never a content comparison. A
        value on a localized slug or an excluded field does not count (#647). A missing
        locale is always (re-)written."""
        snap = client.entry_snapshot(record_id)
        if snap is ABSENT:
            return False
        exact = client.resolve_locale(target_locale)
        loc_fields = snap["fields"]
        return any(
            exact in (loc_fields.get(f) or {})
            for f in client.translated_field_ids(snap["ct"], loc_fields)
        )

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        # `draft` is accepted for the framework signature but ignored: v1 never
        # publishes (a CMA update lands on the entry/draft and publishing is the
        # operator's explicit action).
        if not fields:
            # Every translatable field held back (NEEDS_REVIEW). Write NOTHING and
            # report held-back so the flow records NO suppression hash and a later
            # healthy run re-attempts (taxonomy F).
            return WriteResult(False, False, message=_HELD_BACK)
        # Serialize the read-merge-write span for this (connection, entry) so two
        # in-process writers of DIFFERENT locales of the SAME entry don't lost-update
        # each other (design T1 record lock; same single-process caveat as the shared
        # strategies). Cross-process / external concurrency is caught by the version
        # conflict retry below.
        lock = _record_write_lock_for(getattr(client, "connection_id", None), record_id)
        with lock:
            exact = client.resolve_locale(target_locale)
            last_msg = ""
            total_wait = 0.0
            for _attempt in range(_MAX_VERSION_RETRIES):
                snap = client.entry_snapshot(record_id)
                if snap is ABSENT:
                    return WriteResult(False, False, message=f"entry {record_id} not found")
                cur = snap["fields"]
                version = snap["version"]
                ct = snap["ct"]
                localizable = client.localizable_field_ids(ct)
                # #647: "existed" looks only at the fields this connection translates
                # (plus the fields being written now), never a localized slug or an
                # excluded field that happens to carry a target value.
                scope = (client.translated_field_ids(ct, cur) | set(fields)) & localizable
                existed = any(exact in (cur.get(f) or {}) for f in scope)
                new_fields = copy.deepcopy(cur)
                applied = 0
                dropped_nonloc: list[str] = []
                held_invalid: dict = {}
                for fid, val in fields.items():
                    # Defence-in-depth (M3): selection already restricts `fields` to
                    # localizable translatable fields; NEVER write a non-localizable
                    # field under a target locale (Contentful 422s it, and it would
                    # corrupt the default-locale-only value).
                    if fid not in localizable:
                        dropped_nonloc.append(fid)
                        continue
                    # Pre-write validation (H2): a value that breaks the field's CMA
                    # validations (Symbol 256, size/regex/enum) would 422 the WHOLE
                    # entry PUT and discard the good translations. Hold ONLY the
                    # offending field back (the delta overlay already writes per field).
                    reason = client.validate_value(ct, fid, val)
                    if reason:
                        held_invalid[fid] = reason
                        continue
                    slot = dict(new_fields.get(fid) or {})
                    slot[exact] = val
                    new_fields[fid] = slot
                    applied += 1
                # NOTE (review round 3): we deliberately do NOT pre-check the ECHOED
                # pre-existing values against validations. MT was already spent at flow
                # stage 8 (before write_target), so a pre-check saves zero spend; and it
                # can only DIFFER from the runtime path when the live CMA would have
                # ACCEPTED the echoed value — i.e. it could only manufacture a false
                # permanent block. A genuine 422 (the CMA naming any field, touched or
                # not) is caught below on the actual response and classified as a block
                # then — evidence-based, strictly dominant.
                if applied == 0:
                    if held_invalid and not dropped_nonloc:
                        # F1a: DETERMINISTIC all-validation hold-back. NOT a permanent hard
                        # failure (that would leave the connection red + re-spend MT every
                        # scheduled run). Record a durable validation block (no re-spend);
                        # a source edit or retry_failed re-attempts it.
                        client._record_blocked(record_id, target_locale, held_invalid)
                        return WriteResult(
                            False,
                            not existed,
                            message=(
                                _VBLOCK_PREFIX + " every translatable field's value fails a CMA "
                                "validation: "
                                + "; ".join(f"{k} ({v})" for k, v in sorted(held_invalid.items()))
                            ),
                        )
                    # F1b: a non-localizable config bug (nothing writable, no validation
                    # reason) -> a LOUD hard failure so the operator fixes the mapping.
                    why = []
                    if held_invalid:
                        why.append(
                            "validation: "
                            + "; ".join(f"{k} ({v})" for k, v in sorted(held_invalid.items()))
                        )
                    if dropped_nonloc:
                        why.append("non-localizable field(s): " + ", ".join(sorted(dropped_nonloc)))
                    return WriteResult(
                        False,
                        not existed,
                        message="no writable fields - " + " | ".join(why or ["empty"]),
                    )
                if held_invalid and not existed:
                    # #640: never CREATE a locale with a field missing (a per-field CMA
                    # validation hold, Symbol 256 / size.max): the missing field would stay
                    # missing. The hold is deterministic for the same source, so record it as
                    # a durable validation block (no MT re-spend; a source edit or
                    # retry_failed re-attempts it) and write NOTHING.
                    client._record_blocked(record_id, target_locale, held_invalid)
                    return WriteResult(
                        False,
                        True,
                        message=(
                            _VBLOCK_PREFIX + " new locale not created, field(s) fail a CMA "
                            "validation: "
                            + "; ".join(f"{k} ({v})" for k, v in sorted(held_invalid.items()))
                        ),
                    )
                if new_fields == cur:
                    # Genuine no-op (digest §7.3 JToken.DeepEquals): the applied values
                    # were ALREADY present -> success without a PUT. Reached only when
                    # applied > 0 (the applied==0 guard above already returned), so this
                    # can no longer be a false success on an all-filtered payload (M3).
                    if held_invalid:
                        client._record_incomplete(record_id, target_locale, held_invalid)
                        client._record_vheld(record_id, target_locale, held_invalid)
                    return WriteResult(True, not existed, target_id=record_id, message="unchanged")
                out = client.write_entry(record_id, new_fields, version, snap.get("metadata"))
                if out.ok:
                    if held_invalid:
                        # Partial write: the valid fields landed; record the held-back
                        # invalid fields as an operator-visible incomplete marker (H2/M1).
                        client._record_incomplete(record_id, target_locale, held_invalid)
                        client._record_vheld(record_id, target_locale, held_invalid)
                    return WriteResult(
                        True,
                        not existed,
                        target_id=record_id,
                        message="created" if not existed else "updated",
                    )
                if out.conflict:
                    last_msg = out.message
                    # Bounded backoff honouring X-Contentful-RateLimit-Reset when present,
                    # capped per-wait at base.MAX_SLEEP and per-PAIR at the cumulative
                    # ceiling (review F6/finding 7), so a hostile reset stream cannot stall
                    # this pair beyond _MAX_TOTAL_CONFLICT_WAIT.
                    if (
                        _attempt < _MAX_VERSION_RETRIES - 1
                        and total_wait < _MAX_TOTAL_CONFLICT_WAIT
                    ):
                        wait = (
                            out.retry_after
                            if out.retry_after is not None
                            else min(base.BACKOFF_BASE * (2**_attempt), base.MAX_SLEEP)
                        )
                        wait = min(wait, _MAX_TOTAL_CONFLICT_WAIT - total_wait)
                        total_wait += wait
                        base._sleep(wait)
                    continue  # refetch fresh version + re-apply the delta
                if out.status == 422:
                    # F2 runtime fallback: a terminal 422 is a DETERMINISTIC validation
                    # error (the live CMA may name an untouched field our validator did not
                    # model). Re-sending won't help -> classify as a validation block
                    # (no re-spend), not a generic retryable failure.
                    client._record_blocked(record_id, target_locale, {"_cma": out.message})
                    return WriteResult(
                        False,
                        not existed,
                        message=_VBLOCK_PREFIX + " CMA rejected the write: " + out.message,
                    )
                return WriteResult(False, not existed, message=out.message)
            return WriteResult(
                False,
                False,
                message=f"version conflict after {_MAX_VERSION_RETRIES} "
                f"attempts (concurrent edit); {last_msg}",
            )


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (no watermark): the content-hash gate is the change detector.
# ===========================================================================
def _ctf_preflight(ctx):
    enabled = set(ctx.connector.enabled_locales())
    src = ctx.connector._source_locale().lower()
    # M4: a configured source_locale that the space does not have enabled would silently
    # yield "no translatable source text" on every entry and stay CONNECTED forever with
    # no stated cause. Fail LOUD instead (we already have the enabled set in hand).
    if src not in enabled:
        raise ConnectorError(
            f"Source locale {src!r} is not enabled on this Contentful space/environment "
            f"(enabled: {', '.join(sorted(enabled)) or 'none'}). Set source_locale to an "
            f"enabled locale, or enable it in Contentful."
        )
    return enabled, {"source": src}


def _ctf_partition(locales, enabled, extra):
    """usable = enabled targets minus the source locale; rejected = not enabled. The
    source locale is DROPPED (not rejected) — surfaced honestly in the message."""
    src = (extra or {}).get("source", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != src]  # noqa: E741
    return rejected, usable


def _ctf_prepare(ctx, art):
    title_field, title_text, body_units, chash, body_names = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    aid = art["id"]
    # Remember this entry's source hash so build_result can persist a validation block
    # keyed on it (review F1a/F2) — the durable "don't re-spend MT" marker.
    ctx.connector._chash_by_aid[aid] = chash
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={
            "article_id": aid,
            "title": title_text or None,
            "content_type": art.get("_ct"),
        },
        doc_title=f"{(title_text or art.get('_ct') or 'entry')[:50]} ({aid})",
        extra={
            "title_field": title_field,
            "body_names": body_names,
            "connector": ctx.connector,
            "rich": dict(ctx.connector._rich_by_aid.get(aid) or {}),
        },
    )


_RICH_GUARD_REASON = (
    "Rich Text placeholders did not round-trip through translation; the stored value was kept"
)


def _rich_key(fid: str, idx: int) -> str:
    """Flow p_index of one Rich Text unit: the field id plus the unit number. Contentful
    field ids are [A-Za-z0-9_], so '#' cannot collide with a plain field's key."""
    return f"{fid}#{idx}"


def _rich_rebuild(prep, mapping) -> tuple:
    """({fid: rebuilt document}, {fid: reason}) for the Rich Text fields of one entry.
    A field is all-or-nothing (#648): any unit missing a usable translation (NEEDS_REVIEW)
    or whose placeholders do not round-trip leaves the whole field out, so a partial or
    mangled tree is never written. Units are never source-seeded."""
    docs: dict = {}
    held: dict = {}
    for fid, tree in (prep.extra.get("rich") or {}).items():
        units = rich_text.extract(tree)
        trans = {u.p_index: mapping.get(_rich_key(fid, u.p_index)) for u in units}
        if any(v is None for v in trans.values()):
            held[fid] = ("awaiting review (NEEDS_REVIEW)", False)
            continue
        doc, _bad = rich_text.rebuild(tree, trans)
        if doc is None:
            held[fid] = (_RICH_GUARD_REASON, True)
            continue
        docs[fid] = doc
    return docs, held


def _ctf_write_incomplete(connector, aid, loc) -> bool:
    """Flow hook (#665): the write landed but a field was held back (a CMA validation or a
    review hold), so the pair must not record a content hash."""
    return (str(aid), str(loc)) in (getattr(connector, "_incomplete", {}) or {})


def _ctf_extra_held(prep, loc, mapping):
    """Flow hook: Rich Text fields whose placeholders were lost (guard failures). Review
    holds are already visible to the engine through the unit statuses."""
    _docs, held = _rich_rebuild(prep, mapping)
    names = [f"{fid} (placeholders lost)" for fid, (_r, guard) in sorted(held.items()) if guard]
    if names:
        log.warning(
            "contentful: Rich Text field(s) %s on %s/%s failed the placeholder guard; "
            "the stored value is kept and the pair is retried next run",
            ", ".join(sorted(f for f, (_r, g) in held.items() if g)),
            prep.aid,
            loc,
        )
    return names


def _ctf_parked(connector, aid, loc, chash):
    """Flow hook (#665): a pair parked by a deterministic CMA validation hold for exactly
    this source hash is skipped without MT. It is NOT done (no content hash is recorded)."""
    return connector._is_parked(aid, loc, chash)


def _ctf_is_suppressed(connector, aid, loc):
    # Suppress a pair when its target locale already exists (normal case) OR when a prior
    # run recorded it as a DETERMINISTIC validation block (review F1a/F2) — the latter is
    # consulted only after the flow confirmed the source hash is unchanged, so a source
    # edit or retry_failed still re-attempts. Suppressing here skips the pair BEFORE the
    # materialize/pretranslate stage, so a blocked pair never re-spends MT.
    if connector.strategy.exists_target(connector, aid, loc):
        return True
    return connector._is_validation_blocked(aid, loc)


def _ctf_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one locale: {fieldId: translatedValue} for the fields
    actually translated this run. T1 (like Intercom): a HELD-BACK (NEEDS_REVIEW) field
    is OMITTED entirely — never source-seeded — so the read-merge-write keeps the
    existing target value (create simply leaves that field with no target key). The
    strategy writes NOTHING when this dict is empty (all fields held back)."""
    title_field = prep.extra["title_field"]
    out: dict = {}
    if title_field is not None and seg0_target is not None:
        out[title_field] = seg0_target
    for name in prep.extra["body_names"]:
        if name in mapping:
            out[name] = mapping[name]
    rich_docs, rich_held = _rich_rebuild(prep, mapping)
    out.update(rich_docs)
    # M1 visibility: record fields that WERE translatable candidates but got held back
    # (NEEDS_REVIEW) this run — but ONLY on a PARTIAL write (out non-empty). An all-held
    # write returns {} and stays the taxonomy-F held_back path (no hash, re-attempted),
    # so it must NOT be double-counted as incomplete. build_result confirms these against
    # the run's failures (a pair whose write then failed is a failure, not incomplete).
    if out:
        candidates = (
            ([title_field] if title_field else [])
            + list(prep.extra["body_names"])
            + list(prep.extra.get("rich") or {})
        )
        held = {
            f: (rich_held[f][0] if f in rich_held else "awaiting review (NEEDS_REVIEW)")
            for f in candidates
            if f not in out
        }
        if held:
            prep.extra["connector"]._record_incomplete(prep.aid, loc, held)
    return out


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _is_vblock(f):
    return str((f or {}).get("error", "")).startswith(_VBLOCK_PREFIX)


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _confirmed_incomplete(ctx):
    """The (entry, locale) pairs that were PARTIALLY written this run — some fields
    landed, others were held back (NEEDS_REVIEW or a CMA-validation failure) — CONFIRMED
    against the run's real failures (a pair whose write failed is a failure, not an
    incomplete). Operator-visible; the flow still recorded the content hash for these
    (suppression stands, no MT re-spend loop), and retry_failed can re-attempt them."""
    failed = {(str(f.get("article_id")), str(f.get("locale"))) for f in ctx.failures}
    out = []
    for (aid, loc), held in (getattr(ctx.connector, "_incomplete", {}) or {}).items():
        if (aid, loc) in failed:
            continue
        out.append(
            {
                "article_id": aid,
                "locale": loc,
                "held_fields": sorted(held.keys()),
                "reasons": dict(held),
            }
        )
    return out
