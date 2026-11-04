"""Translation Memory — two parallel stores.

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports XML_LANG,
_META_PROP_MAP, _PROP_META_MAP, _META_VALUE_CAP,
_like_escape, _sanitize_meta_value, _provenance_meta, _meta_from_tu, _tmx_stamp_tu,
_build_export_tmx, lookup, lookup_batch, lookup_segments, concordance from app.services.tm_hub.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _content_type_name, _project_content_type_id,
_tm_source_project_ids, _tm_scope, _own_first, _own_first_key, _content_type_scope, _tm_read_scope,
normalize, is_tm_eligible, hash_text, _lang_match, _pick, LocaleMismatchError,
resolve_import_locales, _project_targets, _pair_has_rows, resolve_export_locales, LEVERAGE_BANDS,
leverage_bands, count, _source_fallback_blocked, ft_count, _export_entries, ft_lookup, ft_store from
app.services.tm_common,
app.services.tm_part2.

Segment TM (the traditional kind):
  - import/export TMX (the industry interchange format)
  - exact lookup only (ADR 0102): 100% exact and 101% in-context matches; fuzzy matching
    was removed

Full-Text TM (the novel kind):
  - key a whole-document translation by a hash of its normalized source
  - re-translating the same document returns from memory instead of calling a
    provider (wired into the orchestrator in Chunk 4)

Documented in: docs/architecture/tm-and-glossary.md
"""

import hashlib
import io
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import sqlalchemy as sa
from lxml import etree
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import branding
from app.models import FullTextTM, SegmentEmbedding, SegmentTM
from app.services import import_lock
from app.services.tm_common import (
    LEVERAGE_BANDS,
    TM_SKIP_PLACEHOLDER,
    LocaleMismatchError,
    _content_type_name,
    _content_type_scope,
    _lang_match,
    _own_first,
    _own_first_key,
    _pair_has_rows,
    _pick,
    _project_content_type_id,
    _project_targets,
    _source_fallback_blocked,
    _tm_read_scope,
    _tm_scope,
    _tm_source_project_ids,
    context_key,
    count,
    ft_count,
    hash_text,
    integrity_skip,
    is_tm_eligible,
    leverage_bands,
    normalize,
    resolve_export_locales,
    resolve_import_locales,
)
from app.services.tm_hub import (
    _META_PROP_MAP,
    _META_VALUE_CAP,
    _PROP_META_MAP,
    TMX_CONTEXT_KEY_PROP,
    XML_LANG,
    _build_export_tmx,
    _context_from_tu,
    _like_escape,
    _meta_from_tu,
    _provenance_meta,
    _sanitize_meta_value,
    _tmx_stamp_tu,
    concordance,
    lookup,
    lookup_batch,
    lookup_segments,
)
from app.services.tm_part2 import _export_entries, ft_lookup, ft_store
from app.services.tm_sdltm import SdltmError

log = logging.getLogger(__name__)
# Bound once: suites swap `tm.time` for a stub that only has sleep() (verify_import_chunks_615).
_monotonic = time.monotonic

__all__ = [
    "LEVERAGE_BANDS",
    "TMX_CONTEXT_KEY_PROP",
    "XML_LANG",
    "_META_PROP_MAP",
    "_META_VALUE_CAP",
    "_PROP_META_MAP",
    "FullTextTM",
    "LocaleMismatchError",
    "SdltmError",
    "SegmentEmbedding",
    "_build_export_tmx",
    "_content_type_name",
    "_content_type_scope",
    "_export_entries",
    "_lang_match",
    "_like_escape",
    "_meta_from_tu",
    "_own_first",
    "_own_first_key",
    "_pair_has_rows",
    "_pick",
    "_project_content_type_id",
    "_project_targets",
    "_provenance_meta",
    "_sanitize_meta_value",
    "_source_fallback_blocked",
    "_tm_read_scope",
    "_tm_scope",
    "_tm_source_project_ids",
    "_tmx_stamp_tu",
    "branding",
    "concordance",
    "count",
    "etree",
    "example_matches_hybrid",
    "ft_count",
    "ft_lookup",
    "ft_store",
    "func",
    "hash_text",
    "hashlib",
    "is_tm_eligible",
    "leverage_bands",
    "lookup",
    "lookup_batch",
    "lookup_segments",
    "math",
    "normalize",
    "resolve_export_locales",
    "resolve_import_locales",
]


def examples_available() -> bool:
    """True when an add-on registered a TM example retriever ("tm_examples"). The core ships
    none (ADR 0102), so surfaces that offer TM few-shot examples hide or label them when this
    is False."""
    from app import extensions

    return extensions.provider("tm_examples") is not None


def example_matches_hybrid(session, project_id, sources, *args, **kwargs) -> dict:
    """TM few-shot examples for a prompt: {source_text: [pairs]} (ADR 0102). The core fills
    exact matches only and ships no example retriever (fuzzy example scoring was removed with
    the fuzzy TM engine). An add-on may register one as the "tm_examples" extension; without
    it every source maps to an empty list, so prompts carry no TM examples."""
    from app import extensions

    retriever = extensions.provider("tm_examples")
    if retriever is not None:
        return retriever.example_matches_hybrid(session, project_id, sources, *args, **kwargs)
    return {s: [] for s in dict.fromkeys(sources)}


# --- Segment TM ----------------------------------------------------------------
# A TMX declaring XML entities is refused before a byte is parsed. This is the SAME
# guard (and the same NUL-stripped second pass) `glossary_tbx._guard` applies to TBX,
# and it is REQUIRED here now that import_tmx streams with ElementTree rather than
# lxml: libxml2 caps entity amplification at a factor of ~10 and raised
# XMLSyntaxError on a 94x file, while expat's default ceiling is 100x AND is not
# enforced at all below an 8 MiB expansion -- measured, a 196 KB TMX expanded to an
# 18 MB source_text and was written to the TM. At the 100 MiB upload cap that is up
# to ~10 GB of expanded text on a small Fly machine. No real TMX declares entities.
#
# The NUL-stripped second pass matters because expat auto-detects UTF-16/32 from the
# BOM and expands the entities identically, while those encodings interleave NUL
# bytes so the raw payload never spells "<!ENTITY". NUL is not a legal XML character,
# so stripping it can never hide a legitimate file's content.
_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)

# How many processed <tu>s import_tmx buffers before pushing them to the DB
# (session.flush()) and dropping every ORM object it holds (expunge + no Python
# ref), INSIDE the import's SAVEPOINT so atomicity is unchanged.
#
# WHY (2026-07-27, paired with the 25 -> 100 MiB upload-cap raise): streaming the
# XML bounded the PARSER's memory but not the SESSION's. Every <tu> left a live
# SegmentTM in the identity map + the flush plan until one final commit, so a
# dense file (many small units) cost ~5 KB of Python heap per unit regardless of
# how small the unit was. MEASURED on a 28.3 MB / 150k-unit TMX: 732 MB peak
# traced heap (25.9x the file) -- an OOM on the 1 GB Fly machine well inside the
# 100 MiB cap.
#
# 500 is chosen off a measured sweep (50k units / 9.4 MB, N in 50..5000), not a
# guess. Wall clock is FLAT across the whole range (39.7s -> 41.1s; the cost is
# per-row hashing + INSERT, not per-batch overhead), while peak heap rises ~4.6 KB
# per buffered unit: 17.2 MB at N=50, 18.8 MB at N=500, 40.8 MB at N=5000. So the
# only thing N buys is a wider executemany, and the only thing it costs is the
# transient batch. 500 keeps that batch ~2 MB -- far below the (unavoidable)
# in-file dedupe map -- while staying wide enough that a round-trip-bound backend
# (the Postgres CI job / a future non-local DB) is not doing 500-statement chatter.
TMX_IMPORT_FLUSH_BATCH = 500
# Rows per executemany in the (lock-holding) write phase; bigger chunks cut per-statement
# overhead while the write lock is held (#615).
TMX_WRITE_BATCH = 5000
# #615: the most rows ONE write transaction of a TMX import stores (new inserts, target updates
# or activity touches). The SQLite write lock is held for one chunk at a time (about 40 us per
# row, so roughly 0.1 s at the default) and released between chunks, instead of for the whole
# file (4 s at 100k units, 12 s at 300k). An import of at most this many rows is one
# transaction, exactly as atomic as before; see the PHASE 2 note in `_import_tmx_attempt`.
TMX_WRITE_CHUNK_ROWS = 2500
# Seconds the importer waits between two chunks with the write lock FREE, so a writer polling
# SQLite's busy handler (sleeps of up to 100 ms) reliably gets in between chunks.
TMX_CHUNK_PAUSE_S = 0.05
# ADR 0105: how many times a chunk retries a compare-and-set UPDATE that matched nothing because
# another writer changed the row again, and how many times it re-inserts units that a concurrent
# writer stored and deleted again, before it stops and counts them (skipped / concurrent_deleted).
TMX_CAS_ROUNDS = 3
TMX_INSERT_ROUNDS = 3


class TmxImportIncomplete(ValueError):
    """A multi-chunk TMX import failed after at least one chunk was committed (#615).

    The committed rows stay (a ValueError so every surface shows the message as a refusal);
    `rows_written` of `rows_total` are in the TM. Importing the same file again completes it
    without duplicates."""

    def __init__(self, message: str, rows_written: int = 0, rows_total: int = 0):
        super().__init__(message)
        self.rows_written = rows_written
        self.rows_total = rows_total


def cached_count(session: Session, project_id: int | None) -> int:
    """`count` served from the process-local cache (S1 item 6): for per-request surfaces."""
    from app.services import tm_counts

    return tm_counts.count(session, project_id)


def cached_ft_count(session: Session, project_id: int | None) -> int:
    """`ft_count` served from the process-local cache (S1 item 6)."""
    from app.services import tm_counts

    return tm_counts.ft_count(session, project_id)


# Issue #567: how many withheld units an import reports row by row (the counts are exact
# for every unit; only the row list is bounded, so a hostile file cannot grow the reply).
TMX_IMPORT_REPORT_ROWS = 20


def import_tmx(
    session: Session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    progress=None,
) -> dict:
    """Import a TMX file into the project's segment TM (the full contract is on
    `_import_tmx_attempt`). Issue #584: all-or-nothing, parsed and validated in full
    BEFORE the first write, then written in short chunk transactions (#615: at most
    TMX_WRITE_CHUNK_ROWS rows each, so no writer waits for the whole file). `progress`, when
    given, is called as progress(rows_written, rows_total) after every committed chunk. A
    failure after a chunk committed raises TmxImportIncomplete; re-importing the same file
    finishes it without duplicates.

    A Trados .sdltm (a SQLite database, recognized by its header) is read by
    `tm_sdltm.import_sdltm`, which turns its units into a TMX and comes back here, so every
    TM import surface (web, API, CLI, MCP) takes both formats through one gate.

    Batch 6: a two-column spreadsheet TM (XLSX, CSV or TSV: source then target, or a
    header naming source/target or the two locale codes) is accepted too. Anything else
    that is not XML (and not an .sdltm) is converted to a TMX body first
    (tabular_import.tm_tmx), so it passes every check below exactly like a TMX upload."""
    from app.services import tm_counts

    try:
        return _import_tmx_dispatch(
            session,
            project_id,
            data,
            source_locale,
            target_locale,
            acknowledge_placeholder_loss=acknowledge_placeholder_loss,
            progress=progress,
        )
    finally:
        # S1 item 6: chunks may have committed even when the import fails part-way.
        tm_counts.invalidate(project_id)


def import_tmx_file(
    session: Session,
    project_id: int | None,
    path,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    progress=None,
    parse_progress=None,
) -> dict:
    """`import_tmx` for a file ON DISK (S6, ADR 0094): the background TM import job and the
    host CLI. A TMX is never read into memory: it is pre-scanned and parsed straight from the
    file under TMX_IMPORT_MAX_BYTES / TMX_IMPORT_MAX_UNITS, and the write plan is spooled to
    disk, so memory stays flat whatever the file size. A Trados .sdltm or a spreadsheet TM is
    converted in memory as before, so it keeps the MAX_UPLOAD_BYTES cap.

    Same reply, rules and failures as `import_tmx`. `parse_progress`, when given, is called as
    parse_progress(bytes_read, bytes_total, units_seen) about once a second while the file is
    parsed; `progress(rows_written, rows_total)` after every committed write chunk."""
    from app import config
    from app.services import tabular_import, tm_counts, tm_sdltm

    with open(path, "rb") as fh:
        head = fh.read(64 * 1024)
    try:
        if tm_sdltm.is_sdltm(head) or not tabular_import.looks_like_tmx(head):
            size = os.path.getsize(path)
            if size > config.MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"this file is {size} bytes; only a TMX may be larger than the "
                    f"{config.MAX_UPLOAD_BYTES}-byte upload limit (SDLTM and spreadsheet "
                    "TMs are converted in memory)."
                )
            with open(path, "rb") as fh:
                data = fh.read()
            return _import_tmx_dispatch(
                session,
                project_id,
                data,
                source_locale,
                target_locale,
                acknowledge_placeholder_loss=acknowledge_placeholder_loss,
                progress=progress,
            )
        return _import_tmx_attempt(
            session,
            project_id,
            None,
            source_locale,
            target_locale,
            acknowledge_placeholder_loss=acknowledge_placeholder_loss,
            progress=progress,
            path=path,
            parse_progress=parse_progress,
        )
    finally:
        # S1 item 6: chunks may have committed even when the import fails part-way.
        tm_counts.invalidate(project_id)


def _import_tmx_dispatch(
    session: Session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    progress=None,
) -> dict:
    from app.services import tabular_import, tm_sdltm

    if tm_sdltm.is_sdltm(data):
        return tm_sdltm.import_sdltm(
            session,
            project_id,
            data,
            source_locale,
            target_locale,
            acknowledge_placeholder_loss=acknowledge_placeholder_loss,
            progress=progress,
        )
    if not tabular_import.looks_like_tmx(data):
        data = tabular_import.tm_tmx(data, None, source_locale, target_locale)
    return _import_tmx_attempt(
        session,
        project_id,
        data,
        source_locale,
        target_locale,
        acknowledge_placeholder_loss=acknowledge_placeholder_loss,
        progress=progress,
    )


def _import_tmx_attempt(
    session: Session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    progress=None,
    path=None,
    parse_progress=None,
) -> dict:
    """Import a TMX file's translation units into the project's segment TM.

    Idempotent (bug #5): re-importing the SAME TMX (or one that overlaps an
    earlier import) no longer duplicates SegmentTM rows. Each unit is keyed by
    the same (project_id, source_locale, target_locale, source_hash) scope the
    lookup path already uses (see hash_text/lookup) -- a unit whose hash already
    exists in that scope UPDATES the existing row's target_text (in case the TMX
    carries a revised translation) instead of inserting a duplicate.

    LOCALES (QA BUG-03, 2026-07-28): `source_locale`/`target_locale` are reconciled
    against the PROJECT's locales before anything is written — coerced when they name
    the same languages in a different region/case, refused when they name a pair the
    project does not translate (see resolve_import_locales). The EFFECTIVE locales are
    returned as "source_locale"/"target_locale" and "locales_normalized" says whether
    a coercion happened, so no surface reports a pair it did not actually store under.

    Returns {"imported": n, "skipped": n, "updated": n, "skipped_refusal": n,
    "source_locale": s, "target_locale": t, "locales_normalized": bool}:
      - imported: brand-new SegmentTM rows inserted
      - updated:  existing rows whose target_text changed on re-import
      - skipped:  existing rows that already matched (true no-op re-import)
      - concurrent_deleted (#615): existing rows the file names that another writer DELETED
        while this import ran (between its read and the row's write chunk); they are not
        brought back and not counted in updated/skipped (re-importing the file stores them)
      - write_chunks (#615): how many short write transactions the import took
      - skipped_refusal: units whose TARGET reads as a refusal / meta-assistant hedge
        (refusal.is_refusal) and were NOT imported/stored at all. RATIONALE
        (SPEC-refusal-choke-point Deliverable 3): MT-exported TMX files carry the same
        hedges the live guard catches, and a stored TM target becomes a 100%%-match
        that BYPASSES the live guard on every later document forever — so a hedge must
        be stopped at IMPORT, not laundered into the TM. Kill-switch aware
        (config.REFUSAL_GUARD_ENABLED via quality.is_refusal_check): OFF -> 0, every
        unit imported exactly as before this gate existed. Surfaced on both the web and
        CLI import flows (parity).
      - skipped_integrity (Issue #567): units withheld by the SAME integrity gate segment
        approve applies (#562): `tagged_source` / `tag_integrity` (inline tokens; never
        acknowledgeable) and `placeholder_integrity` (a dropped or added {0}, %s, {{name}};
        imported anyway only when `acknowledge_placeholder_loss=True`). The import keeps
        going; the report is `skipped_by_code` ({code: n}) plus `skipped_rows`, the first
        TMX_IMPORT_REPORT_ROWS withheld units as {row, code, reason, source}. An existing
        row is never updated to a withheld target. A placeholder loss that WAS acknowledged
        is counted in `acknowledged_placeholder_loss`.
    S6 (ADR 0094): `path` (instead of `data`) imports a TMX from disk without reading it into
    memory (TMX_IMPORT_MAX_BYTES / TMX_IMPORT_MAX_UNITS instead of MAX_UPLOAD_BYTES /
    MAX_DOCUMENT_ROWS), and the write plan the notes below describe as in-memory maps and
    tuples now lives in a throwaway SQLite file (tm_import_stream.ImportSpool) with the same
    counting rules, so memory no longer grows with the file. `parse_progress(bytes_read,
    bytes_total, units_seen)` is called about once a second while the file is parsed.

    `imported` is also the count new callers should treat as "the number of TM
    entries now usable" for a fresh import; existing callers that only cared
    about a plain count can sum imported+updated for "units processed".

    STREAMING (2026-07-27, upload-cap raise 25->100 MiB): a full DOM parse
    (`lxml.etree.fromstring`) of a 100 MiB TMX is a ~1 GB peak-memory spike on
    the small Fly machine, so this walks the file with stdlib `ET.iterparse`
    instead -- one <tu> materialized at a time, cleared as soon as it is
    processed (same clear-as-you-go pattern as `glossary_tbx.parse_rows`, which
    already streams TBX). Behavior preserved exactly:
      * only <tu> elements that are DIRECT children of a <body> that is itself
        a DIRECT child of the document root are processed -- the same reach
        `root.find("body")` + `body.findall("tu")` had (plain, unprefixed tag
        match; a namespaced TMX finds nothing, same as before).
      * the DB is untouched (no ct_id/existing_by_hash query, no commit) when no
        such <body> is found, exactly like the old `body is None` short-circuit
        -- the content-type + pre-load queries are deferred until the FIRST
        qualifying <tu> is actually seen, and never run at all if none is.
      * a malformed document still raises before any row is committed, and the
        rows this call speculatively wrote are UNDONE before the exception
        propagates (see BOUNDED MEMORY below) so the caller's session is left
        exactly as it was found -- the same guarantee the old parse-then-write
        order gave for free. Without that undo the writes survive in the session
        and SQLAlchemy autoflushes them on the caller's next query, so any later
        commit on that session persists a PARTIAL import (reproduced 2026-07-27;
        no shipped caller reuses the session, but the contract must not depend
        on that).

    BOUNDED MEMORY (2026-07-27, second pass): streaming the XML bounded the
    PARSER but not the SESSION -- every <tu> left a live SegmentTM in the
    identity map and the flush plan until the single final commit. Measured on a
    28.3 MB / 150k-unit TMX: 732 MB peak traced heap (25.9x the file), i.e. an
    OOM on the 1 GB Fly machine at a size the 100 MiB cap admits. Now:
      * every TMX_IMPORT_FLUSH_BATCH processed units the pending INSERTs are
        flushed and then EXPUNGED (and every Python ref dropped), and the batch's
        UPDATEs go out as one ORM bulk-update-by-primary-key executemany, so the
        identity map, the dirty set and this function's own bookkeeping all stay
        O(batch) instead of O(file).
      * the idempotency map holds PRIMITIVES (source_hash -> (row id, target
        text)), never ORM instances -- pre-loading 150k SegmentTM objects was a
        348 MB peak on its own. Re-importing the same 28.3 MB file now peaks at
        78 MB (2.8x): the map is the floor, and it is inherent to dedupe.
      * all of it runs inside a SAVEPOINT (session.begin_nested()) opened at the
        first qualifying <tu>. ANY failure rolls back to it, which discards the
        already-flushed rows too -- so the all-or-nothing contract above now
        holds for flushed state, which a per-object undo could not do. The
        caller's outer transaction survives the rollback and stays usable, for
        BOTH failure shapes: an XML ParseError (the savepoint is still ACTIVE
        when the except block runs) and a DB-level failure inside session.flush()
        (SQLAlchemy has already DEACTIVATED the savepoint, so savepoint.rollback()
        is recovering, not merely undoing). Both are pinned in
        verify_tm_import_bounded_memory.py.
      * caller-owned pending objects on the same session are unaffected:
        SessionTransaction._take_snapshot flushes them when the SAVEPOINT is
        opened (autoflush on our first SELECT did the same before this change),
        so they sit in the OUTER transaction, outside the savepoint, and survive
        its rollback -- still uncommitted either way, and the final commit here
        commits them exactly as it always did.
      * PYSQLITE CAVEAT on the SUCCESS path (measured 2026-07-27; net effect is
        unchanged from the old code, so it is documented, not fixed): pysqlite
        emits BEGIN only before DML, never before SAVEPOINT, and app/db.py ships
        no do_begin workaround. So when import_tmx is called on a session holding
        NO pending DML -- the shape of every shipped caller (web/CLI/api all open
        a fresh session and only SELECT before this call) -- our SAVEPOINT is the
        OUTERMOST transaction and the `savepoint.commit()` RELEASE below IS the
        commit: the rows are durable one line BEFORE session.commit() runs, not
        after (verified: visible from an independent connection at that point).
        The two lines are adjacent, so this matches the old single-commit code in
        every practical sense; the one thing it costs is that a failure of
        session.commit() ITSELF now leaves the import persisted where the old
        code persisted nothing. The FAILURE path is unaffected either way --
        ROLLBACK TO SAVEPOINT undoes the flushed batches whether or not an outer
        transaction encloses it. When the caller DOES hold pending DML, pysqlite
        has already begun a real transaction and the savepoint nests properly.
      * SUPERSEDED BY ISSUE #584 (kept for the history; the maintainer chose atomic AND
        short-locking): the whole file is now parsed into compact tuples first and
        written in one short phase-2 transaction, so the write lock is held for the
        bulk write only. The original note follows.
        COST, NOT YET ADJUDICATED (measured 2026-07-27): interleaving writes with
        the parse means SQLite's single WRITER LOCK is held from the FIRST batch
        flush to the final commit -- for the WHOLE import -- where the old
        accumulate-then-commit shape took it only for the final flush. Measured
        on a 6k-unit file: an independent connection's write mid-import blocked
        for the full db.BUSY_TIMEOUT_MS and then failed "database is locked"; on
        the old shape the same write succeeded in 0.00s. The window scales with
        PARSE time (~30s at 40k units, minutes at the 100 MiB cap), so a large TM
        import can now stall every other writer in the process (orchestrator
        steps, spend recording, audit) for its whole duration -- the QA-1
        contention class db.BUSY_TIMEOUT_MS explicitly does NOT cover. On a
        single-writer DB the import can be ATOMIC, MEMORY-BOUNDED, or
        SHORT-LOCKING -- pick two; the alternatives are per-batch commits (drops
        all-or-nothing) or buffering primitives for the whole file (drops the
        memory bound). Needs a product call before this is treated as a pure win.

    GUARDS (also 2026-07-27): the payload is refused outright, before parsing,
    when it exceeds config.MAX_UPLOAD_BYTES or declares XML entities. Both mirror
    `glossary_tbx._guard` -- see `_ENTITY_DECL` above for why the ElementTree
    swap makes the entity refusal mandatory rather than optional, and note that
    the size check is the ONLY cap on the `/api/projects/{id}/tm/import` seam
    (it base64-decodes a JSON body with no bounded read) and on the CLI's
    local-file read."""
    from app import config
    from app.services import quality, seed_caps
    from app.services import tm_import_stream as tstream

    if path is not None:
        # S6 (ADR 0094): a TMX already on disk (the background import, the CLI) is never
        # read into memory. Its own, larger, still bounded size and unit caps apply.
        total_bytes = os.path.getsize(path)
        if total_bytes > config.TMX_IMPORT_MAX_BYTES:
            raise ValueError(
                f"the TMX file is {total_bytes} bytes, over the "
                f"{config.TMX_IMPORT_MAX_BYTES}-byte TMX import limit (TMX_IMPORT_MAX_BYTES)."
            )
        has_entity, _tus = tstream.scan_tmx_file(path)
        if has_entity:
            raise ValueError("this TMX file declares XML entities, which are not accepted.")

        def _check_units(n: int) -> None:
            if n > config.TMX_IMPORT_MAX_UNITS:
                raise seed_caps.InputLimitError(
                    f"This TMX file has more than {config.TMX_IMPORT_MAX_UNITS} translation "
                    "units (TMX_IMPORT_MAX_UNITS). Nothing was stored. Split the file, or ask "
                    "the operator to raise the limit."
                )

        def _open():
            return open(path, "rb")  # closed in the finally below
    else:
        total_bytes = len(data)
        if total_bytes > config.MAX_UPLOAD_BYTES:
            raise ValueError(
                f"the TMX file is {total_bytes} bytes, over the "
                f"{config.MAX_UPLOAD_BYTES}-byte upload limit."
            )
        if _ENTITY_DECL.search(data) or (
            b"\x00" in data and _ENTITY_DECL.search(data.replace(b"\x00", b""))
        ):
            raise ValueError("this TMX file declares XML entities, which are not accepted.")
        # #576: far more <tu> elements than MAX_DOCUMENT_ROWS is refused by a C-speed count
        # before anything is parsed or read from the DB. (A UTF-16 file interleaves NULs, so
        # the count also runs on the NUL-stripped view; the loop below re-checks the count it
        # really walked, so this only has to be cheap, not exact.)
        _tus = data.count(b"<tu>") + data.count(b"<tu ")
        if b"\x00" in data:
            _flat = data.replace(b"\x00", b"")
            _tus = max(_tus, _flat.count(b"<tu>") + _flat.count(b"<tu "))

        def _check_units(n: int) -> None:
            seed_caps.check_import_rows(n, what="This TMX file")

        def _open():
            return io.BytesIO(data)

    _check_units(_tus)

    # QA BUG-03: reconcile the requested pair with the project's BEFORE a single row
    # is written, so an import can never land in a bucket the read path cannot reach.
    # Raises LocaleMismatchError (a ValueError) on an irreconcilable pair; the DB is
    # untouched at this point, so the refusal costs nothing.
    source_locale, target_locale, locales_normalized = resolve_import_locales(
        session, project_id, source_locale, target_locale
    )

    ct_id = None
    # S6 (ADR 0094): the write plan lives in a throwaway SQLite file (tm_import_stream.
    # ImportSpool), not in Python dicts. Before, the plan held every new unit as a tuple
    # plus a hash index and the scope's rows as hash -> (id, target): about 500 bytes per
    # unit, so a 2M-unit TMX needed over 1 GB. The spool holds the same three things
    # (`ex` = the scope's rows as phase 1 read them, `nw` = new rows, `exw` = existing rows
    # the file names) with the same counting rules (ImportSpool.resolve), at a fixed page
    # cache. Created at the first qualifying <tu>, so a file with none never touches the DB.
    spool = None

    def _ensure_loaded() -> None:
        nonlocal ct_id, spool
        if spool is not None:
            return
        # Issue #584: PHASE 1 is read-only. Nothing is written until the whole
        # file has been parsed and validated, so a bad file costs the other
        # writers nothing and the write lock is only taken in phase 2.
        # SPEC-content-types phase 3: stamp the TARGET project's content type on
        # every NEW row (resolved once — all rows share the same target
        # project). Updated rows are NOT retro-restamped (kept type — §4.5).
        ct_id = _project_content_type_id(session, project_id)
        spool = tstream.ImportSpool()
        spool.load_existing(
            session.exec(
                select(SegmentTM.source_hash, SegmentTM.id, SegmentTM.target_text).where(
                    SegmentTM.project_id == project_id,
                    SegmentTM.source_locale == source_locale,
                    SegmentTM.target_locale == target_locale,
                )
            )
        )

    imported = skipped = updated = skipped_refusal = 0
    skipped_integrity = acknowledged_loss = 0
    skipped_by_code: dict[str, int] = {}
    skipped_rows: list[dict] = []
    processed = 0
    # Accepted units waiting to be resolved against the spool, in file order:
    # (source_hash, source_text, target_text, meta). At most TMX_IMPORT_FLUSH_BATCH. A unit's
    # in-context key (ADR 0102) rides in meta["_ck"] through the spool and is moved into the
    # context_key column when the row is inserted.
    pending: list[tuple] = []
    # One-unit lookahead for the in-context key: a unit with no context props takes the key of
    # its neighbours in file order (previous and next unit), so it is finished when the NEXT
    # unit has been read. held = (candidate or None, previous source), prev_src = last source.
    held: tuple | None = None
    prev_src = ""
    # Units of this locale pair seen so far, and whether the file is our own export (header
    # creationtool). The adjacent-unit fallback needs evidence of document order: a lone
    # unit has none (it would get ctx("", "") and become a false 101 for any one-segment
    # document), and our own export states its contexts explicitly, so a unit without a
    # key there stays context-free instead of inheriting its file neighbours.
    n_pairs = 0
    own_export = False

    def _emit(cand: tuple, prev: str, nxt: str) -> None:
        h, src, tgt, meta, ck = cand
        m = dict(meta) if meta else {}
        m["_ck"] = ck or (context_key(prev, nxt) if n_pairs >= 2 and not own_export else None)
        pending.append((h, src, tgt, m))
        if len(pending) >= max(1, int(TMX_IMPORT_FLUSH_BATCH)):
            _flush_pending()

    def _chain(src_i: str | None, cand_i: tuple | None) -> None:
        nonlocal held, prev_src, n_pairs
        if src_i is None:  # not a source/target pair: invisible to adjacency
            return
        n_pairs += 1
        if held is not None and held[0] is not None:
            _emit(held[0], held[1], src_i)
        held = (cand_i, prev_src)
        prev_src = src_i

    def _flush_pending() -> None:
        nonlocal imported, updated, skipped
        if pending:
            i, u, s = spool.resolve(pending)
            imported += i
            updated += u
            skipped += s
            pending.clear()

    # The SPECIFIC <body> element that is a direct child of the document root
    # (root.find("body") returns the FIRST such match, or None) -- latched by
    # IDENTITY at <body>'s own "start" event (when the stack, before pushing it,
    # holds only the root), not by tag comparison at "end" time. Only a <tu>
    # whose parent IS this exact element is processed, matching body.findall("tu")
    # exactly: a same-named but non-root-child <body> (or a second, extra <body>
    # sibling malformed TMX would never have) is never mistaken for it, so no
    # session.add()/DB read ever happens for a <tu> the old parser would have
    # ignored.
    root_body = None
    # The qualifying <tu> currently being built, if any. Everything OUTSIDE such a
    # <tu> is dropped at its own end event: without that, a document the latch never
    # matches (a NAMESPACED TMX -- <tmx xmlns="...">, whose tags read "{ns}body" so
    # root_body stays None -- a body-less file, or any other XML mis-uploaded to the
    # TM form) is never cleared at all and the "streaming" parse silently
    # materializes the WHOLE document. Measured before this: a 17 MB namespaced TMX
    # peaked at 37 MB of Python heap (~2.1x the file), i.e. ~210 MB at the new
    # 100 MiB cap -- the exact spike the cap raise was paired with this rewrite to
    # avoid. Elements inside an open qualifying <tu> are exempt so its <tuv>/<seg>
    # children are never dropped before the <tu>'s own end event reads them.
    open_tu = None

    fh = _open()
    reader = tstream.CountingReader(fh)
    # Parse progress is reported at most about once a second (bytes read of the file).
    last_tick = _monotonic()
    try:
        stack: list = []
        for event, elem in ET.iterparse(reader, events=("start", "end")):  # noqa: S314 -- _ENTITY_DECL / scan_tmx_file refused entity declarations
            if event == "start":
                parent = stack[-1] if stack else None
                if elem.tag == "header" and len(stack) == 1:
                    own_export = elem.get("creationtool") == branding.brand_name()
                if elem.tag == "body" and root_body is None and len(stack) == 1:
                    root_body = elem
                elif (
                    elem.tag == "tu"
                    and open_tu is None
                    and parent is not None
                    and parent is root_body
                ):
                    open_tu = elem
                stack.append(elem)
                continue
            stack.pop()
            parent = stack[-1] if stack else None
            if elem is not open_tu:
                if open_tu is None:
                    # Outside every qualifying <tu>: nothing downstream reads this
                    # subtree, so drop it now and drop the completed siblings its
                    # parent still holds. This is what bounds peak memory for the
                    # no-match documents described above.
                    elem.clear()
                    if parent is not None:
                        parent.clear()
                continue
            open_tu = None

            _ensure_loaded()
            langs: dict[str, str] = {}
            for tuv in elem.findall("tuv"):
                lang = tuv.get(XML_LANG)
                seg = tuv.find("seg")
                text = "".join(seg.itertext()) if seg is not None else None
                if lang and text is not None:
                    langs[lang] = text
            src = _pick(langs, source_locale)
            tgt = _pick(langs, target_locale)
            cand = None
            if src and tgt:
                # Refusal GATE (Deliverable 3): a hedged TARGET is skipped outright
                # — never inserted, updated, or embedded — so an MT-exported TMX
                # can't poison the TM with a 100%%-match that bypasses the live
                # guard. Position-anchored semantics (refusal.is_refusal) hold: a
                # legitimate translation that merely mentions refusal vocabulary
                # mid-string is NOT skipped.
                refused = quality.is_refusal_check(tgt)
                skip = None
                if refused:
                    skipped_refusal += 1
                else:
                    # Issue #567: the approve-path integrity gate (#562). A pair that
                    # breaks placeholders/tags would be reused as a 100% match, so it is
                    # withheld here exactly as approve withholds it; the file goes on.
                    skip = integrity_skip(
                        src, tgt, source_locale=source_locale, target_locale=target_locale
                    )
                    if (
                        skip is not None
                        and skip[0] == TM_SKIP_PLACEHOLDER
                        and acknowledge_placeholder_loss
                    ):
                        acknowledged_loss += 1
                        skip = None
                if skip is not None:
                    skipped_integrity += 1
                    skipped_by_code[skip[0]] = skipped_by_code.get(skip[0], 0) + 1
                    if len(skipped_rows) < TMX_IMPORT_REPORT_ROWS:
                        skipped_rows.append(
                            {
                                "row": processed + 1,
                                "code": skip[0],
                                "reason": skip[1],
                                "source": src[:80],
                            }
                        )
                elif not refused:
                    # Phase 4: our x-* provenance props are read back off the <tu> into
                    # meta (round-trip); None when the TMX carried no recognized prop, so a
                    # legacy/propless import stays byte-identical. Kept for a new row only
                    # (the first occurrence's meta), as before.
                    cand = (
                        hash_text(src, source_locale, target_locale),
                        src,
                        tgt,
                        _meta_from_tu(elem),
                        _context_from_tu(elem),
                    )
            _chain(src if (src and tgt) else None, cand)

            processed += 1
            _check_units(processed)

            # This <tu> is fully processed -- drop its subtree (tuv/seg/prop) and
            # the completed-so-far siblings still held by <body>'s children list,
            # so peak memory is one <tu> rather than the whole termbase (mirrors
            # glossary_tbx.parse_rows's elem.clear() + stack[-1].clear() pattern).
            elem.clear()
            if stack:
                stack[-1].clear()
            if parse_progress is not None and processed % 2000 == 0:
                now_t = _monotonic()
                if now_t - last_tick >= 1.0:
                    last_tick = now_t
                    try:
                        parse_progress(reader.bytes_read, total_bytes, processed)
                    except Exception:  # a progress callback must never fail the import
                        log.debug("tmx import parse progress callback failed", exc_info=True)
        if held is not None and held[0] is not None:
            _emit(held[0], held[1], "")  # the last unit of the file: an edge, no next source
        held = None
        if spool is not None:
            _flush_pending()
    except BaseException:
        if spool is not None:
            spool.close()
        raise
    finally:
        fh.close()

    if root_body is None or spool is None:
        # No qualifying <tu> was ever reached, so _ensure_loaded never ran: no
        # savepoint, no query, no commit -- the DB is untouched.
        return {
            "imported": 0,
            "skipped": 0,
            "updated": 0,
            "skipped_refusal": 0,
            "skipped_integrity": 0,
            "skipped_by_code": {},
            "skipped_rows": [],
            "acknowledged_placeholder_loss": 0,
            "source_locale": source_locale,
            "target_locale": target_locale,
            "locales_normalized": locales_normalized,
            "write_chunks": 0,
            "concurrent_deleted": 0,
        }
    if parse_progress is not None:
        try:
            parse_progress(total_bytes, total_bytes, processed)
        except Exception:
            log.debug("tmx import parse progress callback failed", exc_info=True)
    try:
        return _import_tmx_write(
            session,
            project_id,
            source_locale,
            target_locale,
            spool=spool,
            ct_id=ct_id,
            progress=progress,
            report={
                "imported": imported,
                "skipped": skipped,
                "updated": updated,
                "skipped_refusal": skipped_refusal,
                "skipped_integrity": skipped_integrity,
                "skipped_by_code": skipped_by_code,
                "skipped_rows": skipped_rows,
                "acknowledged_placeholder_loss": acknowledged_loss,
                "source_locale": source_locale,
                "target_locale": target_locale,
                "locales_normalized": locales_normalized,
            },
        )
    finally:
        spool.close()


def _import_tmx_write(
    session: Session,
    project_id: int | None,
    source_locale: str,
    target_locale: str,
    *,
    spool,
    ct_id,
    progress,
    report: dict,
) -> dict:
    """PHASE 2 of `_import_tmx_attempt`: write the spooled plan in short chunk transactions.
    `report` is phase 1's counts; the chunks correct imported/updated/skipped for what other
    writers changed meanwhile and add write_chunks / concurrent_deleted."""
    # ---- PHASE 2 (Issues #584, #615): chunked write transactions. -------------------
    # The file parsed cleanly, so everything below is bulk SQL on rows already planned
    # (in the spool). #584 wrote it all in ONE transaction (all-or-nothing, lock held for
    # the whole write: about 40 us per row, so 4 s at 100k units and 12 s at 300k,
    # stalling every other writer that long). #615 bounds the hold: the rows go out in
    # chunks of at most TMX_WRITE_CHUNK_ROWS, each its own short transaction that takes
    # the write lock, writes, commits and RELEASES it, then pauses TMX_CHUNK_PAUSE_S so a
    # waiting writer gets in.
    #
    # Atomicity, stated honestly:
    #   * a bad file (malformed XML, over a cap, entities, wrong locales) fails in phase 1,
    #     before any write: nothing is stored;
    #   * an import that fits ONE chunk (<= TMX_WRITE_CHUNK_ROWS rows to write) is exactly as
    #     atomic as before;
    #   * a bigger import that fails (disk full, a DB error) after at least one chunk
    #     committed keeps those chunks and raises TmxImportIncomplete, which says how many
    #     rows are in. Retrying with the SAME file finishes the job and never duplicates:
    #     every unit is keyed by (project, locales, source hash), so rows already stored are
    #     counted as unchanged and only the missing ones are inserted.
    #   * another writer (a linguist editing or deleting a TM entry, an approve, a second
    #     import) may commit between phase 1 and a chunk, or between two chunks. #584 handled
    #     that by re-reading the TM under the write lock and re-running the import, so the
    #     FILE's target won over any change committed before the write, and the counts were
    #     those of the re-run. Each chunk now does the same for ITS rows under its own lock
    #     (never on stale phase-1 state): every row is re-read, a target that differs from
    #     the file's is set to the file's (counted `updated`), one that already matches is
    #     counted `skipped`, and a source another writer stored first is updated, not
    #     inserted again (the unique key is never hit). A row DELETED meanwhile is not
    #     brought back: it is counted in `concurrent_deleted`, not as written.
    now = datetime.utcnow()
    tbl = SegmentTM.__table__
    # Compare-and-set: the target is replaced only while it is still the one just re-read
    # under the lock (on SQLite the lock already guarantees it; on Postgres the lock is per
    # row, so the WHERE clause is what makes the check and the write one step).
    upd_changed = (
        tbl.update()
        .where(tbl.c.id == sa.bindparam("b_id"), tbl.c.target_text == sa.bindparam("b_old"))
        .values(
            target_text=sa.bindparam("b_tgt"),
            activity_at=sa.bindparam("b_now"),
            # ADR 0102: a target changed by the file takes the file unit's own in-context key
            # (NULL when it has none): the old key described the OLD target's approval.
            context_key=sa.bindparam("b_ck"),
        )
    )
    upd_touched = (
        tbl.update()
        .where(tbl.c.id == sa.bindparam("b_id"))
        .values(activity_at=sa.bindparam("b_now"))
    )
    chunk_rows = max(1, int(TMX_WRITE_CHUNK_ROWS))
    n_new, n_existing = spool.counts()
    total_rows = n_new + n_existing
    # The spool packs chunks exactly as the in-memory plan did (new rows first, in source
    # hash order: every index on this table leads with or ends in the hash, so hash-ordered
    # inserts append to each b-tree instead of splitting random pages; then existing rows),
    # so the chunk count is known up front.
    n_work = -(-total_rows // chunk_rows)
    handled = 0  # rows of the plan dealt with by committed chunks
    chunks_done = 0

    def _read_present(hashes: list[str]) -> dict[str, tuple[int, str]]:
        """{source hash: (row id, target)} of the hashes the TM holds NOW for this pair."""
        present: dict[str, tuple[int, str]] = {}
        for i in range(0, len(hashes), 500):
            for h, rid, tgt in session.execute(
                sa.select(tbl.c.source_hash, tbl.c.id, tbl.c.target_text).where(
                    tbl.c.project_id == project_id,
                    tbl.c.source_locale == source_locale,
                    tbl.c.target_locale == target_locale,
                    tbl.c.source_hash.in_(hashes[i : i + 500]),
                )
            ):
                present[h] = (rid, tgt)
        return present

    def _read_targets(ids: list[int]) -> dict[int, str]:
        cur: dict[int, str] = {}
        for i in range(0, len(ids), 500):
            for rid, tgt in session.execute(
                sa.select(tbl.c.id, tbl.c.target_text).where(tbl.c.id.in_(ids[i : i + 500]))
            ):
                cur[rid] = tgt
        return cur

    def _cas(rows: list[dict]) -> dict[int, str]:
        """Apply the compare-and-set `upd_changed` and VERIFY it (ADR 0105). The UPDATE only
        matches while the target is still `b_old`; a writer that changed the row between the
        re-read and here makes it match nothing (silently: executemany row counts are not
        reliable on every driver). So the targets are read back: "ok" = the row holds the file's
        target, "gone" = deleted meanwhile, "lost" = still another value after
        TMX_CAS_ROUNDS tries. A miss is retried on the fresh target (the file's target wins over
        any change committed before the write, #584), so only a writer that keeps changing the
        row every round ends as "lost"."""
        outcome: dict[int, str] = {}
        todo = rows
        for _round in range(max(1, int(TMX_CAS_ROUNDS))):
            session.execute(upd_changed, todo)
            cur = _read_targets([r["b_id"] for r in todo])
            again = []
            for r in todo:
                have = cur.get(r["b_id"])
                if have is None:
                    outcome[r["b_id"]] = "gone"
                elif have == r["b_tgt"]:
                    outcome[r["b_id"]] = "ok"
                else:
                    outcome[r["b_id"]] = "lost"
                    again.append({**r, "b_old": have})
            if not again:
                break
            todo = again
        return outcome

    def _insert_fresh(rows: list[tuple]) -> set[str]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING source_hash (ADR 0105): the hashes THIS
        statement stored. A row whose key a concurrent writer stored after the re-read is simply
        not inserted (on Postgres the statement first waits for a writer that has not committed),
        instead of failing the chunk with a unique violation."""
        values = [
            {
                "project_id": project_id,
                "source_locale": source_locale,
                "target_locale": target_locale,
                "source_text": src,
                "target_text": tgt,
                "source_hash": h,
                "content_type_id": ct_id,
                "created_at": now,
                "use_count": 0,
                "last_used_at": None,
                "activity_at": now,
                "meta": {k: v for k, v in meta.items() if k != "_ck"} or None if meta else None,
                "context_key": (meta or {}).get("_ck"),
            }
            for h, src, tgt, meta in rows
        ]
        dialect = session.get_bind().dialect.name
        if dialect in ("postgresql", "sqlite"):
            from sqlalchemy.dialects import postgresql as _pg
            from sqlalchemy.dialects import sqlite as _lite

            ins = (_pg.insert if dialect == "postgresql" else _lite.insert)(tbl)
            stmt = ins.on_conflict_do_nothing(
                index_elements=["project_id", "source_hash", "source_locale", "target_locale"]
            ).returning(tbl.c.source_hash)
            try:
                return {row[0] for row in session.execute(stmt, values)}
            except sa.exc.OperationalError as exc:
                # A legacy SQLite file whose unique index was skipped for pre-existing
                # duplicates (the init_db shim warns): no index to conflict on, so the plain
                # INSERT it always had. The statement failed before running; the savepoint holds.
                if dialect != "sqlite" or "ON CONFLICT clause does not match" not in str(exc):
                    raise
        session.execute(tbl.insert(), values)
        return {r[0] for r in rows}

    def _insert_chunk(chunk: list[tuple], adj: dict, out_new: list, out_changed: list) -> None:
        # Under the write lock: which of these hashes did another writer store since phase 1?
        present = _read_present([r[0] for r in chunk])
        late = [r for r in chunk if r[0] in present]
        fresh = [r for r in chunk if r[0] not in present]
        # ADR 0105: on Postgres the write lock is per row, so a writer can still store one of the
        # "fresh" units between the re-read above and the INSERT below (two imports into one
        # project did, S7c). The INSERT never fails on that: the rows it did not store are
        # re-read and join `late`. Rounds, because a row stored and DELETED again meanwhile is
        # absent on the re-read; after TMX_INSERT_ROUNDS it is counted `concurrent_deleted`.
        for _round in range(max(1, int(TMX_INSERT_ROUNDS))):
            if not fresh:
                break
            won = _insert_fresh(fresh)
            out_new.extend(r for r in fresh if r[0] in won)
            missed = [r for r in fresh if r[0] not in won]
            found = _read_present([r[0] for r in missed]) if missed else {}
            present.update(found)
            late.extend(r for r in missed if r[0] in found)
            fresh = [r for r in missed if r[0] not in found]
        if fresh:
            adj["imported"] -= len(fresh)
            adj["concurrent_deleted"] += len(fresh)
        if late:
            # Stored by another writer since phase 1 (#584 semantics): the file's target is
            # applied to that row (updated), or it already matches (skipped).
            moved = [
                {
                    "b_id": present[r[0]][0],
                    "b_old": present[r[0]][1],
                    "b_tgt": r[2],
                    "b_now": now,
                    "b_ck": (r[3] or {}).get("_ck"),
                }
                for r in late
                if present[r[0]][1] != r[2]
            ]
            adj["imported"] -= len(late)
            adj["updated"] += len(moved)
            adj["skipped"] += len(late) - len(moved)
            if moved:
                outcome = _cas(moved)
                for m in moved:
                    got = outcome[m["b_id"]]
                    if got == "ok":
                        out_changed.append((m["b_id"], m["b_tgt"]))
                    else:  # deleted meanwhile, or changed again every round: not written
                        adj["updated"] -= 1
                        adj["concurrent_deleted" if got == "gone" else "skipped"] += 1

    def _existing_chunk(chunk: list[tuple], adj: dict, out_changed: list) -> None:
        # Under the write lock: re-read these rows and decide on what is stored NOW, not on
        # what phase 1 read (#584 semantics per chunk; see the PHASE 2 note above).
        current = _read_targets([r[0] for r in chunk])
        upd: list[dict] = []
        touch: list[dict] = []
        counted: dict[int, int] = {}  # row id -> units counted `updated` for it so far
        still_skip: dict[int, int] = {}  # row id -> units still counted `skipped` for it
        for rid, orig, final, n_upd, n_skip, ck_set, ck in chunk:
            cur = current.get(rid)
            if cur is None:
                # Deleted by another writer while the import ran: not brought back, and not
                # counted as written (a later re-import of the file stores it again).
                adj["updated"] -= n_upd
                adj["skipped"] -= n_skip
                adj["concurrent_deleted"] += 1
            elif cur == final:
                # Already holds the file's target (unchanged, or another writer stored exactly
                # that meanwhile): only the activity stamp moves.
                if cur != orig:
                    adj["updated"] -= n_upd
                    adj["skipped"] += n_upd
                touch.append({"b_id": rid, "b_now": now})
            else:
                # Differs from the file: the file's target is stored (as #584's re-run did when
                # another writer had changed it before the write).
                counted[rid] = n_upd
                still_skip[rid] = 0
                if cur != orig:
                    adj["skipped"] -= n_skip
                    adj["updated"] += n_skip
                    counted[rid] += n_skip
                else:
                    still_skip[rid] = n_skip  # stay `skipped` unless the row is deleted meanwhile
                # ck_set = 0: the file never changed this row's target, but another writer did
                # and may have re-keyed it; putting the file's target back under that writer's
                # key could pair them wrongly (a false 101), so the key is cleared.
                upd.append(
                    {
                        "b_id": rid,
                        "b_old": cur,
                        "b_tgt": final,
                        "b_now": now,
                        "b_ck": ck if ck_set else None,
                    }
                )
        if upd:
            # ADR 0105: verified compare-and-set; a row changed again since the re-read is retried
            # on its fresh target, one deleted meanwhile or never settled is not counted written.
            outcome = _cas(upd)
            for u in upd:
                got = outcome[u["b_id"]]
                if got == "ok":
                    out_changed.append((u["b_id"], u["b_tgt"]))
                else:
                    adj["updated"] -= counted[u["b_id"]]
                    if got == "gone":
                        adj["skipped"] -= still_skip[u["b_id"]]  # as the cur-is-None branch
                        adj["concurrent_deleted"] += 1
                    else:
                        adj["skipped"] += counted[u["b_id"]]
        if touch:
            session.execute(upd_touched, touch)

    adj = {"imported": 0, "updated": 0, "skipped": 0, "concurrent_deleted": 0}

    from app import db as _db  # local: app.db imports app.services at load time

    for n_chunk, (part_new, part_existing) in enumerate(spool.iter_work(chunk_rows)):
        if n_chunk and TMX_CHUNK_PAUSE_S > 0:
            time.sleep(TMX_CHUNK_PAUSE_S)  # the write lock is free here: let a waiting writer in
        attempt = 0
        while True:  # S3/T4: a Postgres deadlock inside the savepoint re-runs the chunk
            attempt += 1
            savepoint = None
            in_savepoint = False
            # Count adjustments and written rows of THIS chunk, kept only if it commits.
            c_adj = dict.fromkeys(adj, 0)
            c_new: list = []
            c_changed: list = []
            try:
                savepoint = import_lock.begin_write_phase(session, SegmentTM)
                in_savepoint = True
                # Postgres: lock this chunk's existing rows in id order first, so two imports
                # (or an import and an editor) updating the same rows queue instead of
                # deadlocking. No SQL on SQLite (one writer lock already orders everything).
                # Partial by design: rows another writer inserted since phase 1 ("late" rows
                # in _insert_chunk) and the new inserts' unique-index locks are not ordered
                # here; a deadlock there is caught by the retry below. Under REPEATABLE READ
                # or SERIALIZABLE a 40001 repeats (same outer snapshot) and is raised after 3.
                _db.lock_rows_in_order(session, SegmentTM, [r[0] for r in part_existing])
                if part_new:
                    _insert_chunk(part_new, c_adj, c_new, c_changed)
                if part_existing:
                    _existing_chunk(part_existing, c_adj, c_changed)
                in_savepoint = False
                savepoint.commit()  # RELEASE SAVEPOINT -- the writes join the outer txn
                session.commit()  # ... and the commit frees the SQLite write lock
                break
            except BaseException as exc:
                # ROLLBACK TO SAVEPOINT undoes this chunk and leaves the caller's own pending
                # work (flushed into the OUTER transaction before the savepoint opened) untouched.
                import_lock.rollback_quietly(session, savepoint)
                # A Postgres 40P01/40001 raised INSIDE the savepoint is undone by the rollback
                # above and the chunk re-reads everything it writes, so running it again is
                # safe. Never on SQLite (sqlite_locks=False: behavior unchanged there) and never
                # once the commit itself started (the outer transaction may be gone).
                if (
                    in_savepoint
                    and isinstance(exc, Exception)
                    and attempt < _db.CONFLICT_RETRY_TRIES
                    and _db.is_retryable_conflict(exc, sqlite_locks=False)
                ):
                    log.warning(
                        "tmx import: write conflict in chunk %d, retrying it (attempt %d of %d)",
                        n_chunk + 1,
                        attempt + 1,
                        _db.CONFLICT_RETRY_TRIES,
                    )
                    _db.conflict_retry_pause()
                    continue
                if chunks_done and isinstance(exc, Exception):
                    # The full error goes to the log only: a database error text can carry SQL
                    # and row values, which must not reach the page, the API reply or the CLI.
                    log.warning(
                        "tmx import stopped after chunk %d of %d",
                        chunks_done,
                        n_work,
                        exc_info=True,
                    )
                    raise TmxImportIncomplete(
                        f"the TMX import stopped after {handled} of {total_rows} rows were "
                        f"processed ({chunks_done} of {n_work} write chunks) because of a "
                        "server-side error "
                        f"({type(exc).__name__}). Those rows are kept; "
                        "importing the same file again finishes the job and creates no duplicates.",
                        rows_written=handled,
                        rows_total=total_rows,
                    ) from exc
                raise
        for k, v in c_adj.items():
            adj[k] += v
        handled += len(part_new) + len(part_existing)
        chunks_done += 1
        if progress is not None:
            try:
                progress(handled, total_rows)
            except Exception:  # a progress callback must never fail the import
                log.debug("tmx import progress callback failed", exc_info=True)
        if n_work > 1 and (chunks_done % 10 == 0 or chunks_done == n_work):
            log.info("tmx import: %d/%d rows stored (%d chunks)", handled, total_rows, chunks_done)
    if adj["concurrent_deleted"]:
        log.info(
            "tmx import: %d rows deleted by another writer during the import were not restored",
            adj["concurrent_deleted"],
        )
    if not chunks_done:
        session.commit()
    out = dict(report)
    out["imported"] += adj["imported"]
    out["updated"] += adj["updated"]
    out["skipped"] += adj["skipped"]
    out["write_chunks"] = chunks_done
    out["concurrent_deleted"] = adj["concurrent_deleted"]
    return out


def export_tmx(
    session: Session, project_id: int | None, source_locale: str, target_locale: str
) -> bytes:
    src, tgt, entries = _export_entries(session, project_id, source_locale, target_locale)
    return _build_export_tmx(entries, src, tgt)


def export_tmx_result(
    session: Session, project_id: int | None, source_locale: str, target_locale: str
) -> tuple[str, str, int, bytes]:
    """(effective_source, effective_target, tu_count, tmx_bytes) -- for the surfaces that
    report a count. `tu_count` is the REAL row count (len(entries)), not a fragile
    substring scan of the serialized bytes (review H2)."""
    src, tgt, entries = _export_entries(session, project_id, source_locale, target_locale)
    return src, tgt, len(entries), _build_export_tmx(entries, src, tgt)


# Rows read per page when an export is streamed (column-only rows, never ORM objects).
TMX_EXPORT_PAGE = 2000


def export_tmx_stream(
    session: Session, project_id: int | None, source_locale: str, target_locale: str
):
    """(effective_source, effective_target, tu_count, byte_chunks) for a STREAMED export (#615).

    Same rows, same locale coercion and the same per-<tu> content as `export_tmx_result`, but
    the TMX is produced in id-ordered pages by a generator that opens its OWN session, so a
    100k-unit export no longer builds every ORM row, one lxml tree and one base64 copy in
    memory (about 1.1 GB peak at 100k) and the response can start immediately. `tu_count` is
    the real row count at the time of the call; a row added or removed while the stream is
    running appears or not (reads are not frozen), which the JSON export also never promised.

    A failure AFTER the first bytes (e.g. a stored row with an XML-invalid control character)
    cannot change the 200 that is already sent. The generator then raises, the server aborts
    the chunked response without its terminating chunk (HTTP clients report an incomplete
    read), and the body ends WITHOUT the closing `</body></tmx>`. That closing tag is the end
    marker: a truncated file is not well-formed XML, so no TMX reader (ours included) can take
    it for a complete export. Callers that need a count check have X-TM-Count."""
    src, tgt = resolve_export_locales(session, project_id, source_locale, target_locale)
    where = (
        SegmentTM.project_id == project_id,
        SegmentTM.source_locale == src,
        SegmentTM.target_locale == tgt,
    )
    count = session.exec(select(func.count()).select_from(SegmentTM).where(*where)).one()
    bind = session.get_bind()

    def chunks():
        head = etree.Element("tmx", version="1.4")
        header = etree.SubElement(head, "header")
        header.set("creationtool", branding.brand_name())
        header.set("segtype", "sentence")
        header.set("srclang", src)
        header.set("datatype", "plaintext")
        yield (
            b"<?xml version='1.0' encoding='UTF-8'?>\n<tmx version=\"1.4\">\n  "
            + etree.tostring(header, encoding="utf-8")
            + b"\n  <body>\n"
        )
        last_id = 0
        with Session(bind) as page_session:
            while True:
                rows = page_session.exec(
                    select(
                        SegmentTM.id,
                        SegmentTM.source_text,
                        SegmentTM.target_text,
                        SegmentTM.meta,
                        SegmentTM.created_at,
                        SegmentTM.use_count,
                        SegmentTM.context_key,
                    )
                    .where(*where, SegmentTM.id > last_id)
                    .order_by(SegmentTM.id)
                    .limit(TMX_EXPORT_PAGE)
                ).all()
                if not rows:
                    break
                last_id = rows[-1].id
                # One serialization per page (not per unit): build the page under a throwaway
                # <body> and drop its tags, leaving the <tu> run.
                page = etree.Element("body")
                for e in rows:
                    tu = etree.SubElement(page, "tu")
                    _tmx_stamp_tu(tu, e)
                    for lang, text in ((src, e.source_text), (tgt, e.target_text)):
                        tuv = etree.SubElement(tu, "tuv")
                        tuv.set(XML_LANG, lang)
                        etree.SubElement(tuv, "seg").text = text
                yield etree.tostring(page, encoding="utf-8")[len(b"<body>") : -len(b"</body>")]
        yield b"  </body>\n</tmx>\n"

    return src, tgt, int(count), chunks()


# --- write-back + concordance (used by the CAT editor) -------------------------
def write_back(
    session,
    project_id,
    source_text,
    target_text,
    source_locale,
    target_locale,
    segment=None,
    context=None,
):
    """Upsert an approved segment into the segment TM (keyed by source hash).

    Token-bearing segments are NOT written back (see is_tm_eligible): storing a
    target that still contains inline-fidelity tokens would poison later lookups
    for tagless or differently-mapped sources.

    `segment` (optional, GitHub #42) is the Segment row the text came from. When it
    carries a `source_fallback` qa_flag the write is REFUSED — see
    `_source_fallback_blocked`. Passing nothing is byte-identical to the pre-#42
    behaviour.

    CONTEXT (ADR 0102): the row stores the in-context key of this write, so a later lookup
    with the same neighbours (or the same string key) is a 101 % match. `context` is a ready
    key string, or a (prev_source, next_source) pair; when omitted and `segment` is given, the
    key is read from the segment's neighbours by order_index (same document and target locale)
    or from its string key (tm_context.context_key_for_segment). Written on insert AND update
    (the last writer's context wins; a write with no context clears a stale key only when it
    also changes the target)."""
    if not (target_text and target_text.strip()):
        return None
    if not is_tm_eligible(source_text):
        return None
    if _source_fallback_blocked(segment):
        return None
    h = hash_text(source_text, source_locale, target_locale)
    ck = None
    if context is not None:
        ck = context if isinstance(context, str) else context_key(context[0], context[1])
    elif segment is not None:
        from app.services import tm_context

        ck = tm_context.context_key_for_segment(session, segment)
    # SPEC-content-types phase 3: the project's content type (also the NEW-row stamp).
    ct_id = _project_content_type_id(session, project_id)
    # Phase 4 provenance: a write_back is the CAT approve write-back — origin is a
    # human-approved segment. content_type name is cheaply in hand (identity-map
    # cached). Neighbour context (prev/next) is NOT available at this write site (no
    # surrounding segments passed) so those keys are intentionally skipped — adding
    # them would need an N+1 fetch (deferred, documented). meta is ADDITIVE provenance
    # only and is never read by any match path.
    prov = _provenance_meta("human_approved", content_type=_content_type_name(session, ct_id))

    # The read-check (keyed on the tuple ux_segmenttm_upsert_key enforces). Factored so
    # the post-conflict recovery path re-runs the IDENTICAL query.
    def _read_existing():
        return session.exec(
            select(SegmentTM).where(
                SegmentTM.source_hash == h,
                SegmentTM.project_id == project_id,
                SegmentTM.source_locale == source_locale,
                SegmentTM.target_locale == target_locale,
            )
        ).first()

    # The UPDATE arm (re-stamp target + provenance). Shared by the
    # first-check hit and the recovery after a losing INSERT race.
    def _apply_update(row):
        if ck is not None:
            row.context_key = ck
        elif row.target_text != target_text:
            row.context_key = None  # a new target with no known context: the old key is stale
        row.target_text = target_text
        row.activity_at = datetime.utcnow()
        row.meta = prov  # re-stamp current provenance (reassign so JSON is dirty)
        session.add(row)
        session.commit()
        return row

    existing = _read_existing()
    if existing:
        return _apply_update(existing)
    # SPEC-content-types phase 3: stamp the project's content type on the NEW row only
    # (an existing row keeps the type it was written under — no retro-restamp, §4.5).
    row = SegmentTM(
        project_id=project_id,
        source_locale=source_locale,
        target_locale=target_locale,
        source_text=source_text,
        target_text=target_text,
        source_hash=h,
        content_type_id=ct_id,
        meta=prov,
        context_key=ck,
        activity_at=datetime.utcnow(),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # P-C2: a concurrent writer (a second PROCESS — the long-lived server AND a CLI
        # approving the same source — or a slow-disk race that widened the read->insert
        # window) inserted the row for this key between our read-check and this INSERT.
        # ux_segmenttm_upsert_key rejected the duplicate. Recover the upsert's intent by
        # rolling back the failed INSERT, re-reading the winner's row and taking the
        # UPDATE path — last-writer-wins, byte-identical to the no-race outcome.
        session.rollback()
        existing = _read_existing()
        if existing is not None:
            return _apply_update(existing)
        # The conflicting row vanished (a concurrent delete between our rollback and the
        # re-read) — vanishingly unlikely. Re-raise the constraint error rather than
        # silently returning None: the caller sees a real failure (the same class as any
        # other commit error), never a false "not written to TM".
        raise
    return row


def grid_contexts(session, segments, page_segments) -> dict:
    """{source text: context key} for the page's segments (ADR 0102: a stored unit approved in
    the same context shows as a 101 % match). The keys come from the whole ordered locale list
    so a page edge still sees its real neighbours; a source repeated on one page takes its
    first occurrence's context (the grid is keyed by source text)."""
    from app.services import tm_context

    keys = dict(
        zip(
            (s.id for s in segments),
            tm_context.segment_context_keys(session, segments),
            strict=True,
        )
    )
    out: dict = {}
    for s in page_segments:
        out.setdefault(s.source_text, keys.get(s.id))
    return out


def grid_matches(
    session,
    project,
    target_locale,
    sources: list[str],
    min_score=None,
    top_k=5,
    contexts=None,
) -> dict:
    """Best TM match per source for a grid/editor render: the batched replacement for a
    per-segment tm.lookup() loop.

    - Exact layer: ONE lookup_batch() call for the whole `sources` list (100 %, or 101 % when
      `contexts` {source: (prev, next) or key string} names a stored in-context unit). Tagged
      kind "exact". `min_score` is kept for old callers (ValueError below 100, ADR 0102).
    - Returns kind-tagged matches ({"kind": "exact", ...}).

    Returns {source_text: match_dict_or_None} - None entries are omitted (a template does
    `matches.get(source_text)` and gets None on a miss)."""
    out: dict[str, dict] = {}
    if not project or not target_locale or target_locale == "—" or not sources:
        return out

    exact_map = lookup_batch(
        session,
        project.id,
        sources,
        project.source_locale,
        target_locale,
        min_score=min_score,
        contexts=contexts,
    )
    for src, m in exact_map.items():
        if m is not None:
            out[src] = {**m, "kind": "exact"}

    return out
