"""glossary: part 2 (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import re

from sqlmodel import Session, select

from app.models import GlossaryEntry, GlossarySense
from app.services.glossary_common import (
    CSV_FORMAT,
    JSON_FORMAT,
    TBX_FORMAT,
    _entry_page,
    _entry_statement,
    _looks_like_json,
    _looks_like_xml,
    _page_bounds,
    _sync_concept,
    _term_in_text,
    clean_meta,
)


def detect_format(data: bytes, filename: str | None = None) -> str:
    """Which importer a payload wants: TBX_FORMAT or CSV_FORMAT.

    Extension FIRST (case-insensitive) — that is the documented, user-visible rule
    on every surface. When there is no usable name, SNIFF: the /api import carries
    raw base64 bytes with no filename, so a remote CLI must still land in the right
    parser. A leading '<' (after any BOM/whitespace, see _looks_like_xml) is XML and
    no glossary CSV can start that way; anything else stays CSV, which is the
    pre-existing behaviour.

    A .tbx name over CSV bytes deliberately still routes to the TBX parser: the user
    said TBX, so they get the TBX error rather than a silent zero-row CSV import.

    JSON (SPEC-terminology-backend D2): a `.glossary.json` (or `.json`) name, or — when
    there is no name — a first significant byte of `{` (disjoint from the `<`-XML sniff
    and CSV), routes to the concept-aware JSON importer."""
    name = (filename or "").strip().lower()
    if name.endswith((".tbx", ".mtf", ".xml")):
        # .xml/.mtf: a MultiTerm XML export (or a TBX saved as .xml); the TBX parser
        # reads both (glossary_tbx: <mtf><conceptGrp> and <martif>/<tbx>).
        return TBX_FORMAT
    if name.endswith(".glossary.json") or name.endswith(".json"):
        return JSON_FORMAT
    if name.endswith(".csv"):
        return CSV_FORMAT
    if _looks_like_xml(data or b""):
        return TBX_FORMAT
    if _looks_like_json(data or b""):
        return JSON_FORMAT
    return CSV_FORMAT


def add_term(
    session: Session,
    project_id: int,
    source_term: str,
    target_term: str = "",
    source_locale: str = "",
    target_locale: str = "",
    dnt: bool = False,
    note: str = "",
    glossary_id: int | None = None,
    meta: dict | None = None,
) -> GlossaryEntry | None:
    """Persist a single glossary term (the in-app one-click "Add term" path).

    Upserts on (project|glossary, source_term, source_locale, target_locale): a
    repeat add of the same term updates its target/DNT rather than creating a
    duplicate, matching the CSV importer's get_entry semantics. Returns the entry,
    or None when source_term is blank. `note` is accepted for API symmetry with the
    suggestions/builder payloads but isn't a stored column, so it's ignored.

    `meta` (2026-07-14, term database): an optional TBX-ish metadata dict
    (part_of_speech/definition/usage_example/subject_field/notes). When given, its
    ALLOWLISTED, sanitized cells are MERGED onto the entry's meta (reassigned so the
    JSON column is marked dirty). None leaves meta untouched (byte-identical to before
    this parameter existed). Meta is DATA about the term — it never enters a prompt.

    `glossary_id` (Phase 5-B): when given, the term is OWNED by that module Glossary
    (project_id stored NULL — a 0 sentinel violated the project.id FK on Postgres);
    when None the legacy project-scoped upsert is byte-identical."""
    source_term = (source_term or "").strip()
    if not source_term:
        return None
    if glossary_id is not None:
        stmt = select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == glossary_id,
            GlossaryEntry.source_term == source_term,
            GlossaryEntry.source_locale == source_locale,
            GlossaryEntry.target_locale == target_locale,
        )
    else:
        stmt = select(GlossaryEntry).where(
            GlossaryEntry.project_id == project_id,
            GlossaryEntry.glossary_id == None,  # noqa: E711
            GlossaryEntry.source_term == source_term,
            GlossaryEntry.source_locale == source_locale,
            GlossaryEntry.target_locale == target_locale,
        )
    existing = session.exec(stmt).first()
    if existing is None:
        existing = GlossaryEntry(
            project_id=project_id if glossary_id is None else None,
            glossary_id=glossary_id,
            source_locale=source_locale,
            target_locale=target_locale,
            source_term=source_term,
            target_term=(target_term or "").strip(),
            dnt=dnt,
        )
    else:
        existing.target_term = (target_term or "").strip()
        existing.dnt = dnt
    if meta is not None:
        cleaned = clean_meta(meta)
        if cleaned:
            existing.meta = {**(existing.meta or {}), **cleaned}
    session.add(existing)
    # Dual-write (LAW-TERM-2): the entry above is the authoritative write; mirror its
    # backing concept/term in the SAME transaction for a MODULE entry (legacy per-project
    # entries have no concept, LAW-TERM-2a — sync_entry_concept skips them). Flush first so
    # a brand-new entry has its id for the concept link.
    session.flush()
    # LAW-TERM-18 thin adapter (A.2-d): the entry above seeds the derived columns; mirror the
    # concept/source/target terms + senses, then make the concept store the AUTHORITY -- pin
    # the binding (target_term_id + meta_overrides by the MERGE writer's two-way rule) and
    # PROJECT the derived columns back from it. A legacy per-project entry has no concept
    # (sync_entry_concept skips it) and is byte-identical to before.
    _sync_concept(session, existing)
    if existing.glossary_id is not None:
        from app.services import glossary_concepts as _gc

        deriv = _gc._entry_deriv(session, existing)
        ov = _gc._overrides_two_way(existing.meta, deriv)
        _gc.apply_entry_binding(session, existing, meta_overrides=ov)
        # H-2: freeze exactly the shared keys this write moves on the concept's OTHER bindings
        # (LAW-TERM-1), never re-deriving an untouched row's whole override map.
        _gc.pin_shared_meta_on_siblings(session, existing)
        _gc.project(session, existing.glossary_id, entry_ids=[existing.id])
    session.commit()
    session.refresh(existing)
    return existing


def update_entry(
    session: Session,
    entry_id: int,
    target_term=None,
    dnt=None,
    meta=None,
    case_sensitive=None,
) -> GlossaryEntry | None:
    """Edit an EXISTING glossary entry in place (the expandable row editor / CLI
    edit-term / API PATCH). Only the passed fields change:

      target_term  None -> unchanged; a string (incl. "") -> set verbatim (trimmed).
      dnt          None -> unchanged; a bool -> set.
      meta         None -> unchanged; a dict -> REPLACES entry.meta with the cleaned,
                   allowlisted cells (an empty/all-blank dict clears meta to None), so
                   the row editor can both edit AND clear a metadata field.

    Returns the entry, or None if entry_id is unknown. Source term + locales are the
    upsert key and are NOT editable here (delete + re-add to change them). Meta never
    reaches a prompt — this only edits DATA about the term."""
    entry = session.get(GlossaryEntry, entry_id)
    if entry is None:
        return None
    old_meta = entry.meta or None  # the PROJECTED meta this edit's REPLACE semantics diff against
    old_ov = entry.meta_overrides  # kept as-is when meta is None (no override-state change)
    if target_term is not None:
        entry.target_term = (target_term or "").strip()
    if dnt is not None:
        entry.dnt = bool(dnt)
    if case_sensitive is not None:
        entry.case_sensitive = bool(case_sensitive)
    submitted = None
    if meta is not None:
        submitted = clean_meta(meta) or {}
        entry.meta = submitted or None  # so _sync_concept can populate concept/source term
    session.add(entry)
    session.flush()
    # LAW-TERM-18 thin adapter (A.2-d/A.3): mirror the concept + source term (set-when-non-blank),
    # then compute meta_overrides by update_entry's REPLACE-diff rule, pin the binding (resolve
    # target_term_id + retire a superseded target, A.2-f) and PROJECT the derived columns back.
    _sync_concept(session, entry)
    if entry.glossary_id is not None:
        from app.services import glossary_concepts as _gc

        deriv = _gc._entry_deriv(session, entry)
        # r7-4: a target-only / dnt-only edit (meta is None) changes NO override state.
        ov = _gc._overrides_diff_replace(submitted, old_meta, deriv) if meta is not None else old_ov
        _gc.apply_entry_binding(session, entry, meta_overrides=ov)
        # NOTE: the empty-target non-DNT binding (src->'') is a first-class row (A.2-a); the
        # legacy per-binding editor leaves it byte-identical to main. R7-M's DNT-off refusal is
        # a CONCEPT-EDITOR term-level guard (edit_concept_term), not this legacy seam.
        # H-2: freeze only the shared keys this write moves on the concept's OTHER bindings
        # (LAW-TERM-1); a target-only / dnt-only edit moves nothing, so propagation survives.
        _gc.pin_shared_meta_on_siblings(session, entry)
        _gc.project(session, entry.glossary_id, entry_ids=[entry.id])
    session.commit()
    session.refresh(entry)
    return entry


_BASE_OPTION = 0


_CUE_MIN_LEN = 3


# --- sense selection (QA BUG-01) ----------------------------------------------
# Option 0 of the candidate list is ALWAYS "no specific sense — use the entry's BASE
# target_term", never a sense. That ordering is the fix, not a cosmetic choice:
#
#   * every failure mode of a selector lands on index 0 (a model that answers lazily,
#     a truncated/garbled reply, a default in a parser). Before this change index 0
#     was the FIRST-LISTED SENSE, so "I could not decide" was indistinguishable from
#     "I chose the first sense" — and production picked the first sense in 6/6
#     checkable cases, including zero-cue segments ("Please check the configuration
#     file before you continue." demanded the BANKING sense `cheque`).
#   * the offline MockProvider scores cue-token overlap and breaks ties toward the
#     LOWEST index (providers._mock_choose_sense), so with zero cue evidence it now
#     lands on the base translation instead of an arbitrary sense — the mock becomes
#     correct by construction, with no change to providers.py.
#   * it makes the BASE TARGET A REAL CANDIDATE (chip task_17fa5f6b): a SINGLE-sense
#     entry can no longer force its narrow sense onto a segment that gives no cue for
#     it. Before, a one-sense entry was applied unconditionally.
#
# The other half of the fix is that "cannot run / cannot decide" no longer means
# senses[0]. Spend cap, provider exception, unparseable reply and an out-of-range
# index all fall back to _cue_fallback (deterministic, LLM-free) and then to the base.
# Cue tokens shorter than this are ignored by the deterministic fallback: a cue
# phrase like "use when talking about a river" must not match every segment through
# its "a"/"of". Real cues are content words.
def _cue_matches(cue: str, text: str) -> bool:
    """True when a sense's `context_cue` has evidence in `text`.

    CASE-INSENSITIVE and WORD-BOUNDARY-SANE (via _term_in_text), which the previous
    implicit mechanism was not: the whole selection ran through the provider, so an
    offline install's only cue matching was the mock's token-set overlap and a live
    install's was whatever the model felt like. A cue is read as a comma/semicolon/
    pipe/slash separated list of phrases; a multi-word phrase matches as a phrase AND
    (as a fallback) by any of its words, which is what the space-separated cue lists
    in the shipped glossaries ("money account deposit savings") mean in practice."""
    text = text or ""
    for phrase in re.split(r"[,;|/\n]+", cue or ""):
        phrase = phrase.strip()
        if not phrase:
            continue
        if len(phrase) >= _CUE_MIN_LEN and _term_in_text(phrase, text, False):
            return True
        for word in phrase.split():
            if len(word) >= _CUE_MIN_LEN and _term_in_text(word, text, False):
                return True
    return False


def _cue_fallback(segment_text: str, senses: list[GlossarySense]) -> GlossarySense | None:
    """The deterministic, LLM-free decision used when the selector could not run or
    could not decide: EXACTLY ONE sense with cue evidence wins; zero or several ->
    None (use the base target). Never "the first one"."""
    hits = [s for s in senses if _cue_matches(s.context_cue, segment_text)]
    return hits[0] if len(hits) == 1 else None


def page_entries(
    session: Session, *, project_id=None, glossary_id=None, cursor=None, limit=100
) -> dict:
    """One id-ordered keyset page in exactly one scope, with a batched sense fetch.

    Returns {"entries": [{"entry": GlossaryEntry, "senses": [GlossarySense]}],
    "has_more": bool, "next_cursor": int | None}. Two SELECTs, including empty pages.
    The cursor is the last returned id when another page exists, otherwise None.
    """
    cursor, limit = _page_bounds(cursor, limit)
    stmt = (
        _entry_statement(project_id, glossary_id)
        .where(GlossaryEntry.id > cursor)
        .order_by(GlossaryEntry.id)
    )
    return _entry_page(session, stmt, limit)


def window_entries(
    session: Session, *, project_id=None, glossary_id=None, cursor=None, limit=1000, offset=0
) -> dict:
    """SQL compatibility window for AU-7 callers; offset skips rows after cursor.

    Prefer page_entries for keyset traversal. OFFSET preserves the old contract
    but can scan skipped rows in the database; it does not load them into Python.
    """
    cursor, limit = _page_bounds(cursor, limit)
    stmt = (
        _entry_statement(project_id, glossary_id)
        .where(GlossaryEntry.id > cursor)
        .order_by(GlossaryEntry.id)
        .offset(min(2**63 - 1, max(0, int(offset or 0))))
    )
    return _entry_page(session, stmt, limit)


def iter_entries(session: Session, *, project_id=None, glossary_id=None, chunk=500):
    """Yield every entry with senses in bounded id-ordered pages for exporters."""
    cursor = None
    while True:
        page = page_entries(
            session, project_id=project_id, glossary_id=glossary_id, cursor=cursor, limit=chunk
        )
        yield from page["entries"]
        if not page["has_more"]:
            return
        cursor = page["next_cursor"]
