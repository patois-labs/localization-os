"""keys: shared leaf helpers (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import cast, func, or_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session, select

from app.models import (
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    ProjectType,
    Segment,
    StringKey,
    TargetLocale,
)

log = logging.getLogger(__name__)


def is_key_project(project) -> bool:
    return getattr(project, "project_type", ProjectType.DOCUMENT) == ProjectType.KEY_BASED


GNU_PLURAL_FORMS = {
    "ar": (6, "(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5)"),
}


#: gettext's OWN canonical `Plural-Forms:` for locales where the GNU gettext manual's
#: standard expression and Babel's CLDR-derived one assign the SAME categories to
#: DIFFERENT indices. Only Arabic qualifies today: Babel's last two slots are
#: `n%100 in 0..2 -> 4` (CLDR `other`) and `else -> 5` (CLDR `many`), i.e. `many` and
#: `other` swapped relative to every ar.po in the wild. A catalog we write is
#: self-describing either way, but msgmerge against a GNU-header ar.po -- or a runtime
#: with the standard table compiled in -- would then read slot 4 as `many`, silently
#: swapping two translations. We therefore EXPORT the GNU order (import keeps using
#: the file's own header, which is authority for its own contents).
#: Source: GNU gettext manual, "Plural forms" (the ar entry), not our own derivation.
def gnu_plural_forms(locale: str):
    """`(num_plurals, plural_expr)` for a locale whose GNU-canonical header differs
    from Babel's, else None. Keyed on the LANGUAGE (ar, ar-EG, ar_SA all match)."""
    code = (locale or "").strip().lower().replace("_", "-")
    return GNU_PLURAL_FORMS.get(code) or GNU_PLURAL_FORMS.get(code.split("-")[0])


_actor = threading.local()


_ACTOR_ATTR = "_loc_key_actor"


_inheritance_installed = [False]


# --- actor scope (who the change feed attributes a write to) -----------------
#: Attribute stamped on a `threading.Thread` OBJECT at `start()` time, carrying the
#: actor that was active on the thread doing the starting. See `_inherit_actor`.
def _install_actor_inheritance() -> None:
    """Make `actor_scope` survive the thread hand-off EVERY workflow launcher performs.

    The problem (review A6): `actor_scope` is thread-local, but no real launcher runs
    the workflow on the thread that entered the scope. `orchestrator.start_run` drains
    the run on a dedicated daemon thread and `batch_runs` spawns one thread per locale,
    so the write-back the feed cares most about was attributed to "" in every web and
    batch launch -- while `keys.py` claimed the opposite. The claim, not the code, was
    the bug worth fixing.

    The fix has to work WITHOUT editing orchestrator.py (the serialization law), so it
    cannot be an argument threaded through the launcher. Instead `Thread.start` -- which
    runs on the PARENT thread, inside the scope -- is wrapped ONCE so it records the
    parent's actor on the child Thread OBJECT; `current_actor()` reads it back on the
    child. Consequences worth stating plainly:
      * it captures the actor at START time, so two concurrent runs launched by two
        users never see each other's actor (a process-wide "last actor" fallback would
        have misattributed them, which is worse than an empty actor);
      * it chains, so a batch dispatcher thread's members inherit the dispatcher's;
      * a thread started OUTSIDE any scope inherits "", exactly as before;
      * the only effect on stdlib behaviour is one attribute write per Thread.start.

    THE LIMIT, STATED PLAINLY (re-review R5). This covers RAW `Thread.start` ONLY. It
    does NOT cover THREAD POOLS, and cannot: a pool worker is started once, so it
    captures the actor that happened to be active at pool construction and then keeps
    it for every task it ever runs. A pre-spawned worker carries "", and a REUSED
    worker carries a STALE actor -- which in a multi-user process means attributing one
    user's write to another. That is worse than an empty actor, so treat it as a hard
    rule: **any Segment writer running on a pool (`ThreadPoolExecutor`, anyio's worker
    threads behind `run_in_threadpool`, a connector's fan-out) must pass `actor=`
    explicitly rather than rely on inheritance.**

    Today's real launchers are all safe by shape and were verified so: `start_run` and
    `batch_runs` use raw per-run threads created INSIDE the scope, and provider workers
    are session-free by contract, so nothing on a pool reaches a value row. Wave B's
    routes are the place this could first go wrong -- FastAPI runs a sync route handler
    on anyio's worker-thread POOL -- so a route that writes key values takes the actor
    from its request identity and passes it down, never from `current_actor()` on a
    thread it did not start.

    Armed lazily by `actor_scope.__init__` (never at import: see the note under
    `current_actor`) and idempotent under re-import.
    """
    if _inheritance_installed[0]:
        return
    _inheritance_installed[0] = True
    real_start = threading.Thread.start

    def start(self, *args, **kwargs):
        if not hasattr(self, _ACTOR_ATTR):
            try:  # noqa: SIM105
                setattr(self, _ACTOR_ATTR, current_actor())
            # Why: Thread.start must never fail over actor inheritance; no logging in here.
            except Exception:  # noqa: S110
                pass
        return real_start(self, *args, **kwargs)

    start.__doc__ = real_start.__doc__
    start._loc_key_wrapped = True  # re-import / re-exec idempotence
    if not getattr(threading.Thread.start, "_loc_key_wrapped", False):
        threading.Thread.start = start


class actor_scope:
    """`with keys.actor_scope("alice@example.com"):` -- stamps KeyChangeLog.actor for
    every value change flushed inside the block, including changes made by code that
    knows nothing about keys (the orchestrator's write-back), and including changes
    made on a THREAD THE BLOCK STARTED (see `_install_actor_inheritance`): every
    real workflow launcher hands the run to a daemon thread, so without inheritance
    the seam would have been a promise the code did not keep.

    Thread-local otherwise, so two concurrent runs never inherit each other's actor.

    INHERITANCE STOPS AT THREAD POOLS (re-review R5): a pooled worker was started
    before this scope existed and keeps whatever actor it was born with, so a writer
    running on a pool must be handed `actor=` explicitly. Every seam in this module
    takes one for exactly that reason. See `_install_actor_inheritance` for the full
    statement of the limit."""

    def __init__(self, actor: str):
        self.actor, self._prev = actor or "", None
        _install_actor_inheritance()

    def __enter__(self):
        self._prev = current_actor()
        _actor.value = self.actor
        return self

    def __exit__(self, *exc):
        _actor.value = self._prev
        return False


def current_actor() -> str:
    """The actor for THIS thread: its own `actor_scope` if it entered one, otherwise
    the actor inherited from the thread that STARTED it (and so on up the chain).

    On a POOL worker the "thread that started it" is whoever built the pool, which is
    not the caller and may not even be the same user -- so this returns "" or a stale
    name there. Pool-based writers pass `actor=` (re-review R5)."""
    own = getattr(_actor, "value", None)
    if own is not None:
        return own or ""
    try:
        return getattr(threading.current_thread(), _ACTOR_ATTR, "") or ""
    except Exception:
        return ""


def _mock_fallback_workflow(session: Session, workflow_ids) -> bool:
    """True if ANY llm_agent step across these workflows would resolve to a NON-LIVE
    REAL provider (the mock-409 law, spec section 5). An EXPLICIT provider named 'mock'
    is the allowed deterministic dev engine; only a real provider that is not live
    (`provider_is_live` False, e.g. the DEFAULT_PROVIDER with no key) is the silent
    fallback we refuse before spending a run."""
    from app import config
    from app.models import StepType, WorkflowStep
    from app.services import providers

    for wid in sorted(set(workflow_ids)):
        steps = session.exec(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == wid, WorkflowStep.step_type == StepType.LLM_AGENT
            )
        ).all()
        for st in steps:
            eff = (st.provider or config.DEFAULT_PROVIDER or "").strip().lower()
            if eff and eff != "mock" and not providers.provider_is_live(eff):
                return True
    return False


# --- internal helpers --------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.utcnow()


_IMPORT_MEMO = "keys_import_memo"


@contextmanager
def import_memo(session: Session):
    """#585: a bulk key import's per-session memo. While it is open, each namespace's
    internal document is read once per chunk and the project's active locales once per
    key (not once per ensure/set call inside that key), and the session does not
    expire every loaded object on each commit (which cost a reload SELECT per object
    per key). The caller still commits per key (or per chunk) exactly as before.

    Re-entrant: an inner scope shares the outer memo. `import_keys` clears it at each
    chunk boundary and drops the active-locales entry before every key, so a locale
    another writer adds or removes mid-import reaches the very next key (gate).
    Everything is expired on exit so no stale state leaks into the caller's session."""
    outer = session.info.get(_IMPORT_MEMO)
    if outer is not None:
        yield outer
        return
    memo: dict = {}
    session.info[_IMPORT_MEMO] = memo
    previous = session.expire_on_commit
    session.expire_on_commit = False
    try:
        yield memo
    finally:
        session.info.pop(_IMPORT_MEMO, None)
        session.expire_on_commit = previous
        try:
            session.expire_all()
        except Exception:  # pragma: no cover - never mask the import's own outcome
            log.debug("expire_all after import failed", exc_info=True)


def import_memo_of(session: Session):
    """The open import memo for `session`, or None outside a bulk import."""
    return session.info.get(_IMPORT_MEMO)


def active_locales(session: Session, project_id: int) -> list[str]:
    """The project's active target locales, in creation order (the active-pairs list
    every other module reads)."""
    memo = import_memo_of(session)
    if memo is not None:
        cached = memo.get(("locales", project_id))
        if cached is None:
            cached = memo[("locales", project_id)] = _active_locales_query(session, project_id)
        return list(cached)
    return _active_locales_query(session, project_id)


def _active_locales_query(session: Session, project_id: int) -> list[str]:
    return [
        tl.locale
        for tl in session.exec(
            select(TargetLocale)
            .where(TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_(None))
            .order_by(TargetLocale.id)
        ).all()
    ]


def readable_locales(session: Session, project_id: int) -> list[str]:
    """Every target locale whose values may be READ or EXPORTED: the active ones plus the ones
    a connected project has stopped (a stopped language's translations stay readable and
    exportable; only new work and runs skip it). Writes and runs use active_locales."""
    return [
        tl.locale
        for tl in session.exec(
            select(TargetLocale)
            .where(TargetLocale.project_id == project_id)
            .order_by(TargetLocale.id)
        ).all()
    ]


def _next_order_index(session: Session, doc_id: int, locale: str) -> int:
    """Append position within (document, locale). `ux_segment_doc_locale_order` makes
    this tuple unique, so every new value row -- including each plural FORM -- takes
    its own slot.

    #576 gate: one MAX over the unique (document, locale, order) index. It loaded every
    order_index of the document and took max() in Python, once per new value row, so a
    key import was quadratic (10000 keys took 5 minutes, 50000 about two hours)."""
    top = session.exec(
        select(func.max(Segment.order_index)).where(
            Segment.document_id == doc_id, Segment.target_locale == locale
        )
    ).one()
    return (int(top) + 1) if top is not None else 0


def _source_for(base: dict[str, str], category: str | None, fill_missing: bool = True) -> str:
    """The base text a value row in `category` should carry. A target form the SOURCE
    locale does not have (ru's `few` for an en source) falls back to the source's
    `other`, which is the standard behaviour: the translator adapts one source form
    into each target form.

    `fill_missing=False` turns that fallback OFF for a caller who is writing an
    EXPLICIT, PARTIAL plural map and means exactly what it named (live-QA K5): `keys
    add --plural --base "Item"` documents itself as filling `other`, and fanning one
    SINGULAR string across `one`/`few`/`many`/`other` gives every plural form the wrong
    SOURCE text -- which MT then dutifully translates into every slot."""
    if category is None:
        return base.get("", "") or base.get("other", "")
    if not fill_missing:
        return base.get(category) or ""
    return base.get(category) or base.get("other") or base.get("", "")


def log_value_lock_change(session: Session, seg, *, actor: str = "") -> bool:
    """Append a Delta-feed VALUE `updated` row for a PURE-METADATA TM lock transition on
    a KEY value (PROPOSAL-tm-matching §8: "a lock/unlock is an op='updated' ... never a
    silent absence"). The ONLY caller is the EXPLICIT unlock (unlock_segment): a lock
    APPLICATION rides the tm_match FILL's own value-change row and an unlock-via-human-EDIT
    rides that edit's row, so those are NOT silent and must NOT double-log -- only an
    explicit unlock (no text change) would otherwise leave the feed silent. No-op (returns
    False) for a document segment (key_id is None) since the Delta feed is key-project
    only. Rides the caller's transaction."""
    key_id = getattr(seg, "key_id", None)
    if not key_id:
        return False
    key = session.get(StringKey, key_id)
    if key is None:
        return False
    session.add(
        KeyChangeLog(
            project_id=key.project_id,
            entity=KeyEntity.VALUE,
            key_id=key.id,
            namespace=key.namespace or "",
            name=key.name or "",
            target_locale=getattr(seg, "target_locale", None),
            plural_category=getattr(seg, "plural_category", "") or "",
            op=KeyOp.UPDATED,
            changed_at=_utcnow(),
            actor=(actor or current_actor() or ""),
        )
    )
    return True


def _changed(obj, field: str) -> bool:
    """True when this flush actually changes `field` (not merely that SQLAlchemy
    marked the instance dirty -- `session.dirty` admits false positives)."""
    try:
        hist = sa_inspect(obj).attrs[field].history
    except Exception:
        return False
    return hist.has_changes() and hist.added != hist.deleted


def log_bulk_value_deletions(session: Session, segments, *, actor: str | None = None) -> int:
    """Emit `entity=value, op=deleted` rows for value rows a BULK delete is about to
    remove -- the seam the ORM listener structurally cannot see.

    Exists for exactly one class of caller: a bulk `sa_delete(Segment)` that leaves the
    PROJECT (and therefore its keys, and therefore every consumer's copy of them)
    alive. `content.remove_target_locale` is the case the spec's own "construct a delete
    a since-cursor consumer misses" question was about, and `retention.delete_document`
    on an internal key document is the same shape. A whole-project delete deliberately
    does NOT call this: spec section 5 defines a vanished project as "gone,
    re-bootstrap", and tombstones for a project id that no longer resolves are noise.

    Segments without a `key_id` are ignored, so a document-only install calling this
    (retention does, on every document) appends nothing. Rows ride the caller's
    transaction -- no commit here, so a rolled-back delete leaves no phantom tombstone.
    Returns the number of rows appended.

    `actor` is EXPLICIT here for the reason `set_value` and friends take one
    (re-review R5, review B5): the busiest caller is `content.remove_target_locale`,
    reached from a FastAPI sync route running on anyio's worker POOL, where
    `current_actor()` is "" at best and another request's leftover at worst. `None`
    (the default) keeps the thread-local behaviour for callers that really are inside
    a scope."""
    rows = [s for s in segments if getattr(s, "key_id", None)]
    if not rows:
        return 0
    actor = current_actor() if actor is None else (actor or "")
    made = 0
    with session.no_autoflush:
        cache: dict[int, StringKey] = {}
        for seg in rows:
            if seg.key_id not in cache:
                cache[seg.key_id] = session.get(StringKey, seg.key_id)
            key = cache[seg.key_id]
            if key is None:
                continue  # already gone; nothing left to describe
            session.add(
                KeyChangeLog(
                    project_id=key.project_id,
                    entity=KeyEntity.VALUE,
                    key_id=key.id,
                    namespace=key.namespace or "",
                    name=key.name or "",
                    target_locale=seg.target_locale,
                    plural_category=seg.plural_category or "",
                    op=KeyOp.DELETED,
                    changed_at=_utcnow(),
                    actor=actor,
                )
            )
            made += 1
    return made


def _key_dialect(session, *, tag=None, q=None):
    """Keep JSON membership, literal substring and collation dialect choices together."""
    postgres = session.get_bind().dialect.name == "postgresql"
    predicates = []
    if tag:
        if postgres:
            predicates.append(cast(StringKey.tags, JSONB).contains([tag]))
        else:
            members = func.json_each(StringKey.tags).table_valued("value")
            predicates.append(select(1).select_from(members).where(members.c.value == tag).exists())
    if q:
        needle = str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + needle + "%"
        columns = (StringKey.name, StringKey.description, StringKey.context)
        predicates.append(
            or_(
                *(
                    c.ilike(pattern, escape="\\")
                    if postgres
                    else func.loc_lower(c).like(func.loc_lower(pattern), escape="\\")
                    for c in columns
                )
            )
        )
    order = [
        c.collate("C") if postgres else c
        for c in (StringKey.namespace, StringKey.name, StringKey.context)
    ]
    return predicates, [*order, StringKey.id]


def _key_order(session):
    return _key_dialect(session)[1]


_KEY_TRIM_CHARS = (
    " \t\n\r\v\f\u00a0\u1680"
    + "".join(chr(n) for n in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


# Content whitespace: Python strip's common Unicode spaces plus the BOM.
def _key_statement(
    session,
    project_id,
    *,
    namespace=None,
    tag=None,
    q=None,
    include_deleted=False,
    include_archived=True,
    include_hidden=True,
    status=None,
    untranslated_in=None,
):
    stmt = select(StringKey).where(StringKey.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(StringKey.deleted_at.is_(None))
    if namespace is not None:
        stmt = stmt.where(StringKey.namespace == (namespace or ""))
    if not include_archived:
        stmt = stmt.where(StringKey.is_archived.is_(False))
    if not include_hidden:
        stmt = stmt.where(StringKey.is_hidden.is_(False))
    if status:
        stmt = stmt.where(
            select(1)
            .where(
                Segment.key_id == StringKey.id,
                Segment.status == status,
            )
            .exists()
        )
    if untranslated_in:
        trim = func.btrim if session.get_bind().dialect.name == "postgresql" else func.trim
        stmt = stmt.where(
            select(1)
            .where(
                Segment.key_id == StringKey.id,
                Segment.target_locale == untranslated_in,
                trim(func.coalesce(Segment.target_text, ""), _KEY_TRIM_CHARS) == "",
            )
            .exists()
        )
    return stmt.where(*_key_dialect(session, tag=tag, q=q)[0])


def _key_view(key, rows, base) -> dict:
    """ONE key rendered as a plain dict -- THE shared payload behind the web grid,
    the `/api` key routes and `keys list --json`, so the three surfaces cannot drift
    into describing the same key differently (the parity law, mechanized).

    `values` is keyed by target locale; each entry lists one row per plural form with
    its text, status and qa_flags. `base` is the source-locale value(s)."""
    # K8 read-time derived check (spec §9): write-time gating only judges a value when it
    # is NEXT written, so an existing over-limit row would stay invisible until touched.
    # `key_view` already loads every value row, so ONE len() per row yields a DERIVED
    # `over_limit` the grid, /api and `keys list --json` render on the first page load
    # after deploy — no rows modified, no statuses changed, no delta entries emitted. A
    # pure len() comparison (not a heuristic), so the false-positive risk is near zero.
    from app.services import quality

    lim = quality.coerce_char_limit(key.char_limit)
    values: dict[str, list] = {}
    for seg in rows:
        entry = {
            "segment_id": seg.id,
            "plural_category": seg.plural_category or "",
            "text": seg.target_text or "",
            "status": seg.status,
            "qa_flags": list(seg.qa_flags or []),
            # TM-matching lock (PROPOSAL-tm-matching D-1): surface the lock in the key
            # grid. None on every unlocked value = byte-identical.
            "lock_band": seg.lock_band,
            "lock_hard": bool(seg.lock_hard),
        }
        if lim is not None:
            n = quality.char_count(seg.target_text or "")
            if n > lim:
                entry["over_limit"] = {"length": n, "limit": lim}
        values.setdefault(seg.target_locale, []).append(entry)
    return {
        "id": key.id,
        "project_id": key.project_id,
        "namespace": key.namespace or "",
        "name": key.name,
        "context": key.context or "",
        "description": key.description or "",
        "char_limit": key.char_limit,
        "is_plural": bool(key.is_plural),
        "plural_arg_name": key.plural_arg_name,
        "tags": list(key.tags or []),
        "source_file": key.source_file or "",
        "is_hidden": bool(key.is_hidden),
        "is_archived": bool(key.is_archived),
        "custom": dict(key.custom or {}),
        "deleted": key.deleted_at is not None,
        "created_at": key.created_at.isoformat() if key.created_at else "",
        "updated_at": key.updated_at.isoformat() if key.updated_at else "",
        "translations_modified_at": (
            key.translations_modified_at.isoformat() if key.translations_modified_at else ""
        ),
        "base": base,
        "values": values,
    }


def _audit(
    action: str, project_id, target: str, *, actor: str, ip: str = "", detail: str = ""
) -> None:
    """One audit row for a destructive / policy key operation (review B9).

    Written at the SERVICE seam, not per route: `content.delete_document`'s precedent,
    and the only version of this that the web grid, `/api` and the CLI cannot each
    forget. Best-effort by the repo's own convention -- an audit failure never unwinds
    a committed key operation."""
    from app.services import audit as _audit_svc

    try:
        _audit_svc.log(
            actor=actor or "system",
            action=action,
            target=target,
            detail=detail,
            ip=ip or "",
            customer_id=_audit_svc.customer_for_project(project_id),
        )
    except Exception:
        # A destructive key op with no audit row must at least leave a log trace.
        log.warning("audit write failed for %s on %s", action, target, exc_info=True)


def _key_target(key) -> str:
    """The audit `target` string for one key: id plus its identity tuple."""
    return "key {} {}::{}{}".format(
        key.id,
        key.namespace or "(default)",
        key.name,
        f" [{key.context}]" if key.context else "",
    )


def content_hash(text: str | None) -> str:
    """The stable content hash carried on an enriched delta entry (AP-1). DEFINED as
    the lowercase hex SHA-256 of the value text encoded UTF-8:

        sha256(text.encode("utf-8")).hexdigest()   # 64 lowercase hex chars

    A NULL / missing value hashes as the empty string (the well-known
    e3b0c442...b855 digest), so a consumer can compare hashes without special-casing
    "no value yet". The hash is over the EXACT target text -- no normalisation,
    trimming or case-folding -- so a byte-identical value on both sides always
    matches and any real edit always differs. A consumer holding a matching hash may
    skip re-fetching the string; that is the whole point of shipping it inline."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _modified_after_token(ts_iso: str, after_id) -> str:
    """The opaque single-field cursor `ISO~id` (gate NEW-3). A consumer can feed the
    whole `next_modified_after` back as `modified_after` and resume atomically, without
    having to also thread `next_after_id` through a second parameter (a half-cursor that
    silently loses same-microsecond keys)."""
    return "%s~%d" % (ts_iso, after_id) if after_id is not None else ts_iso  # noqa: UP031
