"""tm: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import math

from lxml import etree
from rapidfuzz import fuzz
from sqlalchemy import func
from sqlmodel import Session, select

from app import branding
from app.models import SegmentTM
from app.services.tm_common import (
    _own_first,
    _own_first_key,
    _tm_read_scope,
    hash_text,
    is_tm_eligible,
    normalize,
)

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


TM_FUZZY_CANDIDATE_CAP = 500

# #599: fuzz.ratio is O(len(a) * len(b)) and rapidfuzz holds the GIL for each call, so a
# 100k-character source scored against up to TM_FUZZY_CANDIDATE_CAP long TM rows froze every
# thread (the event loop and /health included) for minutes. Fuzzy matching applies to sources
# and TM rows up to this many NORMALIZED characters (about 1,500 words, far past any real
# segment); longer text still gets the exact (hash) 100% match, never a fuzzy one.
TM_FUZZY_MAX_CHARS = 10_000


def _fuzzy_ratio(q: str, cq: str, floor) -> float:
    """fuzz.ratio(q, cq) in bounded time (#599). Every caller drops a score below its
    floor, so the floor is passed as rapidfuzz's score_cutoff (an early exit and a narrower
    band; a score below it reads 0.0, which the caller drops exactly as before). Text over
    TM_FUZZY_MAX_CHARS is never fuzzy-scored: -1.0, below any floor."""
    if len(q) > TM_FUZZY_MAX_CHARS or len(cq) > TM_FUZZY_MAX_CHARS:
        return -1.0
    cutoff = floor if floor and 0 < floor <= 100 else 0
    return fuzz.ratio(q, cq, score_cutoff=cutoff)


TM_UNION_CHUNK = 150


_META_PROP_MAP = {
    "origin": "x-origin",
    "content_type": "x-content_type",
    "domain": "x-domain",
    "context_prev": "x-context_prev",
    "context_next": "x-context_next",
}


_PROP_META_MAP = {v: k for k, v in _META_PROP_MAP.items()}


_META_VALUE_CAP = 500


# Hard ceiling on how many SegmentEmbedding rows the semantic scorer will ever
# pull into Python for a single semantic_lookup(). The SQL prefilter (project
# scope + locale pair + model) already narrows the set; this cap bounds the worst
# case so semantic lookup can never load an unbounded slice.
# Hard ceiling on how many candidate rows the fuzzy scorer will ever pull into
# Python for a single lookup(). The SQL prefilters (project scope + min_score-aware
# length band + shared-prefix LIKE) already narrow the set; this cap bounds the
# worst case (a huge TM where thousands of rows share a length band + prefix) so a
# single lookup can never load an unbounded slice. Rows are ORDER BY length
# closeness to the query so the cap keeps the most-likely matches.
# Max number of source-segment winnows OR'd into a single lookup_batch() candidate
# query. Each winnowed source contributes a nested OR clause; SQLite caps SQL
# expression-tree DEPTH at 1000, so a batch of a few hundred segments (a long
# document) would overflow one giant union query. lookup_batch() chunks the
# winnowed sources into groups of this size, runs one bounded query per chunk, and
# merges the deduped candidate pools — result-identical to a single union query but
# never exceeding the depth limit. Kept well under 1000 (each source adds up to 2
# winnow clauses, so ~2x this many OR nodes) with headroom for the surrounding
# scope/locale predicates and for other dialects' own limits. Chunking is a no-op
# on engines without a depth limit (e.g. Postgres).
# --- SegmentTM.meta provenance (SPEC-context-metadata phase 4) -----------------
# `SegmentTM.meta` is ADDITIVE provenance, NEVER a TM-match key. It is stamped at
# write time (write_back / import_tmx) and carried into/out of TMX as
# <prop type="x-..."> lines so metadata survives interchange. The four match paths
# (lookup / lookup_batch / example_matches_batch / semantic_lookup) never read meta,
# so stamping it cannot change any match result.
#
# Canonical mapping: meta dict KEY  <->  TMX <prop type="..."> VALUE. Only these keys
# cross the TMX boundary; any other meta key stays local, and a foreign/unknown prop
# on import is ignored. Kept small + stable (a change here is a schema change — refresh
# goldens).
# human_approved | tmx_import | mt | connector:<kind>
# source content kind, when cheaply resolvable
# ITS 2.0 Domain, when in hand
# TMX-style neighbour source (previous segment)
# TMX-style neighbour source (next segment)
# Cap on any single stored/interchange meta value. Values arriving from an external
# TMX (import) or a glossary CSV are DATA about content, never instructions; phase 4
# does not render them into a prompt, but they are newline-flattened + length-capped
# at INGESTION so a future render seam can never be fed a structure-token/newline
# payload smuggled through meta (forward-looking safety, SPEC "Security invariant").
def _like_escape(s: str) -> str:
    """Escape LIKE wildcards in `s` so %/_/\\ match literally under a
    `.like(pattern, escape="\\")` clause — the single escaping used by every TM
    prefix/substring LIKE probe (tm.py + tm_module.py). Escapes the backslash FIRST
    (so the escapes added for % and _ are not themselves re-escaped), then % and _."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sanitize_meta_value(val) -> str:
    """Flatten + cap an externally-sourced meta value (TMX prop / glossary CSV cell).

    Collapses all whitespace (incl. newlines) to single spaces and truncates to
    _META_VALUE_CAP chars. Additive provenance DATA only — never an instruction — but
    sanitized at ingestion regardless."""
    return " ".join(str(val or "").split())[:_META_VALUE_CAP]


def _provenance_meta(origin, *, content_type=None, extra=None):
    """Build a small, stable provenance dict for SegmentTM.meta (empty keys dropped).

    `origin` is the required provenance tag. `content_type` (a name string) is added
    only when cheaply in hand. `extra` folds in already-derived _META_PROP_MAP keys
    (e.g. neighbour context) that a write site has on hand. None/empty values are
    omitted so a stamp is never padded with blanks (keeps TMX export tight)."""
    meta = {"origin": origin}
    if content_type:
        meta["content_type"] = _sanitize_meta_value(content_type)
    if extra:
        for k, v in extra.items():
            if k in _META_PROP_MAP and v:
                meta[k] = _sanitize_meta_value(v)
    return meta


def _meta_from_tu(tu) -> dict | None:
    """Read our x-* <prop> lines off a TMX <tu> into a SegmentTM.meta dict.

    Only recognized props (_PROP_META_MAP) are kept; foreign/absent props are ignored
    so a third-party TMX imports cleanly. Values are sanitized at ingestion. Returns
    None when no recognized prop is present, so the row stays meta-less (a legacy /
    propless import is byte-identical, and a legacy re-export stays byte-identical)."""
    meta: dict = {}
    for prop in tu.findall("prop"):
        key = _PROP_META_MAP.get(prop.get("type") or "")
        if key:
            meta[key] = _sanitize_meta_value("".join(prop.itertext()))
    return meta or None


def _tmx_stamp_tu(tu, entry) -> None:
    """Emit provenance onto a <tu> from a SegmentTM row's meta.

    NO-OP when meta is empty -> byte-identical export for legacy/meta-less rows (the
    golden guarantee). Otherwise adds standard TMX 1.4b provenance attrs
    (creationtool / creationdate / usagecount) + one <prop type="x-..."> per recognized
    meta key. Props are appended BEFORE any <tuv> child (caller adds tuvs after) since
    the TMX DTD requires prop/note to precede tuv."""
    meta = entry.meta or None
    if not meta:
        return
    tu.set("creationtool", branding.brand_name())
    created = getattr(entry, "created_at", None)
    if created is not None:
        try:  # TMX creationdate: ISO 8601 UTC basic form YYYYMMDDThhmmssZ  # noqa: SIM105
            tu.set("creationdate", created.strftime("%Y%m%dT%H%M%SZ"))
        except Exception:  # noqa: S110 -- optional TMX attribute; omitted if the date is odd
            pass
    uc = getattr(entry, "use_count", 0) or 0
    if uc:
        tu.set("usagecount", str(uc))
    for key, prop_type in _META_PROP_MAP.items():
        val = meta.get(key)
        if val:
            prop = etree.SubElement(tu, "prop")
            prop.set("type", prop_type)
            prop.text = str(val)


def _build_export_tmx(entries, source_locale, target_locale) -> bytes:
    tmx = etree.Element("tmx", version="1.4")
    header = etree.SubElement(tmx, "header")
    header.set("creationtool", branding.brand_name())
    header.set("segtype", "sentence")
    header.set("srclang", source_locale)
    header.set("datatype", "plaintext")
    body = etree.SubElement(tmx, "body")
    for e in entries:
        tu = etree.SubElement(body, "tu")
        # Phase 4: stamp provenance props/attrs from e.meta FIRST (DTD requires
        # prop/note before tuv). NO-OP for a meta-less row -> byte-identical export.
        _tmx_stamp_tu(tu, e)
        for lang, text in ((source_locale, e.source_text), (target_locale, e.target_text)):
            tuv = etree.SubElement(tu, "tuv")
            tuv.set(XML_LANG, lang)
            seg = etree.SubElement(tuv, "seg")
            seg.text = text
    return etree.tostring(tmx, xml_declaration=True, encoding="utf-8", pretty_print=True)


def lookup(
    session: Session,
    project_id: int | None,
    source_text: str,
    source_locale: str,
    target_locale: str,
    min_score: int = 75,
    restrict_project_ids: set | None = None,
) -> dict | None:
    """Best segment-TM match for a source string, or None below min_score.

    `restrict_project_ids` (Batch 15 T1, SPEC-tm-match-step-config §7): when NOT None,
    NARROWS the readable project-leg to the intersection with this set (a per-step
    tm_match TM pool selection). None (every non-tm_match caller) = byte-identical.

    Token-bearing sources are excluded from TM (see is_tm_eligible) to avoid
    serving a target built for a different/absent inline_map.

    Scale strategy (unchanged signature/return shape):
      1. exact-hash short-circuit FIRST — a 100% match is looked up by
         source_hash and returned without scanning/scoring the table.
      2. otherwise push the cheap winnowing into SQL before pulling rows into
         Python: project scope (this project OR global), a min_score-aware
         length LOWER bound (lo only — see below), and a shared-prefix LIKE as a
         widening generator; ORDER BY length closeness and hard-cap the candidate
         set (TM_FUZZY_CANDIDATE_CAP).

      Length-band math (lo-only): fuzz.ratio(a,b) <= 100*2*min/(la+lb), so for
      ratio >= s a NORMALIZED-length-C candidate against a NORMALIZED-length-L
      query needs L*s/(2-s) <= C <= L*(2-s)/s. But the SQL band runs on
      func.length(STORED source_text) while scoring runs on normalize()d text,
      and stored length >= normalized length (normalize collapses whitespace).
      So only the LOWER bound is sound: stored_len < lo => normalized_len < lo =>
      genuinely below min_score. The UPPER bound is NOT applied — a row whose
      normalized length is in-band can have a stored length above hi (a stored
      source padded with whitespace runs), so `stored_len <= hi` would wrongly
      drop a legitimate match. The cap + prefix widener bound the pull instead."""
    if not is_tm_eligible(source_text):
        return None

    scope = _tm_read_scope(
        SegmentTM, project_id, session, restrict_project_ids=restrict_project_ids
    )

    # 1. exact-hash short-circuit: an identical (normalized) source is a 100%
    #    match; fetch it by hash and return without any fuzzy scan.
    h = hash_text(source_text, source_locale, target_locale)
    exact = session.exec(
        select(SegmentTM)
        .where(
            SegmentTM.source_hash == h,
            SegmentTM.source_locale == source_locale,
            SegmentTM.target_locale == target_locale,
            scope,
            # OWN PROJECT FIRST, then lowest id (see _own_first). This is the path that
            # auto-applies a target without review, so a borrowed row must never shadow
            # the project's own curated one -- and a reviewer's correction, which lands as
            # a HIGHER-id own row, has to be able to win.
        )
        .order_by(_own_first(SegmentTM, project_id), SegmentTM.id)
    ).first()
    if exact is not None:
        return {
            "match_pct": 100,
            "source_text": exact.source_text,
            "target_text": exact.target_text,
            # `id` lets an APPLY site bump usage (tm_module.bump_usage). Absent on a
            # fuzzy dict (fuzzy matches are never auto-applied, so never counted).
            "id": exact.id,
        }

    q = normalize(source_text)
    qlen = len(q)
    if qlen > TM_FUZZY_MAX_CHARS:
        return None  # #599: exact match only past the fuzzy length ceiling

    stmt = select(SegmentTM).where(
        SegmentTM.source_locale == source_locale,
        SegmentTM.target_locale == target_locale,
        scope,
    )

    # A winnowing predicate = (length band) OR (shared-prefix LIKE). Either alone
    # is safe (see below); OR-ing them only WIDENS the candidate set, so no real
    # match is ever dropped. Adding more rows can only cost fuzzy-scoring time.
    winnow = []

    # min_score-aware length LOWER bound (lo only — the hi bound is UNSOUND, see
    # below). fuzz.ratio(a,b) = 100*2M/(len a+len b) with M <= min(len a, len b),
    # so ratio <= 100*2*min/(la+lb). For ratio >= s (= min_score/100) a candidate
    # of NORMALIZED length C against a query of NORMALIZED length L must satisfy
    # L*s/(2-s) <= C <= L*(2-s)/s.
    #
    # The band runs on func.length(stored source_text) but scoring runs on
    # normalize()d text, and normalize is whitespace-collapsing so the STORED
    # length is always >= the normalized length. That asymmetry makes the two
    # sides behave differently:
    #   - lo side is SOUND: stored_len < lo  =>  normalized_len <= stored_len < lo,
    #     so the row is provably below min_score and safe to exclude in SQL.
    #   - hi side is UNSOUND: a row whose NORMALIZED length is in-band (<= hi) can
    #     have a STORED length > hi (e.g. "the" + 30 spaces + "quick brown fax"),
    #     so `stored_len <= hi` would wrongly drop a legitimate >=min_score match.
    #     (Empirically reproduced.) So we DO NOT apply the hi bound here; the
    #     candidate cap + the prefix-LIKE widener keep the pull bounded instead.
    if qlen and 0 < min_score <= 100:
        s = min_score / 100.0
        lo = int(math.floor(qlen * s / (2.0 - s)))  # noqa: RUF046
        winnow.append(func.length(SegmentTM.source_text) >= lo)

    # Shared-prefix LIKE as a widening generator: rows whose stored source shares
    # the query's first few normalized chars are strong fuzzy candidates and stay
    # cheap via a leading-anchored LIKE (index/prefix-friendly). The escape guards
    # %/_/\ so those chars in the prefix match literally.
    prefix = q[:4]
    if prefix:
        esc = _like_escape(prefix)
        winnow.append(SegmentTM.source_text.like(esc + "%", escape="\\"))

    if winnow:
        clause = winnow[0]
        for w in winnow[1:]:
            clause = clause | w
        stmt = stmt.where(clause)

    # ORDER BY closeness of stored length to the query length so the hard cap
    # keeps the most-likely matches, then bound the pull into Python.
    stmt = stmt.order_by(func.abs(func.length(SegmentTM.source_text) - qlen)).limit(
        TM_FUZZY_CANDIDATE_CAP
    )

    candidates = session.exec(stmt).all()
    if not candidates:
        return None

    best = None
    best_score = -1.0
    best_key = None
    for c in candidates:
        score = _fuzzy_ratio(q, normalize(c.source_text), min_score)
        # Own-project-first, then lowest id, on EQUAL scores (see _own_first_key).
        # The candidate list arrives in length-closeness order, which is silent about
        # both, so a bare `score > best_score` resolved ties by scan order.
        key = _own_first_key(c, project_id, score)
        if best_key is None or key > best_key:
            best_key, best_score, best = key, score, c
    if best is None or best_score < min_score:
        return None
    return {
        # FLOOR, never round (#491): a fuzzy score of 99.5..99.9 must not report 100.
        # Callers gate auto-apply on `match_pct == 100` (orchestrator step-0 TM fill,
        # tm_lock band "100", the LAW-TM-6 fuzzy parity check), so rounding UP a near
        # miss would auto-apply a fuzzy target as if it were hash-exact. A genuine
        # 100.0 ratio (identical text after normalization) still floors to 100.
        "match_pct": math.floor(best_score),
        "source_text": best.source_text,
        "target_text": best.target_text,
        # Batch 15 T1 (SPEC-tm-match-step-config §6.2, finding 12): carry the winning
        # entry id on a FUZZY hit too, so a below-100 tm_match fill stamps real TM
        # provenance (_stamp_lock tm_id) and bumps use_count. Pre-Batch-15 this was
        # absent (fuzzy matches were never auto-applied); the below-threshold fill path
        # now applies them, so the id must be present. No behaviour change for a caller
        # that ignores it.
        "id": best.id,
    }


def lookup_batch(
    session: Session,
    project_id: int | None,
    sources: list[str],
    source_locale: str,
    target_locale: str,
    min_score: int = 75,
    restrict_project_ids: set | None = None,
) -> dict:
    """Batched version of lookup(): one exact-hash query + one widened candidate
    query for the WHOLE list of sources, instead of a query pair per segment.

    Returns results IDENTICAL to calling lookup() once per source (that's the
    test) — same exact-hash short-circuit, same min_score-aware length LOWER
    bound + shared-prefix widening, same hard candidate cap, same fuzz.ratio
    scoring. The guarantee is exact: on the fast path each source's slice is
    re-derived + re-capped from the shared pool to match lookup(); and when the
    shared pool SATURATES (returns exactly its unordered LIMIT, so it may have
    dropped rows a standalone lookup() would keep), the batch falls back to a
    real lookup() per source — so the equality holds at every TM size.

    Strategy:
      1. Token-ineligible / blank sources map to None immediately (no query).
      2. ALL exact-hash lookups resolve in ONE `source_hash IN (...)` query.
      3. For sources still unresolved, build ONE candidate query whose predicate
         is the OR of each source's own (length-lower-bound OR shared-prefix)
         winnow — i.e. the union of what per-source lookup() calls would have
         queried — still scoped like lookup(), capped at cap*n_sources. If that
         shared pull SATURATES (len == its limit) it may be an unordered
         truncation, so each source is re-run via lookup() (correct, bounded);
         otherwise the pool is complete and each source's candidates are
         re-capped/re-ordered in Python to reproduce lookup()'s per-source slice.
      4. Each source is scored against ITS OWN candidate slice (same fuzz.ratio
         loop as lookup()), independent of the other sources in the batch.

    Returns {source_text: match_dict_or_None}, one entry per element of
    `sources` (de-duplicated internally; duplicate input strings share one
    lookup)."""
    out: dict[str, dict | None] = {}
    uniq = list(dict.fromkeys(sources))  # de-dup, preserve order
    if not uniq:
        return out

    eligible = [s for s in uniq if is_tm_eligible(s)]
    for s in uniq:
        if s not in eligible:
            out[s] = None
    if not eligible:
        return out

    scope = _tm_read_scope(
        SegmentTM, project_id, session, restrict_project_ids=restrict_project_ids
    )

    # 2. exact-hash short-circuit, batched: one IN query for every source's hash.
    hash_of = {s: hash_text(s, source_locale, target_locale) for s in eligible}
    by_hash: dict[str, SegmentTM] = {}
    exact_rows = session.exec(
        select(SegmentTM)
        .where(
            SegmentTM.source_hash.in_(set(hash_of.values())),
            SegmentTM.source_locale == source_locale,
            SegmentTM.target_locale == target_locale,
            scope,
            # Same precedence as lookup()'s exact path, and it MUST be the same: the
            # batch path is what a whole document runs through, so a divergence here
            # would mean a segment translated alone and the same segment translated in a
            # batch could disagree. The ORDER BY makes the setdefault below keep the
            # own-project, lowest-id row per hash.
        )
        .order_by(_own_first(SegmentTM, project_id), SegmentTM.id)
    ).all()
    for row in exact_rows:
        by_hash.setdefault(row.source_hash, row)

    remaining = []
    for s in eligible:
        h = hash_of[s]
        row = by_hash.get(h)
        if row is not None:
            out[s] = {
                "match_pct": 100,
                "source_text": row.source_text,
                "target_text": row.target_text,
                # `id` for usage tracking at the apply sites (only 100% hits apply).
                "id": row.id,
            }
        else:
            remaining.append(s)

    if not remaining:
        return out

    # 3. one widened candidate query covering every remaining source's own
    #    (length band OR shared-prefix) winnow, OR'd together. A source whose
    #    winnow is EMPTY (both qlen and prefix falsy — e.g. blank/whitespace-only
    #    normalized text) means lookup() would fall through to an UNFILTERED
    #    scoped scan for it; that source is handled by its own dedicated query
    #    below rather than folded into the shared union (folding it in would
    #    force pulling the whole table into the shared pool for every source).
    norm_of = {s: normalize(s) for s in remaining}
    qlen_of = {s: len(norm_of[s]) for s in remaining}

    per_source_winnow: dict[str, list] = {}
    unfiltered_sources = []
    for s in remaining:
        q = norm_of[s]
        qlen = qlen_of[s]
        winnow = []
        if qlen and 0 < min_score <= 100:
            sc = min_score / 100.0
            lo = int(math.floor(qlen * sc / (2.0 - sc)))  # noqa: RUF046
            # lo-only: the hi bound on func.length(STORED source_text) is unsound
            # (stored length >= normalized length), see lookup()'s docstring.
            winnow.append(func.length(SegmentTM.source_text) >= lo)
        prefix = q[:4]
        if prefix:
            esc = _like_escape(prefix)
            winnow.append(SegmentTM.source_text.like(esc + "%", escape="\\"))
        if winnow:
            per_source_winnow[s] = winnow
        else:
            unfiltered_sources.append(s)

    def _score_against(s: str, candidates: list) -> None:
        q = norm_of[s]
        best = None
        best_score = -1.0
        best_key = None
        for c in candidates:
            score = _fuzzy_ratio(q, normalize(c.source_text), min_score)
            key = _own_first_key(c, project_id, score)  # own-first, then lowest id
            if best_key is None or key > best_key:
                best_key, best_score, best = key, score, c
        if best is None or best_score < min_score:
            out[s] = None
        else:
            out[s] = {
                # FLOOR, never round (#491) -- parity with lookup() above: a batch hit
                # must never report a rounded-up 100 that the step-0 auto-apply gate
                # would treat as hash-exact.
                "match_pct": math.floor(best_score),
                "source_text": best.source_text,
                "target_text": best.target_text,
                # Batch 15 T1 (§6.2, finding 12): fuzzy hits carry the winning entry id
                # so a below-100 tm_match fill stamps provenance + bumps usage (parity
                # with lookup()).
                "id": best.id,
            }

    winnowed_sources = [s for s in remaining if s in per_source_winnow]
    if winnowed_sources:
        # Cap the shared pull generously: each source may need up to
        # TM_FUZZY_CANDIDATE_CAP rows post length-sort, so bound the union pull
        # at cap * number-of-sources (still far smaller than an unbounded table
        # scan). This shared LIMIT has NO ORDER BY (a single length-sort can't
        # serve every source's own closeness order at once), so at SATURATION —
        # when the pool comes back with EXACTLY its limit — the DB may have
        # truncated rows a standalone lookup() would have kept for some source.
        # In that (rare, large-TM) case we fall back to per-source lookup() for
        # correctness; otherwise the pool holds every matching row and the
        # per-source re-sort/re-cap below reproduces lookup() exactly.
        pool_limit = TM_FUZZY_CANDIDATE_CAP * len(winnowed_sources)

        # Build the shared candidate pool by CHUNKING the winnowed sources: one
        # SELECT per chunk whose predicate is the OR of that chunk's per-source
        # winnows, then merge (dedup by row id) across chunks. A single query
        # OR'ing EVERY source's winnow nests one BooleanClauseList per source and
        # blows past SQLite's expression-tree depth limit (max depth 1000) once a
        # document has a few hundred segments — the reported crash. Chunking keeps
        # each statement's OR-tree bounded (<= TM_UNION_CHUNK conditions) while the
        # merged, deduped, capped result is IDENTICAL to what one giant union query
        # would have returned: each chunk's predicate is a SUBSET of the full union,
        # so no chunk can surface a row the union wouldn't, and every matching row
        # appears in exactly the chunk(s) whose sources winnow it. Each chunk pulls
        # up to pool_limit rows (same bound as the old single query) so no chunk can
        # truncate before the merged pool reaches saturation; harmless on Postgres.
        pool = []
        seen_ids = set()
        for i in range(0, len(winnowed_sources), TM_UNION_CHUNK):
            chunk = winnowed_sources[i : i + TM_UNION_CHUNK]
            union_clause = None
            for s in chunk:
                winnow = per_source_winnow[s]
                clause = winnow[0]
                for w in winnow[1:]:
                    clause = clause | w
                union_clause = clause if union_clause is None else (union_clause | clause)

            stmt = (
                select(SegmentTM)
                .where(
                    SegmentTM.source_locale == source_locale,
                    SegmentTM.target_locale == target_locale,
                    scope,
                    union_clause,
                )
                .limit(pool_limit)
            )
            for row in session.exec(stmt).all():
                if row.id not in seen_ids:
                    seen_ids.add(row.id)
                    pool.append(row)
            if len(pool) >= pool_limit:
                # Already at the global cap; more chunks can only add rows past the
                # limit the single giant query would itself have truncated at.
                break

        if len(pool) >= pool_limit:
            # Saturated + unordered => fall back to a standalone lookup() per
            # source (bounded: one query pair each, only on the rare saturated
            # batch) so batch results stay identical to per-source lookup().
            for s in winnowed_sources:
                out[s] = lookup(
                    session,
                    project_id,
                    s,
                    source_locale,
                    target_locale,
                    min_score=min_score,
                    restrict_project_ids=restrict_project_ids,
                )
        else:
            for s in winnowed_sources:
                qlen = qlen_of[s]
                # Re-derive this source's own candidate slice from the shared pool
                # in Python (same predicates as lookup(), evaluated locally so we
                # don't re-issue a query per source): length LOWER bound and/or
                # prefix. lo-only, matching lookup() — hi is unsound (see lookup()).
                lo = None
                if qlen and 0 < min_score <= 100:
                    sc = min_score / 100.0
                    lo = int(math.floor(qlen * sc / (2.0 - sc)))  # noqa: RUF046
                prefix = norm_of[s][:4]

                candidates = [
                    row
                    for row in pool
                    if (lo is not None and len(row.source_text) >= lo)
                    or (prefix and row.source_text.startswith(prefix))
                ]
                candidates.sort(key=lambda r: abs(len(r.source_text) - qlen))
                _score_against(s, candidates[:TM_FUZZY_CANDIDATE_CAP])

    # Sources with no winnow at all (rare: blank/empty normalized text) fall
    # through to the same unfiltered-scan-capped query lookup() would run —
    # one small dedicated query each, mirroring lookup() exactly.
    for s in unfiltered_sources:
        qlen = qlen_of[s]
        stmt = (
            select(SegmentTM)
            .where(
                SegmentTM.source_locale == source_locale,
                SegmentTM.target_locale == target_locale,
                scope,
            )
            .order_by(func.abs(func.length(SegmentTM.source_text) - qlen))
            .limit(TM_FUZZY_CANDIDATE_CAP)
        )
        _score_against(s, session.exec(stmt).all())

    return out


def example_matches_batch(
    session: Session,
    project_id: int | None,
    sources: list[str],
    source_locale: str,
    target_locale: str,
    *,
    floor: int = 75,
    window: int = 3,
) -> dict:
    """Batched TOP-N fuzzy retrieval for TM FEW-SHOT (SPEC-context-metadata phase 2B).

    Where lookup_batch() returns the single BEST match per source, this returns the
    top-`window` DISTINCT fuzzy matches per source as example pairs, RELEVANCE-GATED at
    the `floor` similarity (research: one bad match < none — so SKIP rather than pad).
    It is the batched sibling of lookup_batch (one shared, chunked candidate pull for
    the whole `sources` list, then a per-source score) so a document resolves its
    examples in ONE extra query pair instead of an N+1 of per-segment lookups.

    Distinct from lookup_batch on purpose:
      * FUZZY only (no exact-hash short-circuit, no semantic layer) — a 100%/self match
        is EXCLUDED (a segment that had a 100% hit is short-circuited and never prompted;
        and an example that is the segment's own translation teaches nothing). "Self" is
        defined by NORMALIZED-source equality, which also covers a 100% row.
      * keeps the top-`window` scored candidates (deduped by target_text, best-first),
        not just the single best.

    Returns {source_text: [ {match_pct, source_text, target_text}, ... ]} — best-first,
    length <= window, EMPTY list when nothing clears the floor (the caller then renders
    no examples block, so the prompt stays byte-identical). Token-ineligible / blank
    sources map to []. window <= 0 short-circuits to all-empty (no query)."""
    out: dict[str, list] = {s: [] for s in dict.fromkeys(sources)}
    if window <= 0:
        return out
    uniq = [s for s in dict.fromkeys(sources) if is_tm_eligible(s)]
    if not uniq:
        return out

    scope = _tm_read_scope(SegmentTM, project_id, session)
    norm_of = {s: normalize(s) for s in uniq}
    qlen_of = {s: len(norm_of[s]) for s in uniq}

    # Per-source winnow, identical to lookup_batch's (length LOWER bound OR shared
    # prefix). A source with no winnow (blank normalized text) is is_tm_eligible-False
    # already, so every remaining source has at least a prefix winnow.
    per_source_winnow: dict[str, list] = {}
    for s in uniq:
        q = norm_of[s]
        qlen = qlen_of[s]
        winnow = []
        if qlen and 0 < floor <= 100:
            sc = floor / 100.0
            lo = int(math.floor(qlen * sc / (2.0 - sc)))  # noqa: RUF046
            winnow.append(func.length(SegmentTM.source_text) >= lo)
        prefix = q[:4]
        if prefix:
            esc = _like_escape(prefix)
            winnow.append(SegmentTM.source_text.like(esc + "%", escape="\\"))
        if winnow:
            per_source_winnow[s] = winnow

    def _topn_against(s: str, candidates: list) -> None:
        """Score `candidates` for source `s`, keep the top-`window` distinct fuzzy
        matches over the floor, EXCLUDING the normalized-self (100%) match."""
        q = norm_of[s]
        scored = []
        for c in candidates:
            cq = normalize(c.source_text)
            if cq == q:
                continue  # self / 100% — never an example
            score = _fuzzy_ratio(q, cq, floor)
            if score >= floor:
                scored.append((score, c))
        # Best-first, and at EQUAL scores own-project-first then lowest id — the same
        # precedence the two single-winner paths use, so the few-shot examples a
        # prompt is given cannot rank a borrowed pair above the project's own.
        scored.sort(key=lambda sc: _own_first_key(sc[1], project_id, sc[0]), reverse=True)
        picked: list = []
        seen_tgt: set = set()
        for score, c in scored:
            key = c.target_text or ""
            if key in seen_tgt:
                continue  # dedupe identical target payloads
            seen_tgt.add(key)
            picked.append(
                {
                    # FLOOR, never round (#491) -- parity with lookup()/lookup_batch.
                    "match_pct": math.floor(score),
                    "source_text": c.source_text,
                    "target_text": c.target_text,
                }
            )
            if len(picked) >= window:
                break
        out[s] = picked

    winnowed = [s for s in uniq if s in per_source_winnow]
    if not winnowed:
        return out

    # Shared, CHUNKED candidate pull (same depth-limit-safe pattern as lookup_batch).
    pool_limit = TM_FUZZY_CANDIDATE_CAP * len(winnowed)
    pool = []
    seen_ids: set = set()
    saturated = False
    for i in range(0, len(winnowed), TM_UNION_CHUNK):
        chunk = winnowed[i : i + TM_UNION_CHUNK]
        union_clause = None
        for s in chunk:
            w = per_source_winnow[s]
            clause = w[0]
            for extra in w[1:]:
                clause = clause | extra
            union_clause = clause if union_clause is None else (union_clause | clause)
        stmt = (
            select(SegmentTM)
            .where(
                SegmentTM.source_locale == source_locale,
                SegmentTM.target_locale == target_locale,
                scope,
                union_clause,
            )
            .limit(pool_limit)
        )
        for row in session.exec(stmt).all():
            if row.id not in seen_ids:
                seen_ids.add(row.id)
                pool.append(row)
        if len(pool) >= pool_limit:
            saturated = True
            break

    if saturated:
        # Rare (very large TM): the shared pool may have truncated rows a per-source
        # query would keep. Fall back to a dedicated per-source query (bounded: one
        # each, only on this saturated batch), mirroring lookup_batch's correctness
        # fallback.
        for s in winnowed:
            qlen = qlen_of[s]
            stmt = (
                select(SegmentTM)
                .where(
                    SegmentTM.source_locale == source_locale,
                    SegmentTM.target_locale == target_locale,
                    scope,
                )
                .order_by(func.abs(func.length(SegmentTM.source_text) - qlen))
                .limit(TM_FUZZY_CANDIDATE_CAP)
            )
            _topn_against(s, session.exec(stmt).all())
        return out

    for s in winnowed:
        qlen = qlen_of[s]
        lo = None
        if qlen and 0 < floor <= 100:
            sc = floor / 100.0
            lo = int(math.floor(qlen * sc / (2.0 - sc)))  # noqa: RUF046
        prefix = norm_of[s][:4]
        candidates = [
            row
            for row in pool
            if (lo is not None and len(row.source_text) >= lo)
            or (prefix and row.source_text.startswith(prefix))
        ]
        candidates.sort(key=lambda r: abs(len(r.source_text) - qlen))
        _topn_against(s, candidates[:TM_FUZZY_CANDIDATE_CAP])
    return out


def concordance(session, project_id, q, source_locale, target_locale, limit=25):
    """Substring search across the segment TM (source or target).

    Filtered + limited in SQL: project scope, locale pair, and a case-insensitive
    substring match on either source or target, capped at `limit`. The query text
    is a bound parameter with its LIKE wildcards (%/_/\\) escaped so it matches
    literally (no full-table load into Python).

    OWN-PROJECT ONLY -- deliberately NOT widened by attached TM sources
    (`include_sources=False`; isolation review ISO-3). This is a BROWSE surface, not a
    match surface: the caller supplies an arbitrary substring and gets back every row
    containing it, so honouring attachments here would let any member of the borrowing
    project enumerate the source project's entire memory a query at a time -- without
    needing any visibility into that project. Matching, by contrast, answers about a
    segment the caller already holds. See _tm_read_scope for the full asymmetry."""
    q = (q or "").strip().lower()
    if not q:
        return []
    esc = _like_escape(q)
    pat = "%" + esc + "%"
    rows = session.exec(
        select(SegmentTM)
        .where(
            SegmentTM.source_locale == source_locale,
            SegmentTM.target_locale == target_locale,
            _tm_read_scope(SegmentTM, project_id, session, include_sources=False),
            func.lower(SegmentTM.source_text).like(pat, escape="\\")
            | func.lower(SegmentTM.target_text).like(pat, escape="\\"),
        )
        .limit(limit)
    ).all()
    return [{"source": r.source_text, "target": r.target_text} for r in rows]
