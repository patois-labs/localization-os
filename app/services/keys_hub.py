"""keys: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import (
    DocStatus,
    Document,
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    Project,
    Segment,
    StringKey,
    TransMode,
)
from app.services import settings
from app.services.keys_common import (
    _audit,
    _changed,
    _key_view,
    _utcnow,
    current_actor,
    import_memo_of,
    is_key_project,
)

KEY_DOC_FMT = "keys"


KEY_DOC_PREFIX = "keys::"


DEFAULT_NAMESPACE_LABEL = "__default__"


KEY_TRASH_FILENAME = KEY_DOC_PREFIX + "deleted" + "::" + "__trash__"


NESTED_SEP = "::"


CLDR_CATEGORIES = ("zero", "one", "two", "few", "many", "other")


SOURCE_CHANGED_FLAG_TYPE = "source_changed"


ENABLED_SETTING = "key_projects_enabled"


AUTOUNVERIFY_SETTING = "key_autounverify"


RETENTION_SETTING = "key_changelog_retention_days"


_PROJECT_FLOOR_PREFIX = "key_changelog_floor:"


DEFAULT_DELTA_LIMIT = 500


MAX_DELTA_LIMIT = 1000


MAX_DELTA_ENTRIES = 5000


ORIGIN_HUMAN = "human"


ORIGIN_IMPORT = "key-import"


ORIGIN_API = "key-api"


ORIGINS = (ORIGIN_HUMAN, ORIGIN_IMPORT, ORIGIN_API)


# --- constants ---------------------------------------------------------------
#: `Document.fmt` for an internal key document. Deliberately NOT a registered
#: filter format (`app/filters/__init__.py` has no "keys" module), so no upload
#: path anywhere can produce a document with this fmt -- which is what makes
#: `is_internal_key_document` a sound one-line predicate.
#: `Document.filename` prefix; the label standing in for the default (empty) namespace.
#: The internal document a SOFT-DELETED key's value rows are parked in (Wave B).
#:
#: `delete_key` keeps the rows on disk so `restore_key` is lossless -- but a run
#: targets a DOCUMENT, and `orchestrator._load_segments` selects by
#: (document_id, target_locale) with no notion of keys, so leaving a tombstoned key's
#: rows in its namespace document meant a `keys import --cleanup` followed by a
#: translate run spent provider budget translating strings the customer had just
#: deleted (spec section 5, the Wave B launcher obligation). Moving them out is the
#: only exclusion that is STRUCTURAL rather than per-caller: it holds for the
#: orchestrator, the CAT editor, `document_has_no_content` and any future
#: document-scoped reader, without one line of orchestrator.py changing.
#:
#: The name is UNREACHABLE from `document_filename()`: it contains `::` after the
#: prefix, and `validate_namespace` refuses a namespace containing `::`, so no
#: namespace -- however adversarial -- can ever collide with it. That is why this is a
#: filename constant and not another reserved namespace word.
#: The nested-JSON flatten separator (Lokalise's convention -- research 01 section 3.2).
#: Lives here because it is part of key IDENTITY, not just of the file parser.
#: The six CLDR plural categories, in CLDR order. The order is load-bearing: it is
#: also the order gettext's `msgstr[N]` indices follow for every locale we probe.
#: qa_flags entry type stamped on a translation whose SOURCE changed under it.
#: Settings keys (all registered in settings.DEFAULTS -- never phantom reads).
#: Per-project overrides / bookkeeping are namespaced under the same names with a
#: ":<project_id>" suffix. They are WRITTEN by this module and read ONLY through the
#: two accessors below, so they are not phantom config either.
#: Delta read caps (spec section 5).
#: modified_after emits one entry per value row, so a page's entry count is
#: keys x locales x plural-forms -- unbounded by the KEY limit alone. This ceiling
#: (gate M2) bounds the total entries in one response; the page truncates at a KEY
#: boundary and keyset pagination resumes cleanly on the next poll.
#: `set_value(origin=...)` -- provenance, and the switch that decides whether a write
#: is HUMAN CONTENT (assigned directly, like the CAT editor's save) or FOREIGN TEXT
#: that must pass the refusal choke point on the way in. Only ORIGIN_HUMAN skips it.
#: Every origin `set_value` accepts. The parameter is keyword-REQUIRED and validated
#: against this set (review A7): the choke-point allowlist entry for this module is
#: whole-file, so "forgot to say where this text came from" must be a TypeError at the
#: call site rather than a silent slide into the ungated human posture.
# --- errors ------------------------------------------------------------------
class KeyServiceError(ValueError):
    """Base for every refusal in this module. Surfaces render these as clean
    400 / EXIT_USAGE messages, never a 500."""


def _coerce_char_limit(raw):
    """Validate an INCOMING char_limit at ingestion, or raise KeyServiceError.

    None / "" -> None (unset / cleared). Otherwise it must be a whole number > 0 and is
    capped at `context_pkg.CHAR_LIMIT_MAX` — the SAME rule the doc-side context slot has
    always enforced (context_pkg.resolve). This is the WRITE-side twin of
    `quality.coerce_char_limit` (which treats a bad value as "unset" so the sole write
    seam can never crash on a legacy row): here we REFUSE it, because before this the
    latent-crash surface was wide open — `create_key` never validated char_limit at all, and
    `update_key` was `int()`-only so 0 and negatives passed straight to the column. A 0 or
    a -5 or a non-integer stored today is exactly what a naive `len(text) > limit` at the
    write seam would have raised a TypeError on; this stops new ones landing while the read
    predicate absorbs the ones already there."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        # `bool` is an `int` subclass, so `True` would sail through `int()` as 1 and store a
        # 1-character limit on a key an /api upsert set `"char_limit": true` on (reviewer
        # L1). It is never a real limit; refuse it at ingestion.
        raise KeyServiceError("char_limit must be a whole number.")
    try:
        n = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise KeyServiceError("char_limit must be a whole number.") from None
    if n <= 0:
        raise KeyServiceError("char_limit must be a positive whole number.")
    from app.services.context_pkg import CHAR_LIMIT_MAX

    return min(n, CHAR_LIMIT_MAX)


# #574: ONE wording for "this name cannot be used", so a caller below manager cannot tell a
# hidden key's name from a taken one (existence oracle). Per-row import/upsert refusal text.
KEY_NAME_UNAVAILABLE = "key name not available"


class DuplicateKeyError(KeyServiceError):
    """The identity tuple (project, branch, namespace, name, context) is taken by a
    LIVE key. Raised instead of silently merging -- Lokalise's silent
    `distinguish_by_file` merge is the documented trap we refuse loudly instead."""

    def __init__(self, namespace: str, name: str, context: str = ""):
        self.namespace, self.name, self.context = namespace, name, context
        where = f"namespace {namespace!r}" if namespace else "the default namespace"
        ctx = f" (context {context!r})" if context else ""
        super().__init__(f"the key name {name!r} is not available in {where}{ctx}.")


class NoTargetLocalesError(KeyServiceError):
    """A base value was written to a project with no active target locales. A key's
    base value is stored as the `source_text` of its per-target-locale value rows
    (spec 2.5), so with no locales there is literally nowhere to put it. Refused
    loudly instead of accepting a write that would be silently dropped."""

    def __init__(self, project_id):
        self.project_id = project_id
        super().__init__(
            "this project has no target locales yet, so a key value has nowhere to "
            "be stored. Add at least one target locale first."
        )


class PluralDemotionRefused(KeyServiceError):
    """Demoting a plural key to a single form would destroy translated content in the
    non-`other` forms. Honest refusal naming what would be lost; `force=True` accepts
    the loss deliberately."""

    def __init__(self, forms):
        self.forms = sorted(forms)
        super().__init__(
            "this key still has translated plural forms ({}); demoting it would "
            "discard them. Re-run with force to accept that.".format(", ".join(self.forms))
        )


class KeyValueAwaitingReview(KeyServiceError):
    """#617 item 4 (gate): the value's locale is held by a run parked on a human_review step
    (segment_review.refuse_if_awaiting_review). The /api routes answer 409 `awaiting_review`
    (they read `status`/`code`); the grid and the CLI render the message like any refusal."""

    status = 409
    code = "awaiting_review"

    def __init__(self, message: str, run_id: int):
        self.run_id = run_id
        super().__init__(message)


class UnknownPluralCategory(KeyServiceError):
    """A plural category that the target locale's CLDR rule set does not use."""

    def __init__(self, category, locale, allowed):
        self.category, self.locale, self.allowed = category, locale, list(allowed)
        super().__init__(
            "{!r} is not a plural form of {} (this locale uses: {}).".format(
                category, locale, ", ".join(allowed)
            )
        )


class NotAKeyProjectError(KeyServiceError):
    """A key operation was aimed at a document-driven project. Project type is
    IMMUTABLE (spec 2.1) so this is always a caller bug, never a state to fix."""

    def __init__(self, project_id):
        self.project_id = project_id
        super().__init__(f"project {project_id} is not a key-based project.")


class BadTimestampError(KeyServiceError):
    """An unparseable `modified_after` value (AP-1). ISO-8601 only."""

    def __init__(self, raw):
        self.raw = raw
        super().__init__(
            f"could not parse modified_after {raw!r} (use ISO-8601, e.g. "
            "2026-01-01 or 2026-01-01T00:00:00Z)."
        )


class UnknownLocaleError(KeyServiceError):
    """A `modified_after` request named a `locale` that is not one of the project's
    active target locales (AP-1 / gate H2). Refuse loudly: a bogus locale otherwise
    returns an empty feed with an ADVANCING cursor -- silent, permanent, and
    indistinguishable from 'nothing changed', the one answer this feed must never give
    wrongly."""

    def __init__(self, locale, project_id):
        self.locale = locale
        self.project_id = project_id
        super().__init__(
            f"locale {locale!r} is not an active target locale of project {project_id}."
        )


# --- switches ----------------------------------------------------------------
def enabled() -> bool:
    """The module kill switch (`key_projects_enabled`, default on). An unreadable
    Setting table (bare import-time contexts) reads as the DEFAULT, matching
    image_localization.enabled()."""
    try:
        return str(settings.get(ENABLED_SETTING, "1")) == "1"
    except Exception:
        return True


def autounverify_enabled(project) -> bool:
    """The maintainer's locked call #3: a base-value change flags every other locale's
    translation for review. Install-wide default from `key_autounverify`, with a
    per-project override under `key_autounverify:<id>` (the project settings panel
    writes it via `set_autounverify`)."""
    pid = getattr(project, "id", project)
    try:
        if pid is not None:
            # A BLANK override means "inherit" (Setting rows are never deleted, so
            # clearing the per-project toggle writes "" rather than removing the row).
            override = settings.get(f"{AUTOUNVERIFY_SETTING}:{pid}", None)
            if override is not None and str(override).strip() != "":
                return str(override) == "1"
        return str(settings.get(AUTOUNVERIFY_SETTING, "1")) == "1"
    except Exception:
        return True


def set_autounverify(project, on: bool | None, *, actor: str = "", ip: str = "") -> None:
    """Set (on/off) or CLEAR (None -> inherit the install default) the per-project
    auto-unverify override.

    AUDITED (review B9): this is the switch that decides whether a source edit can
    silently ship a stale translation, so "who turned it off, and when" is exactly the
    question an incident asks."""
    pid = getattr(project, "id", project)
    settings.set_many(
        {f"{AUTOUNVERIFY_SETTING}:{pid}": ("" if on is None else ("1" if on else "0"))}
    )
    _audit(
        "keys.autounverify",
        pid,
        f"project {pid}",
        actor=actor,
        ip=ip or "",
        detail="auto-unverify -> %s" % ("inherit" if on is None else ("on" if on else "off")),
    )


def retention_days() -> int:
    """Days of KeyChangeLog history kept. 0 disables pruning entirely."""
    try:
        return max(0, int(str(settings.get(RETENTION_SETTING, "90")).strip() or "0"))
    except (TypeError, ValueError):
        return 90


def is_internal_key_document(doc) -> bool:
    """THE predicate every document surface calls to hide a key project's internal
    documents (spec 2.5). ONE definition, deliberately one line: `fmt` is a
    system-owned value here -- "keys" is not a registered filter format, so no upload,
    connector or API path can mint a document with it -- which means this cannot be
    fooled into revealing an internal document or into hiding a real one."""
    return getattr(doc, "fmt", None) == KEY_DOC_FMT


def is_trash_document(doc) -> bool:
    """THE predicate a RUN LAUNCHER calls: is this document the place soft-deleted
    keys' value rows are parked (`KEY_TRASH_FILENAME`)?

    `translatable_documents` already excludes it from every list a launcher builds,
    but a list is only an exclusion for callers who build their own scope from it.
    `POST /documents/{id}/run` did not -- it took a bare `session.get(Document)` -- so
    a run could be pointed straight at the trash document and spend provider budget
    translating strings the customer had just deleted, emitting ZERO delta entries
    (a tombstoned key is silent by design, review A9). `orchestrator.start_run` now
    asks THIS, which is what turns "deleted keys are out of run scope" from a claim
    about the launchers we have into a property of the seam every launcher goes
    through (Wave B fix round, A-F1).

    Two conditions, not one: `fmt` alone would refuse the NAMESPACE documents a key
    project's own Translate control legitimately runs over."""
    return is_internal_key_document(doc) and getattr(doc, "filename", None) == KEY_TRASH_FILENAME


_plural_cache: dict[str, tuple] = {}


_gettext_cache: dict[tuple, tuple] = {}


# --- CLDR plurals (Babel; never hand-rolled) ---------------------------------
#: (locale, expr, num) -> derived index order. The derivation walks a 203-value probe
#: series against two rule evaluators, and every import/export of a plural key asks
#: for it, so it is memoized on the exact inputs that determine the answer.
def _babel_locale(locale: str):
    """Babel Locale for a BCP-47 code ("pt-BR", "es_ES", "en"), or None."""
    from babel import Locale, UnknownLocaleError

    code = (locale or "").strip()
    if not code:
        return None
    for candidate in (code.replace("-", "_"), code.split("-")[0].split("_")[0]):
        try:
            return Locale.parse(candidate)
        except (ValueError, TypeError, UnknownLocaleError):
            continue
    return None


def plural_categories_for(locale: str) -> list[str]:
    """The CLDR plural categories `locale` uses, in CLDR order, ALWAYS including
    "other" (the required fallback). en -> [one, other]; ru -> [one, few, many,
    other]; ar -> all six; ja -> [other]. Derived from Babel's CLDR rule set, never
    from a hand-written table (research 01 section 4.3)."""
    key = (locale or "").strip().lower()
    hit = _plural_cache.get(key)
    if hit is not None:
        return list(hit)
    loc = _babel_locale(locale)
    tags = set(getattr(loc, "plural_form", None).tags) if loc is not None else set()
    tags.add("other")
    cats = tuple(c for c in CLDR_CATEGORIES if c in tags)
    _plural_cache[key] = cats
    return list(cats)


# --- qa_flags helpers (shaped like every other flag: {type,severity,message}) --
def source_changed_flag() -> dict:
    return {
        "type": SOURCE_CHANGED_FLAG_TYPE,
        "severity": "major",
        "message": "The base value changed after this translation was written -- "
        "re-check it against the new source.",
        "reason": "base value changed",
    }


def merge_source_changed_flag(existing: list | None) -> list:
    """qa_flags with exactly ONE source_changed entry, every other flag preserved. A
    fresh list each call so SQLAlchemy sees the JSON column as changed (the
    merge_dnt_flags / merge_glossary_flags convention)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == SOURCE_CHANGED_FLAG_TYPE)
    ]
    out.append(source_changed_flag())
    return out


def clear_source_changed_flag(existing: list | None) -> list:
    """qa_flags with the source_changed entry removed -- a human/import write to the
    VALUE is the confirmation event, exactly as the CAT editor clears the QE and
    tag_integrity badges on save."""
    return [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == SOURCE_CHANGED_FLAG_TYPE)
    ]


# NOTE: no module-level `_install_actor_inheritance()` call (re-review R4).
# `actor_scope.__init__` arms it, and that ALWAYS runs before any launcher's
# `Thread.start` can matter -- there is no actor to inherit until somebody has entered
# a scope. Arming it at import time instead meant any import of this module (retention
# used to do one per document delete) monkeypatched `threading.Thread.start`
# process-wide on installs that have never seen a key project.


# --- internal documents ------------------------------------------------------
def validate_namespace(namespace: str) -> str:
    """The stripped namespace, or a refusal (re-review R3).

    A namespace is part of key IDENTITY and is also SERIALIZED into a name
    (`namespace::name`) by every whole-project export, so two spellings cannot be
    allowed to exist:

      * `::` inside a namespace breaks the qualifier round trip SILENTLY -- namespace
        `a::b` + name `x` exports as `a::b::x`, and `split_namespace` reads that back
        as namespace `a`, name `b::x`, quietly forking one key into two;
      * `__default__` is the LABEL the empty namespace is spelled with, so a real
        namespace by that name is indistinguishable from the default one -- and if both
        exist, the export has two different keys with the same qualified name and
        refuses outright, which is a whole-project export that cannot be run at all.
      * a `$`-leading namespace risks the same silent collision with `$meta`, the JSON
        export marker key (`key_files.JSON_META_KEY`, stamped as the FIRST member of a
        qualified export -- see key_files.py:432-434): a namespace of `$meta` (or any
        other `$`-leading spelling) could displace or be displaced by that marker with
        no error at all.

    All three are rejected at every seam that can set a namespace (`create_key`,
    `move_namespace`, `key_files.namespace_for_filename`), so none of these states is
    reachable rather than merely undocumented."""
    ns = (namespace or "").strip()
    if NESTED_SEP in ns:
        raise KeyServiceError(
            f"a namespace cannot contain {NESTED_SEP!r} ({ns!r}): it is the separator a qualified "
            "export uses between the namespace and the key name, so the namespace "
            "would not survive a round trip."
        )
    if ns == DEFAULT_NAMESPACE_LABEL:
        raise KeyServiceError(
            f"{DEFAULT_NAMESPACE_LABEL!r} is reserved: it is how the DEFAULT (empty) namespace is spelled in an "  # noqa: E501
            "exported name. Use the empty namespace, or pick another name."
        )
    if ns.startswith("$"):
        raise KeyServiceError(
            f"a namespace cannot start with '$' ({ns!r}): '$meta' is the export marker key "
            "a qualified export stamps, and a '$'-leading namespace could silently "
            "displace it. Pick another name."
        )
    return ns


def document_filename(namespace: str) -> str:
    return KEY_DOC_PREFIX + (namespace or DEFAULT_NAMESPACE_LABEL)


def internal_document(
    session: Session, project, namespace: str = "", create: bool = True
) -> Document | None:
    """The internal Document that holds `namespace`'s value rows, created lazily on
    first use (spec 2.5). One per (project, namespace), `fmt="keys"`, SEGMENTED, so
    every existing per-document/per-locale mechanism -- the orchestrator's segment
    load, the CAT editor, run history -- addresses key content unchanged.

    The skeleton is a MARKER, not a file structure: there is no original file to
    rebuild, and the only reader of `skeleton["units"]` is the FULL_TEXT branch these
    documents never take."""
    fname = document_filename(namespace)
    memo = import_memo_of(session)  # #585: one lookup per namespace per bulk import
    if memo is not None and (project.id, fname) in memo:
        return memo[(project.id, fname)]
    doc = session.exec(
        select(Document).where(
            Document.project_id == project.id,
            Document.fmt == KEY_DOC_FMT,
            Document.filename == fname,
        )
    ).first()
    if doc is not None and memo is not None:
        memo[(project.id, fname)] = doc
    if doc is not None or not create:
        return doc
    doc = Document(
        project_id=project.id,
        filename=fname,
        fmt=KEY_DOC_FMT,
        mode=TransMode.SEGMENTED,
        status=DocStatus.NEW,
        skeleton={
            "units": [],
            "mode": TransMode.SEGMENTED,
            "seg_locale": getattr(project, "source_locale", "") or "",
            "key_namespace": namespace or "",
            "note": "internal key-project document; not a parsed file",
        },
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def trash_document(session: Session, project, create: bool = True) -> Document | None:
    """The internal document a soft-deleted key's value rows are parked in.

    Same shape as `internal_document` (fmt="keys", SEGMENTED, `is_internal_key_
    document` true, reaped by the same cascades) but addressed by a filename no
    namespace can produce -- see `KEY_TRASH_FILENAME`. One per project, created lazily
    on the first delete, so a project that never deletes a key never grows one."""
    doc = session.exec(
        select(Document).where(
            Document.project_id == project.id,
            Document.fmt == KEY_DOC_FMT,
            Document.filename == KEY_TRASH_FILENAME,
        )
    ).first()
    if doc is not None or not create:
        return doc
    doc = Document(
        project_id=project.id,
        filename=KEY_TRASH_FILENAME,
        fmt=KEY_DOC_FMT,
        mode=TransMode.SEGMENTED,
        status=DocStatus.NEW,
        skeleton={
            "units": [],
            "mode": TransMode.SEGMENTED,
            "seg_locale": getattr(project, "source_locale", "") or "",
            "key_namespace": "",
            "note": "internal key-project document holding SOFT-DELETED keys' "
            "value rows; never translated, never exported",
        },
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def key_documents(session: Session, project_id: int) -> list[Document]:
    """EVERY internal key document of a project, trash included. The reaping /
    cascade paths want this one; a translate launcher wants the next one."""
    return list(
        session.exec(
            select(Document)
            .where(Document.project_id == project_id, Document.fmt == KEY_DOC_FMT)
            .order_by(Document.id)
        ).all()
    )


def translatable_documents(session: Session, project) -> list[Document]:
    """The internal documents a RUN may target: every namespace document, and NEVER
    the trash document (spec section 5's Wave B obligation, "run scope must exclude a
    deleted key's segments"). Deleted keys' rows live only in the trash document, so
    this list IS the exclusion -- there is no per-segment filter for a launcher to
    forget, and the guarantee holds for any future launcher too."""
    return [
        d
        for d in key_documents(session, getattr(project, "id", project))
        if d.filename != KEY_TRASH_FILENAME
    ]


class TranslationEngineError(KeyServiceError):
    """A key-run launch refused because a translate step would resolve to the OFFLINE
    mock -- a real provider with no key that would silently echo instead of translate
    (the mock-409 law). Distinct type so a surface can answer it 409 while other
    KeyServiceError refusals stay 400."""


class KeyBudgetError(KeyServiceError):
    """CV-2 (M1): a key-project fan-out refused because the project is at its monthly
    USD cap. Raised BEFORE the launch loop (not per-run) so a fan-out over many
    docs/locales can never half-launch some runs and then 500 on start_run's raw
    ProjectBudgetExhausted mid-loop. A distinct KeyServiceError subtype so the API
    route answers it 429 (unified with the other budget seams) while the CLI catches
    it as a KeyServiceError and exits cleanly."""


def _project(session: Session, project, *, org_id=None, customer_id=None) -> Project:
    """Resolve + TYPE-CHECK a key project, with the isolation guards content.py uses.

    `org_id` / `customer_id` mirror `content.py`'s convention exactly: None means "the
    caller has already established scope" (a CLI/admin path), and a non-None value that
    does not match the project makes the project resolve as if it did not exist. They
    are here in WAVE A, before a single route exists, for two reasons: the ~9 Wave B
    routes get a seam that already refuses instead of one each of them must remember,
    and `verify_keys_isolation` gets something to mutation-proof. A cross-org project
    is reported as NotAKeyProjectError (the same non-enumerable answer as a missing
    id), never as "exists but forbidden"."""
    proj = project if isinstance(project, Project) else session.get(Project, project)
    if proj is None:
        raise NotAKeyProjectError(project)
    if org_id is not None and getattr(proj, "org_id", None) != org_id:
        raise NotAKeyProjectError(getattr(proj, "id", project))
    if customer_id is not None and getattr(proj, "customer_id", None) != customer_id:
        raise NotAKeyProjectError(getattr(proj, "id", project))
    if not is_key_project(proj):
        raise NotAKeyProjectError(proj.id)
    return proj


def values_for(session: Session, key, locale: str | None = None) -> list[Segment]:
    """Every value row of `key` (optionally one locale), ordered deterministically."""
    stmt = select(Segment).where(Segment.key_id == key.id)
    if locale is not None:
        stmt = stmt.where(Segment.target_locale == locale)
    rows = session.exec(stmt).all()
    return sorted(rows, key=_value_order)


def _value_order(seg):
    return (
        seg.target_locale,
        CLDR_CATEGORIES.index(seg.plural_category)
        if seg.plural_category in CLDR_CATEGORIES
        else -1,
    )


# --- the change feed ---------------------------------------------------------
def _log(
    session: Session,
    key,
    *,
    entity: str,
    op: str,
    target_locale=None,
    plural_category: str = "",
    actor: str | None = None,
) -> KeyChangeLog:
    """Append ONE changelog row. Callers do not commit -- the row rides the caller's
    transaction, so a rolled-back mutation can never leave a phantom change entry."""
    row = KeyChangeLog(
        project_id=key.project_id,
        entity=entity,
        key_id=key.id,
        namespace=key.namespace or "",
        name=key.name or "",
        target_locale=target_locale,
        plural_category=plural_category or "",
        op=op,
        changed_at=_utcnow(),
        actor=(current_actor() if actor is None else (actor or "")),
    )
    session.add(row)
    return row


class OrphanKeyValueError(KeyServiceError):
    """A Segment carries a `key_id` that resolves to no StringKey. Unreachable through
    this module's API (every value row is born beside its key and the FK forbids the
    rest); reachable only by out-of-band surgery. The change feed FAILS CLOSED on it
    rather than skipping the row, because a feed that quietly drops entries is the
    exact failure mode the Delta API exists to beat."""

    def __init__(self, segment_id, key_id):
        self.segment_id, self.key_id = segment_id, key_id
        super().__init__(
            f"segment {segment_id} points at key {key_id}, which does not exist; the key change feed "  # noqa: E501
            "cannot describe this write."
        )


def _append_segment_changes(session, touched, gone, fresh) -> None:
    """The listener's BODY. The cheap guard that decides "no key segments in this
    flush" lives in `app.db._key_change_feed` (so it costs a document install one
    isinstance scan and no import at all); everything here is what only a key project
    ever reaches. tests/verify_keys_inertness.py replaces THIS function with one that
    raises and then runs a whole document-project lifecycle -- the guard stays real,
    and the proof is that its body never ran.

    FAIL-CLOSED on purpose (review A12): a value row whose key is missing raises
    instead of being skipped, so corrupt state is loud.

    A TOMBSTONED key emits no `value` entries (review A9): its rows survive only so
    `restore_key` can bring them back, and "an update to a key I already deleted" is
    a state the consumer contract has no answer for. The key-level `deleted` entry
    already told the consumer everything it needs.

    `before_flush` (not `after_flush`) because new objects may be added freely here
    and are picked up by the very same flush; the StringKey lookups run inside
    `no_autoflush` so a cache miss cannot re-enter the flush it is inside of."""
    actor = current_actor()
    with session.no_autoflush:
        cache: dict[int, StringKey] = {}

        def _key(seg):
            key_id = seg.key_id
            if key_id not in cache:
                cache[key_id] = session.get(StringKey, key_id)
            key = cache[key_id]
            if key is None:
                raise OrphanKeyValueError(getattr(seg, "id", None), key_id)
            return key

        def _entry(key, seg, op):
            session.add(
                KeyChangeLog(
                    project_id=key.project_id,
                    entity=KeyEntity.VALUE,
                    key_id=key.id,
                    namespace=key.namespace or "",
                    name=key.name or "",
                    target_locale=seg.target_locale,
                    plural_category=seg.plural_category or "",
                    op=op,
                    changed_at=_utcnow(),
                    actor=actor,
                )
            )

        for seg in touched:
            if not any(_changed(seg, f) for f in ("target_text", "source_text", "status")):
                continue
            key = _key(seg)
            if key.deleted_at is not None:
                continue
            key.translations_modified_at = _utcnow()
            session.add(key)
            _entry(key, seg, KeyOp.UPDATED)
        for seg in gone:
            key = _key(seg)
            if key.deleted_at is not None:
                continue
            _entry(key, seg, KeyOp.DELETED)
        for seg in fresh:
            key = _key(seg)
            if key.deleted_at is not None:
                continue
            # L1: a NEW value row carrying REAL text is a content change too -- bump the
            # content mtime so the key is visible to modified_after. Without this, a key
            # whose value was only ever CREATED (an import that writes the row directly,
            # never a later edit) kept translations_modified_at NULL and was invisible to
            # the modified_after feed forever. An EMPTY fresh row (a materialized locale
            # placeholder) is NOT a content change and must not bump -- otherwise adding
            # one target locale would mark every key "changed" (and modified_after would
            # re-ship every existing string on the next poll).
            if (seg.target_text or "") != "":
                key.translations_modified_at = _utcnow()
                session.add(key)
            _entry(key, seg, KeyOp.CREATED)


def forget_project_settings(session: Session, project_id: int) -> int:
    """Remove the per-project Setting rows this module parks in the GLOBAL Setting
    table (`key_autounverify:<pid>`, `key_changelog_floor:<pid>`) -- called by
    `content.delete_project` so a deleted project leaves no rows behind for a future
    project to inherit when SQLite reuses its rowid (review A16). Returns the count."""
    from sqlalchemy import delete as sa_delete

    from app.models import Setting

    names = [
        f"{AUTOUNVERIFY_SETTING}:{project_id}",
        _PROJECT_FLOOR_PREFIX + str(project_id),
    ]
    existing = [n for n in names if session.exec(select(Setting).where(Setting.key == n)).first()]
    if existing:
        session.exec(sa_delete(Setting).where(Setting.key.in_(names)))
        session.flush()
        # The cache is dropped while the DELETE is still uncommitted, so a CONCURRENT
        # `settings.get` in that window could re-cache the old value (re-review R11).
        # Left as-is on the single-writer assumption this whole delete path already
        # rests on -- `content.delete_project` hand-orders every child table against
        # the same premise -- and because the alternative (forget AFTER the caller's
        # commit) would need a hook this seam does not have.
        settings.forget(names)
    return len(existing)


def key_views(session: Session, keys, locales=None, *, project_id: int) -> list[dict]:
    """Render project-scoped values and bases with two queries per 500 key ids."""
    keys = list(keys)
    if not keys:
        return []
    ids = [k.id for k in keys]
    grouped = {}
    bases = {}
    wanted = set(locales) if locales is not None else None
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        stmt = (
            select(Segment)
            .join(StringKey, Segment.key_id == StringKey.id)
            .where(StringKey.project_id == project_id, Segment.key_id.in_(chunk))
        )
        if wanted is not None:
            stmt = stmt.where(Segment.target_locale.in_(wanted))
        for seg in sorted(session.exec(stmt).all(), key=_value_order):
            grouped.setdefault(seg.key_id, []).append(seg)
        base_rows = session.exec(
            select(
                Segment.key_id,
                Segment.target_locale,
                Segment.plural_category,
                Segment.source_text,
            )
            .join(StringKey, Segment.key_id == StringKey.id)
            .where(StringKey.project_id == project_id, Segment.key_id.in_(chunk))
        ).all()
        for seg in sorted(base_rows, key=_value_order):
            base = bases.setdefault(seg.key_id, {})
            cat = seg.plural_category or ""
            if cat not in base or not base[cat]:
                base[cat] = seg.source_text or ""
    return [_key_view(k, grouped.get(k.id, []), bases.get(k.id, {})) for k in keys]


# --- the Delta API's read side ----------------------------------------------
def changelog_floor(session: Session, project_id: int) -> int:
    """The highest cursor that RETENTION PRUNING has destroyed for this project. A
    consumer holding a cursor below it has provably missed changes and must
    re-bootstrap. 0 = nothing has ever been pruned.

    Kept as an explicit watermark rather than inferred from `min(id)` of the surviving
    rows, because the ids are GLOBAL: a project whose first change happens to be row
    #500 would otherwise look pruned-to-499 and send every fresh consumer into a
    needless full resync."""
    raw = settings.get(_PROJECT_FLOOR_PREFIX + str(project_id), None)
    try:
        return int(str(raw or "0"))
    except (TypeError, ValueError):
        return 0


def prune_changelog(
    session: Session,
    project_id: int | None = None,
    *,
    days: int | None = None,
    now: datetime | None = None,
) -> int:
    """Delete changelog rows older than the retention window and raise the pruned
    floor. Returns the number of rows removed. `days=0` (or the setting at "0")
    disables pruning entirely -- an install that wants a permanent feed keeps one."""
    window = retention_days() if days is None else int(days)
    if window <= 0:
        return 0
    cutoff = (now or _utcnow()) - timedelta(days=window)
    stmt = select(KeyChangeLog).where(KeyChangeLog.changed_at < cutoff)
    if project_id is not None:
        stmt = stmt.where(KeyChangeLog.project_id == project_id)
    doomed = list(session.exec(stmt).all())
    if not doomed:
        return 0
    highest: dict[int, int] = {}
    for row in doomed:
        highest[row.project_id] = max(highest.get(row.project_id, 0), row.id)
    # THE FLOOR IS RAISED FIRST, THE ROWS GO SECOND (review A4). `settings.set_many`
    # opens its own Session, so the two writes can never share a transaction; the only
    # question is which order fails safe. Floor-then-delete can leave a consumer told
    # "resync" a moment before the rows it would have missed are actually gone -- an
    # unnecessary re-bootstrap, and nothing worse. Delete-then-floor leaves a window
    # where the rows are already destroyed and the floor still reads 0, so a poll
    # lands on `entries: [], resync_required: False` -- "you are all caught up" for
    # changes that no longer exist anywhere. Over-reporting resync is safe; silently
    # under-reporting is the one thing this feed must never do.
    settings.set_many(
        {
            _PROJECT_FLOOR_PREFIX + str(pid): str(max(hi, changelog_floor(session, pid)))
            for pid, hi in highest.items()
        }
    )
    for row in doomed:
        session.delete(row)
    session.commit()
    return len(doomed)


def parse_modified_after(raw) -> datetime:
    """Parse an ISO-8601 `modified_after` value to a NAIVE-UTC datetime (raise
    BadTimestampError on failure). StringKey.updated_at / translations_modified_at are
    stored naive-UTC (`datetime.utcnow`), so:
      * a tz-AWARE input is converted to UTC and its tzinfo dropped, and
      * a NAIVE input is ASSUMED to already be UTC (documented in docs/CLI.md + the
        API help). Callers in other zones must send an explicit offset.
    Accepts the common `...Z` suffix as well as a bare date or date-time."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw or "").strip()
        if not s:
            raise BadTimestampError(raw)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise BadTimestampError(raw) from e
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
