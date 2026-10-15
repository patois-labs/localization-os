"""Wave 3 Train A — the query entity schema + core services (offline).

SPEC-wave3-collaboration §11 Train A. Proves, on a throwaway data dir:

  1. scope resolution — each anchor resolves the full tuple; two disagreeing anchors
     refuse; a key-value segment resolves with key_id set + document_id NULL (advisory).
  2. the tenant ring (HIGH) — a thread in project P is invisible to a foreign-org,
     foreign-customer and non-member caller: 404 on read AND every mutation; a scoped
     list never returns or COUNTS a foreign row (scope-before-pagination).
  3. lifecycle — every §3.3 transition and that no OTHER is reachable (a plain reply
     never flips open->answered; a closed-thread post is 409; the author's post on
     answered reopens; a non-author non-assignee viewer cannot close).
  4. caps — each service-enforced refusal of §3.4 fires with the exact message + code,
     and NOTHING is truncated (the two vendor-WIRE caps live in verify_vendor_queries).
  5. flatten_header_text — CR/LF/C0 collapse; a `\\r\\nBcc:` subject cannot carry a
     newline into a header.
  6. child deletes (N11) — the six §3.5 sites leave zero orphan threads AND posts; the
     two previews count both; retention.delete_document AND content.remove_target_locale
     exercised DIRECTLY; run with queries_enabled=0 as well as on (§5.2 seam 4).
  7. GDPR BOTH directions (HIGH-1) — erase tombstones all four query attribution columns
     AND LQAResult.actor; export yields the query_threads / query_thread_subjects /
     query_posts / lqa_findings sections (body + note included, dedup proven); the
     symmetry law; flag-off too.
  8. build paths — exactly one alembic head; _ADDED_COLUMNS["lqaresult"] carries the two
     new columns (the zero-diff proof itself is verify_alembic_authority/_migrations).
  9. idempotency both branches (MED-4) — record_vendor_post dedups a post AND a
     thread-creating comment; NULL external_id is unconstrained.
  9b. post_count (L4) — equals COUNT(*) after a mixed sequence.
  9c. key-thread drift (MED-8) — moving a key's value segment does not orphan its thread
      from the key; project_id is unchanged.

Kernel facade (§11 item 10) is NOT tested here: both new services are phantoms in Train
A's tree (reached only service->service), so verify_kernel_facade must stay green
UNCHANGED — which is asserted by running that suite, not this one.
"""

import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_queries_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    LQAResult,
    Project,
    QueryPost,
    QueryStatus,
    QueryThread,
    Role,
    Segment,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    access,
    content,
    gdpr,
    lqa,
    retention,
    security,
    settings,
)
from app.services import (
    query_threads as qt,
)

PW = "Sup3rSecret!pw"
_N = [0]


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _multi_org(on: bool) -> None:
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _ns(email, role=Role.VIEWER, uid=None):
    """A lightweight user for pure RBAC checks under auth-off (can_see_all short-circuits
    on auth_required() == False before touching user attributes)."""
    return types.SimpleNamespace(
        email=email, role=role, id=uid or _uniq(), active=True, customer_id=None, org_id=None
    )


def _project(name=None, *, customer_id=None, org_id=None, created_by="", locales="de-DE") -> int:
    with Session(engine) as s:
        p = Project(
            name=name or "QP %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            customer_id=customer_id,
            org_id=org_id,
            created_by=created_by,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _doc(pid, fmt="txt") -> int:
    with Session(engine) as s:
        d = Document(project_id=pid, filename=f"f.{fmt}", fmt=fmt)
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _seg(doc_id, locale="de-DE", target="", order=0, key_id=None) -> int:
    with Session(engine) as s:
        sg = Segment(
            document_id=doc_id,
            target_locale=locale,
            order_index=order,
            source_text="src %d" % _uniq(),  # noqa: UP031
            target_text=target,
            key_id=key_id,
        )
        s.add(sg)
        s.commit()
        s.refresh(sg)
        return sg.id


def _org(name) -> int:
    from app.models import Organization

    with Session(engine) as s:
        o = Organization(name=name, slug="%s-%d" % (name.lower(), _uniq()))  # noqa: UP031
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def _customer(name=None, org_id=None) -> int:
    with Session(engine) as s:
        c = Customer(name=name or "QC %d" % _uniq(), org_id=org_id)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _count(model, **eq) -> int:
    with Session(engine) as s:
        stmt = select(model.id)
        for k, v in eq.items():
            stmt = stmt.where(getattr(model, k) == v)
        return len(s.exec(stmt).all())


def _open(
    pid=None,
    *,
    segment_id=None,
    document_id=None,
    key_id=None,
    subject="Q",
    body="body",
    author="op@x",
    assignee="",
    user=None,
):
    """Seed a thread via the service from an anchor. user=None seeds as the trusted
    operator (the console arm); pass a real user to seed as that user."""
    with Session(engine) as s:
        scope = qt.resolve_scope(
            s, segment_id=segment_id, document_id=document_id, key_id=key_id, project_id=pid
        )
        t = qt.open_thread(
            s,
            user=user,
            scope=scope,
            subject=subject,
            body=body,
            assignee_email=assignee,
            actor=author,
            trusted_operator=(user is None),
            # #515/#73: the trusted seed is the key-verified --operator door (UNSCOPED); a
            # bare None under an active gate now fails closed in _require_view.
            org_id=access.UNSCOPED if user is None else None,
        )
        return t.id


# ============================================================================
# 1. scope resolution
# ============================================================================
def test_scope_resolution():
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    with Session(engine) as s:
        sc = qt.resolve_scope(s, segment_id=sid)
        assert sc == qt.ScopeTuple(pid, did, sid, None, "de-DE"), sc
        sc = qt.resolve_scope(s, document_id=did)
        assert sc.project_id == pid and sc.document_id == did and sc.segment_id is None
        sc = qt.resolve_scope(s, project_id=pid)
        assert sc.project_id == pid and sc.document_id is None
        # zero anchors refuses
        try:
            qt.resolve_scope(s)
            assert False, "no anchor accepted"
        except qt.QueryError:
            pass
        # disagreeing anchors refuse (a segment + a foreign project)
        other = _project()
        try:
            qt.resolve_scope(s, segment_id=sid, project_id=other)
            assert False, "disagreeing anchors accepted"
        except qt.QueryError as e:
            assert "disagree" in str(e), e
    print("  1. scope resolution: anchors resolve; disagreement refuses")


def test_scope_key_segment():
    """A key value's segment resolves with key_id set + document_id NULL (advisory)."""
    pid = _project()
    kdoc = _doc(pid, fmt="keys")
    # a StringKey + its value segment (key_id set)
    from app.models import StringKey

    with Session(engine) as s:
        k = StringKey(project_id=pid, namespace="ns", name="k::a")
        s.add(k)
        s.commit()
        s.refresh(k)
        kid = k.id
    sid = _seg(kdoc, "de-DE", key_id=kid)
    with Session(engine) as s:
        sc = qt.resolve_scope(s, segment_id=sid)
        assert sc.key_id == kid, sc
        assert sc.document_id is None, ("key-value segment must not stamp document_id", sc)
        assert sc.project_id == pid, sc
    print("  1b. key-value segment resolves with key_id set, document_id NULL")


# ============================================================================
# 2. tenant ring (HIGH)
# ============================================================================
def _assert_invisible(user, tid, label):
    with Session(engine) as s:
        for fn in (
            lambda: qt.get_thread(s, user=user, thread_id=tid),
            lambda: qt.add_post(s, user=user, thread_id=tid, body="x", actor=user.email),
            lambda: qt.set_status(
                s, user=user, thread_id=tid, status=QueryStatus.CLOSED, actor=user.email
            ),
            lambda: qt.assign(s, user=user, thread_id=tid, assignee_email="", actor=user.email),
        ):
            try:
                fn()
                assert False, f"{label} reached a foreign thread"
            except qt.QueryNotFound:
                pass  # 404, never 403
        rows, total = qt.list_threads(s, user)
        assert all(r.id != tid for r in rows), f"{label} listed a foreign thread"
        # M5: non-vacuous — a caller with NO in-scope threads must see total == 0, not
        # merely "the foreign row isn't on this page" (which line 221 already covers).
        assert total == 0, "%s: scoped total counted a foreign row (%d)" % (label, total)  # noqa: UP031


def test_tenant_ring():
    _auth(True)
    try:
        # (A) foreign CUSTOMER
        c1 = _customer("Ring C1")
        p1 = _project("Ring P1", customer_id=c1)
        t1 = _open(p1, subject="secret")
        c2 = _customer("Ring C2")
        u_c2 = security.create_user(
            "ring-c2-%d@x.com" % _uniq(),  # noqa: UP031
            "c2",
            Role.LINGUIST,
            PW,
            customer_id=c2,  # noqa: RUF100, UP031
        )
        _assert_invisible(u_c2, t1, "foreign-customer")

        # (A2, NEW-2) NON-EMPTY ring — the scope-before-pagination property actually
        # exercised: a C1-bound viewer sees exactly C1's 3 threads out of 3-own + 5-foreign.
        for i in range(2):
            _open(p1, subject="own-%d" % i)  # p1 now has 3 threads (t1 + 2)  # noqa: UP031
        p_f = _project("Ring Pf", customer_id=c2)
        for i in range(5):
            _open(p_f, subject="foreign-%d" % i)  # 5 foreign threads  # noqa: UP031
        u_c1 = security.create_user(
            "ring-c1-%d@x.com" % _uniq(),  # noqa: UP031
            "c1",
            Role.VIEWER,
            PW,
            customer_id=c1,  # noqa: RUF100, UP031
        )
        with Session(engine) as s:
            rows, total = qt.list_threads(s, u_c1)
            assert total == 3 and len(rows) == 3, (
                "non-empty ring leaked/miscounted",
                total,
                len(rows),
            )
            assert all(r.project_id == p1 for r in rows), "foreign thread in the page"
            rows2, total2 = qt.list_threads(s, u_c1, limit=2)
            assert total2 == 3 and len(rows2) == 2, ("scope-before-pagination", total2, len(rows2))

        # (C) non-member under project_scoping
        settings.set_many({"project_scoping": "1"})
        settings.invalidate_cache()
        p3 = _project("Ring P3", created_by="owner-%d@x.com" % _uniq())  # noqa: UP031
        t3 = _open(p3, subject="secret3")
        u_nm = security.create_user("ring-nm-%d@x.com" % _uniq(), "nm", Role.VIEWER, PW)  # noqa: UP031
        _assert_invisible(u_nm, t3, "non-member")
        # positive control: the project owner sees it
        owner = security.create_user("ring-own-%d@x.com" % _uniq(), "own", Role.VIEWER, PW)  # noqa: UP031
        with Session(engine) as s:
            p = s.get(Project, p3)
            p.created_by = owner.email
            s.add(p)
            s.commit()
        with Session(engine) as s:
            rows, total = qt.list_threads(s, owner)
            assert any(r.id == t3 for r in rows), "owner cannot see own project's thread"
        settings.set_many({"project_scoping": "0"})
        settings.invalidate_cache()

        # (B) foreign ORG
        _multi_org(True)
        try:
            o1, o2 = _org("RingO1"), _org("RingO2")
            p2 = _project("Ring P2", org_id=o1)
            t2 = _open(p2, subject="secret2")
            # #181: created IN its org (the seam refuses an org-less create under the gate).
            u_o2 = security.create_user(
                "ring-o2-%d@x.com" % _uniq(),  # noqa: UP031
                "o2",
                Role.ADMIN,
                PW,
                org_id=o2,
            )
            with Session(engine) as s:
                row = s.get(User, u_o2.id)
                row.org_id = o2
                s.add(row)
                s.commit()
                u_o2 = s.get(User, u_o2.id)
                s.expunge(u_o2)
            _assert_invisible(u_o2, t2, "foreign-org")
        finally:
            _multi_org(False)
    finally:
        _auth(False)
    print(
        "  2. tenant ring: foreign org/customer/non-member -> 404 on read+mutate, "
        "no foreign row listed or counted"
    )


# ============================================================================
# 3. lifecycle
# ============================================================================
def test_lifecycle():
    pid = _project()
    author = "auth-%d@x.com" % _uniq()  # noqa: UP031
    tid = _open(pid, author=author)
    au = _ns(author)
    with Session(engine) as s:
        # a plain reply by a non-author never flips open -> answered
        qt.add_post(s, user=_ns("other@x"), thread_id=tid, body="me too", actor="other@x")
        t, _ = qt.get_thread(s, user=None, thread_id=tid, trusted_operator=True)
        assert t.status == QueryStatus.OPEN, "plain reply flipped status"
        # explicit answer -> answered
        qt.add_post(
            s, user=_ns("other@x"), thread_id=tid, body="the answer", answer=True, actor="other@x"
        )
        t, _ = qt.get_thread(s, user=None, thread_id=tid, trusted_operator=True)
        assert t.status == QueryStatus.ANSWERED, "answer did not mark answered"
        # the author re-asking on answered reopens
        qt.add_post(s, user=au, thread_id=tid, body="still broken?", actor=author)
        t, _ = qt.get_thread(s, user=None, thread_id=tid, trusted_operator=True)
        assert t.status == QueryStatus.OPEN, "author re-ask did not reopen"
        # a non-author non-assignee VIEWER cannot close
        try:
            qt.set_status(
                s,
                user=_ns("passerby@x"),
                thread_id=tid,
                status=QueryStatus.CLOSED,
                actor="passerby@x",
            )
            assert False, "a passer-by closed the thread"
        except qt.QueryForbidden:
            pass
        # the author can close
        qt.set_status(s, user=au, thread_id=tid, status=QueryStatus.CLOSED, actor=author)
        t, _ = qt.get_thread(s, user=None, thread_id=tid, trusted_operator=True)
        assert t.status == QueryStatus.CLOSED and t.closed_by == author, t
        # a post on a closed thread is 409
        try:
            qt.add_post(s, user=au, thread_id=tid, body="more", actor=author)
            assert False, "posted to a closed thread"
        except qt.QueryConflict as e:
            assert e.status == 409, e
    print(
        "  3. lifecycle: no implicit transitions; closed-post 409; author reopen; "
        "passer-by cannot close"
    )


# ============================================================================
# 4. caps
# ============================================================================
def test_caps():
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    with Session(engine) as s:
        sc = qt.resolve_scope(s, segment_id=sid)
        # subject 201 chars
        try:
            qt.open_thread(
                s, user=None, scope=sc, subject="x" * 201, body="b", trusted_operator=True
            )
            assert False
        except qt.QueryError as e:
            assert (
                e.status == 400 and str(e) == "subject is limited to 200 characters (got 201)."
            ), str(e)
        # body 4001 chars
        try:
            qt.open_thread(
                s, user=None, scope=sc, subject="ok", body="y" * 4001, trusted_operator=True
            )
            assert False
        except qt.QueryError as e:
            assert (
                e.status == 400
                and str(e) == "a query post is limited to 4000 characters (got 4001)."
            ), str(e)
        # boundary: exactly 4000 is stored WHOLE, never truncated
        t = qt.open_thread(
            s, user=None, scope=sc, subject="ok", body="z" * 4000, trusted_operator=True
        )
        _t, posts = qt.get_thread(s, user=None, thread_id=t.id, trusted_operator=True)
        assert len(posts[0].body) == 4000, "body was truncated"

    # posts-per-thread cap (200) — bulk-fill to 200 then refuse the 201st
    tid = _open(pid, body="first")
    with Session(engine) as s:
        for i in range(199):
            s.add(QueryPost(thread_id=tid, body="p%d" % i))  # noqa: UP031
        s.commit()
        try:
            qt.add_post(s, user=None, thread_id=tid, body="over", trusted_operator=True)
            assert False
        except qt.QueryConflict as e:
            assert str(e) == "this thread has reached its 200-post limit; open a new query.", str(e)

    # open-threads-per-segment cap (20)
    did2 = _doc(pid)
    sid2 = _seg(did2, "de-DE")
    with Session(engine) as s:
        for i in range(20):  # noqa: B007
            s.add(
                QueryThread(
                    project_id=pid,
                    document_id=did2,
                    segment_id=sid2,
                    target_locale="de-DE",
                    status=QueryStatus.OPEN,
                )
            )
        s.commit()
        sc2 = qt.resolve_scope(s, segment_id=sid2)
        try:
            qt.open_thread(
                s, user=None, scope=sc2, subject="one more", body="b", trusted_operator=True
            )
            assert False
        except qt.QueryConflict as e:
            assert "20 open-query limit" in str(e) and str(sid2) in str(e), str(e)

    # list page: limit/offset/total
    pid3 = _project()
    for i in range(3):
        _open(pid3, subject="t%d" % i)  # noqa: UP031
    with Session(engine) as s:
        rows, total = qt.list_threads(
            s, None, project_id=pid3, limit=2, offset=0, trusted_operator=True
        )
        assert total == 3 and len(rows) == 2, (total, len(rows))
    print(
        "  4. caps: subject/body/posts/open-threads refuse with exact messages; "
        "4000-char body stored whole; list paginates with total"
    )


# ============================================================================
# 5. flatten_header_text
# ============================================================================
def test_flatten():
    assert qt.flatten_header_text("a\r\nBcc: evil@x") == "a  Bcc: evil@x"
    assert qt.flatten_header_text("tab\there\x00null") == "tab here null"
    pid = _project()
    tid = _open(pid, subject="hi\r\nBcc: evil@x.com")
    with Session(engine) as s:
        t = s.get(QueryThread, tid)
        assert "\r" not in t.subject and "\n" not in t.subject, t.subject
    print("  5. flatten_header_text: CR/LF/C0 collapse; a subject cannot carry a newline")


# ============================================================================
# 6. child deletes (N11) — flag-on AND flag-off
# ============================================================================
def _orphans() -> int:
    with Session(engine) as s:
        tids = set(s.exec(select(QueryThread.id)).all())
        posts = s.exec(select(QueryPost.thread_id)).all()
        return sum(1 for pt in posts if pt not in tids)


def _child_delete_pass(flag: str):
    from app.models import TargetLocale

    settings.set_many({"queries_enabled": flag})
    settings.invalidate_cache()

    # (i) project delete + its preview
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    _open(pid, segment_id=sid, subject="seg")
    _open(pid, document_id=did, subject="doc")
    _open(pid, subject="proj")
    with Session(engine) as s:
        prev = content.delete_project_preview(s, pid)
        assert prev["cascade"]["query_threads"] == 3, prev["cascade"]
        assert prev["cascade"]["query_posts"] == 3, prev["cascade"]
        content.delete_project(s, pid)
    assert _count(QueryThread, project_id=pid) == 0, f"project delete left threads ({flag})"
    assert _orphans() == 0, f"project delete left orphan posts ({flag})"

    # (ii) document delete (independent of retention's own reap)
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    t_seg = _open(pid, segment_id=sid)
    t_doc = _open(pid, document_id=did)
    with Session(engine) as s:
        content.delete_document(s, did)
    assert _count(QueryThread, id=t_seg) == 0 and _count(QueryThread, id=t_doc) == 0, flag
    assert _orphans() == 0, f"document delete orphan posts ({flag})"

    # (iii) retention.delete_document DIRECTLY (the load-bearing site, N11)
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    _open(pid, segment_id=sid)
    _open(pid, document_id=did)
    with Session(engine) as s:
        counts = retention.delete_document(s, did, cascade_tm=False)
    assert counts.get("query_threads") == 2, counts
    assert _count(QueryThread, document_id=did) == 0 and _orphans() == 0, flag

    # (iv) remove_target_locale DIRECTLY (independent sa_delete(Segment) site, N11) +
    #      its preview. The locale must be UNtranslated (empty target_text).
    pid = _project()
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid, locale="de-DE"))
        s.add(TargetLocale(project_id=pid, locale="fr-FR"))
        s.commit()
    did = _doc(pid)
    sid_fr = _seg(did, "fr-FR", target="")  # untranslated
    t_seg = _open(pid, segment_id=sid_fr)  # locale fr-FR (from segment)
    t_loc = _open(pid, document_id=did, subject="fr-wide")
    # stamp the doc-scoped thread's locale to fr-FR so the locale reap catches it
    with Session(engine) as s:
        row = s.get(QueryThread, t_loc)
        row.target_locale = "fr-FR"
        s.add(row)
        s.commit()
    with Session(engine) as s:
        prev = content.remove_locale_preview(s, pid, "fr-FR")
        assert prev["deletes"]["query_threads"] == 2, prev["deletes"]
        content.remove_target_locale(s, pid, "fr-FR")
    assert _count(QueryThread, id=t_seg) == 0 and _count(QueryThread, id=t_loc) == 0, flag
    assert _orphans() == 0, f"locale removal orphan posts ({flag})"


def test_child_deletes():
    for flag in ("1", "0"):
        _child_delete_pass(flag)
    settings.set_many({"queries_enabled": "1"})
    settings.invalidate_cache()
    print(
        "  6. child deletes: all six sites reap threads+posts (0 orphans), previews "
        "count, retention+remove_target_locale tested directly, flag-on AND flag-off"
    )


# ============================================================================
# 7. GDPR both directions (HIGH-1) — flag-on AND flag-off
# ============================================================================
def _gdpr_pass(flag: str, tag: str):
    settings.set_many({"queries_enabled": flag})
    settings.invalidate_cache()
    subj = security.create_user(f"subj-{tag}@x.com", "subj", Role.TRANSLATOR, PW)
    email = subj.email
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    with Session(engine) as s:
        # thread the subject authored AND closed (dedup target)
        t1 = QueryThread(
            project_id=pid,
            document_id=did,
            segment_id=sid,
            target_locale="de-DE",
            subject="my own subject",
            status=QueryStatus.CLOSED,
            author=email,
            closed_by=email,
        )
        # thread only ASSIGNED to the subject (exports; its posts do not unless theirs)
        t2 = QueryThread(
            project_id=pid,
            document_id=did,
            target_locale="de-DE",
            subject="someone else's",
            author="other@x",
            assignee_email=email,
        )
        s.add(t1)
        s.add(t2)
        s.commit()
        s.refresh(t1)
        s.refresh(t2)
        s.add(QueryPost(thread_id=t1.id, body="my authored words", author=email))
        s.add(QueryPost(thread_id=t2.id, body="not mine", author="other@x"))
        # a human LQA finding by the subject
        s.add(
            LQAResult(
                document_id=did,
                segment_id=sid,
                dimension="Accuracy",
                severity="major",
                note="my finding note",
                target_locale="de-DE",
                finding_source="human",
                actor=email,
            )
        )
        s.commit()

    # (b) Art.15 export
    sections = {}
    for section, rec in gdpr.export_user(subj.id):
        sections.setdefault(section, []).append(rec)
    assert "query_threads" in sections, sections.keys()
    assert "query_thread_subjects" in sections, sections.keys()
    assert "query_posts" in sections, sections.keys()
    assert "lqa_findings" in sections, sections.keys()
    # dedup: the thread the subject both opened and closed appears ONCE
    qt_ids = [r["id"] for r in sections["query_threads"]]
    assert len(qt_ids) == len(set(qt_ids)), ("query_threads not deduped", qt_ids)
    # body + note are first-order subject data and ARE included
    assert any(r["body"] == "my authored words" for r in sections["query_posts"]), (
        "query post body not exported"
    )
    assert all(r["body"] != "not mine" for r in sections["query_posts"]), (
        "someone else's post body exported"
    )
    assert any(r["note"] == "my finding note" for r in sections["lqa_findings"]), (
        "human finding note not exported"
    )
    # the subject-only subject pass carries the subject line; the thread the subject only
    # ASSIGNED (someone else opened) must NOT contribute its subject here
    subj_lines = [r["subject"] for r in sections["query_thread_subjects"]]
    assert "my own subject" in subj_lines and "someone else's" not in subj_lines, subj_lines

    # (a) Art.17 erase
    ok, _msg = gdpr.erase_user(subj.id, actor=f"admin-{tag}@x.com")
    assert ok, _msg
    with Session(engine) as s:
        for model, cols in (
            (QueryThread, ("author", "assignee_email", "closed_by")),
            (QueryPost, ("author",)),
            (LQAResult, ("actor",)),
        ):
            for row in s.exec(select(model)).all():
                for c in cols:
                    assert getattr(row, c) != email, (
                        f"erased email survived in {model.__name__}.{c} ({flag})"
                    )
        # the assignee tombstone carries the .invalid TLD (a dead SMTP target)
        tomb = [r.assignee_email for r in s.exec(select(QueryThread)).all() if r.assignee_email][0]  # noqa: RUF015
        assert tomb.endswith("@erased.invalid"), tomb


def test_gdpr_both_directions():
    _gdpr_pass("1", "on-%d" % _uniq())  # noqa: UP031
    _gdpr_pass(
        "0",
        "off-%d" % _uniq(),  # noqa: UP031
    )  # NEW-3: dormant PII still exports + tombstones  # noqa: E501, RUF100, UP031
    settings.set_many({"queries_enabled": "1"})
    settings.invalidate_cache()
    print(
        "  7. GDPR: export yields 4 query sections (body/note, dedup); erase tombstones "
        "all 4 query cols + LQAResult.actor; symmetry holds flag-on AND flag-off"
    )


# ============================================================================
# 8. build paths
# ============================================================================
def test_build_paths():
    import glob
    import re

    revs, downs = set(), set()
    for f in glob.glob(os.path.join(_ROOT, "alembic", "versions", "*.py")):
        txt = open(f, encoding="utf-8").read()  # noqa: SIM115
        m = re.search(r"^revision: str = ['\"]([0-9a-f]+)['\"]", txt, re.M)
        d = re.search(r"^down_revision[^=\n]*= ['\"]([0-9a-f]+)['\"]", txt, re.M)
        if m:
            revs.add(m.group(1))
        if d:
            downs.add(d.group(1))
    heads = revs - downs
    # ONE head, and the wave3 migration is IN the chain. (Do not pin the tip itself:
    # every later train's migration moves it — Train I's c7d2e9f4a1b8 turned the old
    # `heads == {"b4d9f2c7e1a3"}` pin stale. Single-head-ness is the invariant this
    # suite owns; the tip's identity belongs to the train that made it.)
    assert len(heads) == 1, ("expected exactly one alembic head", heads)
    assert "b4d9f2c7e1a3" in revs, ("wave3 migration missing from the chain", sorted(revs))
    from app.db import _ADDED_COLUMNS

    cols = dict(_ADDED_COLUMNS["lqaresult"])
    assert cols.get("actor") == "VARCHAR" and cols.get("withdrawn_at") == "DATETIME", cols
    print(
        "  8. build paths: exactly one alembic head; wave3 b4d9f2c7e1a3 in the chain; "
        "_ADDED_COLUMNS carries actor+withdrawn_at"
    )


# ============================================================================
# 9 / 9b / 9c
# ============================================================================
def _vendor_leg(pid, did, locale="de-DE"):
    with Session(engine) as s:
        v = Vendor(name="V %d" % _uniq())  # noqa: UP031
        s.add(v)
        s.commit()
        s.refresh(v)
        wf = Workflow(name="W %d" % _uniq(), project_id=pid)  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        run = WorkflowRun(
            workflow_id=wf.id, document_id=did, target_locale=locale, created_by="runner@x"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        leg = VendorDispatch(
            vendor_id=v.id,
            workflow_run_id=run.id,
            document_id=did,
            target_locale=locale,
            status=VendorDispatchStatus.RETURNED,
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        s.expunge(leg)
        return leg


def test_vendor_idempotency():
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    leg = _vendor_leg(pid, did)
    # reply_to branch: two calls, same (thread, external_id) -> one post
    tid = _open(pid, segment_id=sid, subject="buyer q")
    with Session(engine) as s:
        p1 = qt.record_vendor_post(
            s,
            leg=leg,
            segment_id=sid,
            body="vendor reply",
            author_label="Ana",
            external_id="ext-1",
            reply_to=tid,
        )
        s.commit()
        p1_id = p1.id
    with Session(engine) as s:
        p2 = qt.record_vendor_post(
            s,
            leg=leg,
            segment_id=sid,
            body="vendor reply",
            author_label="Ana",
            external_id="ext-1",
            reply_to=tid,
        )
        s.commit()
        p2_id = p2.id
    assert p1_id == p2_id, "reply_to duplicated a post"
    assert _count(QueryPost, thread_id=tid, external_id="ext-1") == 1
    with Session(engine) as s:
        t = s.get(QueryThread, tid)
        assert t.status == QueryStatus.ANSWERED, "vendor reply did not mark answered"

    # thread-creating branch: two calls, no reply_to, same (dispatch_id, external_id)
    # -> ONE thread (the post-level constraint alone cannot dedup this — MED-4)
    before = _count(QueryThread, project_id=pid)
    with Session(engine) as s:
        qt.record_vendor_post(
            s,
            leg=leg,
            segment_id=sid,
            body="new vendor thread",
            author_label="Bo",
            external_id="ext-2",
            reply_to=None,
        )
        s.commit()
    with Session(engine) as s:
        qt.record_vendor_post(
            s,
            leg=leg,
            segment_id=sid,
            body="new vendor thread",
            author_label="Bo",
            external_id="ext-2",
            reply_to=None,
        )
        s.commit()
    after = _count(QueryThread, project_id=pid)
    assert after - before == 1, ("thread-creating branch duplicated a thread", before, after)
    # a NULL external_id human thread is unconstrained (many posts, no dedup collision)
    tid2 = _open(pid, segment_id=sid, subject="human")
    with Session(engine) as s:
        qt.add_post(s, user=None, thread_id=tid2, body="a", trusted_operator=True)
        qt.add_post(s, user=None, thread_id=tid2, body="b", trusted_operator=True)
    assert _count(QueryPost, thread_id=tid2) == 3, "NULL external_id posts were constrained"
    print(
        "  9. idempotency: reply_to dedups a post; no-reply_to dedups a THREAD; NULL "
        "external_id is unconstrained"
    )


def test_vendor_key_reply():
    """NEW-1/MED-8: a vendor reply_to a KEY-scoped thread (document_id advisory-NULL) on
    the leg's OWN document is ADMITTED (resolved through the segment), while a cross-tenant
    reply_to is still refused."""
    from app.models import StringKey

    pid = _project()
    kdoc = _doc(pid, fmt="keys")
    with Session(engine) as s:
        k = StringKey(project_id=pid, namespace="ns", name="vk::a")
        s.add(k)
        s.commit()
        s.refresh(k)
        kid = k.id
    sid = _seg(kdoc, "de-DE", key_id=kid)
    ktid = _open(pid, segment_id=sid, subject="key q")
    with Session(engine) as s:
        t = s.get(QueryThread, ktid)
        assert t.key_id == kid and t.document_id is None, "fixture not key-scoped"
    # a leg on the KEY value segment's document (the internal key doc)
    with Session(engine) as s:
        v = Vendor(name="VK %d" % _uniq())  # noqa: UP031
        s.add(v)
        s.commit()
        s.refresh(v)
        wf = Workflow(name="WK %d" % _uniq(), project_id=pid)  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        run = WorkflowRun(
            workflow_id=wf.id, document_id=kdoc, target_locale="de-DE", created_by="r@x"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        leg = VendorDispatch(
            vendor_id=v.id,
            workflow_run_id=run.id,
            document_id=kdoc,
            target_locale="de-DE",
            status=VendorDispatchStatus.RETURNED,
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        s.expunge(leg)
    # own-document reply -> ADMITTED (the over-refusal bug is gone)
    with Session(engine) as s:
        p = qt.record_vendor_post(
            s,
            leg=leg,
            segment_id=sid,
            body="vendor on key",
            author_label="V",
            external_id="k-1",
            reply_to=ktid,
        )
        s.commit()
        assert p.id is not None
        assert s.get(QueryThread, ktid).status == QueryStatus.ANSWERED
    # cross-tenant reply_to (a thread on a DIFFERENT document) -> still refused
    other_did = _doc(pid)
    other_sid = _seg(other_did, "de-DE")
    other_t = _open(pid, segment_id=other_sid)
    with Session(engine) as s:
        try:
            qt.record_vendor_post(
                s,
                leg=leg,
                segment_id=sid,
                body="x",
                author_label="V",
                external_id="k-2",
                reply_to=other_t,
            )
            assert False, "cross-document reply_to admitted"
        except qt.QueryNotFound:
            pass
    print(
        "  9d. vendor key-reply: own-document key-thread reply admitted; cross-document "
        "reply_to refused (advisory document_id not trusted)"
    )


def test_post_count():
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    leg = _vendor_leg(pid, did)
    tid = _open(pid, segment_id=sid)  # 1 post
    with Session(engine) as s:
        qt.add_post(s, user=None, thread_id=tid, body="human2", trusted_operator=True)  # 2
        qt.record_vendor_post(
            s, leg=leg, segment_id=sid, body="v", author_label="X", external_id="pc-1", reply_to=tid
        )  # 3
        s.commit()
    # a re-admit (same ext id) must not double-count
    with Session(engine) as s:
        qt.record_vendor_post(
            s, leg=leg, segment_id=sid, body="v", author_label="X", external_id="pc-1", reply_to=tid
        )
        s.commit()
    with Session(engine) as s:
        t = s.get(QueryThread, tid)
        actual = len(s.exec(select(QueryPost.id).where(QueryPost.thread_id == tid)).all())
        assert t.post_count == actual == 3, (t.post_count, actual)
    print("  9b. post_count equals COUNT(*) after a mixed human/vendor/re-admit sequence")


def test_key_thread_drift():
    from app.models import StringKey

    pid = _project()
    kdoc = _doc(pid, fmt="keys")
    with Session(engine) as s:
        k = StringKey(project_id=pid, namespace="ns", name="drift::a")
        s.add(k)
        s.commit()
        s.refresh(k)
        kid = k.id
    sid = _seg(kdoc, "de-DE", key_id=kid)
    tid = _open(pid, segment_id=sid, subject="key q")
    with Session(engine) as s:
        t = s.get(QueryThread, tid)
        assert t.key_id == kid and t.document_id is None and t.project_id == pid, t
    # simulate keys._move_value_rows: the value segment is re-parked under a DIFFERENT
    # internal document; project is NOT touched.
    kdoc2 = _doc(pid, fmt="keys")
    with Session(engine) as s:
        sg = s.get(Segment, sid)
        sg.document_id = kdoc2
        s.add(sg)
        s.commit()
    with Session(engine) as s:
        rows, _total = qt.list_threads(s, None, key_id=kid, trusted_operator=True)
        assert any(r.id == tid for r in rows), "key thread orphaned from its key after move"
        t = s.get(QueryThread, tid)
        assert t.project_id == pid, "tenant anchor changed on a key move"
    print(
        "  9c. key-thread drift: a value-segment move does not orphan the thread; "
        "project_id unchanged"
    )


# ============================================================================
# H2 — attribution is unforgeable
# ============================================================================
def test_forged_actor():
    """A caller cannot pass a fake `actor` string to forge attribution or borrow
    authorization (the gate's probe: close + assign + authored-as)."""
    pid = _project()
    owner = "owner-%d@x.com" % _uniq()  # noqa: UP031
    tid = _open(pid, author=owner)
    viewer = _ns("viewer-%d@x.com" % _uniq())  # a real, non-privileged caller  # noqa: UP031
    with Session(engine) as s:
        # close/assign while forging the owner's address -> still FORBIDDEN (auth is on
        # the derived user.email, never the actor string)
        for fn in (
            lambda: qt.set_status(
                s, user=viewer, thread_id=tid, status=QueryStatus.CLOSED, actor=owner
            ),
            lambda: qt.assign(s, user=viewer, thread_id=tid, assignee_email="", actor=owner),
        ):
            try:
                fn()
                assert False, "forged actor granted authorization"
            except qt.QueryForbidden:
                pass
        # authored-as: open + post while forging a CEO address -> the STORED author is the
        # real caller, not the forged string
        t2 = qt.open_thread(
            s,
            user=viewer,
            scope=qt.resolve_scope(s, project_id=pid),
            subject="forge",
            body="b",
            actor="ceo@x.com",
        )
        assert t2.author == viewer.email, ("open forged author", t2.author)
        p = qt.add_post(s, user=viewer, thread_id=t2.id, body="p", actor="ceo@x.com")
        assert p.author == viewer.email, ("post forged author", p.author)
    print("  H2. forged actor: close/assign refused, author stamped as the real caller")


# ============================================================================
# M1 — assignee validation is scoped, and not-found == out-of-scope (no oracle)
# ============================================================================
def test_assignee_scoping():
    _auth(True)
    _multi_org(True)
    try:
        o_a, o_b = _org("AsgA"), _org("AsgB")
        # a REVIEWER in org A authoring a thread on an org-A project
        # #181: created IN their orgs (the seam refuses an org-less create under the gate).
        rev = security.create_user(
            "asg-rev-%d@x.com" % _uniq(),  # noqa: UP031
            "rev",
            Role.MANAGER,
            PW,
            org_id=o_a,
        )
        with Session(engine) as s:
            row = s.get(User, rev.id)
            row.org_id = o_a
            s.add(row)
            s.commit()
            rev = s.get(User, rev.id)
            s.expunge(rev)
        # an active user in org B (a valid account, just out of the caller's ring)
        other = security.create_user(
            "asg-b-%d@x.com" % _uniq(),  # noqa: UP031
            "b",
            Role.LINGUIST,
            PW,
            org_id=o_b,
        )
        with Session(engine) as s:
            row = s.get(User, other.id)
            row.org_id = o_b
            s.add(row)
            s.commit()
        pid = _project("AsgP", org_id=o_a)
        tid = _open(pid, author=rev.email)  # trusted seed; author = rev for close/assign auth
        with Session(engine) as s:
            t = s.get(QueryThread, tid)
            t.author = rev.email
            s.add(t)
            s.commit()
        templates = []
        with Session(engine) as s:
            for email in (other.email, "ghost-%d@x.com" % _uniq()):  # noqa: UP031
                try:
                    qt.assign(s, user=rev, thread_id=tid, assignee_email=email)
                    assert False, "assigned out-of-scope / nonexistent user"
                except qt.QueryError as e:
                    # strip the caller's OWN echoed address (not an oracle) and compare the
                    # message template: cross-org and nonexistent must be indistinguishable
                    templates.append(str(e).replace(email, "<addr>"))
        assert templates[0] == templates[1], ("assignee oracle: messages differ", templates)
        assert "in your scope" in templates[0], templates
    finally:
        _multi_org(False)
        _auth(False)
    print(
        "  M1. assignee scoping: cross-org refused; not-found and out-of-scope share "
        "one message (no oracle)"
    )


def test_assignee_scoping_trusted_operator():
    """#67: the CLI (trusted-operator) assign/open paths enforce the ASSIGNEE
    project-visibility invariant (#51) too — the trusted-root status exempts only the
    CALLER-RBAC rings, never the data invariant that the assignee can OPEN the thread.
    Mirrors the /api + web refusal; revert-probe: old code returned early on `trusted`
    and PERSISTED a cross-customer black-hole assignment."""
    _auth(True)
    settings.set_many({"project_scoping": "1"})
    settings.invalidate_cache()
    try:
        c1, c2 = _customer("AsgOpC1"), _customer("AsgOpC2")
        pid = _project("AsgOpP", customer_id=c1, created_by="admin@op.test")
        did = _doc(pid)
        sid = _seg(did)
        tid = _open(segment_id=sid, author="admin@op.test")  # trusted seed, unassigned
        # a customer-2-bound viewer: cannot see the customer-1 project (a black hole)
        c2v = security.create_user(
            "asgop-c2v-%d@x.com" % _uniq(),  # noqa: UP031
            "c2v",
            Role.VIEWER,
            PW,
            customer_id=c2,  # noqa: RUF100, UP031
        )
        # an internal admin (see-all) — a legitimately visible assignee
        adm = security.create_user("asgop-adm-%d@x.com" % _uniq(), "adm", Role.ADMIN, PW)  # noqa: UP031
        # (a) trusted-operator ASSIGN of the cross-customer viewer -> REFUSED, not persisted.
        with Session(engine) as s:
            try:
                qt.assign(
                    s,
                    user=None,
                    thread_id=tid,
                    assignee_email=c2v.email,
                    actor="admin@op.test",
                    trusted_operator=True,
                )
                assert False, "#67: CLI assigned a cross-customer black-hole assignee"
            except qt.QueryError as e:
                assert "in your scope" in str(e), e
        with Session(engine) as s:
            assert (s.get(QueryThread, tid).assignee_email or "") == "", (
                "#67: a refused CLI assign still persisted the assignee"
            )
        # (b) trusted-operator ASSIGN of a see-all admin -> SUCCEEDS (visible member).
        with Session(engine) as s:
            r = qt.assign(
                s,
                user=None,
                thread_id=tid,
                assignee_email=adm.email,
                actor="admin@op.test",
                trusted_operator=True,
            )
            s.commit()
            assert r.assignee_email == adm.email, r.assignee_email
        # (c) trusted-operator OPEN with a cross-customer assignee -> REFUSED (same gate).
        with Session(engine) as s:
            scope = qt.resolve_scope(s, segment_id=sid)
            try:
                qt.open_thread(
                    s,
                    user=None,
                    scope=scope,
                    subject="Q2",
                    body="x",
                    assignee_email=c2v.email,
                    actor="admin@op.test",
                    trusted_operator=True,
                )
                assert False, "#67: CLI open_thread accepted a cross-customer assignee"
            except qt.QueryError as e:
                assert "in your scope" in str(e), e
        # leave no live assignment behind (a later suite scans all threads for the FIRST
        # assigned one) — clear it via the trusted path (empty address is always allowed).
        with Session(engine) as s:
            qt.assign(
                s,
                user=None,
                thread_id=tid,
                assignee_email="",
                actor="admin@op.test",
                trusted_operator=True,
            )
            s.commit()
    finally:
        settings.set_many({"project_scoping": "0"})
        settings.invalidate_cache()
        _auth(False)
    print(
        "  M1b (#67). CLI trusted-operator assign/open enforce assignee project-"
        "visibility: cross-customer refused (not persisted); a visible member assigns"
    )


# ============================================================================
# H3 — the re-score guard + the three human-finding helpers
# ============================================================================
def _finding(session, doc_id, seg_id, *, source, note, locale="de-DE", actor="", seg_null=False):
    session.add(
        LQAResult(
            document_id=doc_id,
            segment_id=None if seg_null else seg_id,
            dimension="Accuracy",
            severity="major",
            note=note,
            score=None if source == "human" else 90.0,
            target_locale=None if source is None else locale,
            finding_source=source,
            actor=actor,
            scoring_id=None if source in (None, "human") else "sc-%d" % _uniq(),  # noqa: UP031
        )
    )


def _notes_for(doc_id, locale=None):
    with Session(engine) as s:
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == doc_id)).all()
        return {(r.finding_source, r.note, r.segment_id, r.target_locale) for r in rows}


def test_rescore_guard():
    """§6.3 headline invariant, mixed fixture across TWO locales + a revert-probe."""
    pid = _project()
    did = _doc(pid)
    s_de = _seg(did, "de-DE", target="Hallo", order=0)
    s_fr = _seg(did, "fr-FR", target="Bonjour", order=1)
    rev = _ns("rev@x.com", role=Role.REVIEWER)
    with Session(engine) as s:
        seg_de = s.get(Segment, s_de)
        lqa.log_human_finding(
            s,
            user=rev,
            segment=seg_de,
            dimension="Accuracy/Omission",
            severity="major",
            note="human-de",
        )
        # machine seg-level (de), legacy NULL-source (de), workflow doc-level (NULL seg),
        # and a human finding on the OTHER locale (fr) that must also survive
        _finding(s, did, s_de, source="llm", note="machine-de")
        _finding(s, did, s_de, source=None, note="legacy-de")
        _finding(s, did, s_de, source="llm", note="workflow", seg_null=True)
        seg_fr = s.get(Segment, s_fr)
        lqa.log_human_finding(
            s, user=rev, segment=seg_fr, dimension="Accuracy", severity="major", note="human-fr"
        )
        s.commit()

    # re-score de-DE with persist -> de human survives, de machine replaced, de legacy(NULL)
    # cleared, workflow(NULL seg) untouched, and the FR human is untouched (locale filter)
    with Session(engine) as s:
        lqa.score_document(s, did, "de-DE", persist=True)
    after = _notes_for(did)
    notes = {n for (_fs, n, _sg, _loc) in after}
    assert "human-de" in notes, "re-score DESTROYED a human finding (guard broken)"
    assert "human-fr" in notes, "re-score touched another locale's human finding"
    assert "workflow" in notes, "re-score cleared a workflow (NULL seg) row"
    assert "legacy-de" not in notes, "legacy NULL-source row was not cleared (regression)"
    assert "machine-de" not in notes, "old machine seg-level row was not replaced"

    # REVERT-PROBE: drop the IS NULL/!=human arm and prove the guard is load-bearing — the
    # delete then also removes the human rows. Simulated by running the un-guarded
    # predicate against a fresh fixture.
    from sqlalchemy import func as _func
    from sqlalchemy import or_ as _or

    pid2 = _project()
    did2 = _doc(pid2)
    sd2 = _seg(did2, "de-DE", target="x")
    with Session(engine) as s:
        seg = s.get(Segment, sd2)
        lqa.log_human_finding(
            s, user=rev, segment=seg, dimension="Accuracy", severity="major", note="human-probe"
        )
        s.commit()
    with Session(engine) as s:
        # the GUARDED predicate leaves it; the UN-guarded predicate (no finding_source arm)
        # would delete it — assert the difference.
        guarded = s.exec(
            select(LQAResult).where(
                LQAResult.document_id == did2,
                LQAResult.segment_id.is_not(None),
                _or(
                    _func.lower(LQAResult.target_locale) == "de-de",
                    LQAResult.target_locale.is_(None),
                    LQAResult.target_locale == "",
                ),
                _or(LQAResult.finding_source.is_(None), LQAResult.finding_source != "human"),
            )
        ).all()
        unguarded = s.exec(
            select(LQAResult).where(
                LQAResult.document_id == did2,
                LQAResult.segment_id.is_not(None),
                _or(
                    _func.lower(LQAResult.target_locale) == "de-de",
                    LQAResult.target_locale.is_(None),
                    LQAResult.target_locale == "",
                ),
            )
        ).all()
    assert len(guarded) == 0 and len(unguarded) == 1, (
        "revert-probe: the finding_source arm is what spares the human row",
        len(guarded),
        len(unguarded),
    )
    print(
        "  H3a. re-score guard: human (both locales) survives, machine/legacy replaced, "
        "workflow untouched; revert-probe proves the IS NULL/!=human arm is load-bearing"
    )


def test_human_finding_helpers():
    """create RBAC (can_review+), edit-own-only + manager override, withdraw idempotence,
    machine-row refusal (§6.2/§6.5, enforced in the service as defence in depth)."""
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE", target="x")
    rev = _ns("rev-%d@x.com" % _uniq(), role=Role.REVIEWER)  # noqa: UP031
    rev2 = _ns("rev2-%d@x.com" % _uniq(), role=Role.REVIEWER)  # noqa: UP031
    mgr = _ns("mgr-%d@x.com" % _uniq(), role=Role.MANAGER)  # noqa: UP031
    tr = _ns("tr-%d@x.com" % _uniq(), role=Role.TRANSLATOR)  # noqa: UP031
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        # create: a translator (can_write, NOT can_review) is refused
        try:
            lqa.log_human_finding(
                s, user=tr, segment=seg, dimension="Accuracy", severity="minor", note="n"
            )
            assert False, "a translator logged a finding"
        except lqa.HumanFindingForbidden:
            pass
        # a reviewer succeeds; the actor is the reviewer, unforgeable
        row = lqa.log_human_finding(
            s,
            user=rev,
            segment=seg,
            dimension="Accuracy",
            severity="minor",
            note="orig",
            actor="ceo@x.com",
        )
        assert row.actor == rev.email, ("finding actor forgeable", row.actor)
        fid = row.id
    with Session(engine) as s:
        r = s.get(LQAResult, fid)
        # edit-own-only: another reviewer cannot edit someone else's finding
        try:
            lqa.edit_human_finding(
                s, user=rev2, finding=r, dimension="Accuracy", severity="major", note="hijack"
            )
            assert False, "a non-author reviewer edited another's finding"
        except lqa.HumanFindingForbidden:
            pass
        # the author can edit
        lqa.edit_human_finding(
            s, user=rev, finding=r, dimension="Fluency", severity="critical", note="edited"
        )
        r = s.get(LQAResult, fid)
        assert r.dimension == "Fluency" and r.note == "edited"
        # a MANAGER may withdraw a finding they did not author
        lqa.withdraw_human_finding(s, user=mgr, finding=r)
        r = s.get(LQAResult, fid)
        first_ts = r.withdrawn_at
        assert first_ts is not None
        # withdraw is idempotent (keeps the original timestamp)
        lqa.withdraw_human_finding(s, user=mgr, finding=r)
        r = s.get(LQAResult, fid)
        assert r.withdrawn_at == first_ts, "re-withdraw changed the timestamp"
        # a MACHINE row cannot be edited or withdrawn
        _finding(s, did, sid, source="llm", note="machine")
        s.commit()
    with Session(engine) as s:
        m = s.exec(select(LQAResult).where(LQAResult.finding_source == "llm")).first()
        for fn in (
            lambda: lqa.edit_human_finding(
                s, user=mgr, finding=m, dimension="Accuracy", severity="minor", note="x"
            ),
            lambda: lqa.withdraw_human_finding(s, user=mgr, finding=m),
        ):
            try:
                fn()
                assert False, "a machine row was edited/withdrawn"
            except lqa.HumanFindingError:
                pass
    print(
        "  H3b. helpers: create needs reviewer+; edit own-only + manager override; "
        "withdraw idempotent; machine rows refused; actor unforgeable"
    )


# ============================================================================
# M4 — the Art.15/Art.17 symmetry ratchet
# ============================================================================
def test_gdpr_symmetry_ratchet():
    swept = {label for label, _m, _c in gdpr._ERASE_SWEEP}
    covered = set(gdpr._ERASE_EXPORT_SECTION)
    exempt = set(gdpr._EXPORT_EXEMPT)
    # every swept label is decided EXACTLY once: exported OR exempt, never both/neither
    assert covered & exempt == set(), ("a label is both exported and exempt", covered & exempt)
    assert swept == covered | exempt, (
        "a swept column has no export section and no exemption (add one)",
        swept - (covered | exempt),
        (covered | exempt) - swept,
    )
    # every exemption carries a reason
    for label in exempt:
        assert gdpr._EXPORT_EXEMPT[label].strip(), f"exemption {label!r} has no reason"
    # functional: a subject with a thread + a human finding actually exports the mapped
    # query/lqa sections (proves the map points at REAL sections, not typos)
    subj = security.create_user("ratchet-%d@x.com" % _uniq(), "r", Role.TRANSLATOR, PW)  # noqa: UP031
    pid = _project()
    did = _doc(pid)
    sid = _seg(did, "de-DE")
    with Session(engine) as s:
        s.add(
            QueryThread(
                project_id=pid,
                document_id=did,
                segment_id=sid,
                target_locale="de-DE",
                subject="s",
                author=subj.email,
            )
        )
        s.add(
            LQAResult(
                document_id=did,
                segment_id=sid,
                dimension="Accuracy",
                severity="major",
                note="n",
                target_locale="de-DE",
                finding_source="human",
                actor=subj.email,
            )
        )
        s.commit()
    got = {section for section, _rec in gdpr.export_user(subj.id)}
    for label in ("query_thread_authors", "query_post_authors", "lqa_findings"):
        sec = gdpr._ERASE_EXPORT_SECTION[label]
        if label != "query_post_authors":  # no post seeded, so that section may be absent
            assert sec in got, f"mapped export section {sec!r} absent for {label!r}"
    print(
        "  M4. GDPR symmetry ratchet: every swept column is exported or exempt "  # noqa: UP031
        "(%d exported, %d exempt); a new swept column forces a decision"
        % (len(covered), len(exempt))
    )


def main() -> None:
    init_db()
    settings.set_many({"audit_enabled": "1", "queries_enabled": "1"})
    test_scope_resolution()
    test_scope_key_segment()
    test_tenant_ring()
    test_lifecycle()
    test_forged_actor()
    test_assignee_scoping()
    test_assignee_scoping_trusted_operator()
    test_caps()
    test_flatten()
    test_child_deletes()
    test_gdpr_both_directions()
    test_gdpr_symmetry_ratchet()
    test_build_paths()
    test_vendor_idempotency()
    test_vendor_key_reply()
    test_post_count()
    test_key_thread_drift()
    test_rescore_guard()
    test_human_finding_helpers()
    print("QUERIES OK")


if __name__ == "__main__":
    main()
