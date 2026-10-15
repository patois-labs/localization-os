"""glossary_concepts: shared leaf helpers (layered pure move)

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import (
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryTerm,
    TermAudience,
    TermStatus,
)
from app.services import glossary as gloss_engine

log = logging.getLogger("loc_tms.glossary_concepts")


SCHEMA_V1 = "glossary-concept/v1"


_CONCEPT_META_KEYS = ("definition", "subject_field", "notes")


_TERM_META_KEYS = ("part_of_speech", "usage_example")


# The five legacy meta keys and their concept/term homes (§4.4 derivation map). Kept in
# sync with glossary._GLOSSARY_META_COLS. definition/subject_field/notes live on the
# CONCEPT; part_of_speech/usage_example live on the source TERM.
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sanitize(val) -> str:
    """Flatten whitespace + cap a free-text metadata field at ingestion (§4.2 rule).

    Reuses the engine's single sanitizer so the store-level cap (500 chars, newline
    flattened) is identical to the legacy meta cap — a derived ``entry.meta`` can never
    then exceed ``_META_VALUE_CAP`` (LAW-TERM-1 CSV/grid parity)."""
    return gloss_engine._sanitize_meta_value(val)


# --- enum validation / canonicalization (service-level, not a DB CHECK — §4.3) --------


def canon_audience(value) -> str:
    """Canonical, sorted, comma-joined subset of {human,mt,llm}. Accepts a list, a CSV
    string, or "". An empty/garbage value coerces to the migration default (all three,
    Decision F). Unknown tokens are dropped."""
    if isinstance(value, (list, tuple, set)):
        toks = [str(t).strip().lower() for t in value]
    else:
        toks = [t.strip().lower() for t in str(value or "").split(",")]
    keep = sorted({t for t in toks if t in TermAudience.ALL}, key=TermAudience.ALL.index)
    return ",".join(keep) if keep else TermAudience.DEFAULT


def _valid_status(value) -> str:
    v = (value or "").strip() or TermStatus.PREFERRED
    return v if v in TermStatus.ALL else TermStatus.PREFERRED


def _valid_term_type(value) -> str:
    """A vocabulary value or a free custom string (stored verbatim, §4.3)."""
    return (value or "").strip()


class ConceptError(ValueError):
    """A concept/term validation or lookup failure surfaced to a caller."""


class ConceptWriteResult:
    """The result of a concept-editor TERM write that can touch bindings (A.3): the term
    plus the count of bindings whose per-pair fact changed (``pairs_updated``) and the ids of
    bindings a re-point emptied to DNT-only (``bindings_emptied``). Truthy like the term for
    callers that only need the row."""

    __slots__ = ("bindings_emptied", "pairs_updated", "term")

    def __init__(self, term, pairs_updated: int = 0, bindings_emptied: list[int] | None = None):
        self.term = term
        self.pairs_updated = int(pairs_updated)
        self.bindings_emptied = bindings_emptied or []

    @property
    def id(self):  # so a caller that reads result.id still works
        return getattr(self.term, "id", None)


class SetPairsResult(list):
    """``set_pairs``' return: the ``list[GlossaryPair]`` it already returned, carrying the
    A.4-8 widening counts (``bindings_created`` / ``dnt_bindings_extended``) as attributes so
    the four surfaces can report them without changing ``gm.to_dict``'s shape (R5-L1/R6-M2)."""

    bindings_created = 0
    dnt_bindings_extended = 0
    sibling_targets_skipped = 0


_CONCEPT_EDITOR_KEY = "terminology_concept_editor_enabled"


_CONCEPT_EDITOR_OFF_MSG = (
    "the concept editor is not enabled on this install; enable feature terminology_concept_editor"
)


# --- concept-editor feature gate (T1-lite: terminology_concept_editor) ----------------
# The concept/term-store WRITE seams (create/edit/delete concept, add/edit/delete term,
# set audience) are the dual-write reconciler surface whose round-7 gate found live
# regressions; this train ships them behind a DEFAULT-OFF feature flag
# (app/modules.py slug `terminology_concept_editor`) so the READ path + backfill /
# consolidation / JSON-import-into-EMPTY seed paths stay open while the two-way editor
# sync waits for T1b (authority inversion). Every write seam calls _require_concept_editor
# first, so the refusal reaches all four surfaces (they all delegate here): the web POST
# routes, the /api concept routes, the `glossary ... concepts/terms` CLI leaves, and the
# edit_glossary MCP actions (which post to the /api routes). Reads never call it.
def concept_editor_enabled() -> bool:
    """True when the concept-editor WRITE seams are enabled (feature
    terminology_concept_editor, DEFAULT OFF this release). STRICT `== "1"`, the four-
    delegated-services predicate, so a non-canonical value fails toward OFF; an
    unreadable Setting table (bare import contexts) also reads OFF (the write path must
    fail closed). Reads never consult this."""
    from app.services import settings

    try:
        return str(settings.get(_CONCEPT_EDITOR_KEY, "0")) == "1"
    except Exception:
        return False


def _require_concept_editor() -> None:
    """Refuse a concept-editor WRITE when the feature is off (naming the switch)."""
    if not concept_editor_enabled():
        raise ConceptError(_CONCEPT_EDITOR_OFF_MSG)


# --- entry -> concept upsert (the dual-write / backfill direction) --------------------


def _upsert_term(
    session: Session,
    concept_id: int,
    locale: str,
    term: str,
    *,
    status: str | None = None,
    dnt: bool | None = None,
    case_sensitive: bool | None = None,
    part_of_speech: str | None = None,
    context_example: str | None = None,
    audience: str | None = None,
    cache: dict | None = None,
) -> GlossaryTerm:
    """Idempotent upsert of one GlossaryTerm keyed by (concept_id, locale, term). `term`
    is stored VERBATIM (the authoritative matchable surface, §4.2 boundary); metadata
    fields are sanitized by the caller before they arrive here.

    NO-CLOBBER (gate round 1 M1 + round 2 R2-4): EVERY optional field the legacy dual-write
    path does not own — `status`, `audience`, `dnt`, `case_sensitive`, `part_of_speech`,
    `context_example` — is written ONLY when this call INSERTs the row, or when the caller
    passes it EXPLICITLY (non-None). On a plain update an omitted field is LEFT UNTOUCHED,
    so a concept-editor-set value survives a later legacy-grid edit / CSV re-import of the
    same term. The SOURCE-term dual-write passes `dnt`/`case_sensitive`/`part_of_speech`/
    `context_example` explicitly (they mirror the authoritative entry + its meta, M13); the
    TARGET-term call passes none of them, so an editor-set target `part_of_speech` /
    `context_example` / `case_sensitive` is not wiped (R2-4)."""
    key = (concept_id, locale, term)
    existing = None
    if cache is not None and key in cache:
        existing = session.get(GlossaryTerm, cache[key])
    if existing is None:
        existing = session.exec(
            select(GlossaryTerm).where(
                GlossaryTerm.concept_id == concept_id,
                GlossaryTerm.locale == locale,
                GlossaryTerm.term == term,
            )
        ).first()
    inserting = existing is None
    if inserting:
        existing = GlossaryTerm(concept_id=concept_id, locale=locale, term=term)
        session.add(existing)
        existing.status = status or TermStatus.PREFERRED
        existing.audience = canon_audience(
            audience if audience is not None else TermAudience.DEFAULT
        )
        existing.dnt = bool(dnt)
        existing.case_sensitive = bool(case_sensitive)
        existing.part_of_speech = part_of_speech or ""
        existing.context_example = context_example or ""
    else:
        # update ONLY the fields the caller explicitly passed (never clobber the rest).
        if status is not None:
            existing.status = status
        if audience is not None:
            existing.audience = canon_audience(audience)
        if dnt is not None:
            existing.dnt = bool(dnt)
        if case_sensitive is not None:
            existing.case_sensitive = bool(case_sensitive)
        if part_of_speech is not None:
            existing.part_of_speech = part_of_speech
        if context_example is not None:
            existing.context_example = context_example
    existing.updated_at = _utcnow()
    session.flush()
    if cache is not None:
        cache[key] = existing.id
    return existing


def _find_concept_for_source(
    session: Session,
    glossary_id: int,
    source_locale: str,
    source_term: str,
    *,
    cache: dict | None = None,
) -> GlossaryConcept | None:
    """R4 root fix: the concept identity is (glossary_id, source_locale, source_term) —
    case-folded per the compat matching rules — so all target-locale rows of ONE source
    surface become terms of ONE concept, NOT one concept per (source, target) row. Finds
    that concept by looking for another BASE compat entry of the same source surface that is
    already linked (siblings link via sense.concept_id, so this only finds the base).

    R5-10: Python ``str.casefold()`` is the AUTHORITATIVE fold for the concept identity key —
    on the cache key, on the confirmation below, and in ``consolidate_split_concepts`` and the
    migration's ``_backfill_core``. The DB fallback narrows candidates with a SQL ``func.lower()``
    PRE-FILTER (bounded memory — it returns at most the handful of rows sharing the surface, not
    the whole locale; the O(n) `.all()` over every entry it replaced blew the bulk-import memory
    cap), then CONFIRMS each candidate with ``casefold()`` in Python. For ASCII surfaces
    ``lower() == casefold()`` so this is exact; SQLite's ``lower()`` is ASCII-only and cannot
    casefold a non-ASCII surface (German ``ß``, Turkish dotted/dotless I), but the in-run cache
    (Python casefold, above) covers the hot path and the DB fallback is only a cold-path net."""
    want = (source_term or "").casefold()
    key = (source_locale or "", want)
    if cache is not None and key in cache and cache[key] is not None:
        return session.get(GlossaryConcept, cache[key])
    for cid, sterm in session.exec(
        select(GlossaryEntry.concept_id, GlossaryEntry.source_term).where(
            GlossaryEntry.glossary_id == glossary_id,
            GlossaryEntry.concept_id.is_not(None),
            GlossaryEntry.source_locale == (source_locale or ""),
            func.lower(GlossaryEntry.source_term) == (source_term or "").lower(),
        )
    ).all():
        if (sterm or "").casefold() == want:
            if cache is not None:
                cache[key] = cid
            return session.get(GlossaryConcept, cid)
    return None


def _homonym_group(glossary_id: int, source_term: str) -> str:
    """The homonym-group key (§4.6-4, M6 residue): pair-FREE — a single concept may span
    multiple locale pairs (§4.4-1 M5 fix), so the key is glossary + casefolded source
    surface only. Concepts sharing a source term across pairs are ONE group, which is the
    intended behavior."""
    return f"g{glossary_id}:{(source_term or '').strip().casefold()}"


# --- concept -> entry projection (the append/update-only reconciler) ------------------


def _lang_base_match(a: str, b: str) -> bool:
    """Base-language equality (``es`` matches ``es-ES``), MIRRORS glossary_tbx._lang_match /
    tm._lang_match. Kept local so the concept service does not pull in the format or TM
    layer. Returns False for a blank locale on either side (the wildcard "" is handled as an
    EXACT co-match by the projectors, not as a base fallback)."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.split("-")[0] == b.split("-")[0]


def _pick_target_term(
    concept_terms: list[GlossaryTerm],
    target_locale: str,
    *,
    exclude_id: int | None = None,
    source_locale: str | None = None,
) -> GlossaryTerm | None:
    """The concept's USABLE (preferred>admitted) target-side term for a locale (§4.4-1
    ladder). Wildcard-locale ("") terms co-match. deprecated/forbidden never win.
    `exclude_id` drops the SOURCE term so a DNT-only concept (whose only term is the
    wildcard-locale source) does not pick its own source as the target.

    Round-1 H2: when NO exact/wildcard term serves the locale, fall back to a base-language
    match (a ``es`` term serves an ``es-ES`` pair) -- the same base fallback the entry path
    (glossary_tbx.parse_rows) already applies, so a Microsoft-shape termbase
    (``xml:lang="es"`` terms into an ``es-ES`` pair) projects instead of silently enforcing
    nothing. The fallback is STRICT: an exact/wildcard term always wins over a base-only one.

    Round-2 M1 (two bounds on the base fallback, both mirroring the entry path's
    parse_rows contract, and applied to the FALLBACK TIER ONLY so an exact match is
    unchanged from ``main``):
      * SAME-SECTION guard -- when ``source_locale`` is given, a base-fallback target
        that resolves to the SAME base language as the source is REJECTED, so an
        ``en-US`` source can never bind an ``en-US``/``en-GB`` sibling as its own
        ``en-GB`` target (the S3 ``color -> colour`` phantom). ``parse_rows`` skips
        exactly this "two sides resolve to the same section" case.
      * GENERIC-BASE preference -- among base-fallback candidates, the generic base tag
        (``es`` for an ``es-ES`` pair) is preferred over a differently-regioned sibling
        (``es-419``), so the choice is deterministic and not document-order (S4).

    Round-3 M2 (TOTAL order on the fallback tier + arm parity): the tie-break is now a full
    order independent of document order -- the untagged/base-language tag first, then the
    lexicographically-first locale tag -- so when NO candidate carries the bare base tag the
    choice is still deterministic (round 2's two-value key kept document order in that case).
    ``glossary_tbx._pick_key`` (the DEFAULT/flat entry arm) uses the SAME order, so the same
    bytes resolve the same regional target through both arms (within one status tier)."""
    usable = [t for t in concept_terms if t.status in TermStatus.USABLE and t.id != exclude_id]

    def _exact(t: GlossaryTerm) -> bool:
        return (t.locale or "") == (target_locale or "") or (t.locale or "") == ""

    # exact/wildcard tier FIRST -- byte-identical to main (an exact/wildcard term always
    # wins over a base-only one).
    exact_tier = [t for t in usable if _exact(t)]
    for want in TermStatus.USABLE:
        for t in exact_tier:
            if t.status == want:
                return t
    # base-language fallback tier (round-1 H2) with M1's same-section guard + generic-base
    # preference.
    tgt_base = (target_locale or "").lower().split("-")[0]
    src_base = (source_locale or "").lower().split("-")[0] if source_locale else None
    fallback = [
        t
        for t in usable
        if _lang_base_match(t.locale or "", target_locale or "")
        and not (src_base and (t.locale or "").lower().split("-")[0] == src_base)
    ]
    # round-3 M2: a TOTAL order independent of document order -- the generic base tag
    # (locale == the pair's base language) first, then the lexicographically-first locale tag.
    # Round 2's two-value key was stable but kept document order when NO candidate carried the
    # bare base tag; this second key removes that residual non-determinism.
    fallback.sort(
        key=lambda t: (0 if (t.locale or "").lower() == tgt_base else 1, (t.locale or "").lower())
    )
    for want in TermStatus.USABLE:
        for t in fallback:
            if t.status == want:
                return t
    return None


def _derive_meta(concept: GlossaryConcept, src_term: GlossaryTerm) -> dict | None:
    """Reproduce the five-key legacy ``entry.meta`` from the concept + source term
    (§4.4). Only non-blank keys are emitted (mirrors clean_meta), so a concept/term with
    no metadata derives ``None`` — byte-identical to a legacy entry's NULL meta."""
    out: dict = {}
    if concept.definition:
        out["definition"] = concept.definition
    if concept.domain:
        out["subject_field"] = concept.domain
    if concept.notes:
        out["notes"] = concept.notes
    if src_term.part_of_speech:
        out["part_of_speech"] = src_term.part_of_speech
    if src_term.context_example:
        out["usage_example"] = src_term.context_example
    return out or None


_META_ORDER = ("part_of_speech", "definition", "usage_example", "subject_field", "notes")


# --- LAW-TERM-18 per-pair meta authority (meta_overrides, §A.3) ------------------------
# The five legacy meta keys, in clean_meta / _GLOSSARY_META_COLS order, so a projected
# ``entry.meta`` iterates the keys the same way every legacy writer stored them.
def _project_meta(overrides: dict | None, deriv: dict | None) -> dict | None:
    """Project ``entry.meta`` from the binding's ``meta_overrides`` over the derivation
    (§A.3, three-valued per key): an OVERRIDE (string) wins; a TOMBSTONE (JSON ``null``)
    drops the key even when the concept/source term derives one; an ABSENT key derives from
    the concept/source term. ``overrides`` NULL means no authority is recorded yet (a
    not-yet-backfilled row) -> the pure derivation, which ``backfill_pair_bindings`` then
    pins. Emits keys in clean_meta order; ``None`` when empty (== a legacy NULL meta)."""
    deriv = deriv or {}
    if overrides is None:
        return dict(deriv) or None
    out: dict = {}
    for k in _META_ORDER:
        if k in overrides:
            v = overrides[k]
            if v is None:  # tombstone -> the pair shows no value though the concept has one
                continue
            out[k] = v
        else:
            dv = deriv.get(k)
            if dv:
                out[k] = dv
    return out or None


def _overrides_two_way(meta: dict | None, deriv: dict | None) -> dict | None:
    """Compute ``meta_overrides`` from a stored/legacy meta dict by §A.3's TWO-WAY rule
    (the legacy MERGE writer + the A.5 backfill share this): for each key, a NON-EMPTY
    value -> OVERRIDE (the string); a missing/empty key whose DERIVATION is non-empty ->
    TOMBSTONE (JSON ``null``); missing/empty while the derivation is empty -> ABSENT.

    This is the inverse the projector agrees with BY CONSTRUCTION:
    ``_project_meta(_overrides_two_way(m, d), d) == (m or None)`` for every m, d (the
    LAW-TERM-1 property the write-parity golden + projection-equivalence assert). Returns
    ``None`` when no key needs recording (nothing overridden, nothing tombstoned)."""
    meta = meta or {}
    deriv = deriv or {}
    out: dict = {}
    for k in _META_ORDER:
        mv = meta.get(k)
        if mv:
            out[k] = mv
        elif deriv.get(k):
            out[k] = None  # tombstone: the derivation would surface a value the pair lacks
    return out or None


def _entry_deriv(session: Session, entry: GlossaryEntry) -> dict | None:
    """The five-key derivation for ONE entry (its concept + source term), used by the
    adapters to compute ``meta_overrides`` and by the projector to project ``entry.meta``."""
    concept = session.get(GlossaryConcept, entry.concept_id) if entry.concept_id else None
    src = session.get(GlossaryTerm, entry.term_id) if entry.term_id else None
    if concept is None or src is None:
        return None
    return _derive_meta(concept, src)


def _closure_entry_ids(
    session: Session,
    glossary_id: int,
    entry_ids: list[int] | None,
    concept_ids: list[int] | None,
) -> tuple[set[int], set[str]]:
    """Resolve the projection scope closed over the HOMONYM GROUP (§A.1). Returns the set of
    ENTRY ids to project plus the set of non-blank homonym groups touched (so the sense pass
    knows which groups to derive fresh siblings for). The closure is an ID-ONLY query guarded
    by ``homonym_group != ''`` -- a BLANK group is not a group and contributes nothing
    (R5-M1). A touched sibling pulls in its base entries; a touched base pulls in its sibling
    senses.

    ``entry_ids``/``concept_ids`` are the touched bindings/concepts. An empty list for EITHER
    is a ``ValueError`` (never a silent no-op at the API, §A.1); ``None`` for both means the
    whole glossary (the caller streams it, this is not reached)."""
    if entry_ids is not None and len(entry_ids) == 0:
        raise ValueError(
            "project: entry_ids=[] (a caller that touched nothing must not call project)"
        )
    if concept_ids is not None and len(concept_ids) == 0:
        raise ValueError(
            "project: concept_ids=[] (a caller that touched nothing must not call project)"
        )
    seed_entries: set[int] = set(entry_ids or [])
    seed_concepts: set[int] = set(concept_ids or [])
    # the concepts of the seed entries (so a touched base pulls in its own group).
    if seed_entries:
        for cid in session.exec(
            select(GlossaryEntry.concept_id).where(
                GlossaryEntry.id.in_(list(seed_entries)),
                GlossaryEntry.concept_id.is_not(None),
            )
        ).all():
            seed_concepts.add(cid)
    # the non-blank homonym groups of every seed concept.
    groups: set[str] = set()
    if seed_concepts:
        for hg in session.exec(
            select(GlossaryConcept.homonym_group).where(
                GlossaryConcept.id.in_(list(seed_concepts)),
                GlossaryConcept.homonym_group != "",
            )
        ).all():
            if hg:
                groups.add(hg)
    # every concept in a touched group (base + siblings), guarded by homonym_group != ''.
    group_concepts: set[int] = set()
    if groups:
        for cid in session.exec(
            select(GlossaryConcept.id).where(
                GlossaryConcept.glossary_id == glossary_id,
                GlossaryConcept.homonym_group.in_(list(groups)),
            )
        ).all():
            group_concepts.add(cid)
    all_concepts = seed_concepts | group_concepts
    # every base entry of every concept in scope + the seed entries themselves.
    entries: set[int] = set(seed_entries)
    if all_concepts:
        for eid in session.exec(
            select(GlossaryEntry.id).where(
                GlossaryEntry.glossary_id == glossary_id,
                GlossaryEntry.concept_id.in_(list(all_concepts)),
            )
        ).all():
            entries.add(eid)
    return entries, groups


def _source_term_of(
    terms: list[GlossaryTerm], source_locale: str, source_term: str
) -> GlossaryTerm | None:
    """The concept's SOURCE term for a projected entry (so the sense derivation can exclude it
    from _pick_target_term). Matches the entry's source surface + locale exactly, then by
    surface alone (a base-fallback source keeps the pair locale)."""
    for t in terms:
        if (t.locale or "") == (source_locale or "") and t.term == source_term:
            return t
    for t in terms:
        if t.term == source_term:
            return t
    return None


_PAIR_BINDINGS_MARKER = "c4d8e2a1f7b6"


# the T1b revision that shipped the backfill (versioned)
def _pair_binding_columns_present(session: Session) -> bool:
    """True when ``glossaryentry`` actually carries ``target_term_id``/``meta_overrides``
    (a Postgres ``init_db`` may call the boot backfill at any alembic revision, R2-L4)."""
    try:
        from sqlalchemy import inspect as _sa_inspect

        cols = {c["name"] for c in _sa_inspect(session.get_bind()).get_columns("glossaryentry")}
        return "target_term_id" in cols and "meta_overrides" in cols
    except Exception:
        return False


def _retire_target_term(session: Session, old_term_id: int | None) -> None:
    """A.2-f target retirement (replaces ``retire_superseded_target``): after a binding's
    ``target_term_id`` is re-pointed (incl. blanked) AND FLUSHED, DELETE the previous target
    term iff the OBSERVABLE CONDITION holds -- no binding's ``target_term_id``, no sense's
    ``term_id``, and NO binding's ``term_id`` references it (the LT2-1 live-source-surface
    carve-out subsumes the self-binding). No status gating (LT2-2 case-only change retires).
    Idempotent; a no-op when nothing was re-pointed."""
    if old_term_id is None:
        return
    if (
        session.exec(
            select(GlossaryEntry.id).where(GlossaryEntry.target_term_id == old_term_id).limit(1)
        ).first()
        is not None
    ):
        return
    if (
        session.exec(
            select(GlossaryEntry.id).where(GlossaryEntry.term_id == old_term_id).limit(1)
        ).first()
        is not None
    ):
        return
    if (
        session.exec(
            select(GlossarySense.id).where(GlossarySense.term_id == old_term_id).limit(1)
        ).first()
        is not None
    ):
        return
    old_term = session.get(GlossaryTerm, old_term_id)
    if old_term is None:
        return
    _clear_inbound_replacements(session, [old_term_id])
    session.delete(old_term)
    session.flush()


def pin_shared_meta_on_siblings(session: Session, entry: GlossaryEntry) -> int:
    """H-2 (A.3 always-pin, LAW-TERM-1): a LEGACY adapter write to ``entry`` may set or change
    a SHARED value on the concept (``definition``/``domain``/``notes``) or its source term
    (``part_of_speech``/``usage_example``) that every OTHER binding of the SAME concept derives
    from. Under LAW-TERM-1 a legacy write to one pair must never change another pair's visible
    meta, so each OTHER binding whose projection WOULD move because of this write is frozen to
    its PRE-WRITE projected value -- per key: an OVERRIDE when it was showing one, a TOMBSTONE
    when it was showing nothing. Keys that do NOT move are left DERIVING, so a later
    concept-editor edit of an un-authored key still propagates (R7-D).

    This REPLACES the ``repin_siblings`` flag A.3 rejected (which re-derived an untouched row's
    WHOLE override map on every projection, freezing concept-editor values and killing
    propagation). Only the concept's own other bindings are touched; the touched entry is pinned
    by the adapter itself. Call AFTER ``sync_entry_concept`` (so the derivation reflects the
    write) and BEFORE the touched entry's :func:`project`. Returns the number of siblings pinned.
    """
    if entry.concept_id is None or entry.glossary_id is None:
        return 0
    others = session.exec(
        select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == entry.glossary_id,
            GlossaryEntry.concept_id == entry.concept_id,
            GlossaryEntry.id != entry.id,
        )
    ).all()
    if not others:
        return 0
    concept = session.get(GlossaryConcept, entry.concept_id)
    pinned = 0
    for e2 in others:
        old_proj = e2.meta or {}
        src2 = session.get(GlossaryTerm, e2.term_id) if e2.term_id else None
        new_deriv = (
            _derive_meta(concept, src2) if (concept is not None and src2 is not None) else None
        )
        new_proj = _project_meta(e2.meta_overrides, new_deriv) or {}
        ov = dict(e2.meta_overrides or {})
        changed = False
        for k in _META_ORDER:
            if new_proj.get(k) == old_proj.get(k):
                continue  # this key does not move -> keep its derive/override state
            if k in old_proj:
                ov[k] = old_proj[k]  # was showing a value -> freeze it as an override
            else:
                ov[k] = None  # was showing nothing -> tombstone so it keeps showing nothing
            changed = True
        if changed:
            e2.meta_overrides = ov or None
            session.add(e2)
            pinned += 1
    if pinned:
        session.flush()
    return pinned


def _usable_source_terms(cterms: list[GlossaryTerm], source_locale: str) -> list[GlossaryTerm]:
    """ALL usable (preferred/admitted) source-locale terms of a concept — its SYNONYMS
    (§4.4-2), preferred first then admitted, so each projects its own compat entry.

    Round-1 H2: like :func:`_pick_target_term`, if NO exact/wildcard source term serves the
    locale, fall back to base-language matches (``en`` serves an ``en-US`` pair) so a
    Microsoft-shape termbase projects; an exact/wildcard match always wins over a base-only
    one, so a glossary with both ``en-US`` and ``en`` terms is unaffected."""
    usable = [t for t in cterms if t.status in TermStatus.USABLE]

    def _exact(t: GlossaryTerm) -> bool:
        return (t.locale or "") == (source_locale or "") or (t.locale or "") == ""

    tier = [t for t in usable if _exact(t)]
    if not tier:
        tier = [t for t in usable if _lang_base_match(t.locale or "", source_locale or "")]
    out: list[GlossaryTerm] = []
    for want in TermStatus.USABLE:
        out.extend(t for t in tier if t.status == want)
    return out


# --- delete units (LAW-TERM-2b) -------------------------------------------------------


def _clear_inbound_replacements(session: Session, term_ids: list[int]) -> None:
    """Clear any ``deprecated_replacement_term_id`` (from ANY glossary — the self-FK is
    cross-glossary-legal, M11) pointing at a doomed term, so the final DELETE cannot raise
    IntegrityError on Postgres."""
    if not term_ids:
        return
    for t in session.exec(
        select(GlossaryTerm).where(GlossaryTerm.deprecated_replacement_term_id.in_(term_ids))
    ).all():
        t.deprecated_replacement_term_id = None
        session.add(t)
    session.flush()


def _term_referenced_elsewhere(
    session: Session, term_id: int, *, exclude_entry_id: int | None = None
) -> bool:
    """True when any surviving entry (via term_id OR the LAW-TERM-18 target_term_id) or sense
    (via term_id) still references this GlossaryTerm — the H1 shared-source-term / shared-target
    guard, now also honouring the enforced-target FK so a delete unit never drops a term another
    binding still enforces."""
    q_e = select(GlossaryEntry.id).where(GlossaryEntry.term_id == term_id)
    if exclude_entry_id is not None:
        q_e = q_e.where(GlossaryEntry.id != exclude_entry_id)
    if session.exec(q_e.limit(1)).first() is not None:
        return True
    q_tt = select(GlossaryEntry.id).where(GlossaryEntry.target_term_id == term_id)
    if exclude_entry_id is not None:
        q_tt = q_tt.where(GlossaryEntry.id != exclude_entry_id)
    if session.exec(q_tt.limit(1)).first() is not None:
        return True
    q_s = select(GlossarySense.id).where(GlossarySense.term_id == term_id).limit(1)
    return session.exec(q_s).first() is not None


def _drop_concept_terms(session: Session, concept: GlossaryConcept) -> None:
    term_ids = list(
        session.exec(select(GlossaryTerm.id).where(GlossaryTerm.concept_id == concept.id)).all()
    )
    _clear_inbound_replacements(session, term_ids)
    for t in session.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id == concept.id)).all():
        session.delete(t)
    session.flush()


def delete_glossary_concepts(session: Session, glossary_id: int) -> None:
    """The whole-GLOSSARY concept delete UNIT (LAW-TERM-2b): drop every concept/term of the
    glossary, child-before-parent, clearing inbound ``deprecated_replacement_term_id`` refs
    FROM ANY glossary first (M11). Called inside glossary_module.delete's transaction."""
    concepts = session.exec(
        select(GlossaryConcept).where(GlossaryConcept.glossary_id == glossary_id)
    ).all()
    if not concepts:
        return
    term_ids = list(
        session.exec(
            select(GlossaryTerm.id).where(GlossaryTerm.concept_id.in_([c.id for c in concepts]))
        ).all()
    )
    _clear_inbound_replacements(session, term_ids)
    for t in session.exec(
        select(GlossaryTerm).where(GlossaryTerm.concept_id.in_([c.id for c in concepts]))
    ).all():
        session.delete(t)
    session.flush()
    for c in concepts:
        session.delete(c)
    session.flush()


# --- glossary-concept/v1 JSON codec (D2) ----------------------------------------------


def _doc_concept_id(
    concept: GlossaryConcept, used: set[str], reserved: frozenset[str] = frozenset()
) -> str:
    """The document identity for a concept (§5.1/M6): the stored external_id when it is
    non-blank AND unique in the document, else the synthesized ``c<id>``. Every in-document
    reference (cross_refs / deprecated_replacement.concept) uses this same minted id, so a
    backfilled concept (external_id "") normalizes to ``c<id>`` and round-trips stably."""
    ext = (concept.external_id or "").strip()
    if ext and ext not in used:
        used.add(ext)
        return ext
    minted = f"c{concept.id}"
    # A minted id must not equal a stored external id (an imported `c7`) of ANOTHER
    # concept, or two concepts share one document identity (#510).
    while minted in used or minted in reserved:
        minted += "_"
    used.add(minted)
    return minted


def _term_to_v1(session: Session, t: GlossaryTerm, id_for_concept: dict[int, str]) -> dict:
    out = {
        "locale": t.locale,
        "term": t.term,
        "status": t.status,
        "term_type": t.term_type,
        "part_of_speech": t.part_of_speech,
        "grammatical_gender": t.grammatical_gender,
        "grammatical_number": t.grammatical_number,
        "geographical_usage": t.geographical_usage,
        "audience": (t.audience or TermAudience.DEFAULT).split(","),
        "usage_note": t.usage_note,
        "context_example": t.context_example,
        "case_sensitive": t.case_sensitive,
        "exact": t.exact,
        "dnt": t.dnt,
        "source": t.source,
    }
    if t.deprecated_replacement_term_id:
        repl = session.get(GlossaryTerm, t.deprecated_replacement_term_id)
        if repl is not None and repl.concept_id in id_for_concept:
            out["deprecated_replacement"] = {
                "concept": id_for_concept[repl.concept_id],
                "locale": repl.locale,
                "surface": repl.term,
            }
    return out


def _concepts_share_a_term(session: Session, concept: GlossaryConcept, cdoc: dict) -> bool:
    """R3-3: True when the STORED concept shares at least one (locale, surface) term with the
    INCOMING document concept — i.e. the external_id match is a real upsert and not a
    cross-install id collision. An empty stored concept is safe to reuse."""
    stored = {
        (t.locale or "", t.term)
        for t in session.exec(
            select(GlossaryTerm).where(GlossaryTerm.concept_id == concept.id)
        ).all()
    }
    if not stored:
        return True
    incoming = {
        ((t.get("locale") or ""), t.get("term")) for t in (cdoc.get("terms") or []) if t.get("term")
    }
    return bool(stored & incoming)


def _replacement_surface(session: Session, term: GlossaryTerm) -> str:
    if term.deprecated_replacement_term_id:
        r = session.get(GlossaryTerm, term.deprecated_replacement_term_id)
        if r is not None:
            return r.term
    return ""


# --- audience filter + renderer (T1-built, T3-wired) ----------------------------------


def filter_by_audience(matches: list[dict], channel: str) -> list[dict]:
    """Keep a match iff ``channel`` ∈ the audience of its SELECTED target (LAW-TERM-4,
    applied LAST). A DNT-only concept and a legacy match (no ``_concept``) are ALWAYS kept
    (source-side / no audience data — never silently stripped). This is a pure function;
    T3 wires it at the selection layer feeding all prompt seams (residue [M2], T3)."""
    out: list[dict] = []
    for m in matches:
        meta = m.get("_concept")
        if meta is None or m.get("dnt"):
            out.append(m)
            continue
        # M2: filter on the SOURCE term's audience (the single owner) — the one CSV/export/
        # the editor write.
        aud = (meta.get("audience") or TermAudience.DEFAULT).split(",")
        if channel in aud:
            out.append(m)
    return out


def _render_ordering_key(m: dict):
    """DNT/forbidden-bearing first, then longest source term, then stable (§5.3)."""
    meta = m.get("_concept") or {}
    has_neg = bool(m.get("dnt")) or bool(meta.get("alternatives"))
    return (0 if has_neg else 1, -len(m.get("source_term") or ""))


def _flatten_surface(val: str) -> str:
    """Render-only neutralization of newlines/control chars in a term surface (§5.3/M4).
    The stored value is untouched; only the printed surface is flattened."""
    return " ".join(str(val or "").split())


def _est_tokens(text: str) -> int:
    from app.services.segment import est_tokens

    return est_tokens(text)


def concepts_for_editor(session: Session, glossary_id: int) -> list[dict]:
    """A web-editor view of the concept/term store carrying the DB ids the edit/delete
    forms POST (the wire ``glossary-concept/v1`` deliberately omits internal ids)."""
    concepts = session.exec(
        select(GlossaryConcept)
        .where(GlossaryConcept.glossary_id == glossary_id)
        .order_by(GlossaryConcept.id)
    ).all()
    # M6 (gate round-1): ONE bulk term query grouped in Python (chunked for the SQLite
    # parameter cap), instead of a SELECT per concept on every detail-page render.
    cids = [c.id for c in concepts]
    terms_by_concept: dict[int, list[GlossaryTerm]] = {}
    for i in range(0, len(cids), 400):
        for t in session.exec(
            select(GlossaryTerm)
            .where(GlossaryTerm.concept_id.in_(cids[i : i + 400]))
            .order_by(GlossaryTerm.id)
        ).all():
            terms_by_concept.setdefault(t.concept_id, []).append(t)
    out = []
    for c in concepts:
        terms = terms_by_concept.get(c.id, [])
        out.append(
            {
                "id": c.id,
                "external_id": c.external_id,
                "definition": c.definition,
                "domain": c.domain,
                "notes": c.notes,
                "homonym_group": c.homonym_group,
                "terms": [
                    {
                        "id": t.id,
                        "locale": t.locale,
                        "term": t.term,
                        "status": t.status,
                        "term_type": t.term_type,
                        "audience": (t.audience or TermAudience.DEFAULT).split(","),
                        "dnt": t.dnt,
                        # L-3: the per-term web edit form reflects the case-sensitivity fact.
                        "case_sensitive": t.case_sensitive,
                    }
                    for t in terms
                ],
            }
        )
    return out


def _source_locales(session: Session, glossary_id: int) -> set[str]:
    """The SOURCE locales of the glossary's configured pairs (the locales a compat entry
    keys its source term on)."""
    return {
        p.source_locale
        for p in session.exec(
            select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
        ).all()
    }


def _configured_pairs(session: Session, glossary_id: int) -> list[tuple[str, str]]:
    return [
        ((p.source_locale or ""), (p.target_locale or ""))
        for p in session.exec(
            select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
        ).all()
    ]


def _group_base_id(session: Session, glossary_id: int, concept: GlossaryConcept) -> int:
    """The homonym group's BASE concept id (lowest id member, §4.5) — the concept whose
    bindings are ENTRIES; every other member is a sibling projected as a sense. A blank
    ``homonym_group`` is not a group (R5-M1): the concept is its own base."""
    hg = concept.homonym_group or ""
    if not hg:
        return concept.id
    ids = session.exec(
        select(GlossaryConcept.id).where(
            GlossaryConcept.glossary_id == glossary_id,
            GlossaryConcept.homonym_group == hg,
        )
    ).all()
    return min(ids) if ids else concept.id


def _touch_draft(session: Session, glossary_id: int) -> None:
    from app.services import glossary_module

    glossary_module.touch_draft(session, glossary_id)


def dumps_v1(doc: dict) -> bytes:
    """Serialize a v1 document to canonical UTF-8 JSON bytes (the .glossary.json file)."""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def loads_v1(data: bytes) -> dict:
    return json.loads(data.decode("utf-8-sig"))
