"""Completion email notifications (MCP Phase B chunk 5).

When a document-translation job reaches a terminal state, email the person who
submitted it — the MCP / Claude Desktop flow is fire-and-forget from a chat
client, so the submitter has nowhere to watch progress; an email is how they
learn their files are ready (or that it failed and needs a resubmit).

Scope, deliberately narrow:
- Only `translate_document` jobs. Connector jobs are companion-app machinery
  (schedules, automations) — emailing on every one would be noise.
- Only jobs whose `created_by` is a real email address (a per-user API key /
  signed-in actor). Anonymous / system / pseudo-actors ("scheduler",
  "airtable-automation", "companion-app") get nothing.
- mailer.send returns False when SMTP is unconfigured, so this is a graceful
  no-op on an air-gapped install — the setting `notify_on_job_finish` defaults ON
  precisely because it costs nothing without SMTP.

Called from jobs._execute at the single terminal chokepoint, wrapped in the
caller's try/except so a mail failure never crashes the worker (mirrors the
scheduler's guarded-audit pattern).

Documented in: docs/architecture/incident-response-and-observability.md
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.models import Document, Job, JobStatus, Project, QueryPost, QueryThread, Role, User
from app.services import content, mailer, settings

log = logging.getLogger("loc_tms.notifications")


def enabled() -> bool:
    return settings.get("notify_on_job_finish", "1") == "1"


def _looks_like_email(actor: str) -> bool:
    return bool(actor) and "@" in actor and " " not in actor.strip()


def _active_email(s: Session, email: str) -> str:
    """M-4 (gate fix): re-validate a recipient AT SEND TIME. The assignee/author was a real
    in-scope active user when the thread was written, but they may since have been
    DEACTIVATED, moved, or DELETED — and a query thread outlives the session that created
    it. Return the email only when it still resolves to an ACTIVE User; else "" (no send).
    A vendor/external address is never a query-mail recipient (author/assignee are always
    platform users), so requiring an active User row is correct, not over-strict."""
    from sqlmodel import func

    e = (email or "").strip()
    if not _looks_like_email(e):
        return ""
    u = s.exec(select(User).where(func.lower(User.email) == e.lower(), User.active == True)).first()  # noqa: E712
    return u.email if u is not None else ""


def _flatten_header(value: str) -> str:
    """Collapse CR/LF/C0 controls in ANY value interpolated into an email header
    (§4.6). Reuses query_threads.flatten_header_text — the one sanitizer — so the query
    mails and the pre-existing review-assignment Subject share the exact defence. A
    deferred import (the cross-feature reach declared in the queries Module's
    cross_reach_allow); a failure degrades to the raw value rather than dropping mail."""
    try:
        from app.services import query_threads

        return query_threads.flatten_header_text(value or "")
    except Exception:
        return (value or "").replace("\r", " ").replace("\n", " ")


def _org() -> str:
    from app import branding

    return branding.org_name()


def _describe_job(s: Session, job: Job) -> tuple[str, str]:
    """Return (document_label, engine) for the message body, from the job's
    payload + stored result. Best-effort — missing rows degrade gracefully."""
    doc_label = "your document"
    engine_used = ""
    doc_id = (job.payload or {}).get("document_id")
    if doc_id:
        doc = s.get(Document, doc_id)
        if doc:
            doc_label = doc.filename or doc_label
    # provider_used is accumulated into Job.result stats by translate._apply_result
    used = (job.result or {}).get("provider_used") or {}
    if isinstance(used, dict) and used:
        engine_used = ", ".join(sorted(used))
    return doc_label, engine_used


def on_job_finished(job: Job) -> bool:
    """Email the submitter that their translation job finished. Returns True if a
    message was actually sent (SMTP configured + delivered), else False. Opens its
    OWN DB session — it runs on the worker thread, outside any request scope.

    Safe to call for any job: it self-filters to translate_document jobs with an
    email `created_by`, and no-ops (returns False) when disabled / unconfigured."""
    if not enabled():
        return False
    if job.kind != "translate_document":
        return False
    actor = (job.created_by or "").strip()
    if not _looks_like_email(actor):
        return False
    if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
        return False

    org = _org()
    with Session(engine) as s:
        # Re-read the job so payload/result are fresh (the caller may pass a
        # detached copy), and resolve the document + engine for the body.
        fresh = s.get(Job, job.id) or job
        locale = (fresh.payload or {}).get("locale") or "the requested language"
        doc_label, engine_used = _describe_job(s, fresh)

    if job.status == JobStatus.COMPLETED:
        subject = f"[{org}] Translation ready: {doc_label}"
        body = (
            f"Good news — your translation is ready.\n\n"
            f"Document: {doc_label}\n"
            f"Language: {locale}\n"
            + (f"Engine: {engine_used}\n" if engine_used else "")
            + f"\nYou can download the translated file from {org}.\n"
        )
    else:
        subject = f"[{org}] Translation failed: {doc_label}"
        body = (
            f"The job did not complete.\n\n"
            f"Document: {doc_label}\n"
            f"Language: {locale}\n"
            f"Error: {(job.error or 'unknown').strip()}\n\n"
            f"Open {org} to review the error and resubmit.\n"
        )

    try:
        return mailer.send(actor, subject, body)
    except Exception:
        log.exception("Completion email failed for job %s (%s)", job.id, actor)
        return False


# --- project-budget threshold alert (CV-2) -----------------------------------
def budget_threshold_enabled() -> bool:
    """Gate for the CV-2 project-budget threshold email. Defaults ON (like
    notify_on_job_finish): it is a graceful no-op without SMTP, so costing nothing
    until an operator both sets a budget AND configures mail."""
    return settings.get("notify_on_budget_threshold", "1") == "1"


def _admin_recipients(s: Session, org_id) -> list[str]:
    """The admin email addresses a project-budget alert goes to: every ACTIVE admin
    User with a real email. Org-scoped to the budget's org when set (an org-admin
    hears about their own org's projects), install-wide otherwise. Mirrors the
    admin-recipient query security/gdpr already use."""
    stmt = select(User).where(User.role == Role.ADMIN, User.active == True)  # noqa: E712
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    out: list[str] = []
    for u in s.exec(stmt).all():
        email = (u.email or "").strip()
        if _looks_like_email(email):
            out.append(email)
    return out


def on_project_budget_threshold(
    s: Session,
    project: Project | None,
    band: int,
    pct: float,
    enforced_usd: float,
    limit_usd: float,
    org_id=None,
) -> int:
    """Email the admins that a project crossed a monthly-spend threshold (CV-2).
    Returns the number of messages actually delivered (0 when disabled / no SMTP /
    no admins). Best-effort: a mail failure is logged and swallowed, never raised —
    the crossing is already audited + state-persisted by the caller regardless.

    `band` is the crossed threshold (80 or 100); `pct` the true current percentage;
    `enforced_usd`/`limit_usd` the money figures echoed in the body."""
    if not budget_threshold_enabled():
        return 0
    org = _org()
    name = getattr(project, "name", None) or "a project"
    recipients = _admin_recipients(s, org_id)
    if not recipients:
        return 0
    hard = band >= 100
    subject = f"[{org}] Project budget {'EXCEEDED' if hard else 'at %d%%' % band}: {name}"  # noqa: UP031
    body = (
        (
            f"Project '{name}' has reached its monthly spend budget.\n\n"
            if hard
            else f"Project '{name}' has passed {band}% of its monthly spend budget.\n\n"
        )
        + f"Spent this month: ${enforced_usd:,.2f}\n"
        + f"Monthly budget:   ${limit_usd:,.2f}\n"
        + f"Usage:            {pct:.0f}%\n\n"
        + (
            "New machine-translate submissions and new vendor dispatches for this "
            "project are now blocked until next month or until the budget is raised. "
            "Human review and editing are never blocked.\n"
            if hard
            else "This is an early-warning notice; nothing is blocked yet.\n"
        )
        + f"\nOpen {org} to review the project's spend.\n"
    )
    sent = 0
    for to in recipients:
        try:
            if mailer.send(to, subject, body):
                sent += 1
        except Exception:
            log.exception("Budget-threshold email failed for %s", to)
    return sent


# --- human-review assignment notification (Feature B) ------------------------
def review_assign_enabled() -> bool:
    return settings.get("notify_on_review_assigned", "1") == "1"


def _due_line(project: Project | None, now: datetime | None = None) -> str:
    """One line describing the project's due date for the review email body:
      - "Due date: none set"        when the project has no due date;
      - "Due date: YYYY-MM-DD (OVERDUE)"    when the date is in the past AND the
        project is still active;
      - "Due date: YYYY-MM-DD (Due in N day(s))"  otherwise (N >= 0);
      - "Due date: YYYY-MM-DD"       (no OVERDUE/Due-in suffix) for a project in
        a TERMINAL status (completed/closed/cancelled), even if the date is past
        -- the spec requires all three due-date surfaces (dashboard badge,
        project page, this email) to AGREE, and content.due_status returns None
        (never "overdue") for a finished project. Consulting due_status here
        (rather than re-deriving the terminal-status guard) means the two can
        never drift apart again.

    Naive-UTC on both sides (project.due_date is naive-UTC; `now` = utcnow) so
    there is never a tz mismatch. The day count uses content.days_until_due
    (ceil of the fractional-day delta) -- the SAME math due_status uses to
    decide due_soon -- so the badge and this line always agree on "N days"."""
    due = getattr(project, "due_date", None) if project is not None else None
    if due is None:
        return "Due date: none set"
    now = now or datetime.utcnow()
    stamp = due.strftime("%Y-%m-%d")
    status = content.due_status(project, now)
    if status is None:
        if due < now:
            # Past due, but due_status said None -> a terminal-status project
            # (or no due date, already handled above). Never say OVERDUE for
            # finished work -- matches the badge exactly.
            return f"Due date: {stamp}"
        days = content.days_until_due(due, now)
        return f"Due date: {stamp} (Due in {days} day{'' if days == 1 else 's'})"
    if status == "overdue":
        return f"Due date: {stamp} (OVERDUE)"
    # "due_soon" (or any future non-None status): still report the day count.
    days = content.days_until_due(due, now)
    return f"Due date: {stamp} (Due in {days} day{'' if days == 1 else 's'})"


def _notify_needs_review(run, step) -> None:
    """Part B: a workflow run paused for a human review step is a `run.needs_review` event on
    its project (channels in app/services/notify.py). Never raises."""
    try:
        from app.services import notify

        doc_id = getattr(run, "document_id", None)
        pid = None
        if doc_id is not None:
            with Session(engine) as s:
                doc = s.get(Document, doc_id)
                pid = getattr(doc, "project_id", None)
        if pid is not None:
            notify.notify(
                "run.needs_review",
                pid,
                {"dedup": f"review:{getattr(run, 'id', '')}:{getattr(step, 'id', '')}"},
            )
    except Exception:
        log.exception("needs-review channel notification failed")


def on_human_review_paused(run, step) -> bool:
    """Email the assignee of a human_review step that a run just PAUSED and is
    waiting on their review. Returns True if a message was actually sent (SMTP
    configured + delivered), else False. Opens its OWN DB session — it runs at the
    orchestrator pause chokepoint, which may be on a background worker thread.

    Fires EXACTLY ONCE per actual pause: the caller invokes it right AFTER the
    PAUSED StepRun is persisted at the two orchestrator pause sites (a fresh pause,
    and a re-pause at a SECOND human step, each create a new PAUSED StepRun ->
    correctly notify; observe_run replay creates NO StepRun -> never re-fires).

    Self-filters + no-ops (returns False, never raises) when:
      - notify_on_review_assigned is off;
      - the step has no assignee_email (v1 non-goal: NO team/role broadcast — an
        unassigned human step emails nobody, to avoid spamming a whole team);
      - the assignee is not a plausible email;
      - SMTP is unconfigured (mailer.send returns False).

    Body carries the language pair, project, step/role, a due-date line (OVERDUE /
    Due in N days / none set) and a DIRECT LINK to the run view ({app_base_url}/
    runs/{run_id}) when app_base_url is set, else a fallback line telling the
    reviewer to open the app + go to their review queue."""
    _notify_needs_review(run, step)
    if not review_assign_enabled():
        return False
    assignee = (getattr(step, "assignee_email", "") or "").strip()
    if not _looks_like_email(assignee):
        return False

    org = _org()
    with Session(engine) as s:
        # Re-read the step so a detached copy still resolves its assignee, and
        # resolve the document -> project + the locale pair for the body. All
        # best-effort: a missing row degrades the label, never raises.
        doc = (
            s.get(Document, getattr(run, "document_id", None))
            if getattr(run, "document_id", None)
            else None
        )
        filename = (doc.filename if doc else None) or "your document"
        project = s.get(Project, doc.project_id) if doc else None
        project_name = (project.name if project else None) or "your project"
        src = (project.source_locale if project else None) or "source"
        tgt = (getattr(run, "target_locale", "") or "").strip() or "target"
        role = (getattr(step, "name", "") or "").strip() or "Human review"
        due_line = _due_line(project)
        run_id = getattr(run, "id", None)

    # §4.6 header-injection fix: `filename` is user-supplied text spliced into a
    # Subject: header (the pre-existing exposure at the old :204). Flatten every
    # interpolated header value at the mailer boundary so a `\r\nBcc:` cannot survive.
    subject = _flatten_header(f"[{org}] Review assigned: {filename} ({src} -> {tgt})")

    base_url = settings.external_base_url()
    if base_url and run_id is not None:
        # The run view is where a reviewer actually works; it exists today.
        link_line = f"Open it here to start working: {base_url}/runs/{run_id}"
    else:
        # §10 text-truth: point at the My work page (LC-5) by NAME, not a live URL — the
        # old copy told reviewers to "go to your review queue" when none existed. H-3: a
        # live {base_url}/my-work URL would 404 until Train D ships the page in this same
        # release, so we never EMAIL that URL — the run link above is the live destination.
        link_line = f"Open {org} and go to My work to start working (run #{run_id})."

    body = (
        f"A translation is ready for your review.\n\n"
        f"Document: {filename}\n"
        f"Language pair: {src} -> {tgt}\n"
        f"Project: {project_name}\n"
        f"Step/role: {role}\n"
        f"{due_line}\n\n"
        f"{link_line}\n\n"
        f"Post-edit the text and submit to continue the workflow.\n"
    )

    try:
        return mailer.send(assignee, subject, body)
    except Exception:
        log.exception(
            "Review-assignment email failed for run %s (%s)", getattr(run, "id", None), assignee
        )
        return False


def on_vendor_return_resume_failed(run, leg) -> bool:
    """MEDIUM-4: a vendor RETURN stored its content and closed the leg (RETURNED), but the
    follow-on resume THREW -- so the run is stuck PAUSED with no automatic retry (gate 1
    refuses a RETURNED leg). Email the run's owner so a human knows to RE-DRIVE it (the CLI
    `vendor redrive --dispatch-id <n>` verb / vendor_return.redrive_returned_leg).

    Best-effort + guarded, mirroring on_job_finished: no-op (returns False, never raises)
    when notifications are off, the owner is not a plausible email, or SMTP is unconfigured."""
    if not enabled():
        return False
    owner = (getattr(run, "created_by", "") or "").strip()
    if not _looks_like_email(owner):
        return False
    org = _org()
    run_id = getattr(run, "id", None)
    leg_id = getattr(leg, "id", None)
    base_url = settings.external_base_url()
    link_line = (
        f"Open it here: {base_url}/runs/{run_id}"
        if base_url and run_id is not None
        else f"Open {org} and go to run #{run_id}."
    )
    subject = f"[{org}] Action needed: a vendor return did not resume run #{run_id}"
    body = (
        "A vendor return was accepted and its content stored, but the workflow did not "
        "resume automatically, so the run is paused and waiting on a person.\n\n"
        f"Run: #{run_id}\n"
        f"Dispatch leg: #{leg_id}\n\n"
        f"{link_line}\n\n"
        "Re-drive it from the CLI:\n"
        f"  python -m app.cli vendor redrive --dispatch-id {leg_id}\n"
    )
    try:
        return mailer.send(owner, subject, body)
    except Exception:
        log.exception("Vendor-return resume-failed email failed for run %s", run_id)
        return False


# --- query notifications (SPEC-wave3 §4.6) -----------------------------------
# The email channel is the only push channel there is, and its limits are stated out
# loud, not implied away (the §4.6 honest block, shipped verbatim on the card + page).
# The user-supplied SUBJECT never enters the `Subject:` header (a fixed template
# `[org] Query on project: #id`, flattened); the subject and post body go in the plain
# body. Every function follows the house shape (orchestrator.py:76-88): the CALLER has
# already committed, a deferred import, an own DB session, and a local try/except so a
# mail side effect can never crash the request.
_QUERY_LIMITS_BLOCK = (
    "Query notifications are email only. There is no in-app notification centre, no "
    "digest, no Slack or webhook target, and no per-user notification preferences. If "
    "SMTP is not configured, no notification is sent and nothing is logged. The only "
    "in-app signal that a query is waiting for you is the My work page and the query "
    "lists. An unassigned query notifies nobody, by design."
)


def query_notify_enabled() -> bool:
    """Query notifications: default ON, a graceful no-op without SMTP (mailer.send
    returns False), so it costs nothing by default. ANDed with the queries kill switch —
    NO query mail of any kind is sent when queries_enabled=0 (§5.2)."""
    from app import features

    if not features.is_enabled("queries"):
        return False
    return settings.get("notify_on_query", "1") == "1"


def _query_body(org: str, subject_line: str, message_text: str, tid, lead: str) -> str:
    """The plain-text body shared by the three query mails. The user's subject + the
    message text live HERE, never in a header. The §4.6 limits block ships verbatim."""
    base_url = settings.external_base_url()
    if base_url:
        # /queries/{id} exists in this tree; never emit a live /my-work URL (H-3).
        link_line = f"Open it here: {base_url}/queries/{tid}"
    else:
        link_line = f"Open {org} and go to the Queries list (query #{tid})."
    parts = [lead, ""]
    if subject_line:
        parts.append(f"Subject: {subject_line}")
    if (message_text or "").strip():
        parts.append("")
        parts.append(message_text.strip())
    parts += ["", link_line, "", _QUERY_LIMITS_BLOCK, ""]
    return "\n".join(parts) + "\n"


def _query_header(org: str, project_name: str, tid) -> str:
    """The FIXED Subject: header for a query mail — flattened, and carrying NO
    user-supplied query subject (§4.6 header-injection fix)."""
    return _flatten_header(f"[{org}] Query on {project_name}: #{tid}")


def on_query_opened(thread_id: int, actor_email: str = "") -> bool:
    """Email the assignee that a query was opened (or re-assigned) to them. No-op
    (returns False, never raises) when notifications/feature are off, the thread is
    unassigned, the assignee is not an ACTIVE user (M-4), the assignee IS the actor
    (L-5: a self-assign/self-open never mails the actor), or SMTP is unconfigured."""
    if not query_notify_enabled():
        return False
    actor = (actor_email or "").strip().lower()
    with Session(engine) as s:
        thread = s.get(QueryThread, thread_id)
        if thread is None:
            return False
        to = _active_email(s, thread.assignee_email)  # M-4: active-user at SEND time
        if not to or to.lower() == actor:  # L-5: subtract the actor
            return False
        project = s.get(Project, thread.project_id)
        proj_name = _flatten_header((getattr(project, "name", "") or "") or "a project")
        subject_line = _flatten_header(thread.subject or "")
        first = s.exec(
            select(QueryPost)
            .where(QueryPost.thread_id == thread_id)
            .order_by(QueryPost.created_at, QueryPost.id)
        ).first()
        first_body = first.body if first else ""
        tid = thread.id
    org = _org()
    try:
        return mailer.send(
            to,
            _query_header(org, proj_name, tid),
            _query_body(
                org, subject_line, first_body, tid, "A query was opened and assigned to you."
            ),
        )
    except Exception:
        log.exception("Query-opened email failed for thread %s (%s)", thread_id, to)
        return False


def on_query_post(thread_id: int, post_id: int, actor_email: str) -> int:
    """Email the thread author + assignee (MINUS the actor) that a reply was posted.
    Returns the number delivered (0 when disabled / nobody left after the subtraction /
    no SMTP). One mail per recipient; never one per post-count."""
    if not query_notify_enabled():
        return 0
    actor = (actor_email or "").strip().lower()
    with Session(engine) as s:
        thread = s.get(QueryThread, thread_id)
        post = s.get(QueryPost, post_id) if post_id is not None else None
        if thread is None or post is None:
            return 0
        recips = set()
        for cand in (thread.author, thread.assignee_email):
            c = _active_email(s, cand)  # M-4: active-user re-validation at SEND
            if c and c.lower() != actor:  # minus the actor
                recips.add(c)
        project = s.get(Project, thread.project_id)
        proj_name = _flatten_header((getattr(project, "name", "") or "") or "a project")
        subject_line = _flatten_header(thread.subject or "")
        post_body = post.body or ""
        tid = thread.id
    if not recips:
        return 0
    org = _org()
    header = _query_header(org, proj_name, tid)
    body = _query_body(org, subject_line, post_body, tid, "A new reply was posted.")
    sent = 0
    for to in sorted(recips):
        try:
            if mailer.send(to, header, body):
                sent += 1
        except Exception:
            log.exception("Query-post email failed for thread %s (%s)", thread_id, to)
    return sent


def on_query_closed(thread_id: int, actor_email: str) -> bool:
    """Email the thread author that their query was closed — UNLESS the author is the
    one who closed it. No-op (never raises) when disabled / author-is-closer / no SMTP."""
    if not query_notify_enabled():
        return False
    actor = (actor_email or "").strip().lower()
    with Session(engine) as s:
        thread = s.get(QueryThread, thread_id)
        if thread is None:
            return False
        author = _active_email(s, thread.author)  # M-4: active-user at SEND time
        if not author or author.lower() == actor:
            return False
        project = s.get(Project, thread.project_id)
        proj_name = _flatten_header((getattr(project, "name", "") or "") or "a project")
        subject_line = _flatten_header(thread.subject or "")
        tid = thread.id
    org = _org()
    try:
        return mailer.send(
            author,
            _query_header(org, proj_name, tid),
            _query_body(org, subject_line, "", tid, "Your query was closed."),
        )
    except Exception:
        log.exception("Query-closed email failed for thread %s (%s)", thread_id, author)
        return False
