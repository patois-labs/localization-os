"""The leverage report (ADR 0102): 101 % / repetitions / 100 % / new, in words, per target language.

ONE seam for every surface (project page, job page, GET /api/projects/{id}/leverage, GET
/api/jobs/{id}/leverage, `project leverage`, `job leverage`, the MCP `list_projects` /
`job_status` views, quotes). `analyze` takes a scope (a whole project, or the documents and
target locales one Job covered) and returns four bands per language, in source order:

  context      an in-context TM match, or a segment already filled or locked as 101 / ICE
  repetitions  not context, and the same normalized source occurred earlier in the scope for
               that language (document order, then order_index)
  exact        an exact TM match, or a segment already filled or locked from TM at 100
  new          everything else (MT, human, old 75-99 fuzzy fills)

Precedence is 101 > repetitions > 100 > new. A segment that is filled (non-empty target) keeps the
band it was filled with (read from its persisted tm_match_pct / lock_band); a segment not yet
filled is looked up live against the TM in scope (one positional `tm_hub.lookup_segments` call per
language across all documents, with the segment's context key), so an analysis before translation is
a quote and an analysis after it reports what was actually leveraged. Words use the vendor-cost word
counter. Key-based content is included: a key segment gets 101 from its string key.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import io

from sqlmodel import Session, select

from app import output_csv
from app.models import Document, Job, Project, Segment, StringKey
from app.services import tm_common, tm_context
from app.services.tm_hub import lookup_segments

# The four bands in report order (ADR 0102: 101 % > repetitions > 100 % > new), their labels, and
# the word counter. This module owns them so leverage works with the vendors module off;
# vendor_cost re-exports them as the rate-card vocabulary.
BANDS: tuple[str, ...] = ("context", "repetitions", "exact", "new")
BAND_LABELS: dict[str, str] = {
    "context": "101 % in-context",
    "repetitions": "Repetitions",
    "exact": "100 % exact",
    "new": "New",
}

# The Job kinds whose payload names a document (and a target language). Every other kind
# (connector runs, embedding backfills, exports, TM imports) has no documents to analyze.
_DOCUMENT_JOB_KINDS = ("translate_document",)

_CONTEXT_LOCKS = frozenset({"ice", "101", "context"})
_CHUNK = 150  # stay under SQLite's IN() expression-tree limit on big projects


class LeverageError(ValueError):
    """The scope cannot be analyzed. `status` is the HTTP code a door answers with: 400 for a
    job that covers no documents, 404 for a missing project or document."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def word_count(text: str) -> int:
    """Whitespace-tokenized word count, the deterministic offline counter per-word pricing uses."""
    return len((text or "").split())


def normalize_source(text: str) -> str:
    """Whitespace-normalized source (tm_common.normalize): the repetition key."""
    return tm_common.normalize(text)


def _zero() -> dict:
    return {b: {"segments": 0, "words": 0} for b in BANDS}


def filled_band(tm_match_pct, lock_band) -> str:
    """The band a FILLED segment keeps, from its persisted fields (ADR 0102): pct >= 101 or a
    ice/101/context lock -> context; pct == 100 or any other non-fuzzy lock -> exact; anything
    else (old 75-99 stamps, a 'fuzzy' lock, MT or human text with no stamp) -> new."""
    lock = str(lock_band or "").strip().lower()
    try:
        pct = int(tm_match_pct) if tm_match_pct is not None else None
    except (TypeError, ValueError):
        pct = None
    if lock in _CONTEXT_LOCKS or (pct is not None and pct >= 101):
        return "context"
    if pct == 100 or (lock and lock != "fuzzy"):
        return "exact"
    return "new"


def job_scope(session: Session, job: Job) -> dict:
    """{project_id, document_ids, target_locales} one Job covered, or LeverageError.

    A translate_document job covers its payload document and locale. A job of any other kind
    covers no documents (a clear 400, not an empty report)."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    if job.kind not in _DOCUMENT_JOB_KINDS or payload.get("document_id") is None:
        raise LeverageError(
            f"job {job.id} ({job.kind}) covers no documents, so it has no leverage report.", 400
        )
    try:
        doc = session.get(Document, int(payload["document_id"]))
    except (TypeError, ValueError):
        doc = None
    if doc is None:
        raise LeverageError("the document this job covered no longer exists.", 404)
    locales = []
    raw = payload.get("locales")
    if raw is None:
        raw = payload.get("locale")
    if isinstance(raw, str):
        raw = [raw]
    for loc in raw or []:
        loc = str(loc or "").strip()
        if loc and loc not in locales:
            locales.append(loc)
    return {"project_id": doc.project_id, "document_ids": [doc.id], "target_locales": locales}


def _load_segments(session: Session, document_ids: list[int], target_locales) -> list:
    """Column-only rows (no JSON blobs) for the scope, minus the rows of a locale still being
    added and minus soft-deleted keys."""
    from app.services import locale_seed

    rows: list = []
    for i in range(0, len(document_ids), _CHUNK):
        batch = document_ids[i : i + _CHUNK]
        stmt = select(
            Segment.id,
            Segment.document_id,
            Segment.target_locale,
            Segment.order_index,
            Segment.source_text,
            Segment.target_text,
            Segment.tm_match_pct,
            Segment.lock_band,
            Segment.key_id,
        ).where(Segment.document_id.in_(batch))
        if target_locales:
            stmt = stmt.where(Segment.target_locale.in_(list(target_locales)))
        rows.extend(session.exec(stmt).all())
    held = set(locale_seed.seeding_pairs(session, document_ids))
    if held:
        rows = [r for r in rows if (r.document_id, r.target_locale) not in held]
    key_ids = {r.key_id for r in rows if r.key_id is not None}
    if key_ids:
        dead: set = set()
        ids = sorted(key_ids)
        for i in range(0, len(ids), 500):
            dead.update(
                session.exec(
                    select(StringKey.id).where(
                        StringKey.id.in_(ids[i : i + 500]), StringKey.deleted_at.is_not(None)
                    )
                ).all()
            )
        if dead:
            rows = [r for r in rows if r.key_id not in dead]
    return rows


def analyze(
    session: Session,
    project_id: int | None,
    *,
    job_id: int | None = None,
    document_ids=None,
    target_locales=None,
    segments=None,
) -> dict:
    """The leverage report for a scope.

    Scope: the whole project (every document, key-based content included); `document_ids` /
    `target_locales` narrow it; `job_id` takes the documents and languages that Job covered
    (project_id may then be None, or must match). `segments` is a pre-loaded list of the scope's
    segment rows (attributes id, document_id, target_locale, order_index, source_text,
    target_text, tm_match_pct, lock_band, key_id) for a caller that already has them.

    Returns {project_id, job_id, scope, source_locale, bands, labels, languages: [{locale, bands:
    {band: {segments, words}}, segments, words}], totals: {bands, segments, words}}, bands in the
    order context, repetitions, exact, new. Raises LeverageError."""
    if job_id is not None:
        job = session.get(Job, int(job_id))
        if job is None:
            raise LeverageError(f"no such job: {job_id}", 404)
        sc = job_scope(session, job)
        if project_id is not None and sc["project_id"] != project_id:
            raise LeverageError(f"no such job: {job_id}", 404)
        project_id = sc["project_id"]
        document_ids = sc["document_ids"]
        target_locales = sc["target_locales"] or target_locales
    project = session.get(Project, project_id) if project_id is not None else None
    if project is None:
        raise LeverageError(f"no such project: {project_id}", 404)

    if document_ids is None:
        doc_ids = list(
            session.exec(
                select(Document.id).where(Document.project_id == project.id).order_by(Document.id)
            ).all()
        )
    else:
        doc_ids = sorted({int(d) for d in document_ids})
    locs = [str(x) for x in (target_locales or []) if str(x or "").strip()]
    rows = segments if segments is not None else _load_segments(session, doc_ids, locs)
    if segments is not None and locs:
        rows = [r for r in rows if r.target_locale in locs]

    # (language -> document -> segments in order_index order), documents in id order.
    by_doc: dict = {}
    for r in rows:
        by_doc.setdefault((r.target_locale, r.document_id), []).append(r)
    locales = sorted({k[0] for k in by_doc})
    per_locale = {loc: _zero() for loc in locales}
    # Every string key in scope resolved in one read (not one per document and language).
    keyed = tm_context.key_context_keys(session, (r.key_id for r in rows))
    # The TM read scope (own project plus attached sources) resolves once for the whole
    # report, and each language is ONE positional lookup across all its documents.
    with tm_common.source_ids_memo(session):
        _fill_bands(session, project, by_doc, locales, per_locale, keyed)
    return _report(project, job_id, locales, per_locale)


def _fill_bands(session, project, by_doc, locales, per_locale, keyed) -> None:
    ctx_memo: dict = {}
    norm_memo: dict = {}
    for loc in locales:
        seen: set = set()
        docs = []  # [(segs in order, unfilled indexes, offset in the language batch)]
        items: list = []
        for doc_id in sorted(d for (lc, d) in by_doc if lc == loc):
            segs = sorted(by_doc[(loc, doc_id)], key=lambda r: (r.order_index, r.id))
            # Context keys need the WHOLE document's neighbours; the live lookup only the
            # segments that are not yet filled.
            # The same document in another language has the same sources in the same order,
            # so its keys are computed once.
            sig = (doc_id, tuple((r.source_text, r.key_id) for r in segs))
            ctx_keys = ctx_memo.get(sig)
            if ctx_keys is None:
                ctx_keys = ctx_memo[sig] = tm_context.segment_context_keys(session, segs, keyed)
            todo = [i for i, r in enumerate(segs) if not r.target_text]
            docs.append((segs, todo, len(items)))
            items.extend((segs[i].source_text, ctx_keys[i]) for i in todo)
        hits = (
            lookup_segments(session, project.id, items, project.source_locale, loc) if items else []
        )
        for segs, todo, offset in docs:
            live = {i: hits[offset + n] for n, i in enumerate(todo)}
            cells = per_locale[loc]
            for i, r in enumerate(segs):
                if r.target_text:
                    band = filled_band(r.tm_match_pct, r.lock_band)
                else:
                    hit = live.get(i)
                    pct = int(hit["match_pct"]) if hit else 0
                    band = "context" if pct >= 101 else ("exact" if pct == 100 else "new")
                key = norm_memo.get(r.source_text)
                if key is None:
                    key = norm_memo[r.source_text] = tm_common.normalize(r.source_text)
                if band != "context" and key and key in seen:
                    band = "repetitions"
                if key:
                    seen.add(key)
                cell = cells[band]
                cell["segments"] += 1
                cell["words"] += word_count(r.source_text)


def _report(project, job_id, locales, per_locale) -> dict:
    languages = []
    totals = _zero()
    for loc in locales:
        cells = per_locale[loc]
        for b in BANDS:
            totals[b]["segments"] += cells[b]["segments"]
            totals[b]["words"] += cells[b]["words"]
        languages.append(
            {
                "locale": loc,
                "bands": cells,
                "segments": sum(c["segments"] for c in cells.values()),
                "words": sum(c["words"] for c in cells.values()),
            }
        )
    return {
        "project_id": project.id,
        "job_id": int(job_id) if job_id is not None else None,
        "scope": "job" if job_id is not None else "project",
        "source_locale": project.source_locale,
        "bands": list(BANDS),
        "labels": {b: BAND_LABELS[b] for b in BANDS},
        "languages": languages,
        "totals": {
            "bands": totals,
            "segments": sum(c["segments"] for c in totals.values()),
            "words": sum(c["words"] for c in totals.values()),
        },
    }


def to_rows(report: dict) -> list[dict]:
    """Flat rows for a CSV or a table: one per (language, band) in band order, then a total row
    per language, then the overall rows. {scope, language, band, label, segments, words}."""
    scope = (
        f"job {report['job_id']}"
        if report.get("job_id") is not None
        else f"project {report['project_id']}"
    )
    out: list[dict] = []

    def _add(language, bands, total):
        for b in BANDS:
            cell = bands.get(b) or {}
            out.append(
                {
                    "scope": scope,
                    "language": language,
                    "band": b,
                    "label": BAND_LABELS[b],
                    "segments": int(cell.get("segments") or 0),
                    "words": int(cell.get("words") or 0),
                }
            )
        out.append(
            {
                "scope": scope,
                "language": language,
                "band": "total",
                "label": "Total",
                "segments": int(total["segments"]),
                "words": int(total["words"]),
            }
        )

    for lang in report.get("languages", []):
        _add(lang["locale"], lang["bands"], lang)
    totals = report.get("totals") or {}
    _add("all", totals.get("bands") or _zero(), totals or {"segments": 0, "words": 0})
    return out


CSV_COLUMNS = ("scope", "language", "band", "segments", "words")


def to_csv(report: dict) -> str:
    """The report as CSV for quoting: columns scope, language, band, segments, words; a `total`
    band row closes each language and an `all` language closes the file. LF line endings."""
    buf = io.StringIO()
    # output_csv is the one CSV writer (verify_export_output_escaping): it puts every cell through
    # csv_safe_cell, because a locale code is user-supplied text, and a leading = + - @ would run
    # as a formula when the file is opened in a spreadsheet.
    w = output_csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in to_rows(report):
        w.writerow([r[c] for c in CSV_COLUMNS])
    return buf.getvalue()


def table_text(report: dict) -> list[tuple]:
    """(language, band label, segments, words) rows for a terminal table, ASCII only."""
    return [(r["language"], r["label"], r["segments"], r["words"]) for r in to_rows(report)]
