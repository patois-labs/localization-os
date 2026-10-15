"""glossary_concepts: part 2 (layered pure move)

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import (
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryTerm,
    TermAudience,
    TermStatus,
)
from app.services.glossary_concepts_common import (
    _META_ORDER,
    SCHEMA_V1,
    ConceptError,
    _clear_inbound_replacements,
    _doc_concept_id,
    _drop_concept_terms,
    _find_concept_for_source,
    _flatten_surface,
    _group_base_id,
    _homonym_group,
    _lang_base_match,
    _overrides_two_way,
    _pick_target_term,
    _replacement_surface,
    _sanitize,
    _source_locales,
    _term_referenced_elsewhere,
    _term_to_v1,
    _upsert_term,
    _utcnow,
    _valid_status,
    _valid_term_type,
    canon_audience,
)


def sync_entry_concept(
    session: Session,
    entry: GlossaryEntry,
    *,
    _term_id_cache: dict | None = None,
    _senses: list | None = None,
    _concept_lookup: dict | None = None,
) -> GlossaryConcept | None:
    """Idempotent upsert of the concept/term store backing ONE compat entry — the
    per-entry unit of the dual-write and the backfill (LAW-TERM-2/6). Entry-authoritative:
    the entry row is written by the caller UNCHANGED; this derives its backing concept +
    source/target terms + sense-homonym siblings and links them.

    R4 root fix: the concept is identified by (glossary_id, source_locale, source_term) so a
    multi-language term (en->es AND en->fr for one source surface) becomes ONE concept whose
    target terms are the es and fr surfaces — NOT two un-grouped concepts sharing the source
    surface (the shape that made every collision rule fight the product's own data).

    A legacy per-project entry (``glossary_id`` NULL) has NO concept (LAW-TERM-2a) and is
    skipped. Callers must have flushed the entry so ``entry.id`` is available."""
    if entry.glossary_id is None:  # legacy per-project entry — never linked (LAW-TERM-2a)
        return None
    meta = entry.meta or {}
    # --- base concept: reuse the linked one, else the concept OWNING this source surface,
    # else create a new one (R4 shared-identity) ---
    concept = session.get(GlossaryConcept, entry.concept_id) if entry.concept_id else None
    if concept is None:
        concept = _find_concept_for_source(
            session,
            entry.glossary_id,
            entry.source_locale or "",
            entry.source_term,
            cache=_concept_lookup,
        )
    if concept is None:
        concept = GlossaryConcept(glossary_id=entry.glossary_id)
        session.add(concept)
        session.flush()
        if _concept_lookup is not None:
            _concept_lookup[(entry.source_locale or "", entry.source_term.casefold())] = concept.id
    # Concept-level meta is per SOURCE SURFACE (shared across its locale pairs). Only set a
    # key when this entry actually carries it, so a second pair's blank meta cannot wipe the
    # first pair's definition on a shared concept.
    if meta.get("definition"):
        concept.definition = _sanitize(meta["definition"])
    if meta.get("subject_field"):
        concept.domain = _sanitize(meta["subject_field"])
    if meta.get("notes"):
        concept.notes = _sanitize(meta["notes"])
    # R2-2 + R3-3 (gate rounds 2/3): PERSIST a GLOBALLY-UNIQUE document identity (uuid4, NOT
    # a row id) so exporting then re-importing the file upserts by (glossary_id,
    # external_id) instead of duplicating — and so importing ANOTHER install's export can
    # never alias a local concept (dense row ids collide across installs; uuids do not).
    # Stamped once; an operator-set id is never overwritten.
    if not concept.external_id:
        concept.external_id = str(uuid.uuid4())
    concept.updated_at = _utcnow()
    session.flush()
    entry.concept_id = concept.id
    # --- source term (entry.term_id points here). It is SHARED across the concept's locale
    # pairs, so pass pos/ctx only when THIS entry carries them (None = no-clobber), and
    # never pass status/audience (M1). dnt/case_sensitive mirror the entry.
    src = _upsert_term(
        session,
        concept.id,
        entry.source_locale or "",
        entry.source_term,
        dnt=bool(entry.dnt),
        case_sensitive=bool(entry.case_sensitive),
        part_of_speech=(_sanitize(meta["part_of_speech"]) if meta.get("part_of_speech") else None),
        context_example=(_sanitize(meta["usage_example"]) if meta.get("usage_example") else None),
        cache=_term_id_cache,
    )
    entry.term_id = src.id
    # --- target term (when non-blank) ---
    if (entry.target_term or "").strip():
        _upsert_term(
            session,
            concept.id,
            entry.target_locale or "",
            entry.target_term,
            cache=_term_id_cache,
        )
    # --- senses -> homonym-sibling concepts (§4.5, H2) ---
    senses = (
        _senses
        if _senses is not None
        else session.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id == entry.id)
        ).all()
    )
    # L10: only STAMP homonym_group when this entry has senses; a no-sense edit must NOT
    # clear a grouping a JSON import / concept editor set.
    if senses:
        hg = _homonym_group(entry.glossary_id, entry.source_term)
        concept.homonym_group = hg
    else:
        hg = concept.homonym_group or ""
    session.flush()
    for sense in senses:
        sib = session.get(GlossaryConcept, sense.concept_id) if sense.concept_id else None
        if sib is None:
            sib = GlossaryConcept(glossary_id=entry.glossary_id)
            session.add(sib)
            session.flush()
        # sense_label rides the sibling's domain, context_cue its definition (§4.4-3),
        # so project can reproduce the exact GlossarySense (LAW-TERM-7).
        sib.homonym_group = hg
        sib.domain = _sanitize(sense.sense_label)
        sib.definition = _sanitize(sense.context_cue)
        if not sib.external_id:
            sib.external_id = str(uuid.uuid4())  # R3-3: persist a globally-unique identity
        sib.updated_at = _utcnow()
        session.flush()
        # the sibling shares the SAME source surface as the base entry
        _upsert_term(
            session,
            sib.id,
            entry.source_locale or "",
            entry.source_term,
            dnt=bool(entry.dnt),
            case_sensitive=bool(entry.case_sensitive),
            cache=_term_id_cache,
        )
        sib_tgt = None
        if (sense.target_term or "").strip():
            sib_tgt = _upsert_term(
                session,
                sib.id,
                entry.target_locale or "",
                sense.target_term,
                cache=_term_id_cache,
            )
        sense.concept_id = sib.id
        sense.term_id = sib_tgt.id if sib_tgt is not None else None
        session.add(sense)
    session.flush()
    return concept


def _overrides_diff_replace(
    submitted: dict | None, current_meta: dict | None, deriv: dict | None
) -> dict | None:
    """``update_entry``'s REPLACE-semantics override computation (§A.3 middle row): DIFF the
    cleaned submitted dict against the CURRENT projected ``meta``. An unchanged key keeps its
    override state (so re-derive stays re-derive, an override stays an override); a changed
    non-empty key -> OVERRIDE; a key absent from the submitted dict or submitted blank ->
    TOMBSTONE whenever the derivation is non-empty. ``submitted`` None means a target-only /
    dnt-only edit -> NO change to the override state (the caller must not call this).

    Preserves the current override state for unchanged keys by starting from the current
    (backfilled) overrides and only rewriting the keys the submit actually moves."""
    submitted = submitted or {}
    current_meta = current_meta or {}
    deriv = deriv or {}
    # start from the state that reproduces current_meta, so unchanged keys keep their state.
    out = dict(_overrides_two_way(current_meta, deriv) or {})
    for k in _META_ORDER:
        sv = submitted.get(k)
        cv = current_meta.get(k)
        if sv:
            if sv != cv:
                out[k] = sv  # changed non-empty -> override
            # unchanged non-empty -> leave its state (already an override in `out`)
        else:
            # absent/blank in the submit: tombstone iff the derivation would surface a value.
            if deriv.get(k):
                out[k] = None
            else:
                out.pop(k, None)
    return out or None


def _resolve_target_term_id(
    session: Session,
    concept_id: int,
    target_locale: str,
    target_surface: str,
    *,
    exclude_id: int | None = None,
    cterms: list[GlossaryTerm] | None = None,
) -> int | None:
    """A.4-7 target resolution for a binding: the concept's term whose surface == the pair's
    stored ``target_surface``, preferring an EXACT-locale match, then the wildcard locale
    ``""``, then a base-language match (an ``es`` term serving an ``es-ES`` pair) -- creating
    it as ``admitted`` only when no such term exists. A blank surface -> ``None`` (a DNT-only /
    empty-target binding keeps ``target_term_id`` NULL). Used by the backfill (A.5) and the
    thin adapters to point a binding at the enforced target the legacy row wrote."""
    surface = (target_surface or "").strip()
    if not surface:
        return None
    # `cterms` (optional, #597): the concept's terms already loaded by a bulk caller, so a
    # 5,000-entry import does one query per batch instead of one per entry. A term created
    # below is appended to it, so the caller's list stays the concept's current terms.
    if cterms is None:
        cterms = session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == concept_id)
        ).all()
    cand = [t for t in cterms if t.term == target_surface and t.id != exclude_id]
    tl = (target_locale or "").strip()
    for t in cand:  # exact locale
        if (t.locale or "") == tl:
            return t.id
    for t in cand:  # wildcard locale
        if (t.locale or "") == "":
            return t.id
    for t in cand:  # base-language match
        if _lang_base_match(t.locale or "", tl):
            return t.id
    # a full-wildcard self row (Widget->Widget) whose only term IS the source at this locale.
    if exclude_id is not None:
        self_src = session.get(GlossaryTerm, exclude_id)
        if (
            self_src is not None
            and self_src.term == target_surface
            and ((self_src.locale or "") in (tl, ""))
        ):
            return self_src.id
    # not present as a term (a legacy row whose target was never upserted) -> create admitted.
    created = _upsert_term(session, concept_id, tl, target_surface, status=TermStatus.ADMITTED)
    if cterms is not None:
        cterms.append(created)
    return created.id


def delete_entry_unit(
    session: Session,
    glossary_id: int | None,
    concept_id: int | None,
    term_id: int | None,
    target_locale: str,
) -> None:
    """The compat-ENTRY delete UNIT (LAW-TERM-2b). Called AFTER the entry's senses and the
    entry itself have been deleted+flushed by the caller (mirroring remove_term/delete),
    with the entry's link values CAPTURED before deletion (so they survive the flush that
    may expire the deleted instance). Drops the concept-store projection that entry backed
    so the append/update-only reconciler cannot resurrect it:

      (a) the source ``GlossaryTerm`` — ONLY when no surviving entry OR sense of the
          concept still references it (H1: a shared source term of a multi-pair concept
          survives until its LAST projecting entry is gone);
      (b) each target ``GlossaryTerm`` this entry projected that nothing else references;
      (c) every homonym-sibling ``GlossaryConcept`` whose only projection was a sense of
          this entry (its senses were deleted with the entry);
      (d) the base ``GlossaryConcept`` iff it is now termless.

    Order: clear inbound deprecated_replacement refs → delete terms (child) → delete
    concepts (parent), child-before-parent, identical on SQLite and Postgres."""
    if glossary_id is None or concept_id is None:
        return  # legacy / unlinked — nothing concept-side (LAW-TERM-2a)
    concept = session.get(GlossaryConcept, concept_id)
    if concept is None:
        return
    src_id = term_id
    # target term(s) this entry projected: the concept's terms in the entry's target
    # locale that are not the source term.
    tgt_terms = session.exec(
        select(GlossaryTerm).where(
            GlossaryTerm.concept_id == concept.id,
            GlossaryTerm.id != src_id,
        )
    ).all()
    doomed_terms: list[GlossaryTerm] = []
    # (a) source term
    if src_id is not None and not _term_referenced_elsewhere(session, src_id):
        src_term = session.get(GlossaryTerm, src_id)
        if src_term is not None:
            doomed_terms.append(src_term)
    # (b) target terms no longer referenced (in the entry's target locale)
    for t in tgt_terms:
        if (t.locale or "") in ((target_locale or ""), "") and not _term_referenced_elsewhere(
            session, t.id
        ):
            doomed_terms.append(t)
    # (c) homonym-sibling concepts whose sense this entry carried are handled below via
    #     the group: a sibling is dropped when it has no surviving sense referencing it.
    _clear_inbound_replacements(session, [t.id for t in doomed_terms])
    for t in doomed_terms:
        session.delete(t)
    session.flush()
    # sibling homonym concepts with no surviving sense pointing at them
    if concept.homonym_group:
        siblings = session.exec(
            select(GlossaryConcept).where(
                GlossaryConcept.glossary_id == concept.glossary_id,
                GlossaryConcept.homonym_group == concept.homonym_group,
                GlossaryConcept.id != concept.id,
            )
        ).all()
        for sib in siblings:
            # LT2-4: a GENUINE homonym sibling is projected ONLY by a sense (sense.concept_id) --
            # it is never the concept_id of a base compat entry. `_homonym_group` is pair- AND
            # locale-free, so two BASE concepts that merely share a source SURFACE across different
            # source locales land in the same group; such a co-grouped base concept is still
            # enforced by a live ENTRY (which points at its terms via `entry.term_id`), so dropping
            # it here deletes terms a surviving entry references -> an IntegrityError 500 under
            # `PRAGMA foreign_keys=ON` (and silent data loss without the FK). NEVER treat a concept
            # that still owns a live entry as a disposable sibling.
            base_entry = session.exec(
                select(GlossaryEntry.id).where(GlossaryEntry.concept_id == sib.id).limit(1)
            ).first()
            if base_entry is not None:
                continue
            still = session.exec(
                select(GlossarySense.id).where(GlossarySense.concept_id == sib.id).limit(1)
            ).first()
            if still is None:
                _drop_concept_terms(session, sib)
                session.delete(sib)
        session.flush()
    # (d) base concept iff termless
    remaining = session.exec(
        select(GlossaryTerm.id).where(GlossaryTerm.concept_id == concept.id).limit(1)
    ).first()
    if remaining is None:
        session.delete(concept)
        session.flush()


def to_v1(session: Session, glossary_id: int) -> dict:
    """Serialize a module glossary's concept/term store to a ``glossary-concept/v1``
    document (LAW-TERM-8). Operates directly on concepts/terms so import→export→import is
    a JSON-level fixed point (LAW-TERM-11)."""
    g = session.get(Glossary, glossary_id)
    concepts = session.exec(
        select(GlossaryConcept)
        .where(GlossaryConcept.glossary_id == glossary_id)
        .order_by(GlossaryConcept.id)
    ).all()
    terms_by_concept: dict[int, list[GlossaryTerm]] = {}
    for t in session.exec(
        select(GlossaryTerm)
        .where(GlossaryTerm.concept_id.in_([c.id for c in concepts] or [-1]))
        .order_by(GlossaryTerm.id)
    ).all():
        terms_by_concept.setdefault(t.concept_id, []).append(t)
    # A.6: the concept's per-pair BINDINGS (its base GlossaryEntry rows), so a round trip is
    # lossless for a both-directions glossary (LT3-1). Ordered by id for a stable document.
    entries_by_concept: dict[int, list[GlossaryEntry]] = {}
    for e in session.exec(
        select(GlossaryEntry)
        .where(
            GlossaryEntry.glossary_id == glossary_id,
            GlossaryEntry.concept_id.in_([c.id for c in concepts] or [-1]),
        )
        .order_by(GlossaryEntry.id)
    ).all():
        entries_by_concept.setdefault(e.concept_id, []).append(e)

    used_ids: set[str] = set()
    id_for_concept: dict[int, str] = {}
    reserved = frozenset((c.external_id or "").strip() for c in concepts if c.external_id)
    for c in concepts:
        id_for_concept[c.id] = _doc_concept_id(c, used_ids, reserved)

    from app import branding

    pairs = [
        {"source": p.source_locale, "target": p.target_locale}
        for p in session.exec(
            select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
        ).all()
    ]
    doc: dict = {
        "schema": SCHEMA_V1,
        "glossary": {
            "id": glossary_id,
            "name": g.name if g else "",
            "status": g.status if g else "",
            "customer_id": g.customer_id if g else None,
            "content_type_id": g.content_type_id if g else None,
            "pairs": pairs,
            "generator": f"{branding.brand_name()}",
        },
        "concepts": [],
    }
    term_doc_id: dict[int, str] = {}
    for c in concepts:
        for t in terms_by_concept.get(c.id, []):
            term_doc_id[t.id] = id_for_concept[c.id]
    for c in concepts:
        cterms = terms_by_concept.get(c.id, [])
        cdoc = {
            "external_id": id_for_concept[c.id],
            "domain": c.domain,
            "subdomain": c.subdomain,
            "definition": c.definition,
            "notes": c.notes,
            "cross_refs": [
                id_for_concept[rid]
                for rid in (c.cross_refs or [])
                if isinstance(rid, int) and rid in id_for_concept
            ]
            + [r for r in (c.cross_refs or []) if isinstance(r, str)],
            "source": c.source,
            "homonym_group": c.homonym_group,
            "created_by": c.created_by,
            "terms": [_term_to_v1(session, t, id_for_concept) for t in cterms],
            "bindings": [
                {
                    "source_term": e.source_term,
                    "source_locale": e.source_locale or "",
                    "target_term": e.target_term or "",
                    "target_locale": e.target_locale or "",
                    "dnt": bool(e.dnt),
                    "case_sensitive": bool(e.case_sensitive),
                    "meta_overrides": e.meta_overrides,
                }
                for e in entries_by_concept.get(c.id, [])
            ],
        }
        doc["concepts"].append(cdoc)
    return doc


def _match_doc_concept_by_content(
    session: Session,
    glossary_id: int,
    cdoc: dict,
    hg_doc: str,
    exclude: set[int],
) -> GlossaryConcept | None:
    """Content-identity reconcile for an id-less / id-changed re-import (H-4/H-5, T2 hand-off
    #3/#4). Resolve a document concept to the STORED concept of the same identity so three
    id-less re-imports do not grow the store 2 -> 4 -> 6.

    - **Base-language aware (H-5):** a bare ``en`` doc source matches an ``en-US`` stored term,
      mirroring ``_lang_base_match`` -- an exact-locale test made a vendor-shaped both-sides-base
      document non-reimportable.
    - **One-to-one within the import (H-4):** concepts already touched by an earlier doc concept
      in this import (``exclude``) are skipped, so homonym siblings (same surface, same group)
      never all collapse onto ONE stored concept AND a fresh import never re-matches a concept it
      just created in its own first pass. Among the remaining candidates in the doc concept's
      homonym group, an exact ``domain`` then ``definition`` discriminator wins; failing that the
      lowest-id candidate (group ordinal) is a deterministic match. When NO candidate remains the
      caller mints a NEW concept rather than stealing one."""
    doc_terms = [
        ((t.get("locale") or ""), t.get("term"))
        for t in (cdoc.get("terms") or [])
        if t.get("term") and _valid_status(t.get("status")) in TermStatus.USABLE
    ]
    if not doc_terms:
        return None
    cand_ids: list[int] = []
    seen: set[int] = set()
    for loc, surf in doc_terms:
        want = (surf or "").casefold()
        for cid, tloc, tterm, chg in session.exec(
            select(
                GlossaryConcept.id,
                GlossaryTerm.locale,
                GlossaryTerm.term,
                GlossaryConcept.homonym_group,
            )
            .join(GlossaryTerm, GlossaryTerm.concept_id == GlossaryConcept.id)
            .where(
                GlossaryConcept.glossary_id == glossary_id,
                func.lower(GlossaryTerm.term) == (surf or "").lower(),
            )
        ).all():
            if cid in exclude or cid in seen:
                continue
            if (chg or "") != (hg_doc or ""):
                continue
            if (tterm or "").casefold() != want:
                continue
            if (tloc or "") == (loc or "") or _lang_base_match(tloc or "", loc or ""):
                cand_ids.append(cid)
                seen.add(cid)
    if not cand_ids:
        return None
    doc_domain = _sanitize(cdoc.get("domain", ""))
    doc_def = _sanitize(cdoc.get("definition", ""))
    if doc_domain:
        for cid in cand_ids:
            c = session.get(GlossaryConcept, cid)
            if c is not None and (c.domain or "") == doc_domain:
                return c
    if doc_def:
        for cid in cand_ids:
            c = session.get(GlossaryConcept, cid)
            if c is not None and (c.definition or "") == doc_def:
                return c
    return session.get(GlossaryConcept, min(cand_ids))


def _refuse_doc_source_surface_collisions(session: Session, glossary_id: int, doc: dict) -> None:
    """R3-5 + R4-1 + R4-4: refuse a document with two UN-GROUPED concepts that would project
    CONFLICTING compat entries — i.e. they share a source-locale surface AND an overlapping
    target locale (the same (source_locale, target_locale) pair). Two concepts that share a
    surface but whose target locales are DISJOINT are the normal multi-language shape (they
    are ONE concept in the product's own export) and are NOT refused (R4-1). A shared
    non-blank homonym_group is a legitimate homonym. When the glossary declares no pairs
    (R4-4), the source locales are inferred from the document's own term locales, so the rule
    cannot be bypassed by importing before pairs are set."""
    concepts = doc.get("concepts", []) or []
    src_locs = _source_locales(session, glossary_id)
    if not src_locs:
        src_locs = {
            (t.get("locale") or "")
            for c in concepts
            for t in (c.get("terms") or [])
            if t.get("term") and _valid_status(t.get("status")) in TermStatus.USABLE
        }

    def _concept_source_map(cdoc: dict) -> dict[tuple[str, str], set[str]]:
        """(source_locale, surface) -> {target locales} for one doc concept. M-7 (LT3-1): when
        the concept carries an explicit ``bindings`` block use IT (the actual source surfaces +
        their target locales), so a both-directions glossary -- where a term is a SOURCE in one
        concept and a TARGET in another -- is not read as a collision (its bindings occupy
        DIFFERENT (source_locale, target_locale) keys, LAW-TERM-18). Fall back to inferring from
        the term locales only for a pre-T1b document that carries no bindings."""
        blist = cdoc.get("bindings")
        out: dict[tuple[str, str], set[str]] = {}
        if isinstance(blist, list) and blist:
            for b in blist:
                if not isinstance(b, dict):
                    continue
                st = (b.get("source_term") or "").strip()
                if not st:
                    continue
                sl = b.get("source_locale") or ""
                tl = b.get("target_locale") or ""
                if src_locs and sl not in src_locs:
                    continue
                out.setdefault((sl, st), set()).add(tl)
            return out
        usable = [
            t
            for t in (cdoc.get("terms") or [])
            if t.get("term") and _valid_status(t.get("status")) in TermStatus.USABLE
        ]
        for t in usable:
            loc, surface = (t.get("locale") or ""), t.get("term")
            if loc not in src_locs:
                continue
            tgt_locs = {
                (tt.get("locale") or "") for tt in usable if (tt.get("locale") or "") != loc
            }
            out.setdefault((loc, surface), set()).update(tgt_locs)
        return out

    # (source_locale, surface) -> (concept_index, homonym_group, {target locales})
    seen: dict[tuple[str, str], tuple[int, str, set]] = {}
    for idx, cdoc in enumerate(concepts):
        hg = (cdoc.get("homonym_group") or "").strip()
        for (loc, surface), tgt_locs in _concept_source_map(cdoc).items():
            prev = seen.get((loc, surface))
            if prev is None:
                seen[(loc, surface)] = (idx, hg, tgt_locs)
                continue
            prev_idx, prev_hg, prev_tgts = prev
            same_group = hg and prev_hg and hg == prev_hg
            conflict = bool(tgt_locs & prev_tgts) or (not tgt_locs and not prev_tgts)
            if not same_group and conflict:
                raise ConceptError(
                    f"the document has two concepts (#{prev_idx} and #{idx}) that would "
                    f"project conflicting entries for the source term {surface!r} in {loc!r}; "
                    "group them with a shared homonym_group, or reduce them to one concept."
                )
            # disjoint target locales: the normal multi-language case — merge the target set.
            seen[(loc, surface)] = (prev_idx, prev_hg, prev_tgts | tgt_locs)


def _apply_doc_bindings(
    session: Session, glossary_id: int, concept: GlossaryConcept, blist: list, counters: dict
) -> None:
    """A.6: apply a concept's explicit ``bindings`` block verbatim. For the group BASE
    concept each binding upserts a ``GlossaryEntry`` keyed ``(term_id, target_locale)``
    (creating a missing target term as ``admitted``, counted); ``meta_overrides`` is stored
    three-valued as given. For a SIBLING concept the binding is a sense (derived by the
    projector), so the entry is skipped and a targeted one counts in ``sibling_targets_skipped``."""
    base_id = _group_base_id(session, glossary_id, concept)
    if base_id != concept.id:
        for b in blist:
            if isinstance(b, dict) and (b.get("target_term") or "").strip():
                counters["sibling_targets_skipped"] += 1
        return
    for b in blist:
        if not isinstance(b, dict):
            continue
        st = (b.get("source_term") or "").strip()
        if not st:
            continue
        sl = b.get("source_locale") or ""
        tl = b.get("target_locale") or ""
        tt = (b.get("target_term") or "").strip()
        src = session.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == concept.id,
                GlossaryTerm.locale == sl,
                GlossaryTerm.term == st,
            )
        ).first()
        if src is None:
            src = _upsert_term(session, concept.id, sl, st, status=TermStatus.ADMITTED)
        ttid = None
        if tt:
            tgt = session.exec(
                select(GlossaryTerm).where(
                    GlossaryTerm.concept_id == concept.id,
                    GlossaryTerm.locale == tl,
                    GlossaryTerm.term == tt,
                )
            ).first()
            if tgt is None:
                tgt = _upsert_term(session, concept.id, tl, tt, status=TermStatus.ADMITTED)
                counters["terms_created_from_bindings"] += 1
            ttid = tgt.id
        ov = b.get("meta_overrides")
        if not isinstance(ov, dict):
            ov = None
        e = session.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id == concept.id,
                GlossaryEntry.term_id == src.id,
                GlossaryEntry.target_locale == tl,
            )
        ).first()
        if e is None:
            e = GlossaryEntry(
                glossary_id=glossary_id,
                project_id=None,
                source_locale=sl,
                target_locale=tl,
                source_term=st,
                target_term=tt,
                dnt=bool(b.get("dnt", False)),
                case_sensitive=bool(b.get("case_sensitive", False)),
                meta=None,
                concept_id=concept.id,
                term_id=src.id,
                target_term_id=ttid,
                meta_overrides=ov,
            )
            session.add(e)
        else:
            e.target_term_id = ttid
            if "dnt" in b:
                e.dnt = bool(b.get("dnt"))
            if "case_sensitive" in b:
                e.case_sensitive = bool(b.get("case_sensitive"))
            e.meta_overrides = ov
            session.add(e)
        session.flush()
        counters["bindings_written"] += 1
        if ov:
            counters["bindings_pinned"] += 1


def _sanitize_counted(val, counters: dict) -> str:
    """Sanitize a free-text field AND increment ``truncated_fields`` when the store cap
    actually shortened it (§4.2/M7 — counted + warned, never silent)."""
    raw = str(val or "")
    out = _sanitize(raw)
    # a value is "shortened" when flatten+cap produced fewer chars than the flattened
    # (but uncapped) form would have — i.e. the 500-char cap bit.
    flat = " ".join(raw.split())
    if len(flat) > len(out):
        counters["truncated_fields"] = counters.get("truncated_fields", 0) + 1
    return out


def _resolve_concept_meta(session: Session, entry: GlossaryEntry, chosen: GlossarySense | None):
    """Resolve the detached concept/term metadata for a match (§5.3/H2). For a
    sense-selected match the SIBLING concept (sense.concept_id) supplies metadata + the
    selected target's audience; else the base entry's concept. Returns None for a legacy
    entry (no concept)."""
    if chosen is not None and getattr(chosen, "concept_id", None):
        concept = session.get(GlossaryConcept, chosen.concept_id)
        tgt = session.get(GlossaryTerm, chosen.term_id) if chosen.term_id else None
    elif entry.concept_id:
        concept = session.get(GlossaryConcept, entry.concept_id)
        # A.2-g: the binding's ENFORCED target (target_term_id) supplies the metadata a T3
        # seam renders, so a synonym binding (`bill` -> `recibo`) never ships the preferred
        # target's (`factura`) status / term_type / usage_note. Fall back to _pick_target_term
        # only when the binding is not yet backfilled (target_term_id NULL).
        tgt = session.get(GlossaryTerm, entry.target_term_id) if entry.target_term_id else None
        if tgt is None:
            tgt = _pick_target_term(
                session.exec(
                    select(GlossaryTerm).where(GlossaryTerm.concept_id == entry.concept_id)
                ).all(),
                entry.target_locale,
                exclude_id=entry.term_id,
                source_locale=entry.source_locale,
            )
    else:
        return None
    if concept is None:
        return None
    src = session.get(GlossaryTerm, entry.term_id) if entry.term_id else None
    alts = [
        {"surface": t.term, "status": t.status, "replacement": _replacement_surface(session, t)}
        for t in session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == concept.id)
        ).all()
        if t.status in (TermStatus.DEPRECATED, TermStatus.FORBIDDEN)
    ]
    return {
        "definition": concept.definition,
        "domain": concept.domain,
        "subdomain": concept.subdomain,
        "notes": concept.notes,
        "status": (tgt.status if tgt is not None else TermStatus.PREFERRED),
        "term_type": (tgt.term_type if tgt is not None else ""),
        "part_of_speech": (src.part_of_speech if src is not None else ""),
        "usage_note": (tgt.usage_note if tgt is not None else ""),
        "context_example": (tgt.context_example if tgt is not None else ""),
        # M2 (gate round-1) + R2-8 (round-2): the SOURCE term is the SINGLE owner of
        # audience for the filter — where dnt/case_sensitive already live and where CSV
        # import / export / the concept editor write it. `filter_by_audience` reads THIS
        # key, so a CSV-set `audience=human` on the source term actually drops the term
        # from the llm/mt channels (27sin00). Audience is source-side only (there is no
        # `target_audience` key — it was dead; see the editor/descriptor note).
        "audience": (
            (src.audience if src is not None else TermAudience.DEFAULT) or TermAudience.DEFAULT
        ),
        "alternatives": alts,
    }


_PER_FIELD_BUDGET = 200


def _budget(val: str) -> str:
    val = _flatten_surface(val)
    if len(val) > _PER_FIELD_BUDGET:
        return val[: _PER_FIELD_BUDGET - 1] + "…"
    return val


def _assemble_block(kept: list[dict], *, omitted: int) -> str:
    lines = ["Terminology (apply exactly):"]
    for m in kept:
        meta = m.get("_concept") or {}
        src = _flatten_surface(m.get("source_term"))
        if m.get("dnt"):
            lines.append(f"- {src} -> (do not translate)")
            continue
        tgt = _flatten_surface(m.get("target_term"))
        tags = []
        if meta.get("part_of_speech"):
            tags.append(meta["part_of_speech"])
        if meta.get("domain"):
            tags.append(meta["domain"])
        tagstr = f"  [{'; '.join(tags)}]" if tags else ""
        lines.append(f"- {src} -> {tgt}{tagstr}")
        if meta.get("definition"):
            lines.append(f"  note: {_budget(meta['definition'])}")
        for alt in meta.get("alternatives", []):
            repl = alt.get("replacement")
            tail = f"; use {_flatten_surface(repl)}" if repl else ""
            lines.append(
                f"  do not use: {_flatten_surface(alt['surface'])} ({alt['status']}{tail})"
            )
    if omitted > 0:
        lines.append(f"- (+{omitted} more terms omitted for length)")
    return "\n".join(lines)


# --- concept/term CRUD (surface-facing service seams) ---------------------------------


def list_concepts(session: Session, glossary_id: int) -> dict:
    """Return the glossary's concept/term store as a ``glossary-concept/v1`` document
    (the concept-view read for /api + MCP)."""
    return to_v1(session, glossary_id)


def _resolve_source_surface_collision(
    session: Session,
    glossary_id: int,
    surfaces: list[tuple[str, str]],
    *,
    homonym_group: str = "",
    exclude_concept_id: int | None = None,
    source_locales: set[str] | None = None,
) -> None:
    """R2-5 + R3-4 + R4-2 (gate rounds 2/3/4): a concept must not share a source-locale
    surface with ANOTHER concept unless they are in the SAME `homonym_group`. `surfaces` is
    the list of (locale, surface) the concept contributes to a source locale.

    R4-2: this NEVER mutates a rival concept (no silent auto-group — that manufactured the
    R2-5 double-entry shape when the caller was already linked). Any collision with a
    different-or-un-grouped rival is REFUSED, naming the rival; the operator's remedy is to
    put both concepts in the same homonym_group (via `edit_concept`'s `homonym_group`, or
    build the homonym with `add_sense`, which shares the base concept). Called from
    create_concept, edit_concept_term (rename) and add_concept_term.

    `source_locales` overrides the glossary's configured source locales (R4-4: apply the
    rule on the document's own locales when the glossary declares no pairs)."""
    src_locs = (
        source_locales if source_locales is not None else _source_locales(session, glossary_id)
    )
    if not src_locs:
        return
    for loc, surface in surfaces:
        if not surface or loc not in src_locs:
            continue
        rivals = session.exec(
            select(GlossaryConcept)
            .where(
                GlossaryTerm.concept_id == GlossaryConcept.id,
                GlossaryConcept.glossary_id == glossary_id,
                GlossaryTerm.locale == loc,
                GlossaryTerm.term == surface,
                GlossaryTerm.status.in_(list(TermStatus.USABLE)),
            )
            .distinct()
        ).all()
        for rc in rivals:
            if rc.id == exclude_concept_id:
                continue
            if homonym_group and rc.homonym_group == homonym_group:
                continue  # same homonym group — a legitimate homonym
            raise ConceptError(
                f"a concept (id {rc.id}) already uses the source term {surface!r} in "
                f"{loc!r}; put both in the same homonym_group (edit the existing concept's "
                "homonym_group, or add this meaning as a sense) rather than a second "
                "concept that would conflict."
            )


def _term_surfaces(terms: list[dict]) -> list[tuple[str, str]]:
    """The (locale, surface) pairs of the USABLE terms of a term-doc list."""
    return [
        ((t.get("locale") or ""), t.get("term"))
        for t in terms or []
        if t.get("term") and _valid_status(t.get("status")) in TermStatus.USABLE
    ]


def _apply_term_fields(term: GlossaryTerm, tdoc: dict) -> None:
    if tdoc.get("term"):
        term.term = tdoc["term"]  # VERBATIM
    if "locale" in tdoc:
        term.locale = tdoc.get("locale") or ""
    if "status" in tdoc:
        term.status = _valid_status(tdoc.get("status"))
    if "term_type" in tdoc:
        term.term_type = _valid_term_type(tdoc.get("term_type"))
    if "audience" in tdoc:
        term.audience = canon_audience(tdoc.get("audience"))
    for k in ("part_of_speech", "usage_note", "context_example", "geographical_usage", "source"):
        if k in tdoc:
            setattr(term, k, _sanitize(tdoc.get(k, "")))
    for k in ("grammatical_gender", "grammatical_number"):
        if k in tdoc:
            setattr(term, k, _sanitize(tdoc.get(k, "")))
    for k in ("case_sensitive", "exact", "dnt"):
        if k in tdoc:
            setattr(term, k, bool(tdoc.get(k)))


def _require_concept(session: Session, glossary_id: int, concept_id: int) -> GlossaryConcept:
    concept = session.get(GlossaryConcept, concept_id)
    if concept is None or concept.glossary_id != glossary_id:
        raise ConceptError("concept not found in this glossary.")
    return concept
