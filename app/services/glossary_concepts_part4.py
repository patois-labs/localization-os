"""glossary_concepts: part 4 (layered pure move)

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.models import (
    GlossaryConcept,
    GlossaryEntry,
    GlossarySense,
    GlossaryTerm,
)
from app.services import glossary as gloss_engine
from app.services.glossary_concepts_common import (
    ConceptError,
    _closure_entry_ids,
    _derive_meta,
    _entry_deriv,
    _est_tokens,
    _lang_base_match,
    _overrides_two_way,
    _pair_binding_columns_present,
    _pick_target_term,
    _project_meta,
    _render_ordering_key,
    _retire_target_term,
    _source_term_of,
)
from app.services.glossary_concepts_part2 import (
    _assemble_block,
    _resolve_concept_meta,
    _resolve_target_term_id,
    delete_entry_unit,
    sync_entry_concept,
)
from app.services.glossary_concepts_part3 import (
    _bulk_sync_glossary_from_entries,
    _sync_glossary_from_entries,
)

log = logging.getLogger("loc_tms.glossary_concepts")


def backfill_glossary(session: Session) -> int:
    """One-time, idempotent, per-glossary-guarded backfill of the concept/term store from
    the existing compat entries (LAW-TERM-6; migration step 4 + create_all seed path).

    MODULE glossaries only (``glossary_id`` NOT NULL); legacy per-project entries are never
    touched (LAW-TERM-2a). A glossary that already has ANY concept is SKIPPED (per-glossary
    guard) so re-running is a no-op. Returns the number of glossaries backfilled."""
    gids = list(
        session.exec(
            select(GlossaryEntry.glossary_id)
            .where(GlossaryEntry.glossary_id.is_not(None))
            .distinct()
        ).all()
    )
    done = 0
    for gid in gids:
        has_concept = session.exec(
            select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == gid).limit(1)
        ).first()
        if has_concept is not None:
            continue  # per-glossary guard — already backfilled
        _sync_glossary_from_entries(session, gid)
        done += 1
    if done:
        # FLUSH, not commit — this runs both inside the alembic migration's transaction
        # (which alembic commits) AND from the create_all seed path (which commits after).
        session.flush()
    return done


def consolidate_split_concepts(session: Session) -> int:
    """One-time, idempotent REPAIR (R4): the earlier dual-write/backfill logic minted ONE
    CONCEPT PER COMPAT ENTRY, so a multi-language source surface (en->es AND en->fr for the
    same source term) was split across two UN-GROUPED concepts that shared the source surface
    -- the shape the R4 root fix eliminates and that made every collision rule fight the
    product's own data.

    This merges any store that was already split that way: within a module glossary, BASE
    concepts (the ones that OWN a compat ENTRY) that share
    ``(source_locale, casefold source_term)`` collapse into the lowest-id concept; every compat
    entry on the losers is re-pointed, the losers' terms + concept rows are deleted, and the
    glossary is reconciled so the ``term_id`` links, the merged target terms AND the
    homonym_group are rebuilt on the survivor.

    R5-4: the grouping is over ALL base concepts, NOT only the un-grouped ones — a split whose
    base carries a homonym_group (i.e. any source term that also has a SENSE) was previously
    left split forever. A source surface has exactly ONE base concept under the R4 model, so
    two base concepts sharing it are always a split to merge, whatever their homonym_group.
    Sense-only SIBLING concepts are never touched: they are referenced by ``sense.concept_id``,
    never ``entry.concept_id``, so grouping over base entries excludes them by construction. A
    store already consolidated (or freshly backfilled by the fixed logic) is a NO-OP.

    Runs in the migration and the create_all seed path AFTER :func:`backfill_glossary`.
    Returns the number of concepts merged away (logged, per LAW-TERM-6)."""
    from collections import defaultdict

    merged = 0
    gids = list(
        session.exec(
            select(GlossaryEntry.glossary_id)
            .where(GlossaryEntry.glossary_id.is_not(None))
            .distinct()
        ).all()
    )
    for gid in gids:
        # Group this glossary's BASE concepts (those owning a compat entry) by the R4 identity
        # key — regardless of homonym_group, so a sense-bearing split still merges (R5-4).
        groups: dict[tuple[str, str], set[int]] = defaultdict(set)
        for cid, sloc, sterm in session.exec(
            select(
                GlossaryEntry.concept_id,
                GlossaryEntry.source_locale,
                GlossaryEntry.source_term,
            ).where(
                GlossaryEntry.glossary_id == gid,
                GlossaryEntry.concept_id.is_not(None),
            )
        ).all():
            groups[((sloc or ""), (sterm or "").casefold())].add(cid)
        touched = False
        for cids in groups.values():
            if len(cids) < 2:
                continue
            canonical = min(cids)
            losers = sorted(cids - {canonical})
            # Re-point the losers' entries onto the survivor; NULL term_id so the reconcile
            # below rebuilds it against the survivor's (merged) terms.
            for e in session.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid,
                    GlossaryEntry.concept_id.in_(losers),
                )
            ).all():
                e.concept_id = canonical
                e.term_id = None
                e.target_term_id = None  # LAW-TERM-18: rebuilt by the reconcile + self-heal below
                session.add(e)
            session.flush()
            # Clear any self-FK to a loser term, then delete the loser terms + concepts.
            loser_term_ids = list(
                session.exec(
                    select(GlossaryTerm.id).where(GlossaryTerm.concept_id.in_(losers))
                ).all()
            )
            if loser_term_ids:
                for t in session.exec(
                    select(GlossaryTerm).where(
                        GlossaryTerm.deprecated_replacement_term_id.in_(loser_term_ids)
                    )
                ).all():
                    t.deprecated_replacement_term_id = None
                    session.add(t)
                # LAW-TERM-18: NULL any binding's target_term_id pointing at a loser term (incl.
                # the SURVIVOR's own entries), so deleting the loser terms cannot trip the FK; the
                # projector self-heals target_term_id from the surviving surface (A.2-c).
                for e in session.exec(
                    select(GlossaryEntry).where(
                        GlossaryEntry.glossary_id == gid,
                        GlossaryEntry.target_term_id.in_(loser_term_ids),
                    )
                ).all():
                    e.target_term_id = None
                    session.add(e)
                for sn in session.exec(
                    select(GlossarySense).where(GlossarySense.term_id.in_(loser_term_ids))
                ).all():
                    sn.term_id = None
                    session.add(sn)
                session.flush()
                for t in session.exec(
                    select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(losers))
                ).all():
                    session.delete(t)
                session.flush()
            for lc in losers:
                obj = session.get(GlossaryConcept, lc)
                if obj is not None:
                    session.delete(obj)
                    merged += 1
            session.flush()
            touched = True
        if touched:
            # Rebuild term_id links + merged target terms on the survivors (idempotent).
            _sync_glossary_from_entries(session, gid)
            # L-4: the re-pointed survivors had target_term_id NULLed above; re-resolve them now
            # (A.4-7) rather than leaving un-backfilled rows for a later projection/boot to heal.
            backfill_pair_bindings(session, gid)
    if merged:
        log.info("consolidate_split_concepts merged %d split concept(s)", merged)
        session.flush()
    return merged


def reconcile_glossary_from_entries(
    session: Session,
    glossary_id: int,
    *,
    entry_ids: list[int] | None = None,
    cache: dict | None = None,
) -> None:
    """Public seam for the bulk importer (glossary._apply_rows): after the entries of a
    module glossary are written, mirror the concept/term store from them IN THE SAME
    transaction (LAW-TERM-2). Unguarded upsert (unlike backfill), so an incremental import
    that added/changed entries reconciles them. The caller owns the commit.

    Uses the LINEAR bulk builder (:func:`_bulk_sync_glossary_from_entries`) — byte-identical
    store to the per-entry :func:`sync_entry_concept`, with no per-row ORM construction/flush
    and no O(n^2) source-surface scan (batched RETURNING inserts on PostgreSQL, per-row on
    SQLite; the backfill / consolidate boot paths keep the per-entry
    :func:`_sync_glossary_from_entries`).

    ``entry_ids`` (#615): reconcile only those entries, for one chunk of a big import; the
    default is the whole glossary, as before. ``cache`` (a dict the caller keeps for the whole
    import) carries the source-surface identity map between chunks so it is not rebuilt."""
    _bulk_sync_glossary_from_entries(session, glossary_id, entry_ids, cache)


def bulk_bind_touched(session: Session, glossary_id: int, touched_ids: set[int]) -> int:
    """A.2-d bulk-adapter tail (the NAMED EXEMPTION): after ``_apply_rows`` wrote the derived
    columns directly and ``reconcile_glossary_from_entries`` linked the concepts/terms, resolve
    ``target_term_id`` for every TOUCHED row (re-pointing a moved target), pin ``meta_overrides``
    by A.3's two-way rule (touched rows only, never an untouched row -- R7-A), retire a superseded
    target by A.2-f, then ``project(entry_ids=touched, verify=True)`` to assert byte-identity.
    Returns ``bindings_pinned`` (rows whose ``meta_overrides`` gained a key, r6-12). The caller
    owns the transaction/savepoint; a real divergence raises ``ConceptError``."""
    if not touched_ids:
        return 0
    from app.services.glossary import GLOSSARY_IMPORT_FLUSH_BATCH as _FLUSH

    # 100 entries per pass (not the 500 flush batch): the batched concept/term prefetch below keeps
    # one pass of ORM rows live, and the import is held to a peak-heap bound
    # (verify_glossary_import_bounded_memory) as well as a short lock (#597).
    _BATCH = min(_FLUSH, 100)

    # Expunge discipline (bounded memory, mirrors _sync_glossary_from_entries): detach only the
    # rows THIS pass loads/creates after each chunk, never the caller's (the import route's
    # Glossary row relies on expire-on-commit), so live ORM state stays O(batch).
    _caller_ids = {id(o) for o in session}
    ordered = sorted(touched_ids)
    superseded: set[int] = set()
    pinned = 0
    for i in range(0, len(ordered), _BATCH):
        chunk = ordered[i : i + _BATCH]
        batch = session.exec(select(GlossaryEntry).where(GlossaryEntry.id.in_(chunk))).all()
        # #597: one query per table for the whole batch instead of 3 per entry. The concepts
        # and source terms land in the identity map, so _entry_deriv's session.get() calls
        # are hits; the concept's terms are handed to _resolve_target_term_id directly.
        cids = sorted({e.concept_id for e in batch if e.concept_id is not None})
        tids = sorted({e.term_id for e in batch if e.term_id is not None})
        # (held in locals: the identity map is weak-referencing, so unreferenced rows would be
        # collected and the gets below would query again)
        held_concepts = (
            session.exec(select(GlossaryConcept).where(GlossaryConcept.id.in_(cids))).all()
            if cids
            else []
        )
        held_terms = (
            session.exec(select(GlossaryTerm).where(GlossaryTerm.id.in_(tids))).all()
            if tids
            else []
        )
        terms_by_concept: dict[int, list] = {c: [] for c in cids}
        if cids:
            for t in session.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(cids))):
                terms_by_concept[t.concept_id].append(t)
        for e in batch:
            if e.glossary_id is None or e.concept_id is None or e.term_id is None:
                continue
            deriv = _entry_deriv(session, e)
            old_ttid = e.target_term_id
            old_ov = e.meta_overrides
            new_ttid = _resolve_target_term_id(
                session,
                e.concept_id,
                e.target_locale or "",
                e.target_term or "",
                exclude_id=e.term_id,
                cterms=terms_by_concept.get(e.concept_id),
            )
            e.target_term_id = new_ttid
            e.meta_overrides = _overrides_two_way(e.meta, deriv)
            session.add(e)
            if old_ov is None and e.meta_overrides is not None:
                pinned += 1
            if old_ttid is not None and old_ttid != new_ttid:
                superseded.add(old_ttid)
        session.flush()
        del held_concepts, held_terms
        for obj in list(session):
            if id(obj) not in _caller_ids:
                session.expunge(obj)
    for old in superseded:
        _retire_target_term(session, old)
    project(session, glossary_id, entry_ids=ordered, verify=True)
    return pinned


def project(
    session: Session,
    glossary_id: int,
    *,
    entry_ids: list[int] | None = None,
    concept_ids: list[int] | None = None,
    verify: bool = False,
    derive_senses: bool = False,
) -> dict:
    """LAW-TERM-18 PROJECTOR: the concept store is the single authority; the compat
    ``glossaryentry``/``glossarysense`` rows are a pure PROJECTION of the per-pair BINDINGS.
    This is the SOLE writer of the DERIVED columns (``source_term``/``source_locale``/
    ``target_term``/``meta`` on entries; ``sense_label``/``context_cue``/``target_term`` on
    senses). The binding-owned columns (``concept_id``/``term_id``/``target_term_id``/
    ``target_locale``/``dnt``/``case_sensitive``/``meta_overrides`` on the entry;
    ``concept_id``/``term_id`` on the sense) are written by the adapters / editor / import --
    the ONE exception is the A.2-c inline self-heal, which fills ``target_term_id`` and pins
    ``meta_overrides`` on an un-backfilled row it meets in WRITE mode (L-3).

    NEVER deletes a row and never creates an ENTRY binding (both live in the adapters / editor
    / import). Sense rows ARE derived: a stored sense is UPDATED in place from its sibling
    concept, and a group sibling holding a usable target with no stored sense on a base row
    gains one (DIFF-1). The caller owns the commit.

    Scope: ``entry_ids`` (touched base bindings) and/or ``concept_ids`` (touched concepts --
    the only way to NAME a touched sibling, whose bindings are senses), closed over the
    HOMONYM GROUP (§A.1). ``None`` for BOTH = the whole glossary (streamed). An empty list for
    either is a ``ValueError``.

    ``verify=True`` writes NOTHING: it computes what it WOULD write for the touched ENTRY
    columns (``source_term``/``source_locale``/``target_term``/``meta`` as a parsed dict) and
    compares against the stored row, raising ``ConceptError`` (naming the binding + column) on
    any real difference so the bulk importer's savepoint rolls back byte-identically. Returns
    ``{"bindings_verified": n}`` in verify mode, ``{"projected": n}`` otherwise."""
    from app.services.glossary import GLOSSARY_IMPORT_FLUSH_BATCH as _BATCH

    if entry_ids is None and concept_ids is None:
        scope_ids: set[int] | None = None
    elif verify:
        # A.2-d: VERIFY compares ONLY the TOUCHED rows, never the group closure -- an untouched
        # pre-existing binding's override MAP may legitimately differ (the derivation is not
        # frozen; a later pair's legacy write can add a shared-concept key the untouched row
        # never tombstoned), yet its STORED derived columns are already byte-identical to main
        # because the bulk path never rewrote them. Comparing the closure would abort a valid
        # import (R7-A's class); the projection-equivalence test proves LAW-TERM-1 via backfill.
        if entry_ids is not None and len(entry_ids) == 0:
            raise ValueError("project(verify): entry_ids=[]")
        scope_ids = set(entry_ids or [])
        if not scope_ids:
            return {"bindings_verified": 0}
    else:
        scope_ids, _groups = _closure_entry_ids(session, glossary_id, entry_ids, concept_ids)
        if not scope_ids:
            return {"bindings_verified": 0} if verify else {"projected": 0}

    verified = 0
    projected = 0
    # legacy-key uniqueness accumulator (source_term, source_locale, target_locale) -> entry id.
    legacy_keys: dict[tuple[str, str, str], int] = {}

    def _project_batch(entries: list[GlossaryEntry]) -> None:
        nonlocal verified, projected
        if not entries:
            return
        cids = {e.concept_id for e in entries if e.concept_id is not None}
        term_ids = {e.term_id for e in entries if e.term_id is not None}
        term_ids |= {e.target_term_id for e in entries if e.target_term_id is not None}
        # load every term of every concept in the batch (senses need the sibling's terms).
        concepts: dict[int, GlossaryConcept] = {}
        if cids:
            for c in session.exec(
                select(GlossaryConcept).where(GlossaryConcept.id.in_(list(cids)))
            ).all():
                concepts[c.id] = c
        terms_by_concept: dict[int, list[GlossaryTerm]] = {}
        terms_by_id: dict[int, GlossaryTerm] = {}
        # VERIFY compares ENTRY columns only, so it needs just the source + target TERMS
        # (terms_by_id), never every term of every concept (terms_by_concept, which the sense
        # derivation needs) -- loading the latter in verify made a dense module import unbounded.
        if cids and not verify:
            for t in session.exec(
                select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(list(cids)))
            ).all():
                terms_by_concept.setdefault(t.concept_id, []).append(t)
                terms_by_id[t.id] = t
        # the specific source + target terms every entry needs (all of them, in verify mode).
        missing = [tid for tid in term_ids if tid not in terms_by_id]
        if missing:
            for t in session.exec(select(GlossaryTerm).where(GlossaryTerm.id.in_(missing))).all():
                terms_by_id[t.id] = t
        eids = [e.id for e in entries]
        senses_by_entry: dict[int, list[GlossarySense]] = {}
        if eids and not verify:
            for s in session.exec(
                select(GlossarySense)
                .where(GlossarySense.glossary_entry_id.in_(eids))
                .order_by(GlossarySense.id)
            ).all():
                senses_by_entry.setdefault(s.glossary_entry_id, []).append(s)

        for e in entries:
            concept = concepts.get(e.concept_id)
            src = terms_by_id.get(e.term_id) if e.term_id else None
            # H-3 (A.2-a "never blank"): a row the projector CANNOT derive -- concept_id NULL,
            # term_id NULL, or its source term missing -- is SKIPPED with a WARNING, leaving
            # every derived column exactly as stored, in WRITE and VERIFY alike. Main preserved
            # such a row; blanking it to src->'' is the E-T1-R3-1 fail-open. The A.5 boot
            # invariant heals it (concept_id/term_id NULL self-heal via sync_entry_concept);
            # here we merely refuse to destroy it.
            if e.concept_id is None or e.term_id is None or src is None:
                log.warning(
                    "project: binding %s not derivable (concept_id=%r term_id=%r src=%r) "
                    "-- left as stored",
                    e.id,
                    e.concept_id,
                    e.term_id,
                    None if src is None else src.id,
                )
                continue
            # A.2-c self-heal (WRITE mode): an un-backfilled row (target_term_id NULL AND
            # target_term != '') met by a projection is backfilled inline before deriving, so
            # a T1-linked-but-not-pair-bound store (backfill_glossary output) reproduces its
            # target instead of projecting "". A genuinely empty-target binding (target_term
            # blank) is left NULL. Pins meta_overrides by the two-way rule where still NULL.
            if (
                not verify
                and e.target_term_id is None
                and (e.target_term or "").strip()
                and e.concept_id is not None
                and e.term_id is not None
            ):
                healed = _resolve_target_term_id(
                    session,
                    e.concept_id,
                    e.target_locale or "",
                    e.target_term or "",
                    exclude_id=e.term_id,
                )
                if healed is not None:
                    e.target_term_id = healed
                    if healed not in terms_by_id:
                        ht = session.get(GlossaryTerm, healed)
                        if ht is not None:
                            terms_by_id[healed] = ht
                if e.meta_overrides is None:
                    e.meta_overrides = _overrides_two_way(
                        e.meta, _derive_meta(concept, src) if (concept and src) else None
                    )
            # --- derived source columns (with the round-2 H1 base-fallback promotion) ---
            new_source_term = e.source_term
            new_source_locale = e.source_locale
            if src is not None:
                new_source_term = src.term
                t_loc = src.locale or ""
                if not (
                    t_loc
                    and e.source_locale
                    and t_loc != e.source_locale
                    and _lang_base_match(t_loc, e.source_locale)
                ):
                    new_source_locale = t_loc
            # --- derived target column (from the enforced-target pointer) ---
            if e.target_term_id:
                tgt = terms_by_id.get(e.target_term_id)
                new_target_term = tgt.term if tgt is not None else ""
            else:
                new_target_term = ""
            # --- derived meta (from meta_overrides over the concept/source-term derivation) ---
            deriv = (
                _derive_meta(concept, src) if (concept is not None and src is not None) else None
            )
            new_meta = _project_meta(e.meta_overrides, deriv)

            if verify:
                # A.2-d guard: an un-backfilled row (target_term_id NULL AND target_term != '')
                # is SKIPPED with a WARNING, never a mismatch -- the bulk sync must resolve
                # target_term_id for every row it touches, so this can only be a pre-T1b row the
                # sync UPDATED without resolving; §B asserts ZERO such warnings on the corpora.
                if e.target_term_id is None and (e.target_term or ""):
                    log.warning(
                        "project(verify): binding %s un-backfilled (target_term_id NULL, "
                        "target_term=%r) -- skipped",
                        e.id,
                        e.target_term,
                    )
                    continue
                mism = None
                if (e.source_term or "") != (new_source_term or ""):
                    mism = ("source_term", e.source_term, new_source_term)
                elif (e.source_locale or "") != (new_source_locale or ""):
                    mism = ("source_locale", e.source_locale, new_source_locale)
                elif (e.target_term or "") != (new_target_term or ""):
                    mism = ("target_term", e.target_term, new_target_term)
                elif (e.meta or None) != (new_meta or None):
                    mism = ("meta", e.meta, new_meta)
                if mism is not None:
                    col, stored, would = mism
                    raise ConceptError(
                        f"binding {e.id}: {col} would be {would!r} but importer wrote {stored!r}"
                    )
                verified += 1
            else:
                e.source_term = new_source_term
                e.source_locale = new_source_locale
                e.target_term = new_target_term
                e.meta = new_meta
                session.add(e)
                projected += 1
                # legacy-key uniqueness (checked after the whole projection).
                lk = (e.source_term or "", e.source_locale or "", e.target_locale or "")
                if lk in legacy_keys and legacy_keys[lk] != e.id:
                    raise ConceptError(
                        f"two concepts would both enforce the source term {lk[0]!r} "
                        f"({lk[1]!r} -> {lk[2]!r}); group them with a shared homonym_group or "
                        f"reduce them to one concept (glossary entries {legacy_keys[lk]} "
                        f"and {e.id})"
                    )
                legacy_keys[lk] = e.id

            # --- senses: update stored ones in place; derive fresh group siblings (DIFF-1) ---
            # Sense MAINTENANCE (update-in-place + DIFF-1) is a concept-editor / import act only
            # (``derive_senses``). The legacy adapters are LINK-ONLY on senses -- the stored
            # add_sense rows keep their verbatim target_term/label (byte-identical to main, which
            # never rewrites a stored sense on a legacy reconcile). VERIFY compares entry columns
            # only. R7-E (sense staleness) is closed on the editor/import path, not the bulk path.
            if verify or not derive_senses:
                continue
            stored = senses_by_entry.get(e.id, [])
            seen_sibs: set[int] = set()
            for sn in stored:
                sib = concepts.get(sn.concept_id) if sn.concept_id else None
                if sib is None and sn.concept_id:
                    sib = session.get(GlossaryConcept, sn.concept_id)
                if sib is None:
                    continue  # a concept_id-less legacy sense heals via sync_entry_concept
                seen_sibs.add(sib.id)
                sib_terms = terms_by_concept.get(sib.id)
                if sib_terms is None:
                    sib_terms = session.exec(
                        select(GlossaryTerm).where(GlossaryTerm.concept_id == sib.id)
                    ).all()
                    terms_by_concept[sib.id] = sib_terms
                sib_src = _source_term_of(sib_terms, new_source_locale, new_source_term)
                sib_tgt = _pick_target_term(
                    sib_terms,
                    e.target_locale,
                    exclude_id=sib_src.id if sib_src is not None else None,
                    source_locale=new_source_locale,
                )
                sn.sense_label = sib.domain
                sn.context_cue = sib.definition
                sn.target_term = sib_tgt.term if sib_tgt is not None else ""
                sn.term_id = sib_tgt.id if sib_tgt is not None else None
                session.add(sn)
            # DIFF-1: a group sibling holding a usable target with no stored sense on this base
            # row gains one -- reachable ONLY through the concept editor / a document import
            # (``derive_senses``), NEVER the legacy adapters (add_term/edit_term/CSV), whose
            # senses are the STORED add_sense rows updated in place. The legacy fuzz's senses are
            # thus byte-identical to main (which materialises fresh sibling senses only in narrow
            # timing windows); DIFF-1 fan-out is the declared concept-editor/import change.
            if derive_senses and concept is not None and concept.homonym_group:
                sibs = concepts_by_group.get(concept.homonym_group, [])
                for sib in sibs:
                    if sib.id == concept.id or sib.id in seen_sibs:
                        continue
                    if sib.id in concepts_with_entries:
                        continue  # enforced by its OWN base entry, not derived as a sense here
                    sib_terms = terms_by_concept.get(sib.id)
                    if sib_terms is None:
                        sib_terms = session.exec(
                            select(GlossaryTerm).where(GlossaryTerm.concept_id == sib.id)
                        ).all()
                        terms_by_concept[sib.id] = sib_terms
                    sib_src = _source_term_of(sib_terms, new_source_locale, new_source_term)
                    sib_tgt = _pick_target_term(
                        sib_terms,
                        e.target_locale,
                        exclude_id=sib_src.id if sib_src is not None else None,
                        source_locale=new_source_locale,
                    )
                    if sib_tgt is None:
                        continue  # a homeless sibling target (A.1): no entry, no sense
                    session.add(
                        GlossarySense(
                            glossary_entry_id=e.id,
                            sense_label=sib.domain,
                            context_cue=sib.definition,
                            target_term=sib_tgt.term,
                            concept_id=sib.id,
                            term_id=sib_tgt.id,
                        )
                    )
                    seen_sibs.add(sib.id)

    # sibling concepts by homonym group, for the DIFF-1 fresh-sense pass (write mode only).
    concepts_by_group: dict[str, list[GlossaryConcept]] = {}
    # concept ids that own a LIVE base entry: a co-grouped concept enforced by its own entry is
    # NEVER also derived as a sense on the base row (it would double-enforce; main projects it as
    # an entry only -- the LT2-4 co-grouped-base shape). Only sense-only siblings get a sense.
    concepts_with_entries: set[int] = set()
    if not verify:
        for c in session.exec(
            select(GlossaryConcept).where(
                GlossaryConcept.glossary_id == glossary_id,
                GlossaryConcept.homonym_group != "",
            )
        ).all():
            concepts_by_group.setdefault(c.homonym_group, []).append(c)
        for cid in session.exec(
            select(GlossaryEntry.concept_id).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id.is_not(None),
            )
        ).all():
            concepts_with_entries.add(cid)

    # Expunge discipline (bounded memory): detach only rows THIS projection loads after each
    # batch, never the caller's (a web/import route relies on expire-on-commit for its own rows).
    _caller_ids = {id(o) for o in session}

    def _shrink() -> None:
        for obj in list(session):
            if id(obj) not in _caller_ids:
                session.expunge(obj)

    if scope_ids is None:
        # whole glossary: stream by id in batches.
        last_id = 0
        while True:
            batch = session.exec(
                select(GlossaryEntry)
                .where(GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.id > last_id)
                .order_by(GlossaryEntry.id)
                .limit(_BATCH)
            ).all()
            if not batch:
                break
            last_id = batch[-1].id
            _project_batch(batch)
            session.flush()
            _shrink()
    else:
        # VERIFY holds only column comparisons, but each batch materialises entry + concept +
        # term ORM rows; use a SMALLER batch there so a whole-glossary bulk import's verify tail
        # stays well within the bounded-memory ceiling (the write path keeps the flush batch).
        step = 100 if verify else _BATCH
        ordered = sorted(scope_ids)
        for i in range(0, len(ordered), step):
            chunk = ordered[i : i + step]
            batch = session.exec(
                select(GlossaryEntry).where(GlossaryEntry.id.in_(chunk)).order_by(GlossaryEntry.id)
            ).all()
            _project_batch(batch)
            session.flush()
            _shrink()

    return {"bindings_verified": verified} if verify else {"projected": projected}


def backfill_pair_bindings(
    session: Session, glossary_id: int | None = None, *, entry_ids: list[int] | None = None
) -> int:
    """A.5 backfill: populate ``target_term_id``/``meta_overrides`` on EXISTING module
    entries so the projector reproduces the stored compat row. NEW function with its OWN
    versioned completion marker (``_PAIR_BINDINGS_MARKER``, checked in db.py); NOT routed
    through :func:`backfill_glossary`. Granularity: ``glossary_id=None`` -> all module
    glossaries; an int -> one glossary; ``entry_ids`` -> those rows (the A.2-c self-heal).

    WRITES ONLY NULLS (r7-1): a row that already CARRIES ``target_term_id`` OR
    ``meta_overrides`` is left exactly as it is -- so it never re-pins a concept-first binding
    or a key ``update_entry`` deliberately left unpinned, and never re-points a chosen target.
    For a to-fill row: if ``concept_id IS NULL`` run the T1 backfill first
    (:func:`sync_entry_concept`);
    resolve ``target_term_id`` by A.4-7 (blank target -> NULL); compute ``meta_overrides`` from
    the row's current ``meta`` by A.3's TWO-WAY rule so projection reproduces the stored dict.
    Senses need no target backfill (``term_id`` already points at the sibling's target). Creates
    NO bindings, NO rows. Idempotent. No-ops with a WARNING when the columns are absent
    (Postgres pre-migration). Returns the number of entries backfilled."""
    if not _pair_binding_columns_present(session):
        log.warning(
            "backfill_pair_bindings: glossaryentry.target_term_id/meta_overrides absent "
            "(pre-migration DB) -- no-op."
        )
        return 0
    from app.services.glossary import GLOSSARY_IMPORT_FLUSH_BATCH as _BATCH

    # r7-1: only a row carrying NEITHER column is a candidate. The NULL check is done in PYTHON
    # (via the ORM value): a JSON column stores Python None as JSON ``null`` text, not SQL NULL,
    # so a SQL ``meta_overrides IS NULL`` filter would never match a None-valued row.
    #
    # L-2: STREAM by id in GLOSSARY_IMPORT_FLUSH_BATCH chunks with expunge discipline (A.5 runs
    # this install-wide, so an unbatched .all() over every module entry was unbounded). Detach
    # only rows THIS backfill loads after each chunk, never the caller's own session objects.
    _caller_ids = {id(o) for o in session}

    def _shrink() -> None:
        for obj in list(session):
            if id(obj) not in _caller_ids:
                session.expunge(obj)

    n = 0
    last_id = 0
    while True:
        stmt = select(GlossaryEntry).where(
            GlossaryEntry.glossary_id.is_not(None), GlossaryEntry.id > last_id
        )
        if glossary_id is not None:
            stmt = stmt.where(GlossaryEntry.glossary_id == glossary_id)
        if entry_ids is not None:
            if not entry_ids:
                return 0
            stmt = stmt.where(GlossaryEntry.id.in_(list(entry_ids)))
        batch = session.exec(stmt.order_by(GlossaryEntry.id).limit(_BATCH)).all()
        if not batch:
            break
        last_id = batch[-1].id
        for e in batch:
            if e.target_term_id is not None or e.meta_overrides is not None:
                continue
            if e.concept_id is None or e.term_id is None:
                sync_entry_concept(session, e)  # T1 backfill: link concept + terms first
                session.flush()
            if e.concept_id is None or e.term_id is None:
                continue  # a legacy per-project shape that never links (defensive)
            ttid = _resolve_target_term_id(
                session,
                e.concept_id,
                e.target_locale or "",
                e.target_term or "",
                exclude_id=e.term_id,
            )
            deriv = _entry_deriv(session, e)
            ov = _overrides_two_way(e.meta, deriv)
            changed = False
            if e.target_term_id is None and ttid is not None:
                e.target_term_id = ttid
                changed = True
            if e.meta_overrides is None and ov is not None:
                e.meta_overrides = ov
                changed = True
            if changed:
                session.add(e)
                n += 1
        session.flush()
        _shrink()
    return n


def apply_entry_binding(
    session: Session, entry: GlossaryEntry, *, meta_overrides: dict | None
) -> None:
    """The thin-adapter tail (A.2-d): AFTER the concept/source/target terms + senses are
    synced (:func:`sync_entry_concept`) and the entry's derived columns seeded, resolve the
    binding's ``target_term_id`` by A.4-7, pin ``meta_overrides`` per the writer class (the
    caller computed it), retire a superseded target (A.2-f), and flush. The caller then calls
    :func:`project` scoped to this entry. Module entries only (a legacy per-project entry has
    no concept)."""
    if entry.concept_id is None or entry.term_id is None:
        return
    old_ttid = entry.target_term_id
    new_ttid = _resolve_target_term_id(
        session,
        entry.concept_id,
        entry.target_locale or "",
        entry.target_term or "",
        exclude_id=entry.term_id,
    )
    entry.target_term_id = new_ttid
    entry.meta_overrides = meta_overrides
    session.add(entry)
    session.flush()
    if old_ttid is not None and old_ttid != new_ttid:
        _retire_target_term(session, old_ttid)


def _delete_entry_and_unit(session: Session, glossary_id: int, entry: GlossaryEntry) -> None:
    """Delete ONE compat entry + its senses, flush, then run the LAW-TERM-2b compat-ENTRY
    delete unit — the shared body of glossary_module.remove_term and delete_concept (base
    case). Captures the link values BEFORE the flush (which may expire the deleted row),
    flushes senses before the entry (no ORM relationship orders the sense→entry edge under
    SQLite `PRAGMA foreign_keys=ON`), and never commits (the caller owns the transaction)."""
    concept_id, term_id, target_locale = entry.concept_id, entry.term_id, entry.target_locale
    for s in session.exec(
        select(GlossarySense).where(GlossarySense.glossary_entry_id == entry.id)
    ).all():
        session.delete(s)
    session.flush()
    session.delete(entry)
    session.flush()
    delete_entry_unit(session, glossary_id, concept_id, term_id, target_locale)


# --- concept-aware match selector (LAW-TERM-1/H6) -------------------------------------


def match_concepts(
    session: Session,
    project_id: int,
    segment_text: str,
    source_locale: str,
    target_locale: str,
    provider=None,
    glossary_id: int | None = None,
    disambig: str | None = None,
) -> list[dict]:
    """The concept-aware selector (SPEC §5.3/H6). Reuses ``glossary``'s shared selection
    core, so its five-key compat projection is byte-identical to ``match_terms`` (the
    §9-T1 non-divergence test). Each result is the five-key compat dict PLUS a detached
    ``_concept`` dict (plain values, never live ORM rows — M7) carrying the backing
    concept/term metadata the T3 renderer needs; a legacy/unlinked match has
    ``_concept`` = None (renderer falls back to the base term)."""
    entries = gloss_engine._select_matched_entries(
        session, project_id, segment_text, source_locale, target_locale, glossary_id
    )
    out: list[dict] = []
    for e in entries:
        emitted, chosen, provider = gloss_engine._emit_for_entry(
            session, e, segment_text, provider, disambig, project_id=project_id
        )
        if emitted is None:
            continue
        emitted = dict(emitted)
        emitted["_concept"] = _resolve_concept_meta(session, e, chosen)
        out.append(emitted)
    return out


def render_for_llm(
    concepts: list[dict],
    *,
    concept_cap: int = 10,
    token_cap: int = 1500,
) -> tuple[str, list[dict]]:
    """Render matched concepts to the Markdown prompt block AND return the exact
    ``shown_terms`` (five-key compat dicts) that survived ordering + caps (LAW-TERM-13/15).

    T1 builds and tests this directly; T3 wires it behind the per-step toggle. Inputs are
    the (already audience-filtered) ``match_concepts`` dicts. Ordering: DNT/forbidden
    first, then longest source term. Caps: at most ``concept_cap`` concepts, then drop
    whole concepts from the tail until the block PLUS its ``(+N more...)`` marker fits
    ``token_cap`` (L4 — the marker is counted inside the cap)."""
    ordered = sorted(concepts, key=_render_ordering_key)
    if not ordered:
        # L1 (gate round-1): ON with zero renderable concepts (nothing matched, or every
        # match dropped by the audience filter) emits NOTHING — never a bare
        # "Terminology (apply exactly):" header with no terms under it. The caller's OFF
        # path renders glossary_line's "(none)"; a toggle-ON step with no terms to show
        # simply injects no glossary block (shown_terms empty).
        return "", []
    if concept_cap and len(ordered) > concept_cap:
        ordered = ordered[:concept_cap]
    kept = list(ordered)
    while kept:
        block = _assemble_block(kept, omitted=len(concepts) - len(kept))
        if not token_cap or _est_tokens(block) <= token_cap:
            break
        kept = kept[:-1]
    block = _assemble_block(kept, omitted=len(concepts) - len(kept))
    # L4 (gate round-1): when even the header + the `(+N more…)` marker over an EMPTY kept
    # set exceeds token_cap, emit NOTHING — LAW-TERM-13 forbids any prompt path over the
    # cap.
    if token_cap and _est_tokens(block) > token_cap:
        return "", []
    _five = ("source_term", "target_term", "sense_label", "dnt", "case_sensitive")
    shown = [{k: m[k] for k in _five} for m in kept]
    return block, shown
