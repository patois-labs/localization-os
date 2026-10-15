"""Data retention: automated purge of aged data + GDPR Art. 17-reaching deletes.

Two jobs live here:

  purge(now)          — the scheduled sweep. When `retention_enabled` is on and
                        `retention_days` > 0, it deletes anything older than the
                        cutoff: upload files on disk, Document/Segment rows, and
                        TERMINAL-state Job rows (never QUEUED/RUNNING — a live job
                        must never be reaped out from under a worker). Deleting a
                        Document cascades to its Segments but NOT to its
                        project-scoped TM (TM is shared across every document in the
                        project — cascading per-document would silently wipe TM that
                        younger, un-aged documents in the same project still rely on).
                        Unused TM, including emptied projects' TM, ages out
                        row by row, with its embeddings. Recent updates/use, surviving
                        segment provenance and key catalogues protect live TM. Aged
                        memory events are deleted; entries and suggestions are erased
                        into content-free tombstones through the shared llmemory path.

  delete_document / delete_project_tm — the on-demand Art. 17 path (a right-to-
                        erasure request reaching translation memory), modelled on
                        gdpr.erase_user's single-transaction pattern. delete_document
                        keeps cascade_tm=True (the default) for the explicit GDPR
                        Art. 17 API — an erasure request DOES mean "reach the TM".

`delete_document` is the ONE FK-clean per-document delete seam in the codebase.
Every other document-delete surface routes through it (`content.delete_document`,
`content.delete_project` step 4, the Art. 17 path, and this module's own purge), so
the child-before-parent ordering that keeps the delete legal under FK enforcement
lives HERE, once, rather than being re-derived per caller. It reaps the document's
RUN HISTORY too (StepRun -> WorkflowRun -> WorkflowBatch): `WorkflowRun.document_id`
is a NON-NULL FK, so a document that was ever translated simply cannot be deleted
while its runs live. See `_reap_document_children` for the full referrer table.

That consolidation (2026-08-31) fixed THREE surfaces, not one. The ticket was
`retention.purge` crashing on any translated document, but the same audit found the
hand-rolled ordering in `content.delete_document` was ALSO incomplete — it missed
`workflowbatch.document_id` (NOT NULL) and `termcandidate.source_document_id` — so the
admin document-delete surface raised on any document with a multi-locale fan-out batch
or an extracted term candidate, and `content.delete_project` inherited that failure for
any project containing one. Neither ordering was complete; the fix is their union.

Retention age is UPLOAD age, not last-activity: Document has only `created_at`
(no last-touched column), so a document that was uploaded long ago but is still
being post-edited will still age out on its upload date. If a last-activity guard
is ever wanted, add a `last_activity_at` column and cut over the cutoff filter.

Safety model (purge is DESTRUCTIVE and irreversible):
- Previously authorized blob deletions retry on every tick, independently of the
  age-retention switches below. Those switches govern new age-based erasures only.
- `retention_enabled` defaults OFF ("0"). New age-based deletion requires an admin
  to explicitly turn it on, so an existing install is never swept by accident.
- `retention_days` defaults "0", which short-circuits even when enabled: a cutoff
  of "0 days ago" is treated as "no retention window configured", never "delete
  everything". A window only exists for days >= 1.
- Terminal Jobs only: QUEUED/RUNNING rows are excluded, so an in-flight or
  never-started job is never deleted mid-life.
- Upload blobs are deleted as opaque files — never decrypted first (the whole
  point is to destroy them; reading them back would be pointless and, with
  encrypt_uploads on, would touch the Fernet key needlessly).
- The scheduler daemon thread starts ONLY on real installs (see app/main._startup,
  gated on `not config.IS_TEST`); tests call tick()/purge() directly.

Documented in: docs/architecture/data-and-migrations.md
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import or_ as _or
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    BlobDeleteOutbox,
    DeliveryEmptyApproval,
    Document,
    FullTextTM,
    Job,
    JobStatus,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemorySuggestion,
    MemorySuggestionEvidence,
    QualityFinding,
    QueryPost,
    QueryThread,
    RunStatus,
    Segment,
    SegmentEmbedding,
    SegmentPostEdit,
    SegmentTM,
    StepRun,
    VendorDispatch,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import audit, settings
from app.services.maintenance import MaintenancePass

log = logging.getLogger("loc_tms.retention")

TICK_SECONDS = 3600  # one sweep an hour is plenty for a day-granularity policy

_TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED)

# #543: a WorkflowRun in one of these states is live work over its document; the age sweep must
# never delete the document (and with it the run, its steps, batch and vendor legs) mid-run.
_RUN_IN_FLIGHT = (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED)


def _document_has_inflight_run(s: Session, doc_id: int) -> bool:
    return (
        s.exec(
            select(WorkflowRun.id)
            .where(WorkflowRun.document_id == doc_id, WorkflowRun.status.in_(_RUN_IN_FLIGHT))
            .limit(1)
        ).first()
        is not None
    )


_started = False
_start_lock = threading.Lock()


def enabled() -> bool:
    return settings.get("retention_enabled", "0") == "1"


def retention_days() -> int:
    """The retention window in days. 0 (or invalid) = no window = purge is a
    no-op even when enabled (a cutoff of 'now' must never mean 'delete all')."""
    try:
        # GitHub #502: clamp on read too (a value stored before the write validator existed):
        # `now - timedelta(days=N)` overflows the datetime range for N past about 730,000.
        return max(
            0, min(int(str(settings.get("retention_days", "0") or 0)), settings.RETENTION_DAYS_MAX)
        )
    except (TypeError, ValueError):
        return 0


def _cutoff(now: datetime) -> datetime | None:
    days = retention_days()
    if days <= 0:
        return None
    return now - timedelta(days=days)


# --- TM-reaching deletes (the GDPR Art. 17 path) ----------------------------
def _delete_project_tm(s: Session, project_id: int) -> int:
    """Delete every SegmentTM + FullTextTM row scoped to `project_id`, plus the
    project's SegmentEmbedding vectors (source-derived personal data must not
    outlive an Art. 17 erasure — GDPR finding). Returns the row count removed.
    Runs inside the caller's transaction (no commit here).

    SegmentEmbedding is deleted EXPLICITLY (not via ondelete=CASCADE): adding an
    ON DELETE CASCADE would require a full SQLite table rebuild for no gain over
    these two-line explicit deletes. SegmentEmbedding carries a denormalized
    project_id, so it can be scoped directly without a join.

    ORDER MATTERS: SegmentEmbedding.segment_tm_id -> segmenttm.id, so the child
    embeddings must be deleted BEFORE the SegmentTM rows they reference. Postgres
    has always enforced this FK (this delete-before-parent would have failed there
    on any project with embeddings); with SQLite FK enforcement now on, the SQLite
    engine agrees. FullTextTM has no such child dependency, so its position is
    free — only SegmentEmbedding-before-SegmentTM is load-bearing.

    The returned count still spans all three models (the historical contract:
    callers/tests treat SegmentTM + FullTextTM + SegmentEmbedding as the erased set)."""
    removed = 0
    for model in (SegmentEmbedding, SegmentTM, FullTextTM):
        rows = s.exec(select(model.id).where(model.project_id == project_id)).all()
        if rows:
            s.exec(sa_delete(model).where(model.project_id == project_id))
            removed += len(rows)
    return removed


def _delete_document_tm(session: Session, project_id: int, rows) -> int:
    """Delete ONLY the TM rows that came from one document's content (#539, 2026-10-05).

    ``rows`` is the document's (source_text, target_locale) pairs, read BEFORE its segments are
    deleted. TM rows carry no document link, so provenance is the content itself: a row of this
    project whose source hash (project source locale + the segment's target locale + normalized
    source) or exact source text and locale pair equals one of the document's segments. The rest
    of the project TM stays. A row two documents share by identical source text is erased (the
    fail-safe direction for an erasure). Embeddings of the erased SegmentTM rows go first (FK).
    Runs inside the caller's transaction (no commit). Returns the rows removed."""
    from app.models import Project
    from app.services.tm_common import hash_text

    project = session.get(Project, project_id)
    src_loc = getattr(project, "source_locale", None) or "en-US"
    by_locale: dict[str, set] = {}
    for source_text, target_locale in rows:
        if source_text and target_locale:
            by_locale.setdefault(target_locale, set()).add(source_text)
    removed = 0
    for tgt_loc, texts in by_locale.items():
        texts = sorted(texts)
        for start in range(0, len(texts), 400):
            chunk = texts[start : start + 400]
            hashes = [hash_text(t, src_loc, tgt_loc) for t in chunk]
            for model in (SegmentTM, FullTextTM):
                ids = session.exec(
                    select(model.id).where(
                        model.project_id == project_id,
                        model.target_locale == tgt_loc,
                        _or(
                            model.source_hash.in_(hashes),
                            (model.source_locale == src_loc) & model.source_text.in_(chunk),
                        ),
                    )
                ).all()
                if not ids:
                    continue
                if model is SegmentTM:
                    session.exec(
                        sa_delete(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id.in_(ids))
                    )
                session.exec(sa_delete(model).where(model.id.in_(ids)))
                removed += len(ids)
    return removed


def delete_project_tm(session: Session, project_id: int) -> int:
    """Public single-transaction wrapper: remove a project's translation memory
    (both stores). Returns the number of TM rows deleted."""
    n = _delete_project_tm(session, project_id)
    session.commit()
    return n


def _delete_blob_key(store, key: str) -> str:
    """Delete one blob key through ``store`` and classify the outcome as an
    ``app.ports.blobstore.DeleteStatus`` string (#399). NEVER raises -- the historical
    totality guard (a blob-store hiccup must not stop the DB rows from being reaped)
    doubles as the classification: ANY raise, a malformed-key ``ValueError`` included,
    is FAILED, exactly where the old code returned a bare False.

    Adapters that implement ``delete_status`` report REMOVED / MISSING / FAILED directly.
    A legacy adapter has only ``delete() -> bool``, whose False conflates missing with
    failure; the #399 rule (never discard the only blob key on a transient failure)
    resolves that ambiguity conservatively as FAILED -- a spurious pending row costs one
    no-op re-delete, while a missed failure orphans private content forever."""
    from app.ports import blobstore

    status_fn = getattr(store, "delete_status", None)
    try:
        if status_fn is not None:
            return status_fn(key)
        if store.delete(key):
            return blobstore.DeleteStatus.REMOVED
        return blobstore.DeleteStatus.FAILED
    except Exception:
        log.warning("Blob delete failed for key %r (best-effort, continuing)", key, exc_info=True)
        return blobstore.DeleteStatus.FAILED


def _delete_upload_file(doc: Document) -> str:
    """Best-effort delete of a document's upload blob via the BlobStore port. The file is
    removed as an opaque blob — NEVER decrypted first. Returns a
    ``app.ports.blobstore.DeleteStatus`` string (#399): REMOVED when a blob was removed,
    MISSING when none existed, FAILED when an operational error may have left it behind.

    The persisted upload key is canonical; legacy rows resolve to ``id.fmt``.
    The old ``or 'bin'`` fallback was DEAD by
    construction and is dropped: ``ingest_document``'s ``UnsupportedFormat`` guard aborts
    BEFORE any Document row or upload blob is created, so a persisted document always has a
    non-empty ``fmt`` and an empty-``fmt`` row can never coexist with a blob — the ``bin``
    branch could only ever have keyed a delete against a path no blob was written to (a
    guaranteed no-op). STOP and reconcile if any path is found writing a blob under ``bin``.

    TOTALITY GUARD (belt AND braces): ``retention.purge``'s per-document loop now has its own
    try/except (added 2026-08-31 with the run-history fix — it was UNGUARDED before, which is
    how one FK-blocked document took whole sweeps down), so a raise here would cost only this
    ONE document rather than the sweep. We still swallow ANY exception at this level: the loop
    guard SKIPS the document, whereas a blob-store hiccup should not stop the DB rows from
    being reaped at all. The default DiskBlobStore adapter never raises, but a future adapter
    (e.g. a phase-3 S3 store raising ``botocore.ClientError``) could. A swallowed failure now
    returns FAILED — and ``delete_document`` turns that into a durable retry row — instead of
    the old bare False that erased the key's only durable reference (finding dbdata-07)."""
    from app.ports import blobstore

    return _delete_blob_key(blobstore.get(), blobstore.document_key(doc))


def _drain_blob_delete_outbox(s: Session) -> tuple[int, int]:
    """Retry every persisted failed blob delete (#399). Returns
    ``(resolved, still_pending)``. A retry reporting REMOVED or MISSING resolves the
    row (nothing left to retry — a blob that vanished on its own needs no retry either);
    FAILED bumps ``attempts`` and keeps the key for a later sweep. Per-row commits so
    one poison key never blocks the rest of the ledger, matching the sweep's totality
    philosophy everywhere else."""
    from app.ports import blobstore

    rows = s.exec(select(BlobDeleteOutbox).order_by(BlobDeleteOutbox.id)).all()
    store = blobstore.get()
    resolved = pending = 0
    for row in rows:
        status = _delete_blob_key(store, row.blob_key)
        if status in (blobstore.DeleteStatus.REMOVED, blobstore.DeleteStatus.MISSING):
            s.delete(row)
            resolved += 1
        else:
            row.attempts += 1
            s.add(row)
            pending += 1
        s.commit()
    return resolved, pending


# --- run-history reap (shared by every document/project delete surface) ------
def _erase_memory_events(session: Session, event_ids: list) -> dict:
    """Erase LLMemory events (and every entry or suggestion citing only them) inside the
    caller's transaction. With the LLMemory add-on present this is `llmemory.erase_events`
    verbatim. With it ABSENT (an install carried over from the full edition still has rows;
    the tables are core) a plain-SQL fallback removes the citation joins and the events
    themselves, which is exactly what an FK-clean document/project delete needs."""
    from app import extensions as _extensions

    if _extensions.memory.installed:
        return _extensions.memory.erase_events(session, event_ids)
    from sqlalchemy import delete as _sa_delete

    ids = list(event_ids)
    gone = set(ids)
    entries_gone = suggestions_gone = 0
    for start in range(0, len(ids), 500):
        part = ids[start : start + 500]
        # Text LEARNED FROM these events (an entry or suggestion citing ONLY erased events)
        # is erased with them; one that another event still supports keeps its row with a
        # corrected evidence count. Decided BEFORE the citation joins go.
        en_ids, en_alive = _leftover_dependents(
            session, MemoryEvidence.memory_id, MemoryEvidence.event_id, part, gone
        )
        sg_ids, _sg_alive = _leftover_dependents(
            session,
            MemorySuggestionEvidence.suggestion_id,
            MemorySuggestionEvidence.event_id,
            part,
            gone,
        )
        session.exec(_sa_delete(MemoryEvidence).where(MemoryEvidence.event_id.in_(part)))
        session.exec(
            _sa_delete(MemorySuggestionEvidence).where(MemorySuggestionEvidence.event_id.in_(part))
        )
        session.exec(_sa_delete(MemoryEvent).where(MemoryEvent.id.in_(part)))
        entries_gone += _delete_leftover_entries(session, en_ids, en_alive)
        suggestions_gone += _delete_leftover_suggestions(session, sg_ids)
    return {
        "memory_events_deleted": len(ids),
        "memory_entries_deleted": entries_gone,
        "memory_suggestions_deleted": suggestions_gone,
    }


def _leftover_dependents(session: Session, owner_col, event_col, part: list, gone: set):
    """Ids of entries/suggestions (`owner_col`) citing an event in `part` and NO event outside
    `gone`, plus the owners that another event still supports."""
    owners = {
        o for o in session.exec(select(owner_col).where(event_col.in_(part))).all() if o is not None
    }
    dead, alive = [], []
    for o in owners:
        cited = set(session.exec(select(event_col).where(owner_col == o)).all())
        (dead if cited <= gone else alive).append(o)
    return dead, alive


def _delete_leftover_entries(session: Session, en_ids: list, alive: list) -> int:
    from sqlalchemy import delete as _sa_delete
    from sqlalchemy import func as _func
    from sqlalchemy import update as _sa_update

    if en_ids:
        session.exec(
            _sa_update(MemoryEntry)
            .where(MemoryEntry.superseded_by_id.in_(en_ids))
            .values(superseded_by_id=None)
        )
        session.exec(
            _sa_update(MemorySuggestionEvidence)
            .where(MemorySuggestionEvidence.memory_id.in_(en_ids))
            .values(memory_id=None)
        )
        session.exec(_sa_delete(MemoryEvidence).where(MemoryEvidence.memory_id.in_(en_ids)))
        session.exec(_sa_delete(MemoryEntry).where(MemoryEntry.id.in_(en_ids)))
    # Surviving entries that lost a citation keep an honest evidence count (callers delete
    # the erased events' joins BEFORE this runs).
    for entry_id in alive:
        entry = session.get(MemoryEntry, entry_id)
        if entry is None:
            continue
        n = session.exec(
            select(_func.count())
            .select_from(MemoryEvidence)
            .where(MemoryEvidence.memory_id == entry_id)
        ).one()
        entry.evidence_count = n
        session.add(entry)
    return len(en_ids)


def _delete_leftover_suggestions(session: Session, sg_ids: list) -> int:
    from sqlalchemy import delete as _sa_delete
    from sqlalchemy import update as _sa_update

    if not sg_ids:
        return 0
    session.exec(
        _sa_update(MemorySuggestion)
        .where(MemorySuggestion.superseded_by_id.in_(sg_ids))
        .values(superseded_by_id=None)
    )
    session.exec(
        _sa_delete(MemorySuggestionEvidence).where(
            MemorySuggestionEvidence.suggestion_id.in_(sg_ids)
        )
    )
    session.exec(_sa_delete(MemorySuggestion).where(MemorySuggestion.id.in_(sg_ids)))
    return len(sg_ids)


def erase_project_memory(session: Session, project_id: int) -> dict:
    """Privacy ERASURE of a project's LLMemory (issue #441 decision, 2026-10-05).

    A plain project delete keeps what the customer learned (the rows are un-scoped to
    customer-wide, see content.delete_project step 8e). An ERASURE request is different: the
    entries, events and suggestions LEARNED FROM the erased project hold human-typed text of
    that project, so they are erased too. Everything is done inside the caller's transaction
    (no commit): the project's events (and, through erase_events, every entry or suggestion
    that depended only on them), then every entry still scoped to the project (with its whole
    belief group), then every suggestion still scoped to it. Rows survive as content-free
    tombstones, exactly like llmemory.purge. Returns counts."""
    from app import extensions as _extensions

    llmemory = _extensions.memory
    llmemory_suggestions = _extensions.memory_suggestions

    stats = {"memory_events_erased": 0, "memory_entries_erased": 0, "memory_suggestions_erased": 0}
    ev_ids = list(
        session.exec(select(MemoryEvent.id).where(MemoryEvent.project_id == project_id)).all()
    )
    if ev_ids:
        _erase_memory_events(session, ev_ids)
        stats["memory_events_erased"] = len(ev_ids)
    if not llmemory.installed:
        # Add-on absent: its tombstoning logic is not here. An erasure request still must
        # not leave text learned from the project behind in rows carried over from the full
        # edition, so the project's entries and suggestions are deleted outright.
        from sqlalchemy import delete as _sa_delete
        from sqlalchemy import update as _sa_update

        en_ids = list(
            session.exec(select(MemoryEntry.id).where(MemoryEntry.project_id == project_id)).all()
        )
        sg_ids = list(
            session.exec(
                select(MemorySuggestion.id).where(MemorySuggestion.project_id == project_id)
            ).all()
        )
        if en_ids:
            session.exec(
                _sa_update(MemoryEntry)
                .where(MemoryEntry.superseded_by_id.in_(en_ids))
                .values(superseded_by_id=None)
            )
            session.exec(
                _sa_update(MemorySuggestionEvidence)
                .where(MemorySuggestionEvidence.memory_id.in_(en_ids))
                .values(memory_id=None)
            )
            session.exec(_sa_delete(MemoryEvidence).where(MemoryEvidence.memory_id.in_(en_ids)))
            session.exec(_sa_delete(MemoryEntry).where(MemoryEntry.id.in_(en_ids)))
            stats["memory_entries_erased"] = len(en_ids)
        if sg_ids:
            session.exec(
                _sa_update(MemorySuggestion)
                .where(MemorySuggestion.superseded_by_id.in_(sg_ids))
                .values(superseded_by_id=None)
            )
            session.exec(
                _sa_delete(MemorySuggestionEvidence).where(
                    MemorySuggestionEvidence.suggestion_id.in_(sg_ids)
                )
            )
            session.exec(_sa_delete(MemorySuggestion).where(MemorySuggestion.id.in_(sg_ids)))
            stats["memory_suggestions_erased"] = len(sg_ids)
        session.flush()
        return stats
    entries = session.exec(
        select(MemoryEntry).where(
            MemoryEntry.project_id == project_id, MemoryEntry.status != "purged"
        )
    ).all()
    for entry in entries:
        for row in llmemory._belief_group(session, entry):
            if row.status == "purged":
                continue
            llmemory._purge_one(session, row, "erasure", commit=False)
            stats["memory_entries_erased"] += 1
    sugg = session.exec(
        select(MemorySuggestion.id, MemorySuggestion.customer_id).where(
            MemorySuggestion.project_id == project_id
        )
    ).all()
    by_customer: dict = {}
    for sid, cid in sugg:
        by_customer.setdefault(cid, []).append(sid)
    for cid, sids in by_customer.items():
        for start in range(0, len(sids), 500):
            res = llmemory_suggestions.purge_suggestions_for(
                session, customer_id=cid, suggestion_ids=sids[start : start + 500], commit=False
            )
            stats["memory_suggestions_erased"] += res["suggestions_content_cleared"]
    session.flush()
    return stats


def purge_vendor_dispatch_legs(session: Session, run_ids) -> int:
    """Delete every VendorDispatch leg anchored to a WorkflowRun in ``run_ids`` (#63).

    ``VendorDispatch.workflow_run_id`` is NOT NULL, so a leg CANNOT survive its run: a
    document/project delete destroys the run itself, so the leg must go with it. (This is
    distinct from ``delete_document``'s ``document_id``-scoped clear, which only severs
    ``document_id`` + wipes the frozen package on a leg whose RUN LIVES ON — there the
    reconciliation ledger row is entitled to stay. Here the anchor itself is gone, leaving
    nothing valid to point at.) Without this the WorkflowRun delete that follows RAISES
    ``IntegrityError`` and rolls the whole transaction back, leaving the document/project
    undeletable.

    THIS FUNCTION ESTABLISHES ITS OWN PRECONDITION (fix round, F2). It used to merely
    DOCUMENT that the leg's four ``dispatch_id`` referrers (``LQAResult`` /
    ``SegmentPostEdit`` / ``QueryThread`` / ``QueryPost``) had to be gone first, and leave
    that to the caller — but NO caller ever scoped anything by ``dispatch_id``. Both callers
    scope by document/segment (``_reap_document_children`` steps 1-2) or by project
    (``content.delete_project`` steps 1/1b), which happens to cover every row a PRODUCTION
    writer creates only because those writers stamp document/segment alongside dispatch_id.
    That is an undocumented writer invariant, not a constraint: a row carrying ONLY a
    ``dispatch_id`` (document/segment NULL, or pointing at a DIFFERENT document) survives the
    callers' sweeps and makes the DELETE below raise IntegrityError. The precondition is now
    structural — established here, in the one place that knows which legs are dying.

    SEVER, NOT DELETE, and deliberately so. Any referrer that is THIS document's own content
    was already DELETED by the caller a moment ago; whatever still points at a dying leg by
    the time we get here is BY CONSTRUCTION not this document's — it belongs to another
    document or to no document at all. Destroying it would be collateral damage on an
    erasure path (``SegmentPostEdit.document_id``/``segment_id`` are NOT NULL, so such a row
    is provably some OTHER document's post-edit episode; a QueryThread would take another
    project's discussion thread down with it). All four ``dispatch_id`` columns are nullable,
    so severing is legal and leaves the row intact minus the vendor-leg provenance that no
    longer exists — the same "narrower FK NULLed, wider owner keeps the row" rule this module
    applies to MemoryEvent, TermCandidate and non-document-mode DeliveryEmptyApproval.
    (NOTE: the fix-round instruction asked for a DELETE here; sever closes the same hole
    structurally without the collateral, so it was taken instead — flagged for ratification.)

    The last remaining referrer is the self-FK ``rework_of``, NULLed for pointers into the
    deleted set first so the DELETE raises no self-referential FK error. Returns the count
    of legs removed.

    LIVES HERE, NOT IN content, on IMPORT WEIGHT: ``app.services.retention`` alone pulls in
    471 modules, ``+ app.services.content`` 553. Retention is reached by the scheduler daemon
    on every install and by the bare-subprocess probes in tests/verify_keys_inertness.py,
    so it stays the LOWER layer and content depends on it, never the reverse. Nothing
    ENFORCES that today — the [G] probe only asserts that ``app.services.keys`` is absent and
    ``Thread.start`` is unpatched, and would NOT notice a content import — so this is a
    discipline, not a guarantee. ``content.delete_project`` calls this for its
    workflow-anchored runs."""
    if not run_ids:
        return 0
    leg_ids = list(
        session.exec(
            select(VendorDispatch.id).where(VendorDispatch.workflow_run_id.in_(run_ids))
        ).all()
    )
    if not leg_ids:
        return 0
    # Sever every remaining dispatch_id referrer (see the docstring). Ordered posts-then-
    # threads only for tidiness — these are UPDATEs, so no child-before-parent rule applies.
    for _model in (LQAResult, SegmentPostEdit, QueryPost, QueryThread):
        session.exec(
            sa_update(_model).where(_model.dispatch_id.in_(leg_ids)).values(dispatch_id=None)
        )
    session.flush()
    session.exec(
        sa_update(VendorDispatch)
        .where(VendorDispatch.rework_of.in_(leg_ids))
        .values(rework_of=None)
    )
    session.flush()
    session.exec(sa_delete(VendorDispatch).where(VendorDispatch.id.in_(leg_ids)))
    session.flush()
    return len(leg_ids)


def unscope_memory_event_runs(session: Session, run_ids) -> int:
    """NULL ``MemoryEvent.workflow_run_id`` for events captured against a WorkflowRun in
    ``run_ids``, BEFORE that run is deleted (#63). The FK is nullable (ondelete=None), so
    an event outlives its run un-scoped — the same treatment as the
    ``project_id`` un-scope in ``content.delete_project`` step 8e. (Document deletes no
    longer use it: since #276 they DELETE the document's events outright, which also
    clears this arm.) Without this the WorkflowRun delete RAISES ``IntegrityError``.
    Returns the count of events un-scoped. (Homed here for the same reason as
    ``purge_vendor_dispatch_legs`` above.)"""
    if not run_ids:
        return 0
    n = len(
        session.exec(select(MemoryEvent.id).where(MemoryEvent.workflow_run_id.in_(run_ids))).all()
    )
    if n:
        session.exec(
            sa_update(MemoryEvent)
            .where(MemoryEvent.workflow_run_id.in_(run_ids))
            .values(workflow_run_id=None)
        )
        session.flush()
    return n


def _reap_document_children(session: Session, doc_id: int, seg_ids, run_ids) -> dict:
    """Remove/sever EVERY row that FK-references this document, its segments, or its runs,
    in child-before-parent order, so the caller's ``DELETE FROM segment`` +
    ``DELETE FROM document`` are legal under FK enforcement (SQLite PRAGMA foreign_keys=ON
    — app/db.py — and Postgres). Runs inside the caller's transaction (no commit here).
    Returns the counts to fold into the caller's dict.

    THE COMPLETE REFERRER TABLE (audited off app/models.py; keep it in sync — a NEW FK to
    document/segment/workflowrun/vendordispatch that is not handled here makes the whole
    delete raise IntegrityError and roll back, which is how the retention sweep silently
    stopped ageing out every translated document until 2026-08-31):

      segment.document_id           NOT NULL  -> DELETED by the caller (the document's own rows)
      workflowbatch.document_id     NOT NULL  -> DELETED (run history dies with the document)
      workflowrun.document_id       NOT NULL  -> DELETED  ditto
      workflowrun.batch_id          NULL-able -> un-scoped defensively before the batch delete
      steprun.workflow_run_id       NOT NULL  -> DELETED (child of WorkflowRun)
      vendordispatch.workflow_run_id NOT NULL -> leg row DELETED (cannot outlive its anchor)
      vendordispatch.document_id    NULL-able -> severed + frozen package cleared (ledger kept)
      vendordispatch.rework_of      NULL-able -> un-scoped inside purge_vendor_dispatch_legs
      lqaresult.document_id/.segment_id -> DELETED (this document's QA findings)
      segmentpostedit.document_id/.segment_id -> DELETED (Segment child, §3.2.6)
      querythread.document_id/.segment_id -> DELETED (with its QueryPosts)
      lqaresult/segmentpostedit/querythread/querypost .dispatch_id -> the rows that are THIS
          document's are already gone by the arms above; ANY that remain belong to another
          document (or none) and are SEVERED inside purge_vendor_dispatch_legs, which
          establishes its own precondition rather than trusting the caller (F2)
      deliveryemptyapproval.document_id -> DELETED when doc-mode; .run_id un-scoped otherwise
      memoryevent.document_id/.segment_id/.workflow_run_id -> DELETED (#276: the event's
          before/after text is the document's text and must not outlive it; its
          MemoryEvidence / MemorySuggestionEvidence citation rows go first)
      termcandidate.source_document_id  NULL-able -> context scrubbed, then UN-SCOPED

    WHY DELETE vs SEVER: content rows that only make sense as part of THIS document
    (segments, run history, QA findings, post-edit episodes, queries) are erasure targets and
    go — now INCLUDING the document's MemoryEvents (#276, r2-data-01): an event's
    before_text/after_text columns hold the document's text verbatim, and the pre-#276
    un-scope left that exact text alive forever in the learning store (document_id NULL),
    where neither retention nor any erasure surface could reach it. LEDGER/LEARNING rows
    that belong to a wider owner and carry NO document text survive with the narrower FK
    NULLed: a TermCandidate's accept/drop decision is the GLOSSARY's noise-control memory
    (with document-derived context erased before its provenance is severed), and a
    DeliveryEmptyApproval that is NOT this document's (key/project-mode) is a human's durable
    release decision — the workflows.py B-H2 treatment, mirrored here.

    THE ONE ASYMMETRY worth knowing: a VendorDispatch leg is a reconciliation LEDGER row we
    otherwise keep, but ``workflow_run_id`` is NOT NULL, so a leg anchored to a run we delete
    CANNOT be kept — the row goes. Only a leg whose run belongs to a DIFFERENT document (the
    cross-run shape) survives, via the document_id-scoped clear the caller does first."""
    counts: dict = {}
    # (1) LQA findings + post-edit episodes. FIRST because they reference BOTH the
    #     document/segment AND vendordispatch.dispatch_id, so this document's own rows
    #     predecease the legs at (4). Both arms (document_id and segment_id) are swept:
    #     LQAResult's are independently nullable, and a row can carry either alone.
    #     A row that carries a dispatch_id but is NOT this document's is NOT caught here and
    #     is not meant to be — purge_vendor_dispatch_legs severs those itself (F2).
    _lqa = set(session.exec(select(LQAResult.id).where(LQAResult.document_id == doc_id)).all())
    _pe = set(
        session.exec(select(SegmentPostEdit.id).where(SegmentPostEdit.document_id == doc_id)).all()
    )
    if seg_ids:
        _lqa |= set(
            session.exec(select(LQAResult.id).where(LQAResult.segment_id.in_(seg_ids))).all()
        )
        _pe |= set(
            session.exec(
                select(SegmentPostEdit.id).where(SegmentPostEdit.segment_id.in_(seg_ids))
            ).all()
        )
    if _lqa:
        session.exec(sa_delete(LQAResult).where(LQAResult.id.in_(list(_lqa))))
    if _pe:
        session.exec(sa_delete(SegmentPostEdit).where(SegmentPostEdit.id.in_(list(_pe))))
    session.flush()
    counts["lqa_results"] = len(_lqa)
    # `segment_post_edits` keeps its historical name/meaning (the PE rows this delete
    # removed); it now counts the UNION of both FK arms rather than the segment arm alone.
    counts["segment_post_edits"] = len(_pe)
    # (1b) Quality Lens findings (SPEC-quality-lenses-p2 §2.3, T3 hand-off (a)). QualityFinding
    #      carries HARD FKs to document/segment (and to advancedrun/advancedstagedtarget); the
    #      run-parented arm is reaped per-UNIT by step (1c) below on the document path AND by the
    #      run-delete seam on a run deletion. SQLite enforces the FKs (PRAGMA foreign_keys=ON), so a
    #      finding pointing at this document or one of its segments would make the Document/Segment
    #      delete below RAISE IntegrityError and roll the whole erasure back -- the SAME FK class as
    #      the parked delete_workflow train. Deleted child-before-parent here, on BOTH FK arms.
    #      T3 writes only run-parented findings, so this arm is dormant until P3 writes
    #      segment/document-parented ones; it is wired now so the delete seam is complete on the day
    #      those land, not a latent break. Findings are provenance, not GDPR-reachable content
    #      (no source text), so they are DELETED with the erasure rather than severed.
    _qf = set(
        session.exec(select(QualityFinding.id).where(QualityFinding.document_id == doc_id)).all()
    )
    if seg_ids:
        _qf |= set(
            session.exec(
                select(QualityFinding.id).where(QualityFinding.segment_id.in_(seg_ids))
            ).all()
        )
    if _qf:
        session.exec(sa_delete(QualityFinding).where(QualityFinding.id.in_(list(_qf))))
        session.flush()
    counts["quality_findings"] = len(_qf)
    # (1c) The ADVANCED tier's own document-derived CONTENT (spec review gate 09-05, H3 + round-1
    #      H1/M1/L1). The tier writes RUN-parented findings (run_id + staged_target_id set,
    #      document_id/segment_id NULL -- the CLASSIC arm above does NOT see them) AND stages the
    #      document's source/target text into its OWN AdvancedStagedTarget / AdvancedTranscriptEntry
    #      / AdvancedMission tables whose segment_id is a SOFT reference (no DB FK). So the
    #      Document/Segment delete below did NOT raise and simply LEFT that document's content alive
    #      in four tables until the run was deleted -- a SILENT residue, one of it read back by an
    #      exposed seam. The orchestrator ranked erasure above the audit trail: reap findings and
    #      SEVER the staged text / roster previews / transcript content this document's segments
    #      produced, keyed per-UNIT (never run_id, so a sibling document in the same run survives),
    #      while LEAVING the run/mission rows + their accounting (a document erasure is not a run
    #      deletion). The seam lives in app/services/advanced/ (the HARD RULE + the 09-03
    #      delete_project FK-husk lesson): it takes THIS session, and is imported LAZILY so the
    #      retention module's import weight + the bare-subprocess inertness probes are unchanged.
    from app import extensions as _extensions

    _adv = _extensions.run_reapers("document_content", session, doc_id, seg_ids)
    counts["advanced_findings_reaped"] = _adv.get("findings_reaped", 0)
    counts["advanced_staged_erased"] = _adv.get("staged_erased", 0)
    counts["advanced_transcript_rows_erased"] = _adv.get("transcript_rows_erased", 0)
    counts["advanced_roster_previews_erased"] = _adv.get("roster_previews_erased", 0)
    # round-3 M2: caller-authored roster units with a non-integer segment_id/key_id were SKIPPED
    # (never raised) -- reported so a malformed scripted mission is diagnosable from the counts.
    counts["advanced_units_unparseable"] = _adv.get("units_unparseable", 0)
    # (2) Query threads + posts are Document / Segment children (SPEC-wave3-collaboration
    #     §3.5). This is the LOAD-BEARING per-document reap reached by GDPR erase
    #     (cascade_tm=True) and by content.delete_document; N11 tests it directly. Child
    #     (posts) before parent (threads), and both before the VendorDispatch legs below —
    #     a thread/post may carry a dispatch_id. NOT feature-gated (§5.2 seam 4). Scoped to
    #     this document or its segments (a key value's thread is caught by the segment arm).
    _thread_cond = QueryThread.document_id == doc_id
    if seg_ids:
        _thread_cond = _or(_thread_cond, QueryThread.segment_id.in_(seg_ids))
    q_thread_ids = session.exec(select(QueryThread.id).where(_thread_cond)).all()
    if q_thread_ids:
        counts["query_posts"] = len(
            session.exec(select(QueryPost.id).where(QueryPost.thread_id.in_(q_thread_ids))).all()
        )
        session.exec(sa_delete(QueryPost).where(QueryPost.thread_id.in_(q_thread_ids)))
        session.flush()
        session.exec(sa_delete(QueryThread).where(QueryThread.id.in_(q_thread_ids)))
        session.flush()
    else:
        counts["query_posts"] = 0
    counts["query_threads"] = len(q_thread_ids)
    # (3) VENDOR-DISPATCH CONTENT (AUDIT-omegat-roundtrip-2026-08-10). A VendorDispatch leg's
    # `retry_contract` can hold a WIRE-LESS (manual) leg's FROZEN OPERATOR-DELIVERY PACKAGE --
    # the full dispatch-time segment CONTENT (sources + target drafts) the operator downloads as
    # the delivery. So, in this SAME transaction and BEFORE the Document delete, we clear
    # `retry_contract` (the CONTENT) and NULL `document_id` (sever the now-dangling
    # `VendorDispatch.document_id` FK) for every leg of this document, so the frozen CONTENT does
    # not outlive its document. We do NOT delete the leg ROWS here: the rest of VendorDispatch is
    # the audit/reconciliation LEDGER (ids, costs, statuses, timestamps -- counts/ids, not source
    # content), which the append-only trail is entitled to keep.
    #
    # This clear is now EFFECTIVE (it used to be forward-looking only: the surrounding document
    # delete was FK-blocked for every document with a workflow run, so the whole transaction --
    # including this clear -- rolled back; fixed 2026-08-31 by the run reap at (7)/(8) below).
    # For the ordinary shape the leg's run belongs to THIS document, so step (4) deletes the row
    # outright a moment later and the clear is belt-and-braces; the clear is what LANDS for the
    # cross-run shape (a leg whose run points at a DIFFERENT document), where the ledger row
    # legitimately survives its document.
    counts["dispatch_packages_cleared"] = len(
        session.exec(
            select(VendorDispatch.id).where(
                VendorDispatch.document_id == doc_id, VendorDispatch.retry_contract.is_not(None)
            )
        ).all()
    )
    session.exec(
        sa_update(VendorDispatch)
        .where(VendorDispatch.document_id == doc_id)
        .values(retry_contract=None, document_id=None)
    )
    session.flush()
    # (4) legs anchored to THIS document's runs: the row itself cannot survive the run
    #     (workflow_run_id NOT NULL). This document's own dispatch_id referrers died at
    #     (1)/(2); any foreign stragglers are severed by the helper itself (F2).
    counts["dispatch_legs_deleted"] = purge_vendor_dispatch_legs(session, run_ids)
    # (5) LLMEMORY EVENTS (#63, hardened by #276). MemoryEvent carries NULLABLE FKs
    # document_id -> document.id, segment_id -> segment.id and workflow_run_id ->
    # workflowrun.id (all ondelete=None), so with SQLite FK enforcement ON the
    # Segment/Document/WorkflowRun deletes RAISE IntegrityError unless these events are
    # dealt with first. DELETE, not un-scope (#276 / HECVAT AI-35 + Privacy-79): an
    # event's before_text/after_text IS the document's text, and the pre-#276 un-scope
    # left that exact text alive forever as a document_id-NULL row no sweep could reach.
    # ALL THREE FK arms are swept (a run-only event is this document's episode too), and
    # the citation joins pointing at these events go first (child-before-parent) -- the
    # run arm being covered here, the WorkflowRun delete at (7) needs no separate
    # un-scope pass.
    _memev_cond = MemoryEvent.document_id == doc_id
    if seg_ids:
        _memev_cond = _or(_memev_cond, MemoryEvent.segment_id.in_(seg_ids))
    if run_ids:
        _memev_cond = _or(_memev_cond, MemoryEvent.workflow_run_id.in_(run_ids))
    _memev_ids = list(session.exec(select(MemoryEvent.id).where(_memev_cond)).all())
    counts["memory_evidence_deleted"] = 0
    if _memev_ids:
        # Kept as a whole-table delta (#333 gate R2 N1): erase_events' evidence_swept misses
        # the suggestion-evidence rows the entry cascade sweeps, so it undercounts here. This
        # per-document path can still drift under a concurrent capture writer (tracked).
        before = _memory_counts(session)
        _erase_memory_events(session, _memev_ids)
        after = _memory_counts(session)
        counts["memory_evidence_deleted"] = (
            before["memory_evidence_aged"] - after["memory_evidence_aged"]
        )
    counts["memory_events_deleted"] = len(_memev_ids)
    # (6) Batch 15 T2: a DOCUMENT-mode empty-delivery approval FKs this document (and, via
    # run_id, one of its runs), so reap it BEFORE the WorkflowRun + Document deletes (SQLite
    # FK enforcement). Key/project-mode rows (document_id NULL) are NOT this document's --
    # they are reaped by the project-level pass in content.delete_project (step 2c) -- but one
    # of them CAN still carry a run_id pointing at a run we are about to delete (a run over an
    # internal key document). That row must SURVIVE (it is the human's durable release
    # decision, and run_id is documented as informational provenance only), so its run_id is
    # UN-SCOPED, mirroring workflows.delete_workflow's B-H2 treatment. Both counted.
    _dea_n = len(
        session.exec(
            select(DeliveryEmptyApproval.id).where(DeliveryEmptyApproval.document_id == doc_id)
        ).all()
    )
    if _dea_n:
        session.exec(
            sa_delete(DeliveryEmptyApproval).where(DeliveryEmptyApproval.document_id == doc_id)
        )
        session.flush()
    counts["delivery_empty_approvals"] = _dea_n
    _dea_unscoped = 0
    if run_ids:
        _dea_unscoped = len(
            session.exec(
                select(DeliveryEmptyApproval.id).where(DeliveryEmptyApproval.run_id.in_(run_ids))
            ).all()
        )
        if _dea_unscoped:
            session.exec(
                sa_update(DeliveryEmptyApproval)
                .where(DeliveryEmptyApproval.run_id.in_(run_ids))
                .values(run_id=None)
            )
            session.flush()
    counts["delivery_empty_approvals_unscoped"] = _dea_unscoped
    # (7) RUN HISTORY: StepRun -> WorkflowRun -> WorkflowBatch, child before parent,
    # flush-pinned. THIS is the arm whose absence made `retention.purge()` raise
    # IntegrityError on every document that was ever translated (WorkflowRun.document_id is
    # a NON-NULL FK with no cascade), so retention silently aged out NOTHING but untranslated
    # uploads. Retention/erasure of a document takes its run history with it -- the same
    # semantics content.delete_document/delete_project have always implemented.
    counts["step_runs"] = 0
    counts["workflow_runs"] = 0
    if run_ids:
        counts["step_runs"] = len(
            session.exec(select(StepRun.id).where(StepRun.workflow_run_id.in_(run_ids))).all()
        )
        session.exec(sa_delete(StepRun).where(StepRun.workflow_run_id.in_(run_ids)))
        session.flush()
        session.exec(sa_delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))
        session.flush()
        counts["workflow_runs"] = len(run_ids)
    # (8) WorkflowBatch: `document_id` is a NOT-NULL FK to document, so a fan-out batch blocks
    # the Document delete exactly as a run does. Its members are runs of THIS document (all
    # deleted at (7)), but `workflowrun.batch_id` is un-scoped first anyway so a stray member
    # from an anomalous row can never make the batch delete raise.
    _batch_ids = list(
        session.exec(select(WorkflowBatch.id).where(WorkflowBatch.document_id == doc_id)).all()
    )
    if _batch_ids:
        session.exec(
            sa_update(WorkflowRun).where(WorkflowRun.batch_id.in_(_batch_ids)).values(batch_id=None)
        )
        session.flush()
        session.exec(sa_delete(WorkflowBatch).where(WorkflowBatch.id.in_(_batch_ids)))
        session.flush()
    counts["workflow_batches"] = len(_batch_ids)
    # (9) Keep glossary decisions, but scrub document context in candidates and
    # accepted examples BEFORE severing the provenance that identifies the copies.
    from app.services.document_erasure import scrub_term_examples

    counts["term_candidates_unscoped"] = scrub_term_examples(session, doc_id)
    return counts


def delete_document(
    session: Session,
    doc_id: int,
    cascade_tm: bool = True,
    *,
    privacy_erasure: bool | None = None,
    purge_exports: bool = True,
    erase_document_tm: bool = False,
) -> dict:
    """Delete one document: its upload blob, its RUN HISTORY, every row that FK-references
    it or its segments, its Segment rows, the Document row, and (when cascade_tm) its
    project-scoped TM. Single transaction, modelled on gdpr.erase_user. Returns a counts
    dict. A missing document is a harmless no-op. `file_pending` in the counts (#399)
    means the upload blob could NOT be removed and its key was persisted to
    BlobDeleteOutbox in this same transaction for the next purge to retry.
    Privacy erasure defaults to cascade_tm, preserving the existing Art. 17
    default. It deletes Lab snapshots and their runs, scores and gold labels even
    if Lab is disabled or the source was already ordinarily deleted. Ordinary
    deletion preserves snapshots and reports lab_datasets_preserved. Retention
    explicitly requests privacy erasure without deleting shared project TM.

    THE ONE FK-CLEAN DOCUMENT-DELETE SEAM. `content.delete_document` and
    `content.delete_project` (step 4) both delegate here, and `purge` below calls it per
    aged document, so the child-before-parent ordering is derived ONCE — see
    `_reap_document_children` for the referrer table and the delete-vs-sever rationale.
    Deleting a document DELETES ITS RUN HISTORY (StepRun/WorkflowRun/WorkflowBatch and the
    vendor legs anchored to those runs) AND its MemoryEvents (#276: they carry the
    document's before/after text, which must not outlive the document); term-extraction
    decisions and non-document-mode delivery approvals SURVIVE with their narrower FKs
    un-scoped."""
    from app.services.document_erasure import erase_lab_snapshots

    if privacy_erasure is None:
        privacy_erasure = cascade_tm
    doc = session.get(Document, doc_id)
    lab_counts = erase_lab_snapshots(
        session, doc_id, erase=privacy_erasure, created_after=doc.created_at if doc else None
    )
    if not doc:
        if lab_counts["lab_datasets_erased"]:
            session.commit()
        return {"documents": 0, **lab_counts}
    project_id = doc.project_id
    counts = {"documents": 1, **lab_counts}
    seg_ids = session.exec(select(Segment.id).where(Segment.document_id == doc_id)).all()
    counts["segments"] = len(seg_ids)
    # #539: read the document's own content BEFORE the segments go, to find its TM rows.
    doc_tm_rows = (
        [
            (r[0], r[1])
            for r in session.exec(
                select(Segment.source_text, Segment.target_locale).where(
                    Segment.document_id == doc_id
                )
            ).all()
        ]
        if erase_document_tm
        else []
    )
    run_ids = list(
        session.exec(select(WorkflowRun.id).where(WorkflowRun.document_id == doc_id)).all()
    )
    # KEY VALUE TOMBSTONES (SPEC-key-based-projects 2.3, review A3). The bulk delete
    # below is invisible to the key change feed's ORM listener. For an ordinary
    # document that is fine -- the document is the consumer's unit and it is gone --
    # but an INTERNAL KEY DOCUMENT holds one namespace's value rows while the project
    # and every StringKey survive, so a since-cursor consumer would keep zombie values
    # forever. Announce them first, in this same transaction.
    #
    # The PREDICATE IS INLINE and the import is inside the branch (re-review R4).
    # Calling `keys.is_internal_key_document` first imported app.services.keys on EVERY
    # document delete, and importing that module used to arm a process-wide
    # `Thread.start` wrapper -- so a pure document install got key machinery installed
    # behind its back, contradicting app/db.py's own inertness comment, and the
    # inertness probe could not see it (it replaces the listener BODY, not the import).
    # `fmt` is the whole predicate: "keys" is deliberately not a registered filter
    # format, so no upload path can produce it (keys.KEY_DOC_FMT, kept in sync by
    # tests/verify_keys_inertness.py).
    if (getattr(doc, "fmt", None) or "") == "keys":
        from app.services import keys as _keys

        _keys.log_bulk_value_deletions(
            session, session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        )
    # EVERY FK referrer of this document / its segments / its runs, child-before-parent, in
    # the ONE place that ordering is derived (see `_reap_document_children`'s referrer table).
    counts.update(_reap_document_children(session, doc_id, seg_ids, run_ids))
    # The BLOB delete is irreversible and lives OUTSIDE the transaction, so it is done LAST,
    # once every FK referrer is out of the way -- as late as possible before the row deletes
    # that can still legally fail. Doing it first (as this used to) meant a reap that raised
    # rolled the DB back while the upload was already destroyed on disk, leaving a Document
    # row pointing at nothing. Nothing between here and the commit can raise on FK grounds.
    # A FAILED blob delete (an open handle, a transient storage error -- NOT a missing blob)
    # persists its key in THIS transaction (#399): the row deletes below erase the key's only
    # other durable reference, and without the ledger row the next sweep would have nothing
    # to retry, orphaning the uploaded content forever (finding dbdata-07). The erase itself
    # is never refused because storage is flaky -- that is what the retry ledger is for.
    from app.ports.blobstore import DeleteStatus

    blob_status = _delete_upload_file(doc)
    counts["file_removed"] = 1 if blob_status == DeleteStatus.REMOVED else 0
    counts["file_pending"] = 1 if blob_status == DeleteStatus.FAILED else 0
    if blob_status == DeleteStatus.FAILED:
        from app.ports.blobstore import document_key

        session.add(BlobDeleteOutbox(blob_key=document_key(doc), source_document_id=doc_id))
    session.exec(sa_delete(Segment).where(Segment.document_id == doc_id))
    session.exec(sa_delete(Document).where(Document.id == doc_id))
    if cascade_tm:
        # _delete_project_tm reaches SegmentTM + FullTextTM + SegmentEmbedding
        # (all project-scoped) in the same transaction, so an Art. 17 erasure
        # leaves no orphan source-derived vectors behind (GDPR finding).
        counts["tm"] = _delete_project_tm(session, project_id)
    elif erase_document_tm:
        counts["tm"] = _delete_document_tm(session, project_id, doc_tm_rows)
    # DX-207 (Batch 13) + files-03: a STAGED full-dump export covering this document's project
    # would keep the now-erased content alive in EXPORTS_DIR (outside every other erasure
    # sweep). Flip every live export covering the project (project, customer, workspace AND
    # self scopes) to `purged` IN THIS TRANSACTION, so the delete and the invalidation commit
    # together; the staged bytes are removed right after the commit. Routed through gdpr so
    # app.services.data_export stays imported only there + scheduler.
    from app.services import gdpr as _gdpr

    if not purge_exports:
        # #539: the rollback of a failed upload deletes a document that was never exportable;
        # sweeping would cancel every other user's staged workspace/self export.
        session.commit()
        return counts
    pending = _gdpr.mark_export_artifacts_for_project(session, project_id)
    session.commit()
    _gdpr.finish_export_artifact_purge(pending, actor="retention.delete_document")
    # Fallback sweep (the pre-files-03 best-effort path): catches anything the in-transaction
    # mark could not reach (it returns None on error). Idempotent: purged rows are not live.
    try:
        _gdpr.purge_export_artifacts_for_document(project_id, actor="retention.delete_document")
    except Exception:
        log.warning("delete_document: staged-export artifact purge failed (doc=%s)", doc_id)
    return counts


# --- row-level TM + LLMemory aging (issue #276) -----------------------------
def _memory_counts(s: Session) -> dict:
    """Content-bearing rows and receipts, excluding already erased tombstones."""
    from sqlalchemy import String, cast, func

    def count(model, *conditions):
        return s.exec(select(func.count()).select_from(model).where(*conditions)).one()

    return {
        "memory_events_aged": count(MemoryEvent),
        "memory_entries_aged": count(MemoryEntry, MemoryEntry.status != "purged"),
        "memory_suggestions_aged": count(
            MemorySuggestion,
            _or(
                MemorySuggestion.rationale != "",
                MemorySuggestion.diff_text != "",
                cast(MemorySuggestion.proposed_change, String) != "null",
                cast(MemorySuggestion.applied_snapshot, String) != "null",
            ),
        ),
        "memory_evidence_aged": count(MemoryEvidence) + count(MemorySuggestionEvidence),
    }


def _age_out_tm_rows(s: Session, cutoff: datetime) -> int:
    """Materialize live keys once, then commit each bounded TM delete.

    The caller pins the connection so the temporary table survives batch commits.
    SQLite uses a covering index, PostgreSQL uses a hash index for arbitrary-length
    source text. No persistent content copy or quadratic segment scan is created.
    """
    from sqlalchemy import Column, Index, Integer, MetaData, String, Table, func

    from app.models import ProjectTmSource

    live = Table(
        "retention_live_tm",
        MetaData(),
        Column("project_id", Integer),
        Column("target_locale", String),
        Column("source_text", String),
        prefixes=["TEMPORARY"],
    )
    conn = s.connection()
    live.create(conn)
    try:
        own = select(Document.project_id, Segment.target_locale, Segment.source_text).join(
            Segment, Segment.document_id == Document.id
        )
        attached = (
            select(ProjectTmSource.source_project_id, Segment.target_locale, Segment.source_text)
            .select_from(Document)
            .join(Segment, Segment.document_id == Document.id)
            .join(ProjectTmSource, ProjectTmSource.project_id == Document.project_id)
        )
        s.execute(live.insert().from_select(list(live.c.keys()), own.union(attached)))
        if conn.dialect.name == "postgresql":
            Index("ix_retention_live_tm", live.c.source_text, postgresql_using="hash").create(conn)
        else:
            Index("ix_retention_live_tm", *live.c).create(conn)
        s.commit()
        total = 0
        for model in (SegmentTM, FullTextTM):
            last_id = 0
            # Keyset ranges bound both the scan and the write lock, including all-live batches.
            while ids := s.exec(
                select(model.id).where(model.id > last_id).order_by(model.id).limit(500)
            ).all():
                upper = ids[-1]
                matching = (
                    select(live.c.project_id)
                    .where(
                        live.c.project_id == model.project_id,
                        live.c.target_locale == model.target_locale,
                        live.c.source_text == model.source_text,
                    )
                    .exists()
                )
                stale = (
                    (model.id > last_id)
                    & (model.id <= upper)
                    & (
                        func.coalesce(
                            model.activity_at,
                            getattr(model, "last_used_at", None),
                            model.created_at,
                        )
                        < cutoff
                    )
                    & ~matching
                )
                count = 0
                if model is SegmentTM:
                    count += s.exec(
                        sa_delete(SegmentEmbedding)
                        .where(SegmentEmbedding.segment_tm_id.in_(select(model.id).where(stale)))
                        .execution_options(synchronize_session=False)
                    ).rowcount
                count += s.exec(
                    sa_delete(model).where(stale).execution_options(synchronize_session=False)
                ).rowcount
                s.commit()
                total += count
                # Publish committed progress even if a later batch or memory pass fails.
                if "retention_result" in s.info:
                    s.info["retention_result"]["tm_aged"] += count
                last_id = upper
        return total
    finally:
        s.rollback()
        live.drop(s.connection())
        s.commit()


def _memory_batch_aged(s: Session, kind: str, ids: list) -> dict:
    """Erase ONE memory-aging batch and count only the rows the batch itself removed.

    Issue #333 (gate #276 round 3, N1): the counts this replaced were whole-table
    before-minus-after totals, so an LLMemory capture writer inserting a row inside a
    batch window skewed the reported numbers (a 10k-event purge reported 9,997). Each
    key now counts the ids the batch selected and actually deleted or flipped: the
    erasure callees' own row stats (including the suggestion content each tombstone
    cleared, reported by the callee at the point of clearing), the DELETE rowcounts of
    the rows they sweep (an event listener armed on THIS session's pinned connection for
    the duration of one batch, so another thread's statements never reach it), and the
    entry status comparison around the run. A concurrent writer's rows are never
    touched by this transaction, so they can no longer move a count.

    The erasure call is resolved on the service MODULES at call time, so a test may
    patch llmemory.erase_events / llmemory._purge_one /
    llmemory_suggestions.purge_suggestions_for; the counts stay honest when a
    passthrough double returns the callee's real return value.
    """
    from sqlalchemy import event

    from app import extensions as _extensions

    llmemory = _extensions.memory
    llmemory_suggestions = _extensions.memory_suggestions

    ids = list(ids)
    if not llmemory.installed:
        # Add-on absent: its erasure logic is not here to run; a core install has no rows.
        return dict.fromkeys(_memory_counts(s), 0)
    delete_tables = {
        MemoryEvidence.__tablename__: "evidence",
        MemorySuggestionEvidence.__tablename__: "evidence",
    }
    swept = {"evidence": 0}

    def count_swept(conn, cursor, statement, parameters, context, executemany):
        # Only statements on THIS session's pinned connection reach this listener,
        # so every delete it sees was executed by this batch, never by a writer.
        if statement.startswith("DELETE FROM "):
            table = statement[len("DELETE FROM ") :].split(" ", 1)[0].lower()
            if table in delete_tables:
                swept[delete_tables[table]] += cursor.rowcount

    def statuses() -> dict:
        if not ids:
            return {}
        return dict(
            s.exec(select(MemoryEntry.id, MemoryEntry.status).where(MemoryEntry.id.in_(ids))).all()
        )

    def run() -> dict:
        if kind == "events":
            return llmemory.erase_events(s, ids) or {}
        if kind == "entries":
            orphaned = 0
            cleared = 0
            for entry in s.exec(select(MemoryEntry).where(MemoryEntry.id.in_(ids))).all():
                one = llmemory._purge_one(s, entry, "retention", commit=False)
                orphaned += one["events_orphaned"]
                cleared += one["suggestions_content_cleared"]
            return {"orphaned": orphaned, "suggestions_content_cleared": cleared}
        by_customer = {}
        for suggestion in s.exec(
            select(MemorySuggestion).where(MemorySuggestion.id.in_(ids))
        ).all():
            by_customer.setdefault(suggestion.customer_id, []).append(suggestion.id)
        cleared = 0
        for cid, part in by_customer.items():
            cleared += llmemory_suggestions.purge_suggestions_for(
                s, customer_id=cid, suggestion_ids=part, commit=False
            )["suggestions_content_cleared"]
        return {"suggestions_content_cleared": cleared}

    s.flush()
    before_status = statuses() if kind == "entries" else {}
    conn = s.connection()
    event.listen(conn, "after_cursor_execute", count_swept)
    try:
        stats = run()
        s.flush()
    finally:
        event.remove(conn, "after_cursor_execute", count_swept)
    events_aged = (
        stats.get("memory_events_deleted", 0) + stats.get("memory_events_orphaned", 0)
        if kind == "events"
        else stats.get("orphaned", 0)
    )
    entries_aged = (
        sum(
            1
            for rid, status in (statuses() if kind == "entries" else {}).items()
            if status == "purged" and before_status.get(rid) != "purged"
        )
        if kind == "entries"
        else stats.get("entries_tombstoned", 0)
    )
    # Suggestions count what the batch's own tombstones CLEARED, reported by the callee
    # at the point of clearing (#333 fix round 1: a citation-join estimate counted one
    # suggestion once per citing row, and the entries-phase cascade counted nothing).
    suggestions_aged = stats.get("suggestions_content_cleared", 0)
    return {
        "memory_events_aged": events_aged,
        "memory_entries_aged": entries_aged,
        "memory_suggestions_aged": suggestions_aged,
        "memory_evidence_aged": sum(swept.values()),
    }


def _age_out_leftover_memory_rows(s: Session, cutoff: datetime) -> dict:
    """Plain-SQL aging for LLMemory rows left in the tables when the add-on is ABSENT.

    The tables are core and an install carried over from the full edition may still hold
    rows. Without the add-on's tombstoning logic, aged rows are DELETED outright (the
    stronger erasure): events (with their citation joins), then aged entries that no
    fresh event still supports, then aged suggestions. Batches commit like the add-on's."""
    from sqlalchemy import delete as _sa_delete
    from sqlalchemy import update as _sa_update

    out = dict.fromkeys(_memory_counts(s), 0)
    while events := s.exec(
        select(MemoryEvent.id).where(MemoryEvent.created_at < cutoff).limit(500)
    ).all():
        ev = list(events)
        out["memory_evidence_aged"] += (
            s.exec(_sa_delete(MemoryEvidence).where(MemoryEvidence.event_id.in_(ev))).rowcount
            + s.exec(
                _sa_delete(MemorySuggestionEvidence).where(
                    MemorySuggestionEvidence.event_id.in_(ev)
                )
            ).rowcount
        )
        s.exec(_sa_delete(MemoryEvent).where(MemoryEvent.id.in_(ev)))
        out["memory_events_aged"] += len(ev)
        s.commit()
    fresh_evidence = (
        select(MemoryEvidence.id)
        .join(MemoryEvent, MemoryEvidence.event_id == MemoryEvent.id)
        .where(MemoryEvidence.memory_id == MemoryEntry.id, MemoryEvent.created_at >= cutoff)
        .exists()
    )
    while entries := s.exec(
        select(MemoryEntry.id).where(MemoryEntry.created_at < cutoff, ~fresh_evidence).limit(500)
    ).all():
        en = list(entries)
        s.exec(
            _sa_update(MemoryEntry)
            .where(MemoryEntry.superseded_by_id.in_(en))
            .values(superseded_by_id=None)
        )
        s.exec(
            _sa_update(MemorySuggestionEvidence)
            .where(MemorySuggestionEvidence.memory_id.in_(en))
            .values(memory_id=None)
        )
        out["memory_evidence_aged"] += s.exec(
            _sa_delete(MemoryEvidence).where(MemoryEvidence.memory_id.in_(en))
        ).rowcount
        s.exec(_sa_delete(MemoryEntry).where(MemoryEntry.id.in_(en)))
        out["memory_entries_aged"] += len(en)
        s.commit()
    last_id = 0
    while suggestions := s.exec(
        select(MemorySuggestion.id)
        .where(MemorySuggestion.created_at < cutoff, MemorySuggestion.id > last_id)
        .order_by(MemorySuggestion.id)
        .limit(500)
    ).all():
        sg = list(suggestions)
        last_id = sg[-1]
        s.exec(
            _sa_update(MemorySuggestion)
            .where(MemorySuggestion.superseded_by_id.in_(sg))
            .values(superseded_by_id=None)
        )
        out["memory_evidence_aged"] += s.exec(
            _sa_delete(MemorySuggestionEvidence).where(
                MemorySuggestionEvidence.suggestion_id.in_(sg)
            )
        ).rowcount
        s.exec(_sa_delete(MemorySuggestion).where(MemorySuggestion.id.in_(sg)))
        out["memory_suggestions_aged"] += len(sg)
        s.commit()
    if "retention_result" in s.info:
        for key, count in out.items():
            s.info["retention_result"][key] += count
    return out


def _age_out_tm_and_memory_rows(s: Session, cutoff: datetime) -> dict:
    """Commit TM and memory in bounded batches, publishing only committed counts.

    Each memory batch commits and reports the rows IT erased (see
    _memory_batch_aged); whole-table before/after counting drifted under a concurrent
    LLMemory capture writer (#333).
    """
    tm_count = _age_out_tm_rows(s, cutoff)
    from app import extensions as _extensions

    if not _extensions.memory.installed:
        return {"tm_aged": tm_count, **_age_out_leftover_memory_rows(s, cutoff)}

    totals = dict.fromkeys(_memory_counts(s), 0)

    def yield_writer():
        # SQLite's busy handler polls. Leave a small unlocked interval so a
        # waiting save can acquire the writer slot between successive batches.
        if s.bind.dialect.name == "sqlite":
            import time

            time.sleep(0.1)

    def commit_memory(delta):
        s.commit()
        for key, count in delta.items():
            totals[key] += count
            if "retention_result" in s.info:
                s.info["retention_result"][key] += count
        yield_writer()

    # Re-query a bounded batch after each erasure; no whole-table IN list.
    while events := s.exec(
        select(MemoryEvent.id).where(MemoryEvent.created_at < cutoff).limit(500)
    ).all():
        commit_memory(_memory_batch_aged(s, "events", events))
    # After aged receipts are erased, a supported belief still belongs to the window.
    # Do not feed it to the Art.17 eraser, which intentionally erases all receipts.
    fresh_evidence = (
        select(MemoryEvidence.id)
        .join(MemoryEvent, MemoryEvidence.event_id == MemoryEvent.id)
        .where(MemoryEvidence.memory_id == MemoryEntry.id, MemoryEvent.created_at >= cutoff)
        .exists()
    )
    from sqlalchemy import func, update

    last_id = 0
    while ids := s.exec(
        select(MemoryEntry.id)
        .where(
            MemoryEntry.id > last_id,
            MemoryEntry.created_at < cutoff,
            MemoryEntry.status != "purged",
            fresh_evidence,
        )
        .order_by(MemoryEntry.id)
        .limit(500)
    ).all():
        s.exec(
            update(MemoryEntry)
            .where(
                MemoryEntry.id > last_id,
                MemoryEntry.id <= ids[-1],
                MemoryEntry.created_at < cutoff,
                MemoryEntry.status != "purged",
                fresh_evidence,
            )
            .values(
                evidence_count=select(func.count())
                .select_from(MemoryEvidence)
                .where(MemoryEvidence.memory_id == MemoryEntry.id)
                .scalar_subquery()
            )
            .execution_options(synchronize_session=False)
        )
        s.commit()
        yield_writer()
        last_id = ids[-1]
    while entries := s.exec(
        select(MemoryEntry.id)
        .where(MemoryEntry.created_at < cutoff, MemoryEntry.status != "purged", ~fresh_evidence)
        .limit(500)
    ).all():
        commit_memory(_memory_batch_aged(s, "entries", entries))
    last_id = 0
    while suggestions := s.exec(
        select(MemorySuggestion.id)
        .where(MemorySuggestion.created_at < cutoff, MemorySuggestion.id > last_id)
        .order_by(MemorySuggestion.id)
        .limit(500)
    ).all():
        commit_memory(_memory_batch_aged(s, "suggestions", suggestions))
        last_id = suggestions[-1]
    return {"tm_aged": tm_count, **totals}


# --- the scheduled purge ----------------------------------------------------
@MaintenancePass(lambda: {"busy": True})
def purge(now: datetime | None = None) -> dict:
    """Delete aged uploads, their document content and terminal jobs.

    TM ages by coalesce(activity_at, last_used_at, created_at). Full-text TM has
    no usage column, so its legacy fallback is created_at.
    Surviving document sources, including attached-source borrowers and internal
    catalogues, protect matching TM. Each TM batch commits independently. Memory
    events age on their own clock; supported beliefs retain their fresh evidence.
    Documents use the shared Art.17 erasure path without a project-wide TM cascade.
    Failed documents and row-aging failures are reported and retried next sweep.
    A document whose upload blob could NOT be deleted leaves its key in the
    BlobDeleteOutbox (same transaction); every sweep retries those keys and reports
    them as pending until a delete finally succeeds (#399).
    Disabled retention or a zero window skips new age-based deletion, but still
    retries previously authorized blob erasures and reports their counters.
    """
    # `key_image_blobs` is a DISTINCT counter (M-4): `files` means aged upload files reaped
    # by delete_document, so folding key-image reaps into it would desync the irreversible-
    # purge confirm dialog + the audit `detail` from what was actually removed -- the exact
    # under-reporting the delete_project_preview count exists to prevent.
    # `documents_skipped` (2026-08-31 fix-train): documents this sweep could NOT delete. It is
    # a REPORTED counter, not a silent one -- the whole reason this bug went unnoticed for so
    # long is that a purge that deleted nothing looked exactly like a purge with nothing to do.
    result = {
        "documents": 0,
        "segments": 0,
        "tm": 0,
        "jobs": 0,
        "jobqueue_rows": 0,
        "files": 0,
        "key_image_blobs": 0,
        "documents_skipped": 0,
        # H3 + round-1 H1/M1 (2026-09-05): the Advanced tier's own document-derived content erased
        # for the aged documents -- run-parented findings reaped, staged rows severed, transcript
        # content entries removed, mission-roster previews stripped. REPORTED counters, like
        # documents_skipped -- the whole reason the leak went unnoticed is that a purge that reaped
        # nothing looked exactly like a purge with nothing to do.
        "advanced_findings_reaped": 0,
        "advanced_staged_erased": 0,
        "advanced_transcript_rows_erased": 0,
        "advanced_roster_previews_erased": 0,
        # round-3 M2: scripted roster units skipped for a non-integer segment_id/key_id -- a
        # REPORTED counter so a malformed mission never silently blocks the sweep (it used to
        # RAISE, counting the whole document documents_skipped forever).
        "advanced_units_unparseable": 0,
        # #276 (2026-09-28): per-document MemoryEvents DELETED by the aged documents'
        # delete_document passes (they carry the document's before/after text), plus the
        # row-level aging of TM + LLMemory rows in projects/customers that stay live
        # (see _age_out_tm_and_memory_rows) -- REPORTED counters, same reasoning as
        # documents_skipped.
        "memory_events_deleted": 0,
        "lab_datasets_erased": 0,
        "tm_aged": 0,
        "row_aging_failed": 0,
        "memory_events_aged": 0,
        "memory_entries_aged": 0,
        "memory_suggestions_aged": 0,
        "memory_evidence_aged": 0,
        # #399 (finding dbdata-07): the blob-delete outbox drain. `blob_deletes_retried`
        # counts ledger rows RESOLVED this sweep (a retried delete removed the blob or
        # proved it gone); `blob_deletes_pending` counts keys storage still refuses to
        # give up -- the erasure is PENDING until the delete succeeds, and the keys stay
        # durable in BlobDeleteOutbox until then. REPORTED counters, same reasoning.
        "blob_deletes_retried": 0,
        "blob_deletes_pending": 0,
    }
    # Previously authorized erasures are independent of the age-retention policy.
    # Drain before either policy gate, including on default-config scheduler ticks.
    try:
        with Session(engine) as s:
            _retried, _pending = _drain_blob_delete_outbox(s)
        result["blob_deletes_retried"] = _retried
        result["blob_deletes_pending"] = _pending
        if _retried or _pending:
            audit.log(
                actor="retention",
                action="retention.blob_delete_retry",
                detail=f"resolved={_retried} file_pending={_pending}",
            )
    except Exception:
        log.exception("Retention: blob-delete outbox drain failed (sweep continues)")
    if not enabled():
        return result
    now = now or datetime.utcnow()
    cutoff = _cutoff(now)
    if cutoff is None:
        return result  # retention_days=0: no new age-based erasures

    with Session(engine) as s:
        from app.services import assist_limits

        assist_limits.delete_expired(s, now)
        s.commit()
        # SPEC-key-based-projects 2.5 (Wave B fix round, A-F5): INTERNAL KEY DOCUMENTS
        # ARE NEVER AGED OUT. They are created ONCE, on the first key in a namespace,
        # and their `created_at` never moves again — so on any install with retention
        # on, every key project's catalogue was GUARANTEED to be destroyed exactly
        # `retention_days` after it was started, no matter how live its keys were.
        # (`delete_document` even emits honest tombstones for the reaped values, so
        # the feed would have told consumers that a fully live catalogue had been
        # deleted key by key.) Retention ages UPLOADS; a key document is not one, and
        # its rows are the product's current state rather than an aged artefact.
        # `fmt` alone is the whole predicate here — "keys" is deliberately not a
        # registered filter format, so no upload path can mint it (the idiom
        # `delete_document` below already uses).
        old_docs = s.exec(
            select(Document.id).where(Document.created_at < cutoff, Document.fmt != "keys")
        ).all()
    for doc_id in old_docs:
        # TOTALITY GUARD, per document (the `_delete_upload_file` philosophy, applied to the
        # loop itself). This loop was UNGUARDED, so ONE pathological document aborted the
        # ENTIRE sweep -- which is exactly how the WorkflowRun FK bug hid: the first aged
        # translated document raised IntegrityError out of tick(), the daemon logged
        # "Retention tick crashed", and every YOUNGER aged document, every aged Job and every
        # stale key-image blob went unreaped too, forever. Isolate the blast radius to the one
        # document, log it LOUDLY (log.exception, not a debug line) and keep sweeping.
        try:
            with Session(engine) as s:
                # #543: a document with a live run is NOT aged out (it is re-checked on the
                # next tick); deleting it here destroyed the run's delivery mid-flight.
                if _document_has_inflight_run(s, doc_id):
                    continue
                # cascade_tm=False: TM is project-scoped and shared — never reap it for
                # a single aged document while siblings may still be live.
                counts = delete_document(s, doc_id, cascade_tm=False, privacy_erasure=True)
        except Exception:
            result["documents_skipped"] += 1
            log.exception(
                "Retention: could not purge document %s (skipped, sweep continues)", doc_id
            )
            continue
        result["documents"] += counts.get("documents", 0)
        result["segments"] += counts.get("segments", 0)
        result["files"] += counts.get("file_removed", 0)
        result["blob_deletes_pending"] += counts.get("file_pending", 0)
        result["advanced_findings_reaped"] += counts.get("advanced_findings_reaped", 0)
        result["advanced_staged_erased"] += counts.get("advanced_staged_erased", 0)
        result["advanced_transcript_rows_erased"] += counts.get(
            "advanced_transcript_rows_erased", 0
        )
        result["advanced_roster_previews_erased"] += counts.get(
            "advanced_roster_previews_erased", 0
        )
        result["advanced_units_unparseable"] += counts.get("advanced_units_unparseable", 0)
        result["memory_events_deleted"] += counts.get("memory_events_deleted", 0)
        result["lab_datasets_erased"] += counts.get("lab_datasets_erased", 0)

    try:
        from app.services.document_erasure import erase_aged_orphan_snapshots

        with Session(engine) as s:
            erased = erase_aged_orphan_snapshots(s, cutoff)
            s.commit()
            result["lab_datasets_erased"] += erased
    except Exception:
        result["row_aging_failed"] = 1
        log.exception("Retention: orphan Lab snapshot aging failed (sweep continues)")

    # (#276) Row-level aging: TM + LLMemory rows older than the cutoff are deleted even
    # while their project/customer still has live documents (HECVAT AI-35 / Privacy-79).
    # Own transaction AFTER the per-document sweep, guarded like the loop itself: a
    # failure here is logged loudly and never unwinds the document deletes already
    # committed (they run in their own sessions).
    try:
        with engine.connect() as conn, Session(conn) as s:
            s.info["retention_result"] = result
            aged_counts = _age_out_tm_and_memory_rows(s, cutoff)
            s.commit()
            result.update(aged_counts)
    except Exception:
        result["row_aging_failed"] = 1
        log.exception("Retention: row-level TM/memory aging failed (sweep continues)")

    # Batch 14 backstop (LAW-IMG-16): reap the blobs of (a) long-soft-deleted KeyImage rows
    # and (b) `ready=False` stubs older than the cutoff (a crash between the row commit and
    # the blob put left a findable stub), then hard-delete those rows. Best-effort: a
    # missing blob or a delete error NEVER unwinds the sweep. Counted under its OWN
    # `key_image_blobs` key (M-4), distinct from `files` (aged upload docs).
    with Session(engine) as s:
        # `_or` is now imported at module level (the run-history reap needs it); only the
        # KeyImage/blobstore imports stay lazy here.
        from app.models import KeyImage
        from app.ports import blobstore

        stale = s.exec(
            select(KeyImage.id, KeyImage.blob_key).where(
                _or(
                    KeyImage.deleted_at.is_not(None) & (KeyImage.deleted_at < cutoff),
                    (KeyImage.ready == False) & (KeyImage.uploaded_at < cutoff),  # noqa: E712
                )
            )
        ).all()
        if stale:
            _store = blobstore.get()
            for _iid, _bk in stale:
                if _bk:
                    status = _delete_blob_key(_store, _bk)
                    if status == blobstore.DeleteStatus.REMOVED:
                        result["key_image_blobs"] += 1
                    elif status == blobstore.DeleteStatus.FAILED:
                        if (
                            s.exec(
                                select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == _bk)
                            ).first()
                            is None
                        ):
                            s.add(BlobDeleteOutbox(blob_key=_bk))
                        result["blob_deletes_pending"] += 1
            s.exec(sa_delete(KeyImage).where(KeyImage.id.in_([i for i, _b in stale])))
            s.commit()

    # Terminal Jobs only — NEVER touch a QUEUED/RUNNING job (a live worker owns it).
    with Session(engine) as s:
        old_jobs = s.exec(
            select(Job.id).where(Job.created_at < cutoff, Job.status.in_(_TERMINAL))
        ).all()
        if old_jobs:
            s.exec(
                sa_delete(Job).where(Job.id.in_(old_jobs), Job.status.in_(_TERMINAL))
            )  # re-assert terminal at delete time
            s.commit()
        result["jobs"] = len(old_jobs)
        # #438: with job_queue_backend=db every dispatched Job also left one `jobqueue` row,
        # which release() only marks done/failed and nothing else ever deleted. Reap the
        # FINISHED rows older than the cutoff in the same sweep; a pending/running row is live
        # dispatch state and is never touched. Harmless (a no-op) on the memory backend.
        from app.models import JobQueue, JobQueueState

        _finished = (JobQueueState.DONE, JobQueueState.FAILED)
        reaped_q = s.exec(
            sa_delete(JobQueue).where(JobQueue.state.in_(_finished), JobQueue.updated_at < cutoff)
        ).rowcount
        s.commit()
        result["jobqueue_rows"] = int(reaped_q or 0)

    if any(result.values()):
        try:
            audit.log(
                actor="retention",
                action="retention.purge",
                detail=(
                    f"days={retention_days()} docs={result['documents']} "
                    f"segs={result['segments']} tm={result['tm']} "
                    f"jobs={result['jobs']} jobqueue_rows={result['jobqueue_rows']} "
                    f"files={result['files']} "
                    f"key_image_blobs={result['key_image_blobs']} "
                    f"skipped={result['documents_skipped']} "
                    f"advanced_findings_reaped={result['advanced_findings_reaped']} "
                    f"advanced_staged_erased={result['advanced_staged_erased']} "
                    f"advanced_transcript_rows_erased={result['advanced_transcript_rows_erased']} "
                    f"advanced_roster_previews_erased={result['advanced_roster_previews_erased']} "
                    f"advanced_units_unparseable={result['advanced_units_unparseable']} "
                    f"memory_events_deleted={result['memory_events_deleted']} "
                    f"lab_datasets_erased={result['lab_datasets_erased']} "
                    f"tm_aged={result['tm_aged']} "
                    f"row_aging_failed={result['row_aging_failed']} "
                    f"memory_events_aged={result['memory_events_aged']} "
                    f"memory_entries_aged={result['memory_entries_aged']} "
                    f"memory_suggestions_aged={result['memory_suggestions_aged']} "
                    f"memory_evidence_aged={result['memory_evidence_aged']} "
                    f"blob_deletes_retried={result['blob_deletes_retried']} "
                    f"blob_deletes_pending={result['blob_deletes_pending']}"
                ),
            )
        except Exception:
            log.exception("Audit log failed for retention purge")
        log.info("Retention purge removed %s", result)
    return result


def tick(now: datetime | None = None) -> dict:
    """One scheduler pass — the daemon loop calls this every TICK_SECONDS; tests
    call it directly (deterministic, no thread)."""
    return purge(now)


def _loop() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        try:
            tick()
        except Exception:
            log.exception("Retention tick crashed")


def start() -> None:
    """Start the retention daemon thread (idempotent). Started only on real
    installs (see app/main._startup); tests drive tick()/purge() directly."""
    global _started
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_loop, name="loc-retention", daemon=True).start()
        _started = True
