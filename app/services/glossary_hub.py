"""glossary: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import logging
import time

import sqlalchemy as sa
from sqlmodel import Session, select

from app import config
from app.filters.base import InputLimitError
from app.models import GlossaryEntry, GlossarySense
from app.services import import_lock, llm_json
from app.services.glossary_common import (
    CSV_FORMAT,
    JSON_FORMAT,
    TBX_FORMAT,
    GlossaryImportIncomplete,
    _apply_csv_term_columns,
    _csv_rows,
    _meta_from_row,
)
from app.services.glossary_part2 import (
    _BASE_OPTION,
    _cue_fallback,
    detect_format,
)
from app.services.providers import get_provider

log = logging.getLogger(__name__)


def import_csv(
    session: Session,
    project_id: int,
    data: bytes,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
) -> dict:
    """Import a glossary CSV.

    Columns (header row): source_term, target_term, dnt, sense_label, context_cue,
    and the OPTIONAL TBX-ish meta columns part_of_speech, definition, usage_example,
    subject_field (SPEC-context-metadata phase 4). A row with a sense_label adds a
    context-dependent sense to its term; a row without one sets the term's base
    translation / DNT flag. Any present TBX-ish cells are merged into the term's
    GlossaryEntry.meta. A legacy CSV WITHOUT the four meta columns imports exactly as
    before (nothing stamped, meta stays NULL) — full back-compat.

    `glossary_id` (Phase 5-B): when given, entries are OWNED by that first-class
    module Glossary (deduped within it) and project_id is NULL (a module glossary is
    not project-bound — a 0 sentinel violated the project.id FK on Postgres). When
    None, the pre-module project-scoped behaviour is byte-identical (entries keyed by
    project_id, glossary_id NULL).

    This is now a thin parse-then-apply pair: the CSV reader below produces the rows
    and `_apply_rows` (shared with the TBX importer, see import_bytes) is the single
    write path. Return shape is UNCHANGED (BUG-09 QA finding, 2026-07-29): exactly
    {"entries": n, "senses": n} -- this is a long-standing public entry point with
    its own exact-equality regression tests, so it stays narrow even though
    `_apply_rows` now tracks four more counters internally (see its docstring).
    `import_bytes` is the seam that surfaces the fuller, reconciling count, and
    `import_csv_full` below is this same CSV write with that fuller dict, for a
    caller that needs the counters without the format dispatch.
    """
    res = import_csv_full(session, project_id, data, source_locale, target_locale, glossary_id)
    return {"entries": res["entries"], "senses": res["senses"]}


def import_csv_full(
    session: Session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
) -> dict:
    """`import_csv`'s write, returning _apply_rows's FULL counter dict instead of
    the legacy two-key view (see _apply_rows's COUNTERS section).

    Exists because `import_csv`'s narrow {"entries", "senses"} return is a frozen
    legacy contract, yet a CALLER may need the richer counters -- notably
    `glossary_module.import_csv`, which must know whether the import actually
    CHANGED anything to decide an approved glossary's draft revert, and cannot
    learn that from two creation counts. `import_bytes`'s CSV arm calls this too,
    so there is exactly ONE CSV write path and the decode contract (`_csv_rows`)
    cannot drift between entry points."""
    res = _apply_rows(
        session, project_id, lambda: _csv_rows(data), source_locale, target_locale, glossary_id
    )
    # SPEC-terminology-backend §7.1: the six concept-model term columns are applied to
    # the linked source GlossaryTerm AFTER the entries+concepts are written — MODULE
    # glossaries only (legacy per-project entries have no concept, M9, so the columns
    # are silently dropped there by design). This never touches _apply_rows' hot loop,
    # so LAW-TERM-1 holds; the columns write GlossaryTerm columns, never the meta blob.
    if glossary_id is not None:
        _apply_csv_term_columns(
            session,
            glossary_id,
            data,
            source_locale,
            target_locale,
            chunk_rows=GLOSSARY_WRITE_CHUNK_ROWS,
            pause_s=GLOSSARY_CHUNK_PAUSE_S,
        )
    return res


GLOSSARY_IMPORT_FLUSH_BATCH = 500

# #615: the most entries (plus new senses) ONE write transaction of a PROJECT glossary import
# stores. The SQLite write lock is held for one chunk at a time (about 35 us per row, so
# roughly 0.09 s at the default; 3.5 s for a 100k-row CSV before) and released between chunks.
# A file that fits one chunk is one transaction, exactly as atomic as before (ADR 0061).
GLOSSARY_WRITE_CHUNK_ROWS = 2500
# The same bound for a MODULE glossary import, whose chunks also reconcile the concept/term
# store and bind the touched rows (about 0.8 ms per row, so roughly 0.2 s at the default; 84 s
# for a 100k-row file before).
GLOSSARY_MODULE_CHUNK_ROWS = 250
# Seconds between two chunks with the write lock FREE (a writer polling SQLite's busy handler
# sleeps up to 100 ms, so this lets it in). Shared with the CSV term-column pass.
GLOSSARY_CHUNK_PAUSE_S = 0.05
# The lock hold a chunk is sized to (seconds); chunks adapt down from the maximum to meet it.
# 0 turns the adaptation off (every chunk is the maximum): tests use it for fixed sizes.
GLOSSARY_CHUNK_TARGET_S = 0.15


# How many rows _apply_rows processes before flushing pending writes and
# expunging every ORM object it holds (mirrors tm.TMX_IMPORT_FLUSH_BATCH -- same
# batch width, same shape; see tm.import_tmx's docstring for the fuller measured
# reasoning behind 500). A term whose entry is still PENDING (a brand-new insert
# made by THIS batch) is mutated directly in Python, cheaply; an entry from an
# EARLIER batch -- or one that pre-existed before this call -- is tracked as
# PRIMITIVES ONLY (id + current target_term/dnt/meta) and its changes are queued
# as one bulk UPDATE per batch, never re-fetched as a live object.
#
# WHAT THIS DOES AND DOES NOT BOUND (measured 2026-07-29, review round 1 -- the
# earlier wording here claimed "nothing grows with the FILE", which is not true
# and would have hidden the real ceiling). ORM state -- the identity map, the
# flush plan, this function's live-object bookkeeping -- is O(batch). The
# `state` dict is NOT: it holds one small primitives dict per DISTINCT source
# term the call has seen, which is the pre-existing rows in scope PLUS every new
# term the file introduces, so it is O(distinct terms) -- measured ~300 bytes
# per new term (2.76 MB at 2k rows vs 4.60 MB at 8k on an otherwise identical
# import). That floor is INHERENT TO DEDUPE (the same cost class, and the same
# admission, as tm.import_tmx's `existing_by_hash`): resolving "have I already
# seen this term?" without it means a SELECT per row. At the 100 MiB upload cap
# it is the term COUNT, not the file size, that sets the ceiling.
def _apply_rows(
    session: Session,
    project_id: int | None,
    rows,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
    *,
    on_row=None,
) -> dict:
    """THE glossary bulk write path (see `_apply_rows_once` for the full contract).

    Issue #584: the import is parsed and validated in full BEFORE the first write.
    Issue #615: the write then goes out in bounded chunks, each its own short transaction
    that re-reads its rows under the write lock (see `_apply_rows_once`), so nothing needs
    the rows twice. `rows` may still be a zero-argument CALLABLE returning the iterable
    (the shipped callers pass one); it is called exactly once."""
    if callable(rows):
        rows = rows()
    return _apply_rows_once(
        session, project_id, rows, source_locale, target_locale, glossary_id, on_row=on_row
    )


def _apply_rows_once(
    session: Session,
    project_id: int | None,
    rows,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
    *,
    on_row=None,
) -> dict:
    """THE glossary bulk write path — shared by every import format.

    `rows` is any iterable of dicts keyed by _CSV_COLUMNS -- a csv.DictReader for
    a CSV, a GENERATOR for glossary_tbx.parse_rows (2026-07-29); both are consumed
    exactly once, lazily. Splitting this out of import_csv is what lets TBX reuse
    the dedupe/upsert/sense/meta semantics verbatim instead of growing a second,
    drifting inserter. Returns the four counters described under COUNTERS below.

    BOUNDED MEMORY + WALL TIME (2026-07-29, chip task_67a4223c -- follows the
    tm.import_tmx pattern; see its docstring for the fuller measured reasoning).
    This used to hold ONE LIVE GlossaryEntry per DISTINCT term for the WHOLE
    import (a `term -> ORM object` dict that never shrank) and COMMIT once per
    brand-new entry -- a real memory problem at the 100 MiB TBX upload cap (the
    Microsoft Terminology termbase is ~54 MB / hundreds of thousands of terms) on
    the 1 GB Fly machine, and a real WALL-TIME problem separately: a commit is a
    transaction boundary (fsync under SQLite's default journal mode), not just a
    write, so committing once per new term paid that cost per term instead of
    once for the whole file.

    Now: pre-existing entries in this (project|glossary, locale-pair) scope are
    preloaded ONCE as PRIMITIVES (id/target_term/dnt/meta -- a column-only SELECT,
    never a GlossaryEntry instance), mirroring tm.import_tmx's `existing_by_hash`.
    A brand-new term gets a real ORM object (needed so SQLAlchemy assigns its
    autoincrement id) held ONLY until the next batch boundary, where it is
    FLUSHED (not committed -- no transaction boundary, no fsync, everything stays
    inside one open SAVEPOINT) and immediately EXPUNGED; every further row for
    that term is then a primitives-only read/compute plus a queued bulk UPDATE,
    exactly like a term that pre-existed before this call. GlossarySense rows are
    batched the same way (flushed + expunged, never held for the whole file).

    All of it runs inside ONE SAVEPOINT (session.begin_nested()), released with a
    single COMMIT at the end -- so a mid-file failure (a truncated TBX raising
    partway through glossary_tbx.parse_rows's generator, now that it streams
    instead of materializing every row upfront) rolls back every batch already
    flushed, not just the unflushed tail; a CSV's csv.DictReader essentially never
    raises mid-iteration, but the guarantee is uniform across both formats since
    they share this one write path. See tm.import_tmx's docstring for the two
    caveats that apply here unchanged and are not re-derived: the pysqlite
    savepoint-is-the-real-transaction quirk on the success path (our
    savepoint.commit() below IS the durable point when the caller session held no
    prior pending DML, which is every shipped caller).

    ISSUE #584 (supersedes the batching/flush description above where they differ):
    the import is TWO-PHASE. Phase 1 parses the whole file and merges it into `state`
    with NO writes (a brand-new term is just a dict; a sense row on a brand-new term
    needs no id yet). Phase 2 takes the SQLite write lock (import_lock), re-checks
    that the entries phase 1 relied on did not change meanwhile, bulk-INSERTs the new
    entries (ids come back with RETURNING), bulk-UPDATEs the touched existing ones,
    inserts/updates senses, reconciles the concept store and commits ONCE. All-or-
    nothing is unchanged; the write lock is held for the bulk write, not the parse.

    A SENSE ROW WHOSE TERM IS BRAND NEW THIS BATCH needs its owning entry's real
    (flushed) id for the FK -- there is no ORM relationship wired between
    GlossaryEntry and GlossarySense to defer that resolution, so such a row
    triggers one ordinary flush (never a commit) on the spot. TBX never reaches
    this path (a TBX import creates no senses at all -- glossary_tbx module
    docstring); a hand-authored CSV whose very first row for a never-before-seen
    term happens to be a sense row does, and it costs one flush, same as any
    other batch boundary.

    DEDUPE (unchanged): repeated rows for the same source_term keep mutating the
    SAME entry -- last non-blank target_term/dnt/meta values win, including
    across a flush/batch boundary (a duplicate whose two occurrences straddle one
    resolves as an UPDATE of the same id, never a second INSERT -- pinned by
    tests/verify_glossary_import_bounded_memory.py's straddling-duplicate test).
    SENSE ROWS STILL NEVER CLOBBER THE BASE ENTRY'S META (2026-07-28 fix,
    preserved): meta is a property of the TERM, and the term's authority is its
    BASE row, so a sense row only fills keys still absent while a base row keeps
    writing authoritatively.

    SENSES ARE UPSERTED BY (entry, sense_label) (2026-07-29 -- was a blind
    INSERT). Re-importing the same CSV used to add a SECOND GlossarySense row for
    every sense row in the file, then a third, growing without bound and leaving
    the sense selector choosing between identical duplicates. It also made every
    sense-bearing re-import look like it created content (`senses` > 0), which is
    exactly the signal `glossary_module.import_bytes` gates its approval revert
    on -- so the duplication bug was silently masking the idempotency it is
    supposed to preserve. Now a sense row whose label already exists on the term
    UPDATES that row's context_cue/target_term (and counts as a CHANGE only when
    one of them actually differs); a new label still inserts and still counts
    under `senses`. A term's existing senses are loaded LAZILY -- one column-only
    SELECT the first time a sense row for that term arrives, never for a term
    this call created (it has none by construction) and never at all for a TBX
    import or a sense-free CSV, which are the overwhelmingly common shapes. A
    term that already carries DUPLICATE rows for one label (written before this
    fix -- i.e. every sense-bearing glossary that was ever re-imported) is
    CONVERGED: the upsert writes the file's values to EVERY row for that label,
    so the duplicates stop disagreeing. Rows are never DELETED by an import.
    Writing only the LAST of them (the first version of this fix) left the
    earlier copy holding the OLD value and still selectable -- nothing
    downstream prefers the last row, and `_cue_fallback` in fact abstains
    entirely when two rows share a label and a cue -- so an import that reported
    success left the term resolving to a value the file no longer contained
    (reproduced, adversarial review 2026-07-29). Converging DIVERGENT duplicates
    counts as a `changed` row even when the row is otherwise byte-identical,
    because the term's effective translation really does move.

    COUNTERS (BUG-09 QA finding, 2026-07-29 -- "reported counts don't add up"):
    this used to return only {"entries": n, "senses": n}, where `entries` counts
    a DISTINCT brand-new term exactly ONCE (on its first row) no matter how many
    further rows touch it. Every OTHER row that referenced an EXISTING term --
    one that already existed in the DB before this call, or one this SAME call
    already created on an earlier row -- contributed to NEITHER counter, so a
    re-import that updated 500 existing terms reported "0 entries, 0 senses" even
    though the file plainly had 500 usable rows. Now also returns:
      "updated":      rows that touched a term NOT newly created by this row --
                      i.e. every row for a term after the one that created it (a
                      sense row or a base row alike), plus every row for a term
                      that pre-existed the whole call. Blank-source_term rows are
                      excluded (see "skipped_rows") and never counted anywhere.
                      NOTE this is a ROW count, not a CHANGE count: a row that
                      rewrites a term with byte-identical values still counts.
                      That is deliberately NOT tm.import_tmx's `updated` (which
                      means "the stored value actually changed" and pairs with a
                      `skipped` for true no-ops) -- the two importers use the
                      same word for different things, so do not build a
                      "something really changed" decision on THIS counter; use
                      "changed" below, which does carry tm's meaning.
      "changed":      the subset of "updated" whose row ACTUALLY ALTERED stored
                      state (2026-07-29) -- tm.import_tmx's `updated` semantics,
                      under a name that does not collide with the row count this
                      importer has always published. A row is a change when it
                      moves the term's target_term, dnt or meta, or when it
                      inserts a new sense or edits an existing sense's
                      context_cue/target_term. (`case_sensitive` is deliberately
                      absent: no import format can write it -- there is no
                      _CSV_COLUMNS entry and no TBX mapping -- so it can never
                      differ across an import.) THE POINT of this counter is
                      `glossary_module.import_bytes`'s approval revert: an
                      idempotent re-import of an APPROVED glossary must leave it
                      approved (a workflow step pinned to it requires APPROVED),
                      while an import that rewrites even one target must revert
                      it to draft.
                      Comparison is PER ROW against the state the row found --
                      exactly tm.import_tmx's per-<tu> rule -- not against the
                      DB state at the start of the call. So a single file that
                      writes A then B then A back for one term reports 2 changes
                      even though the net effect is nil. That is the conservative
                      direction (it can only over-report a change, never miss
                      one) and it keeps the counter a row-level fact that
                      composes with the identities below.
      "unchanged":    the rest of "updated" -- rows that touched a known term and
                      left every stored value byte-identical (tm.import_tmx's
                      `skipped`, again renamed to avoid this module's TBX-only
                      "skipped" key). Surfaced alongside "changed" so a no-op
                      re-import reports honestly rather than looking like it did
                      nothing at all. Sub-identity: `changed + unchanged ==
                      updated`.
      "skipped_rows": rows whose source_term was blank and were dropped without
                      touching any entry (previously silently uncounted). NAMED
                      APART FROM "skipped" ON PURPOSE (review round 1): every
                      user-facing surface already renders an import's "skipped"
                      as "entries with no matching language pair", which is a
                      TBX parse-level fact. Reusing that key for blank CSV rows
                      made a CSV whose trailing rows are empty -- what Excel
                      emits constantly -- report "Skipped 2 entries with no
                      matching language pair" on the project page, the glossary
                      page and both CLI import verbs (reproduced).
    The reconciling identity this restores: `entries + updated + skipped_rows`
    equals the number of rows the caller handed in (`senses` is an ORTHOGONAL tag
    on some of those same rows, not a fourth term of the sum -- a sense row is
    counted under `entries` or `updated` exactly like any other row, AND under
    `senses`), with `changed + unchanged == updated` splitting the middle term.
    NOTHING WAS RENAMED to add the split: "entries"/"senses"/"updated"/
    "skipped_rows" all keep their exact meanings and their exact keys, because
    this dict is splatted verbatim into the /api import responses and both CLI
    `--json` payloads, and `updated` ("rows that touched a known term") is still
    the honest total. The two new keys are purely additive.
    `import_csv`'s public return stays the narrower legacy
    {"entries", "senses"} shape (see its docstring; `import_csv_full` is the
    same write with this full dict); `import_bytes` -- the modern seam every
    user-facing surface calls -- surfaces this fuller dict.

    WHAT DOES *NOT* RECONCILE, and must not be claimed to (review round 1): for
    TBX, `glossary_tbx.parse_rows`'s `stats["concepts"]`. One conceptEntry emits
    ONE ROW PER DISTINCT SOURCE TERM in its source langSet, and listing synonyms
    in one concept is the normal shape of a real termbase (both MS Terminology
    and IATE do it). A single 3-synonym concept therefore yields entries=3 with
    concepts=1. The only true relation is the INEQUALITY
    `entries + updated >= concepts - parse-level skipped` (equality exactly when
    every usable concept contributes one source term), pinned by
    tests/verify_glossary_import_bounded_memory.py's synonym-concept test."""
    if glossary_id is not None:
        scope = (GlossaryEntry.glossary_id == glossary_id,)
    else:
        scope = (GlossaryEntry.project_id == project_id, GlossaryEntry.glossary_id == None)  # noqa: E711

    n_entries = 0
    n_senses = 0
    n_updated = 0
    n_changed = 0
    n_unchanged = 0
    n_skipped_rows = 0

    # PRIMITIVES-only cache: term -> {"id", "target_term", "dnt", "meta", "senses"}.
    # "id" is None for a term this call created until phase 2 inserts it (#584).
    # "senses" is the term's sense_label -> primitives map, ABSENT until the
    # first sense row for that term forces a lazy load (see _sense_state); a
    # term this call created gets {} at birth and is never queried.
    # #615: the highest entry id BEFORE the scope is read. The chunks look for entries other
    # writers create meanwhile only above it (ids only grow, and the table has no index on
    # source_term, so a lookup by name would scan the whole scope once per chunk).
    base_max = session.execute(sa.select(sa.func.max(GlossaryEntry.id))).scalar() or 0
    state: dict[str, dict] = {}
    for term, eid, tt, dnt, meta in session.exec(
        select(
            GlossaryEntry.source_term,
            GlossaryEntry.id,
            GlossaryEntry.target_term,
            GlossaryEntry.dnt,
            GlossaryEntry.meta,
        ).where(
            *scope,
            GlossaryEntry.source_locale == source_locale,
            GlossaryEntry.target_locale == target_locale,
        )
    ):
        state[term] = {
            "id": eid,
            "target_term": tt or "",
            "dnt": bool(dnt),
            "meta": meta or {},
        }

    # Issue #584: PHASE 1 (everything up to the loop's end) is READ-ONLY. The whole file
    # is parsed and merged into `state` in memory; nothing is written, so no other writer
    # waits on this import and a bad file costs nothing. PHASE 2 (after the loop,
    # `_write_chunks`) writes the result in chunks, each its own short transaction that
    # first RE-READS its rows under the write lock (#615), so what phase 1 read is only
    # the plan, never the thing written from.
    # Pre-existing entries this call touched, as first read: term -> (id, target_term,
    # dnt, meta). `meta` is stored by reference, which is safe: the merge below REBINDS
    # st["meta"] to a new dict and never mutates one in place. A chunk compares it with the
    # fresh read to learn that another connection changed the row meanwhile (the counters
    # are then re-classified against what the file really did to the stored row).
    snapshot: dict[str, tuple] = {}
    # Senses loaded for pre-existing entries, as first read: entry id -> {label: (ids,
    # context_cue, target_term)}; compared the same way as `snapshot`.
    sense_snapshot: dict[int, dict] = {}
    new_terms: list[str] = []  # brand-new terms, in first-seen order
    dirty_existing: dict[int, dict] = {}  # id -> entry state to UPDATE

    def _entry_state(term: str) -> dict:
        nonlocal n_entries
        st = state.get(term)
        if st is not None:
            return st
        st = {
            "id": None,
            "target_term": "",
            "dnt": False,
            "meta": {},
            "senses": {},  # brand-new term: no senses to load, ever
        }
        state[term] = st
        new_terms.append(term)
        n_entries += 1
        return st

    def _sense_state(st: dict) -> dict:
        """The term's sense_label -> {"ids","context_cue","target_term",
        "divergent"} map, loaded lazily (a column-only SELECT, never a
        GlossarySense instance) the first time a sense row for that term
        arrives. Absent from `state` until then, so a TBX import (no senses at
        all) and a sense-free CSV never pay for it.

        A pre-existing DUPLICATE label -- rows this importer wrote before the
        upsert fix, which is EVERY sense-bearing glossary that was ever
        re-imported -- collapses to ONE state entry carrying ALL of its ids and
        the LAST row's values, plus `divergent` when the copies disagree.
        An upsert then writes EVERY id (adversarial review, 2026-07-29): writing
        only the last left the earlier copy holding the OLD target, and nothing
        downstream prefers the last -- `_choose_sense` offers the model both
        rows as separate options and `_cue_fallback` sees two cue hits for one
        label and gives up on the sense entirely -- so a "successful" import
        left the term answering with a value the file no longer contained.
        Rows are never DELETED by an import; converging them is the strongest
        repair an importer is allowed to make."""
        ss = st.get("senses")
        if ss is not None:
            return ss
        ss = {}
        for sid, label, cue, tgt in session.exec(
            select(
                GlossarySense.id,
                GlossarySense.sense_label,
                GlossarySense.context_cue,
                GlossarySense.target_term,
            )
            .where(GlossarySense.glossary_entry_id == st["id"])
            .order_by(GlossarySense.id)
        ):
            cur = ss.get(label)
            vals = (cue or "", tgt or "")
            if cur is None:
                ss[label] = {
                    "ids": [sid],
                    "context_cue": vals[0],
                    "target_term": vals[1],
                    "divergent": False,
                }
            else:
                cur["ids"].append(sid)
                cur["divergent"] = (
                    cur["divergent"] or (cur["context_cue"], cur["target_term"]) != vals
                )
                cur["context_cue"], cur["target_term"] = vals
        st["senses"] = ss
        if st["id"] is not None:
            sense_snapshot[st["id"]] = {
                label: (tuple(v["ids"]), v["context_cue"], v["target_term"])
                for label, v in ss.items()
            }
        return ss

    def _write_through(st: dict) -> None:
        """Queue st's final target_term/dnt/meta for the bulk UPDATE of phase 2 (a
        pre-existing term). A brand-new term needs nothing here: it is INSERTed from its
        final state."""
        if st["id"] is not None:
            dirty_existing[st["id"]] = st

    # LAW-TERM-18 (A.2-d): the bulk-adapter tail (`_write_chunks`) pins meta_overrides and
    # resolves target_term_id for TOUCHED rows only (never an untouched pre-existing row --
    # R7-A): the entries each chunk writes are exactly the ones the file touched.
    _n_rows = 0
    for row in rows:
        _n_rows += 1
        if _n_rows > config.MAX_DOCUMENT_ROWS:
            # #576: one import may not carry more rows than MAX_DOCUMENT_ROWS. Raised in
            # phase 1, so nothing was written.
            raise InputLimitError(
                f"This glossary import has more than {config.MAX_DOCUMENT_ROWS} rows "
                "(MAX_DOCUMENT_ROWS). Nothing was imported. Split it into smaller files."
            )
        term = (row.get("source_term") or "").strip()
        if not term:
            n_skipped_rows += 1
            continue
        # COUNTERS: is this term already known (pre-existing before this
        # call, or created by an EARLIER row of this same call)? If so, this
        # row is an "update" of it, whichever branch below it takes -- checked
        # BEFORE _entry_state, which would otherwise create+register it.
        pre_existing = term in state
        if on_row is not None:
            # Per-entry import report (termbase-import train): tells the caller whether
            # this row created a term or merged into one that already existed (in the
            # glossary, or earlier in the same file). Called BEFORE the row's meta is
            # read, so the TBX caller may fold its generated do-not-use line into the
            # row's notes here (glossary_tbx.merge_dont_use); nothing else may change.
            on_row(row, pre_existing)
        st = _entry_state(term)
        if pre_existing and st["id"] is not None and st["id"] not in snapshot:
            snapshot[st["id"]] = (st["id"], st["target_term"], st["dnt"], st["meta"])
        # CHANGE DETECTION (see the COUNTERS docstring section): snapshot the
        # stored values this row FOUND, so "did this row alter anything?" is
        # answered by comparison, not by which branch ran. `meta` is copied
        # because the merge below rebinds it to a NEW dict only when the row
        # actually carries meta cells.
        before = (st["target_term"], st["dnt"], dict(st["meta"] or {}))
        row_changed = False
        sense_label = (row.get("sense_label") or "").strip()
        # Phase 4: merge any TBX-ish meta cells on this row into the term's
        # running meta state (empty for a legacy CSV -> untouched). A sense
        # row only FILLS KEYS STILL ABSENT (2026-07-28 fix, preserved -- see
        # the docstring); a base row keeps writing authoritatively.
        gmeta = _meta_from_row(row)
        if gmeta:
            prior = st["meta"] or {}
            st["meta"] = {**gmeta, **prior} if sense_label else {**prior, **gmeta}
        # #615: what the FILE says about a pre-existing term, kept apart from the stored
        # values it was merged onto. A chunk replays it on the row as stored when the chunk
        # writes, so the file wins over an edit made while the import ran, and a field the
        # file leaves blank keeps the CURRENT value, not the one phase 1 read. (A brand-new
        # term needs none: its final state is the file's.)
        delta = None
        if st["id"] is not None:
            delta = st.setdefault("f", {})
            if gmeta:
                if sense_label:
                    for k, v in gmeta.items():
                        delta.setdefault("ms", {}).setdefault(k, v)
                else:
                    delta.setdefault("mb", {}).update(gmeta)
        if sense_label:
            sense_target = (row.get("target_term") or "").strip()
            sense_cue = (row.get("context_cue") or "").strip()
            if delta is not None:
                delta.setdefault("sn", {})[sense_label] = (sense_cue, sense_target)
                if sense_target:
                    delta.setdefault("fill", sense_target)
            senses = _sense_state(st)
            prior_sense = senses.get(sense_label)
            if prior_sense is None:
                sense_st = {
                    "ids": [],
                    "context_cue": sense_cue,
                    "target_term": sense_target,
                    "new": True,
                    "label": sense_label,
                    "divergent": False,
                }
                senses[sense_label] = sense_st
                n_senses += 1
                row_changed = True
            elif prior_sense["divergent"] or (
                prior_sense["context_cue"],
                prior_sense["target_term"],
            ) != (sense_cue, sense_target):
                # UPSERT (not a second INSERT): the label already exists on
                # this term, and its cue/target actually differ -- or the
                # term carries pre-fix DUPLICATE rows for this label that
                # disagree with each other, in which case the write happens
                # even for an otherwise byte-identical row, because
                # converging them really does change what the term resolves
                # to (see _sense_state).
                prior_sense["context_cue"] = sense_cue
                prior_sense["target_term"] = sense_target
                prior_sense["divergent"] = False
                row_changed = True
            # else: a byte-identical sense row -- nothing written, nothing
            # counted. This is what makes an idempotent re-import of a
            # sense-bearing CSV a true no-op.
            #
            # Bug #6 backfill (preserved): a term whose ONLY rows are sense
            # rows still needs a base target_term (else the grid/lookup,
            # which reads target_term first, shows blank). A later plain row
            # still overrides it explicitly; a later sense row never clobbers
            # a base a plain row (or an earlier sense) already set.
            if not st["target_term"] and sense_target:
                st["target_term"] = sense_target
        else:
            if delta is not None and row.get("target_term"):
                delta["tgt"] = row["target_term"].strip()
            st["target_term"] = (row.get("target_term") or st["target_term"]).strip()
            # Issue #497: a BLANK or missing dnt cell keeps the stored flag --
            # the docstring's "last NON-BLANK value wins", mirroring the
            # target_term line above. Reparsing an empty cell used to set
            # dnt=False, so a re-import that carries no dnt column at all (or
            # a vendor TBX with no doNotTranslate marker -- glossary_tbx
            # emits dnt="" for a markerless row) silently un-flagged a DNT
            # term and reverted the approved glossary. Only a non-blank cell
            # writes; an explicit false/0/no still clears the flag.
            # `or ""` (not a `, ""` default) so a SHORT csv row -- DictReader
            # fills a missing trailing column with None, and str(None) is the
            # truthy "None", which would read as a non-blank cell and clear
            # the flag all over again.
            dnt_cell = str(row.get("dnt") or "").strip()
            if dnt_cell:
                st["dnt"] = dnt_cell.lower() in ("1", "true", "yes", "y")
                if delta is not None:
                    delta["dnt"] = st["dnt"]
        if (st["target_term"], st["dnt"], st["meta"] or {}) != before:
            row_changed = True
        # The write itself stays UNCONDITIONAL even for a no-op row: the
        # counters are a new, separate fact and this train deliberately does
        # not alter which statements the importer emits (a redundant UPDATE
        # of identical values is harmless; skipping it would be a behaviour
        # change riding along on an approval-semantics fix).
        _write_through(st)
        if pre_existing:
            n_updated += 1
            if row_changed:
                n_changed += 1
            else:
                n_unchanged += 1
            if st["id"] is not None:
                # per-entry row counts, so a chunk can take them back out if another writer
                # deleted the entry meanwhile (or re-classify them if it edited it)
                key = "rc" if row_changed else "ru"
                st[key] = st.get(key, 0) + 1

    # ---- PHASE 2 (Issues #584, #615): chunked write transactions. -------------------
    # The file parsed and merged cleanly, so everything from here is bulk SQL. #584 wrote it
    # all in ONE transaction (all-or-nothing, the write lock held for the whole write: 3.5 s
    # at 100k project-glossary rows, 84 s at 100k MODULE rows, whose concept reconcile and
    # binding pass dominate). #615 bounds the hold: the work goes out in chunks of at most
    # GLOSSARY_WRITE_CHUNK_ROWS (module glossaries: GLOSSARY_MODULE_CHUNK_ROWS) entries, each
    # its own short transaction that takes the write lock, RE-READS its rows, writes, commits
    # and releases it, then pauses GLOSSARY_CHUNK_PAUSE_S so a waiting writer gets in. See
    # `_write_chunks` for the exact atomicity and concurrent-writer rules (ADR 0061).
    counters = {
        "entries": n_entries,
        "senses": n_senses,
        "updated": n_updated,
        "changed": n_changed,
        "unchanged": n_unchanged,
        "skipped_rows": n_skipped_rows,
        "bindings_pinned": 0,
        "concurrent_deleted": 0,
        "write_chunks": 0,
    }
    _write_chunks(
        session,
        scope=scope,
        project_id=project_id,
        glossary_id=glossary_id,
        source_locale=source_locale,
        target_locale=target_locale,
        state=state,
        new_terms=new_terms,
        dirty_existing=dirty_existing,
        snapshot=snapshot,
        sense_snapshot=sense_snapshot,
        counters=counters,
        watermark=base_max,
    )
    return counters


def _iter_chunks(new_terms: list, existing_items: list, state: dict, limit: dict):
    """Yield (new terms, pre-existing items, weight) chunks: new entries first, then the
    pre-existing ones, each weighing one unit plus the senses it carries. `limit["rows"]` is
    read at the START of every chunk (the driver adapts it between chunks), and an item heavier
    than the limit gets a chunk of its own."""
    units = [("n", t, 1 + len(state[t].get("senses") or ())) for t in new_terms]
    units += [("e", it, 1 + len((it[1].get("f") or {}).get("sn") or ())) for it in existing_items]
    i = 0
    while i < len(units):
        cur_new: list = []
        cur_ex: list = []
        used = 0
        budget = max(1, int(limit["rows"]))
        while i < len(units) and (not used or used + units[i][2] <= budget):
            kind, payload, weight = units[i]
            (cur_new if kind == "n" else cur_ex).append(payload)
            used += weight
            i += 1
        yield cur_new, cur_ex, used


def _replay(cur_target: str, cur_dnt: bool, cur_meta: dict, d: dict) -> tuple:
    """What the FILE does to an entry as it is stored NOW: phase 1's merge rules (last
    non-blank base target and dnt win, a sense row only fills a still-empty target and
    still-absent meta keys, a base row's meta keys always win) replayed on the fresh row, so a
    field the file leaves blank keeps its current value."""
    target = d.get("tgt", cur_target)
    if not target and d.get("fill"):
        target = d["fill"]
    dnt = d.get("dnt", cur_dnt)
    meta = dict(cur_meta)
    if d.get("mb"):
        meta.update(d["mb"])
    for k, v in (d.get("ms") or {}).items():
        if k not in meta:
            meta[k] = v
    return target, dnt, meta


def _load_sense_map(session: Session, entry_ids: list) -> dict:
    """entry id -> {label: {"ids", "cue", "tgt", "divergent"}} as stored now (the shape
    `_sense_state` builds in phase 1: a label stored twice collapses to one record carrying
    every id and the last row's values)."""
    out: dict[int, dict] = {eid: {} for eid in entry_ids}
    for i in range(0, len(entry_ids), 500):
        for sid, eid, label, cue, tgt in session.execute(
            sa.select(
                GlossarySense.id,
                GlossarySense.glossary_entry_id,
                GlossarySense.sense_label,
                GlossarySense.context_cue,
                GlossarySense.target_term,
            )
            .where(GlossarySense.glossary_entry_id.in_(entry_ids[i : i + 500]))
            .order_by(GlossarySense.id)
        ):
            vals = (cue or "", tgt or "")
            g = out[eid].get(label)
            if g is None:
                out[eid][label] = {"ids": [sid], "cue": vals[0], "tgt": vals[1], "divergent": False}
            else:
                g["ids"].append(sid)
                g["divergent"] = g["divergent"] or (g["cue"], g["tgt"]) != vals
                g["cue"], g["tgt"] = vals
    return out


def _chunk_write(
    session: Session,
    *,
    part_new: list,
    part_ex: list,
    state: dict,
    scope: tuple,
    project_id: int | None,
    glossary_id: int | None,
    source_locale: str,
    target_locale: str,
    snapshot: dict,
    sense_snapshot: dict,
    adj: dict,
    track: dict,
) -> tuple[list, int]:
    """Write ONE chunk under the write lock the caller already holds. Returns (ids of the
    entries this chunk covers, number of rows it changed in the database).

    Every row is RE-READ here and decided on what is stored NOW, never on phase 1's picture
    (the review gate's TMX finding): a pre-existing entry gets the file's values replayed on its
    current row (`_replay`), so an edit made while the import ran is overwritten by the file
    where the file speaks (as #584's re-run did) and kept where the file is silent; a new
    entry another writer created first is UPDATED, not inserted twice; an entry deleted
    meanwhile is NOT brought back and is counted in `concurrent_deleted`. `adj` receives the
    count corrections, applied by the caller only if the chunk commits."""
    # ---- re-read (FOR UPDATE where the backend has row locks; SQLite's write lock covers it)
    cur_by_id: dict[int, tuple] = {}
    ids = [eid for eid, _ in part_ex]
    for i in range(0, len(ids), 500):
        for eid, tt, dnt, meta in session.execute(
            sa.select(
                GlossaryEntry.id, GlossaryEntry.target_term, GlossaryEntry.dnt, GlossaryEntry.meta
            )
            .where(GlossaryEntry.id.in_(ids[i : i + 500]))
            .with_for_update()
        ):
            cur_by_id[eid] = (tt or "", bool(dnt), meta or {})
    # Entries OTHER writers created since phase 1 sit in the gaps between our own chunks' ids
    # (ids only grow and one writer at a time holds the lock), so only the ids between the end of
    # our previous chunk and now are read -- never the whole scope, the table has no index on
    # source_term. They are remembered in `track["foreign"]` and matched against this and every
    # later chunk's planned-new terms.
    now_max = session.execute(sa.select(sa.func.max(GlossaryEntry.id))).scalar() or 0
    foreign: dict[str, tuple] = track["foreign"]
    if now_max > track["last_end"]:
        for term, eid, tt, dnt, meta in session.execute(
            sa.select(
                GlossaryEntry.source_term,
                GlossaryEntry.id,
                GlossaryEntry.target_term,
                GlossaryEntry.dnt,
                GlossaryEntry.meta,
            )
            .where(
                *scope,
                GlossaryEntry.id > track["last_end"],
                GlossaryEntry.id <= now_max,
                GlossaryEntry.source_locale == source_locale,
                GlossaryEntry.target_locale == target_locale,
            )
            .order_by(GlossaryEntry.id)
        ):
            foreign.setdefault(term, (eid, tt or "", bool(dnt), meta or {}))
    # a candidate may have been deleted since it was noticed: re-read it by id, keep the live ones
    cand = {t: foreign[t] for t in part_new if t in foreign}
    late: dict[str, tuple] = {}
    if cand:
        wanted = sorted({v[0] for v in cand.values()})
        fresh_rows: dict[int, tuple] = {}
        for i in range(0, len(wanted), 500):
            for eid, tt, dnt, meta in session.execute(
                sa.select(
                    GlossaryEntry.id,
                    GlossaryEntry.target_term,
                    GlossaryEntry.dnt,
                    GlossaryEntry.meta,
                )
                .where(GlossaryEntry.id.in_(wanted[i : i + 500]))
                .with_for_update()
            ):
                fresh_rows[eid] = (tt or "", bool(dnt), meta or {})
        for term, (eid, *_old) in cand.items():
            if eid in fresh_rows:
                late[term] = (eid, *fresh_rows[eid])
            else:
                del foreign[term]

    # ---- the entries that exist now: (entry id, entry state, file delta, label -> (cue, tgt))
    live: list[tuple] = []
    for eid, st in part_ex:
        cur = cur_by_id.get(eid)
        d = st.get("f") or {}
        if cur is None:
            rc, ru = st.get("rc", 0), st.get("ru", 0)
            adj["updated"] -= rc + ru
            adj["changed"] -= rc
            adj["unchanged"] -= ru
            adj["senses"] -= sum(
                1 for label in d.get("sn", {}) if (st.get("senses") or {}).get(label, {}).get("new")
            )
            adj["concurrent_deleted"] += 1
            continue
        live.append((eid, st, d, d.get("sn") or {}, False))
    inserts: list[str] = []
    for term in part_new:
        st = state[term]
        found = late.get(term)
        if found is None:
            inserts.append(term)
            continue
        eid, tt, dnt, meta = found
        cur_by_id[eid] = (tt, dnt, meta)
        st["id"] = eid
        d = {}
        if st["target_term"]:
            d["tgt"] = st["target_term"]
        if st["dnt"]:
            d["dnt"] = True
        if st["meta"]:
            d["mb"] = st["meta"]
        labels = {
            label: (s["context_cue"], s["target_term"]) for label, s in (st["senses"] or {}).items()
        }
        live.append((eid, st, d, labels, True))
        adj["entries"] -= 1

    sense_maps = _load_sense_map(session, [eid for eid, _s, _d, labels, _l in live if labels])

    # ---- decide, per live entry, what the file changes on the row as stored now
    row_updates: list[dict] = []
    sense_updates: dict[int, dict] = {}
    sense_inserts: list[dict] = []
    for eid, st, d, labels, was_new in live:
        cur_t, cur_dnt, cur_meta = cur_by_id[eid]
        final_t, final_dnt, final_meta = _replay(cur_t, cur_dnt, cur_meta, d)
        fresh = sense_maps.get(eid, {})
        entry_changed = (final_t, final_dnt, final_meta) != (cur_t, cur_dnt, cur_meta)
        sense_changed = False
        for label, (cue, tgt) in labels.items():
            g = fresh.get(label)
            phase1_new = bool((st.get("senses") or {}).get(label, {}).get("new"))
            if g is None:
                sense_inserts.append(
                    {
                        "glossary_entry_id": eid,
                        "sense_label": label,
                        "context_cue": cue,
                        "target_term": tgt,
                    }
                )
                sense_changed = True
                if not phase1_new:  # stored when phase 1 looked, gone now: stored again
                    adj["senses"] += 1
            else:
                if phase1_new:  # another writer stored this label first: updated, not added
                    adj["senses"] -= 1
                if g["divergent"] or (g["cue"], g["tgt"]) != (cue, tgt):
                    for sid in g["ids"]:
                        sense_updates[sid] = {"context_cue": cue, "target_term": tgt}
                    sense_changed = True
        changed_now = entry_changed or sense_changed
        if entry_changed:
            row_updates.append(
                {"id": eid, "target_term": final_t, "dnt": final_dnt, "meta": final_meta or None}
            )
        if was_new:
            # Phase 1 counted this row as a new entry; it is an update of the row that is there.
            adj["updated"] += 1
            adj["changed" if changed_now else "unchanged"] += 1
            continue
        snap = snapshot.get(eid)
        moved = snap is not None and snap[1:] != (cur_t, cur_dnt, cur_meta)
        if not moved and eid in sense_snapshot and labels:
            moved = {
                label: (tuple(g["ids"]), g["cue"], g["tgt"]) for label, g in fresh.items()
            } != sense_snapshot[eid]
        if moved:
            # Another writer changed the row after phase 1 read it: re-classify its rows by
            # what the file really did to the stored row.
            rc, ru = st.get("rc", 0), st.get("ru", 0)
            if rc and not changed_now:
                adj["changed"] -= rc
                adj["unchanged"] += rc
            elif not rc and changed_now and ru:
                adj["changed"] += 1
                adj["unchanged"] -= 1

    # ---- write
    step = GLOSSARY_IMPORT_FLUSH_BATCH
    written = 0
    for i in range(0, len(row_updates), step):
        session.execute(sa.update(GlossaryEntry), row_updates[i : i + step])
    written += len(row_updates)
    new_ids: list[int] = []
    # #615: SQLite degrades RETURNING to one INSERT per row. We hold the single write lock
    # here, so insert each batch with ONE executemany (ids still DB-assigned) and read the new
    # ids back: rowids are handed out in insertion order, so the rows past the previous max id
    # are exactly this batch. Other backends keep RETURNING.
    for i in range(0, len(inserts), step):
        batch = inserts[i : i + step]
        insert_stmt = sa.insert(GlossaryEntry)
        base_id = 0
        if config.IS_SQLITE:
            base_id = session.execute(sa.select(sa.func.max(GlossaryEntry.id))).scalar() or 0
        else:
            insert_stmt = insert_stmt.returning(GlossaryEntry.id, sort_by_parameter_order=True)
        result = session.execute(
            insert_stmt,
            [
                {
                    "project_id": project_id if glossary_id is None else None,
                    "glossary_id": glossary_id,
                    "source_locale": source_locale,
                    "target_locale": target_locale,
                    "source_term": t,
                    "target_term": state[t]["target_term"],
                    "dnt": state[t]["dnt"],
                    "case_sensitive": False,
                    "meta": state[t]["meta"] or None,
                }
                for t in batch
            ],
        )
        if config.IS_SQLITE:
            got = list(
                session.execute(
                    sa.select(GlossaryEntry.id)
                    .where(GlossaryEntry.id > base_id)
                    .order_by(GlossaryEntry.id)
                ).scalars()
            )
        else:
            got = [row[0] for row in result.all()]
        if len(got) != len(batch):
            raise RuntimeError("glossary import: bulk insert returned the wrong id count")
        for t, eid in zip(batch, got, strict=True):
            st = state[t]
            st["id"] = eid
            new_ids.append(eid)
            for label, s in (st["senses"] or {}).items():
                sense_inserts.append(
                    {
                        "glossary_entry_id": eid,
                        "sense_label": label,
                        "context_cue": s["context_cue"],
                        "target_term": s["target_term"],
                    }
                )
    written += len(inserts)
    for i in range(0, len(sense_inserts), step):
        session.execute(sa.insert(GlossarySense), sense_inserts[i : i + step])
    upd = list(sense_updates.items())
    for i in range(0, len(upd), step):
        session.execute(
            sa.update(GlossarySense), [{"id": sid, **cols} for sid, cols in upd[i : i + step]]
        )
    written += len(sense_inserts) + len(upd)
    track["last_end"] = max([now_max, *new_ids])
    return [eid for eid, *_ in live] + new_ids, written


def _write_chunks(
    session: Session,
    *,
    scope: tuple,
    project_id: int | None,
    glossary_id: int | None,
    source_locale: str,
    target_locale: str,
    state: dict,
    new_terms: list,
    dirty_existing: dict,
    snapshot: dict,
    sense_snapshot: dict,
    counters: dict,
    watermark: int,
) -> None:
    """PHASE 2 driver (#615, ADR 0061): write the planned entries in chunks, each its own
    short transaction (`import_lock.begin_write_phase`, `_chunk_write`, and for a MODULE
    glossary the concept/term reconcile and binding pass of just that chunk's entries, then
    commit), with a lock-free pause between chunks.

    Chunk size: at most GLOSSARY_WRITE_CHUNK_ROWS entries (module glossaries:
    GLOSSARY_MODULE_CHUNK_ROWS). The cost per entry is not constant (a module re-import that
    moves targets retires the old target terms, about 8 ms each, ten times a fresh insert), so a
    bigger file starts at a fifth of that and then adapts: each chunk is sized from how long the
    previous one held the lock, aiming at GLOSSARY_CHUNK_TARGET_S, never above the maximum.

    Atomicity, stated honestly: a bad file (parse error, row cap, unknown header, bad TBX)
    fails in phase 1 and stores nothing. A file whose work fits ONE chunk (at most the maximum)
    is exactly as atomic as before, and its module reconcile still covers the whole glossary. A
    bigger one that fails after at least one chunk committed keeps those chunks and raises
    GlossaryImportIncomplete (counts only; the full error goes to the server log). Importing the
    same file again finishes the job with no duplicates: entries are keyed by (scope, locales,
    source term), so the stored ones count as unchanged and only the rest is written. A failure
    in the FIRST chunk re-raises the original error and stores nothing."""
    from app.services import glossary_concepts

    max_rows = max(
        1, int(GLOSSARY_MODULE_CHUNK_ROWS if glossary_id is not None else GLOSSARY_WRITE_CHUNK_ROWS)
    )
    existing_items = list(dirty_existing.items())
    total = len(new_terms) + len(existing_items)
    if not total:
        # Nothing to write (an empty or all-blank file). A module glossary still reconciles
        # its concept store, inside a savepoint so a failure there rolls back.
        savepoint = None
        try:
            if glossary_id is not None:
                savepoint = import_lock.begin_write_phase(session, GlossaryEntry)
                glossary_concepts.reconcile_glossary_from_entries(session, glossary_id)
                counters["bindings_pinned"] = glossary_concepts.bulk_bind_touched(
                    session, glossary_id, set()
                )
        except BaseException:
            import_lock.rollback_quietly(session, savepoint)
            raise
        if savepoint is not None:
            savepoint.commit()
        session.commit()
        return
    weight = sum(1 + len(state[t].get("senses") or ()) for t in new_terms) + sum(
        1 + len((st.get("f") or {}).get("sn") or ()) for _eid, st in existing_items
    )
    single = weight <= max_rows
    adaptive = GLOSSARY_CHUNK_TARGET_S > 0 and not single
    limit = {"rows": max(1, max_rows // 5) if adaptive else max_rows}
    track = {"last_end": watermark, "foreign": {}}
    recon_cache: dict = {}
    if glossary_id is not None and not single:
        # The module's identity map is read ONCE, here, with the write lock still free.
        from app.services.glossary_concepts_part3 import prime_reconcile_cache

        prime_reconcile_cache(session, glossary_id, recon_cache)
    avg_unit = None
    handled = 0
    committed_writes = 0
    for n, (part_new, part_ex, used) in enumerate(
        _iter_chunks(new_terms, existing_items, state, limit)
    ):
        if n and GLOSSARY_CHUNK_PAUSE_S > 0:
            time.sleep(GLOSSARY_CHUNK_PAUSE_S)  # the write lock is free here
        savepoint = None
        c_adj = dict.fromkeys(
            ("entries", "senses", "updated", "changed", "unchanged", "concurrent_deleted"), 0
        )
        pinned = 0
        held_from = time.monotonic()
        try:
            savepoint = import_lock.begin_write_phase(session, GlossaryEntry)
            held_from = time.monotonic()  # the lock is ours now: time only what we hold it for
            ids, written = _chunk_write(
                session,
                part_new=part_new,
                part_ex=part_ex,
                state=state,
                scope=scope,
                project_id=project_id,
                glossary_id=glossary_id,
                source_locale=source_locale,
                target_locale=target_locale,
                snapshot=snapshot,
                sense_snapshot=sense_snapshot,
                adj=c_adj,
                track=track,
            )
            if glossary_id is not None:
                # Dual-write reconcile (LAW-TERM-2): mirror the concept/term store from the
                # just-written entries IN THE SAME savepoint. A one-chunk import reconciles the
                # whole glossary as before; a bigger one only the chunk's own entries.
                if single:
                    glossary_concepts.reconcile_glossary_from_entries(session, glossary_id)
                else:
                    glossary_concepts.reconcile_glossary_from_entries(
                        session, glossary_id, entry_ids=ids, cache=recon_cache
                    )
                # LAW-TERM-18 (A.2-d bulk exemption): pin meta_overrides + resolve
                # target_term_id for the TOUCHED rows, retire a superseded target (A.2-f), then
                # VERIFY byte-identity.
                pinned = glossary_concepts.bulk_bind_touched(session, glossary_id, set(ids))
            savepoint.commit()  # RELEASE SAVEPOINT -- the writes join the outer txn
            session.commit()  # ... and the commit frees the SQLite write lock
        except BaseException as exc:
            # ROLLBACK TO SAVEPOINT undoes this chunk and leaves the caller's own pending work
            # untouched; phase 1 wrote nothing.
            import_lock.rollback_quietly(session, savepoint)
            if n and isinstance(exc, Exception):
                # The full error goes to the log only: a database error text carries SQL and
                # bound row values (glossary terms), which must not reach a page, an API reply,
                # the CLI or an MCP answer.
                log.warning("glossary import stopped after %d chunks", n, exc_info=True)
                raise GlossaryImportIncomplete(
                    f"the glossary import stopped after {handled} of {total} entries were "
                    f"processed ({n} write chunks stored) because of a server-side error "
                    f"({type(exc).__name__}). Those entries are kept; importing the same file "
                    "again finishes the job and creates no duplicates.",
                    rows_written=handled,
                    rows_total=total,
                    content_changed=committed_writes > 0,
                ) from exc
            raise
        held = time.monotonic() - held_from
        for k, v in c_adj.items():
            counters[k] += v
        counters["bindings_pinned"] += pinned
        counters["write_chunks"] += 1
        committed_writes += written
        handled += len(part_new) + len(part_ex)
        if adaptive:
            # Size the next chunk from what the last ones cost (a running average, so one cheap
            # chunk does not unleash a slow one), aiming a little under the target hold and
            # growing by at most a quarter at a time.
            per_unit = max(held, 1e-6) / max(used, 1)
            avg_unit = per_unit if avg_unit is None else 0.5 * avg_unit + 0.5 * per_unit
            want = int(0.8 * GLOSSARY_CHUNK_TARGET_S / avg_unit)
            limit["rows"] = max(1, min(max_rows, want, int(limit["rows"] * 1.25) + 1))
            if (n + 1) % 10 == 0:
                log.info("glossary import: %d/%d entries stored (%d chunks)", handled, total, n + 1)
    if counters["concurrent_deleted"]:
        log.info(
            "glossary import: %d entries deleted by another writer during the import were "
            "not restored",
            counters["concurrent_deleted"],
        )


def import_bytes(
    session: Session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
    filename: str | None = None,
    *,
    strict: bool = False,
    concepts: bool = False,
) -> dict:
    """Import a glossary from CSV **or** TBX bytes — the single seam every surface
    calls (web import routes, CLI `glossary import` / `glossary module import`, and
    the /api handlers, local and remote alike).

    Returns _apply_rows's full counter set -- "entries", "senses", "updated",
    "changed", "unchanged" and "skipped_rows" (see _apply_rows's docstring for
    what each means and the reconciling identities they satisfy) -- plus "format"
    ("csv"|"tbx"), and for TBX additionally "concepts" (entries seen) and
    "skipped" (concepts dropped for want of a usable source/target pair).

    "skipped" IS TBX-ONLY AND STAYS TBX-ONLY (review round 1). It is
    glossary_tbx.parse_rows's parse-level count and nothing else, because that is
    exactly what every surface renders it as ("entries with no matching language
    pair" -- project page, glossary page, both CLI import verbs). A CSV import
    emits no "skipped" key at all, exactly as before this train; its blank-source
    rows are reported separately as "skipped_rows". Folding the two together made
    an ordinary Excel-exported CSV with trailing empty rows claim, in the UI,
    that N entries had "no matching language pair" (reproduced). The fold was
    also a no-op by construction -- glossary_tbx never emits a blank source_term,
    so the TBX arm's row-level count is always 0 -- so nothing is lost by keeping
    the two facts apart.

    "updated" (BUG-09 QA finding, 2026-07-29) is the counter that was previously
    entirely missing: a row that updates a term already known to this call (one
    that pre-existed in the DB, or one an earlier row of the SAME import already
    created) used to vanish from every count, so re-importing a file of entirely
    pre-existing terms reported "0 entries, 0 senses" despite writing real
    updates. It counts ROWS, not changes -- "changed"/"unchanged" split it into
    the rows that really moved a stored value and the true no-ops, and "changed"
    is the ONLY counter to build a "did anything change?" decision on (it is what
    `glossary_module.import_bytes` reverts approval on). A
    malformed/unusable TBX raises glossary_tbx.TbxError whose message is
    user-facing; the CSV arm's WRITE behaviour is byte-for-byte the previous
    behaviour (import_csv's own public return stays the narrower legacy shape --
    see its docstring).

    Batch 6: a glossary XLSX or TSV is converted to the glossary CSV first
    (tabular_import.glossary_payload) and then imported by the CSV arm unchanged."""
    from app.services import tabular_import

    data, filename = tabular_import.glossary_payload(data, filename, source_locale, target_locale)
    fmt = detect_format(data, filename)
    if fmt == JSON_FORMAT:
        # Concept-aware .glossary.json import (D2). Module glossaries ONLY — a concept
        # requires a module Glossary; a JSON import without a glossary_id is refused.
        from app.services import glossary_concepts

        if glossary_id is None:
            raise glossary_concepts.ConceptError(
                ".glossary.json import requires a module glossary."
            )
        counters = glossary_concepts.from_v1(session, glossary_id, glossary_concepts.loads_v1(data))
        return {**counters, "format": JSON_FORMAT}
    if fmt == TBX_FORMAT:
        from app.services import glossary_tbx

        # DEFAULT and ONLY import path: a TBX import takes the tolerant entry path
        # (parse_rows -> _apply_rows), the SAME linear/bounded path a CSV import uses -- for a
        # legacy per-project import (glossary_id is None, no concept home, LAW-TERM-2a) AND for a
        # module glossary, which additionally mirrors the concept store from those entries via
        # reconcile_glossary_from_entries (the bulk-import train).
        #
        # `concepts=True` is RESERVED (Terminology T2 scope decision): the concept-preserving TBX
        # import (all languages, synonyms, every §6.1 category, LAW-TERM-9) is DEFERRED to the
        # terminology authority train (T1b), which re-homes it onto the linear/bounded
        # `import_concepts_batched`. Until then the option is refused on every surface BEFORE any
        # parse or write, so a termbase is never half-imported through an un-owned reconciler. Use
        # the standard (entry) import; a `.glossary.json` round-trip carries the concept store
        # losslessly in the meantime.
        if concepts:
            raise glossary_tbx.TbxError(
                "Concept-preserving TBX import arrives with the terminology authority train; "
                "use the standard import."
            )
        # L1 (round-2): the entity-declaration + size refusal runs BEFORE `validate_strict`
        # hands the bytes to lxml, so a hostile TBX (entity bomb / external-entity XXE) is met
        # with the honest TbxError on every arm rather than a raw lxml error on the strict
        # arm. `parse_rows` also guards eagerly, so this is idempotent for the non-strict path.
        glossary_tbx._guard(data)
        if strict:
            glossary_tbx.validate_strict(data)
        parsed: dict = {}

        def _tbx_rows():
            # Re-created on the (rare) retry, so `parsed["stats"]` is the stats of the
            # parse that actually produced the committed rows.
            rows_iter, parsed["stats"] = glossary_tbx.parse_rows(data, source_locale, target_locale)
            return rows_iter

        tracker = glossary_tbx.EntryReportTracker()

        def _tbx_rows_tracked():
            tracker.reset()
            return _tbx_rows()

        def _on_row(row, pre_existing):
            tracker.row(row, pre_existing)
            # The generated do-not-use line never clobbers a curated note (review gate).
            glossary_tbx.merge_dont_use(row, pre_existing, module=glossary_id is not None)

        res = _apply_rows(
            session,
            project_id,
            _tbx_rows_tracked,
            source_locale,
            target_locale,
            glossary_id,
            on_row=_on_row,
        )
        stats = parsed["stats"]
        report = tracker.finish(stats)
        # Forbidden/deprecated terms survive a module-glossary import as status-bearing
        # GlossaryTerms on their concept (never a compat target); a per-project glossary has
        # no concept store, so there they live in the entry's notes only (merge_dont_use).
        if glossary_id is not None and stats.get("status_terms"):
            report["status_terms_kept"] = glossary_tbx.record_status_terms(
                session, glossary_id, source_locale, target_locale, stats["status_terms"]
            )
        return {
            **res,
            "format": TBX_FORMAT,
            "skipped": stats["skipped"],
            "concepts": stats["concepts"],
            "dialect": stats.get("dialect", ""),
            "report": report,
        }
    # Via import_csv_FULL (not import_csv) so the whole counter dict reaches this
    # seam; import_csv narrows its own public return to the legacy
    # {"entries", "senses"} shape (see its docstring). Both go through
    # import_csv_full/_csv_rows, so the decode contract cannot drift.
    res = import_csv_full(session, project_id, data, source_locale, target_locale, glossary_id)
    return {**res, "format": CSV_FORMAT}


def _choose_sense(
    segment_text: str,
    senses: list[GlossarySense],
    provider=None,
    disambig: str | None = None,
    base_target: str = "",
) -> GlossarySense | None:
    """Disambiguate a multi-sense term for `segment_text`.

    Returns the chosen GlossarySense, or **None meaning "no sense applies — use the
    entry's BASE target_term"**. None is returned whenever disambiguation cannot run
    (spend cap, provider error), cannot be understood (unparseable/out-of-range
    reply), or genuinely decides no specific sense fits; see the block comment above
    for why that is never senses[0] any more.

    `base_target` is the owning entry's base `target_term`. It is rendered as option
    0 so the selector can pick it explicitly.

    `disambig` (Gap D, SPEC-context-metadata phase 2A): a SHORT, already-sanitized
    context hint (domain + audience) from the run's resolved context package, threaded
    down from match_terms/match_terms_merged. When present it is added as ONE extra
    orientation line so sense selection knows the domain."""
    if not senses:
        return None
    provider = provider or get_provider()
    options = [{"label": "general", "cue": "", "target": base_target or "(no required term)"}]
    options += [
        {"label": s.sense_label, "cue": s.context_cue, "target": s.target_term} for s in senses
    ]
    payload = {"segment": segment_text, "senses": options}
    prompt = (
        "Choose the single best sense for the term as used in this segment.\n"
        f"Segment: {segment_text}\n"
        + (f"Content context: {disambig}\n" if disambig else "")
        + "\n".join(
            f"{i}. {o['label']} (use when: "
            + (o["cue"] or "the segment gives no clear cue for any sense below")
            + f") -> {o['target']}"
            for i, o in enumerate(options)
        )
        + '\nReply as JSON: {"index": <n>}'
    )
    # Spend cap: this is a direct provider call (not through run_completion), so
    # give it the same brake. Over cap on a REAL engine -> decide deterministically
    # (cue evidence, else the base target) without calling. Mock is exempt.
    from app.services import spend as _spend

    try:
        raw = _spend.complete_direct(provider, prompt, task="choose_sense", payload=payload)
        # TOLERANT parse (llm_json, the ONE extractor the LLM-judge seams use): a
        # bare json.loads was correct only for a model that returns nothing but
        # JSON. The project's own reference local model prefixes its JSON with prose
        # and/or a ```json fence, and Claude commonly does the same — every such
        # reply raised, hit `except: idx = 0`, and silently pinned the FIRST SENSE.
        # This is the single most likely path behind the production evidence.
        idx = int(llm_json.loads(raw).get("index", _BASE_OPTION))
    except Exception:
        return _cue_fallback(segment_text, senses)
    if idx == _BASE_OPTION:
        return None  # the selector chose the base translation
    if 1 <= idx <= len(senses):
        return senses[idx - 1]
    return _cue_fallback(segment_text, senses)  # nonsense index -> cannot decide


def _emit_for_entry(
    session: Session,
    e: GlossaryEntry,
    segment_text: str,
    provider,
    disambig: str | None,
    project_id=None,
) -> tuple[dict | None, GlossarySense | None, object]:
    """The shared per-entry emission core (SPEC-terminology-backend §5.3/H6). Runs
    sense selection for one surviving entry and returns
    `(emitted_five_key_dict_or_None, chosen_sense_or_None, provider)`.

    `match_terms` keeps only the dict; `glossary_concepts.match_concepts` additionally
    reads `chosen_sense` to resolve the sibling homonym concept for a sense-selected
    match (§4.4/H2). The returned `provider` propagates the LAZY resolution: it is
    resolved (get_provider()) ONLY when a matched term actually carries senses, once,
    and passed back so the caller caches it across entries — byte-identical to the
    pre-refactor provider timing. The emitted dict is EXACTLY the five keys
    source_term/target_term/sense_label/dnt/case_sensitive, in that order (LAW-TERM-1/E5)."""
    senses = session.exec(
        select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
    ).all()
    if senses:
        # AI opt-out (DPAI-08): sense choice is an LLM call that carries the segment text.
        # For an opted-out project it is skipped and the entry's BASE translation applies
        # (the same fallback as an undecided sense), whatever surface is matching.
        from app.services import ai_optout

        if project_id is not None and ai_optout.is_opted_out(session, project_id):
            chosen = None
        else:
            if provider is None:
                provider = get_provider()  # resolved lazily, only now that a multi-sense
                #                             term needs disambiguation
            chosen = _choose_sense(
                segment_text, senses, provider, disambig=disambig, base_target=e.target_term
            )
        if chosen is None:
            # No sense applies (or the selector could not decide): fall back to the
            # entry's BASE translation — never an arbitrary sense. With no base target
            # and no DNT flag there is no constraint to state, so the term is dropped.
            if not (e.target_term or e.dnt):
                return None, None, provider
            return (
                {
                    "source_term": e.source_term,
                    "target_term": e.target_term,
                    "sense_label": None,
                    "dnt": e.dnt,
                    "case_sensitive": e.case_sensitive,
                },
                None,
                provider,
            )
        return (
            {
                "source_term": e.source_term,
                "target_term": chosen.target_term,
                "sense_label": chosen.sense_label,
                "dnt": e.dnt,
                # carried so the deterministic DNT gate can check target preservation
                # with the SAME case rule the source match used. Additive.
                "case_sensitive": e.case_sensitive,
            },
            chosen,
            provider,
        )
    return (
        {
            "source_term": e.source_term,
            "target_term": e.target_term,
            "sense_label": None,
            "dnt": e.dnt,
            "case_sensitive": e.case_sensitive,
        },
        None,
        provider,
    )


def build_from_text(text: str, provider=None) -> list[dict]:
    """Propose candidate glossary terms from a chunk of source text."""
    provider = provider or get_provider()
    prompt = (
        "Extract key terminology (product names, domain terms, do-not-translate "
        "items) from the text below. Reply as a JSON list of "
        '{"source_term","target_term","note"}.\n\n' + text[:6000]
    )
    # Spend cap (direct provider call): over cap on a REAL engine -> return no
    # candidates without calling (same as an error). Mock is exempt.
    from app.services import spend as _spend

    try:
        raw = _spend.complete_direct(
            provider, prompt, task="build_glossary", payload={"text": text}
        )
    except Exception:
        # Spend cap, engine error or the key-echo guard: no candidates (unchanged).
        return []
    # TOLERANT parse (llm_json, the same extractor `_choose_sense` uses): a bare
    # json.loads was correct only for a model that returns nothing but JSON. The
    # reference local model and Claude both wrap the array in a ```json fence or
    # prefix it with a line of prose; every such reply raised, the exception was
    # swallowed, and the UI showed "no terms found" AFTER the API spend.
    try:
        items = llm_json.loads(raw)
    except Exception:
        # Log (never swallow silently) but keep the raw reply text out of the log.
        log.warning("glossary build_from_text: could not parse the provider reply as JSON")
        return []
    if not isinstance(items, list):
        return []
    # Keep only dict items with a non-empty source_term: a reply like ["invoice"]
    # parses but breaks callers that index item["source_term"], so drop it (and any
    # item missing the key) instead of passing a list of strings downstream.
    return [it for it in items if isinstance(it, dict) and str(it.get("source_term") or "").strip()]
