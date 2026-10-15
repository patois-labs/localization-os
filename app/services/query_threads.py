"""Threaded queries / comments — the core service (SPEC-wave3-collaboration §3-§4).

The ONE truth for the query entity: every surface (web / /api / CLI / MCP, Train B) and
the vendor round trip (Train E) is a thin caller of the functions here. Nothing else
writes `querythread` / `querypost` (the cascade-delete and GDPR paths reach the TABLES
through app/models, not this module — §5.2's fourth seam).

TENANCY (§2.2, the wave's HIGH-severity surface). A thread's tenant anchor is its
`project_id`, resolved once at creation from whatever it was scoped to. It is the only
column `access.visible_project_ids` speaks, so every read/write funnels through the
access ring on that column and nothing else. An out-of-scope thread is a 404, never a
403 (non-enumerable).

RBAC (§4.5). Reading/opening/posting/answering needs only VIEW of the project (RV-1: a
customer-bound client sits at viewer rank and must be able to raise a query). Closing /
reopening needs the thread author, its assignee, or `can_review`+; assigning needs the
author or `can_review`+. Deletion is nobody's verb — close is the exit.

THE TRUSTED OPERATOR. The in-process CLI (`app/cli/__main__.py:35-39`) runs with no RBAC
and no session user: it passes `trusted_operator=True` to EVERY verb (read AND write) as
the console arm, and supplies the author through the `actor` string. `user is None` is NOT
by itself the console — under auth-ON `security.session_user` returns None for an
ANONYMOUS / EXPIRED / DEACTIVATED request, which must be REFUSED, not trusted (M3). So a
verb trusts a caller iff `trusted_operator=True`; a `user is None` caller without the flag
is refused (an out-of-scope 404 for reads/writes; `visible_project_ids` already returns []
for None under auth-ON, so list is safe by construction).

ATTRIBUTION IS UNFORGEABLE (H2). For an authenticated caller (`user` set) the acting
identity is ALWAYS `user.email` — the free `actor` string is ignored, so a viewer cannot
pass `actor="owner@x"` to close a thread or forge a `closed_by`/author. The `actor` string
is meaningful ONLY on the trusted-operator arm, where there is no session user to derive
from.

Documented in: docs/architecture/collaboration-and-review.md
"""

from __future__ import annotations

import logging
from collections import namedtuple
from datetime import datetime

from sqlalchemy import false, func
from sqlmodel import Session, select

from app.models import (
    Document,
    Project,
    QueryPost,
    QueryStatus,
    QueryThread,
    Segment,
    StringKey,
    User,
    VendorDispatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access

log = logging.getLogger("loc.query_threads")

# --- caps (§3.4): every limit is a REFUSAL naming the limit, never a silent truncation.
SUBJECT_MAX = 200
BODY_MAX = 4000
POSTS_PER_THREAD_MAX = 200
OPEN_THREADS_PER_SEGMENT_MAX = 20
LIST_PAGE_MAX = 50


# --- typed errors; `.status` is the HTTP code every surface maps to ---------- #
class QueryError(Exception):
    """Bad input / validation refusal — 400."""

    status = 400


class QueryNotFound(QueryError):
    """Thread does not exist, or is outside the caller's scope — 404, non-enumerable."""

    status = 404


class QueryForbidden(QueryError):
    """Caller may view the thread but not perform this lifecycle action — 403."""

    status = 403


class QueryConflict(QueryError):
    """Lifecycle / cap conflict — 409 (post to a closed thread, per-thread or
    per-segment cap breach)."""

    status = 409


ScopeTuple = namedtuple("ScopeTuple", "project_id document_id segment_id key_id target_locale")

# A denormalized list row (§4.1): the list surfaces read `post_count` and never N+1 to
# count posts. Mirrors the thread columns a list/worklist needs; carries no post bodies.
ThreadRow = namedtuple(
    "ThreadRow",
    "id project_id document_id segment_id key_id target_locale subject status "
    "author author_kind assignee_email vendor_id dispatch_id post_count "
    "created_at updated_at closed_at closed_by",
)


def _row(t: QueryThread) -> ThreadRow:
    return ThreadRow(
        id=t.id,
        project_id=t.project_id,
        document_id=t.document_id,
        segment_id=t.segment_id,
        key_id=t.key_id,
        target_locale=t.target_locale,
        subject=t.subject,
        status=t.status,
        author=t.author,
        author_kind=t.author_kind,
        assignee_email=t.assignee_email,
        vendor_id=t.vendor_id,
        dispatch_id=t.dispatch_id,
        post_count=t.post_count,
        created_at=t.created_at,
        updated_at=t.updated_at,
        closed_at=t.closed_at,
        closed_by=t.closed_by,
    )


def _utcnow() -> datetime:
    return datetime.utcnow()


def flatten_header_text(s: str) -> str:
    """Collapse CR / LF / C0 control chars (and DEL) to a single space — the ONE
    sanitizer applied to `subject` (§3.4). It defends the email `Subject:` header
    against injection (a `\\r\\nBcc:` cannot survive) and keeps a subject one line.
    Rendering safety (HTML autoescape, lxml for XLIFF, JSON encoding) is the renderer's
    job; a query body is human text and is stored verbatim."""
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        # C0 controls (0x00-0x1F, includes CR/LF/TAB), and DEL (0x7F) -> space.
        out.append(" " if o < 0x20 or o == 0x7F else ch)
    return "".join(out)


# --- scope resolution (§3.2): one function, one truth ------------------------ #
def resolve_scope(
    session: Session, *, segment_id=None, key_id=None, document_id=None, project_id=None
) -> ScopeTuple:
    """Resolve a scope anchor to the full `(project_id, document_id, segment_id, key_id,
    target_locale)` tuple, RAISING on an unresolvable or inconsistent combination.

    Exactly one anchor is expected; when more than one is supplied they must AGREE
    (a disagreement is a 400, never a silent precedence). A segment resolves upward to
    its document + project and carries the segment's locale; a segment that belongs to
    an internal KEY document is admissible and is stamped with its `key_id` so the
    thread renders on the key grid, not on a document page the user cannot open (§3.2.3).
    A key resolves through `StringKey.project_id`; its `document_id` is left NULL —
    advisory and resolved at read time (§3.1/MED-8)."""
    supplied = [v for v in (segment_id, key_id, document_id, project_id) if v is not None]
    if not supplied:
        raise QueryError("a query must be scoped to a segment, key, document or project.")

    r_proj = r_doc = r_seg = r_key = None
    r_loc = ""

    if segment_id is not None:
        seg = session.get(Segment, segment_id)
        if seg is None:
            raise QueryNotFound("no such segment.")
        r_seg = seg.id
        r_loc = seg.target_locale or ""
        if seg.key_id is not None:
            # A key value's segment: stamp key_id (renders on the key grid), resolve the
            # project through the key. document_id stays NULL — advisory for key threads.
            r_key = seg.key_id
            sk = session.get(StringKey, seg.key_id)
            if sk is None:
                raise QueryNotFound("no such key.")
            r_proj = sk.project_id
        else:
            r_doc = seg.document_id
            doc = session.get(Document, seg.document_id)
            if doc is None:
                raise QueryNotFound("no such document.")
            r_proj = doc.project_id
    elif key_id is not None:
        sk = session.get(StringKey, key_id)
        if sk is None:
            raise QueryNotFound("no such key.")
        r_key = sk.id
        r_proj = sk.project_id
    elif document_id is not None:
        doc = session.get(Document, document_id)
        if doc is None:
            raise QueryNotFound("no such document.")
        r_doc = doc.id
        r_proj = doc.project_id
    else:
        proj = session.get(Project, project_id)
        if proj is None:
            raise QueryNotFound("no such project.")
        r_proj = proj.id

    # Consistency: any coarser anchor also supplied must agree with the resolution.
    if project_id is not None and project_id != r_proj:
        raise QueryError(
            "scope anchors disagree: the segment/key/document does not "
            f"belong to project {project_id}."
        )
    if document_id is not None and r_doc is not None and document_id != r_doc:
        raise QueryError(f"scope anchors disagree: the segment is not in document {document_id}.")
    if key_id is not None and r_key is not None and key_id != r_key:
        raise QueryError(f"scope anchors disagree: the segment is not a value of key {key_id}.")

    return ScopeTuple(
        project_id=r_proj, document_id=r_doc, segment_id=r_seg, key_id=r_key, target_locale=r_loc
    )


def anchor_projects(session: Session, **anchors) -> list[int | None]:
    """The project id each SINGLE supplied anchor resolves to (#586), or None for an anchor
    that does not exist. Lets a caller check the visibility ring on whatever the anchors
    point at BEFORE any not-found / disagreement answer is allowed to differ between a
    missing id and a hidden one: a ringed caller must treat a None (missing) exactly like a
    project it may not see, or a visible segment next to a missing project id would answer
    "anchors disagree" while the same segment next to a hidden project answers the ring."""
    out: list[int | None] = []
    for name in ("segment_id", "key_id", "document_id", "project_id"):
        val = anchors.get(name)
        if val is None:
            continue
        try:
            out.append(resolve_scope(session, **{name: val}).project_id)
        except QueryError:
            out.append(None)
    return out


def resolve_scope_in_ring(
    session: Session, *, user, trusted: bool = False, org_id=None, **anchors
) -> ScopeTuple:
    """`resolve_scope` for a caller with a visibility ring (#586): an anchor that does not
    exist answers EXACTLY like one the caller may not see ("no such query thread." 404, the
    ring message), and a disagreement between anchors is only reported once the caller may
    see every project the anchors point at. Otherwise POST /api/queries and `query open`
    revealed whether a hidden segment, document, key or project id exists.

    A trusted operator with no org scope (gate off, or the key-verified --operator door)
    sees everything, so it keeps the specific messages."""
    ringed = (
        not trusted
        or access.org_scope_blocked(org_id)
        or access.org_scope_value(org_id) is not None
    )
    try:
        return resolve_scope(session, **anchors)
    except QueryNotFound:
        if ringed:
            raise QueryNotFound("no such query thread.") from None
        raise
    except QueryError:
        if ringed:
            for pid in anchor_projects(session, **anchors):
                if pid is None:  # a missing anchor answers like a hidden one
                    raise QueryNotFound("no such query thread.") from None
                _require_view(session, user, pid, trusted=trusted, org_id=org_id)
        raise


# --- identity + access-ring helpers ------------------------------------------ #
def _identity(user, actor: str, trusted: bool) -> str:
    """The unforgeable acting identity (H2). Authenticated -> ALWAYS `user.email`, never
    the caller-supplied `actor` (which a viewer could set to another person's address).
    Trusted operator (console) -> the free `actor` string, else "system"."""
    if trusted:
        return actor or "system"
    return getattr(user, "email", "") or ""


def _require_view(session: Session, user, project_id: int, *, trusted: bool = False, org_id=None):
    """The tenant ring. A caller who cannot view the project gets a 404 (never a 403 —
    the thread is non-enumerable). ONLY `trusted=True` (the console) skips the ring; a
    `user is None` caller without the flag is an anonymous/expired session and is REFUSED
    (M3).

    #515: a trusted caller (the in-process CLI) normally skips the ring, but under multi-org
    it passes the org it was scoped to with `--org`: a project of another org then reads as
    "no such query thread" exactly like an authenticated cross-org caller would see. None
    (the key-verified `--operator` door, or gate off) keeps the unscoped behavior."""
    if trusted:
        # #73 converged law: a bare None under an ACTIVE gate is blocked (the CLI passes
        # access.UNSCOPED for the key-verified --operator door); gate off: no-op.
        if access.org_scope_blocked(org_id):
            raise QueryNotFound("no such query thread.")
        org_id = access.org_scope_value(org_id)
        if org_id is not None:
            proj = session.get(Project, project_id)
            if proj is None or proj.org_id != org_id:
                raise QueryNotFound("no such query thread.")
        return
    if user is None:
        raise QueryNotFound("no such query thread.")
    proj = session.get(Project, project_id)
    if proj is None or not access.can_view_project(session, user, proj):
        raise QueryNotFound("no such query thread.")


def _visible_thread(
    session: Session, user, thread_id: int, *, trusted: bool = False, org_id=None
) -> QueryThread:
    t = session.get(QueryThread, thread_id)
    if t is None:
        raise QueryNotFound("no such query thread.")
    _require_view(session, user, t.project_id, trusted=trusted, org_id=org_id)
    return t


def _is_reviewer(user) -> bool:
    from app.services import security

    return security.can_review(user)


def _is_manager(user) -> bool:
    from app.services import security

    return security.can_manage(user)


def _validate_assignee(
    session: Session,
    user,
    assignee_email: str,
    *,
    project_id: int | None = None,
    trusted: bool = False,
) -> str:
    """`assignee_email` must resolve to an existing, active user WITHIN THE CALLER'S
    VISIBILITY RING, or be empty (clears the assignment). This closes both the mail-relay
    vector (§4.5.d — the field can never mail an arbitrary address) AND the cross-tenant
    assignment + user-existence ORACLE (M1): a caller may not assign to, or probe the
    existence of, a user outside its org/customer. NOT-FOUND and OUT-OF-SCOPE raise the
    SAME message, so the refusal never reveals whether the address exists.

    `project_id` is the THREAD's tenant anchor (§2.2). When the thread's project is
    customer-bound the assignee is ALSO confined to that customer (F1) — this is what stops
    an internal-staff caller, whose caller-customer ring is NULL and thus skipped, from
    assigning across customers; see the inline note below."""
    email = (assignee_email or "").strip()
    if not email:
        return ""
    # One refusal for BOTH "no such user" and "user you cannot see" — no existence oracle.
    generic = QueryError(f"assignee {email!r} is not an assignable user in your scope.")
    u = session.exec(
        select(User).where(func.lower(User.email) == email.lower(), User.active == True)  # noqa: E712
    ).first()
    if u is None:
        raise generic
    # L-7 (Train B gate) + #67 (round-3): the trusted-operator arm (the in-process CLI,
    # __main__.py:35-39) DELIBERATELY skips the CALLER org/customer rings below — it is the
    # trusted ROOT operator (same trust as editing the DB directly), the documented dept-budget
    # / console precedent, so a cross-org assign+mail from the console is by design, not a hole.
    # It is still bounded: the target MUST be an existing ACTIVE user (the mail-relay guard
    # holds), and the in-process CLI already gates multi-org sweeps behind --org/--operator
    # (require_org_scope). An AUTHENTICATED caller (user set, trusted False) is always ring-
    # confined below.
    #
    # But the trusted bypass is CALLER-RBAC ONLY. The two ASSIGNEE data-invariants that follow
    # (the F1 thread-customer ring + the #51 project-visibility gate) are NOT about the caller —
    # they are about whether the CHOSEN ASSIGNEE can ever OPEN the thread. Assigning a customer-2
    # user to a customer-1 thread persists an unopenable obligation + a live mail target no
    # matter WHO issued it (#67: CLI `query assign` skipped these because it returned early
    # here). So the caller rings are gated behind `not (trusted or user is None)`, while the
    # assignee-invariant arms run UNCONDITIONALLY (a no-op with auth off — see below).
    if not (trusted or user is None):
        # Confine to the caller's rings: same org (multi-org) and, for a customer-bound
        # caller, the same customer (internal-staff assignees — customer_id NULL — are still
        # visible to a bound caller's PM, matching can_see_all's customer ring).
        if access.multi_org_active():
            cu_org = getattr(user, "org_id", None)
            if cu_org is not None and getattr(u, "org_id", None) != cu_org:
                raise generic
        cid = access.current_customer_id(user)
        if cid is not None and getattr(u, "customer_id", None) not in (None, cid):
            raise generic
    # F1 (2026-08-20 verification pass): ALSO confine to the THREAD's OWN customer — the
    # thread's tenant anchor is its project (§2.2). The caller-customer ring above is SKIPPED
    # for an INTERNAL-STAFF caller (customer_id NULL -> cid None), so without this an
    # authenticated reviewer+ could assign a customer-A thread to a customer-B CLIENT account:
    # notifications.on_query_opened would mail customer-A's project name + query text to B, and
    # A's own client would then see B's address on the thread. A customer-bound project may
    # assign ONLY to its own customer or to a global (internal-staff, NULL) user; the shared
    # workflow-family ring predicate decides, so this cannot drift from the other isolation
    # seams. Gated on auth: with auth OFF the SYSTEM operator is the trusted root, so this ring
    # is a no-op there. RUNS FOR THE TRUSTED OPERATOR TOO (#67): it is an assignee invariant, not
    # caller RBAC, so the CLI's trusted status does not exempt it — a customer-2 assignee on a
    # customer-1 thread is refused on EVERY surface (/api, web, and now CLI).
    if project_id is not None:
        from app.services import security

        if security.auth_required():
            proj = session.get(Project, project_id)
            proj_cid = getattr(proj, "customer_id", None) if proj is not None else None
            if access.customer_ring_blocks(proj_cid, getattr(u, "customer_id", None)):
                raise generic
    # Issue #51 (round-2): the ASSIGNEE must also be able to SEE the thread's project.
    # A query assignment is an OBLIGATION plus a /my-work entry (the queries card's promise);
    # assigning to a user with no visibility of the project is a silent black hole — the read
    # ring then hides the very thread from its own assignee (/my-work empty, the mailed
    # /queries/{id} link 404s, the "assigned to me" filter blank). Reuse the SAME shared read
    # predicate the ring itself uses (access.can_view_project — the same seam _require_view
    # calls) so the assignee gate can NEVER drift from what actually makes a thread visible.
    # Same generic no-oracle refusal (never reveals whether the user exists or why). RUNS FOR
    # THE TRUSTED OPERATOR TOO (#67): it is an ASSIGNEE data invariant, not caller RBAC, so the
    # CLI's documented trusted-root status does NOT exempt it (the caller-RBAC rings above are the
    # only part the trusted arm skips). With auth OFF can_view_project is universally True
    # (can_see_all short-circuits), so this stays a no-op for the single-operator install. All
    # THREE assignee write sites funnel through _validate_assignee, so all inherit this gate:
    # open_thread(assignee=...) and assign() call it directly with the caller as `user` (or None
    # + trusted for the CLI); the vendor writer (_vendor_thread_assignee, §7.3) calls it in a
    # SYSTEM context and, on refusal, leaves the vendor thread UNASSIGNED rather than raising.
    if project_id is not None:
        proj = session.get(Project, project_id)
        if proj is None or not access.can_view_project(session, u, proj):
            raise generic
    return u.email


def _check_subject(subject: str) -> str:
    if len(subject) > SUBJECT_MAX:
        raise QueryError(
            "subject is limited to %d characters (got %d)." % (SUBJECT_MAX, len(subject))  # noqa: UP031
        )
    return flatten_header_text(subject)


def _check_body(body: str) -> str:
    b = body or ""
    if len(b) > BODY_MAX:
        raise QueryError("a query post is limited to %d characters (got %d)." % (BODY_MAX, len(b)))  # noqa: UP031
    if not b.strip():
        raise QueryError("a query post cannot be empty.")
    return b


def _post_count_rows(session: Session, thread_id: int) -> int:
    return session.exec(
        select(func.count()).select_from(QueryPost).where(QueryPost.thread_id == thread_id)
    ).one()


# --- write verbs ------------------------------------------------------------- #
def open_thread(
    session: Session,
    *,
    user,
    scope: ScopeTuple,
    subject: str,
    body: str,
    assignee_email: str = "",
    actor: str = "",
    trusted_operator: bool = False,
    org_id=None,
) -> QueryThread:
    """Open a new thread + its first post. The resolved `scope.project_id` is run through
    the caller's access ring BEFORE any write (§3.2.4). Caps of §3.4 are refusals. The
    author is the UNFORGEABLE identity (H2): `user.email` when authenticated, the `actor`
    string only on the trusted-operator (console) arm."""
    _require_view(session, user, scope.project_id, trusted=trusted_operator, org_id=org_id)
    me = _identity(user, actor, trusted_operator)
    subj = _check_subject(subject)
    text = _check_body(body)
    assignee = _validate_assignee(
        session, user, assignee_email, project_id=scope.project_id, trusted=trusted_operator
    )

    # Open-threads-per-segment cap (§3.4) — counts rows, not post_count.
    if scope.segment_id is not None:
        open_n = session.exec(
            select(func.count())
            .select_from(QueryThread)
            .where(
                QueryThread.segment_id == scope.segment_id, QueryThread.status != QueryStatus.CLOSED
            )
        ).one()
        if open_n >= OPEN_THREADS_PER_SEGMENT_MAX:
            raise QueryConflict(
                "segment %s has reached its %d open-query limit; close a query before "  # noqa: UP031
                "opening another." % (scope.segment_id, OPEN_THREADS_PER_SEGMENT_MAX)
            )

    now = _utcnow()
    thread = QueryThread(
        project_id=scope.project_id,
        document_id=scope.document_id,
        segment_id=scope.segment_id,
        key_id=scope.key_id,
        target_locale=scope.target_locale,
        subject=subj,
        status=QueryStatus.OPEN,
        author=me,
        author_kind="human",
        assignee_email=assignee,
        post_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    session.flush()  # assign thread.id
    _append_post(session, thread, body=text, author=me, author_kind="human")
    session.commit()
    session.refresh(thread)
    return thread


def _append_post(
    session: Session,
    thread: QueryThread,
    *,
    body: str,
    author: str,
    author_kind: str = "human",
    author_label: str = "",
    vendor_id=None,
    dispatch_id=None,
    external_id=None,
) -> QueryPost:
    """Insert ONE post and bump the thread's denormalized post_count + updated_at, in the
    SAME transaction (L4 — post_count has exactly one owner, this path). Not a public
    verb: callers use add_post / record_vendor_post, which apply the lifecycle rules."""
    post = QueryPost(
        thread_id=thread.id,
        body=body,
        author=author,
        author_kind=author_kind,
        author_label=author_label,
        vendor_id=vendor_id,
        dispatch_id=dispatch_id,
        external_id=external_id,
        created_at=_utcnow(),
    )
    session.add(post)
    thread.post_count = (thread.post_count or 0) + 1
    thread.updated_at = _utcnow()
    session.add(thread)
    return post


def add_post(
    session: Session,
    *,
    user,
    thread_id: int,
    body: str,
    answer: bool = False,
    actor: str = "",
    trusted_operator: bool = False,
    org_id=None,
) -> QueryPost:
    """Reply to a thread. `answer=True` marks it ANSWERED. A plain reply NEVER flips
    open->answered implicitly (§3.3); a plain reply by the thread AUTHOR on an ANSWERED
    thread reopens it (the author is re-asking). A post on a CLOSED thread is a 409. The
    post author is the unforgeable identity (H2)."""
    thread = _visible_thread(session, user, thread_id, trusted=trusted_operator, org_id=org_id)
    me = _identity(user, actor, trusted_operator)
    text = _check_body(body)
    if thread.status == QueryStatus.CLOSED:
        raise QueryConflict("cannot post to a closed query; reopen it first.")
    if _post_count_rows(session, thread.id) >= POSTS_PER_THREAD_MAX:
        raise QueryConflict(
            "this thread has reached its %d-post limit; open a new query." % POSTS_PER_THREAD_MAX  # noqa: UP031
        )

    post = _append_post(session, thread, body=text, author=me, author_kind="human")
    if answer:
        thread.status = QueryStatus.ANSWERED
    elif thread.status == QueryStatus.ANSWERED and me and me == thread.author:
        # The author re-asking on an answered thread reopens it (§3.3).
        thread.status = QueryStatus.OPEN
    session.add(thread)
    session.commit()
    session.refresh(post)
    return post


def set_status(
    session: Session,
    *,
    user,
    thread_id: int,
    status: str,
    actor: str = "",
    trusted_operator: bool = False,
    org_id=None,
) -> QueryThread:
    """Explicit lifecycle transition. `closed` needs author/assignee/reviewer+; a reopen
    (`open` from closed/answered) needs author or reviewer+; `answered` needs only view.
    No transition happens as a side effect of anything but these (§3.3). Authorization is
    on the UNFORGEABLE identity (H2): a viewer cannot pass `actor="owner@x"` to close."""
    if status not in QueryStatus.ALL:
        raise QueryError("status must be one of open / answered / closed.")
    thread = _visible_thread(session, user, thread_id, trusted=trusted_operator, org_id=org_id)
    me = _identity(user, actor, trusted_operator)
    cur = thread.status

    if status == QueryStatus.CLOSED:
        if cur == QueryStatus.CLOSED:
            return thread
        if not (trusted_operator or _is_close_authorized(user, me, thread)):
            raise QueryForbidden("only the query author, its assignee or a reviewer may close it.")
        thread.status = QueryStatus.CLOSED
        thread.closed_at = _utcnow()
        thread.closed_by = me
    elif status == QueryStatus.ANSWERED:
        if cur == QueryStatus.CLOSED:
            raise QueryConflict("reopen the query before marking it answered.")
        thread.status = QueryStatus.ANSWERED
    else:  # OPEN
        if cur == QueryStatus.OPEN:
            return thread
        if not (trusted_operator or _is_reopen_authorized(user, me, thread)):
            raise QueryForbidden("only the query author or a reviewer may reopen it.")
        thread.status = QueryStatus.OPEN
        thread.closed_at = None
        thread.closed_by = ""
    thread.updated_at = _utcnow()
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def assign(
    session: Session,
    *,
    user,
    thread_id: int,
    assignee_email: str,
    actor: str = "",
    trusted_operator: bool = False,
    org_id=None,
) -> QueryThread:
    """Set or clear a thread's assignee (an empty string clears). Assigning creates an
    obligation for someone else, so it needs the author or reviewer+ (§4.5); the target
    must be an existing active user IN THE CALLER'S SCOPE who can ALSO SEE the thread's
    project (§4.5.d / M1 / issue #51 — otherwise the read ring hides the thread from its
    own assignee). Authorization is on the unforgeable identity (H2)."""
    thread = _visible_thread(session, user, thread_id, trusted=trusted_operator, org_id=org_id)
    me = _identity(user, actor, trusted_operator)
    if not (trusted_operator or _is_reopen_authorized(user, me, thread)):
        raise QueryForbidden("only the query author or a reviewer may assign it.")
    thread.assignee_email = _validate_assignee(
        session, user, assignee_email, project_id=thread.project_id, trusted=trusted_operator
    )
    thread.updated_at = _utcnow()
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def _is_close_authorized(user, me: str, thread: QueryThread) -> bool:
    """Authorize a close on the UNFORGEABLE identity `me` (H2) — never the raw actor
    string. `user` is consulted ONLY for the role rung (can_review+)."""
    if me and (me == thread.author or me == thread.assignee_email):
        return True
    return _is_reviewer(user)


def _is_reopen_authorized(user, me: str, thread: QueryThread) -> bool:
    if me and me == thread.author:
        return True
    return _is_reviewer(user)


# --- read verbs -------------------------------------------------------------- #
def get_thread(
    session: Session, *, user, thread_id: int, trusted_operator: bool = False, org_id=None
) -> tuple[QueryThread, list[QueryPost]]:
    """One thread + its posts (oldest first). Out-of-scope -> 404."""
    thread = _visible_thread(session, user, thread_id, trusted=trusted_operator, org_id=org_id)
    posts = session.exec(
        select(QueryPost)
        .where(QueryPost.thread_id == thread.id)
        .order_by(QueryPost.created_at, QueryPost.id)
    ).all()
    return thread, list(posts)


def list_threads(
    session: Session,
    user,
    *,
    project_id=None,
    document_id=None,
    segment_id=None,
    key_id=None,
    status=None,
    assignee=None,
    mine=False,
    author=None,
    limit=LIST_PAGE_MAX,
    offset=0,
    trusted_operator=False,
    org_id=None,
) -> tuple[list[ThreadRow], int]:
    """Scoped, filtered, paginated thread list. Scope is applied BEFORE pagination and
    the count, so a page or a `total` can never leak or count a foreign row
    (job_history.unified_history shape). Returns (rows, total)."""
    # Resolve the allowed project id set (None => unrestricted).
    if trusted_operator:  # noqa: SIM108
        allowed = None
    else:
        allowed = access.visible_project_ids(session, user)
    # Web/API callers rely on the project ring above (they pass no org_id); only the CLI passes
    # an org scope (access.UNSCOPED for --operator, collapsed here to "no extra narrow").
    org_id = access.org_scope_value(org_id)
    if org_id is not None:
        org_ids = set(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
        allowed = org_ids if allowed is None else (set(allowed) & org_ids)

    stmt = select(QueryThread)
    if allowed is not None:
        allowed = list(allowed)
        if not allowed:
            return [], 0
        stmt = stmt.where(QueryThread.project_id.in_(allowed))
    if project_id is not None:
        stmt = stmt.where(QueryThread.project_id == project_id)
    if document_id is not None:
        stmt = stmt.where(QueryThread.document_id == document_id)
    if segment_id is not None:
        stmt = stmt.where(QueryThread.segment_id == segment_id)
    if key_id is not None:
        stmt = stmt.where(QueryThread.key_id == key_id)
    if status is not None:
        stmt = stmt.where(QueryThread.status == status)
    if assignee is not None:
        stmt = stmt.where(func.lower(QueryThread.assignee_email) == assignee.lower())
    if author is not None:
        stmt = stmt.where(func.lower(QueryThread.author) == author.lower())
    if mine:
        # A session user (web / api cookie) filters --mine on their own email. The CLI has no
        # session user and carries the acting identity in `author`, so there --mine means "the
        # actor's own threads" and the `author` clause above already restricts it. With neither
        # a user email nor an actor, match NOTHING rather than the vendor-authored threads
        # (author == ""), which an empty email filter would otherwise return (#456).
        me = (getattr(user, "email", "") or "").lower()
        if me:
            stmt = stmt.where(func.lower(QueryThread.author) == me)
        elif not (author or "").strip():
            stmt = stmt.where(false())

    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    # L1: one page cap — the spec's 50 (§3.4), not a second magic number.
    page = session.exec(
        stmt.order_by(QueryThread.updated_at.desc(), QueryThread.id.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, LIST_PAGE_MAX)))
    ).all()
    return [_row(t) for t in page], int(total)


# --- the vendor writer (§7.3) — the ONLY vendor path into these tables ------- #
def _vendor_thread_assignee(session: Session, leg: VendorDispatch, project_id) -> str:
    """The address a vendor-originated thread is assigned to so it lands in a human's
    worklist (§7.3): the paused step's `assignee_email` if any, else the run's `created_by`.

    VALIDATED (issue #51 + the mail-relay guard §4.5.d): the candidate is the THIRD assignee
    write site and the most exposed one — `WorkflowStep.assignee_email` is UNVALIDATED free
    text (workflows.py only strips it) and vendor_return._notify_query_posts MAILS whatever
    lands here. So the candidate is run through the SAME `_validate_assignee` semantics
    (existence/active/customer-ring/project-visibility) in a SYSTEM context: the synthetic
    SYSTEM operator is the caller, which SKIPS the caller org/customer rings (it has none) but
    STILL enforces that the assignee is a real ACTIVE user, inside the THREAD's customer ring
    (F1), and able to SEE the thread's project (#51). On ANY failure the thread is left
    UNASSIGNED (a vendor return must NEVER bounce because a workflow step named a bad address)
    and no assignee mail is sent (the buyer thread-author notification, if any, still fires);
    warn once. Returns the CANONICAL resolved email, or ""."""
    from app.services import security

    candidate = ""
    if leg.step_id is not None:
        step = session.get(WorkflowStep, leg.step_id)
        if step and (step.assignee_email or "").strip():
            candidate = step.assignee_email
    if not candidate:
        run = session.get(WorkflowRun, leg.workflow_run_id) if leg.workflow_run_id else None
        candidate = (run.created_by if run else "") or ""
    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    try:
        # SYSTEM caller (not None, unbound, trusted=False): the caller org/customer rings
        # short-circuit (org_id/customer None), but existence/active + the thread-customer
        # ring + the assignee's project visibility all still run against the resolved user.
        return _validate_assignee(
            session, security.SYSTEM, candidate, project_id=project_id, trusted=False
        )
    except QueryError:
        log.warning(
            "vendor-thread assignee %r is not an assignable, project-visible active "
            "user; leaving the thread UNASSIGNED (dispatch=%s step=%s)",
            candidate,
            leg.id,
            leg.step_id,
        )
        return ""


def record_vendor_post(
    session: Session,
    *,
    leg: VendorDispatch,
    segment_id,
    body,
    author_label,
    external_id,
    reply_to=None,
) -> QueryPost:
    """Admit ONE vendor comment as a QueryPost — the only vendor writer (§7.3).

    LOOKS UP BEFORE IT CREATES (MED-4). With `reply_to` set, the post appends to that
    buyer thread and flips it ANSWERED (a vendor reply IS the answer — the one stated
    exception to §3.3's no-implicit-transition rule). Without `reply_to`, a NEW thread is
    opened scoped to the segment, assigned into a human's worklist; the thread is deduped
    on `(dispatch_id, external_id)` so a re-admitted comment reuses its thread instead of
    minting a second one, and the post is deduped on `(thread_id, external_id)`.

    Raises QueryConflict on a per-thread post-cap breach so the caller (Train E) can route
    it to `comments_rejected` rather than failing the whole return (§7.3/MED-5). Runs no
    commit of its own — the caller owns the transaction (the N4 begin_nested + retry)."""
    vendor_id = leg.vendor_id
    dispatch_id = leg.id
    label = flatten_header_text(author_label or "")

    if reply_to is not None:
        thread = session.get(QueryThread, reply_to)
        # M2 (defence in depth — the single vendor writer). The reply target MUST be a
        # thread on THIS leg's document + locale. A cross-tenant reply_to is refused
        # INDISTINGUISHABLY from a nonexistent id (no existence oracle across tenants),
        # so vendor B can never land a comment on customer A's thread or flip its
        # lifecycle. check_contract (Train E) also validates, but the writer fails closed.
        #
        # NEW-1/MED-8: resolve the thread's TRUE document through its segment — NEVER the
        # advisory `document_id` column, which resolve_scope leaves NULL on a key-scoped
        # thread (§3.2.3). Trusting the column over-refused every key-project reply. A
        # document/project-scoped thread with no segment falls back to its stored
        # document_id. NEW-3: the locale compare is case-normalized (the L2/L5 lesson).
        thread_doc = None
        if thread is not None:
            if thread.segment_id is not None:
                seg = session.get(Segment, thread.segment_id)
                thread_doc = seg.document_id if seg is not None else None
            else:
                thread_doc = thread.document_id
        if (
            thread is None
            or thread_doc != leg.document_id
            or (thread.target_locale or "").lower() != (leg.target_locale or "").lower()
        ):
            raise QueryNotFound("no such query thread for reply_to.")
        # Post-level idempotency: an already-admitted (thread, external_id) is a no-op.
        if external_id is not None:
            existing = session.exec(
                select(QueryPost).where(
                    QueryPost.thread_id == thread.id, QueryPost.external_id == external_id
                )
            ).first()
            if existing is not None:
                return existing
        if _post_count_rows(session, thread.id) >= POSTS_PER_THREAD_MAX:
            raise QueryConflict(
                "this thread has reached its %d-post limit; open a new query."  # noqa: UP031
                % POSTS_PER_THREAD_MAX
            )
        post = _append_post(
            session,
            thread,
            body=body,
            author="",
            author_kind="vendor",
            author_label=label,
            vendor_id=vendor_id,
            dispatch_id=dispatch_id,
            external_id=external_id,
        )
        # A vendor reply IS the answer (the stated exception to §3.3).
        if thread.status != QueryStatus.CLOSED:
            thread.status = QueryStatus.ANSWERED
        session.add(thread)
        return post

    # No reply_to -> open (or reuse) a vendor-originated thread. Dedup on the
    # thread-level (dispatch_id, external_id) key (MED-4).
    thread = None
    if external_id is not None:
        thread = session.exec(
            select(QueryThread).where(
                QueryThread.dispatch_id == dispatch_id, QueryThread.external_id == external_id
            )
        ).first()
    if thread is not None:
        # Reuse: the post is deduped by the post-level constraint.
        if external_id is not None:
            existing = session.exec(
                select(QueryPost).where(
                    QueryPost.thread_id == thread.id, QueryPost.external_id == external_id
                )
            ).first()
            if existing is not None:
                return existing
        return _append_post(
            session,
            thread,
            body=body,
            author="",
            author_kind="vendor",
            author_label=label,
            vendor_id=vendor_id,
            dispatch_id=dispatch_id,
            external_id=external_id,
        )

    scope = resolve_scope(session, segment_id=segment_id)
    # W3-E gate H3: the thread-CREATING branch must honour the per-segment open-thread cap
    # (§3.4) exactly as open_thread does -- otherwise a vendor return can mint unbounded new
    # threads on one segment, each stamping an assignee_email. Over the cap raises
    # QueryConflict so Train E routes it to comments_rejected (the return still admits, MED-5).
    if scope.segment_id is not None:
        open_n = session.exec(
            select(func.count())
            .select_from(QueryThread)
            .where(
                QueryThread.segment_id == scope.segment_id, QueryThread.status != QueryStatus.CLOSED
            )
        ).one()
        if open_n >= OPEN_THREADS_PER_SEGMENT_MAX:
            raise QueryConflict(
                "segment %s has reached its %d open-query limit; a vendor comment cannot "  # noqa: UP031
                "open another." % (scope.segment_id, OPEN_THREADS_PER_SEGMENT_MAX)
            )
    now = _utcnow()
    thread = QueryThread(
        project_id=scope.project_id,
        document_id=scope.document_id,
        segment_id=scope.segment_id,
        key_id=scope.key_id,
        target_locale=scope.target_locale,
        subject=flatten_header_text((body or "")[:SUBJECT_MAX]),
        status=QueryStatus.OPEN,
        author="",
        author_kind="vendor",
        assignee_email=_vendor_thread_assignee(session, leg, scope.project_id),
        vendor_id=vendor_id,
        dispatch_id=dispatch_id,
        external_id=external_id,
        post_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    session.flush()
    return _append_post(
        session,
        thread,
        body=body,
        author="",
        author_kind="vendor",
        author_label=label,
        vendor_id=vendor_id,
        dispatch_id=dispatch_id,
        external_id=external_id,
    )
