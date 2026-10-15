"""Key file round trip: JSON (flat + nested) and gettext .po.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports FORMATS, EMPTY_MODES, MODES, SORTS, PLURAL_STYLES,
JSON_META_KEY, JSON_META_OWNER, PO_META_COMMENT, PO_LOCK_COMMENT_PREFIX, _PO_LOCK_RE,
NS_MODE_QUALIFIED, SUB_KEPT_FOR_TRANSPORT, SUB_TM_LOCKED, _LOCK_STATE_VOCAB, _LOCK_STATE_FROM_VOCAB,
_lock_transport, _lock_from_transport, SUB_EMPTY_AS_BASE, CHAR_OVER_LIMIT, FILTERED_OUT,
NOTICE_NAME_CAP, _NamespaceFromKey, NS_FROM_KEY, KeyFileError, KeyCollisionError, _is_plural_object,
_FORCED_NUMERIC_SEGMENT, _UNREPRESENTABLE_SEGMENT, _is_forced_segment, _scalar, _take_json_meta,
_record_origin, parse_po_keys, _note_merged_slot, _MSGSTR_SLOT, _MSGID_LINE,
_note_dropped_msgstr_slots, split_namespace, qualified_name, format_for_filename,
namespace_for_filename, _report_names, substituted_keys, kept_damaged_keys, tm_locked_keys,
empty_as_base_keys, over_char_limit_keys, filtered_out_keys, _UNSAFE_IN_NAME, ascii_name,
download_filename, export_notices, HDR_SUBSTITUTED, HDR_SUBSTITUTED_DETAIL, HDR_SUBSTITUTED_KEYS,
HDR_KEPT, HDR_KEPT_KEYS, HDR_WITHHELD, HDR_WITHHELD_KEYS, HDR_OVER_LIMIT, HDR_OVER_LIMIT_KEYS,
HDR_FILTERED, HDR_FILTERED_KEYS, HEADER_NAME_CAP, HEADER_VALUE_BUDGET, _MORE, _DETAIL_OVERHEAD,
_fit_names, _header_list, export_headers, audit_detail, report_from_headers, _name_list,
_CTRL_IN_NAME, IMPORT_NOTICE_BUDGET, fit_name_list, import_notices, _resolve_locale, _key_label,
_note, _note_skipped, _fold, _restates_source, flatten_json, unflatten_json, _no_duplicate_members,
parse_json_keys, render_json_keys, render_po_keys, parse_keys, _apply_record, export_locale from
app.services.key_files_po_parse_render, app.services.key_files_json_parse_render,
app.services.key_files_export.

SPEC-key-based-projects section 4. The maintainer's locked call #1 fixed v1's formats at JSON
and .po; everything else lands later as additive parsers behind the same two seams
(`import_keys`, `export_locale`).

WHY THIS IS NOT `app/filters/` (the seam-map's finding, worth restating at the top of
the file someone will try to "unify" one day). The `Filter` abstraction is POSITIONAL:
`parse()` returns a skeleton plus an ORDERED list of TextUnits and `build()` puts
translated strings back by index. `json_filter.py` literally discards object keys and
`po_filter.py` turns msgid into source text -- both are correct for a document-driven
project, where the file IS the unit of work, and both are useless here, where the KEY
is the unit of work and the file is a transport. Key parsers therefore live outside the
Filter registry and produce key UPSERTS, not skeleton+units. (That "keys" is not a
registered filter format is also what makes `keys.is_internal_key_document` sound.)

THE FIDELITY RULES THAT ARE PRODUCT, NOT POLISH (research 01 section 9.6, risk 5):
  * `::` is the nested separator, never `.` -- so a key literally named "user.name"
    and two levels of nesting stay distinguishable. `:::` forces object output for a
    numeric-looking segment, and that escape is NARROW: a leading ':' means something
    only in front of digits, so a user key named ":weird" round trips untouched.
  * A parse that would collide two paths into ONE key name is a REFUSAL, not a merge --
    including a REPEATED MEMBER inside one JSON object, which every parser silently
    collapses to the last value before a path-level guard could ever see it.
    Lokalise's silent `distinguish_by_file` merge is a documented user trap; we fail
    loudly and name both sides, naming the REAL cause (namespace and/or context).
  * NAMESPACE IS PART OF IDENTITY, so it survives the trip. A WHOLE-PROJECT export
    always qualifies every name as `namespace::name` and says so IN THE FILE; a
    namespace-scoped export always keeps bare names. The shape follows the CALL, never
    the data -- deriving it from "does this export span more than one namespace?" meant
    adding a single key could rewrite every msgid in the product. Feed a marked file
    back with `import_keys(namespace=NS_FROM_KEY)` and export -> import is a fixed
    point; feed it an UNMARKED one and it refuses instead of forking the key space.
    Both renderers REFUSE a duplicate rather than merging -- .po included, where
    gettext's (msgid, msgctxt) key made the silent merge easy to ship.
  * A NAMESPACE CANNOT CONTAIN `::` AND CANNOT BE `__default__` (keys.validate_
    namespace): both make the qualified name ambiguous, and both were reachable.
  * Export ordering is stable (`a_z` by default, `first_added` available) -- a tool
    that reorders keys produces a diff nobody will merge.
  * Plural forms round trip through CLDR NAMES. gettext's numeric `msgstr[N]` indices
    are mapped via the file's own `Plural-Forms:` expression (keys.gettext_*), never
    stored. IMPORT trusts the file's header; EXPORT writes the GNU-canonical header and
    slot order where Babel's differs (Arabic), so msgmerge against a real ar.po cannot
    silently swap `many` and `other`.
  * A CATEGORY THE FILE DID NOT CARRY IS SILENCE, NOT AN EMPTY VALUE. Russian's CLDR
    `other` is fraction-only and has no gettext slot at all; reading its absence as ""
    wrote over a stored translation and called it an update.
  * `#, fuzzy` means "suggested, not reviewed" -- our needs-review state -- in BOTH
    directions.
  * Facts that are neither errors nor content ride in the report's `notes`: lossy JSON
    scalar coercions, vanished empty containers, and msgstr indices the file's own
    header has no slot for.
  * The EXISTING export tag-damage guard runs on every exported value, because key
    values are Segments and `export.segment_tag_damage` is a Segment predicate --
    ON THE BUILD FILE. See "the guard is a DELIVERY guard" below (live-QA K1).
  * SOURCE TEXT IN A TARGET LOCALE IS NOT A TRANSLATION, on the way in (live-QA K1c).
    An imported value that RESTATES that form's own base text -- compared NFC-folded
    and stripped, because byte identity was one trailing newline from bypass
    (fix-round F3), but NOT casefolded, because that refused a whole locale pair
    (final round) -- is never written at all: a translated row keeps its
    translation and an EMPTY row stays empty and stays in the translator's queue
    (fix-round F7). Every skip is named in the report's `notes`.

THE GUARD IS A DELIVERY GUARD, NOT A STORAGE GUARD (live-QA K1, HIGH).
  `export_locale` used to source-substitute a tag-damaged value on EVERY shape. That is
  right for the BUILD file -- damaged markup must never ship -- and destructive on the
  TRANSPORT file, which exists to be re-imported: the substitution wrote ENGLISH into
  the file, and re-importing it overwrote the real (recoverable) translation and landed
  it as `mt`, i.e. as a confident machine translation with the `needs_review` warning
  gone. A pure round trip with no human edit in between destroyed three Russian values
  and reported `3 updated`.
  The guard is therefore scoped to the shape it protects. `qualify` already IS the
  build/transport discriminator (see the marker block below), so:
    * BUILD file (bare names, `namespace=<x>`)      -> substitute, exactly as before.
    * TRANSPORT file (qualified + marked)           -> carry the TRUE stored state.
  Nothing is weakened: no deliverable ever gains a damaged value, and the transport
  file's damaged values are the ones already flagged `needs_review` in the product
  (and emitted `#, fuzzy` in .po). BOTH facts are counted and reported (live-QA K2) --
  `export_notices` renders them for every surface.

  WHAT THAT MEANS FOR `export_tag_damage_policy=quarantine` (fix-round F10, recorded
  because the setting reads as "never let a damaged value out"): the omission is a
  DELIVERY policy too, so it applies to the BUILD file only. A transport file still
  carries the damaged value -- it is not a deliverable, it is this product talking to
  itself, and omitting the value there would silently DELETE the translation on the
  way back in, which is a worse outcome than the one quarantine exists to prevent.
  The value is counted and named as kept-for-transport either way.

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import io
import json
import os
import re

from sqlmodel import Session, select

from app import branding, config
from app.models import Segment, SegStatus
from app.services import keys as keys_svc
from app.services.key_files_bridge import (
    NOT_IN_TEMPLATE,
    all_formats,
    bridge_formats,
    export_ext,
    export_media_type,
    is_bridged,
)
from app.services.key_files_export import export_locale
from app.services.key_files_json_parse_render import (
    _apply_record,
    _no_duplicate_members,
    flatten_json,
    parse_json_keys,
    parse_keys,
    render_json_keys,
    render_po_keys,
    unflatten_json,
)
from app.services.key_files_po_parse_render import (
    _CTRL_IN_NAME,
    _DETAIL_OVERHEAD,
    _FORCED_NUMERIC_SEGMENT,
    _LOCK_STATE_FROM_VOCAB,
    _LOCK_STATE_VOCAB,
    _MORE,
    _MSGID_LINE,
    _MSGSTR_SLOT,
    _PO_LOCK_RE,
    _UNREPRESENTABLE_SEGMENT,
    _UNSAFE_IN_NAME,
    CHAR_OVER_LIMIT,
    EMPTY_BASE,
    EMPTY_EMPTY,
    EMPTY_MODES,
    EMPTY_SKIP,
    FILTERED_OUT,
    FORMATS,
    HDR_FILTERED,
    HDR_FILTERED_KEYS,
    HDR_KEPT,
    HDR_KEPT_KEYS,
    HDR_NOT_IN_TEMPLATE,
    HDR_NOT_IN_TEMPLATE_KEYS,
    HDR_OVER_LIMIT,
    HDR_OVER_LIMIT_KEYS,
    HDR_SUBSTITUTED,
    HDR_SUBSTITUTED_DETAIL,
    HDR_SUBSTITUTED_KEYS,
    HDR_WITHHELD,
    HDR_WITHHELD_KEYS,
    HEADER_NAME_CAP,
    HEADER_VALUE_BUDGET,
    IMPORT_NOTICE_BUDGET,
    JSON_META_KEY,
    JSON_META_OWNER,
    MODE_CLEANUP,
    MODE_MERGE,
    MODES,
    NOTICE_NAME_CAP,
    NS_FROM_KEY,
    NS_MODE_QUALIFIED,
    PLURAL_I18NEXT,
    PLURAL_OBJECT,
    PLURAL_STYLES,
    PO_LOCK_COMMENT_PREFIX,
    PO_META_COMMENT,
    SORT_AZ,
    SORT_FIRST_ADDED,
    SORTS,
    SUB_EMPTY_AS_BASE,
    SUB_KEPT_FOR_TRANSPORT,
    SUB_TM_LOCKED,
    KeyCollisionError,
    KeyFileError,
    _fit_names,
    _fold,
    _header_list,
    _is_forced_segment,
    _is_plural_object,
    _key_label,
    _lock_from_transport,
    _lock_transport,
    _name_list,
    _NamespaceFromKey,
    _note,
    _note_dropped_msgstr_slots,
    _note_merged_slot,
    _note_skipped,
    _record_origin,
    _report_names,
    _resolve_locale,
    _restates_source,
    _scalar,
    _take_json_meta,
    ascii_name,
    audit_detail,
    download_filename,
    empty_as_base_keys,
    export_headers,
    export_notices,
    filtered_out_keys,
    fit_name_list,
    format_for_filename,
    import_notices,
    kept_damaged_keys,
    namespace_for_filename,
    not_in_template_keys,
    over_char_limit_keys,
    parse_po_keys,
    qualified_name,
    report_from_headers,
    split_namespace,
    substituted_keys,
    tm_locked_keys,
)
from app.services.keys import (
    CLDR_CATEGORIES,
    DEFAULT_NAMESPACE_LABEL,
    NESTED_SEP,
    KeyServiceError,
)
from app.services.keys_common import import_memo

__all__ = [
    "CHAR_OVER_LIMIT",
    "CLDR_CATEGORIES",
    "DEFAULT_NAMESPACE_LABEL",
    "EMPTY_BASE",
    "EMPTY_EMPTY",
    "EMPTY_MODES",
    "EMPTY_SKIP",
    "FILTERED_OUT",
    "FORMATS",
    "HDR_FILTERED",
    "HDR_FILTERED_KEYS",
    "HDR_KEPT",
    "HDR_KEPT_KEYS",
    "HDR_NOT_IN_TEMPLATE",
    "HDR_NOT_IN_TEMPLATE_KEYS",
    "HDR_OVER_LIMIT",
    "HDR_OVER_LIMIT_KEYS",
    "HDR_SUBSTITUTED",
    "HDR_SUBSTITUTED_DETAIL",
    "HDR_SUBSTITUTED_KEYS",
    "HDR_WITHHELD",
    "HDR_WITHHELD_KEYS",
    "HEADER_NAME_CAP",
    "HEADER_VALUE_BUDGET",
    "IMPORT_NOTICE_BUDGET",
    "JSON_META_KEY",
    "JSON_META_OWNER",
    "MODES",
    "MODE_CLEANUP",
    "MODE_MERGE",
    "NESTED_SEP",
    "NOTICE_NAME_CAP",
    "NOT_IN_TEMPLATE",
    "NS_FROM_KEY",
    "NS_MODE_QUALIFIED",
    "PLURAL_I18NEXT",
    "PLURAL_OBJECT",
    "PLURAL_STYLES",
    "PO_LOCK_COMMENT_PREFIX",
    "PO_META_COMMENT",
    "SORTS",
    "SORT_AZ",
    "SORT_FIRST_ADDED",
    "SUB_EMPTY_AS_BASE",
    "SUB_KEPT_FOR_TRANSPORT",
    "SUB_TM_LOCKED",
    "_CTRL_IN_NAME",
    "_DETAIL_OVERHEAD",
    "_FORCED_NUMERIC_SEGMENT",
    "_LOCK_STATE_FROM_VOCAB",
    "_LOCK_STATE_VOCAB",
    "_MORE",
    "_MSGID_LINE",
    "_MSGSTR_SLOT",
    "_PO_LOCK_RE",
    "_UNREPRESENTABLE_SEGMENT",
    "_UNSAFE_IN_NAME",
    "KeyCollisionError",
    "KeyFileError",
    "SegStatus",
    "_NamespaceFromKey",
    "_apply_record",
    "_fit_names",
    "_fold",
    "_header_list",
    "_is_forced_segment",
    "_is_plural_object",
    "_key_label",
    "_lock_from_transport",
    "_lock_transport",
    "_name_list",
    "_no_duplicate_members",
    "_note",
    "_note_dropped_msgstr_slots",
    "_note_merged_slot",
    "_note_skipped",
    "_record_origin",
    "_report_names",
    "_resolve_locale",
    "_restates_source",
    "_scalar",
    "_take_json_meta",
    "all_formats",
    "ascii_name",
    "audit_detail",
    "branding",
    "bridge_formats",
    "download_filename",
    "empty_as_base_keys",
    "export_ext",
    "export_headers",
    "export_locale",
    "export_media_type",
    "export_notices",
    "filtered_out_keys",
    "fit_name_list",
    "flatten_json",
    "format_for_filename",
    "import_notices",
    "io",
    "is_bridged",
    "json",
    "kept_damaged_keys",
    "namespace_for_filename",
    "not_in_template_keys",
    "over_char_limit_keys",
    "parse_json_keys",
    "parse_keys",
    "parse_po_keys",
    "qualified_name",
    "re",
    "render_json_keys",
    "render_po_keys",
    "report_from_headers",
    "split_namespace",
    "substituted_keys",
    "tm_locked_keys",
    "unflatten_json",
]


# --- import ------------------------------------------------------------------
#: #585: keys per lookup chunk of a bulk import (the same 1000-row scale as #571's chunks).
IMPORT_CHUNK = 500


def _release_loaded(session: Session, keep: set) -> None:
    """Drop the key, value and feed rows THIS IMPORT loaded and committed from the
    session's identity map, so a 50000-key import does not drag every earlier key
    through every later flush. `keep` holds the ids of the objects the caller's session
    already had when the import began (a caller may hold a key it created before
    importing); those, and anything modified, are never released."""
    from app.models import KeyChangeLog, StringKey

    dirty = {id(o) for o in session.dirty}
    for obj in list(session.identity_map.values()):
        if (
            isinstance(obj, (StringKey, KeyChangeLog, Segment))
            and id(obj) not in dirty
            and id(obj) not in keep
        ):
            session.expunge(obj)


def _prefetch_keys(session: Session, project_id: int, recs: list, *, ns_default, from_key) -> dict:
    """The live keys `recs` name, by (namespace, name, context), in ONE query per
    namespace -- what `get_key` answered one SELECT at a time. As in `get_key`, the
    newest live row wins if an identity somehow has two."""
    from app.models import StringKey

    wanted: dict[str, set] = {}
    for rec in recs:
        name = rec["name"]
        ns = ns_default
        if from_key:
            ns, name = split_namespace(name)
        wanted.setdefault(ns, set()).add(name)
    out: dict = {}
    for ns, names in wanted.items():
        rows = session.exec(
            select(StringKey).where(
                StringKey.project_id == project_id,
                StringKey.namespace == (ns or ""),
                StringKey.name.in_(sorted(names)),
                StringKey.deleted_at.is_(None),
            )
        ).all()
        for row in sorted(rows, key=lambda k: k.id):
            out[(ns, row.name, row.context or "")] = row
    return out


def _base_of(rec: dict):
    """The base value `_apply_record` would hand `set_base_value` for this record."""
    if rec.get("is_plural"):
        return {c: v for c, v in (rec.get("plural_values") or {}).items() if c in CLDR_CATEGORIES}
    return rec.get("value", "")


def import_keys(
    session: Session,
    project,
    filename: str,
    data,
    *,
    fmt: str = "",
    locale: str = "",
    mode: str = MODE_MERGE,
    namespace=None,
    actor: str = "",
    ip: str = "",
    include_hidden: bool = True,
) -> dict:
    """Import one key file into `project` for `locale`, returning a REPORT.

    `include_hidden=False` is for a caller below manager (#540): a key hidden from
    translators that the file names is REFUSED (an `errors[]` entry, never written), and
    the cleanup sweep never deletes or counts one.

    `locale` == the project's source locale writes BASE values (and therefore triggers
    auto-unverify on every other locale); any other locale writes translations.

    PARTIAL FAILURE IS REPORTED, NOT RAISED (Lokalise's `errors[]` shape): a key whose
    write is refused -- a bad plural form, a name that collides with a live key -- is
    recorded in `report["errors"]` and the rest of the file still lands. Only a file we
    cannot PARSE is fatal, because then there is nothing to partially apply.

    `mode="cleanup"` additionally SOFT-DELETES (with tombstones) every live key in the
    imported namespace that the file does not mention. Destructive, so callers gate it
    behind an explicit confirmation exactly like the other destructive verbs, and it is
    accepted ONLY for a BASE-LOCALE import (review B17): a translation file is a
    partial view of the key set by nature -- a translator who has finished 40 of 200
    strings uploads 40 -- so letting one drive deletions wipes 160 KEYS project-wide,
    not merely 160 values.

    `namespace=NS_FROM_KEY` reads each key's namespace off the FRONT of its own name,
    which is how a namespace-qualified export reads back in (review B2). It is ACCEPTED
    ONLY on a file that carries the qualification marker (re-review R2). Nothing about
    a bare nested file distinguishes it from a qualified one -- both have `::` in their
    names -- so pointed at the wrong file the sentinel silently FORKED the key space,
    filing `error::generic` under a brand-new namespace `error` beside the real
    `common::error::generic` and reporting a clean import. An unmarked file is now a
    refusal that names the fix.

    THE LOCALE IS VALIDATED UP FRONT (review B11): it must be the source locale or an
    ACTIVE target. A one-character typo used to create the whole key set and then error
    every single value, leaving a project full of value-less keys behind.
    """
    proj = keys_svc._project(session, project)
    fmt = (fmt or format_for_filename(filename) or "json").lower()
    from app.services import key_files_bridge as bridge

    bridged = bridge.resolve(fmt)  # Batch 9: a resource format through its filter
    fmt = bridged or fmt
    mode = (mode or MODE_MERGE).lower()
    if mode not in MODES:
        raise KeyFileError(
            "unknown import mode {!r} (expected {}).".format(mode, " or ".join(MODES))
        )
    locale = _resolve_locale(session, proj, locale or proj.source_locale, "import")
    from_key = namespace is NS_FROM_KEY
    ns_default = (
        ""
        if from_key
        else (
            namespace_for_filename(filename)
            if namespace is None
            else keys_svc.validate_namespace(namespace)
        )
    )
    is_base = locale == proj.source_locale
    if mode == MODE_CLEANUP and not is_base:
        raise KeyFileError(
            f"cleanup mode deletes KEYS, so it is only accepted for a {proj.source_locale} (source) "  # noqa: E501
            f"import; {locale!r} is a translation file and is a partial view of the key set by "
            "nature. Import it as a merge, or run the cleanup from the base file."
        )
    notes: list[str] = []
    #: The K1(c) skips as BARE KEY LABELS, parallel to the sentences in `notes` (final
    #: round, blocker 2). `import_notices` needs names, not prose, to fit them all into
    #: a bounded one-shot notice; parsing them back out of the sentences would be a
    #: string-shaped coupling between the message text and the grid.
    skipped: list[str] = []
    meta: dict = {}
    records = parse_keys(data, fmt, locale=locale, notes=notes, meta=meta)
    # #576: rows, not just keys. Each key becomes a segment row per active target language
    # when the project materializes its values, so the #571 row cap applies to keys x locales.
    _n_locales = max(1, len(keys_svc.active_locales(session, proj.id)))
    if len(records) * _n_locales > config.MAX_DOCUMENT_ROWS:
        raise KeyFileError(
            f"this key file would create {len(records) * _n_locales} segment rows "
            f"({len(records)} keys x {_n_locales} target languages); the limit is "
            f"{config.MAX_DOCUMENT_ROWS} (MAX_DOCUMENT_ROWS). Remove target languages from the "
            "project or split the file."
        )
    # TM locks (§8, D-1, MED-6): a JSON transport carries them in $meta.locks (out_name ->
    # {slot: lockdict}); distribute onto each record so _apply_record sees rec["lock"]
    # uniformly, exactly like a PO file already carries it per-record. Never overwrites a
    # lock the record already parsed (PO wins for its own file).
    _lockmap = meta.get("locks") or {}
    if _lockmap:
        for rec in records:
            if rec.get("lock") is None and rec.get("name") in _lockmap:
                rec["lock"] = _lockmap[rec["name"]]
    if from_key and meta.get("namespaces") != NS_MODE_QUALIFIED:
        raise KeyFileError(
            f"{filename!r} does not say it is namespace-qualified, so its key names cannot be "
            "read as `namespace::name` -- doing it anyway would file every key under a "
            "namespace invented from its first path segment. Only a WHOLE-PROJECT "
            "export carries that marker. Import this file with an explicit namespace "
            "(or none, to take the file stem)."
        )
    if bridged:
        # Batch 9: a key another FORMAT already defined in this namespace is a collision,
        # refused before anything is written (two resource files are two key sets).
        bridge.check_cross_format(session, proj.id, ns_default, bridged, filename, records)
        if is_base and records:
            # The source file is the export template for this namespace and format (only
            # when it brings keys: project delete finds templates through key namespaces).
            raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
            bridge.save_template(proj, ns_default, bridged, bytes(raw))

    report = {
        "filename": filename,
        "fmt": fmt,
        "locale": locale,
        "mode": mode,
        "namespace": "(from key names)" if from_key else ns_default,
        "total": len(records),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "errors": [],
        "notes": notes,
        "skipped_keys": skipped,
    }
    # (namespace, name, context) of every key the FILE MENTIONED, recorded BEFORE any
    # write is attempted (review B1). It used to be recorded after the create/update
    # block, so a key whose own write was REFUSED -- a plural demotion, a bad form --
    # counted as absent from the file and was soft-deleted by the cleanup sweep below.
    # A refusal must never be indistinguishable from an omission.
    touched: set[tuple[str, str, str]] = set()
    namespaces_seen: set[str] = set()

    with keys_svc.actor_scope(actor or keys_svc.current_actor()), import_memo(session) as memo:
        known: dict = {}
        preexisting = {id(o) for o in session.identity_map.values()}
        for idx, rec in enumerate(records):
            if idx % IMPORT_CHUNK == 0:
                # #585: one lookup per chunk of IMPORT_CHUNK keys instead of one per key,
                # and a fresh memo so a locale another writer adds mid-import is seen
                # within one chunk. Every key still commits on its own (below the seam),
                # so other writers are never held off for more than one key's write.
                memo.clear()
                _release_loaded(session, preexisting)
                # Gate: the project row (auto-unverify setting) is re-read per chunk too.
                if proj in session:
                    session.expire(proj)
                known = _prefetch_keys(
                    session,
                    proj.id,
                    records[idx : idx + IMPORT_CHUNK],
                    ns_default=ns_default,
                    from_key=from_key,
                )
            # Gate: the active locales are read once PER KEY (then shared by that key's
            # ensure/set calls), never per chunk. Adding a language materializes rows for
            # the keys that exist at that moment; a key created later from a stale list
            # would never get its row for the new language (or would get one for a
            # removed language), and nothing later fills that gap.
            memo.pop(("locales", proj.id), None)
            name, context = rec["name"], rec.get("context") or ""
            ns = ns_default
            if from_key:
                ns, name = split_namespace(name)
            touched.add((ns, name, context))
            namespaces_seen.add(ns)
            try:
                key = known.get((ns, name, context))
                made = False
                if key is None:
                    try:
                        key = keys_svc.create_key(
                            session,
                            proj,
                            name,
                            namespace=ns,
                            context=context,
                            description=rec.get("description") or "",
                            is_plural=bool(rec.get("is_plural")),
                            source_file=os.path.basename(filename or ""),
                            actor=actor,
                            # #585: a BASE import of a new key hands its base value to
                            # create_key (the same set_base_value call _apply_record would
                            # make) instead of paying a second ensure/values_for round and a
                            # before/after read whose result is ignored for a created key.
                            base_value=_base_of(rec) if is_base else None,
                            known_absent=True,
                        )
                        report["created"] += 1
                        made = True
                    except keys_svc.DuplicateKeyError:
                        # Review: the chunk lookup is up to one chunk old, so another
                        # writer may have created this very key since. Look it up once
                        # more and treat it as the existing key it now is (what the
                        # per-key lookup of old would have seen); only a key that is
                        # still absent is a real duplicate.
                        key = keys_svc.get_key(session, proj.id, name, ns, context)
                        if key is None:
                            raise
                    known[(ns, name, context)] = key
                if not made:
                    if key.is_hidden and not include_hidden:
                        # #540: same refusal the bulk upsert gives; the key is untouched.
                        report["errors"].append(
                            {
                                "key": name,
                                "context": context,
                                "error": keys_svc.KEY_NAME_UNAVAILABLE,
                            }
                        )
                        continue
                    if rec.get("description") and rec["description"] != key.description:
                        keys_svc.update_key(
                            session, key, description=rec["description"], actor=actor
                        )
                    if bool(rec.get("is_plural")) != bool(key.is_plural):
                        # An import that turns a key plural materializes the forms; the
                        # DEMOTE direction keeps its refusal (force is never implicit).
                        keys_svc.set_plural(session, key, bool(rec.get("is_plural")), actor=actor)
                changed = (
                    False
                    if (made and is_base)
                    else _apply_record(
                        session, key, rec, locale, is_base, actor, notes=notes, skipped=skipped
                    )
                )
                if not made:
                    report["updated" if changed else "unchanged"] += 1
            except KeyServiceError as exc:
                report["errors"].append({"key": name, "context": context, "error": str(exc)})
            except Exception as exc:
                report["errors"].append(
                    {
                        "key": name,
                        "context": context,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        if mode == MODE_CLEANUP:
            failed = {(e["key"], e.get("context") or "") for e in report["errors"]}
            sweep = sorted(namespaces_seen) if from_key else [ns_default]
            for ns in sweep:
                for key in keys_svc.iter_keys(session, proj.id, namespace=ns):
                    if (ns, key.name, key.context or "") in touched:
                        continue
                    if (key.name, key.context or "") in failed:
                        continue  # belt and braces on the B1 class of bug
                    if key.is_hidden and not include_hidden:
                        continue  # #540: never deleted (or counted) by a caller below manager
                    if key.is_archived:
                        # ARCHIVED means "retired from the product, kept for reference"
                        # (review B13). A build file naturally stops mentioning it, so a
                        # cleanup sweep would delete exactly the keys an archive was
                        # created to preserve.
                        continue
                    # audit_event=False: the ONE `keys.import` row below carries the
                    # deleted COUNT, and 200 per-key rows would bury it (review B9).
                    keys_svc.delete_key(session, key, actor=actor, audit_event=False)
                    report["deleted"] += 1
    # AUDITED (review B9): an import is the one key operation that can rewrite a whole
    # catalogue in a single call, and in cleanup mode it can retire keys wholesale.
    # The row names the MODE and what it did, so "who replaced the catalogue" is
    # answerable without reading the change feed key by key.
    keys_svc._audit(
        "keys.import",
        proj.id,
        f"project {proj.id} {filename}",
        actor=actor,
        ip=ip,
        detail="mode=%s fmt=%s locale=%s namespace=%s: %d created, %d updated, "  # noqa: UP031
        "%d unchanged, %d deleted, %d error(s)"
        % (
            mode,
            fmt,
            locale,
            report["namespace"],
            report["created"],
            report["updated"],
            report["unchanged"],
            report["deleted"],
            len(report["errors"]),
        ),
    )
    return report
