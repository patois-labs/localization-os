"""Terminology concept/term store — the concept level beneath the compat termbase.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports SCHEMA_V1, log, _CONCEPT_META_KEYS, _TERM_META_KEYS,
_utcnow, _sanitize, canon_audience, _valid_status, _valid_term_type, ConceptError,
ConceptWriteResult, SetPairsResult, _CONCEPT_EDITOR_KEY, _CONCEPT_EDITOR_OFF_MSG,
concept_editor_enabled, _require_concept_editor, _upsert_term, _find_concept_for_source,
_homonym_group, _lang_base_match, _pick_target_term, _derive_meta, _META_ORDER, _project_meta,
_overrides_two_way, _entry_deriv, _closure_entry_ids, _source_term_of, _PAIR_BINDINGS_MARKER,
_pair_binding_columns_present, _retire_target_term, pin_shared_meta_on_siblings,
_usable_source_terms, _clear_inbound_replacements, _term_referenced_elsewhere, _drop_concept_terms,
delete_glossary_concepts, _doc_concept_id, _term_to_v1, _concepts_share_a_term,
_replacement_surface, filter_by_audience, _render_ordering_key, _flatten_surface, _est_tokens,
concepts_for_editor, _source_locales, _configured_pairs, _group_base_id, _touch_draft, dumps_v1,
loads_v1, sync_entry_concept, _overrides_diff_replace, _resolve_target_term_id, delete_entry_unit,
to_v1, _match_doc_concept_by_content, _refuse_doc_source_surface_collisions, _apply_doc_bindings,
_sanitize_counted, _resolve_concept_meta, _PER_FIELD_BUDGET, _budget, _assemble_block,
list_concepts, _resolve_source_surface_collision, _term_surfaces, _apply_term_fields,
_require_concept, _sync_glossary_from_entries, _bulk_sync_glossary_from_entries,
create_entry_bindings, _repoint_off_target, set_audience, _add_term_row, backfill_glossary,
consolidate_split_concepts, reconcile_glossary_from_entries, bulk_bind_touched, project,
backfill_pair_bindings, apply_entry_binding, _delete_entry_and_unit, match_concepts, render_for_llm,
from_v1 from app.services.glossary_concepts_common, app.services.glossary_concepts_part2,
app.services.glossary_concepts_part3, app.services.glossary_concepts_part4,
app.services.glossary_concepts_part5.

SPEC-terminology-backend T1 (D1 + D2). This module owns the concept/term rich store
(``GlossaryConcept`` / ``GlossaryTerm``) that sits BENEATH the derived-compat
``GlossaryEntry`` / ``GlossarySense`` rows, plus the ``glossary-concept/v1`` JSON codec,
the concept-aware match selector, and the (T1-inert, T3-wired) prompt renderer.

Core invariants it upholds (LAW-TERM-* in the spec), as amended by T1b (LAW-TERM-18):

* **The concept store is the single authority (LAW-TERM-18, T1b).** Every write, on every
  surface, is a concept-store operation; the compat ``glossaryentry``/``glossarysense`` rows
  are a pure PROJECTION of the per-pair BINDINGS. The binding-owned columns
  (``concept_id``/``term_id``/``target_term_id``/``target_locale``/``dnt``/``case_sensitive``/
  ``meta_overrides`` on the entry; ``concept_id``/``term_id`` on the sense) are written by the
  thin adapters / editor / import; the DERIVED columns (``source_term``/``source_locale``/
  ``target_term``/``meta`` on the entry; ``sense_label``/``context_cue``/``target_term`` on the
  sense) are written ONLY by :func:`project`, which runs at the end of every write in the same
  transaction. This is what makes ``match_terms``/gates/grid/CSV/api byte-identical to the
  pre-T1b store BY CONSTRUCTION (LAW-TERM-1); the write-parity golden asserts it.
* **``meta_overrides`` records the per-pair meta authority (A.3).** Three-valued per key:
  absent = derive from the concept/source term; a string = override; JSON ``null`` = tombstone
  (the pair shows no value though the concept has one). Legacy MERGE writers pin non-empty
  values (two-way rule); ``update_entry`` replaces by diff; the concept editor / ``from_v1`` /
  TBX are concept-first and pin nothing. :func:`project` reproduces ``entry.meta`` from the
  overrides over the derivation.
* **The projector NEVER deletes and never creates an ENTRY binding (A.2).** Entry-binding
  creation lives in the adapters / :func:`create_entry_bindings` (A.4-2) / ``set_pairs`` /
  import; SENSE rows are derived (created + updated in place, never deleted) only on the
  concept-editor / import path (``derive_senses``); the legacy adapters are LINK-ONLY on
  senses. Rows vanish only through explicit delete units
  (:func:`delete_entry_unit` / :func:`delete_glossary_concepts` / target retirement A.2-f).
* **Idempotent backfill (LAW-TERM-6 / A.5).** :func:`backfill_glossary` links concepts/terms;
  :func:`backfill_pair_bindings` (its own versioned marker in ``db.py``) resolves
  ``target_term_id`` + pins ``meta_overrides`` by the two-way rule, writing NULLs only;
  ``project(backfill(strip(X))) == X`` for the derived columns (projection-equivalence).
* **Shared selection core (LAW-TERM-1/H6).** :func:`match_concepts` reuses ``glossary``'s
  ``_select_matched_entries`` / ``_emit_for_entry``, so its five-key compat projection is
  byte-identical to ``match_terms`` (the §9-T1 non-divergence test).

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
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
from app.services import glossary as gloss_engine
from app.services.glossary_concepts_common import (
    _CONCEPT_EDITOR_KEY,
    _CONCEPT_EDITOR_OFF_MSG,
    _CONCEPT_META_KEYS,
    _META_ORDER,
    _PAIR_BINDINGS_MARKER,
    _TERM_META_KEYS,
    SCHEMA_V1,
    ConceptError,
    ConceptWriteResult,
    SetPairsResult,
    _clear_inbound_replacements,
    _closure_entry_ids,
    _concepts_share_a_term,
    _configured_pairs,
    _derive_meta,
    _doc_concept_id,
    _drop_concept_terms,
    _entry_deriv,
    _est_tokens,
    _find_concept_for_source,
    _flatten_surface,
    _group_base_id,
    _homonym_group,
    _lang_base_match,
    _overrides_two_way,
    _pair_binding_columns_present,
    _pick_target_term,
    _project_meta,
    _render_ordering_key,
    _replacement_surface,
    _require_concept_editor,
    _retire_target_term,
    _sanitize,
    _source_locales,
    _source_term_of,
    _term_referenced_elsewhere,
    _term_to_v1,
    _touch_draft,
    _upsert_term,
    _usable_source_terms,
    _utcnow,
    _valid_status,
    _valid_term_type,
    canon_audience,
    concept_editor_enabled,
    concepts_for_editor,
    delete_glossary_concepts,
    dumps_v1,
    filter_by_audience,
    loads_v1,
    pin_shared_meta_on_siblings,
)
from app.services.glossary_concepts_part2 import (
    _PER_FIELD_BUDGET,
    _apply_doc_bindings,
    _apply_term_fields,
    _assemble_block,
    _budget,
    _match_doc_concept_by_content,
    _overrides_diff_replace,
    _refuse_doc_source_surface_collisions,
    _require_concept,
    _resolve_concept_meta,
    _resolve_source_surface_collision,
    _resolve_target_term_id,
    _sanitize_counted,
    _term_surfaces,
    delete_entry_unit,
    list_concepts,
    sync_entry_concept,
    to_v1,
)
from app.services.glossary_concepts_part3 import (
    _add_term_row,
    _bulk_sync_glossary_from_entries,
    _repoint_off_target,
    _sync_glossary_from_entries,
    create_entry_bindings,
    set_audience,
)
from app.services.glossary_concepts_part4 import (
    _delete_entry_and_unit,
    apply_entry_binding,
    backfill_glossary,
    backfill_pair_bindings,
    bulk_bind_touched,
    consolidate_split_concepts,
    match_concepts,
    project,
    reconcile_glossary_from_entries,
    render_for_llm,
)
from app.services.glossary_concepts_part5 import from_v1

__all__ = [
    "SCHEMA_V1",
    "_CONCEPT_EDITOR_KEY",
    "_CONCEPT_EDITOR_OFF_MSG",
    "_CONCEPT_META_KEYS",
    "_META_ORDER",
    "_PAIR_BINDINGS_MARKER",
    "_PER_FIELD_BUDGET",
    "_TERM_META_KEYS",
    "ConceptError",
    "ConceptWriteResult",
    "GlossaryPair",
    "SetPairsResult",
    "TermAudience",
    "_add_term_row",
    "_apply_doc_bindings",
    "_apply_term_fields",
    "_assemble_block",
    "_budget",
    "_bulk_sync_glossary_from_entries",
    "_clear_inbound_replacements",
    "_closure_entry_ids",
    "_concepts_share_a_term",
    "_configured_pairs",
    "_delete_entry_and_unit",
    "_derive_meta",
    "_doc_concept_id",
    "_drop_concept_terms",
    "_entry_deriv",
    "_est_tokens",
    "_find_concept_for_source",
    "_flatten_surface",
    "_group_base_id",
    "_homonym_group",
    "_lang_base_match",
    "_match_doc_concept_by_content",
    "_overrides_diff_replace",
    "_overrides_two_way",
    "_pair_binding_columns_present",
    "_pick_target_term",
    "_project_meta",
    "_refuse_doc_source_surface_collisions",
    "_render_ordering_key",
    "_replacement_surface",
    "_repoint_off_target",
    "_require_concept",
    "_require_concept_editor",
    "_resolve_concept_meta",
    "_resolve_source_surface_collision",
    "_resolve_target_term_id",
    "_retire_target_term",
    "_sanitize",
    "_sanitize_counted",
    "_source_locales",
    "_source_term_of",
    "_sync_glossary_from_entries",
    "_term_referenced_elsewhere",
    "_term_surfaces",
    "_term_to_v1",
    "_touch_draft",
    "_upsert_term",
    "_usable_source_terms",
    "_utcnow",
    "_valid_status",
    "_valid_term_type",
    "apply_entry_binding",
    "backfill_glossary",
    "backfill_pair_bindings",
    "bulk_bind_touched",
    "canon_audience",
    "concept_editor_enabled",
    "concepts_for_editor",
    "consolidate_split_concepts",
    "create_entry_bindings",
    "datetime",
    "delete_entry_unit",
    "delete_glossary_concepts",
    "dumps_v1",
    "filter_by_audience",
    "from_v1",
    "func",
    "gloss_engine",
    "json",
    "list_concepts",
    "loads_v1",
    "match_concepts",
    "or_",
    "pin_shared_meta_on_siblings",
    "project",
    "reconcile_glossary_from_entries",
    "render_for_llm",
    "sa_insert",
    "sa_update",
    "set_audience",
    "sync_entry_concept",
    "timezone",
    "to_v1",
]


log = logging.getLogger("loc_tms.glossary_concepts")


def match_concepts_merged(
    session: Session,
    project_id: int,
    segment_text: str,
    source_locale: str,
    target_locale: str,
    plan,
    provider=None,
    conflict_state=None,
    disambig=None,
) -> list[dict]:
    """Concept-aware analogue of ``glossary_module.match_terms_merged``: merge the
    concept matches across a GlossaryPlan, first-writer-wins by source_term. The compat
    projection (drop ``_concept``) equals ``match_terms_merged`` for every fixture
    (LAW-TERM-1)."""
    out: dict = {}

    def _consume(matches):
        for t in matches:
            st = t.get("source_term")
            if st in out:
                continue
            out[st] = t

    for gid in plan.glossary_ids:
        _consume(
            match_concepts(
                session,
                project_id,
                segment_text,
                source_locale,
                target_locale,
                provider=provider,
                glossary_id=gid,
                disambig=disambig,
            )
        )
    if plan.include_legacy:
        _consume(
            match_concepts(
                session,
                project_id,
                segment_text,
                source_locale,
                target_locale,
                provider=provider,
                disambig=disambig,
            )
        )
    merged = list(out.values())
    if len(plan.glossary_ids) + int(bool(plan.include_legacy)) > 1:
        merged = gloss_engine.drop_shadowed_terms(merged, segment_text)
    return merged


def create_concept(session: Session, glossary_id: int, **fields) -> GlossaryConcept:
    _require_concept_editor()
    g = session.get(Glossary, glossary_id)
    if g is None:
        raise ConceptError("glossary not found.")
    hg = _sanitize(fields.get("homonym_group", ""))
    _resolve_source_surface_collision(
        session, glossary_id, _term_surfaces(fields.get("terms") or []), homonym_group=hg
    )
    concept = GlossaryConcept(
        glossary_id=glossary_id,
        external_id=_sanitize(fields.get("external_id", "")),
        domain=_sanitize(fields.get("domain", "")),
        subdomain=_sanitize(fields.get("subdomain", "")),
        definition=_sanitize(fields.get("definition", "")),
        notes=_sanitize(fields.get("notes", "")),
        source=_sanitize(fields.get("source", "")),
        homonym_group=hg,
        created_by=_sanitize(fields.get("created_by", "")),
    )
    session.add(concept)
    session.flush()
    if not concept.external_id:
        concept.external_id = str(uuid.uuid4())  # R3-3: persist a globally-unique identity
    _terms = fields.get("terms") or []
    for tdoc in _terms:
        _add_term_row(session, concept.id, tdoc)
    # A.4-2: create the entry bindings for the base concept's usable source/target terms; the
    # projector then fills the derived columns and derives any sibling senses (concept-first,
    # so no meta_overrides is pinned).
    create_entry_bindings(session, glossary_id, [concept.id])
    project(session, glossary_id, concept_ids=[concept.id], derive_senses=True)
    session.commit()
    _touch_draft(session, glossary_id)
    return concept


def edit_concept(session: Session, glossary_id: int, concept_id: int, **fields) -> GlossaryConcept:
    _require_concept_editor()
    concept = _require_concept(session, glossary_id, concept_id)
    # R4-6: `homonym_group` IS applied here (it is advertised on the /api edit route and is
    # the remedy the collision refusals prescribe — accepting-and-discarding it was the
    # worst of the three options). Blank clears it.
    for key in (
        "external_id",
        "domain",
        "subdomain",
        "definition",
        "notes",
        "source",
        "homonym_group",
    ):
        if key in fields:
            setattr(concept, key, _sanitize(fields[key]))
    concept.updated_at = _utcnow()
    session.add(concept)
    session.flush()
    # LAW-TERM-18 (R7-D/R7-K): a concept-editor edit of definition/domain/notes is a
    # concept-first write -- it PINS nothing. The projector re-derives ``entry.meta`` from each
    # binding's ``meta_overrides`` over the (now-edited) concept derivation, so the edit is
    # visible on every binding that carries neither an override nor a tombstone for that key,
    # and invisible on a binding a legacy writer pinned (A.3). Scope the projection to this
    # concept's group closure via ``concept_ids=``.
    project(session, glossary_id, concept_ids=[concept_id], derive_senses=True)
    session.commit()
    _touch_draft(session, glossary_id)
    return concept


def delete_concept(session: Session, glossary_id: int, concept_id: int) -> bool:
    """Delete EXACTLY ONE concept (R4-5) — never cascade into its homonym siblings.

    Deletes the concept's OWN compat entries and the senses ON those entries (a sense may
    belong to a SIBLING — deleting it merely unprojects the sibling), the senses that
    PROJECT this concept (the sibling case), its terms and the concept row; then
    `project` re-projects the surviving group members, so deleting the BASE of a
    homonym group promotes the lowest-id survivor to the new base (its own entry) instead of
    wiping the whole group. Standalone concepts delete cleanly. An unexpected FK
    IntegrityError is rolled back and surfaced as ConceptError."""
    _require_concept_editor()
    _require_concept(session, glossary_id, concept_id)
    try:
        # this concept's own compat entries + the senses attached to them
        for e in session.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id == concept_id,
            )
        ).all():
            for s in session.exec(
                select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
            ).all():
                session.delete(s)
            session.flush()
            session.delete(e)
        session.flush()
        # senses that PROJECT this concept (it is a sibling) — its FKs into it
        for s in session.exec(
            select(GlossarySense).where(GlossarySense.concept_id == concept_id)
        ).all():
            session.delete(s)
        session.flush()
        # clear inbound deprecated_replacement refs, drop this concept's terms + the concept
        concept = session.get(GlossaryConcept, concept_id)
        if concept is not None:
            _drop_concept_terms(session, concept)
            session.delete(concept)
            session.flush()
        # A sibling of a deleted BASE must be PROMOTED to base (gain its own entry binding);
        # the projector never creates entry bindings, so create them for the surviving group
        # first, then project the whole glossary to refresh derived columns + senses.
        survivors = session.exec(
            select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == glossary_id)
        ).all()
        if survivors:
            create_entry_bindings(session, glossary_id, list(survivors))
        project(session, glossary_id, derive_senses=True)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConceptError("could not delete the concept (it is still referenced).") from exc
    _touch_draft(session, glossary_id)
    return True


def add_concept_term(session: Session, glossary_id: int, concept_id: int, **tdoc) -> GlossaryTerm:
    _require_concept_editor()
    concept = _require_concept(session, glossary_id, concept_id)
    # R3-4: adding a source-locale surface that belongs to another concept collides — same
    # rule as create_concept (a synonym of THIS concept is fine; the rule excludes it).
    _resolve_source_surface_collision(
        session,
        glossary_id,
        _term_surfaces([tdoc]),
        homonym_group=concept.homonym_group,
        exclude_concept_id=concept_id,
    )
    # A.1: a SIBLING concept's target in a pair the group BASE does not cover is refused (the
    # base's row is the only home for the group's senses in a pair).
    base_id = _group_base_id(session, glossary_id, concept)
    term = _add_term_row(session, concept_id, tdoc)
    if base_id != concept_id and (term.locale or "") and term.status in TermStatus.USABLE:
        # this is a sibling adding a TARGET-side term; refuse when the base has no binding for L.
        base_has_pair = session.exec(
            select(GlossaryEntry.id)
            .where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id == base_id,
                GlossaryEntry.target_locale == (term.locale or ""),
            )
            .limit(1)
        ).first()
        src_locs = _source_locales(session, glossary_id)
        if base_has_pair is None and (term.locale or "") not in src_locs:
            base = session.get(GlossaryConcept, base_id)
            label = (base.domain or base.definition or "") if base is not None else ""
            raise ConceptError(
                f"the base concept #{base_id} {label!r} has no target for "
                f"{term.locale or ''!r} yet; add it first."
            )
    # A.4-2: adding a source synonym / a target may create new bindings for the concept's group.
    create_entry_bindings(session, glossary_id, [concept_id])
    project(session, glossary_id, concept_ids=[concept_id], derive_senses=True)
    session.commit()
    _touch_draft(session, glossary_id)
    return term


def edit_concept_term(
    session: Session, glossary_id: int, concept_id: int, term_id: int, **tdoc
) -> GlossaryTerm:
    _require_concept_editor()
    concept = _require_concept(session, glossary_id, concept_id)
    term = session.get(GlossaryTerm, term_id)
    if term is None or term.concept_id != concept_id:
        raise ConceptError("term not found in this concept.")
    # R3-4: a RENAME onto another concept's source surface projects two conflicting compat
    # entries — enforce the same collision rule create_concept does (excluding THIS term's
    # concept), so it cannot slip in through the rename seam.
    # #510: PROMOTING a deprecated term to a usable status makes its surface a live source
    # surface, so the same collision rule applies as for a rename.
    promoted = (
        tdoc.get("status") in TermStatus.USABLE
        and term.status not in TermStatus.USABLE
        and not (tdoc.get("term") and tdoc.get("term") != term.term)
    )
    if promoted:
        _resolve_source_surface_collision(
            session,
            glossary_id,
            [((tdoc.get("locale") if "locale" in tdoc else term.locale) or "", term.term)],
            homonym_group=concept.homonym_group,
            exclude_concept_id=concept_id,
        )
    new_surface = tdoc.get("term")
    if new_surface and new_surface != term.term:
        _resolve_source_surface_collision(
            session,
            glossary_id,
            [((tdoc.get("locale") if "locale" in tdoc else term.locale) or "", new_surface)],
            homonym_group=concept.homonym_group,
            exclude_concept_id=concept_id,
        )
    # A.4-4 (M-6): a LOCALE edit is REFUSED on a term that backs any binding -- as a SOURCE term
    # (GlossaryEntry.term_id), a bound TARGET term (GlossaryEntry.target_term_id) or a sibling's
    # target (GlossarySense.term_id) -- because the pair identity would silently break; delete +
    # re-add instead. Allowed on an unbound term.
    new_locale = tdoc.get("locale")
    if "locale" in tdoc and new_locale is not None and new_locale != term.locale:
        backs_binding = (
            session.exec(
                select(GlossaryEntry.id).where(GlossaryEntry.term_id == term.id).limit(1)
            ).first()
            is not None
            or session.exec(
                select(GlossaryEntry.id).where(GlossaryEntry.target_term_id == term.id).limit(1)
            ).first()
            is not None
            or session.exec(
                select(GlossarySense.id).where(GlossarySense.term_id == term.id).limit(1)
            ).first()
            is not None
        )
        if backs_binding:
            raise ConceptError(
                "cannot change the locale of a term that backs a glossary binding; "
                "delete and re-add it instead."
            )
    old_status = term.status
    old_dnt, old_cs = bool(term.dnt), bool(term.case_sensitive)
    _apply_term_fields(term, tdoc)
    term.updated_at = _utcnow()
    session.add(term)
    session.flush()
    pairs_updated = 0
    bindings_emptied: list[int] = []
    # A.3: a term-level dnt/case_sensitive edit is a PER-PAIR fact that applies to EVERY binding
    # of this SOURCE term ("propagate, visibly"). A surface rename needs no write-through -- the
    # projector derives source_term/target_term from term_id/target_term_id.
    dnt_changed = "dnt" in tdoc and bool(term.dnt) != old_dnt
    cs_changed = "case_sensitive" in tdoc and bool(term.case_sensitive) != old_cs
    if dnt_changed or cs_changed:
        cterms = session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == concept_id)
        ).all()
        for e in session.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.term_id == term.id
            )
        ).all():
            if dnt_changed and not term.dnt and not e.target_term_id:
                # turning DNT off: re-point to a usable target for the pair if one exists,
                # else REFUSE (R7-M -- a non-DNT binding may not enforce an empty target).
                tgt = _pick_target_term(
                    cterms, e.target_locale, exclude_id=term.id, source_locale=e.source_locale
                )
                if tgt is None:
                    raise ConceptError("give the term a target before clearing Do Not Translate.")
                e.target_term_id = tgt.id
            if dnt_changed:
                e.dnt = bool(term.dnt)
            if cs_changed:
                e.case_sensitive = bool(term.case_sensitive)
            session.add(e)
            pairs_updated += 1
    # A.4-3: demoting the last usable BOUND target for a pair re-points, or refuses (non-DNT).
    demoted = (
        "status" in tdoc
        and old_status in TermStatus.USABLE
        and term.status not in TermStatus.USABLE
    )
    if demoted:
        _, emptied = _repoint_off_target(session, glossary_id, concept_id, term.id)
        bindings_emptied.extend(emptied)
    # A newly-usable target / promoted term may create bindings (A.4-2).
    create_entry_bindings(session, glossary_id, [concept_id])
    session.flush()
    project(session, glossary_id, concept_ids=[concept_id], derive_senses=True)
    session.commit()
    _touch_draft(session, glossary_id)
    return ConceptWriteResult(term, pairs_updated, bindings_emptied)


def delete_concept_term(session: Session, glossary_id: int, concept_id: int, term_id: int) -> bool:
    """Delete one GlossaryTerm — the LAW-TERM-2b delete-TERM unit (gate round-1 H1). The
    term may be referenced by `glossaryentry.term_id` (it is a SOURCE term — possibly
    shared across a multi-pair concept's entries) and/or `glossarysense.term_id` (a
    sibling's target); those children are deleted FIRST (with their own senses), flushed,
    THEN inbound deprecated_replacement refs are cleared, THEN the term, THEN the concept
    iff now termless — so no FK IntegrityError can escape. A leftover FK error is rolled
    back and surfaced as ConceptError."""
    _require_concept_editor()
    _require_concept(session, glossary_id, concept_id)
    term = session.get(GlossaryTerm, term_id)
    if term is None or term.concept_id != concept_id:
        return False
    try:
        # (0) A.4-3: re-point (or refuse) every base binding whose ENFORCED target is this
        # term BEFORE the delete, so no non-DNT binding is left with the row silently emptied.
        _repoint_off_target(session, glossary_id, concept_id, term_id)
        session.flush()
        # (1) every compat entry whose SOURCE term is this term (+ its senses)
        for e in session.exec(select(GlossaryEntry).where(GlossaryEntry.term_id == term_id)).all():
            for s in session.exec(
                select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
            ).all():
                session.delete(s)
            session.flush()
            session.delete(e)
        session.flush()
        # (2) every sense whose TARGET term is this term (the sibling re-derives on projection)
        for s in session.exec(select(GlossarySense).where(GlossarySense.term_id == term_id)).all():
            session.delete(s)
        session.flush()
        # (3) inbound deprecated_replacement refs, then the term
        _clear_inbound_replacements(session, [term_id])
        session.delete(term)
        session.flush()
        # (4) concept goes if now termless
        remaining = session.exec(
            select(GlossaryTerm.id).where(GlossaryTerm.concept_id == concept_id).limit(1)
        ).first()
        if remaining is None:
            concept = session.get(GlossaryConcept, concept_id)
            if concept is not None:
                session.delete(concept)
                session.flush()
        else:
            # a surviving synonym/target still projects — a promoted synonym may need a
            # binding, then re-derive the compat rows + senses.
            create_entry_bindings(session, glossary_id, [concept_id])
            project(session, glossary_id, concept_ids=[concept_id], derive_senses=True)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConceptError("could not delete the term (it is still referenced).") from exc
    _touch_draft(session, glossary_id)
    return True
