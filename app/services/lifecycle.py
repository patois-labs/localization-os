"""Project status lifecycle (P1, items 2+3) — the single authority for moving a
project through its ProjectStatus states, shared by the web routes and the CLI.

Design (grounded in MASTER_PLAN P1 + DECISIONS 1/2 + digest §item-2):
  * set_status()  — a validated manual/automatic transition; writes
                    status_changed_at, stamps completed_at the first time a project
                    reaches COMPLETED, and audits `project.status`.
  * mark_completed_if_done() — idempotent auto-complete: called after a translate
                    job / workflow run finishes; flips a still-active project to
                    COMPLETED only when EVERY target locale of EVERY document is
                    fully translated (never fires on a partially-translated
                    multi-locale project — else it would close work early).
  * reopen_if_terminal() — living-projects reopen (Phrase Completed→Assigned):
                    adding a locale or uploading a document to a COMPLETED/CLOSED
                    project flips it back to IN_TRANSLATION. Never wipes targets —
                    it only changes status.
  * document_status() / document_status_map() — the DERIVED document lifecycle (new,
                    in translation, in review, done) read from the segments; the stored
                    Document.status column is never moved, so surfaces print this.
  * auto_close_tick() — the scheduler hook: COMPLETED projects whose completed_at
                    is older than `project_auto_close_days` become CLOSED (audited
                    `project.auto_close`); the setting "0" disables it.

Completion predicate (mark_completed_if_done): the project has >=1 document, and
for every segment across its documents `target_text` is non-empty. Because ingest
seeds one segment set per project TargetLocale, "all segments translated" is
exactly "all locales of all documents done". An empty project (no documents, or a
document with no segments) is NEVER auto-completed.

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.models import Document, Project, ProjectStatus, Segment
from app.services import audit


def _utcnow() -> datetime:
    return datetime.utcnow()


# Statuses that are "still working" — a reopen/auto-complete may move them, and a
# terminal status (closed/cancelled) may still reopen (living projects).
_ACTIVE_FOR_COMPLETE = {
    ProjectStatus.DRAFT,
    ProjectStatus.PRE_TRANSLATING,
    ProjectStatus.IN_TRANSLATION,
    ProjectStatus.IN_REVIEW,
}
_TERMINAL = {ProjectStatus.COMPLETED, ProjectStatus.CLOSED, ProjectStatus.CANCELLED}


# --- Derived document status (#563) -------------------------------------------------
# Document.status is stamped "new" at upload and never moved, so every surface that
# printed it said NEW forever. The real lifecycle is a fact about the document's
# segments across ALL its target languages, so it is derived on read, never stored:
#   new            no segment has any target text yet (or the document has no segments)
#   in_translation some segments are translated and some are still untouched
#   in_review      every segment has a translation, but not all are approved
#   done           every segment of every language is approved
DOC_STATUS_LABELS = {
    "new": "New",
    "in_translation": "In translation",
    "in_review": "In review",
    "done": "Done",
}


def _derive_document_status(untouched: int, translated_open: int, approved: int) -> str:
    total = untouched + translated_open + approved
    if total == 0 or untouched == total:
        return "new"
    if approved == total:
        return "done"
    if untouched:
        return "in_translation"
    return "in_review"


def document_status_map(session: Session, doc_ids) -> dict[int, str]:
    """{document_id: derived status} for the given documents, from ONE grouped segment
    query per chunk of ids (never per-document). A document with no segments is "new"."""
    from sqlalchemy import func

    ids = [int(i) for i in doc_ids if i is not None]
    counts: dict[int, list[int]] = {i: [0, 0, 0] for i in ids}  # untouched, open, approved
    has_target = func.length(func.trim(Segment.target_text)) > 0
    for start in range(0, len(ids), 150):
        chunk = ids[start : start + 150]
        rows = session.exec(
            select(Segment.document_id, Segment.status, has_target, func.count())
            .where(Segment.document_id.in_(chunk))
            .group_by(Segment.document_id, Segment.status, has_target)
        ).all()
        for doc_id, seg_status, target, n in rows:
            bucket = counts[doc_id]
            if seg_status == "approved":
                bucket[2] += n
            elif seg_status == "new" and not target:
                bucket[0] += n
            else:
                bucket[1] += n
    return {i: _derive_document_status(*counts[i]) for i in ids}


def document_status(session: Session, doc: Document) -> str:
    """The derived lifecycle status of one document (see DOC_STATUS_LABELS)."""
    return document_status_map(session, [doc.id]).get(doc.id, "new")


def _has_target_py(target_text) -> bool:
    """`length(trim(target_text)) > 0` as the database evaluates it: trim strips only
    spaces (Postgres and SQLite alike), NULL is false. NOT str.strip(), which also strips
    tabs and newlines and would call a target of "\\n" empty."""
    return target_text is not None and len(target_text.strip(" ")) > 0


def document_status_from_segments(segments) -> str:
    """The derived status of ONE document from its segment rows already in hand (any object
    with `.status` and `.target_text`; ALL the document's segments, every locale). Exactly
    the buckets document_status_map counts in SQL, so a caller that loaded the segments
    anyway does not run the grouped query a second time."""
    untouched = open_ = approved = 0
    for sg in segments:
        if sg.status == "approved":
            approved += 1
        elif sg.status == "new" and not _has_target_py(sg.target_text):
            untouched += 1
        else:
            open_ += 1
    return _derive_document_status(untouched, open_, approved)


def document_status_column():
    """A correlated scalar subquery giving a Document row's derived status (the same value
    document_status_map computes), so a list of documents reads its statuses in the same
    statement as the documents. Use inside a select whose FROM is `document`.

    The buckets are document_status_map's: approved; status "new" with no target text
    (NULL, empty or spaces only: the SQL trim) is untouched; everything else is open."""
    from sqlalchemy import and_, case, func, or_
    from sqlalchemy import select as sa_select

    approved = case((Segment.status == "approved", 1), else_=0)
    untouched = case(
        (
            and_(
                Segment.status == "new",
                or_(
                    Segment.target_text.is_(None), func.length(func.trim(Segment.target_text)) == 0
                ),
            ),
            1,
        ),
        else_=0,
    )
    total = func.count()
    n_untouched = func.sum(untouched)
    status = case(
        (or_(total == 0, n_untouched == total), "new"),
        (func.sum(approved) == total, "done"),
        (n_untouched > 0, "in_translation"),
        else_="in_review",
    )
    return (
        sa_select(status)
        .where(Segment.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


# #580: the MANUAL status graph, enforced by the user-facing seams (web form, /api, CLI) via
# transition_error(). Built from what the app already means by each status: the four working
# states move freely among themselves and to completed or cancelled; a completed project
# can be reopened, closed or cancelled (the project page's Reopen / Close / Cancel buttons);
# closed and cancelled are terminal except the page's Reopen (back to in_translation).
# set_status() itself stays unguarded on purpose: the app's own writes (auto-complete, the
# living-project reopen, the auto-close clock) go through it and must never be refused.
_WORKING_STATES = (
    ProjectStatus.DRAFT,
    ProjectStatus.PRE_TRANSLATING,
    ProjectStatus.IN_TRANSLATION,
    ProjectStatus.IN_REVIEW,
)
MANUAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    **{
        s: tuple(t for t in ProjectStatus.ALL if t != s and t != ProjectStatus.CLOSED)
        for s in _WORKING_STATES
    },
    ProjectStatus.COMPLETED: (
        ProjectStatus.IN_TRANSLATION,
        ProjectStatus.IN_REVIEW,
        ProjectStatus.CLOSED,
        ProjectStatus.CANCELLED,
    ),
    ProjectStatus.CLOSED: (ProjectStatus.IN_TRANSLATION,),
    ProjectStatus.CANCELLED: (ProjectStatus.IN_TRANSLATION,),
}


def allowed_next(status: str) -> tuple[str, ...]:
    """The statuses a manual change may move a project in `status` to (empty for an
    unknown status, so a corrupt row is refused rather than waved through)."""
    return MANUAL_TRANSITIONS.get(status, ())


def transition_error(before: str, target: str) -> str | None:
    """None when a manual move before -> target is allowed, else the message the surfaces
    show (400 on /api, usage exit on the CLI, a flash on the web): it names the move and
    lists the allowed next states."""
    if target in allowed_next(before):
        return None
    if target == before:
        return (
            f"project is already {before!r}; allowed next states: "
            f"{', '.join(allowed_next(before)) or '(none)'}."
        )
    return (
        f"cannot move a {before!r} project to {target!r}; allowed next states: "
        f"{', '.join(allowed_next(before)) or '(none)'}."
    )


def set_status(
    session: Session, project_id: int, status: str, actor: str = "system", ip: str = ""
) -> Project | None:
    """Transition a project to `status`. Validates the target against
    ProjectStatus.ALL; a no-op transition (already in that status) still returns
    the project but writes nothing. Stamps completed_at the FIRST time the project
    reaches COMPLETED (the auto-close anchor) and clears it when it leaves a
    terminal state (so a reopened-then-recompleted project restarts its clock).
    Audits `project.status`. Returns the Project, or None when it doesn't exist /
    the status is invalid."""
    if status not in ProjectStatus.ALL:
        return None
    project = session.get(Project, project_id)
    if project is None:
        return None
    before = project.status
    if before == status:
        return project
    project.status = status
    project.status_changed_at = _utcnow()
    if status == ProjectStatus.COMPLETED and project.completed_at is None:
        project.completed_at = _utcnow()
    elif status not in (ProjectStatus.COMPLETED, ProjectStatus.CLOSED):
        # Leaving the completed→closed track (reopen / cancel) resets the anchor so
        # a future re-completion starts a fresh 5-day clock.
        project.completed_at = None
    session.add(project)
    session.commit()
    session.refresh(project)
    # R2-3: stamp the customer (project-scoped event) so a customer-bound user's
    # /audit shows its own status changes and never another customer's.
    audit.log(
        actor=actor or "system",
        action="project.status",
        target=f"project {project.id}",
        detail=f"{before} -> {status}",
        ip=ip,
        customer_id=project.customer_id,
    )
    if status == ProjectStatus.COMPLETED:
        # Part B notifications: never raises, nothing happens until a channel routes the event.
        from app.services import notify

        notify.notify("project.completed", project.id, {"dedup": f"completed:{project.id}"})
    return project


def _all_segments_translated(session: Session, project_id: int, stopped_locales=None) -> bool:
    """True when the project has >=1 document AND every segment across its
    documents carries non-empty target_text. False for an empty project (no docs
    or no segments) — never auto-complete work that hasn't started.

    `stopped_locales`: the project's stopped target locales (raw `TargetLocale.locale`
    values) when the caller has already read them (mark_completed_if_done reads them with
    the project row); None reads them here."""
    # SPEC-key-based-projects 2.5: internal key documents are excluded. Their rows
    # include a SOFT-DELETED key's parked values (keys.trash_document), which would
    # hold a project below 100% forever; and a key project is continuous by design,
    # so "every string translated => the project is complete" is not a statement its
    # owner wants made automatically. A key project therefore never auto-completes.
    # S4 query diet (ADR 0093): ONE read of just the two columns the predicate needs,
    # joined to the project's non-key documents (it used to read every document row, then
    # every full segment row of the project, after each finished translate job). No
    # document -> no segment row -> False, as before.
    from sqlalchemy import bindparam

    from app import db

    segs = session.exec(
        db.prebuilt(  # S7b: built once (this runs after every translate job)
            "lifecycle.project_segment_targets",
            lambda: (
                select(Segment.target_locale, Segment.target_text)
                .join(Document, Document.id == Segment.document_id)
                .where(Document.project_id == bindparam("project_id"), Document.fmt != "keys")
            ),
        ),
        params={"project_id": project_id},
    ).all()
    # A connected project's STOPPED languages are kept but no longer worked on: their open
    # segments must not hold the project below 100% for ever.
    from app.models import TargetLocale
    from app.services import languages_catalog

    if stopped_locales is None:
        stopped_locales = [
            tl.locale
            for tl in session.exec(
                select(TargetLocale).where(
                    TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_not(None)
                )
            ).all()
        ]
    stopped = {languages_catalog.normalize(loc) for loc in stopped_locales}
    if stopped:
        segs = [s for s in segs if languages_catalog.normalize(s.target_locale) not in stopped]
    if not segs:
        return False
    return all((s.target_text or "").strip() for s in segs)


def mark_completed_if_done(session: Session, project_id: int, actor: str = "system") -> bool:
    """Auto-complete: if the project is still ACTIVE and every locale of every
    document is fully translated, transition it to COMPLETED. Idempotent (a
    COMPLETED/CLOSED/CANCELLED project is left alone) and safe on partial multi-
    locale projects (the predicate demands ALL segments translated). Returns True
    when it transitioned, else False."""
    # S4 follow-up: the project and its stopped target locales in one read (a project with
    # none comes back once with locale None).
    from sqlalchemy import bindparam

    from app import db
    from app.models import TargetLocale

    rows = session.exec(
        db.prebuilt(  # S7b: built once (this runs after every translate job)
            "lifecycle.project_stopped_locales",
            lambda: (
                select(Project, TargetLocale.locale)
                .outerjoin(
                    TargetLocale,
                    (TargetLocale.project_id == Project.id) & TargetLocale.stopped_at.is_not(None),
                )
                .where(Project.id == bindparam("project_id"))
            ),
        ),
        params={"project_id": project_id},
    ).all()
    project = rows[0][0] if rows else None
    if project is None or project.status not in _ACTIVE_FOR_COMPLETE:
        return False
    stopped = [loc for _p, loc in rows if loc is not None]
    if not _all_segments_translated(session, project_id, stopped_locales=stopped):
        return False
    set_status(session, project_id, ProjectStatus.COMPLETED, actor=actor)
    return True


def reopen_if_terminal(session: Session, project_id: int, actor: str = "system") -> bool:
    """Living-projects reopen: a COMPLETED or CLOSED project transitions back to
    IN_TRANSLATION when content/a locale is added (the caller invokes this AFTER
    the additive change). CANCELLED is NOT auto-reopened (an explicit manual
    decision to abandon). Never touches existing targets. Returns True when it
    reopened, else False."""
    project = session.get(Project, project_id)
    if project is None:
        return False
    if project.status not in (ProjectStatus.COMPLETED, ProjectStatus.CLOSED):
        return False
    set_status(session, project_id, ProjectStatus.IN_TRANSLATION, actor=actor)
    return True


def auto_close_days() -> int:
    """The configured auto-close window in days; 0 disables auto-close."""
    from app.services import settings

    try:
        return max(0, int(str(settings.get("project_auto_close_days") or "0").strip()))
    except (TypeError, ValueError):
        return 0


def auto_close_tick(now: datetime | None = None) -> list[int]:
    """One auto-close pass, hooked into the existing scheduler thread. Moves every
    COMPLETED project whose completed_at is older than `project_auto_close_days` to
    CLOSED (audited `project.auto_close`). Returns the project ids it closed. A
    no-op when the setting is "0". Tests call this directly with a fixed `now` +
    backdated completed_at (deterministic, no thread)."""
    days = auto_close_days()
    if days <= 0:
        return []
    now = now or _utcnow()
    cutoff = now - timedelta(days=days)
    from app.db import engine

    with Session(engine) as s:
        due = s.exec(
            select(Project).where(
                Project.status == ProjectStatus.COMPLETED,
                Project.completed_at.is_not(None),
                Project.completed_at < cutoff,
            )
        ).all()
        due_ids = [p.id for p in due]
    closed: list[int] = []
    for pid in due_ids:
        with Session(engine) as s:
            project = s.get(Project, pid)
            # Re-check under a fresh read: a concurrent reopen since the scan must
            # be honored (never close a project that left COMPLETED).
            if project is None or project.status != ProjectStatus.COMPLETED:
                continue
            before = project.status
            cust_id = project.customer_id
            project.status = ProjectStatus.CLOSED
            project.status_changed_at = now
            s.add(project)
            s.commit()
        audit.log(
            actor="system",
            action="project.auto_close",
            target=f"project {pid}",
            detail=f"{before} -> closed (>{days}d)",
            customer_id=cust_id,
        )
        closed.append(pid)
    return closed
