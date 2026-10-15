"""Dedicated glossary module (Phase 5-B) — the ONE service the /glossaries web tab,
the `glossary module` CLI subgroup, and the /api module endpoints all call.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, _NO_CT_FILTER, _utcnow, GlossaryModuleError,
_reject_archived_customer, _norm, _canon, create, _resolve_content_type, set_content_type,
assign_customer, rename, delete, get, _require, list_glossaries, list_for_customer, list_unassigned,
visible, approve, unapprove, touch_draft, _parse_pairs, pairs_for, covers_pair, content_type_allows,
options_for_step, GlossaryPlan, _concept_shaped_import, _compat_fingerprint, _concept_fingerprint,
_import_changed_content, list_entries, page_entries, entry_count, term_counts, preview_terms,
to_dict from app.services.glossary_module_locale_canonicalization_.

The pre-module glossary machinery in ``app/services/glossary.py`` is UNCHANGED as a
run-time engine (static term->term + DNT, GlossarySense context selection, LLM
builder, CSV import). This module wraps it with the two genuinely new concepts:

  * a first-class ``Glossary`` CONTAINER (org-owned, optionally customer-scoped) with
    an APPROVAL lifecycle (draft <-> approved, recorded approved_by/approved_at);
  * LANGUAGE-PAIR association (``GlossaryPair`` rows, canonical locale codes) that a
    workflow step's glossary dropdown filters on.

Ownership / scoping mirrors PromptTemplate/EngineProfile: ``org_id`` stamped at
create, filtered in list routes; ``customer_id`` for R2-3 confinement. No route-local
business logic (repo law) — every web/CLI/api surface delegates here.

Approval semantics (documented choice): editing an APPROVED glossary's TERMS or PAIRS
reverts it to DRAFT (``touch_draft``) so a published glossary that changes must be
re-approved before it re-appears in the workflow dropdown — the safest state model,
matching how a changed artifact loses its prior sign-off. Renaming does NOT revert
(metadata, not content), and neither does an IMPORT THAT CHANGED NOTHING: a
re-import whose every row is byte-identical to what is stored leaves an approved
glossary approved (see ``_import_changed_content``), so an idempotent re-run cannot
silently unpublish a glossary a workflow step is pinned to.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from sqlmodel import Session, select

from app.models import (
    Customer,
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryStatus,
    GlossaryTerm,
)
from app.services import glossary as gloss_engine
from app.services import ids, languages_catalog
from app.services.glossary_module_locale_canonicalization_ import (
    _NO_CT_FILTER,
    GlossaryModuleError,
    GlossaryPlan,
    _canon,
    _compat_fingerprint,
    _concept_fingerprint,
    _concept_shaped_import,
    _import_changed_content,
    _norm,
    _parse_pairs,
    _reject_archived_customer,
    _require,
    _resolve_content_type,
    _utcnow,
    approve,
    assign_customer,
    content_type_allows,
    covers_pair,
    create,
    delete,
    entry_count,
    get,
    list_entries,
    list_for_customer,
    list_glossaries,
    list_unassigned,
    log,
    option_label,
    options_for_step,
    page_entries,
    pairs_for,
    preview_terms,
    rename,
    set_content_type,
    term_counts,
    to_dict,
    touch_draft,
    unapprove,
    visible,
)

__all__ = [
    "_NO_CT_FILTER",
    "Customer",
    "GlossaryModuleError",
    "GlossaryPlan",
    "GlossaryTerm",
    "NamedTuple",
    "_canon",
    "_compat_fingerprint",
    "_concept_fingerprint",
    "_concept_shaped_import",
    "_import_changed_content",
    "_norm",
    "_parse_pairs",
    "_reject_archived_customer",
    "_require",
    "_resolve_content_type",
    "_utcnow",
    "approve",
    "assign_customer",
    "content_type_allows",
    "covers_pair",
    "create",
    "datetime",
    "delete",
    "entry_count",
    "get",
    "ids",
    "languages_catalog",
    "list_entries",
    "list_for_customer",
    "list_glossaries",
    "list_unassigned",
    "log",
    "logging",
    "option_label",
    "options_for_step",
    "page_entries",
    "pairs_for",
    "preview_terms",
    "rename",
    "set_content_type",
    "term_counts",
    "to_dict",
    "touch_draft",
    "unapprove",
    "visible",
]


# --- language-pair association ----------------------------------------------
def set_pairs(session: Session, glossary_id: int, pairs) -> list[GlossaryPair]:
    """Replace a glossary's language pairs (delete-all-then-insert; the edit path).

    `pairs` is an iterable of (source, target) tuples/lists OR "SRC:TGT"/"SRC>TGT"
    strings. Locales are canonicalized; a blank source/target is rejected. Editing
    pairs is a CONTENT change -> reverts approval to draft (touch_draft)."""
    from app.services import glossary_concepts as _gc

    _require(session, glossary_id)
    parsed = _parse_pairs(pairs)
    # A.4-8: the pairs configured BEFORE the delete-all, canonicalized, so a resubmit spelled
    # es_ES or reordered is not an "addition" (the widening must be idempotent).
    before = {
        (_norm(p.source_locale), _norm(p.target_locale))
        for p in session.exec(
            select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
        ).all()
    }
    for row in session.exec(
        select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
    ).all():
        session.delete(row)
    out = _gc.SetPairsResult()
    for src, tgt in parsed:
        row = GlossaryPair(glossary_id=glossary_id, source_locale=src, target_locale=tgt)
        session.add(row)
        out.append(row)
    session.flush()
    # A.4-8 widening: for each ADDED pair, create bindings for the base concepts holding a
    # usable target (or a DNT source) in the added target locale, then project. Removed pairs
    # leave their bindings in place (a log line, never deleted). Idempotent: no added pair -> no
    # binding write. set_pairs STILL COMMITS once, at the end (R6-M3).
    added = [(src, tgt) for src, tgt in parsed if (_norm(src), _norm(tgt)) not in before]
    if added:
        cids = list(
            session.exec(
                select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == glossary_id)
            ).all()
        )
        if cids:
            created, dnt_ext = _gc.create_entry_bindings(session, glossary_id, cids, pairs=added)
            out.bindings_created = created
            out.dnt_bindings_extended = dnt_ext
            _gc.project(session, glossary_id, concept_ids=cids, derive_senses=True)
    removed = before - {(_norm(src), _norm(tgt)) for src, tgt in parsed}
    if removed:
        log.info(
            "glossary %s: pairs removed %s; existing bindings left in place",
            glossary_id,
            removed,
        )
    session.commit()
    touch_draft(session, glossary_id)
    return out


def build_glossary_plan(
    session: Session, project, source_locale, target_locale, pinned_glossary_id=None, notes=None
) -> GlossaryPlan:
    """Resolve the glossary sources for a translation of `project` on (src, tgt).

    THE AUTO-ATTACH GATE (SPEC-content-types §3.4 + the byte-identical invariant):

      1. An explicit step PIN wins as an exclusive override — use ONLY that glossary,
         no auto-attach, no legacy merge (byte-identical to the pre-phase-2 gid=X path).
         The caller (orchestrator._resolved_glossary_id) has already applied the
         content-type SKIP, so any pin that reaches here is content-type-valid.

      2. No pin + an UNTYPED project (content_type_id is None — EVERY existing project
         and every content-type-free install) -> auto-attach NOTHING; the plan is
         {legacy per-project entries only}, exactly the pre-phase-2 gid=None path. This
         is the gate that preserves byte-identical behaviour: auto-attach of even a
         General (NULL-type) glossary NEVER fires on an untyped project, so the existing
         glossary/orchestrator/translate suites (untyped fixtures) see no new terms.

      3. No pin + a TYPED project -> auto-attach every APPROVED glossary visible to the
         project (its customer OR global), covering the pair, of the project's content
         type OR General (NULL). Precedence: content-type-specific first, then General,
         id-ascending within each tier; legacy per-project entries merge last.

    PROJECT-LEVEL PINS (feedback item 5, ProjectGlossary) are unioned in ahead of the
    auto-attach tiers, for BOTH typed and untyped projects. This does not weaken the
    gate in rule 2, it sits beside it: that gate protects IMPLICIT attach — the case
    where a glossary appears in a run because of a type match nobody asked for. A
    project pin is an EXPLICIT act (somebody clicked Attach, or ran `project glossaries
    add`), so an untyped project honouring it is the requested behaviour, not a
    surprise. The byte-identical guarantee is preserved by the only thing that could
    still surprise an existing install: with NO pin rows, project_sources returns (),
    every branch below reduces to exactly its previous expression, and rule 2 still
    returns the identical ((), True) plan.

    Pins rank FIRST because explicit beats implicit — a pinned glossary's term wins a
    conflict against an auto-attached one — and they are ordered by when they were
    pinned so precedence between two pins is stable and explainable. They are NOT
    content-type filtered (again: explicit choice; the content-type filter exists to
    decide what to attach on the operator's behalf, and here they already decided), but
    they ARE filtered by approval and by the org/customer ring, re-derived at run time
    from the live Glossary row — see project_sources.pinned_glossary_ids. A step pin
    still overrides everything, unchanged, in rule 1 above."""
    if pinned_glossary_id is not None:
        return GlossaryPlan(glossary_ids=(pinned_glossary_id,), include_legacy=False)

    # Explicit project-level pins. () for every project that has none, which is what
    # keeps both branches below byte-identical for existing installs.
    from app.services import project_sources

    pinned = project_sources.pinned_glossary_ids(session, project)

    project_ct = getattr(project, "content_type_id", None)
    if project_ct is None:
        # Untyped project: NO *implicit* auto-attach (the byte-identical gate). Legacy
        # entries only -- plus any EXPLICIT pins, which the gate was never about.
        if pinned and notes is not None:
            notes.append(
                f"project-pinned {len(pinned)} approved glossary(ies) "
                f"[{', '.join(str(i) for i in pinned)}]"
            )
        return GlossaryPlan(glossary_ids=tuple(pinned), include_legacy=True)

    # Typed project: gather matching approved module glossaries (customer OR global).
    stmt = select(Glossary)
    # NULL-org normalization (re-review NEW-1). This filter is older than the attach
    # feature and carried the same latent bug main has today: off-gate NOTHING writes
    # org_id (content._org_for stamps NULL unless the sacred gate is on) and
    # db._ensure_default_org backfills only AT BOOT, so a project created since the last
    # restart has org_id NULL and this filter silently auto-attached NOTHING, while the
    # same project after a restart auto-attached normally. access.org_id_clause
    # normalizes both sides (NULL -> the install's default org) so the answer stops
    # depending on uptime; under the sacred gate it is the plain equality it always was.
    from app.services import access as _access

    org_clause = _access.org_id_clause(
        Glossary.org_id, _access.normalized_org_id(getattr(project, "org_id", None))
    )
    if org_clause is not None:
        stmt = stmt.where(org_clause)
    cust = project.customer_id
    if cust is None:
        stmt = stmt.where(Glossary.customer_id.is_(None))  # un-customer -> global only
    else:
        stmt = stmt.where((Glossary.customer_id.is_(None)) | (Glossary.customer_id == cust))
    specific, general = [], []
    for g in session.exec(stmt.order_by(Glossary.id)).all():
        if g.status != GlossaryStatus.APPROVED:
            continue
        if not content_type_allows(g.content_type_id, project_ct):
            continue
        if not covers_pair(session, g.id, source_locale, target_locale):
            continue
        if g.id in pinned:
            continue  # already ranked ahead of both tiers by the pin
        if g.content_type_id == project_ct:
            specific.append(g.id)
        else:  # content_type_allows -> the only other case is NULL
            general.append(g.id)
    auto = tuple(specific + general)
    # Pins first (explicit > implicit), then the two auto tiers. With no pins this is
    # `tuple(() ) + auto == auto` -- the identical value the pre-feature code returned.
    ordered = tuple(pinned) + auto
    if notes is not None and pinned:
        notes.append(
            f"project-pinned {len(pinned)} approved glossary(ies) "
            f"[{', '.join(str(i) for i in pinned)}]"
            + ("; a project pin outranks an auto-attached glossary" if auto else "")
        )
    if notes is not None and auto:
        notes.append(
            f"auto-attached {len(auto)} approved glossary(ies) for content type "
            f"{project_ct} [{', '.join(str(i) for i in auto)}]"
            + (
                "; conflicts resolve content-type-specific > general > id-ascending"
                if len(auto) > 1
                else ""
            )
        )
    return GlossaryPlan(glossary_ids=ordered, include_legacy=True)


def match_terms_merged(
    session: Session,
    project_id: int,
    segment_text: str,
    source_locale: str,
    target_locale: str,
    plan: GlossaryPlan,
    provider=None,
    conflict_state=None,
    disambig=None,
) -> list[dict]:
    """Merge the glossary term constraints for ONE segment across a GlossaryPlan.

    `disambig` (Gap D, SPEC-context-metadata phase 2A): a short, sanitized
    domain/audience hint from the run's resolved context package, forwarded verbatim
    to each source's match_terms so multi-sense disambiguation knows the domain.
    None/"" leaves sense selection byte-identical (no context package -> no change).

    Sources are consulted in the plan's precedence order (module glossary_ids first,
    then — when include_legacy — the project's legacy per-project entries). FIRST writer
    wins per source_term: a higher-precedence source binds the target; a lower one is
    dropped. On a genuine conflict (a dropped term whose target/DNT DIFFERS) a one-time
    run-trace warning is logged, deduped across segments via the caller-owned
    `conflict_state` set.

    BYTE-IDENTICAL: a single-source plan — one glossary_id, or legacy-only — returns
    EXACTLY glossary.match_terms(...) in its original order (the dict preserves
    insertion order and every source_term within one source is unique for a fixed
    locale pair), so the pre-phase-2 gid=X and gid=None paths are unchanged."""
    out: dict = {}

    def _consume(terms):
        for t in terms:
            st = t.get("source_term")
            if st in out:
                if conflict_state is not None and st not in conflict_state:
                    prev = out[st]
                    if prev.get("target_term") != t.get("target_term") or prev.get("dnt") != t.get(
                        "dnt"
                    ):
                        conflict_state.add(st)
                        log.info(
                            "glossary merge: source term %r bound to %r by a "
                            "higher-precedence glossary; dropped conflicting %r",
                            st,
                            prev.get("target_term"),
                            t.get("target_term"),
                        )
                continue
            out[st] = t

    for gid in plan.glossary_ids:
        _consume(
            gloss_engine.match_terms(
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
            gloss_engine.match_terms(
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
    # Longest match wins ACROSS sources (#663): match_terms shadows within one glossary
    # only, so "Colorado Trail" (module glossary) and a generic `trail` (legacy) would
    # both reach the prompt. A single-source plan is returned exactly as match_terms
    # produced it (byte-identical contract above).
    if len(plan.glossary_ids) + int(bool(plan.include_legacy)) > 1:
        merged = gloss_engine.drop_shadowed_terms(merged, segment_text)
    return merged


# --- term editing (delegates to the run-time engine, then reverts approval) --
def add_term(
    session: Session,
    glossary_id: int,
    source_term: str,
    target_term: str = "",
    source_locale: str = "",
    target_locale: str = "",
    dnt: bool = False,
    meta: dict | None = None,
) -> GlossaryEntry | None:
    """Add (or upsert) a term. T2-03: source_locale/target_locale go through the same
    catalog validation as pairs (`_canon`) — blank stays blank (a wildcard "any locale"
    term, the legitimate case), but a non-blank unknown/garbage code raises
    GlossaryModuleError instead of being stored verbatim.

    `meta` (term database, 2026-07-14): optional TBX-ish metadata merged onto the
    term (sanitized + allowlisted by the engine). None = untouched. Content edit ->
    reverts approval."""
    _require(session, glossary_id)
    src_loc, tgt_loc = _canon(source_locale), _canon(target_locale)
    entry = gloss_engine.add_term(
        session,
        project_id=None,
        source_term=source_term,
        target_term=target_term,
        source_locale=src_loc,
        target_locale=tgt_loc,
        dnt=dnt,
        glossary_id=glossary_id,
        meta=meta,
    )
    if entry is not None:
        touch_draft(session, glossary_id)
    return entry


def edit_term(
    session: Session,
    glossary_id: int,
    entry_id: int,
    target_term=None,
    dnt=None,
    meta=None,
    case_sensitive=None,
) -> GlossaryEntry | None:
    """Edit an existing term of THIS glossary (the row editor / CLI / API / MCP). Verifies the
    entry belongs to the glossary (else None — a forged entry_id can't edit a foreign
    glossary's term). Delegates the field update to the engine (meta sanitized there),
    then reverts approval (a content edit). Returns the entry, or None if the entry_id
    is unknown or not owned by this glossary.

    ``case_sensitive`` (A.3, M-2): the per-pair case-sensitivity fact on the binding. None ->
    unchanged; a bool -> set on the binding. Carried by all four surfaces so the row editor,
    CLI ``edit-term``, ``/api`` PATCH and the MCP entry action can toggle it."""
    _require(session, glossary_id)
    entry = session.get(GlossaryEntry, entry_id)
    if entry is None or entry.glossary_id != glossary_id:
        return None
    updated = gloss_engine.update_entry(
        session,
        entry_id,
        target_term=target_term,
        dnt=dnt,
        meta=meta,
        case_sensitive=case_sensitive,
    )
    if updated is not None:
        touch_draft(session, glossary_id)
    return updated


def remove_term(session: Session, glossary_id: int, entry_id: int) -> bool:
    """Delete one entry (and its senses) from a glossary. Reverts approval.

    Runs the LAW-TERM-2b compat-ENTRY delete UNIT in the SAME transaction: after the
    senses + entry are deleted, drop the concept-store projection behind them (the source
    term when nothing else references it — H1; each orphaned target term; sibling homonym
    concepts; the base concept iff termless) so the append/update-only reconciler cannot
    resurrect the just-deleted row."""
    from app.services import glossary_concepts

    _require(session, glossary_id)
    entry = session.get(GlossaryEntry, entry_id)
    if entry is None or entry.glossary_id != glossary_id:
        return False
    # R2-7: the shared body — deletes the senses (flushed first), the entry, then runs the
    # LAW-TERM-2b compat-ENTRY delete unit. One copy of the FK-ordering-sensitive routine.
    glossary_concepts._delete_entry_and_unit(session, glossary_id, entry)
    session.commit()
    touch_draft(session, glossary_id)
    return True


def add_sense(
    session: Session,
    glossary_id: int,
    entry_id: int,
    sense_label: str,
    target_term: str,
    context_cue: str = "",
) -> GlossarySense | None:
    """Add a context-dependent sense to an entry of this glossary. Reverts approval."""
    _require(session, glossary_id)
    entry = session.get(GlossaryEntry, entry_id)
    if entry is None or entry.glossary_id != glossary_id:
        return None
    label = (sense_label or "").strip()
    if not label:
        raise GlossaryModuleError("a sense needs a label.")
    sense = GlossarySense(
        glossary_entry_id=entry_id,
        sense_label=label,
        context_cue=(context_cue or "").strip(),
        target_term=(target_term or "").strip(),
    )
    session.add(sense)
    session.flush()
    # Dual-write (LAW-TERM-2/§4.5): fold the new sense into a homonym-sibling concept in
    # the same transaction (re-syncs the whole entry so the base concept's homonym_group
    # is set and the sense's concept_id/term_id link the sibling). Module entries only.
    from app.services import glossary_concepts

    # add_sense writes the sense row + links the sibling concept via sync_entry_concept (the
    # sense's derived columns are the row's own); it does NOT project (matching the legacy
    # trigger points -- only add_term/edit_term/import re-derive), so the group's sibling-sense
    # set is materialised at those seams, not multiplied here.
    glossary_concepts.sync_entry_concept(session, entry)
    session.commit()
    session.refresh(sense)
    touch_draft(session, glossary_id)
    return sense


def _import_locale(code: str) -> str:
    """Canonicalize an import's locale the way add_term does (`es_ES`, `es-es` -> `es-ES`),
    because matching is exact SQL equality (#510). An unknown code is passed through
    unchanged: the importer has always accepted it and a refusal here would be new."""
    try:
        return _canon(code)
    except GlossaryModuleError:
        return code


def _revert_after_partial_import(session: Session, glossary_id: int, exc) -> None:
    """A big import that failed after some chunks committed (#615) HAS changed the glossary:
    revert an APPROVED glossary to draft exactly as a finished import would, so a workflow step
    pinned to it cannot keep running on content nobody approved. Importing the same file again
    finishes the job."""
    if getattr(exc, "content_changed", True):
        touch_draft(session, glossary_id)


def import_csv(
    session: Session, glossary_id: int, data: bytes, source_locale: str, target_locale: str
) -> dict:
    """Import a CSV into a module glossary (delegates to the engine importer with
    glossary_id set). Reverts approval when the import CHANGED the glossary's
    content -- see _import_changed_content above for the exact rule.

    RETURN SHAPE is deliberately the engine's narrow legacy {"entries", "senses"}
    (this seam has always mirrored gloss_engine.import_csv, which has its own
    exact-equality regression tests). It calls `import_csv_full` internally purely
    so the revert decision can see the `changed` counter -- the two counts it
    publishes cannot express "an existing term was rewritten"."""
    _require(session, glossary_id)
    source_locale, target_locale = _import_locale(source_locale), _import_locale(target_locale)
    try:
        res = gloss_engine.import_csv_full(
            session,
            project_id=None,
            data=data,
            source_locale=source_locale,
            target_locale=target_locale,
            glossary_id=glossary_id,
        )
    except gloss_engine.GlossaryImportIncomplete as exc:
        _revert_after_partial_import(session, glossary_id, exc)
        raise
    if _import_changed_content(res):
        touch_draft(session, glossary_id)
    return {"entries": res["entries"], "senses": res["senses"]}


def import_bytes(
    session: Session,
    glossary_id: int,
    data: bytes,
    source_locale: str,
    target_locale: str,
    filename: str | None = None,
    *,
    strict: bool = False,
    concepts: bool = False,
) -> dict:
    """Import a CSV **or** TBX termbase into a module glossary (delegates to the
    engine's format-dispatching importer). Raises glossary_tbx.TbxError
    (user-facing message) on an unreadable TBX.

    APPROVAL REVERT -- THE RULE, as of 2026-07-29 (the maintainer's call, closing the gap
    the previous review round flagged rather than fixed). An import reverts an
    APPROVED glossary to DRAFT when it CHANGED the glossary's content: it created
    a term, created a sense, or rewrote a stored value on a term or sense that
    already existed. An IDEMPOTENT re-import -- the same file again, every row
    byte-identical to what is stored -- changes nothing and leaves an approved
    glossary APPROVED. See `_import_changed_content` for why both halves of that
    rule are load-bearing, and glossary._apply_rows's COUNTERS section for how
    `changed` is computed (tm.import_tmx's per-unit value-changed semantics).

    WHAT THIS FIXED: gating on `entries or senses` alone -- creation counts --
    meant an import that REWRITES every existing term (new translations for the
    whole termbase, new DNT flags, new metadata) created nothing and so left the
    glossary APPROVED with different content underneath it, while editing ONE
    term in the UI correctly reverted. Two smaller things had to change with it:
    the naive alternative (`updated`) counts byte-identical rewrites and would
    have broken idempotency instead, hence the new `changed` counter; and
    _apply_rows's sense INSERT had to become an UPSERT by (entry, sense_label),
    because the duplicate-sense bug made `senses` non-zero on EVERY sense-bearing
    re-import and was masking the idempotency this rule promises."""
    _require(session, glossary_id)
    source_locale, target_locale = _import_locale(source_locale), _import_locale(target_locale)
    # M4 (round-1): the concept-preserving arm (a TBX `--concepts` import or a
    # `.glossary.json` import) returns written-concept/term COUNTS, not the entry path's
    # `changed`/`unchanged` value-diff. Reporting those counts as "changed content" made an
    # idempotent re-import always revert an APPROVED glossary to draft (re-arming the pinned-
    # workflow breakage `_import_changed_content` exists to prevent). Detect a real change the
    # same way the entry path does -- on the compat store (the enforcement surface): snapshot
    # it before and after, and set `changed` from the diff. The default TBX import and every
    # CSV import take the entry path and carry their own `changed`, so they skip the snapshot.
    concept_arm = _concept_shaped_import(data, filename, concepts)
    before = _compat_fingerprint(session, glossary_id) if concept_arm else None
    try:
        res = gloss_engine.import_bytes(
            session,
            project_id=None,
            data=data,
            source_locale=source_locale,
            target_locale=target_locale,
            glossary_id=glossary_id,
            filename=filename,
            strict=strict,
            concepts=concepts,
        )
    except gloss_engine.GlossaryImportIncomplete as exc:
        _revert_after_partial_import(session, glossary_id, exc)
        raise
    if concept_arm and "changed" not in res:
        res = {**res, "changed": int(_compat_fingerprint(session, glossary_id) != before)}
    if _import_changed_content(res):
        touch_draft(session, glossary_id)
    return res


def export_bytes(
    session: Session,
    glossary_id: int,
    fmt: str = gloss_engine.CSV_FORMAT,
    *,
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
    drops_out: list[str] | None = None,
) -> bytes:
    """Export a module glossary as CSV (default), TBX-Basic v3 (DCA), or `.glossary.json`.
    TBX defaults to the TBX-Basic dialect with the los: private namespace;
    `dialect="tbx-core"` and `strict_portable=True` drop los: (§6.4/§6.5), filling `drops_out`
    (if passed) with the NAMES of the dropped field categories (round-1 M1). A read, so —
    like export_csv — it does NOT revert approval."""
    _require(session, glossary_id)
    return gloss_engine.export_bytes(
        session,
        project_id=None,
        glossary_id=glossary_id,
        fmt=fmt,
        dialect=dialect,
        strict_portable=strict_portable,
        drops_out=drops_out,
    )


def export_csv(session: Session, glossary_id: int) -> bytes:
    """Export a module glossary to CSV (delegates to the engine exporter with
    glossary_id set) -- round-trips import_csv above, mirroring the legacy
    per-project glossary.export_csv. Includes the full TBX-ish meta columns (blank
    when a term has no meta). A read, so it does NOT revert approval or touch the
    glossary's draft state (unlike import)."""
    _require(session, glossary_id)
    return gloss_engine.export_csv(session, project_id=None, glossary_id=glossary_id)
