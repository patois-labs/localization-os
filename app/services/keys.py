"""Key-based projects: the STRING-KEY IDENTITY LAYER over the Segment spine.

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports KEY_DOC_FMT, KEY_DOC_PREFIX, DEFAULT_NAMESPACE_LABEL,
KEY_TRASH_FILENAME, NESTED_SEP, CLDR_CATEGORIES, SOURCE_CHANGED_FLAG_TYPE, ENABLED_SETTING,
AUTOUNVERIFY_SETTING, RETENTION_SETTING, _PROJECT_FLOOR_PREFIX, DEFAULT_DELTA_LIMIT,
MAX_DELTA_LIMIT, MAX_DELTA_ENTRIES, ORIGIN_HUMAN, ORIGIN_IMPORT, ORIGIN_API, ORIGINS,
KeyServiceError, _coerce_char_limit, DuplicateKeyError, NoTargetLocalesError, PluralDemotionRefused,
UnknownPluralCategory, NotAKeyProjectError, BadTimestampError, UnknownLocaleError, enabled,
autounverify_enabled, set_autounverify, retention_days, is_internal_key_document, is_trash_document,
_plural_cache, _gettext_cache, _babel_locale, plural_categories_for, source_changed_flag,
merge_source_changed_flag, clear_source_changed_flag, validate_namespace, document_filename,
internal_document, trash_document, key_documents, translatable_documents, TranslationEngineError,
KeyBudgetError, _project, values_for, _value_order, _log, OrphanKeyValueError,
_append_segment_changes, forget_project_settings, key_views, changelog_floor, prune_changelog,
parse_modified_after, gettext_categories_for, launch_translation, _move_value_rows, namespaces,
_guard, base_values, _categories_for_key, get_key, list_keys, count_keys, key_view, search_keys,
page_keys, delete_key, parse_modified_after_cursor, changes_since, gettext_index_map, iter_keys,
EDIT_META_FIELDS, _PLAIN_META_FIELDS, EDIT_FIELDS, _write_key_edit, update_key, rename_key,
move_namespace, restore_key, ensure_value_rows, set_base_value, set_value, changes_modified_after
from app.services.keys_hub, app.services.keys_hub2, app.services.keys_hub3.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports is_key_project, GNU_PLURAL_FORMS, gnu_plural_forms,
_actor, _ACTOR_ATTR, _inheritance_installed, _install_actor_inheritance, actor_scope, current_actor,
_mock_fallback_workflow, _utcnow, active_locales, _next_order_index, _source_for,
log_value_lock_change, _changed, log_bulk_value_deletions, _key_dialect, _key_order,
_KEY_TRIM_CHARS, _key_statement, _key_view, _audit, _key_target, content_hash,
_modified_after_token, _enrich_value_entries from app.services.keys_common, app.services.keys_part2.

SPEC-key-based-projects sections 2, 3 and 8. The architecture law, in one sentence:

    Keys are an identity layer on top of the existing Segment spine -- never a
    parallel pipeline.

A key's VALUES are real `Segment` rows under internal, system-managed `Document`
rows (one per namespace, `fmt="keys"`), so the orchestrator, TM leverage, glossary
matching, LLMemory recall, LQA, the CAT editor, the export tag guard, spend safety
and org/customer isolation all apply to key content with ZERO pipeline changes.
Nothing in this module imports (let alone modifies) orchestrator.py, translate.py or
store.py: it produces the rows those modules already know how to consume.

WHAT LIVES HERE
  * the kill switch + the two behaviour switches (section 8);
  * the internal-document wiring (`internal_document`, `is_internal_key_document`);
  * CRUD on keys with the identity tuple, soft delete, restore;
  * the value write seams (`set_base_value`, `set_value`) including AUTO-UNVERIFY;
  * CLDR plural support via Babel (`plural_categories_for`), materialization, and
    the demote refusal;
  * the Delta API's read (`changes_since`), its retention pruning, and its
    pruned-floor resync signal.

THE CHANGE FEED IS AN ORM HOOK, NOT A CALL SITE (the decision worth defending).
`KeyChangeLog` must carry EVERY mutation of a key value -- human edit, import, API
write, TM auto-apply, AND workflow write-back -- because a delta consumer that misses
one accumulates silently-stale strings, and a consumer that misses a DELETION
accumulates zombie keys forever (research 01 section 6). Spraying `_log(...)` calls
across every writer would guarantee the opposite: the orchestrator writes
`Segment.target_text` directly and is deliberately outside this batch's blast radius,
so a call-site design would have shipped a feed that never reports a single machine
translation. Instead a single `before_flush` listener turns "a key's value row
changed" into a changelog row NO MATTER WHO WROTE IT. It is structurally inert on a
document install: the very first thing it does is look for a Segment carrying a
non-NULL `key_id`, of which there are none.

THE LISTENER IS REGISTERED IN `app/db.py`, NOT HERE (review A2). Registering it as an
import side effect of THIS module made it real only in processes that happened to
touch a key surface: a CLI/worker/API process that imported `app.db` and wrote
`target_text` the orchestrator way logged NOTHING, which falsifies the whole "no
matter who wrote it" claim. `app.db` is imported by every process that can reach a
Segment at all, so the hook now lives beside the engine: the cheap guard is
`app.db._key_change_feed` (it needs only `models.Segment`) and it calls
`_append_segment_changes` below -- imported lazily -- only once a key value row is
actually in the flush. Nothing here registers a listener, so there is exactly one.

KNOWN EDGES, recorded rather than hidden:
  * The listener sees ORM writes. THREE bulk `sa_delete(Segment)` paths bypass it:
    content.delete_project and retention.delete_document destroy the whole
    project/document (a per-value tombstone is moot -- a whole-project delete is a
    404 to a consumer, which spec section 5 defines as "gone, re-bootstrap"), but
    content.remove_target_locale leaves the PROJECT alive while removing one locale's
    segments, so a consumer would keep zombie value rows forever. That path therefore
    calls `log_bulk_value_deletions` explicitly before its bulk delete; the tombstones
    are real entries, not an absence. `retention.delete_document` does the same when
    the document it is reaping is an internal key document, because there too the
    project (and every key) survives.
  * A workflow write-back is attributed to whoever entered `actor_scope`, INCLUDING
    across the thread hand-off every web/batch launcher performs (orchestrator's
    daemon run thread, batch_runs' thread per locale) -- see `actor_scope` for how,
    and why it needs no orchestrator change.
  * A key's BASE value has no home other than its value rows' `source_text` (spec
    2.5), so a project with zero target locales has nowhere to put one:
    `set_base_value` REFUSES loudly (NoTargetLocalesError) rather than accepting a
    write it would silently drop.
  * A TOMBSTONED key's value rows stay on disk so `restore_key` can bring them back,
    but the feed stops emitting `value` entries for them: "an update to a key I
    already deleted" has no meaning in the consumer contract (spec section 5).

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, cast, func, or_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    DocStatus,
    Document,
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    Project,
    ProjectType,
    RunStatus,
    Segment,
    SegStatus,
    StringKey,
    TargetLocale,
    TransMode,
)
from app.services import settings
from app.services.keys_common import (
    _ACTOR_ATTR,
    _KEY_TRIM_CHARS,
    GNU_PLURAL_FORMS,
    _actor,
    _audit,
    _changed,
    _inheritance_installed,
    _install_actor_inheritance,
    _key_dialect,
    _key_order,
    _key_statement,
    _key_target,
    _key_view,
    _mock_fallback_workflow,
    _modified_after_token,
    _next_order_index,
    _source_for,
    _utcnow,
    active_locales,
    actor_scope,
    content_hash,
    current_actor,
    gnu_plural_forms,
    import_memo_of,
    is_key_project,
    log_bulk_value_deletions,
    log_value_lock_change,
)
from app.services.keys_hub import (
    _PROJECT_FLOOR_PREFIX,
    AUTOUNVERIFY_SETTING,
    CLDR_CATEGORIES,
    DEFAULT_DELTA_LIMIT,
    DEFAULT_NAMESPACE_LABEL,
    ENABLED_SETTING,
    KEY_DOC_FMT,
    KEY_DOC_PREFIX,
    KEY_NAME_UNAVAILABLE,
    KEY_TRASH_FILENAME,
    MAX_DELTA_ENTRIES,
    MAX_DELTA_LIMIT,
    NESTED_SEP,
    ORIGIN_API,
    ORIGIN_HUMAN,
    ORIGIN_IMPORT,
    ORIGINS,
    RETENTION_SETTING,
    SOURCE_CHANGED_FLAG_TYPE,
    BadTimestampError,
    DuplicateKeyError,
    KeyBudgetError,
    KeyServiceError,
    NotAKeyProjectError,
    NoTargetLocalesError,
    OrphanKeyValueError,
    PluralDemotionRefused,
    TranslationEngineError,
    UnknownLocaleError,
    UnknownPluralCategory,
    _append_segment_changes,
    _babel_locale,
    _coerce_char_limit,
    _gettext_cache,
    _log,
    _plural_cache,
    _project,
    _value_order,
    autounverify_enabled,
    changelog_floor,
    clear_source_changed_flag,
    document_filename,
    enabled,
    forget_project_settings,
    internal_document,
    is_internal_key_document,
    is_trash_document,
    key_documents,
    key_views,
    merge_source_changed_flag,
    parse_modified_after,
    plural_categories_for,
    prune_changelog,
    retention_days,
    set_autounverify,
    source_changed_flag,
    translatable_documents,
    trash_document,
    validate_namespace,
    values_for,
)
from app.services.keys_hub2 import (
    _categories_for_key,
    _guard,
    _move_value_rows,
    base_values,
    changes_since,
    count_keys,
    delete_key,
    get_key,
    gettext_categories_for,
    key_view,
    launch_translation,
    list_keys,
    namespaces,
    page_keys,
    parse_modified_after_cursor,
    search_keys,
)
from app.services.keys_hub3 import (
    _PLAIN_META_FIELDS,
    EDIT_FIELDS,
    EDIT_META_FIELDS,
    _write_key_edit,
    changes_modified_after,
    ensure_value_rows,
    gettext_index_map,
    iter_keys,
    move_namespace,
    rename_key,
    restore_key,
    set_base_value,
    set_value,
    update_key,
)
from app.services.keys_part2 import _enrich_value_entries

__all__ = [
    "AUTOUNVERIFY_SETTING",
    "CLDR_CATEGORIES",
    "DEFAULT_DELTA_LIMIT",
    "DEFAULT_NAMESPACE_LABEL",
    "EDIT_FIELDS",
    "EDIT_META_FIELDS",
    "ENABLED_SETTING",
    "GNU_PLURAL_FORMS",
    "JSONB",
    "KEY_DOC_FMT",
    "KEY_DOC_PREFIX",
    "KEY_NAME_UNAVAILABLE",
    "KEY_TRASH_FILENAME",
    "MAX_DELTA_ENTRIES",
    "MAX_DELTA_LIMIT",
    "NESTED_SEP",
    "ORIGINS",
    "ORIGIN_API",
    "ORIGIN_HUMAN",
    "ORIGIN_IMPORT",
    "RETENTION_SETTING",
    "SOURCE_CHANGED_FLAG_TYPE",
    "_ACTOR_ATTR",
    "_KEY_TRIM_CHARS",
    "_PLAIN_META_FIELDS",
    "_PROJECT_FLOOR_PREFIX",
    "BadTimestampError",
    "DocStatus",
    "Document",
    "DuplicateKeyError",
    "KeyBudgetError",
    "KeyChangeLog",
    "KeyServiceError",
    "NoTargetLocalesError",
    "NotAKeyProjectError",
    "OrphanKeyValueError",
    "PluralDemotionRefused",
    "Project",
    "ProjectType",
    "RunStatus",
    "SegStatus",
    "Segment",
    "TargetLocale",
    "TransMode",
    "TranslationEngineError",
    "UnknownLocaleError",
    "UnknownPluralCategory",
    "_actor",
    "_append_segment_changes",
    "_audit",
    "_babel_locale",
    "_categories_for_key",
    "_changed",
    "_coerce_char_limit",
    "_enrich_value_entries",
    "_gettext_cache",
    "_guard",
    "_inheritance_installed",
    "_install_actor_inheritance",
    "_key_dialect",
    "_key_order",
    "_key_statement",
    "_key_target",
    "_key_view",
    "_log",
    "_mock_fallback_workflow",
    "_modified_after_token",
    "_move_value_rows",
    "_next_order_index",
    "_plural_cache",
    "_project",
    "_source_for",
    "_utcnow",
    "_value_order",
    "_write_key_edit",
    "active_locales",
    "actor_scope",
    "and_",
    "autounverify_enabled",
    "base_values",
    "cast",
    "changelog_floor",
    "changes_modified_after",
    "changes_since",
    "clear_source_changed_flag",
    "content_hash",
    "count_keys",
    "current_actor",
    "datetime",
    "delete_key",
    "document_filename",
    "enabled",
    "ensure_value_rows",
    "forget_project_settings",
    "func",
    "get_key",
    "gettext_categories_for",
    "gettext_index_map",
    "gnu_plural_forms",
    "hashlib",
    "internal_document",
    "is_internal_key_document",
    "is_key_project",
    "is_trash_document",
    "iter_keys",
    "key_documents",
    "key_view",
    "key_views",
    "launch_translation",
    "list_keys",
    "log_bulk_value_deletions",
    "log_value_lock_change",
    "merge_source_changed_flag",
    "move_namespace",
    "namespaces",
    "or_",
    "page_keys",
    "parse_modified_after",
    "parse_modified_after_cursor",
    "plural_categories_for",
    "prune_changelog",
    "rename_key",
    "restore_key",
    "retention_days",
    "sa_inspect",
    "search_keys",
    "select",
    "set_autounverify",
    "set_base_value",
    "set_value",
    "settings",
    "source_changed_flag",
    "threading",
    "time",
    "timedelta",
    "timezone",
    "translatable_documents",
    "trash_document",
    "update_key",
    "validate_namespace",
    "values_for",
]


def create_key(
    session: Session,
    project,
    name: str,
    *,
    namespace: str = "",
    context: str = "",
    description: str = "",
    char_limit=None,
    is_plural: bool = False,
    plural_arg_name=None,
    tags=None,
    source_file: str = "",
    custom=None,
    is_hidden: bool = False,
    is_archived: bool = False,
    base_value=None,
    base_fill_missing: bool = True,
    actor: str = "",
    known_absent: bool = False,
) -> StringKey:
    """Create a key (and, when `base_value` is given, its value rows).

    `known_absent=True` (#585, the bulk key import only) says the caller has just read this
    identity and found no live key, so the per-key courtesy lookup is skipped. The partial
    unique index stays the real guarantee: a concurrent creator still lands in the
    IntegrityError branch below and gets the same DuplicateKeyError.

    `base_fill_missing=False` says the `base_value` map means EXACTLY the forms it
    names, so a plural form it does not mention is born with an empty source instead
    of a copy of `other` (live-QA K5). IT IS A PROPERTY OF THE CALL, NOT OF THE DATA,
    for the same reason the export's name shape is: `keys add --plural --base X` and
    the web add form both wrap ONE singular string as `{"other": X}` and their help has
    always said it fills `other`, while a caller handing over a genuine per-form map
    (`{"one": "1 item", "other": "%d items"}` -- the /api bulk upsert, an import) means
    the documented fallback, where ru's `few` legitimately adapts from an English
    source that has no `few` at all. Inferring the difference from the map's shape
    would make "did you happen to name more than one form?" change the semantics.

    UPSERT SEMANTICS on the identity tuple: an existing LIVE key with the same
    (namespace, name, context) is a DuplicateKeyError, never a silent merge. A
    SOFT-DELETED key with that tuple does not block the create -- the partial unique
    index is defined exactly so a name can be reused after a cleanup import."""
    proj = _project(session, project)
    name = (name or "").strip()
    if not name:
        raise KeyServiceError("a key name is required.")
    namespace, context = validate_namespace(namespace), (context or "").strip()
    # K8 ingestion hardening: `create_key` never validated char_limit at all — a non-integer,
    # a 0 or a negative from an /api upsert, a JSON import or a bulk call landed straight
    # in the column, where the write-seam predicate then had to defend against a TypeError.
    # Refuse it here, at the one create seam every surface routes through.
    char_limit = _coerce_char_limit(char_limit)
    if not known_absent and get_key(session, proj.id, name, namespace, context) is not None:
        raise DuplicateKeyError(namespace, name, context)
    now = _utcnow()
    key = StringKey(
        project_id=proj.id,
        branch_id=None,
        namespace=namespace,
        name=name,
        context=context,
        description=description or "",
        char_limit=char_limit,
        is_plural=bool(is_plural),
        plural_arg_name=plural_arg_name,
        tags=list(tags) if tags else None,
        source_file=source_file or "",
        is_hidden=bool(is_hidden),
        is_archived=bool(is_archived),
        custom=dict(custom) if custom else None,
        created_at=now,
        updated_at=now,
    )
    with actor_scope(actor or current_actor()):
        # ONE transaction for the key AND its `created` entry (review A5). Committing
        # the key first left a window where a failure appended a LIVE key the delta
        # feed never announces -- and with no base value there are no value rows
        # either, so the key would have been invisible to every consumer forever.
        session.add(key)
        try:
            session.flush()
            _log(session, key, entity=KeyEntity.KEY, op=KeyOp.CREATED, actor=actor or None)
            session.commit()
        except IntegrityError as exc:
            # The partial unique index is the real guarantee; the get_key check above
            # is a courtesy that two concurrent creators can both pass. Answer with
            # the SAME named refusal rather than a raw 500 (review A8).
            session.rollback()
            raise DuplicateKeyError(namespace, name, context) from exc
        # #585: inside a bulk import the session does not expire on commit, so the key is
        # already current and the two reload SELECTs per key are skipped.
        memo = import_memo_of(session)
        bulk = memo is not None
        if bulk:
            memo.setdefault("fresh_keys", set()).add(key.id)
        else:
            session.refresh(key)
        if base_value is not None:
            set_base_value(session, key, base_value, actor=actor, fill_missing=base_fill_missing)
    if not bulk:
        session.refresh(key)
    return key


def apply_key_edit(session: Session, key, *, actor: str = "", **fields) -> StringKey:
    """ONE key edit, VALIDATED IN FULL BEFORE THE FIRST WRITE.

    THE FINDING THIS EXISTS FOR (review B6). An edit can touch five seams --
    `set_plural`, `update_key`, `rename_key`, `move_namespace`, `set_base_value` --
    and EVERY ONE OF THEM COMMITS. Calling them in sequence therefore had no
    all-or-nothing property at all: `PATCH /api/keys/{id}` with a good description and
    a colliding name answered 400 while the description was already durable AND an
    `updated` entry was already on the change feed. A delta consumer that reads a
    refusal as "nothing happened" then drifts from the server permanently -- the one
    failure mode the feed exists to prevent.

    So every refusal this edit could raise is raised FIRST, against the state the edit
    WOULD produce: the resulting identity tuple (namespace, name, context -- all three
    are editable, and all three are identity), a non-integer char limit, a plural
    demotion that would destroy translated forms, and a promote or base write on a
    project with no target locales. Only then does anything get written.

    What is deliberately NOT claimed: this is not a transaction. A concurrent writer
    can still take the name between the check and the rename, and the partial unique
    index will answer that one with a DuplicateKeyError from `rename_key` itself. The
    class of failure this closes is the DETERMINISTIC one -- the bad request the caller
    could have been told about before anything moved -- which is every case the
    reviewer reproduced.

    THE SHARED SEAM for `PATCH /api/keys/{id}` and `keys edit` (the parity law): the
    two surfaces cannot drift into validating differently, and the CLI's remote arm
    reaches the same code through the route."""
    bad = [f for f in fields if f not in EDIT_FIELDS]
    if bad:
        raise KeyServiceError(
            "cannot edit {} (allowed: {}).".format(", ".join(sorted(bad)), ", ".join(EDIT_FIELDS))
        )
    force = bool(fields.pop("force", False))
    fields = {f: v for f, v in fields.items() if v is not None or f in ("char_limit",)}

    # --- 1. validate -------------------------------------------------------
    new_name = key.name
    if "name" in fields:
        new_name = str(fields["name"] or "").strip()
        if not new_name:
            raise KeyServiceError("a key name is required.")
    new_ns = (
        validate_namespace(str(fields["namespace"]))
        if "namespace" in fields
        else (key.namespace or "")
    )
    new_ctx = str(fields["context"] or "").strip() if "context" in fields else (key.context or "")
    if (new_name, new_ns, new_ctx) != (key.name, key.namespace or "", key.context or ""):
        clash = get_key(session, key.project_id, new_name, new_ns, new_ctx)
        if clash is not None and clash.id != key.id:
            raise DuplicateKeyError(new_ns, new_name, new_ctx)
    if fields.get("char_limit") is not None:
        # K8: was `int()`-only, so 0 and negatives passed to the column. `_coerce_char_limit`
        # rejects <=0 and non-integers and caps at CHAR_LIMIT_MAX — the same rule create_key
        # and the doc-side context slot enforce, in ONE place so the surfaces cannot drift.
        fields["char_limit"] = _coerce_char_limit(fields["char_limit"])
    if "tags" in fields and not isinstance(fields["tags"], (list, tuple)):
        raise KeyServiceError("tags must be a list of strings.")

    want_plural = fields.pop("is_plural", None)
    if want_plural is not None:
        want_plural = bool(want_plural)
        if want_plural != bool(key.is_plural):
            if not want_plural and not force:
                lost = {
                    s.plural_category
                    for s in values_for(session, key)
                    if (s.plural_category or "other") != "other" and (s.target_text or "").strip()
                }
                if lost:
                    raise PluralDemotionRefused(lost)
            if want_plural and not active_locales(session, key.project_id):
                # `ensure_value_rows` would raise this HALFWAY through the promote.
                raise NoTargetLocalesError(key.project_id)
    base = fields.pop("base", None)
    if base is not None and not active_locales(session, key.project_id):
        raise NoTargetLocalesError(key.project_id)

    # --- 2. apply ----------------------------------------------------------
    # ONE seam for the identity tuple AND the metadata (re-review W2): one
    # transaction, one commit, one `updated` entry. `context` is deliberately NOT in
    # `meta` -- it is identity, and it travels with `name` and `namespace`.
    meta = {f: v for f, v in fields.items() if f in _PLAIN_META_FIELDS}
    if want_plural is not None:
        set_plural(
            session,
            key,
            want_plural,
            plural_arg_name=meta.pop("plural_arg_name", None),
            force=force,
            actor=actor,
        )
    _write_key_edit(
        session, key, name=new_name, namespace=new_ns, context=new_ctx, meta=meta, actor=actor
    )
    if base is not None:
        set_base_value(session, key, base, actor=actor)
    session.refresh(key)
    return key


def set_plural(
    session: Session,
    key,
    is_plural: bool,
    *,
    plural_arg_name=None,
    force: bool = False,
    actor: str = "",
) -> StringKey:
    """Promote a key to plural (materializing every locale's CLDR forms) or demote it
    back to a single value.

    DEMOTION REFUSES (PluralDemotionRefused) when any non-`other` form still holds a
    translation, because demoting destroys those rows. `force=True` accepts the loss.
    The `other` form is the survivor in both directions, so promote-then-demote round
    trips the value a simple key had."""
    is_plural = bool(is_plural)
    if is_plural == key.is_plural:
        if plural_arg_name is not None and plural_arg_name != key.plural_arg_name:
            return update_key(session, key, plural_arg_name=plural_arg_name, actor=actor)
        return key
    with actor_scope(actor or current_actor()):
        rows = values_for(session, key)
        if is_plural:
            # The existing single row becomes `other`; siblings are materialized.
            for seg in rows:
                seg.plural_category = "other"
                session.add(seg)
            key.is_plural = True
            if plural_arg_name is not None:
                key.plural_arg_name = plural_arg_name
            key.updated_at = _utcnow()
            session.add(key)
            session.flush()
            ensure_value_rows(session, key, actor=actor)
        else:
            lost = {
                s.plural_category
                for s in rows
                if (s.plural_category or "other") != "other" and (s.target_text or "").strip()
            }
            if lost and not force:
                raise PluralDemotionRefused(lost)
            for seg in rows:
                if (seg.plural_category or "other") != "other":
                    session.delete(seg)
            session.flush()
            for seg in values_for(session, key):
                seg.plural_category = None
                session.add(seg)
            key.is_plural, key.plural_arg_name = False, None
            key.updated_at = _utcnow()
            session.add(key)
        _log(session, key, entity=KeyEntity.KEY, op=KeyOp.UPDATED, actor=actor or None)
        session.commit()
    session.refresh(key)
    return key
