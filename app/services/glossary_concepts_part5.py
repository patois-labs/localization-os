"""glossary_concepts: part 5 (layered pure move)

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import logging
import uuid

from sqlmodel import Session, select

from app.models import (
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossaryTerm,
)
from app.services.glossary_concepts_common import (
    ConceptError,
    _concepts_share_a_term,
    _configured_pairs,
    _pick_target_term,
    _sanitize,
    _usable_source_terms,
    _utcnow,
    _valid_status,
    _valid_term_type,
    canon_audience,
)
from app.services.glossary_concepts_part2 import (
    _apply_doc_bindings,
    _match_doc_concept_by_content,
    _refuse_doc_source_surface_collisions,
    _sanitize_counted,
)
from app.services.glossary_concepts_part3 import create_entry_bindings
from app.services.glossary_concepts_part4 import project

log = logging.getLogger("loc_tms.glossary_concepts")


def from_v1(
    session: Session,
    glossary_id: int,
    doc: dict,
    *,
    rename: bool = False,
    enforce_projection: bool = False,
) -> dict:
    """Import a ``glossary-concept/v1`` document into an EXISTING module glossary
    (LAW-TERM-8/9/11/12). The target is ALWAYS ``glossary_id`` (route scope, LAW-TERM-12) —
    the header's id/customer_id/content_type_id/generator are IGNORED (M3). Writes the
    concept/term store as the PRIMARY write, then :func:`project` derives the compat
    entries. Returns the JSON counter dict ``{concepts, terms, dropped_refs,
    truncated_fields, projected_entries}`` (§5.3/M5).

    ``projected_entries`` (round-1 H2) is the count of concept-linked compat entries the
    store projects for the glossary's configured pairs — how many terms actually ENFORCE.
    With ``enforce_projection`` (the TBX import path), a document that wrote terms but
    projects 0 entries for the glossary's pair(s) is REFUSED before commit (a glossary that
    would enforce nothing is the fail-open class), rolling the whole import back."""
    schema = (doc or {}).get("schema")
    if not schema or not str(schema).startswith("glossary-concept/v"):
        raise ConceptError("not a glossary-concept document (missing/unknown schema).")
    major = str(schema).split("/v", 1)[1].split(".", 1)[0]
    if major != "1":
        raise ConceptError(f"unsupported glossary-concept major version: {schema}")
    # #576: a concept document may not carry more concepts than MAX_DOCUMENT_ROWS.
    from app import config as _config

    if len((doc or {}).get("concepts") or []) > _config.MAX_DOCUMENT_ROWS:
        raise ConceptError(
            f"This glossary file has more than {_config.MAX_DOCUMENT_ROWS} concepts "
            "(MAX_DOCUMENT_ROWS). Nothing was imported. Split it into smaller files."
        )
    # A.6/A.7: the concept-preserving import is UNGATED on every install (the inversion makes it
    # safe — it upserts only the NAMED concepts and derives the compat rows by projection, so it
    # can never rewrite an untouched pre-existing row, the round-7 write-through/prune class).
    if rename:
        name = ((doc.get("glossary") or {}).get("name") or "").strip()
        if name:
            g = session.get(Glossary, glossary_id)
            if g is not None:
                g.name = name
                session.add(g)

    # R3-5: refuse a document that carries two UN-GROUPED concepts sharing a source-locale
    # surface (they would silently half-import — the compat store enforces one, the editor
    # shows two). A grouped (homonym) collision is fine.
    _refuse_doc_source_surface_collisions(session, glossary_id, doc)

    counters = {
        "concepts": 0,
        "terms": 0,
        "dropped_refs": 0,
        "truncated_fields": 0,
        "id_conflicts": 0,
        "concepts_added": 0,
        "concepts_replaced": 0,
        "bindings_written": 0,
        "terms_created_from_bindings": 0,
        "sibling_targets_skipped": 0,
        "bindings_pinned": 0,
    }
    # A.6: concept row + its explicit `bindings` doc list, applied after the term upsert pass.
    doc_bindings: list[tuple[GlossaryConcept, list]] = []
    # First pass: UPSERT concepts + terms (M4 — keyed by (glossary_id, external_id) and
    # (concept_id, locale, term), so a re-import of the same document does NOT duplicate
    # the store), tracking doc external_id -> concept row and (concept_doc_id, locale,
    # surface) -> term row for replacement/xref relinking. A BLANK external_id cannot be an
    # upsert key, so such a concept is always inserted (documented in errata E-T1-M4).
    doc_id_to_concept: dict[str, GlossaryConcept] = {}
    term_index: dict[tuple[str, str, str], GlossaryTerm] = {}
    pending_replacements: list[tuple[GlossaryTerm, dict]] = []
    # round-2 H1: the concepts THIS import upserted -- `projected_entries` counts the
    # entries these concepts enforce, never a pre-existing unrelated row.
    # H-4: every concept this import has already handled (matched or minted). The
    # content-identity reconcile excludes it so homonym siblings never collapse onto one concept
    # and a fresh import never re-matches a concept it just created in its own first pass.
    touched_concept_ids: set[int] = set()
    for cdoc in doc.get("concepts", []) or []:
        ext = _sanitize((cdoc.get("external_id") or "").strip())
        concept = None
        if ext:
            concept = session.exec(
                select(GlossaryConcept).where(
                    GlossaryConcept.glossary_id == glossary_id,
                    GlossaryConcept.external_id == ext,
                )
            ).first()
            # R3-3: an external_id match is only a real UPSERT when the stored concept
            # actually shares a term surface with the incoming one — otherwise this is a
            # FOREIGN document whose id happens to collide, and reusing the local concept
            # would hijack it. In that case do NOT reuse: create a fresh concept (with a new
            # unique id) and COUNT the conflict.
            if concept is not None and not _concepts_share_a_term(session, concept, cdoc):
                counters["id_conflicts"] += 1
                log.warning(
                    "glossary %s: incoming concept external_id %r matched a local concept "
                    "(id %s) with no shared term — importing as a NEW concept, not upserting.",
                    glossary_id,
                    ext,
                    concept.id,
                )
                concept = None
                ext = ""  # let the new concept get its own minted identity below
        if concept is None:
            # T2 hand-off #3/#4 (H-4/H-5): reconcile an id-less (or id-changed) re-import by
            # CONTENT IDENTITY -- an existing concept of the same identity is the SAME concept, not
            # a new one (else three id-less re-imports grow the store 2 -> 4 -> 6). Base-language
            # aware (a bare `en` doc source matches an `en-US` stored term, H-5), one-to-one within
            # the import (a claimed stored concept is never reused, so homonym siblings do not
            # collapse, H-4). When nothing matches, mint below rather than steal.
            hg_doc = _sanitize(cdoc.get("homonym_group", ""))
            concept = _match_doc_concept_by_content(
                session, glossary_id, cdoc, hg_doc, touched_concept_ids
            )
        if concept is None:
            concept = GlossaryConcept(glossary_id=glossary_id, external_id=ext)
            session.add(concept)
            counters["concepts_added"] += 1
        else:
            counters["concepts_replaced"] += 1
        concept.domain = _sanitize_counted(cdoc.get("domain", ""), counters)
        concept.subdomain = _sanitize_counted(cdoc.get("subdomain", ""), counters)
        concept.definition = _sanitize_counted(cdoc.get("definition", ""), counters)
        concept.notes = _sanitize_counted(cdoc.get("notes", ""), counters)
        concept.source = _sanitize(cdoc.get("source", ""))
        concept.homonym_group = _sanitize(cdoc.get("homonym_group", ""))
        concept.created_by = _sanitize(cdoc.get("created_by", ""))
        concept.updated_at = _utcnow()
        session.flush()
        # R3-3: a concept created here without a document id gets its own globally-unique
        # identity (never a row id), so a later export of THIS glossary round-trips cleanly.
        if not concept.external_id:
            concept.external_id = str(uuid.uuid4())
            session.flush()
        counters["concepts"] += 1
        touched_concept_ids.add(concept.id)
        _blist = cdoc.get("bindings")
        if isinstance(_blist, list) and _blist:
            doc_bindings.append((concept, _blist))
        if ext:
            doc_id_to_concept[ext] = concept
        for tdoc in cdoc.get("terms", []) or []:
            surface = tdoc.get("term")
            if not surface:
                continue
            loc = tdoc.get("locale") or ""
            term = session.exec(
                select(GlossaryTerm).where(
                    GlossaryTerm.concept_id == concept.id,
                    GlossaryTerm.locale == loc,
                    GlossaryTerm.term == surface,
                )
            ).first()
            if term is None:
                term = GlossaryTerm(concept_id=concept.id, locale=loc, term=surface)
                session.add(term)
            term.status = _valid_status(tdoc.get("status"))
            term.term_type = _valid_term_type(tdoc.get("term_type"))
            term.part_of_speech = _sanitize_counted(tdoc.get("part_of_speech", ""), counters)
            term.grammatical_gender = _sanitize(tdoc.get("grammatical_gender", ""))
            term.grammatical_number = _sanitize(tdoc.get("grammatical_number", ""))
            term.geographical_usage = _sanitize(tdoc.get("geographical_usage", ""))
            term.audience = canon_audience(tdoc.get("audience"))
            term.usage_note = _sanitize_counted(tdoc.get("usage_note", ""), counters)
            term.context_example = _sanitize_counted(tdoc.get("context_example", ""), counters)
            term.case_sensitive = bool(tdoc.get("case_sensitive", False))
            term.exact = bool(tdoc.get("exact", False))
            term.dnt = bool(tdoc.get("dnt", False))
            term.source = _sanitize(tdoc.get("source", ""))
            term.updated_at = _utcnow()
            session.flush()
            counters["terms"] += 1
            key = (ext or f"c{concept.id}", term.locale or "", term.term)
            term_index[key] = term
            repl = tdoc.get("deprecated_replacement")
            if isinstance(repl, dict):
                pending_replacements.append((term, repl))
    # Second pass: relink deprecated_replacement by (concept, locale, surface) triple.
    for term, repl in pending_replacements:
        key = (
            (repl.get("concept") or "").strip(),
            (repl.get("locale") or ""),
            (repl.get("surface") or ""),
        )
        target = term_index.get(key)
        if target is None:
            counters["dropped_refs"] += 1
            continue
        term.deprecated_replacement_term_id = target.id
        session.add(term)
    # cross_refs: resolve string doc ids -> concept ids, drop unresolved (counted).
    for cdoc in doc.get("concepts", []) or []:
        ext = (cdoc.get("external_id") or "").strip()
        if not ext:
            continue
        concept = doc_id_to_concept.get(ext)
        if concept is None:
            continue
        resolved: list = []
        for rid in cdoc.get("cross_refs", []) or []:
            if isinstance(rid, str) and rid in doc_id_to_concept:
                resolved.append(doc_id_to_concept[rid].id)
            else:
                counters["dropped_refs"] += 1
        concept.cross_refs = resolved or None
        session.add(concept)
    session.flush()
    # A.6: apply explicit per-concept `bindings` blocks verbatim (field-level, incl.
    # meta_overrides + dnt/case_sensitive + a target created as `admitted` when absent).
    bound_concept_ids: set[int] = set()
    for concept, blist in doc_bindings:
        _apply_doc_bindings(session, glossary_id, concept, blist, counters)
        bound_concept_ids.add(concept.id)
    session.flush()
    # A.6 no-`bindings` fallback: create still-missing bindings per A.4-2 for the configured pairs
    # ONLY for concepts the document did NOT bind explicitly (concept-first: no meta_overrides).
    # A concept whose bindings block is present is AUTHORITATIVE and must not be auto-widened into
    # a pair its bindings never named -- that manufactured the LT3-1 reverse-direction collision
    # (M-7). The projector then fills the derived columns + derives sibling senses.
    fallback_ids = [cid for cid in touched_concept_ids if cid not in bound_concept_ids]
    if fallback_ids:
        create_entry_bindings(session, glossary_id, fallback_ids)
    project(session, glossary_id, derive_senses=True)
    # L-1: a bindings-bearing concept is AUTHORITATIVE and is deliberately NOT auto-widened into a
    # configured pair its `bindings` block never named (that manufactured the LT3-1 reverse-
    # direction collision, M-7). But a ONE-pair export restored into a WIDER glossary then leaves a
    # configured pair the concept COULD enforce (it carries a usable source + target for it) UNBOUND
    # -- silent on 5ac14235 (only `projected_entries` hinted at it). COUNT each such (concept, pair)
    # as `bindings_skipped` and WARN naming the pair, so the under-binding is observable on every
    # surface. The binding is intentionally NOT created (M-7/LT3-1 parity holds).
    skipped = 0
    if bound_concept_ids:
        cfg_widen = _configured_pairs(session, glossary_id)
        for cid in bound_concept_ids:
            cterms = session.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == cid)).all()
            bound_tls = {
                (e.target_locale or "")
                for e in session.exec(
                    select(GlossaryEntry).where(
                        GlossaryEntry.glossary_id == glossary_id,
                        GlossaryEntry.concept_id == cid,
                    )
                ).all()
            }
            for sl, tl in cfg_widen:
                if (tl or "") in bound_tls or "" in bound_tls:
                    continue  # a binding (or a wildcard binding) already covers this target locale
                srcs = _usable_source_terms(cterms, sl)
                if not srcs:
                    continue
                if any(
                    _pick_target_term(cterms, tl, exclude_id=s.id, source_locale=sl) is not None
                    for s in srcs
                ):
                    skipped += 1
                    log.warning(
                        "glossary %s: concept %s carries an explicit bindings block and is NOT "
                        "auto-widened into the configured pair %s->%s it could enforce; that pair "
                        "is left unbound (bindings_skipped).",
                        glossary_id,
                        cid,
                        sl or "*",
                        tl or "*",
                    )
    counters["bindings_skipped"] = skipped
    # round-2 H1: `projected_entries` = the concept-linked compat entries THIS import's
    # concepts project that actually ENFORCE for a configured pair -- how many terms the
    # match path can select. The round-1 counter counted EVERY concept-linked row of the
    # glossary, so (a) it counted rows stored at a locale the exact-match selection core
    # never selects (the base-fallback bug, now fixed above), and (b) it counted a
    # PRE-EXISTING unrelated entry, letting a termbase that enforces nothing defeat the
    # fail-closed guard by riding on prior content (gate probe P7). Restrict to this
    # import's concepts and to rows whose (source_locale, target_locale) serves a configured
    # pair (blank = wildcard). An idempotent re-import still counts its own live entries, so
    # it is never falsely refused.
    cfg_pairs = [
        ((sl or ""), (tl or ""))
        for sl, tl in session.exec(
            select(GlossaryPair.source_locale, GlossaryPair.target_locale).where(
                GlossaryPair.glossary_id == glossary_id
            )
        ).all()
    ]

    def _enforces(e_sl: str, e_tl: str) -> bool:
        return any((e_sl in (sl, "")) and (e_tl in (tl, "")) for sl, tl in cfg_pairs)

    # round-3 M1: count "entries that enforce AFTER this import" by CONTENT IDENTITY (source
    # surface + source/target locale), never by conceptEntry id. Round 2 scoped the counter to
    # THIS import's ``touched_concept_ids`` alone. But a re-import whose conceptEntry ids are
    # REGENERATED or ABSENT mints all-new concept rows (a fresh ``external_id``), and those
    # rows' SURFACES already enforce -- ``project`` dedupes their projection against the
    # entries the prior import left behind, so they add 0 NEW entries and the id-scoped counter
    # saw 0 and FALSELY refused an import whose languages match the pair EXACTLY (gate round-3
    # M1). The dedupe hit is proof the surface enforces, so also count every enforcing entry
    # whose (source_term, source_locale, target_locale) is a key THIS import's concepts project.
    import_keys: set[tuple[str, str, str]] = set()
    if touched_concept_ids and cfg_pairs:
        t_terms: dict[int, list[GlossaryTerm]] = {}
        for t in session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(list(touched_concept_ids)))
        ).all():
            t_terms.setdefault(t.concept_id, []).append(t)
        for cid in touched_concept_ids:
            cterms = t_terms.get(cid, [])
            for sl, tl in cfg_pairs:
                for src in _usable_source_terms(cterms, sl):
                    # round-4 M1: mirror _project_pair's GUARD, not just its key. _project_pair
                    # emits an entry for a pair ONLY when the concept has a usable TARGET for it
                    # (R4-3), the sole exception being a concept-first DNT source (R5-3, a
                    # targetless brand/product name). Without this guard `import_keys` counted a
                    # source surface alone as "enforcing", so a prior import's entry could
                    # satisfy the fail-closed guard for a NEW import that projects nothing (gate
                    # round-4 M1, case A). A key only belongs here when this concept would
                    # actually project for the pair.
                    tgt = _pick_target_term(cterms, tl, exclude_id=src.id, source_locale=sl)
                    if tgt is None and not src.dnt:
                        continue
                    # mirror _project_pair's key: an exact/wildcard source keeps its own
                    # locale; a base-fallback source is promoted to the pair's source locale.
                    src_exact = (src.locale or "") == (sl or "") or (src.locale or "") == ""
                    eff_sl = (src.locale or "") if src_exact else (sl or "")
                    import_keys.add((src.term, eff_sl, tl or ""))
    projected = 0
    for e_sl, e_tl, e_src, e_cid in session.exec(
        select(
            GlossaryEntry.source_locale,
            GlossaryEntry.target_locale,
            GlossaryEntry.source_term,
            GlossaryEntry.concept_id,
        ).where(GlossaryEntry.glossary_id == glossary_id)
    ).all():
        sl_v, tl_v, src_v = (e_sl or ""), (e_tl or ""), (e_src or "")
        if not _enforces(sl_v, tl_v):
            continue
        if e_cid in touched_concept_ids or (src_v, sl_v, tl_v) in import_keys:
            projected += 1
    counters["projected_entries"] = projected
    # 0 enforcing entries with a non-empty termbase and >=1 pair means this import enforces
    # nothing for any configured pair (no concept provides both a usable source AND target for
    # a pair, even by base language): fail closed BEFORE commit rather than create a
    # silently-inert glossary. An import whose SURFACES already enforce (a re-import with
    # regenerated/absent ids) is counted above, so it is never falsely refused (round-3 M1).
    if enforce_projection and projected == 0 and counters.get("terms") and cfg_pairs:
        raise ConceptError(
            "this termbase imported terms but projects 0 entries that enforce for the "
            "glossary's configured language pair(s): no imported concept provides both a "
            "usable source and target term for a configured pair, even by base language. "
            "Check the termbase's locales; nothing was written."
        )
    session.commit()
    return counters
