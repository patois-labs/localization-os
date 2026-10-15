"""glossary_concepts: part 3 (layered pure move)

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func as sa_func
from sqlalchemy import insert as sa_insert
from sqlalchemy import or_
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from app import config
from app.models import (
    GlossaryConcept,
    GlossaryEntry,
    GlossarySense,
    GlossaryTerm,
    TermAudience,
)
from app.services.glossary_concepts_common import (
    ConceptError,
    _configured_pairs,
    _group_base_id,
    _pick_target_term,
    _require_concept_editor,
    _sanitize,
    _touch_draft,
    _usable_source_terms,
    _utcnow,
    canon_audience,
)
from app.services.glossary_concepts_part2 import _apply_term_fields, sync_entry_concept

log = logging.getLogger("loc_tms.glossary_concepts")


def _sync_glossary_from_entries(session: Session, glossary_id: int) -> None:
    """Upsert the concept/term store for EVERY module entry of one glossary (the
    unguarded core of the backfill, and the reconcile pass the bulk CSV importer runs).

    BOUNDED-MEMORY + FEWER QUERIES (gate round-1 M5). Processes entries in
    GLOSSARY_IMPORT_FLUSH_BATCH-sized batches and `expunge_all()`s after each so live ORM
    state stays O(batch) rather than O(glossary) — the same posture glossary._apply_rows
    holds for the entry writes. A per-glossary PRIMITIVES cache
    ``(concept_id, locale, term) -> term_id`` is preloaded once (chunked to respect
    SQLite's parameter cap) so a re-import skips the per-entry source/target-term SELECTs;
    it holds ints, never ORM rows, so it is O(distinct terms) — the same admission
    `_apply_rows`' own `state` dict makes for dedupe. Idempotent."""
    from app.services.glossary import GLOSSARY_IMPORT_FLUSH_BATCH as _BATCH

    # Objects the CALLER already had in the session before this reconcile (e.g. the web
    # route's `Glossary` row, which relies on expire-on-commit, not detach). We expunge
    # only what THIS reconcile loads/creates, never these. Keyed by id() — SQLModel rows
    # are unhashable; the caller's rows stay referenced (alive) so their ids are stable.
    _caller_ids = {id(o) for o in session}

    # Preload existing term identity as primitives so re-imports skip the SELECTs.
    concept_ids = list(
        session.exec(
            select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == glossary_id)
        ).all()
    )
    term_id_cache: dict[tuple[int, str, str], int] = {}
    for i in range(0, len(concept_ids), 400):
        chunk = concept_ids[i : i + 400]
        for tid, cid, loc, term in session.exec(
            select(
                GlossaryTerm.id, GlossaryTerm.concept_id, GlossaryTerm.locale, GlossaryTerm.term
            ).where(GlossaryTerm.concept_id.in_(chunk))
        ).all():
            term_id_cache[(cid, loc, term)] = tid

    # R4 shared identity: (source_locale, casefold source_term) -> concept_id, so every
    # locale pair of one source surface shares ONE concept across batches (a DB fallback in
    # _find_concept_for_source covers a cache miss after an expunge).
    concept_lookup: dict[tuple[str, str], int] = {}
    last_id = 0
    while True:
        entries = session.exec(
            select(GlossaryEntry)
            .where(GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.id > last_id)
            .order_by(GlossaryEntry.id)
            .limit(_BATCH)
        ).all()
        if not entries:
            break
        # Preload this batch's senses in ONE query (avoids a SELECT per entry).
        eids = [e.id for e in entries]
        senses_by_entry: dict[int, list] = {}
        for s in session.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))
        ).all():
            senses_by_entry.setdefault(s.glossary_entry_id, []).append(s)
        for e in entries:
            sync_entry_concept(
                session,
                e,
                _term_id_cache=term_id_cache,
                _senses=senses_by_entry.get(e.id, []),
                _concept_lookup=concept_lookup,
            )
            last_id = e.id
        session.flush()
        # Bound memory: detach ONLY the rows THIS reconcile loaded/created (not the
        # caller's objects), so live ORM state stays O(batch); the primitives cache
        # survives. `expunge_all()` would also detach the caller's Glossary row, breaking
        # a web import's reliance on expire-on-commit (gate round-1 regression).
        for obj in list(session):
            if id(obj) not in _caller_ids:
                session.expunge(obj)


def prime_reconcile_cache(session: Session, glossary_id: int, cache: dict) -> None:
    """Load the glossary's source-surface identity map ONCE into ``cache`` (#615 review), for the
    chunked module import: ``(source_locale, casefold source_term) -> concept_id`` over every
    concept-linked entry, plus the highest entry id seen (``wm``). The importer calls this
    BEFORE its first chunk takes the write lock, so the one full scan never holds the lock;
    :func:`_scoped_reconcile_maps` then only tops the map up from ids above ``wm``."""
    concept_by_src: dict[tuple[str, str], int] = {}
    for cid, sloc, sterm in session.exec(
        select(GlossaryEntry.concept_id, GlossaryEntry.source_locale, GlossaryEntry.source_term)
        .where(GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.concept_id.is_not(None))
        .order_by(GlossaryEntry.id)
    ).all():
        concept_by_src.setdefault(((sloc or ""), (sterm or "").casefold()), cid)
    cache["map"] = concept_by_src
    cache["wm"] = (
        session.exec(
            select(sa_func.max(GlossaryEntry.id)).where(GlossaryEntry.glossary_id == glossary_id)
        ).first()
        or 0
    )


def _scoped_reconcile_maps(
    session: Session, glossary_id: int, entry_ids: list[int], cache: dict | None = None
):
    """The four preloaded lookups of :func:`_bulk_sync_glossary_from_entries`, restricted to one
    chunk of entries (#615): ``(concept_by_src, term_by_key, sense_entry_ids, blank_ext_cids)``.

    * concepts: the BASE concept already linked for each source surface the chunk names, found
      by a SQL ``lower()`` pre-filter on ``source_term`` (cheap, no row text loaded beyond the
      matches) and CONFIRMED with Python ``casefold()`` -- the authoritative fold, exactly as
      ``_find_concept_for_source`` does (SQLite's ``lower()`` is ASCII-only, so a non-ASCII
      case variant stored by another writer is the one shape this misses, the same cold-path
      limit that function documents; ``consolidate_split_concepts`` heals it);
    * terms: only those of the concepts the chunk can reach;
    * sense-bearing entries and blank-external-id concepts: only the chunk's own."""
    ids = sorted(set(entry_ids))
    chunk_rows: list[tuple] = []
    for i in range(0, len(ids), 500):
        chunk_rows.extend(
            session.exec(
                select(
                    GlossaryEntry.id,
                    GlossaryEntry.concept_id,
                    GlossaryEntry.source_locale,
                    GlossaryEntry.source_term,
                ).where(
                    GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.id.in_(ids[i : i + 500])
                )
            ).all()
        )
    wanted = {((sloc or ""), (sterm or "").casefold()) for _id, _c, sloc, sterm in chunk_rows}
    lowers = sorted({(sterm or "").lower() for _id, _c, _s, sterm in chunk_rows})
    if cache is None:
        concept_by_src: dict[tuple[str, str], int] = {}
        for i in range(0, len(lowers), 400):
            for cid, sloc, sterm in session.exec(
                select(
                    GlossaryEntry.concept_id, GlossaryEntry.source_locale, GlossaryEntry.source_term
                )
                .where(
                    GlossaryEntry.glossary_id == glossary_id,
                    GlossaryEntry.concept_id.is_not(None),
                    sa_func.lower(GlossaryEntry.source_term).in_(lowers[i : i + 400]),
                )
                .order_by(GlossaryEntry.id)
            ).all():
                key = ((sloc or ""), (sterm or "").casefold())
                if key in wanted:
                    concept_by_src.setdefault(key, cid)
        hit_keys = set(concept_by_src)
    else:
        # ONE identity map for the whole import (#615 review): the lower() pre-filter above has
        # no index, so repeating it per chunk costs a scan of the glossary each time (about
        # 0.2 s at 100k entries, more than the chunk itself). Load the map once, then top it up
        # each chunk with only the entries ABOVE the last id seen (primary-key range, so another
        # writer's new entries are still found); this import's own concepts are registered by
        # the reconcile in place.
        if cache.get("map") is None:
            prime_reconcile_cache(session, glossary_id, cache)
        concept_by_src = cache["map"]
        wm = int(cache.get("wm") or 0)
        top = wm
        for eid, cid, sloc, sterm in session.exec(
            select(
                GlossaryEntry.id,
                GlossaryEntry.concept_id,
                GlossaryEntry.source_locale,
                GlossaryEntry.source_term,
            )
            .where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.id > wm,
                GlossaryEntry.concept_id.is_not(None),
            )
            .order_by(GlossaryEntry.id)
        ).all():
            top = max(top, eid)
            concept_by_src.setdefault(((sloc or ""), (sterm or "").casefold()), cid)
        cache["wm"] = top
        # The map was read before the lock, so CHECK every hit against the store as it is now
        # (gate): the mapped concept must still exist in this glossary AND still carry this
        # source surface (a term in the source locale with the same casefold). A concept another
        # writer deleted (or renamed away from the surface) is dropped, and the surface is looked
        # up again the authoritative way below, so a surface deleted and created again meanwhile
        # links to the live concept instead of minting a second one. Indexed by concept_id.
        hit_keys = {k for k in wanted if k in concept_by_src}
        cand = sorted({concept_by_src[k] for k in hit_keys})
        surfaces: set[tuple[int, str, str]] = set()
        for i in range(0, len(cand), 400):
            for cid, loc, term in session.exec(
                select(GlossaryTerm.concept_id, GlossaryTerm.locale, GlossaryTerm.term)
                .join(GlossaryConcept, GlossaryConcept.id == GlossaryTerm.concept_id)
                .where(
                    GlossaryTerm.concept_id.in_(cand[i : i + 400]),
                    GlossaryConcept.glossary_id == glossary_id,
                )
            ).all():
                surfaces.add((cid, loc or "", (term or "").casefold()))
        stale = {k for k in hit_keys if (concept_by_src[k], k[0], k[1]) not in surfaces}
        for k in stale:
            del concept_by_src[k]
        hit_keys -= stale
        if stale:
            # Rare (only under a concurrent edit of these surfaces): the lower() pre-filter for
            # just the stale surfaces, confirmed by casefold, exactly as the uncached path.
            stale_lowers = sorted(
                {
                    (sterm or "").lower()
                    for _id, _c, sloc, sterm in chunk_rows
                    if ((sloc or ""), (sterm or "").casefold()) in stale
                }
            )
            for i in range(0, len(stale_lowers), 400):
                for cid, sloc, sterm in session.exec(
                    select(
                        GlossaryEntry.concept_id,
                        GlossaryEntry.source_locale,
                        GlossaryEntry.source_term,
                    )
                    .where(
                        GlossaryEntry.glossary_id == glossary_id,
                        GlossaryEntry.concept_id.is_not(None),
                        sa_func.lower(GlossaryEntry.source_term).in_(stale_lowers[i : i + 400]),
                    )
                    .order_by(GlossaryEntry.id)
                ).all():
                    key = ((sloc or ""), (sterm or "").casefold())
                    if key in stale and key not in concept_by_src:
                        concept_by_src[key] = cid
                        hit_keys.add(key)
    concept_ids = sorted(
        {concept_by_src[k] for k in hit_keys}
        | {cid for _id, cid, _s, _t in chunk_rows if cid is not None}
    )
    term_by_key: dict[tuple[int, str, str], int] = {}
    blank_ext_cids: set[int] = set()
    for i in range(0, len(concept_ids), 400):
        batch = concept_ids[i : i + 400]
        for tid, cid, loc, term in session.exec(
            select(
                GlossaryTerm.id, GlossaryTerm.concept_id, GlossaryTerm.locale, GlossaryTerm.term
            ).where(GlossaryTerm.concept_id.in_(batch))
        ).all():
            term_by_key[(cid, loc, term)] = tid
        blank_ext_cids.update(
            session.exec(
                select(GlossaryConcept.id).where(
                    GlossaryConcept.id.in_(batch),
                    GlossaryConcept.glossary_id == glossary_id,
                    or_(GlossaryConcept.external_id == "", GlossaryConcept.external_id.is_(None)),
                )
            ).all()
        )
    sense_entry_ids: set[int] = set()
    for i in range(0, len(ids), 500):
        sense_entry_ids.update(
            session.exec(
                select(GlossarySense.glossary_entry_id)
                .where(GlossarySense.glossary_entry_id.in_(ids[i : i + 500]))
                .distinct()
            ).all()
        )
    return concept_by_src, term_by_key, sense_entry_ids, blank_ext_cids


def _bulk_sync_glossary_from_entries(
    session: Session,
    glossary_id: int,
    entry_ids: list[int] | None = None,
    cache: dict | None = None,
) -> None:
    """BULK concept/term reconcile for the importer — same store as
    :func:`_sync_glossary_from_entries` (proven byte-identical up to ids/uuids/timestamps
    by tests/verify_glossary_bulk_import_scaling.py's golden), but LINEAR: ids are DB-assigned
    through ``insert(...).returning(..., sort_by_parameter_order=True)``, which is ONE batched
    statement per flush batch on PostgreSQL but degrades to one INSERT per row on SQLite
    (the dialect reports the sentinel as NOT_SUPPORTED) -- still linear, no per-row ORM
    construction/flush, and no O(n^2) source-surface scan (gate round 2, M2).

    The per-entry :func:`sync_entry_concept` path is correct but has two costs that made a
    real termbase import (61,956 termEntry Microsoft TBX) quadratic and flush-bound:

    * ``_find_concept_for_source`` falls back to a ``lower(source_term)`` SELECT over the
      whole glossary on every cache miss — and a fresh import misses on every first-seen
      surface — an O(n) scan per entry (no index on ``source_term``) -> O(n^2).
    * ``_upsert_term`` / ``sync_entry_concept`` flush once per concept and per term to read
      back the autoincrement id -> O(rows) write round-trips.

    This builder removes both: it PRE-LOADS the glossary's concept identity map
    ``(source_locale, casefold source_term) -> concept_id`` (base concepts only, the R4
    identity ``_find_concept_for_source`` keys on) and the term identity map
    ``(concept_id, locale, term) -> term_id`` ONCE, so resolution is a pure dict lookup and
    the fallback scan is never taken; and it CREATES each batch's new concepts and terms
    with ONE Core ``executemany`` INSERT each, letting the DB assign the ids and reading
    them back with ``insert(...).returning(id, sort_by_parameter_order=True)`` (SQLAlchemy 2
    insertmanyvalues) so the in-memory maps are filled from the DB-assigned ids. No id is
    minted in Python: the DB is the sole id authority on BOTH SQLite (rowid) and Postgres
    (SERIAL sequence), so the sequence advances and a later ORM insert — the sense delegation
    below, or a subsequent per-entry ``add_term`` — can never collide with a reserved id
    (gate round 1 H1/H2). Concept meta and existing source-term field updates are applied
    with one ORM ``executemany`` UPDATE per column-set. Bounded memory is preserved exactly as
    ``_sync_glossary_from_entries``: entries are streamed in ``GLOSSARY_IMPORT_FLUSH_BATCH``
    chunks and everything but the caller's own rows is expunged after each batch; the maps
    hold ints/short strings only (O(distinct concepts/terms), the dedupe floor the importer
    already pays).

    Field semantics are copied verbatim from ``sync_entry_concept`` + ``_upsert_term`` (the
    equivalence golden enforces this):

    * base concept per (glossary, source_locale, casefold source_term); ``external_id`` a
      uuid4 stamped once; ``definition``/``domain``/``notes`` set from the entry's meta ONLY
      when the entry carries the key (no-clobber on a shared concept), applied in entry-id
      order so the last non-blank wins exactly as the per-entry path.
    * source ``GlossaryTerm`` keyed by the VERBATIM surface, carrying the entry's
      ``dnt``/``case_sensitive`` and (only when present) ``part_of_speech``/``usage_example``
      meta; on a shared concept a later entry UPDATES those the same no-clobber way.
    * target ``GlossaryTerm`` (when the entry's target is non-blank), no optional fields.
    * ``entry.concept_id`` / ``entry.term_id`` linked to the base concept + source term.

    A sense-bearing entry (never produced by a TBX import; only a hand-authored CSV with a
    ``sense_label`` column) is DELEGATED to the proven :func:`sync_entry_concept` after the
    batch's base rows exist, sharing the same in-run caches — so the homonym-group stamping
    and sibling-concept creation (§4.5) are unchanged. Idempotent; the caller owns the
    savepoint/commit.

    ``entry_ids`` (#615): reconcile ONLY those entries (one chunk of a big import), so a chunk
    costs what its own rows cost, not what the whole glossary costs. The identity maps are then
    loaded for just the surfaces and concepts the chunk names (a SQL ``lower()`` pre-filter
    confirmed by ``casefold()`` in Python, the way ``_find_concept_for_source`` does), and the
    pass touches nothing else. ``None`` is the whole-glossary reconcile, unchanged."""
    from app.services.glossary import GLOSSARY_IMPORT_FLUSH_BATCH as _BATCH

    # The reconcile streams entries in its OWN batch, capped below the importer's flush batch:
    # each batch's transient insert-dict lists + SQLAlchemy's executemany parameter expansion
    # are O(batch), and a dense (tiny-file, many-tiny-terms) import measures peak heap as a
    # multiple of a very small file, so a smaller batch keeps that multiple under the bound
    # (tests/verify_glossary_import_bounded_memory.py) while staying well within throughput.
    _RBATCH = min(_BATCH, 250)
    _caller_ids = {id(o) for o in session}
    default_aud = canon_audience(TermAudience.DEFAULT)

    if entry_ids is not None:
        concept_by_src, term_by_key, sense_entry_ids, blank_ext_cids = _scoped_reconcile_maps(
            session, glossary_id, entry_ids, cache
        )
    else:
        # --- preload the two identity maps ONCE (primitives only) -------------------------
        # concept identity: (source_locale, casefold source_term) -> concept_id, from the BASE
        # entries already linked (a re-import), so a first pass over an EMPTY glossary preloads
        # nothing and the map is grown in-run. This is the same key _find_concept_for_source
        # uses; because it is fully authoritative here (every existing base concept is loaded,
        # and every concept this run creates is registered), the O(n) DB fallback is never run.
        concept_by_src: dict[tuple[str, str], int] = {}
        for cid, sloc, sterm in session.exec(
            select(
                GlossaryEntry.concept_id, GlossaryEntry.source_locale, GlossaryEntry.source_term
            ).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id.is_not(None),
            )
        ).all():
            concept_by_src.setdefault(((sloc or ""), (sterm or "").casefold()), cid)

        # term identity: (concept_id, locale, term) -> term_id, chunked to respect SQLite's
        # parameter cap (same shape as _sync_glossary_from_entries' preload).
        concept_ids = list(
            session.exec(
                select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == glossary_id)
            ).all()
        )
        term_by_key: dict[tuple[int, str, str], int] = {}
        for i in range(0, len(concept_ids), 400):
            chunk = concept_ids[i : i + 400]
            for tid, cid, loc, term in session.exec(
                select(
                    GlossaryTerm.id, GlossaryTerm.concept_id, GlossaryTerm.locale, GlossaryTerm.term
                ).where(GlossaryTerm.concept_id.in_(chunk))
            ).all():
                term_by_key[(cid, loc, term)] = tid

        # entry ids that carry >=1 sense (delegated to the per-entry path). One query; empty for
        # a TBX import (no senses at all), so that hot path never pays for it.
        sense_entry_ids: set[int] = set(
            session.exec(
                select(GlossarySense.glossary_entry_id)
                .join(GlossaryEntry, GlossarySense.glossary_entry_id == GlossaryEntry.id)
                .where(GlossaryEntry.glossary_id == glossary_id)
                .distinct()
            ).all()
        )

        # existing concepts of this glossary that carry a BLANK external_id (a migration-backfilled
        # concept: `_backfill_core` leaves external_id ""), so the bulk path can SELF-HEAL them the
        # way the per-entry `sync_entry_concept` does (round-1 LOW: stamp a uuid4 the first time a
        # batch touches such a concept). One query; empty for a fresh import (nothing to heal).
        blank_ext_cids: set[int] = set(
            session.exec(
                select(GlossaryConcept.id).where(
                    GlossaryConcept.glossary_id == glossary_id,
                    or_(GlossaryConcept.external_id == "", GlossaryConcept.external_id.is_(None)),
                )
            ).all()
        )

    # NO id is minted in Python (round-1 H1/H2). Every new concept/term is inserted with the id
    # column OMITTED and read back via ``insert(...).returning(id, sort_by_parameter_order=True)``
    # (SQLAlchemy 2 insertmanyvalues) so the DB assigns the id on BOTH SQLite (rowid) and Postgres
    # (SERIAL) and the sequence advances — a later ORM insert (the sense delegation here, or a
    # subsequent per-entry ``add_term``) then gets a fresh id and can never collide with a value
    # this path reserved. Insert dicts carry ONLY the columns that differ from the model/Column
    # defaults (status -> "preferred", audience -> the canonical default, case_sensitive/dnt/exact
    # -> False, free-text fields -> "", created_at -> now via the Column default), plus updated_at
    # (round-1 LOW: created/updated rows carry `updated_at` the way `_upsert_term` writes it;
    # created_at/updated_at are excluded from the equivalence golden). Minimal, uniform-key dicts
    # keep the executemany parameter footprint small — the bounded-memory bound is a multiple of a
    # TINY dense file, so every retained byte per row counts.
    now = _utcnow()
    # `default_aud` must equal the Field default the omitted `audience` column falls back to, else
    # a bulk-inserted term would silently carry a wrong audience. An explicit raise (not a bare
    # `assert`, which `python -O` strips) makes the guard load-bearing in production (round-1 LOW).
    if default_aud != TermAudience.DEFAULT:
        raise ConceptError(
            f"term audience default drift: canon_audience(DEFAULT)={default_aud!r} != "
            f"{TermAudience.DEFAULT!r}; the bulk insert omits the `audience` column and relies on "
            f"it falling to the Field default, so this would silently import a wrong audience."
        )

    def _apply_updates(model, mapping: dict) -> None:
        """Bulk-update rows keyed by pk. `mapping` = {pk_id: {col: val, ...}}; grouped by
        column-set so each group is one ORM executemany UPDATE (rows in a group are uniform)."""
        if not mapping:
            return
        buckets: dict[tuple, list] = {}
        for pk, cols in mapping.items():
            buckets.setdefault(tuple(sorted(cols)), []).append({"id": pk, **cols})
        for rows in buckets.values():
            session.execute(sa_update(model), rows)

    def _insert_returning_ids(model, rows: list[dict]) -> list[int]:
        """One Core executemany INSERT of `rows` (id omitted) returning the DB-assigned ids in
        input order (insertmanyvalues + sort_by_parameter_order) so callers can align them to the
        keys they inserted. Empty list -> no-op."""
        if not rows:
            return []
        if config.IS_SQLITE:
            # #597: SQLite has no insertmanyvalues batching (RETURNING degrades to one INSERT
            # per row, the bulk of the time the importer held the write lock). Insert the whole
            # batch with ONE executemany and the id column OMITTED (the DB still assigns every
            # id), then read the new ids back. The importer holds the single SQLite write lock
            # here (import_lock.begin_write_phase), so nothing else can insert in between and
            # rowids are handed out in insertion order: the rows past the previous max id are
            # exactly this batch, in order.
            base = session.execute(sa_select(sa_func.max(model.id))).scalar() or 0
            session.execute(sa_insert(model), rows)
            ids = list(
                session.execute(
                    sa_select(model.id).where(model.id > base).order_by(model.id)
                ).scalars()
            )
            if len(ids) != len(rows):
                raise ConceptError(
                    f"bulk insert into {model.__tablename__} produced {len(ids)} ids for "
                    f"{len(rows)} rows; the write lock was not held"
                )
            return ids
        return list(
            session.execute(
                sa_insert(model).returning(model.id, sort_by_parameter_order=True),
                rows,
            ).scalars()
        )

    last_id = 0
    scope_ids = sorted(set(entry_ids)) if entry_ids is not None else None
    scope_pos = 0
    while True:
        if scope_ids is None:
            entries = session.exec(
                select(GlossaryEntry)
                .where(GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.id > last_id)
                .order_by(GlossaryEntry.id)
                .limit(_RBATCH)
            ).all()
        else:
            # #615: only this chunk's entries, in id order, one reconcile batch at a time.
            if scope_pos >= len(scope_ids):
                break
            entries = session.exec(
                select(GlossaryEntry)
                .where(
                    GlossaryEntry.glossary_id == glossary_id,
                    GlossaryEntry.id.in_(scope_ids[scope_pos : scope_pos + _RBATCH]),
                )
                .order_by(GlossaryEntry.id)
            ).all()
            scope_pos += _RBATCH
            if not entries:
                continue
        if not entries:
            break
        last_id = entries[-1].id
        plain = [e for e in entries if e.id not in sense_entry_ids]
        sense_entries = [e for e in entries if e.id in sense_entry_ids]

        # --- pass 1: resolve/collect NEW base concepts, insert them, learn their ids ---------
        # Concepts are created BEFORE the term pass so every source/target term FK resolves to a
        # concept that already exists in the DB. New concepts are deduped within the batch by
        # source-surface key (multiple entries share one concept), inserted through one RETURNING
        # insert call (batched on PostgreSQL, per-row on SQLite -- see the module docstring),
        # and their DB-assigned ids folded into `concept_by_src`.
        # concept inserts (id omitted): each {glossary_id, external_id, updated_at}
        concept_rows: list[dict] = []
        new_concept_keys: list[tuple[str, str]] = []
        new_concept_seen: set[tuple[str, str]] = set()
        for e in plain:
            if e.concept_id is not None:
                continue
            ckey = ((e.source_locale or ""), e.source_term.casefold())
            if ckey in concept_by_src or ckey in new_concept_seen:
                continue
            new_concept_seen.add(ckey)
            new_concept_keys.append(ckey)
            concept_rows.append(
                {"glossary_id": glossary_id, "external_id": str(uuid.uuid4()), "updated_at": now}
            )
        for ckey, cid in zip(
            new_concept_keys, _insert_returning_ids(GlossaryConcept, concept_rows), strict=True
        ):
            concept_by_src[ckey] = cid

        # --- pass 2: terms, concept meta, source-term field updates, entry links -------------
        concept_updates: dict[int, dict] = {}  # cid -> concept field updates (meta/ext_id)
        new_src_rows: dict[tuple[int, str, str], dict] = {}  # src skey -> insert dict (id omitted)
        new_tgt_rows: dict[tuple[int, str, str], dict] = {}  # tgt tkey -> insert dict (id omitted)
        src_updates: dict[int, dict] = {}  # existing src term id -> field updates
        entry_link: dict[int, tuple[int, tuple[int, str, str]]] = {}  # entry id -> (cid, src skey)

        for e in plain:
            # base concept (R4 shared identity) — always resolvable now (pass 1 created new ones).
            if e.concept_id is not None:
                cid = e.concept_id
            else:
                cid = concept_by_src[((e.source_locale or ""), e.source_term.casefold())]
            # self-heal a blank external_id on an EXISTING concept, ONCE (round-1 LOW).
            if cid in blank_ext_cids:
                blank_ext_cids.discard(cid)
                d = concept_updates.setdefault(cid, {})
                d["external_id"] = str(uuid.uuid4())
                d["updated_at"] = now
            meta = e.meta or {}
            # concept-level meta, folded in entry-id order (only keys the entry carries).
            if meta.get("definition") or meta.get("subject_field") or meta.get("notes"):
                d = concept_updates.setdefault(cid, {})
                if meta.get("definition"):
                    d["definition"] = _sanitize(meta["definition"])
                if meta.get("subject_field"):
                    d["domain"] = _sanitize(meta["subject_field"])
                if meta.get("notes"):
                    d["notes"] = _sanitize(meta["notes"])
                d["updated_at"] = now
            # --- source term (VERBATIM surface), created once; later entries UPDATE it ---
            pos = _sanitize(meta["part_of_speech"]) if meta.get("part_of_speech") else None
            ctx = _sanitize(meta["usage_example"]) if meta.get("usage_example") else None
            skey = (cid, e.source_locale or "", e.source_term)
            if skey in term_by_key:
                sid = term_by_key[skey]
                # a source term dual-write mirrors the entry's dnt/case_sensitive ALWAYS and
                # pos/ctx only when the entry carries them (no-clobber) — last entry wins.
                u = src_updates.setdefault(sid, {})
                u["dnt"] = bool(e.dnt)
                u["case_sensitive"] = bool(e.case_sensitive)
                if pos is not None:
                    u["part_of_speech"] = pos
                if ctx is not None:
                    u["context_example"] = ctx
                u["updated_at"] = now
            elif skey in new_src_rows:
                # a second entry of the SAME batch sharing this new source term: mutate the row
                # dict in place (last dnt/cs wins; pos/ctx no-clobber-when-present) — identical to
                # the per-entry create-then-update semantics, but before the id exists.
                row = new_src_rows[skey]
                row["dnt"] = bool(e.dnt)
                row["case_sensitive"] = bool(e.case_sensitive)
                if pos is not None:
                    row["part_of_speech"] = pos
                if ctx is not None:
                    row["context_example"] = ctx
            else:
                new_src_rows[skey] = {
                    "concept_id": cid,
                    "locale": e.source_locale or "",
                    "term": e.source_term,
                    "dnt": bool(e.dnt),
                    "case_sensitive": bool(e.case_sensitive),
                    "part_of_speech": pos or "",
                    "context_example": ctx or "",
                    "updated_at": now,
                }
            entry_link[e.id] = (cid, skey)
            # --- target term (when non-blank), no optional fields; existing = no-op -------
            tgt = (e.target_term or "").strip()
            if tgt:
                tkey = (cid, e.target_locale or "", e.target_term)
                if tkey not in term_by_key and tkey not in new_tgt_rows:
                    new_tgt_rows[tkey] = {
                        "concept_id": cid,
                        "locale": e.target_locale or "",
                        "term": e.target_term,
                        "updated_at": now,
                    }

        # write this batch's new terms (concepts already inserted in pass 1) with RETURNING, then
        # apply the concept/source-term field updates. Source and target inserts are SEPARATE
        # executemany calls: their column sets differ, and keeping each uniform + minimal keeps the
        # parameter footprint small for the bounded-memory bound. Omitted columns take their
        # Field/Column defaults; the DB assigns every id.
        for skey, tid in zip(
            new_src_rows.keys(),
            _insert_returning_ids(GlossaryTerm, list(new_src_rows.values())),
            strict=True,
        ):
            term_by_key[skey] = tid
        for tkey, tid in zip(
            new_tgt_rows.keys(),
            _insert_returning_ids(GlossaryTerm, list(new_tgt_rows.values())),
            strict=True,
        ):
            term_by_key[tkey] = tid
        _apply_updates(GlossaryConcept, concept_updates)
        _apply_updates(GlossaryTerm, src_updates)
        # link the plain entries by MUTATING the already-loaded ORM rows (concepts/terms now
        # exist, so the FK is satisfiable): SQLAlchemy dirties only a row whose value actually
        # changed, so an idempotent re-import touches nothing, and the batch flush below emits
        # one UPDATE executemany — no separate entry-row dicts to retain (bounded memory).
        for e in plain:
            cid, skey = entry_link[e.id]
            sid = term_by_key[skey]
            if e.concept_id != cid:
                e.concept_id = cid
            if e.term_id != sid:
                e.term_id = sid

        # --- sense-bearing entries: delegate to the proven per-entry path -------------
        # Now collision-free by construction: the pass above minted no ids, so the ORM
        # autoincrement inserts inside sync_entry_concept advance the same DB counter the next
        # batch's Core inserts read from (round-1 H1).
        for e in sense_entries:
            senses = session.exec(
                select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
            ).all()
            sync_entry_concept(
                session,
                e,
                _term_id_cache=term_by_key,
                _senses=senses,
                _concept_lookup=concept_by_src,
            )
        session.flush()

        # bound memory: detach everything but the caller's own rows.
        for obj in list(session):
            if id(obj) not in _caller_ids:
                session.expunge(obj)


def create_entry_bindings(
    session: Session,
    glossary_id: int,
    concept_ids: list[int],
    *,
    pairs: list[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """A.4-2 / A.4-8 binding creation (lives in the editor / set_pairs, NEVER the projector,
    R2-H5). For the BASE concept of every group touched by ``concept_ids``, create a
    ``GlossaryEntry`` binding for every usable SOURCE synonym in each configured pair that has
    no binding yet: a usable target -> a normal binding (``target_term_id`` set); a DNT source
    with no target -> a DNT-only binding (``target_term_id`` NULL). Concept-first: pins NO
    ``meta_overrides``. Deduped by ``(term_id, target_locale)`` and by the legacy key
    ``(source_locale, surface)`` per pair. Returns ``(bindings_created, dnt_bindings_extended)``
    (a DNT-only binding counts in the second). The caller flushes and projects the group."""
    if not concept_ids:
        return (0, 0)
    pairs = pairs if pairs is not None else _configured_pairs(session, glossary_id)
    if not pairs:
        return (0, 0)
    seen = {
        c.id: c
        for c in session.exec(
            select(GlossaryConcept).where(GlossaryConcept.id.in_(list(set(concept_ids))))
        ).all()
    }
    base_ids = {_group_base_id(session, glossary_id, c) for c in seen.values()}
    created = 0
    dnt_extended = 0
    for bid in base_ids:
        cterms = session.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == bid)).all()
        existing = session.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id == bid,
            )
        ).all()
        by_term_pair = {(e.term_id, e.target_locale or "") for e in existing}
        for sl, tl in pairs:
            claimed = {
                (e.source_locale or "", e.source_term)
                for e in existing
                if (e.target_locale or "") == (tl or "")
            }
            for src in _usable_source_terms(cterms, sl):
                src_exact = (src.locale or "") == (sl or "") or (src.locale or "") == ""
                eff_sl = (src.locale or "") if src_exact else (sl or "")
                if (src.id, tl or "") in by_term_pair:
                    continue
                if (eff_sl, src.term) in claimed:
                    continue
                tgt = _pick_target_term(cterms, tl, exclude_id=src.id, source_locale=sl)
                if tgt is None and not src.dnt:
                    continue
                e = GlossaryEntry(
                    glossary_id=glossary_id,
                    project_id=None,
                    source_locale=eff_sl,
                    target_locale=tl or "",
                    source_term=src.term,
                    target_term=tgt.term if tgt is not None else "",
                    dnt=bool(src.dnt),
                    case_sensitive=bool(src.case_sensitive),
                    meta=None,
                    concept_id=bid,
                    term_id=src.id,
                    target_term_id=tgt.id if tgt is not None else None,
                    meta_overrides=None,
                )
                session.add(e)
                session.flush()
                by_term_pair.add((src.id, tl or ""))
                claimed.add((eff_sl, src.term))
                if tgt is None:
                    dnt_extended += 1
                else:
                    created += 1
    return (created, dnt_extended)


def _repoint_off_target(
    session: Session, glossary_id: int, concept_id: int, lost_term_id: int
) -> tuple[int, list[int]]:
    """A.4-3 re-point on target loss: for every base binding of ``concept_id`` whose enforced
    target is ``lost_term_id`` (a demoted or about-to-be-deleted target term), re-point it to
    the next usable target in that pair; if none remains, a DNT binding becomes DNT-only
    (``target_term_id = NULL``, recorded) and a non-DNT binding makes the operation REFUSE.
    Returns ``(repointed, bindings_emptied)``; senses re-derive on the following projection."""
    cterms = [
        t
        for t in session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == concept_id)
        ).all()
        if t.id != lost_term_id
    ]
    lost = session.get(GlossaryTerm, lost_term_id)
    lost_surface = lost.term if lost is not None else ""
    repointed = 0
    emptied: list[int] = []
    for e in session.exec(
        select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == glossary_id,
            GlossaryEntry.concept_id == concept_id,
            GlossaryEntry.target_term_id == lost_term_id,
        )
    ).all():
        new = _pick_target_term(
            cterms, e.target_locale, exclude_id=e.term_id, source_locale=e.source_locale
        )
        if new is not None:
            e.target_term_id = new.id
            session.add(e)
            repointed += 1
        elif e.dnt:
            e.target_term_id = None
            session.add(e)
            emptied.append(e.id)
            log.info("glossary %s: binding %s emptied to DNT-only (target lost)", glossary_id, e.id)
        else:
            raise ConceptError(
                f"{lost_surface!r} is the only target for {e.source_term!r} -> "
                f"{e.target_locale!r}; delete the glossary row or add another target first."
            )
    return repointed, emptied


def set_audience(session: Session, glossary_id: int, term_ids: list[int], audience) -> int:
    """Q2 bulk action: set the audience of a set of the glossary's terms. Returns the
    count updated."""
    _require_concept_editor()
    canon = canon_audience(audience)
    n = 0
    for tid in term_ids or []:
        term = session.get(GlossaryTerm, tid)
        if term is None:
            continue
        concept = session.get(GlossaryConcept, term.concept_id)
        if concept is None or concept.glossary_id != glossary_id:
            continue
        term.audience = canon
        term.updated_at = _utcnow()
        session.add(term)
        n += 1
    if n:
        session.commit()
        _touch_draft(session, glossary_id)
    return n


def _add_term_row(session: Session, concept_id: int, tdoc: dict) -> GlossaryTerm:
    surface = tdoc.get("term")
    if not surface:
        raise ConceptError("a term needs a surface.")
    term = GlossaryTerm(concept_id=concept_id, term=surface, locale=(tdoc.get("locale") or ""))
    _apply_term_fields(term, tdoc)
    session.add(term)
    session.flush()
    return term
