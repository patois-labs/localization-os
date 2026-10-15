"""keys: second pass part 3 (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    Project,
    Segment,
    SegStatus,
    StringKey,
)
from app.services.keys_common import (
    _audit,
    _key_target,
    _modified_after_token,
    _next_order_index,
    _source_for,
    _utcnow,
    active_locales,
    actor_scope,
    content_hash,
    current_actor,
    import_memo_of,
)
from app.services.keys_hub import (
    CLDR_CATEGORIES,
    DEFAULT_DELTA_LIMIT,
    MAX_DELTA_ENTRIES,
    MAX_DELTA_LIMIT,
    ORIGIN_HUMAN,
    ORIGINS,
    DuplicateKeyError,
    KeyServiceError,
    KeyValueAwaitingReview,
    NoTargetLocalesError,
    UnknownLocaleError,
    UnknownPluralCategory,
    _coerce_char_limit,
    _log,
    _project,
    _value_order,
    autounverify_enabled,
    clear_source_changed_flag,
    internal_document,
    merge_source_changed_flag,
    plural_categories_for,
    retention_days,
    validate_namespace,
    values_for,
)
from app.services.keys_hub2 import (
    _categories_for_key,
    _guard,
    _move_value_rows,
    base_values,
    get_key,
    gettext_categories_for,
    page_keys,
    parse_modified_after_cursor,
)


def gettext_index_map(locale: str, plural_expr: str = "", num_plurals: int = 0) -> dict[str, int]:
    """The inverse of `gettext_categories_for`: CLDR category -> msgstr index."""
    cats = gettext_categories_for(locale, plural_expr, num_plurals)
    out: dict[str, int] = {}
    for idx, cat in enumerate(cats):
        out.setdefault(cat, idx)
    return out


def iter_keys(session: Session, project_id: int, *, batch=1000, **filters):
    """Yield id-ordered batches; raise ValueError unless batch is in 1..1000."""
    if not 1 <= batch <= 1000:
        raise ValueError("batch must be between 1 and 1000")
    cursor = 0
    while True:
        page = page_keys(session, project_id, cursor=cursor, limit=batch, **filters)
        yield from page["keys"]
        if not page["has_more"]:
            return
        cursor = page["next_cursor"]


EDIT_META_FIELDS = (
    "description",
    "context",
    "char_limit",
    "tags",
    "source_file",
    "is_hidden",
    "is_archived",
    "custom",
    "plural_arg_name",
)


_PLAIN_META_FIELDS = tuple(f for f in EDIT_META_FIELDS if f != "context")


EDIT_FIELDS = EDIT_META_FIELDS + ("name", "namespace", "is_plural", "force", "base")  # noqa: RUF005


#: The metadata fields `update_key` owns. `context` is in the list because callers
#: pass it here, but it is IDENTITY, not metadata -- see `_write_key_edit`.
#: The metadata fields that are NOT part of the identity tuple.
#: Everything ONE key edit may carry, across all five seams it can reach.
def _write_key_edit(
    session: Session,
    key,
    *,
    name: str,
    namespace: str,
    context: str,
    meta: dict | None = None,
    actor: str = "",
) -> bool:
    """THE ONE TRANSACTION a key's IDENTITY and metadata are written in. Returns True
    when anything actually changed.

    THE FINDING THIS EXISTS FOR (review B-6, re-review W2/W3). All three of `name`,
    `namespace` and `context` are IDENTITY (spec 2.1), and each used to have its own
    committing seam -- `rename_key`, `move_namespace`, `update_key`. `apply_key_edit`
    validated the FINAL tuple and then called them in sequence, so an edit walked
    through INTERMEDIATE tuples nobody had validated and each one committed on the way
    past. `PATCH {name: bbb, namespace: g2, description: ...}` on a key in `g1`, with
    `(g1, bbb)` already taken, therefore: committed the description, published an
    `updated` entry, then refused with a 400 naming `g1` -- a namespace the caller had
    not asked for. The context variant did not even get a 400: `update_key` set
    `context` first and the partial unique index answered with a raw IntegrityError,
    i.e. HTTP 500. And `POST /keys/{id}/meta` reached the same 500 with a plain
    context edit, no rename involved at all.

    VALIDATING THE INTERMEDIATE ORDERS IS PROVABLY INSUFFICIENT (the re-reviewer
    checked): with three mutable identity fields there are orders in which EVERY
    intermediate tuple collides, so an edit that is legal end-to-end has no legal
    path. The only correct shape is not to have intermediate states at all -- write
    the whole tuple plus the metadata in ONE transaction, ONE commit, ONE `updated`
    entry.

    Two refusals, deliberately distinct:
      * the DETERMINISTIC one -- `get_key` finds a live key on the resulting tuple --
        is raised BEFORE anything is written, naming the tuple THE CALLER ASKED FOR;
      * the RACE -- a concurrent writer takes it between the check and the commit --
        is caught as `IntegrityError` off the partial unique index and re-raised as
        the SAME `DuplicateKeyError`, after a rollback that unwinds the moved value
        rows, the field writes and the changelog row together. `create_key` already
        uses exactly this pairing (review A8).

    The one thing that outlives a rollback is a namespace document created for the
    move (`internal_document` commits its own row). That is deliberate and harmless:
    an empty internal document is what a key project lazily grows anyway, it is
    created BEFORE any of this key's state is touched, and `translatable_documents`
    would simply find nothing in it."""
    meta = dict(meta or {})
    bad = [f for f in meta if f not in _PLAIN_META_FIELDS]
    if bad:
        raise KeyServiceError(
            "cannot update {} here (allowed: {}).".format(
                ", ".join(sorted(bad)), ", ".join(EDIT_META_FIELDS)
            )
        )
    name = (name or "").strip()
    if not name:
        raise KeyServiceError("a key name is required.")
    namespace = validate_namespace(namespace)
    context = (context or "").strip()

    old = (key.name, key.namespace or "", key.context or "")
    identity_changed = (name, namespace, context) != old
    if identity_changed:
        clash = get_key(session, key.project_id, name, namespace, context)
        if clash is not None and clash.id != key.id:
            raise DuplicateKeyError(namespace, name, context)

    # char_limit is validated HERE, at the one seam every metadata edit reaches (the
    # API metadata PATCH calls `update_key` directly, not `apply_key_edit`), so no
    # surface can store a 0 / negative / non-integer / bool the write-seam predicate
    # then has to defend against. `_coerce_char_limit` is idempotent on a valid int, so
    # `apply_key_edit` coercing first (keys.py) is harmless -- never a double-coercion.
    if "char_limit" in meta:
        meta["char_limit"] = _coerce_char_limit(meta["char_limit"])

    # The metadata delta, with `update_key`'s own normalization preserved exactly.
    pending = {}
    for field, value in meta.items():
        if value is None and field in ("description", "source_file"):
            continue
        if field in ("is_hidden", "is_archived"):
            value = bool(value)
        if getattr(key, field) != value:
            pending[field] = value
    if not identity_changed and not pending:
        return False  # a no-op save must not pollute the delta feed

    # A namespace move re-parks the value rows. The destination document is resolved
    # (and lazily created) BEFORE the transaction below, so its own commit cannot
    # split this one; `_move_value_rows` only flushes, so its writes roll back with
    # everything else.
    if namespace != (key.namespace or ""):
        target_doc = internal_document(session, _project(session, key.project_id), namespace)
        _move_value_rows(session, key, target_doc)

    key.name, key.namespace, key.context = name, namespace, context
    for field, value in pending.items():
        setattr(key, field, value)
    key.updated_at = _utcnow()
    session.add(key)
    _log(session, key, entity=KeyEntity.KEY, op=KeyOp.UPDATED, actor=actor or None)
    try:
        session.commit()
    except IntegrityError as exc:
        # The partial unique index is the real guarantee; the `get_key` check above is
        # a courtesy two concurrent editors can both pass. Same named refusal, never a
        # raw 500 -- and the rollback leaves NOTHING half-applied.
        session.rollback()
        raise DuplicateKeyError(namespace, name, context) from exc
    session.refresh(key)
    return True


def update_key(session: Session, key, *, actor: str = "", **fields) -> StringKey:
    """Update key METADATA (description, context, tags, char_limit, hidden/archived,
    plural_arg_name, source_file, custom). One changelog row per call, and only when
    something really changed -- a no-op save must not pollute the delta feed.

    `is_plural` is NOT settable here: promotion/demotion materializes or destroys
    value rows and has its own refusal, so it goes through `set_plural`. `name` and
    `namespace` move a key's IDENTITY and go through `rename_key` / `move_namespace`.

    `context` IS accepted here (the web grid's metadata drawer edits it alongside the
    description) and it IS identity, so this delegates to `_write_key_edit` rather
    than assigning it: a context edit onto a taken tuple used to reach the partial
    unique index and answer HTTP 500 (re-review W3)."""
    bad = [f for f in fields if f not in EDIT_META_FIELDS]
    if bad:
        raise KeyServiceError(
            "cannot update {} here (allowed: {}).".format(
                ", ".join(sorted(bad)), ", ".join(EDIT_META_FIELDS)
            )
        )
    new_ctx = key.context or ""
    if fields.get("context") is not None:
        new_ctx = str(fields["context"]).strip()
    _write_key_edit(
        session,
        key,
        name=key.name,
        namespace=key.namespace or "",
        context=new_ctx,
        meta={f: v for f, v in fields.items() if f != "context"},
        actor=actor,
    )
    return key


def rename_key(session: Session, key, name: str, *, actor: str = "") -> StringKey:
    """Rename a key IN PLACE (same identity row, new name). Refuses a collision with a
    live key exactly as `create_key` does.

    A thin caller of `_write_key_edit` since the fix round 2 (W2): the one-field verbs
    and the whole-edit seam must not be able to disagree about what a legal identity
    is, or about whether a refusal leaves anything behind."""
    name = (name or "").strip()
    if not name:
        raise KeyServiceError("a key name is required.")
    if name == key.name:
        return key
    _write_key_edit(
        session,
        key,
        name=name,
        namespace=key.namespace or "",
        context=key.context or "",
        actor=actor,
    )
    return key


def move_namespace(session: Session, key, namespace: str, *, actor: str = "") -> StringKey:
    """Move a key to another namespace: its value rows move to that namespace's
    internal document (appended there), and the key gets an `updated` changelog row.
    A rare path -- correctness over speed (spec 3).

    The move itself lives in `_write_key_edit` (fix round 2, W2), which keeps
    `_move_value_rows`'s ordering discipline -- read the append slot BEFORE dirtying
    the row, because assigning `document_id` first autoflushes a half-updated row into
    the (document, locale, order_index) unique index -- and now also rolls the moved
    rows back with the rest of the edit when the commit loses a race."""
    namespace = validate_namespace(namespace)
    if namespace == (key.namespace or ""):
        return key
    _write_key_edit(
        session, key, name=key.name, namespace=namespace, context=key.context or "", actor=actor
    )
    return key


def restore_key(session: Session, key, *, actor: str = "", ip: str = "") -> StringKey:
    """Undo a soft delete. Refuses when the identity tuple has been taken by a LIVE
    key in the meantime (restoring would violate the identity rule).

    The value rows come back WITH the key and each gets its own `value`/`created`
    entry (re-review R10). The rows were never deleted -- `delete_key` leaves them on
    disk precisely so this can work -- but from the FEED's point of view they ceased to
    exist the moment the key was tombstoned: a tombstoned key emits no value entries at
    all (review A9), so a consumer that rebuilds state from the feed had already
    dropped them. Announcing only the key would hand that consumer a live key with no
    values and no event that could ever fill them in. The feed stays self-contained."""
    if key.deleted_at is None:
        return key
    clash = get_key(session, key.project_id, key.name, key.namespace, key.context)
    if clash is not None and clash.id != key.id:
        raise DuplicateKeyError(key.namespace, key.name, key.context)
    proj = session.get(Project, key.project_id)
    # LOCALES ADDED WHILE THE KEY WAS TOMBSTONED HAVE NO ROWS (review B15). Nothing
    # materializes for a deleted key -- `_materialize_key_values` walks `list_keys`,
    # which excludes it -- so a restore used to hand back a key with a gap: the grid
    # rendered a dash with no input for the new locale, and only a base edit could ever
    # fill it. Materialized HERE, while the key is still tombstoned, so the change-feed
    # listener stays silent about the new rows and the explicit `value`/`created` loop
    # below remains the single announcement for every row the consumer gets back.
    if active_locales(session, proj.id):
        ensure_value_rows(session, key)
    key.deleted_at, key.updated_at = None, _utcnow()
    session.add(key)
    # Un-park: the rows go back to their namespace document, so a restored key is
    # translatable again (and `document_has_no_content` sees it) exactly as before.
    # Done while the key is LIVE in the session, but a move changes no watched field,
    # so the `value`/`created` entries below stay the only ones the feed gets.
    _move_value_rows(session, key, internal_document(session, proj, key.namespace))
    _log(session, key, entity=KeyEntity.KEY, op=KeyOp.CREATED, actor=actor or None)
    for seg in values_for(session, key):
        _log(
            session,
            key,
            entity=KeyEntity.VALUE,
            op=KeyOp.CREATED,
            target_locale=seg.target_locale,
            plural_category=seg.plural_category or "",
            actor=actor or None,
        )
    session.commit()
    session.refresh(key)
    _audit(
        "keys.restore",
        key.project_id,
        _key_target(key),
        actor=actor,
        ip=ip or "",
        detail="restored from the trash document with %d value row(s)"  # noqa: UP031
        % len(values_for(session, key)),
    )
    return key


# --- value rows --------------------------------------------------------------
def ensure_value_rows(
    session: Session, key, *, base: dict | None = None, actor: str = "", fill_missing: bool = True
) -> list[Segment]:
    return _ensure_value_rows(session, key, base=base, fill_missing=fill_missing)[0]


def _ensure_value_rows(
    session: Session, key, *, base: dict | None = None, fill_missing: bool = True
) -> tuple[list[Segment], dict, bool]:
    """Materialize the value rows `key` should have: one per (active target locale x
    required CLDR form). Idempotent, and the ONLY place a value row is born -- adding
    a locale later and re-calling this fills the gap without disturbing existing rows.

    `fill_missing=False` is `_source_for`'s no-fallback mode: a form the explicit
    `base` map does not name is born with an EMPTY source rather than a copy of
    `other` (live-QA K5).
    """
    proj = _project(session, key.project_id)
    locales = active_locales(session, proj.id)
    if not locales:
        raise NoTargetLocalesError(proj.id)
    base = base if base is not None else base_values(session, key)
    doc = internal_document(session, proj, key.namespace)
    memo = import_memo_of(session)
    if memo is not None and key.id in memo.get("fresh_keys", ()):
        # #585: a key this bulk import created a moment ago has no value rows yet.
        existing: dict = {}
    else:
        existing = {(s.target_locale, s.plural_category or ""): s for s in values_for(session, key)}
    made: list[Segment] = []
    raced = False
    #: #585: the append position is ONE MAX per locale per call, then counted up in
    #: memory. It was one MAX query plus one SAVEPOINT and flush per value row.
    tops: dict[str, int] = {}
    wanted: list[Segment] = []
    for locale in locales:
        for cat in _categories_for_key(key, locale):
            if (locale, cat or "") in existing:
                continue
            if locale not in tops:
                tops[locale] = (
                    memo[("order", doc.id, locale)]
                    if memo is not None and ("order", doc.id, locale) in memo
                    else _next_order_index(session, doc.id, locale)
                )
            wanted.append(
                Segment(
                    document_id=doc.id,
                    target_locale=locale,
                    order_index=tops[locale],
                    source_text=_source_for(base, cat, fill_missing),
                    target_text="",
                    status=SegStatus.NEW,
                    key_id=key.id,
                    plural_category=cat,
                )
            )
            tops[locale] += 1
    if wanted:
        try:
            # ONE SAVEPOINT for the whole batch, not the caller's transaction (re-review
            # R6): it must undo THESE INSERTS AND NOTHING ELSE. `session.rollback()` here
            # threw away the CALLER's uncommitted work too, and this function's busiest
            # caller is `set_plural`'s promote, which flushes `is_plural = True` and the
            # `other` re-labelling and THEN materializes the siblings.
            with session.begin_nested():
                session.add_all(wanted)
                session.flush()
            made.extend(wanted)
            if memo is not None:
                for locale, nxt in tops.items():
                    memo[("order", doc.id, locale)] = nxt
        except IntegrityError:
            # `ux_segment_key_locale_form` can refuse a row: a concurrent writer
            # materialized this exact (key, locale, form) between our read and our flush
            # (or took the order slot we counted up to). Materialization is IDEMPOTENT by
            # contract, so accept the other writer's rows and redo the batch ONE ROW AT A
            # TIME, exactly as before #585, so only the colliding rows are skipped.
            raced = True
            if memo is not None:
                for k in [k for k in memo if k[:2] == ("order", doc.id)]:
                    del memo[k]
            for locale in locales:
                for cat in _categories_for_key(key, locale):
                    if (locale, cat or "") in existing:
                        continue
                    seg = Segment(
                        document_id=doc.id,
                        target_locale=locale,
                        order_index=_next_order_index(session, doc.id, locale),
                        source_text=_source_for(base, cat, fill_missing),
                        target_text="",
                        status=SegStatus.NEW,
                        key_id=key.id,
                        plural_category=cat,
                    )
                    try:
                        with session.begin_nested():
                            session.add(seg)
                            session.flush()
                    except IntegrityError:
                        continue
                    made.append(seg)
    if made:
        session.commit()
    return made, existing, raced


def _rows_complete(session: Session, key, rows: list) -> bool:
    """True when `rows` already holds every (active locale x required form) value row
    of `key`, i.e. `ensure_value_rows` would make nothing."""
    have = {(s.target_locale, s.plural_category or "") for s in rows}
    locales = active_locales(session, key.project_id)
    if not locales:
        return False  # ensure_value_rows must run to raise NoTargetLocalesError
    for locale in locales:
        for cat in _categories_for_key(key, locale):
            if (locale, cat or "") not in have:
                return False
    return True


def set_base_value(
    session: Session, key, value, *, actor: str = "", fill_missing: bool = True
) -> list[Segment]:
    """Write the key's BASE (source-locale) value across its value rows' `source_text`,
    with AUTO-UNVERIFY (spec 3, the maintainer's locked call #3).

    `fill_missing=False` writes ONLY the plural forms the `value` map actually names
    and leaves every other form exactly as it was (live-QA K5). `create_key` passes it
    for an explicit plural map -- see `_source_for`.

    `value` is a string for a simple key, or a {CLDR category: text} mapping for a
    plural one. When the base text a row carries ACTUALLY changes and that row already
    holds a translation, the row flips to `needs_review` and gains a `source_changed`
    qa_flag -- so a source edit can never silently ship a stale translation. An
    unchanged value is a complete no-op (no status change, no changelog row): a
    re-import of an identical file must not flag an entire project for review."""
    base = (
        {"": str(value or "")}
        if not isinstance(value, dict)
        else {(k or ""): str(v or "") for k, v in value.items()}
    )
    with actor_scope(actor or current_actor()):
        made_rows, had, raced = _ensure_value_rows(
            session, key, base=base, fill_missing=fill_missing
        )
        auto = autounverify_enabled(session.get(Project, key.project_id))
        if import_memo_of(session) is not None and not raced:
            # #585: in a bulk import the rows ensure just read plus the ones it just made
            # ARE the key's rows (the session does not expire on commit), so the second
            # values_for SELECT is skipped.
            rows = sorted([*had.values(), *made_rows], key=_value_order)
        else:
            rows = values_for(session, key)
        flagged, changed = [], False
        for seg in rows:
            if not fill_missing and (seg.plural_category or "") not in base:
                # A form the caller did not name is not a form the caller cleared.
                continue
            new_source = _source_for(base, seg.plural_category, fill_missing)
            if (seg.source_text or "") == new_source:
                continue
            seg.source_text, changed = new_source, True
            if auto and (seg.target_text or "").strip():
                seg.status = SegStatus.NEEDS_REVIEW
                seg.qa_flags = merge_source_changed_flag(seg.qa_flags)
                flagged.append(seg)
            session.add(seg)
        if changed:
            # ONE key-level row for the base change itself; the per-(locale, form)
            # `value` rows are appended by the change-feed listener during this flush.
            _log(session, key, entity=KeyEntity.KEY, op=KeyOp.UPDATED, actor=actor or None)
            key.updated_at = _utcnow()
            session.add(key)
            session.commit()
    return flagged


def set_value(
    session: Session,
    key,
    locale: str,
    text: str,
    *,
    origin: str,
    plural_category: str | None = None,
    actor: str = "",
    status: str | None = None,
    refusal_sink: list | None = None,
) -> Segment:
    """THE single-value write seam shared by grid edits, imports, the API and the CLI.

    `origin` is KEYWORD-REQUIRED and validated (review A7). It used to default to
    ORIGIN_HUMAN, i.e. to the UNGATED posture: a future caller in this growing module
    that simply forgot it would have written unvetted machine text straight into a
    value row, and the refusal tripwire's allowlist entry for keys.py is whole-file, so
    nothing would have noticed. Now forgetting it is a TypeError and an unknown value
    is a named refusal -- the safe direction is the one you cannot omit.

    `origin` decides which of the TWO EXISTING write postures this call takes, and it
    is the only interesting thing in this function:

      * ORIGIN_HUMAN (the default -- the key grid, the CAT editor, `keys set`): a
        direct assignment with the CAT editor's exact semantics (blank clears to
        `new`, otherwise `edited`). Human text is content and is never gated, which
        is why `app/web/documents.py` is on the refusal tripwire's allowlist for the
        identical write.
      * ANY OTHER origin -- an imported file, an /api write, machine output -- goes
        through `quality.commit_mt_output`, THE sanctioned writer. So a .po whose
        `msgstr` is an LLM hedge ("I'm sorry, I can't...") is QUARANTINED on the way
        in: not written, routed to needs_review, flagged with its provenance, and made
        TM-ineligible -- byte-identically to the same text arriving on a document.

    That is what "the refusal perimeter applies exactly as on document paths" has to
    mean in code. Nothing here re-implements the perimeter; it routes into it.

    An explicit `status` (a caller that knows better) still wins afterwards.

    `refusal_sink` (F4, adversarial review): an optional list a machine caller (the /api
    write) passes to LEARN that the seam REFUSED this write instead of silently reporting
    ok:true. When the seam withholds the write it appends a reason string -- "status_protected"
    (a human-approved/edited value the import/api may not overwrite) or "locked" (a hard TM
    lock) -- so the route can answer 409 with a named reason. Left None (every non-/api caller)
    -> byte-identical; the human/direct-writer branch never refuses, so it never appends."""
    if key.deleted_at is not None:
        raise KeyServiceError("this key is deleted; restore it before writing values.")
    if origin not in ORIGINS:
        raise KeyServiceError(
            "unknown value origin {!r} (expected one of: {}).".format(origin, ", ".join(ORIGINS))
        )
    cat = plural_category or None
    if key.is_plural:
        allowed = plural_categories_for(locale)
        cat = cat or "other"
        if cat not in allowed:
            raise UnknownPluralCategory(cat, locale, allowed)
    elif cat is not None:
        raise KeyServiceError(f"{key.name!r} is not a plural key; it has no plural forms.")
    with actor_scope(actor or current_actor()):
        memo = import_memo_of(session)
        rows_all = None
        if memo is not None:
            # #585: a bulk import reads the key's rows ONCE and skips the (idempotent)
            # materialization when every active locale x form already has its row, which is
            # nearly always; the old path read the rows three more times to learn that.
            rows_all = values_for(session, key)
            if not _rows_complete(session, key, rows_all):
                ensure_value_rows(session, key, actor=actor)
                rows_all = None
        else:
            ensure_value_rows(session, key, actor=actor)
        seg = next(
            (
                s
                for s in (
                    [r for r in rows_all if r.target_locale == locale]
                    if rows_all is not None
                    else values_for(session, key, locale)
                )
                if (s.plural_category or "") == (cat or "")
            ),
            None,
        )
        if seg is None:
            raise KeyServiceError(f"{locale} is not an active target locale of this project.")
        text = text or ""
        if (seg.target_text or "") == text and (status is None or seg.status == status):
            # NOTHING TO DO IS NOTHING TO DO. The `status is None` half is the original
            # no-op; the second half is what makes an export -> import round trip a
            # FIXED POINT (live-QA K1). A .po transport file re-states a needs-review
            # value AND its `#, fuzzy` flag, so the caller passes an explicit status
            # equal to the one already stored -- and running that through
            # `commit_mt_output` again could re-flag, re-quarantine or re-write text
            # that nobody changed. A restatement is not an edit.
            #
            # ONE THING THIS SKIPS, for the next caller (fix-round F11): a HUMAN write
            # of unchanged text no longer clears the `source_changed` qa_flag, because
            # it never reaches the branch below that does. Today only
            # `key_files._apply_record` passes a `status`, and it is an IMPORT (which
            # must not confirm anything on a human's behalf anyway), so nothing
            # user-facing changes. A future caller that passes an explicit status for a
            # HUMAN confirmation would find its confirmation silently dropped -- give
            # this function an explicit `confirm=` argument at that point rather than
            # widening the early return further.
            return seg
        # #617 item 4 (gate): a run parked on a human_review step holds this locale; refuse
        # every origin (grid, CLI, /api, MCP, import) rather than write what the resume will
        # overwrite. Memoised per bulk import (one lookup per document+locale).
        _held_key = ("awaiting_review", seg.document_id, seg.target_locale)
        if memo is not None and _held_key in memo:
            _held = memo[_held_key]
        else:
            from app.services import segment_review

            try:
                segment_review.refuse_if_awaiting_review(
                    session, seg.document_id, seg.target_locale
                )
                _held = None
            except segment_review.AwaitingReviewError as exc:
                _held = (str(exc), exc.run_id)
            if memo is not None:
                memo[_held_key] = _held
        if _held is not None:
            raise KeyValueAwaitingReview(*_held)
        _prior_target = seg.target_text or ""
        if origin == ORIGIN_HUMAN or (
            not text.strip()
            and not seg.lock_band
            and seg.status not in (SegStatus.APPROVED, SegStatus.EDITED)
        ):
            # Human content, or a CLEAR of an UNLOCKED, NON-finalized segment (an empty value
            # from a non-human origin is not machine output and has nothing to gate) --
            # `segment_save`'s semantics, verbatim. TWO blank-non-human cases DELIBERATELY do
            # NOT come here and fall to the seam branch instead, so it refuses them and leaves
            # the row byte-untouched: (1) a LOCKED segment (BLOCKER -- a hard lock must REFUSE
            # rather than blank a locked translation while the lock stands, which left the row
            # permanently unfillable; a soft lock is committed + cleared by 7a); and (2) an
            # APPROVED/EDITED segment (F3, human-work protection -- a blank machine write must
            # not DESTROY a human sign-off by clearing it to `new`; the seam's status gate
            # refuses it, exactly as it refuses a non-blank machine overwrite).
            seg.target_text = text
            seg.status = status or (SegStatus.EDITED if text.strip() else SegStatus.NEW)
            # Clearing `source_changed` is a CONFIRMATION event: a human looked at the
            # new base text and wrote this value. A BLANK non-human write is neither --
            # an import whose msgstr is empty for a key whose base just changed must
            # leave the flag standing, or auto-unverify silently un-flags exactly the
            # values it was invented to protect (review A7, related LOW).
            if origin == ORIGIN_HUMAN:
                seg.qa_flags = clear_source_changed_flag(seg.qa_flags)
                # item 5a (PROPOSAL-tm-matching §5): a HUMAN edit of a locked value WINS
                # -- clear the lock + provenance (+ audit). Gated on an ACTUAL text change
                # (MED-10) so a save-without-edit never destroys a lock with an audit row
                # claiming an edit that did not happen.
                if text != _prior_target:
                    from app.services import tm_lock

                    tm_lock.clear_lock_on_human_edit(session, seg, actor=actor or "")
            # K8 (spec §2a) — the human/machine ASYMMETRY. A linguist typing into the slot
            # gets the SAME char_limit flag the machine path sets (so the grid badge and the
            # export report still carry the fact), but the status STAYS `edited`: quality.py
            # documents that human text is never gated, and bouncing a linguist to
            # needs_review as they type reads as the tool fighting them. The machine branch
            # below routes to review, because on THAT path nobody has looked at it yet. This
            # covers a CLEAR too (empty text -> "" reason -> the flag is dropped).
            from app.services import quality as _quality

            # `key` is the segment's own StringKey, already in scope — use its limit
            # directly instead of `segment_char_limit` re-fetching it by `seg.key_id` (one
            # session.get per human save). This IS `segment_char_limit`'s resolution today
            # (it reads `key.char_limit` only); when the phase-2 per-unit override lands,
            # resolve it in `segment_char_limit` and route this seam back through it.
            _cl_limit = key.char_limit
            _cl_reason = _quality.char_limit_violation(text, _cl_limit)
            if _cl_reason:
                seg.qa_flags = _quality.merge_char_limit_flags(
                    seg.qa_flags,
                    _cl_reason,
                    _quality.char_count(text),
                    _quality.coerce_char_limit(_cl_limit),
                )
            else:
                seg.qa_flags = _quality.clear_char_limit_flags(seg.qa_flags)
        else:
            from app.services import quality

            outcome = quality.commit_mt_output(
                session, seg, text, source=origin, set_status=(status is None)
            )
            # MED-8: a hard-lock refusal leaves the locked value UNTOUCHED, so the caller's
            # explicit `status` must NOT be stamped onto it (that would relabel a value the
            # import did not write). The import layer reports the refusal with its own kind
            # (see key_files._apply_record), never as an `updated`. STATUS PROTECTION: an import
            # over a human-approved/edited value is refused the SAME way -- the seam left the
            # row byte-untouched, so an explicit `status` must not relabel it either.
            if status is not None and not outcome.lock_refused and not outcome.status_protected:
                seg.status = status
            if not outcome.flagged:
                seg.qa_flags = clear_source_changed_flag(seg.qa_flags)
            # F4: surface a withheld machine write to the caller (the /api route -> 409),
            # so a refused write is never reported as ok:true. status_protected first (the
            # human-work protection); a hard-lock refusal shares the same silent shape and is
            # surfaced here too. A committed write appends nothing.
            if refusal_sink is not None:
                if outcome.status_protected:
                    refusal_sink.append("status_protected")
                elif outcome.lock_refused:
                    refusal_sink.append("locked")
        session.add(seg)
        session.commit()
        if memo is None:  # #585: no reload SELECT inside a bulk import (no expiry there)
            session.refresh(seg)
    return seg


def changes_modified_after(
    session: Session,
    project_id: int,
    modified_after,
    limit: int = DEFAULT_DELTA_LIMIT,
    *,
    after_id=None,
    locale: str | None = None,
    org_id=None,
    customer_id=None,
    include_hidden: bool = True,
) -> dict:
    """THE delta read (spec 5), MODIFIED-AFTER mode (AP-1). Returns the CURRENT-STATE
    value rows of keys whose CONTENT (or status, or source text) changed after
    `modified_after`, i.e. keys with `translations_modified_at > T` (the
    MAX-over-child-value mtime, bumped by every value/source/status write). Each emitted
    entry is one live value segment (locale + plural form), ENRICHED with `text` +
    `content_hash`, so a consumer pulls the changed strings in one call.

    WHAT THIS MODE DOES AND DOES NOT GUARANTEE (do not conflate it with `since`):
      * It reports CURRENT STATE only: a value that changed twice since T appears once,
        carrying its latest text. There is no per-op history here.
      * It NEVER reports DELETIONS: a removed key/value simply stops appearing (its
        content mtime cannot advance once the row is gone). A consumer that must learn
        about removals uses the `since=` changelog mode, whose tombstones are the only
        deletion signal. It also does NOT report metadata-only key edits (tags,
        description bump `updated_at` but not `translations_modified_at`); those live
        in the changelog as key `updated` ops.
      * It emits a CLEARED value (`text:""`, empty-string hash) so a consumer RETIRES
        the string, but it EXCLUDES a never-translated PLACEHOLDER (gate NEW-1/L3). Both
        are empty target_text with status NEW in the schema (a human clear resets status
        to NEW too, so status cannot tell them apart) -- so the distinguisher is the
        changelog: a value with a `value/updated` op was written at least once, an empty
        one of those is a CLEAR; a placeholder carries only its materialization
        `created` op. (Retention edge: once a cleared value's `updated` op prunes past
        the 90-day window it reads as a placeholder again, but it is empty either way, so
        a live consumer that already retired it is unaffected; the `since` mode is the
        lossless record.)
      * `modified_after` compares against a NAIVE-UTC column; the caller's timestamp is
        normalised to UTC by `parse_modified_after` (a naive value is assumed UTC).
      * `locale`, if given, is VALIDATED against the project's active target locales
        (UnknownLocaleError -> 400) so a typo cannot masquerade as "nothing changed".
      * If `modified_after` is older than the CHANGE-HISTORY WINDOW (`now -
        retention_days`, the horizon past which `value/updated` ops are pruned), the
        response is `resync_required: True` with no entries -- because a value cleared
        before that horizon can no longer be told from a placeholder, so the feed refuses
        rather than silently dropping it. Re-bootstrap with `since=0` (or a newer
        `modified_after`). This mirrors the `since`-mode pruned-floor signal;
        `retention_days=0` disables pruning and this never fires.
      * SINGLE-WRITER assumption for perfect completeness: the mtime is stamped in the
        same transaction as the write, but on a store without a single-writer lock
        (Postgres under concurrency) a transaction that stamps the column just before a
        slow commit could in principle be straddled by a poll at exactly that instant.
        The `since` changelog mode (whose cursor is a committed autoincrement id) is the
        strictly lossless alternative; `modified_after` is the wall-clock convenience.

    PAGING is KEYSET (gate M1/NEW-3), not offset and not a tie-drain: strict lexicographic
    order on `(translations_modified_at, id)`. The cursor is the pair
    `(modified_after, after_id)`; the predicate is
    `mtime > T OR (mtime == T AND id > after_id)`, so a re-poll resumes EXACTLY after the
    last key with no skip and no repeat, even across a microsecond shared by 10000 keys.
    To make that ATOMIC for a caller who threads only ONE field, `next_modified_after` is
    returned as the opaque token `ISO~id` whenever `has_more` (feed it straight back as
    `modified_after`); `next_after_id` still rides alongside for a caller who prefers the
    explicit param, and a plain ISO input stays valid for a human-authored first call.
    Bounded two ways: `limit` caps KEYS (clamped to MAX_DELTA_LIMIT), and
    MAX_DELTA_ENTRIES caps total ENTRIES -- the page stops at a KEY boundary when the next
    key's rows would breach the ceiling (always emitting at least one key so paging makes
    progress), `has_more=true`, and keyset resumes on the next poll.

    Isolation identical to `changes_since` (a cross-scope project 404s before any read)."""
    _guard(session, project_id, org_id, customer_id)
    # NEW-3: modified_after may be the opaque `ISO~id` token; split it. An explicit
    # after_id param wins over the token's id (both keep working).
    after, token_id = parse_modified_after_cursor(modified_after)
    # a READ: a language a connected project has stopped keeps its values readable
    from app.services.keys_common import readable_locales

    if locale is not None and locale not in readable_locales(session, project_id):
        raise UnknownLocaleError(locale, project_id)
    try:
        after_id = int(after_id) if after_id is not None else None
    except (TypeError, ValueError):
        after_id = None
    if after_id is None:
        after_id = token_id
    try:
        limit = int(limit or DEFAULT_DELTA_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_DELTA_LIMIT
    limit = max(1, min(limit, MAX_DELTA_LIMIT))

    # F-3: cleared-value detection (NEW-1) reads the changelog `value/updated` ops, which
    # are pruned after `retention_days`. So the feed can only PROMISE cleared-value
    # completeness back to `now - retention_days`: a value cleared before that horizon has
    # had its op pruned and would read as a placeholder (silently dropped). A T older than
    # the window therefore answers `resync_required` and refuses to serve, mirroring the
    # since-mode pruned floor -- rather than degrading silently. `retention_days()==0`
    # disables pruning, so history is unbounded and this never fires.
    window = retention_days()
    if window > 0:
        floor_time = _utcnow() - timedelta(days=window)
        if after < floor_time:
            return {
                "mode": "modified_after",
                "modified_after": after.isoformat(),
                "entries": [],
                "next_modified_after": after.isoformat(),
                "next_after_id": after_id,
                "has_more": False,
                "resync_required": True,
                "message": "modified_after %s is older than the %d-day change-history "  # noqa: UP031
                "window; re-bootstrap with since=0." % (after.isoformat(), window),
            }

    tmod = StringKey.translations_modified_at
    conds = [StringKey.project_id == project_id, StringKey.deleted_at.is_(None), tmod.is_not(None)]
    if not include_hidden:
        # #540: keys hidden from translators never ride this feed for a caller below
        # manager (they are filtered in SQL, so keyset paging stays exact).
        conds.append(StringKey.is_hidden.is_(False))
    # Keyset predicate: strictly-after the (mtime, id) cursor. With no after_id this is
    # the plain `mtime > T` first page; with one it also admits the same-mtime tail.
    if after_id is not None:
        conds.append(or_(tmod > after, and_(tmod == after, StringKey.id > after_id)))
    else:
        conds.append(tmod > after)
    keys = list(
        session.exec(
            select(StringKey).where(*conds).order_by(tmod, StringKey.id).limit(limit + 1)
        ).all()
    )
    key_has_more = len(keys) > limit
    keys = keys[:limit]

    key_ids = [k.id for k in keys]
    # NEW-1: which (key, locale, form) tuples on this page were WRITTEN at least once
    # (a `value/updated` changelog op)? An empty target on such a tuple is a CLEAR and
    # must emit; an empty target that is NOT here is a pristine placeholder and is
    # skipped. One batched query, bounded by the page's keys -- never N+1.
    written: set[tuple] = set()
    # F-1: hoist the value-row read OUT of the per-key loop -- one batched query for the
    # whole page (was `values_for` per key: ~500 SQL at the default limit, contradicting
    # this module's own de-N+1 intent that `_enrich_value_entries`/`written` already
    # follow). Group in Python and replicate `values_for`'s (locale, CLDR-form) order.
    segs_by_key: dict[int, list[Segment]] = {}
    if key_ids:
        for kid, loc, form in session.exec(
            select(
                KeyChangeLog.key_id, KeyChangeLog.target_locale, KeyChangeLog.plural_category
            ).where(
                KeyChangeLog.key_id.in_(key_ids),
                KeyChangeLog.entity == KeyEntity.VALUE,
                KeyChangeLog.op == KeyOp.UPDATED,
            )
        ).all():
            written.add((kid, loc, form or ""))
        seg_stmt = select(Segment).where(Segment.key_id.in_(key_ids))
        if locale is not None:
            seg_stmt = seg_stmt.where(Segment.target_locale == locale)
        for seg in session.exec(seg_stmt).all():
            segs_by_key.setdefault(seg.key_id, []).append(seg)
        for rows in segs_by_key.values():
            rows.sort(
                key=lambda s: (
                    s.target_locale,
                    CLDR_CATEGORIES.index(s.plural_category)
                    if s.plural_category in CLDR_CATEGORIES
                    else -1,
                )
            )

    entries: list[dict] = []
    last = None  # the last key fully accounted for (cursor source)
    entry_truncated = False
    for key in keys:
        segs = [
            s
            for s in segs_by_key.get(key.id, [])
            if (s.target_text or "") != ""
            or (key.id, s.target_locale, s.plural_category or "") in written
        ]
        # M2 ceiling: stop at a key boundary before breaching the entry cap, but always
        # let the FIRST key through so a single huge key cannot stall paging.
        if entries and len(entries) + len(segs) > MAX_DELTA_ENTRIES:
            entry_truncated = True
            break
        stamp = key.translations_modified_at.isoformat() if key.translations_modified_at else ""
        for seg in segs:
            text = seg.target_text or ""
            entries.append(
                {
                    "entity": KeyEntity.VALUE,
                    "op": KeyOp.UPDATED,
                    "key_id": key.id,
                    "namespace": key.namespace or "",
                    "name": key.name or "",
                    "target_locale": seg.target_locale,
                    "plural_category": seg.plural_category or "",
                    "changed_at": stamp,
                    "text": text,
                    "content_hash": content_hash(text),
                }
            )
        last = key  # even an all-empty key advances the cursor past it

    has_more = key_has_more or entry_truncated
    if last is not None:
        ts_iso = last.translations_modified_at.isoformat() if last.translations_modified_at else ""
        next_after_id = last.id
        # Token form while paging (atomic single-field resume); plain ISO once caught up.
        next_after = _modified_after_token(ts_iso, last.id) if has_more else ts_iso
    else:
        # No key was consumed (empty page): hold the cursor exactly where it was so a
        # re-poll is stable rather than silently advancing past unseen rows.
        next_after = (
            _modified_after_token(after.isoformat(), after_id)
            if after_id is not None
            else after.isoformat()
        )
        next_after_id = after_id
    return {
        "mode": "modified_after",
        "modified_after": after.isoformat(),
        "entries": entries,
        "next_modified_after": next_after,
        "next_after_id": next_after_id,
        "has_more": has_more,
        "resync_required": False,
    }
