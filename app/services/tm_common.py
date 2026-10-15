"""tm: shared leaf helpers (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import hashlib

import sqlalchemy as sa
from sqlalchemy import func
from sqlmodel import Session, select

from app.models import FullTextTM, SegmentTM


def _content_type_name(session, content_type_id):
    """Cheap (identity-map-cached) content-type NAME for provenance, or None.

    Uses session.get so a repeat resolve within one write batch is free. None when
    there is no content type to resolve (untyped project / no session / missing)."""
    if session is None or content_type_id is None:
        return None
    from app.models import ContentType

    ct = session.get(ContentType, content_type_id)
    return getattr(ct, "name", None) if ct is not None else None


def _project_content_type_id(session, project_id):
    """The content_type_id of `project_id`, or None (SPEC-content-types phase 3).

    None whenever there is no project scope to resolve (project_id None, no session,
    or the project row is missing) OR the project is UNTYPED — both collapse to "the
    caller is in the shared General bucket", which the read scope renders as
    `content_type_id IS NULL` only. Uses session.get (identity-map cached), so the
    per-lookup resolution is cheap and repeated within a batch."""
    if session is None or project_id is None:
        return None
    from app.models import Project

    proj = session.get(Project, project_id)
    return getattr(proj, "content_type_id", None) if proj is not None else None


def _tm_source_project_ids(session, project_id):
    """The ATTACHED TM source projects `project_id` may read from RIGHT NOW, or ().

    Thin delegation to project_sources.allowed_tm_source_ids so this module keeps one
    import-cheap entry point; that function is where the security argument lives. The
    short version: a ProjectTmSource row is a HINT, and the ids it names are re-derived
    against the LIVE Project rows through access.cross_project_scope_allows — the same
    predicate the attach write enforces — on EVERY lookup. A stale link (the source
    moved customer/org afterwards) or a hostile one (planted straight into the table)
    therefore contributes ZERO ids, so no SQL scope built below can leak through it.

    Returns () whenever there is nothing to resolve, which is the overwhelmingly common
    case and the one that keeps _tm_scope byte-identical to its pre-feature form. Never
    raises into a lookup: a link-resolution failure degrades to "no attached sources"
    (the safe direction — fewer matches, never more)."""
    if session is None or project_id is None:
        return ()
    try:
        from app.services import project_sources

        return project_sources.allowed_tm_source_ids(session, project_id)
    except Exception:
        return ()


def _tm_scope(col, project_id, source_ids=(), restrict_ids=None):
    """Project-scope predicate for a TM/embedding lookup (leak vector 9).

    Batch 15 T1 (SPEC-tm-match-step-config §7, LAW-TM-9): `restrict_ids`, when NOT None,
    NARROWS the readable project-leg to the intersection of the live readable set
    ({project_id} + validated source_ids) with `restrict_ids` -- a per-step tm_match TM
    pool selection. It can ONLY narrow (the intersection never adds a pool the run could
    not already read); a selected id outside the live readable set is silently dropped
    here (the orchestrator surfaces it as a run note). A non-None selection that resolves
    to the EMPTY set matches NOTHING (`sa.false()`, fail-closed) -- preserving the
    NULL-vs-[] distinction (NULL = "all readable" is `restrict_ids=None`, unchanged
    below). The NULL/global leg is inert in SQL anyway (a NULL project_id never matches),
    so narrowing the concrete id set is the whole mechanism.

    Normally a lookup matches THIS project's rows OR the NULL-project GLOBAL rows
    (`col.in_((project_id, None))`). In MULTI-ORG mode (the sacred gate ON) that
    NULL-project leg is DROPPED — the lookup is confined to the given project's own
    rows (`col == project_id`) — so a NULL-project (org-less) global TM row can
    never suggest into a scoped org's document (leak vector 9). Gate OFF:
    byte-identical to today (the `IN (pid, NULL)` form is kept).

    ATTACHED TM SOURCES (feedback item 5) widen the OWN-PROJECT leg — and ONLY that
    leg — by adding the already-validated `source_ids` to it. Two properties matter:

      * `source_ids` is EMPTY unless the project has links that pass the live scope
        predicate, and an empty tuple leaves both branches character-for-character what
        they were, so an install that attaches nothing is byte-identical;
      * the widening rides INSIDE the multi-org branch too (`col.in_((pid, *srcs))`
        rather than `col == pid`), which is the point: the ids were already filtered to
        same-org, same-customer projects by the caller, so under the gate the predicate
        stays a closed set of same-org project ids — it never re-opens the NULL/global
        leg the gate exists to close. That is why the widening cannot resurrect leak
        vector 9: NULL is not, and can never be, a member of source_ids.

    This is the PROJECT layer only. Content-type scoping (SPEC-content-types §4) is a
    SECOND, orthogonal layer ANDed on top by _tm_read_scope — kept separate so this
    primitive's predicate stays exactly what the multi-org isolation proof asserts.

    Caveat (why this is belt-and-suspenders): SQL `col IN (pid, NULL)` does NOT
    actually match rows where col IS NULL (NULL compares unknown), so the historical
    "global TM" fallback was already inert for NULL-project rows in practice. This
    predicate makes the isolation EXPLICIT (no NULL leg at all under the gate) rather
    than relying on that SQL quirk. A genuinely shared, org-scoped global TM would
    need an org_id column on the TM tables (flagged as a P3 follow-up below).

    Follow-up (flagged for a later phase): re-introducing a SCOPED global TM in
    multi-org mode (a home-org-wide shared TM) needs an org_id column on SegmentTM/
    FullTextTM/SegmentEmbedding — those tables carry only project_id today, so P3
    cannot express "global within one org." Until then multi-org TM is per-project."""
    from app.services import access

    srcs = tuple(s for s in (source_ids or ()) if s is not None and s != project_id)
    if restrict_ids is not None:
        # NARROW-ONLY (LAW-TM-9): intersect the live readable concrete id set with the
        # selection. NULL/global is dropped (inert anyway). Empty -> match nothing.
        readable = {project_id, *srcs}
        keep = tuple(sorted(readable & {r for r in restrict_ids if r is not None}))
        if not keep:
            return sa.false()
        return col.in_(keep)
    if access.multi_org_active():
        if not srcs:
            return col == project_id
        return col.in_((project_id,) + srcs)  # noqa: RUF005
    if not srcs:
        return col.in_((project_id, None))
    return col.in_((project_id,) + srcs + (None,))  # noqa: RUF005


# --- ATTACHED-SOURCE PRECEDENCE: your own memory always wins ------------------
# Widening the read scope created a question the single-project world never had: when
# the project's OWN row and an attached source's row are EQUALLY good, which one is
# served? Before these helpers the answer was "whichever the database happened to
# return first", which in practice meant the LOWER rowid -- and the attached source is
# by definition the OLDER, already-established TM, so it usually had it.
#
# Two consequences, both bad and both silent:
#   * a project's own curated translation was shadowed by a borrowed one on the 100%
#     exact short-circuit, which is the path that AUTO-APPLIES without review; and
#   * a reviewer's correction could never take effect. Approving a better target
#     writes a new own-project row with a HIGHER id, so the borrowed row kept winning
#     forever. The only escape was detaching the source entirely -- i.e. the fix for
#     "one segment is wrong" was "give up all borrowed memory".
#
# THE RULE: OWN PROJECT FIRST, then lowest id. Own-first is the semantic half (what
# you curated outranks what you borrowed; a correction lands immediately). Lowest-id
# is the determinism half: without it the winner among two equally-ranked rows still
# depends on scan order, so the same lookup could answer differently on two installs,
# or after a VACUUM.
#
# Applied on EVERY path that ranks candidates -- the two exact short-circuits, both
# fuzzy scorers, the few-shot example sort, full-text and semantic -- so no two paths
# can disagree about precedence. (ft_lookup and semantic_lookup were missed in the
# first pass and added in re-review; the full-text one mattered most, because a
# FULL_TEXT hit short-circuits an entire document.)
#
# HONEST SCOPE OF THE CHANGE. With nothing attached the OWN-FIRST term is a constant
# (every candidate is own-project), so it changes no result. The LOWEST-ID term is
# not: on the fuzzy paths an EQUAL-score tie used to be settled by scan order, and is
# now settled by lowest id, on every install whether or not it has ever attached
# anything. That is a deliberate improvement -- a coin-flip became a rule -- but it is
# a behaviour change, not a no-op, and calling it "byte-identical" would be false. The
# exact paths were already effectively id-ordered by the scan, so those are unchanged
# in practice.
def _own_first(model, project_id):
    """SQL ordering term: 0 for `project_id`'s own rows, 1 for attached-source rows.

    Pair it with an `id` term (`.order_by(_own_first(M, pid), M.id)`) for the full
    deterministic precedence. With nothing attached every row scores 0 and the ORDER BY
    collapses to `id` — the order an unordered single-project scan already produced in
    practice, so the SQL paths are unchanged there. Note this is NOT true of the
    in-memory twin below on the fuzzy paths: see the block comment's "honest scope"
    note, where an equal-score tie moves from scan order to lowest id everywhere."""
    return sa.case((model.project_id == project_id, 0), else_=1)


def _own_first_key(row, project_id, score=None, row_id=None):
    """The Python twin of _own_first for the paths that score in memory.

    Returns a tuple that sorts BEST-FIRST under `>` (higher wins): score, then the
    own-project bonus, then the negated id. Used by the fuzzy scorers, which pull
    candidates in a length-closeness order that says nothing about project or id, so
    a bare `score > best_score` comparison would resolve ties by scan order.

    `row_id` overrides which id breaks the final tie. semantic_lookup scores
    SegmentEmbedding rows but resolves to SegmentTM rows, and passes the SegmentTM id
    so every path tie-breaks on the SAME identity rather than on whichever side table
    happened to be scanned."""
    return (
        (score if score is not None else 0),
        0 if row.project_id == project_id else -1,
        -((row_id if row_id is not None else row.id) or 0),
    )


def _content_type_scope(model, content_type_id):
    """Content-type read predicate for a TM/embedding lookup (SPEC-content-types §4).

    THE GENERAL-BUCKET RULE (§8.4, maintainer-approved): a TYPED project (content_type X)
    reads its OWN type + the shared "General" (NULL) bucket — `content_type_id == X OR
    content_type_id IS NULL`. An UNTYPED project (content_type_id None) reads the
    General bucket ONLY — `content_type_id IS NULL`.

    Why this is byte-identical for untyped installs: with zero ContentType rows every
    project is untyped (content_type_id None here) and every TM row's content_type_id
    is NULL, so BOTH the typed and untyped legs reduce to `content_type_id IS NULL`,
    which matches every existing row. So an install that never defines a content type
    sees exactly today's TM set on all four lookup paths.

    Note the explicit `IS NULL` legs (not `col.in_((X, None))`): SQL `IN (X, NULL)`
    does NOT match rows where col IS NULL (NULL compares unknown), which would silently
    hide the General bucket. The OR-with-IS-NULL form matches it correctly."""
    if content_type_id is None:
        return model.content_type_id.is_(None)
    return sa.or_(model.content_type_id == content_type_id, model.content_type_id.is_(None))


def _tm_read_scope(model, project_id, session, *, include_sources=True, restrict_project_ids=None):
    """The full read scope for a TM/embedding lookup: PROJECT layer AND CONTENT-TYPE
    layer (SPEC-content-types §4) — the single seam every one of the four lookup paths
    (exact-hash, fuzzy, full-text, semantic) + concordance funnels through, so they all
    inherit both isolations.

      1. project scope — _tm_scope(model.project_id, project_id, source_ids) (carries
         the multi-org leak-vector-9 isolation, now also widened by any ATTACHED TM
         source projects; see _tm_source_project_ids for why a link row cannot leak).
      2. content-type scope — _content_type_scope for the project's resolved content
         type: own-type + General(NULL) for a typed project, General-only for an
         untyped one. Resolved via _project_content_type_id(session, project_id).

    Byte-identical for untyped installs with no attachments: layer 1 is unchanged (an
    empty source set leaves the predicate exactly as it was) and layer 2 collapses to
    `content_type_id IS NULL`, which matches every (NULL) row that exists today.

    NOTE that the content-type layer still applies to ATTACHED rows — the READING
    project's content type governs what it may see, wherever the row came from. An
    attach widens WHOSE rows are candidates; it never relaxes any other isolation.

    This is deliberately the ONLY seam that learns about attachments: SegmentTM and
    FullTextTM and SegmentEmbedding all funnel through here, so fuzzy match, the batch
    path, TM few-shot examples, the full-text path, semantic lookup and the 100%-exact
    short-circuit inherit attachment support (and its isolation) without a single one
    of them being edited.

    THE MATCH / BROWSE ASYMMETRY (`include_sources=False`, isolation review ISO-3).
    Attachment widens what a project MATCHES, never what a human may READ OUT. The two
    are not the same permission, and conflating them is a real exposure: a match
    returns one target for one segment the reader already has, whereas CONCORDANCE
    takes an arbitrary substring and returns every row containing it, so a project
    member with no visibility into the source project could walk its whole memory a
    query at a time. Every MATCH path therefore keeps the default (True); every BROWSE
    surface passes False and stays own-project. The /tm module browse was never routed
    through here at all and is unchanged (it scopes by visible_project_ids)."""
    ct_id = _project_content_type_id(session, project_id)
    srcs = _tm_source_project_ids(session, project_id) if include_sources else ()
    return sa.and_(
        _tm_scope(model.project_id, project_id, srcs, restrict_ids=restrict_project_ids),
        _content_type_scope(model, ct_id),
    )


def normalize(text: str) -> str:
    """Whitespace-normalized form used for hashing + exact comparison.

    Whitespace-only: collapse runs of whitespace and trim. Inline-fidelity
    placeholder tokens (⟦r1⟧, ⟦g1⟧…) are deliberately NOT stripped here — stripping
    them poisoned the TM (a tokenized "click ⟦g1⟧here⟦/g1⟧" collided with its
    tagless twin "click here", and a token-bearing target got served to a segment
    with a different/absent inline_map, leaking literal tokens). Instead,
    token-bearing segments are excluded from TM entirely (see is_tm_eligible /
    lookup / write_back), so this normalize never sees them via those paths."""
    return " ".join((text or "").split())


def is_tm_eligible(source_text: str) -> bool:
    """True when a segment may participate in TM (lookup + write-back).

    Segments whose source carries inline-fidelity placeholder tokens are excluded:
    their target must be reconstructed from a segment-specific inline_map, so
    reusing a TM target (built for a different map, or tagless) would leak literal
    tokens or misplace formatting. The lost reuse is limited to the inline-tag
    slice and is the safe trade."""
    from app.services.segment import find_placeholders

    return not find_placeholders(source_text or "")


def hash_text(text: str, source_locale: str, target_locale: str) -> str:
    key = f"{source_locale}\x1f{target_locale}\x1f{normalize(text)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _lang_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.split("-")[0] == b.split("-")[0]


def _pick(langs: dict, locale: str):
    if locale in langs:
        return langs[locale]
    for k, v in langs.items():
        if _lang_match(k, locale):
            return v
    return None


# --- import locale coercion (QA BUG-03) ----------------------------------------
# The READ path (lookup / lookup_batch / example_matches_batch / semantic_lookup)
# matches source_locale/target_locale by EXACT EQUALITY, and hash_text keys on them,
# so the locales a row is STORED under are the only ones that can ever match it. The
# WRITE path took the caller's locale arguments verbatim. Reproduced in production
# 2026-07-28: `tm import 8 file.tmx --source en --target es` into an en-US -> es-ES
# project reported "Imported 23112 TM entries", the project page then claimed 23,112
# TM segments, and NOT ONE of them could ever be returned by lookup. Re-importing the
# same file under the corrected locales imported another 23,112 rows (the flags, not
# the file, decide the bucket), so the operator now had two buckets, one of them dead.
#
# Public TMX corpora (OPUS/TED/DGT/GlobalVoices) all carry bare `xml:lang="en"/"es"`,
# so passing `en`/`es` is the NATURAL thing to do — and note the READER already
# base-matches (`_pick`/`_lang_match`), which is exactly why the import looked like it
# worked. The inconsistency was: tolerant when READING the file, literal when WRITING
# the row, exact when READING the row back.
#
# The rule, applied at the SERVICE SEAM so web + CLI + /api all inherit it (repo law:
# one seam, thin surfaces):
#   * exact match with the project's locales                 -> unchanged, verbatim.
#   * same base language, different region/case (en vs en-US) -> COERCED to the
#     project's spelling, and reported ("locales_normalized": True) so the coercion is
#     never silent.
#   * a different language entirely (de -> fr into an en-US -> es-ES project) -> REFUSED
#     with a message naming both pairs. Storing it would be a guaranteed dead bucket.
#   * ambiguous (`es` against a project carrying BOTH es-ES and es-MX) -> REFUSED. The
#     mechanism must not guess which of the operator's targets they meant.
# The project's own locales are the truth because they are what the READ path passes.
# When there is no project row, or the project declares no target locales, there is no
# truth to check against and the arguments stand (byte-identical to the old behaviour).
class LocaleMismatchError(ValueError):
    """A TMX import whose locale pair cannot be reconciled with the project's.

    Subclasses ValueError so every existing caller's `except Exception` / `except
    ValueError` arm (CLI -> exit 1 with the message, /api -> 400, web -> error
    redirect) surfaces it unchanged."""


def resolve_import_locales(
    session: Session, project_id: int | None, source_locale: str, target_locale: str
) -> tuple[str, str, bool]:
    """(source_locale, target_locale, normalized) to STORE a TMX import under.

    Raises LocaleMismatchError when the requested pair cannot be reconciled with the
    project's. See the block comment above for the full rule."""
    if project_id is None:
        return source_locale, target_locale, False
    from app.models import Project

    project = session.get(Project, project_id)
    if project is None:
        return source_locale, target_locale, False

    normalized = False
    p_src = (project.source_locale or "").strip()
    src_out = source_locale
    if p_src and source_locale != p_src:
        if _lang_match(source_locale, p_src):
            src_out, normalized = p_src, True
        else:
            raise LocaleMismatchError(
                f"this project translates {p_src} -> "
                f"{', '.join(_project_targets(session, project_id)) or '(no targets)'}, "
                f"but the import asked for {source_locale} -> {target_locale}. "
                f"TM is only ever matched on the project's own locales, so these "
                f"entries could never be used. Re-run with the project's locales."
            )

    targets = _project_targets(session, project_id)
    tgt_out = target_locale
    if targets and target_locale not in targets:
        near = [t for t in targets if _lang_match(t, target_locale)]
        if len(near) == 1:
            tgt_out, normalized = near[0], True
        elif len(near) > 1:
            raise LocaleMismatchError(
                f"target locale {target_locale!r} is ambiguous for this project: it "
                f"matches {', '.join(near)}. Re-run naming the exact target locale."
            )
        else:
            raise LocaleMismatchError(
                f"this project translates {p_src or '(none)'} -> "
                f"{', '.join(targets)}, but the import asked for "
                f"{source_locale} -> {target_locale}. TM is only ever matched on the "
                f"project's own locales, so these entries could never be used. "
                f"Re-run with the project's locales."
            )
    return src_out, tgt_out, normalized


def _project_targets(session: Session, project_id: int) -> list[str]:
    """The project's declared target locales, in id order."""
    from app.models import TargetLocale

    return [
        t
        for t in session.exec(
            select(TargetLocale.locale).where(TargetLocale.project_id == project_id)
        ).all()
        if t
    ]


def _pair_has_rows(session, project_id, source_locale, target_locale) -> bool:
    """True when (project, source, target) has at least one SegmentTM row -- a bounded
    EXISTS-shaped probe (LIMIT 1, no row load), not a full fetch."""
    return (
        session.exec(
            select(SegmentTM.id)
            .where(
                SegmentTM.project_id == project_id,
                SegmentTM.source_locale == source_locale,
                SegmentTM.target_locale == target_locale,
            )
            .limit(1)
        ).first()
        is not None
    )


def resolve_export_locales(
    session: Session, project_id: int | None, source_locale: str, target_locale: str
) -> tuple[str, str]:
    """The (source, target) spelling `export_tmx` must query to reach the SAME bucket
    the write path stored under (QA BUG-03, read-back leg).

    THE THIRD READ-BACK. `import_tmx` coerces a region/case variant of the requested
    pair to the project's own spelling before it writes (resolve_import_locales), and
    every LOOKUP path base-matches (`_pick`/`_lang_match`), so a row for a project whose
    pair is en-US -> es-419 is STORED and MATCHED under en-US/es-419 no matter whether
    the operator typed `en`/`es` or `EN-us`. Export was the one read-back still doing an
    EXACT-equality query, so `tm export --source en --target es` (the natural spelling —
    public TMX corpora and CLDR region codes both invite it) returned an EMPTY TMX while
    `/tm` and `tm/stats` plainly showed the rows (AUDIT-phase2 FINDING 5's "verify
    trap"). Coercing to the project's spelling makes export tolerant EXACTLY as the
    write path is, so the same spelling reaches the same bucket on both legs.

    LITERAL-FIRST (review M4). Coercion must never SHADOW a real bucket: a project can
    carry rows for BOTH es-419 AND es-ES (the second is durable after remove_target_locale
    drops the target locale), and a blind coerce of the requested `es-ES` to the project's
    first-matching target `es-419` would return the WRONG rows and leave the es-ES rows
    unreachable. So: if the LITERAL requested pair already has rows, use it verbatim; only
    when the literal query is empty do we coerce a region/case variant to the project's
    spelling. An exact/populated pair is therefore byte-identical, and the coercion only
    ever RESCUES an otherwise-empty export.

    Tolerant, never stricter: a genuinely UNRELATED or AMBIGUOUS pair (which
    resolve_import_locales REFUSES for a write) is not an error here — the project simply
    has no such rows, so we keep the literal locales and the query returns an empty
    export. No project scope to resolve (project_id None, or no project row) -> the
    arguments stand, unchanged."""
    if project_id is None:
        return source_locale, target_locale
    # M4: the literal pair wins whenever it actually has rows (never shadow it).
    if _pair_has_rows(session, project_id, source_locale, target_locale):
        return source_locale, target_locale
    try:
        src, tgt, _ = resolve_import_locales(session, project_id, source_locale, target_locale)
        return src, tgt
    except LocaleMismatchError:
        # An unrelated/ambiguous pair has no bucket to reach; keep the literal locales
        # (an empty export), never raise — export has never errored on a bad pair.
        return source_locale, target_locale


LEVERAGE_BANDS = ("exact", "fuzzy", "new", "untranslated")


# --- TM-leverage rollup (viz quick-win) ----------------------------------------
# Bands the persisted data actually supports. `Segment.tm_match_pct` is written in
# exactly two shapes (translate.py:559/629, content.py:1351, orchestrator.py:2176):
#   100   -> the exact-hash short-circuit (segment TM 100% hit)
#   75-99 -> a fuzzy TM reference was found and cited (tm.lookup's floor is 75 -
#            see the module docstring, "100% exact + 75-99% fuzzy" - so no stored
#            value ever lands below 75)
#   None  -> NO TM reference was ever cited for this segment, for ANY reason: an
#            MT call with no fuzzy match to draw on, a HUMAN-TYPED/edited target
#            (the CAT editor never stamps tm_match_pct on a manual edit), or an
#            IMPORTED translation (bulk import writes target_text without ever
#            touching tm_match_pct). The write sites above are the ONLY ones that
#            ever set this field - every other path that fills target_text simply
#            leaves it None. So this band's honest label is "No TM match", NEVER
#            "New (MT)" - labeling it as machine-translated would misreport real
#            translator work (typed/edited/imported) as MT, including in the
#            aria-label/sr-only text an accessibility user relies on verbatim.
#            The internal key stays "new" (nothing reads the key as UI text - only
#            the label strings in _viz_macros.html do, and those say "No TM match").
# Semantic-TM matches (tm.semantic_lookup, kind="semantic") are surfaced live as
# CAT-editor suggestions but are NEVER persisted back onto tm_match_pct anywhere in
# the codebase (grep confirms only the four exact/fuzzy write sites above exist) -
# so a true 4th "semantic" band would require a schema change (out of scope here).
# These 4 bands are therefore what the data actually has, and they partition every
# segment exactly once (an untranslated segment counts as "untranslated", not "new").
def leverage_bands(segs) -> dict[str, int]:
    """Bucket a list of Segment rows (or dicts with the same 2 fields) into the TM
    -leverage bands for the leverage-bar viz. Pure/offline - no query, just a
    rollup over segments the CALLER already loaded (project Overview / reports
    both already fetch the full segment list for their existing KPIs; this reuses
    that list rather than issuing a new scoped query). Every segment lands in
    exactly one band, so sum(bands.values()) == len(segs) always.

    The "new" band (tm_match_pct is None, target_text present) covers ANY
    translated segment with no cited TM reference - net-new MT AND human-typed/
    edited/imported segments alike (none of those write tm_match_pct). Render it
    as "No TM match", never "New (MT)" - see the LEVERAGE_BANDS comment above."""
    bands = dict.fromkeys(LEVERAGE_BANDS, 0)
    for s in segs:
        if isinstance(s, dict):
            pct, target = s.get("tm_match_pct"), s.get("target_text")
        else:
            pct, target = s.tm_match_pct, s.target_text
        if not target:
            bands["untranslated"] += 1
        elif pct == 100:
            bands["exact"] += 1
        elif pct is not None:
            bands["fuzzy"] += 1
        else:
            bands["new"] += 1
    return bands


def count(session: Session, project_id: int | None) -> int:
    return session.exec(
        select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == project_id)
    ).one()


# Issue #567: the skip codes every non-approve TM write path shares with the approve seam
# (segment_review, #562 / #581). Only `placeholder_integrity` may be acknowledged; tag
# loss and tagged sources are never TM-eligible, whatever the operator accepts.
TM_SKIP_PLACEHOLDER = "placeholder_integrity"
TM_SKIP_TAG = "tag_integrity"
TM_SKIP_TAGGED_SOURCE = "tagged_source"


def integrity_skip(
    source_text: str | None,
    target_text: str | None,
    *,
    acknowledge_placeholder_loss: bool = False,
    source_locale: str | None = None,
    target_locale: str | None = None,
) -> tuple[str, str] | None:
    """(code, reason) when the pair must NOT enter TM, else None (Issue #567).

    The same three checks `segment_review.approve_and_write_tm` applies, in the same order
    and with the same detectors (`is_tm_eligible`, `quality.tag_damage`,
    `quality.placeholder_violation` with default args), so TMX import, manual TM edit and
    every other TM writer agree with approve:
      * tagged_source: a source carrying inline-fidelity tokens is never TM-eligible;
      * tag_integrity: the target's inline tokens diverge from the source's;
      * placeholder_integrity: the real-placeholder multiset ({0}, %s, {{name}}) diverges;
        the ONLY code `acknowledge_placeholder_loss=True` overrides.
    An empty target is not judged here (callers own the empty-target rule)."""
    from app.services import quality

    if not is_tm_eligible(source_text or ""):
        return (
            TM_SKIP_TAGGED_SOURCE,
            "source has inline tags; tagged segments are never saved to TM",
        )
    tag_reason = quality.tag_damage(source_text, target_text)
    if tag_reason:
        return (
            TM_SKIP_TAG,
            f"inline tag integrity broken ({tag_reason}); damaged inline tags never enter TM",
        )
    ph_reason = quality.placeholder_violation(
        source_text, target_text, source_locale=source_locale, target_locale=target_locale
    )
    if ph_reason and not acknowledge_placeholder_loss:
        return (TM_SKIP_PLACEHOLDER, f"placeholder integrity broken ({ph_reason})")
    return None


def _source_fallback_blocked(segment) -> bool:
    """True when `segment` carries a `source_fallback` qa_flag (GitHub #42).

    A FULL_TEXT paragraph reverted to its SOURCE text produces a body that is partly
    UNTRANSLATED but structurally perfect, so every downstream check reads it as clean.
    Without this refusal `segment_approve`'s TM write-back (and the FULL_TEXT `ft_store`
    on the translate path) would launder that English straight into shared TM as a
    100%-match, and a TM hit skips the review gates on every later document. Same
    posture as the refusal / non-translation guards: the text is kept for THIS
    document, it just may never seed shared TM."""
    if segment is None:
        return False
    from app.services import quality

    return bool(quality.source_fallback_indices(getattr(segment, "qa_flags", None)))


def ft_count(session: Session, project_id: int | None) -> int:
    return session.exec(
        select(func.count()).select_from(FullTextTM).where(FullTextTM.project_id == project_id)
    ).one()
