"""tm: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import re

from lxml import etree
from sqlalchemy import func
from sqlmodel import Session, select

from app import branding
from app.models import SegmentTM
from app.services.tm_common import (
    _own_first,
    _tm_read_scope,
    context_key,
    hash_text,
    is_tm_eligible,
)

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


_META_PROP_MAP = {
    "origin": "x-origin",
    "content_type": "x-content_type",
    "domain": "x-domain",
    "context_prev": "x-context_prev",
    "context_next": "x-context_next",
}


_PROP_META_MAP = {v: k for k, v in _META_PROP_MAP.items()}


_META_VALUE_CAP = 500

# ADR 0102: the app's own TMX prop carrying SegmentTM.context_key (export writes it, import
# reads it first). Trados-style neighbour props are read as a second choice.
TMX_CONTEXT_KEY_PROP = "x-locos-context-key"
_CONTEXT_KEY_RE = re.compile(r"(?:ctx|key):[0-9a-f]{64}")
_TMX_CONTEXT_PRE_PROP = "x-context-pre"
_TMX_CONTEXT_POST_PROP = "x-context-post"


# --- SegmentTM.meta provenance (SPEC-context-metadata phase 4) -----------------
# `SegmentTM.meta` is ADDITIVE provenance, NEVER a TM-match key. It is stamped at
# write time (write_back / import_tmx) and carried into/out of TMX as
# <prop type="x-..."> lines so metadata survives interchange. The match paths
# (lookup / lookup_batch / lookup_segments) never read meta,
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


def _context_from_tu(tu) -> str | None:
    """The in-context key a <tu> declares, or None to let the importer fall back to the
    adjacent units in file order: the app's own x-locos-context-key prop first, then a
    Trados-style x-context-pre / x-context-post pair (either may be absent: an absent side is
    the empty string, a document edge). Only the first two shapes are read here."""
    own = pre = post = None
    for prop in tu.findall("prop"):
        kind = prop.get("type") or ""
        if kind == TMX_CONTEXT_KEY_PROP:
            val = "".join(prop.itertext()).strip()
            own = val if _CONTEXT_KEY_RE.fullmatch(val) else None
        elif kind == _TMX_CONTEXT_PRE_PROP:
            pre = "".join(prop.itertext())
        elif kind == _TMX_CONTEXT_POST_PROP:
            post = "".join(prop.itertext())
    if own:
        return own
    if pre is not None or post is not None:
        return context_key(pre or "", post or "")
    return None


def _tmx_stamp_tu(tu, entry) -> None:
    """Emit provenance onto a <tu> from a SegmentTM row's meta.

    NO-OP when meta is empty -> byte-identical export for legacy/meta-less rows (the
    golden guarantee). Otherwise adds standard TMX 1.4b provenance attrs
    (creationtool / creationdate / usagecount) + one <prop type="x-..."> per recognized
    meta key. Props are appended BEFORE any <tuv> child (caller adds tuvs after) since
    the TMX DTD requires prop/note to precede tuv."""
    meta = entry.meta or None
    ck = getattr(entry, "context_key", None)
    if not meta:
        if ck:  # ADR 0102: a unit with only a context key still round-trips its 101 %
            etree.SubElement(tu, "prop", type=TMX_CONTEXT_KEY_PROP).text = ck
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
    if ck:
        etree.SubElement(tu, "prop", type=TMX_CONTEXT_KEY_PROP).text = ck


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


# --- exact lookup (ADR 0102) ---------------------------------------------------
# TM fill is exact-only: a unit matches a source when the normalized source hash is equal
# (100 %), and matches in context (101 %) when the unit's stored context_key also equals the
# query's. There is no fuzzy scoring anywhere: every read path funnels into _lookup_items, one
# chunked `source_hash IN (...)` query per batch through the _tm_read_scope seam.
_REMOVED_FUZZY = "fuzzy TM matching was removed (ADR 0102); TM fills exact matches only"

# Hashes per IN list (SQLite builds before 3.32 cap bound variables at 999).
_EXACT_IN_CHUNK = 500


def _check_min_score(min_score) -> None:
    """`min_score` survives only for callers that still pass it: None or >= 100 is the only
    meaning there is. Anything lower asked for a fuzzy match, which no longer exists."""
    if min_score is not None and min_score < 100:
        raise ValueError(_REMOVED_FUZZY)


def _as_context_key(ctx) -> str | None:
    """A context given as a ready key string, a (prev, next) pair of source texts, or None."""
    if ctx is None or ctx == "":
        return None
    if isinstance(ctx, str):
        return ctx
    prev_src, next_src = ctx
    return context_key(prev_src, next_src)


def _lookup_items(
    session: Session,
    project_id: int | None,
    items: list,
    source_locale: str,
    target_locale: str,
    restrict_project_ids: set | None = None,
) -> list:
    """Exact lookup of [(source_text, context_key_or_None)] -> positional list of hit dicts
    (None for a miss, a token-bearing or blank source). One IN query per _EXACT_IN_CHUNK
    distinct hashes. Among the rows of one hash: if the project has its own row it wins (101 when
    its key matches, or when an attached-source in-context row holds the same target; else 100);
    with no own row an in-context row wins and reports 101, else lowest id reports 100."""
    out: list = [None] * len(items)
    hash_of: dict[int, str] = {}
    for i, (src, _ctx) in enumerate(items):
        if is_tm_eligible(src):
            hash_of[i] = hash_text(src, source_locale, target_locale)
    if not hash_of:
        return out

    scope = _tm_read_scope(
        SegmentTM, project_id, session, restrict_project_ids=restrict_project_ids
    )
    hashes = list(dict.fromkeys(hash_of.values()))
    by_hash: dict[str, list] = {}
    cols = (
        SegmentTM.id,
        SegmentTM.source_hash,
        SegmentTM.source_text,
        SegmentTM.target_text,
        SegmentTM.context_key,
        SegmentTM.project_id,
    )
    for i in range(0, len(hashes), _EXACT_IN_CHUNK):
        # The hash probe is its own subquery on purpose. With the scope predicates in the same
        # WHERE, SQLite (no ANALYZE statistics) walked ix_segmenttm_content_type for the
        # `content_type_id IS NULL` leg and read the whole TM for every batch: 220 ms per batch of
        # 50 at 200,000 units, against 1 ms with the hash index driving. Same rows, same scope.
        hit_ids = select(SegmentTM.id).where(
            SegmentTM.source_hash.in_(hashes[i : i + _EXACT_IN_CHUNK]),
            SegmentTM.source_locale == source_locale,
            SegmentTM.target_locale == target_locale,
        )
        rows = session.exec(
            select(*cols)
            .where(
                SegmentTM.id.in_(hit_ids),
                scope,
            )
            # OWN PROJECT FIRST, then lowest id (see _own_first): this path auto-applies a
            # target without review, so a borrowed row must never shadow the project's own
            # curated one, and a reviewer's correction (a HIGHER-id own row) can still win
            # once the lower ids are gone. Rows arrive best-first per hash.
            .order_by(_own_first(SegmentTM, project_id), SegmentTM.id)
        ).all()
        for row in rows:
            by_hash.setdefault(row.source_hash, []).append(row)

    for i, h in hash_of.items():
        rows = by_hash.get(h)
        if not rows:
            continue
        want = items[i][1]
        best = rows[0]
        pct = 100
        if want:
            own = rows[0] if project_id is not None and rows[0].project_id == project_id else None
            if own is not None:
                # The project's own row is never shadowed by a borrowed (attached-source) row:
                # this path applies without review. It is 101 when its own key matches, or
                # when an in-context row from another source holds the SAME target text
                # (the borrowed row confirms it); a borrowed in-context row with a different
                # target leaves the own row at 100.
                if own.context_key == want or any(
                    r.context_key == want and r.target_text == own.target_text for r in rows
                ):
                    pct = 101
            else:
                for r in rows:
                    if r.context_key == want:
                        best, pct = r, 101
                        break
        out[i] = {
            "match_pct": pct,
            "source_text": best.source_text,
            "target_text": best.target_text,
            # `id` lets an APPLY site bump usage (tm_module.bump_usage).
            "id": best.id,
            "context_key": best.context_key,
        }
    return out


def lookup_segments(
    session: Session,
    project_id: int | None,
    items: list,
    source_locale: str,
    target_locale: str,
    *,
    restrict_project_ids: set | None = None,
) -> list:
    """Exact TM lookup for [(source_text, context_key_or_None)], positional: one hit dict or
    None per item, in the order given.

    The same source can occur twice in one document with different neighbours, so the result
    cannot be keyed by source text (lookup_batch is). Pass `context_key` strings from
    tm_context.segment_context_keys / tm_common.context_key; None means "no context", which can
    only ever give a 100 % hit."""
    return _lookup_items(
        session,
        project_id,
        [(s, _as_context_key(c)) for s, c in items],
        source_locale,
        target_locale,
        restrict_project_ids,
    )


def lookup(
    session: Session,
    project_id: int | None,
    source_text: str,
    source_locale: str,
    target_locale: str,
    min_score: int | None = None,
    restrict_project_ids: set | None = None,
    context=None,
) -> dict | None:
    """Exact TM match for one source, or None. A thin wrapper over the batch path.

    `min_score` is accepted for old callers: None or >= 100 only (ValueError below 100, the
    fuzzy engine is gone; ADR 0102). `restrict_project_ids` (Batch 15 T1, SPEC-tm-match-step-
    config section 7) narrows the readable project-leg to the intersection with that set (a
    per-step tm_match TM pool selection). `context` is a context-key string or a (prev, next)
    pair; an equal stored context_key makes the hit 101 instead of 100. Token-bearing sources
    are excluded from TM (see is_tm_eligible)."""
    _check_min_score(min_score)
    return _lookup_items(
        session,
        project_id,
        [(source_text, _as_context_key(context))],
        source_locale,
        target_locale,
        restrict_project_ids,
    )[0]


def lookup_batch(
    session: Session,
    project_id: int | None,
    sources: list[str],
    source_locale: str,
    target_locale: str,
    min_score: int | None = None,
    restrict_project_ids: set | None = None,
    *,
    contexts: dict | None = None,
) -> dict:
    """Exact TM lookup for a whole list of sources in ONE chunked `source_hash IN (...)` query.

    Returns {source_text: hit_dict_or_None}, one entry per distinct input string (duplicates
    share one lookup). `contexts` maps a source text to its context: a (prev_source,
    next_source) tuple or a ready context-key string (string-key projects); a source with an
    equal stored context_key reports match_pct 101, every other hit 100. The same source text
    twice in one document with different neighbours needs lookup_segments (this result is keyed
    by source). `min_score` is kept for old callers: None or >= 100 (ValueError below 100;
    fuzzy matching was removed, ADR 0102)."""
    _check_min_score(min_score)
    uniq = list(dict.fromkeys(sources))
    if not uniq:
        return {}
    ctx = contexts or {}
    hits = _lookup_items(
        session,
        project_id,
        [(s, _as_context_key(ctx.get(s))) for s in uniq],
        source_locale,
        target_locale,
        restrict_project_ids,
    )
    return dict(zip(uniq, hits, strict=True))


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
