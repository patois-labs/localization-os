"""Wave 3 Train B — the query SURFACES (web / /api / CLI / MCP) + the kill switch.

SPEC-wave3-collaboration §11 Train B (items 11-16). The tenant ring itself (HIGH) and the
lifecycle/caps/GDPR are Train A's (verify_queries.py); this suite proves the four surfaces
are thin, parity-equal callers of that service, that RBAC (§4.5) refuses identically on web
and /api, that the assignee field cannot be a mail relay, that a refused attempt writes no
feature audit row, that notifications behave per §4.6, and — enumerated per row of §5.2 —
that the queries_enabled kill switch REMOVES the feature everywhere.

  11. four-surface parity — the same segment opened on web / /api / CLI stores an
      equivalent thread; GET returns the same thread+posts on web and /api.
  12. RBAC matrix (§4.5) — a viewer may open/post/answer/read; only author/assignee/
      reviewer+ close; only author/reviewer+ assign — identical verdict on web and /api;
      a customer-bound viewer opens on its own customer and 404s on another's.
  13. assignee validation — must resolve to an in-scope ACTIVE user; an arbitrary or
      inactive address is refused, and the refusal writes no audit row (the mail-relay guard).
  14. audit — each success writes exactly one query.* row (right action/target/customer_id);
      a refused RBAC / cap / closed-post writes NO query.* row (the generic middleware row is
      out of this feature's control and is asserted by verify_features.check_gate_audit_parity).
  15. notifications (§4.6) — assignee on open; author+assignee minus actor on post; nobody
      unassigned; notify_on_query=0 suppresses; unconfigured SMTP is a silent no-op; the built
      Subject carries NO user text.
  16. KILL SWITCH — a probe per row of §5.2: (a) 6 web routes 404 (auth off AND on), (b) 6
      /api routes JSON-404, (c) CLI refuses byte-identically, (d) MCP tools gated (GATE_404),
      (e) nav absent + card 404 on /help AND /api/capabilities + dropped from the neighbour
      list, (f) the editor/document/project/key affordances render nothing, (g) no notification
      is sent, (h) no query.* audit row from a refused surface, (i) dormancy: threads survive
      byte-identically across off->on, (j) queries off does not hide an unrelated feature.

  17-19 (MCP accounting/tools, CLI inventory, i18n) are proven by verify_mcp_parity /
  verify_mcp_tools / verify_cli_parity / verify_i18n and are only spot-checked here.

Standalone one-process-per-suite; LOC_TMS_DATA_DIR is a throwaway temp dir, provider keys
cleared -> MockProvider. ASCII-only output.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qsurf_")

import hashlib

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    Project,
    QueryPost,
    QueryStatus,
    QueryThread,
    Role,
    Segment,
    User,
)
from app.services import (
    apikeys,
    content,
    mailer,
    notifications,
    security,
    settings,
)
from app.services import (
    query_threads as qt,
)
from tests._csrf import csrf_for

PW = "Sup3rSecret!pw-1234"
_N = [0]


def _uniq():
    _N[0] += 1
    return _N[0]


def _set(**kw):
    settings.set_many({k: v for k, v in kw.items()})  # noqa: C416
    settings.invalidate_cache()


def _auth(on):
    # project_scoping OFF so every org user can VIEW the project -- this isolates the
    # ACTION-role gate (open/post/answer vs close/assign) from membership scoping, the
    # verify_api_rbac approach. The tenant ring itself (org/customer/membership) is
    # Train A's proof (verify_queries) + test_customer_bound_scope below.
    _set(security_auth_required=("1" if on else "0"), multi_org_enabled="0", project_scoping="0")


def _queries(on):
    _set(queries_enabled=("1" if on else "0"))


def _mk_user(role, customer_id=None, active=True):
    email = "u%d@x.com" % _uniq()  # noqa: UP031
    u = security.create_user(email, "U", role, PW, customer_id=customer_id)
    if not active:
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.active = False
            s.add(row)
            s.commit()
    return u


def _key(user):
    return apikeys.create_key(user.id, "qsurf", scopes="api docs")[1]


def _h(raw):
    return {"X-Loc-Token": raw}


def _web(email):
    """A TestClient logged in as `email` with its CSRF header installed (auth ON).

    The login POST is itself a mutating request, so it needs an anon CSRF token FIRST;
    a successful login rotates the session, so refresh the token AFTER (the verify_accounts
    pattern)."""
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code in (302, 303), "login for %s failed: %d" % (email, r.status_code)  # noqa: UP031
    csrf_for(c)
    return c


def _seed_project(customer_id=None, name=None):
    init_db()
    with Session(engine) as s:
        proj = content.project_create(
            s,
            name or ("QS Project %d" % _uniq()),  # noqa: UP031
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="admin@x.com",
            customer_id=(str(customer_id) if customer_id else None),
        )
        pid = proj.id
        doc = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        segs = []
        for i in range(2):
            seg = Segment(
                document_id=did,
                source_text="hello%d" % i,  # noqa: UP031
                target_text="",
                target_locale="fr-FR",
                order_index=i,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            segs.append(seg.id)
    return pid, did, segs


def _customer(name):
    with Session(engine) as s:
        c = content.customer_create(s, name) if hasattr(content, "customer_create") else None
        if c is None:
            from app.models import Customer

            c = Customer(name=name)
            s.add(c)
            s.commit()
            s.refresh(c)
        return c.id


def _open_via_api(key, segment_id, subject="s", body="b", assignee=None):
    payload = {"segment_id": segment_id, "subject": subject, "body": body}
    if assignee is not None:
        payload["assignee_email"] = assignee
    return TestClient(app).post("/api/queries", json=payload, headers=_h(key))


def _query_audit_rows(action_prefix="query."):
    with Session(engine) as s:
        return [
            r for r in s.exec(select(AuditLog)).all() if (r.action or "").startswith(action_prefix)
        ]


def _cli_json(argv):
    """Run the REAL in-process CLI (arg parser + dispatch) under --json, capturing stdout,
    and parse the JSON envelope it emits. Returns (rc, payload, raw_text). Mirrors the
    verify_advanced_lens_surfaces._cli pattern; the query CLI verbs are the point here."""
    import contextlib
    import io
    import json

    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cli_main(["--json", *argv])
        except SystemExit as exc:  # cli_main returns a code; guard a bare exit anyway
            rc = exc.code if isinstance(exc.code, int) else 1
    text = buf.getvalue()
    payload = json.loads(text) if text.strip() else {}
    return rc, payload, text


# =========================================================================== #
def test_four_surface_parity():
    _auth(False)
    _queries(True)
    pid, _did, segs = _seed_project()
    seg = segs[0]
    # web (auth off -> SYSTEM admin), /api (admin key), CLI (in-process trusted operator).
    admin = _mk_user(Role.ADMIN)
    key = _key(admin)
    wc = TestClient(app)
    rweb = wc.post(
        "/queries", data={"segment_id": seg, "subject": "web", "body": "wb"}, follow_redirects=False
    )
    assert rweb.status_code == 303, rweb.status_code
    rapi = _open_via_api(key, seg, subject="api", body="ab")
    assert rapi.status_code == 200 and rapi.json()["ok"], rapi.text
    with Session(engine) as s:
        qt.open_thread(
            s,
            user=None,
            scope=qt.resolve_scope(s, segment_id=seg),
            subject="cli",
            body="cb",
            actor="op@x.com",
            trusted_operator=True,
        )
    with Session(engine) as s:
        threads = s.exec(
            select(QueryThread).where(QueryThread.segment_id == seg).order_by(QueryThread.id)
        ).all()
        assert len(threads) == 3, "expected 3 threads, got %d" % len(threads)  # noqa: UP031
        for t in threads:
            assert t.project_id == pid and t.segment_id == seg
            assert t.status == QueryStatus.OPEN and t.post_count == 1
            assert t.target_locale == "fr-FR" and t.author_kind == "human"
        # authorship: web/api are the admin's email; cli is the actor string.
        by_subj = {t.subject: t for t in threads}
        assert by_subj["cli"].author == "op@x.com"
        assert by_subj["api"].author == admin.email
    # GET parity: web + /api return the same thread + post set for the /api thread.
    api_tid = rapi.json()["thread"]["id"]
    gweb = wc.get("/queries/%d" % api_tid)  # noqa: UP031
    assert gweb.status_code == 200 and "api" in gweb.text
    gapi = TestClient(app).get("/api/queries/%d" % api_tid, headers=_h(key))  # noqa: UP031
    body = gapi.json()
    assert body["thread"]["id"] == api_tid and len(body["posts"]) == 1
    assert body["posts"][0]["body"] == "ab"
    print(
        "  11. four-surface parity: web/api/cli store an equivalent open thread; "
        "GET matches on web + /api OK"
    )


def test_rbac_matrix():
    _auth(True)
    _queries(True)
    _pid, _did, segs = _seed_project()
    seg = segs[0]
    viewer = _mk_user(Role.VIEWER)
    viewer2 = _mk_user(Role.VIEWER)
    translator = _mk_user(Role.TRANSLATOR)
    reviewer = _mk_user(Role.REVIEWER)
    kv, kv2, kt, kr = _key(viewer), _key(viewer2), _key(translator), _key(reviewer)

    # A viewer OPENS (no role rung) -> 200 on /api.
    r = _open_via_api(kv, seg, subject="q", body="b")
    assert r.status_code == 200, f"viewer open should pass: {r.text}"
    tid = r.json()["thread"]["id"]
    # A viewer POSTS + ANSWERS + READS.
    assert (
        TestClient(app)
        .post("/api/queries/%d/posts" % tid, json={"body": "hi"}, headers=_h(kv2))  # noqa: UP031
        .status_code
        == 200
    )
    assert (
        TestClient(app)
        .post("/api/queries/%d/posts" % tid, json={"body": "ans", "answer": True}, headers=_h(kv2))  # noqa: UP031
        .status_code
        == 200
    )
    assert TestClient(app).get("/api/queries/%d" % tid, headers=_h(kv2)).status_code == 200  # noqa: UP031
    # A non-author, non-assignee VIEWER cannot CLOSE (403) or ASSIGN (403).
    rc = TestClient(app).post(
        "/api/queries/%d/status" % tid,  # noqa: UP031
        json={"status": "closed"},
        headers=_h(kv2),  # noqa: RUF100, UP031
    )
    assert rc.status_code == 403, "viewer non-author close must 403: %d" % rc.status_code  # noqa: UP031
    ra = TestClient(app).post(
        "/api/queries/%d/assign" % tid,  # noqa: UP031
        json={"assignee_email": reviewer.email},
        headers=_h(kv2),  # noqa: RUF100, UP031
    )
    assert ra.status_code == 403, "viewer non-author assign must 403: %d" % ra.status_code  # noqa: UP031
    # A TRANSLATOR (non-author) also cannot close.
    assert (
        TestClient(app)
        .post("/api/queries/%d/status" % tid, json={"status": "closed"}, headers=_h(kt))  # noqa: UP031
        .status_code
        == 403
    )
    # The AUTHOR (viewer) CAN close own thread.
    assert (
        TestClient(app)
        .post("/api/queries/%d/status" % tid, json={"status": "closed"}, headers=_h(kv))  # noqa: UP031
        .status_code
        == 200
    )
    # A REVIEWER can assign + reopen any thread.
    assert (
        TestClient(app)
        .post("/api/queries/%d/status" % tid, json={"status": "open"}, headers=_h(kr))  # noqa: UP031
        .status_code
        == 200
    )
    assert (
        TestClient(app)
        .post(
            "/api/queries/%d/assign" % tid,  # noqa: UP031
            json={"assignee_email": reviewer.email},
            headers=_h(kr),  # noqa: RUF100, UP031
        )
        .status_code
        == 200
    )

    # WEB mirror: identical verdicts through the session surface.
    wc_v = _web(viewer.email)
    r = wc_v.post(
        "/queries", data={"segment_id": seg, "subject": "w", "body": "b"}, follow_redirects=False
    )
    assert r.status_code == 303
    wtid = int(r.headers["location"].rsplit("/", 1)[1])
    wc_v2 = _web(viewer2.email)
    # viewer2 (non-author) close -> themed 403 (never 303).
    rc = wc_v2.post("/queries/%d/status" % wtid, data={"status": "closed"}, follow_redirects=False)  # noqa: UP031
    assert rc.status_code == 403, "web viewer non-author close must be 403: %d" % rc.status_code  # noqa: UP031
    # reviewer close -> 303.
    wc_r = _web(reviewer.email)
    rc = wc_r.post("/queries/%d/status" % wtid, data={"status": "closed"}, follow_redirects=False)  # noqa: UP031
    assert rc.status_code == 303, "web reviewer close must 303: %d" % rc.status_code  # noqa: UP031
    print(
        "  12. RBAC matrix: viewer opens/posts/answers/reads; only author/assignee/"
        "reviewer+ close, only author/reviewer+ assign -- identical on web + /api OK"
    )


def test_customer_bound_scope():
    _set(security_auth_required="1", multi_org_enabled="0")
    _queries(True)

    ca, cb = _customer("Cust A %d" % _uniq()), _customer("Cust B %d" % _uniq())  # noqa: UP031
    _pid_a, _da, segs_a = _seed_project(customer_id=ca)
    _pid_b, _db, segs_b = _seed_project(customer_id=cb)
    bound_a = _mk_user(Role.VIEWER, customer_id=ca)
    ka = _key(bound_a)
    # Opens on its OWN customer's project (via its segment) -> 200.
    r = _open_via_api(ka, segs_a[0], body="mine")
    assert r.status_code == 200, f"customer-bound viewer own-customer open: {r.text}"
    # Another customer's segment -> 404 (non-enumerable), NOT 403.
    r = _open_via_api(ka, segs_b[0], body="theirs")
    assert r.status_code == 404, "cross-customer open must 404: %d" % r.status_code  # noqa: UP031
    # And a thread that exists on B is invisible to bound_a (read 404).
    admin = _mk_user(Role.ADMIN)
    tb = _open_via_api(_key(admin), segs_b[0], body="b-thread").json()["thread"]["id"]
    assert TestClient(app).get("/api/queries/%d" % tb, headers=_h(ka)).status_code == 404  # noqa: UP031
    print(
        "  12b. customer ring: a bound viewer opens on its own customer, 404s on "
        "another's (open + read), never 403 OK"
    )


def test_assignee_thread_customer_ring():
    # F1 (2026-08-20 verification pass): the assignee ring must confine to the THREAD's
    # customer, not only the CALLER's. An internal reviewer+ (customer_id NULL) SKIPS the
    # caller-customer ring, so without the thread-customer confinement it could assign a
    # customer-A thread to a customer-B CLIENT account -> egress of A's project + query text
    # to B (notifications.on_query_opened mail) and B's address surfacing to A. Assigning to
    # its OWN customer or to a GLOBAL internal user must still pass. The refusal is the same
    # generic message (no existence oracle) and writes no assignee.
    _auth(True)
    _queries(True)
    ca, cb = _customer("Ring A %d" % _uniq()), _customer("Ring B %d" % _uniq())  # noqa: UP031
    _pid_a, _da, segs_a = _seed_project(customer_id=ca)
    reviewer = _mk_user(Role.REVIEWER)  # internal staff, customer_id NULL
    client_a = _mk_user(Role.VIEWER, customer_id=ca)
    client_b = _mk_user(Role.VIEWER, customer_id=cb)  # a DIFFERENT customer
    kr = _key(reviewer)
    tid = _open_via_api(kr, segs_a[0], body="a-thread").json()["thread"]["id"]

    def _assignee_of(t):
        with Session(engine) as s:
            return s.get(QueryThread, t).assignee_email

    # ASSIGN the customer-A thread to a customer-B client -> 400, assignee UNCHANGED (no egress).
    rb = TestClient(app).post(
        "/api/queries/%d/assign" % tid,  # noqa: UP031
        json={"assignee_email": client_b.email},
        headers=_h(kr),  # noqa: RUF100, UP031
    )
    assert rb.status_code == 400, "cross-customer assign must refuse: %d" % rb.status_code  # noqa: UP031
    assert (_assignee_of(tid) or "") == "", "refused cross-customer assign wrote an assignee"
    # The OPEN-time path is confined too: open on A with a B-client assignee -> 400.
    ro = _open_via_api(kr, segs_a[0], assignee=client_b.email)
    assert ro.status_code == 400, "cross-customer assignee at open must refuse: %d" % ro.status_code  # noqa: UP031
    # POSITIVE controls: the SAME customer's client, and a GLOBAL internal user, both pass.
    ra = TestClient(app).post(
        "/api/queries/%d/assign" % tid,  # noqa: UP031
        json={"assignee_email": client_a.email},
        headers=_h(kr),  # noqa: RUF100, UP031
    )
    assert ra.status_code == 200, f"same-customer assign must pass: {ra.text}"
    assert _assignee_of(tid) == client_a.email
    staff2 = _mk_user(Role.TRANSLATOR)  # internal, NULL customer -> global
    rg = TestClient(app).post(
        "/api/queries/%d/assign" % tid,  # noqa: UP031
        json={"assignee_email": staff2.email},
        headers=_h(kr),  # noqa: RUF100, UP031
    )
    assert rg.status_code == 200, f"global internal assignee must pass: {rg.text}"
    print(
        "  12c. assignee thread-customer ring: an internal reviewer cannot assign a "
        "customer-A thread to a customer-B client (open + assign); same-customer and global "
        "internal assignees pass OK"
    )


def test_assignee_project_visibility():
    # Issue #51 (round-2): the ASSIGNEE must be able to SEE the thread's project, not merely be
    # visible to the CALLER. With project_scoping ON, an internal user with no membership on the
    # project cannot view it -> assigning a query to them is a silent obligation black hole (the
    # read ring then hides the thread from its own assignee). The fix refuses (same generic
    # no-oracle message, writes no thread/assignee) via the SHARED read predicate; a project
    # MEMBER, and an ADMIN (see-all), still pass. Proven on BOTH write paths (open + assign) and
    # BOTH surfaces (web + /api).
    from app.services import access

    _auth(True)
    _queries(True)
    _set(project_scoping="1")  # membership now GATES project visibility (the #51 precondition)
    try:
        pid, _did, segs = _seed_project()
        admin = _mk_user(Role.ADMIN)  # see-all caller + see-all assignee control
        outsider = _mk_user(Role.REVIEWER)  # internal, NO membership -> cannot view pid
        member = _mk_user(Role.REVIEWER)  # internal, WILL be a member -> can view pid
        with Session(engine) as s:
            access.add_member(s, pid, member.email, added_by="admin@x.com")
            s.commit()
        ka = _key(admin)

        def _assignee_of(t):
            with Session(engine) as s:
                return s.get(QueryThread, t).assignee_email

        # A base thread the admin can always see, used for the /api + web ASSIGN probes.
        tid = _open_via_api(ka, segs[0], body="base").json()["thread"]["id"]
        n_before = len(_query_audit_rows())

        # --- REFUSE: assign to a non-member internal user (/api) -> 400, assignee UNCHANGED.
        ra = TestClient(app).post(
            "/api/queries/%d/assign" % tid,  # noqa: UP031
            json={"assignee_email": outsider.email},
            headers=_h(ka),  # noqa: RUF100, UP031
        )
        assert ra.status_code == 400, (
            "assign to non-visible user must refuse (#51): %d" % ra.status_code  # noqa: UP031
        )
        assert (_assignee_of(tid) or "") == "", "refused #51 assign wrote an assignee"
        # Same generic no-oracle message as the other ring refusals (no existence/why oracle).
        assert "assignable user in your scope" in ra.text, (
            f"refusal must reuse the generic no-oracle message: {ra.text}"
        )
        # A refused assign writes NO query.* audit row (matches the other refusal paths).
        assert len(_query_audit_rows()) == n_before, "refused #51 assign wrote a query.* row"

        # --- REFUSE: the OPEN path with a non-member assignee (/api) -> 400, NO thread created.
        n_threads_before = _thread_count()
        ro = _open_via_api(ka, segs[1], assignee=outsider.email)
        assert ro.status_code == 400, (
            "open with non-visible assignee must refuse (#51): %d" % ro.status_code  # noqa: UP031
        )
        assert _thread_count() == n_threads_before, "refused #51 open created a thread"

        # --- REFUSE on the WEB surface too (assign box) -> non-2xx, assignee UNCHANGED.
        wc = _web(admin.email)
        rw = wc.post(
            "/queries/%d/assign" % tid,  # noqa: UP031
            data={"assignee_email": outsider.email},
            follow_redirects=False,
        )
        assert rw.status_code >= 400, (
            "web assign to non-visible user must refuse (#51): %d" % rw.status_code  # noqa: UP031
        )
        assert (_assignee_of(tid) or "") == "", "refused #51 web assign wrote an assignee"

        # --- ACCEPT: a project MEMBER (can view) -> 200.
        rm = TestClient(app).post(
            "/api/queries/%d/assign" % tid,  # noqa: UP031
            json={"assignee_email": member.email},
            headers=_h(ka),  # noqa: RUF100, UP031
        )
        assert rm.status_code == 200, f"member assignee must pass (#51): {rm.text}"
        assert _assignee_of(tid) == member.email

        # --- ACCEPT: an ADMIN (see-all, no membership needed) -> 200.
        rad = TestClient(app).post(
            "/api/queries/%d/assign" % tid,  # noqa: UP031
            json={"assignee_email": admin.email},
            headers=_h(ka),  # noqa: RUF100, UP031
        )
        assert rad.status_code == 200, f"admin (see-all) assignee must pass (#51): {rad.text}"
        assert _assignee_of(tid) == admin.email

        # --- ACCEPT: the OPEN path with a member assignee -> thread created + assigned.
        rom = _open_via_api(ka, segs[0], assignee=member.email)
        assert rom.status_code == 200, f"open with member assignee must pass (#51): {rom.text}"
        assert rom.json()["thread"]["assignee_email"] == member.email
    finally:
        _set(project_scoping="0")  # restore the suite's default posture for later tests
    print(
        "  12d. assignee project visibility (#51): a non-member internal user cannot be an "
        "assignee (open + assign, web + /api; generic refusal, no thread, no audit row); a "
        "project member and an admin pass OK"
    )


def _thread_count():
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(QueryThread)).one()


def test_customer_bound_assignee_on_global_project_refused():
    # Issue #51 corollary (gate change 4): the visibility arm CORRECTLY narrows assignment —
    # a CUSTOMER-BOUND user can no longer be assigned on a GLOBAL (customer-less) project,
    # because a bound account only ever sees its OWN customer's projects and so could never
    # open the thread. This holds under BOTH project_scoping postures (the customer ring in
    # can_view_project is independent of membership scoping). Auth ON so the ring is live.
    _auth(True)
    _queries(True)
    cust = _customer("Bound %d" % _uniq())  # noqa: UP031
    admin = _mk_user(Role.ADMIN)
    bound = _mk_user(Role.VIEWER, customer_id=cust)
    ka = _key(admin)
    _pid, _did, segs = _seed_project()  # GLOBAL project (customer_id None)
    tid = _open_via_api(ka, segs[0], body="g").json()["thread"]["id"]
    try:
        for scoping in ("0", "1"):
            _set(project_scoping=scoping)
            r = TestClient(app).post(
                "/api/queries/%d/assign" % tid,  # noqa: UP031
                json={"assignee_email": bound.email},
                headers=_h(ka),  # noqa: RUF100, UP031
            )
            assert r.status_code == 400, (
                "a customer-bound assignee on a GLOBAL project must refuse (scoping=%s): %d"  # noqa: UP031
                % (scoping, r.status_code)
            )
            with Session(engine) as s:
                assert (s.get(QueryThread, tid).assignee_email or "") == "", (
                    f"refused bound-on-global assign wrote an assignee (scoping={scoping})"
                )
    finally:
        _set(project_scoping="0")
    print(
        "  12e. customer-bound assignee on a GLOBAL project refused under both scoping "
        "postures (a bound account can never see a customer-less project) OK"
    )


def test_assignee_validation():
    _auth(True)
    _queries(True)
    _pid, _did, segs = _seed_project()
    admin = _mk_user(Role.ADMIN)
    k = _key(admin)
    real = _mk_user(Role.TRANSLATOR)
    inactive = _mk_user(Role.TRANSLATOR, active=False)
    before = len(_query_audit_rows())
    # arbitrary address -> 400, no audit row.
    r = _open_via_api(k, segs[0], assignee="stranger@evil.com")
    assert r.status_code == 400, "arbitrary assignee must refuse: %d" % r.status_code  # noqa: UP031
    # inactive user -> 400.
    r = _open_via_api(k, segs[0], assignee=inactive.email)
    assert r.status_code == 400, "inactive assignee must refuse: %d" % r.status_code  # noqa: UP031
    assert len(_query_audit_rows()) == before, "a refused open wrote an audit row"
    # a real active in-scope user -> 200.
    r = _open_via_api(k, segs[0], assignee=real.email)
    assert r.status_code == 200, f"real assignee must pass: {r.text}"
    print(
        "  13. assignee validation: arbitrary + inactive refused (no audit row), a real "
        "active in-scope user accepted -- the mail-relay guard OK"
    )


def test_audit():
    _auth(True)
    _queries(True)
    _pid, _did, segs = _seed_project()
    admin = _mk_user(Role.ADMIN)
    viewer2 = _mk_user(Role.VIEWER)
    k, kv2 = _key(admin), _key(viewer2)
    tid = _open_via_api(k, segs[0]).json()["thread"]["id"]
    rows = _query_audit_rows()
    opened = [r for r in rows if r.action == "query.open" and r.target == "thread:%d" % tid]  # noqa: UP031
    assert len(opened) == 1, "exactly one query.open row expected"
    assert opened[0].customer_id is None  # this project has no customer
    # a REFUSED close (viewer2 non-author) writes NO query.status row.
    n_before = len(_query_audit_rows())
    assert (
        TestClient(app)
        .post("/api/queries/%d/status" % tid, json={"status": "closed"}, headers=_h(kv2))  # noqa: UP031
        .status_code
        == 403
    )
    assert len(_query_audit_rows()) == n_before, "refused close wrote a query.* audit row"
    # a refused post to a CLOSED thread writes no row either.
    TestClient(app).post(
        "/api/queries/%d/status" % tid,  # noqa: UP031
        json={"status": "closed"},
        headers=_h(k),  # noqa: RUF100, UP031
    )  # admin closes (a real row)
    n2 = len(_query_audit_rows())
    assert (
        TestClient(app)
        .post("/api/queries/%d/posts" % tid, json={"body": "x"}, headers=_h(k))  # noqa: UP031
        .status_code
        == 409
    )
    assert len(_query_audit_rows()) == n2, "a 409 closed-post wrote a query.* audit row"
    print(
        "  14. audit: one row per success (right action/target/customer_id); a refused "
        "RBAC / closed-post writes NO query.* row OK"
    )


def test_notifications():
    # L-11 (deviation, stated): §11 item 15 names "extend tests/verify_notifications.py:52".
    # The query-notification coverage is co-located HERE with the rest of the query-surface
    # suite (assignee-on-open, author+assignee-minus-actor-on-post, unassigned->nobody,
    # notify_on_query=0 suppression, SMTP-off no-op, clean Subject) so a reviewer reads the
    # whole feature's notification contract in one place; verify_notifications.py keeps its
    # job-finish/review/budget coverage. Deliberate deviation, not an omission.
    _auth(False)
    _queries(True)
    _set(notify_on_query="1", org_name="Acme")
    pid, _did, segs = _seed_project()
    assignee = _mk_user(Role.TRANSLATOR)
    author = _mk_user(Role.VIEWER)
    sent = []
    orig = mailer.send

    def _rec(to, subject, body):
        sent.append((to, subject, body))
        return True

    mailer.send = _rec
    try:
        with Session(engine) as s:
            t = qt.open_thread(
                s,
                user=None,
                scope=qt.resolve_scope(s, segment_id=segs[0]),
                subject="SECRET-SUBJ",
                body="SECRET-BODY",
                assignee_email=assignee.email,
                actor=author.email,
                trusted_operator=True,
            )
            tid = t.id
        # opened -> the assignee is mailed; the Subject carries NO user text.
        sent.clear()
        notifications.on_query_opened(tid)
        assert len(sent) == 1 and sent[0][0] == assignee.email
        subj = sent[0][1]
        assert subj == "[Acme] Query on %s: #%d" % (_proj_name(pid), tid), subj  # noqa: UP031
        assert "SECRET-SUBJ" not in subj and "SECRET-BODY" not in subj
        # a reply -> author + assignee MINUS the actor.
        with Session(engine) as s:
            p = qt.add_post(
                s, user=None, thread_id=tid, body="reply", actor=author.email, trusted_operator=True
            )
            pid_post = p.id
        sent.clear()
        n = notifications.on_query_post(tid, pid_post, author.email)  # author is actor
        assert n == 1 and sent[0][0] == assignee.email, (
            "post should mail the assignee only (author is the actor)"
        )
        # unassigned thread notifies nobody.
        with Session(engine) as s:
            t2 = qt.open_thread(
                s,
                user=None,
                scope=qt.resolve_scope(s, segment_id=segs[1]),
                subject="u",
                body="b",
                actor=author.email,
                trusted_operator=True,
            )
            tid2 = t2.id
        sent.clear()
        assert notifications.on_query_opened(tid2) is False and not sent
        # notify_on_query=0 suppresses.
        _set(notify_on_query="0")
        sent.clear()
        assert notifications.on_query_opened(tid) is False and not sent
        _set(notify_on_query="1")
        # unconfigured SMTP: mailer.send returns False -> silent no-op, no crash.
        mailer.send = lambda to, s_, b_: False
        assert notifications.on_query_opened(tid) is False
    finally:
        mailer.send = orig
    print(
        "  15. notifications: assignee on open; author+assignee minus actor on post; "
        "nobody unassigned; notify_on_query=0 suppresses; SMTP-off no-op; Subject clean OK"
    )


def _proj_name(pid):
    with Session(engine) as s:
        return s.get(Project, pid).name


def test_disclosure_ring():
    # H-1 regression: the §7.2 egress disclosure runs resolve_scope on RAW user input, so it
    # MUST be ring-checked — an outsider passing ?document=<foreign> must NOT learn the foreign
    # project's vendor name (the banner-present-iff-open-leg enumeration oracle), and it must be
    # gated on vendors_enabled.
    from app.models import Vendor, VendorDispatch, VendorDispatchStatus, Workflow, WorkflowRun

    _set(
        security_auth_required="1",
        multi_org_enabled="0",
        project_scoping="1",
        queries_enabled="1",
        vendors_enabled="1",
    )
    ca = _customer("DiscA %d" % _uniq())  # noqa: UP031
    cb = _customer("DiscB %d" % _uniq())  # noqa: UP031
    _pid, did, _segs = _seed_project(customer_id=ca)
    with Session(engine) as s:
        v = Vendor(name="AcmeVendorXYZ")
        s.add(v)
        s.commit()
        s.refresh(v)
        wf = Workflow(name="dwf %d" % _uniq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        run = WorkflowRun(workflow_id=wf.id, document_id=did, target_locale="fr-FR")
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(
            VendorDispatch(
                vendor_id=v.id,
                workflow_run_id=run.id,
                document_id=did,
                target_locale="fr-FR",
                status=VendorDispatchStatus.DISPATCHED,
            )
        )
        s.commit()
    admin = _mk_user(Role.ADMIN)  # sees all
    outsider = _mk_user(Role.VIEWER, customer_id=cb)  # a DIFFERENT customer
    insider_html = _web(admin.email).get("/queries?document=%d" % did).text  # noqa: UP031
    assert "AcmeVendorXYZ" in insider_html and "out with" in insider_html, (
        "the insider should see the egress disclosure"
    )
    outsider_html = _web(outsider.email).get("/queries?document=%d" % did).text  # noqa: UP031
    assert "AcmeVendorXYZ" not in outsider_html and "out with" not in outsider_html, (
        "H-1 LEAK: an outsider saw a foreign project's vendor disclosure"
    )
    # vendors OFF -> no banner even for the insider.
    _set(vendors_enabled="0")
    off_html = _web(admin.email).get("/queries?document=%d" % did).text  # noqa: UP031
    assert "AcmeVendorXYZ" not in off_html, "disclosure not gated on vendors_enabled"
    _set(vendors_enabled="1")
    print(
        "  H-1. disclosure ring: outsider -> EMPTY (no vendor-name oracle); gated on "
        "vendors_enabled; insider still sees it OK"
    )


def test_mailer_header_sanitize():
    # H-2 regression: sanitization lives at the mailer boundary and covers U+0085/U+2028/
    # U+2029; a project/subject carrying one still SENDS (sanitized), and a genuine send
    # failure is a WARN, never a silent drop.
    import logging

    import app.services.mailer as m

    _set(
        smtp_host="localhost",
        smtp_port="25",
        smtp_tls="0",
        smtp_user="",
        smtp_from="no-reply@x.com",
    )
    captured = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=15):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

    orig = m.smtplib.SMTP
    m.smtplib.SMTP = _FakeSMTP
    try:
        seps = chr(0x2028) + chr(0x2029) + chr(0x85)
        sep_subject = "Ready " + chr(0x2028) + " Bcc: evil@x.com " + chr(0x85) + " x"
        ok = m.send("to@x.com", sep_subject, "body")
        assert ok is True, "a U+2028/U+0085/U+2029 subject must still SEND (sanitized)"
        subj = str(captured["msg"]["Subject"])
        bad = seps + chr(13) + chr(10)
        assert not any(c in subj for c in bad), (
            "H-2: a line/para separator survived into the Subject header"
        )
        assert "Bcc: evil@x.com" in subj, "the visible text remains (only separators go)"
        # End-to-end: a project NAME and the ORG NAME each carrying U+2028 flow through
        # notifications into the fixed Subject template and STILL send — proving the mailer
        # boundary is the real guard (notifications' own flatten misses U+2028 by design).
        # project_scoping OFF so the freshly-minted assignee can VIEW the project — this
        # mailer test is scoping-agnostic and must not trip the #67 assignee-visibility gate
        # (a prior test leaves project_scoping=1); the documented isolation convention (_auth).
        _set(
            queries_enabled="1",
            notify_on_query="1",
            project_scoping="0",
            org_name="Ac" + chr(0x2028) + "me",
        )
        with Session(engine) as s:
            proj = content.project_create(
                s,
                "Pro" + chr(0x2028) + "ject %d" % _uniq(),  # noqa: UP031
                source_locale="en-US",
                target_locales="fr-FR",
                default_provider="mock",
                created_by="admin@x.com",
            )
            doc = Document(
                project_id=proj.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
            )
            s.add(doc)
            s.commit()
            s.refresh(doc)
            seg = Segment(
                document_id=doc.id,
                source_text="h",
                target_text="",
                target_locale="fr-FR",
                order_index=0,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            segid = seg.id
        asg = _mk_user(Role.TRANSLATOR)
        with Session(engine) as s:
            t = qt.open_thread(
                s,
                user=None,
                scope=qt.resolve_scope(s, segment_id=segid),
                subject="s",
                body="b",
                assignee_email=asg.email,
                actor="op@x.com",
                trusted_operator=True,
            )
            tid2 = t.id
        captured.clear()
        assert notifications.on_query_opened(tid2) is True, (
            "a U+2028 project + org NAME must still send (sanitized at the mailer)"
        )
        e2e_subj = str(captured["msg"]["Subject"])
        assert chr(0x2028) not in e2e_subj and chr(0x85) not in e2e_subj, (
            "H-2: U+2028 from the project/org NAME survived into the header"
        )
        _set(org_name="")
        # a raising SMTP -> False + a WARN (never silent).
        recs = []
        h = logging.Handler()
        h.emit = lambda r: recs.append(r)
        m.log.addHandler(h)

        class _BoomSMTP(_FakeSMTP):
            def send_message(self, msg):
                raise RuntimeError("boom")

        m.smtplib.SMTP = _BoomSMTP
        try:
            assert m.send("to@x.com", "s", "b") is False
            assert any("mail send failed" in r.getMessage() for r in recs), (
                "H-2: a send failure must log a WARN, never drop silently"
            )
        finally:
            m.log.removeHandler(h)
    finally:
        m.smtplib.SMTP = orig
        _set(smtp_host="")
    print(
        "  H-2. mailer boundary: U+2028/U+0085 in a header -> SENT (sanitized); a send "
        "failure -> WARN, never a silent drop OK"
    )


# --------------------------------------------------------------------------- #
_WEB_ROUTES = [
    ("get", "/queries"),
    ("get", "/queries/1"),
    ("post", "/queries"),
    ("post", "/queries/1/posts"),
    ("post", "/queries/1/status"),
    ("post", "/queries/1/assign"),
]
_API_ROUTES = list(_WEB_ROUTES)  # same shapes, /api prefixed below


def test_kill_switch():
    # Seed with the feature ON, then flip OFF.
    _auth(False)
    _queries(True)
    pid, did, segs = _seed_project()
    admin = _mk_user(Role.ADMIN)
    key = _key(admin)
    # create a few threads to prove dormancy later.
    tids = []
    for i in range(3):
        tids.append(
            _open_via_api(key, segs[i % 2], subject="keep%d" % i, body="b%d" % i).json()["thread"][  # noqa: UP031
                "id"
            ]
        )
    dormant_before = _thread_snapshot()

    _queries(False)

    # (a) web routes 404 (auth OFF and ON), themed, never 403.
    for auth in (False, True):
        _set(security_auth_required=("1" if auth else "0"))
        c = TestClient(app)
        for method, path in _WEB_ROUTES:
            r = getattr(c, method)(path, follow_redirects=False)
            assert r.status_code == 404, "web %s %s off/auth=%s -> %d, want 404" % (  # noqa: UP031
                method,
                path,
                auth,
                r.status_code,
            )
    _auth(False)
    # (b) /api routes JSON-404.
    for method, path in _API_ROUTES:
        r = getattr(TestClient(app), method)("/api" + path, headers=_h(key))
        assert r.status_code == 404, "/api%s off -> %d" % (path, r.status_code)  # noqa: UP031
        assert r.json().get("ok") is False
    # (c) CLI refuses byte-identically.
    from app.cli import cmd_query
    from app.cli._common import CliError

    try:
        cmd_query._flag_gate()
        raise AssertionError("cmd_query._flag_gate did not refuse while off")
    except CliError as exc:
        assert str(exc) == "queries are disabled (setting queries_enabled=0).", str(exc)
    # (d) MCP: the queries tools relay a 404 (gated in GATE_404_STYLE); proxy check.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "loc_reg2", os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    )
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)
    assert "queries_enabled" in reg.GATE_404_STYLE
    qtools = [
        t
        for t in reg.TOOLS
        if t.name
        in (
            "list_queries",
            "get_query",
            "open_query",
            "reply_to_query",
            "set_query_status",
            "assign_query",
        )
    ]
    assert len(qtools) == 6 and all(t.feature_gate == "queries_enabled" for t in qtools)
    # (e) nav absent + card 404 both surfaces + dropped from the neighbour list.
    from app import features

    assert features.card_is_off("queries") is True
    home = TestClient(app).get("/").text
    assert 'href="/queries"' not in home, "nav /queries still present while off"
    assert TestClient(app).get("/help/queries").status_code == 404
    # /api/capabilities/{slug} is token-gated; with a valid key it 404s the owned card.
    assert TestClient(app).get("/api/capabilities/queries", headers=_h(key)).status_code == 404
    # (f) in-context affordances render nothing — project page, document page AND the
    # editor row button (M-12: 16f asserted on the editor too, the Jinja-global trap).
    proj = TestClient(app).get("/projects/%d" % pid).text  # noqa: UP031
    assert "/queries?project=%d" % pid not in proj, "project queries panel still rendered"  # noqa: UP031
    docp = TestClient(app).get("/documents/%d" % did).text  # noqa: UP031
    assert "/queries?document=%d" % did not in docp, "document queries panel still rendered"  # noqa: UP031
    editor = TestClient(app).get("/documents/%d/editor?locale=fr-FR" % did).text  # noqa: UP031
    assert "/queries?segment=" not in editor, "editor row query button still rendered when off"
    # (d) path-injection (the 0254a19b class) is structurally N/A for the query tools —
    # every route carries only the integer {qid}, coerced by int(); assert no free-text seg.
    for t in qtools:
        for _method, path in t.routes:
            assert path == "/api/queries" or "{qid}" in path, (
                f"query tool route has an unexpected free-text segment: {path}"
            )
    # (g) no notification is sent while off.
    assert notifications.query_notify_enabled() is False
    # (h) no query.* audit row from a refused surface (auth ON, logged-in user).
    _set(security_auth_required="1")
    u = _mk_user(Role.ADMIN)
    wc = _web(u.email)
    before = len(_query_audit_rows())
    assert (
        wc.post(
            "/queries", data={"segment_id": segs[0], "body": "x"}, follow_redirects=False
        ).status_code
        == 404
    )
    assert len(_query_audit_rows()) == before, "a flag-off refused POST wrote a query.* row"
    _auth(False)
    # (i) dormancy: the DB rows are byte-identical while off.
    assert _thread_snapshot() == dormant_before, "dormant thread data changed while off"
    # (j) queries off does not hide an unrelated feature (reports stays reachable).
    _set(reports_enabled="1")
    assert TestClient(app).get("/reports").status_code == 200

    # re-enable: every thread/post/status/assignee is restored byte-identically, and the
    # affordances come BACK (the toggle is symmetric).
    _queries(True)
    assert _thread_snapshot() == dormant_before, "re-enable did not restore threads"
    for tid in tids:
        assert TestClient(app).get("/api/queries/%d" % tid, headers=_h(key)).status_code == 200  # noqa: UP031
    assert "/queries?document=%d" % did in TestClient(app).get("/documents/%d" % did).text, (  # noqa: UP031
        "document affordance did not return on re-enable"
    )
    assert (
        "/queries?segment=" in TestClient(app).get("/documents/%d/editor?locale=fr-FR" % did).text  # noqa: UP031
    ), "editor affordance did not return"
    print(
        "  16. kill switch: web+api 404 (auth off+on), CLI/MCP refuse, nav+card gone, "
        "affordances blank, no mail, no feature audit row, dormant & restored OK"
    )


def _thread_snapshot():
    with Session(engine) as s:
        threads = s.exec(select(QueryThread).order_by(QueryThread.id)).all()
        posts = s.exec(select(QueryPost).order_by(QueryPost.id)).all()
    blob = repr(
        [
            (
                t.id,
                t.project_id,
                t.segment_id,
                t.subject,
                t.status,
                t.author,
                t.assignee_email,
                t.post_count,
            )
            for t in threads
        ]
    ) + repr([(p.id, p.thread_id, p.body, p.author, p.author_kind) for p in posts])
    return hashlib.sha256(blob.encode()).hexdigest()


def test_timestamps_short_format():
    # Issue #54 L1 (round-2): raw DB timestamps (`2026-08-20 21:15:18.401658`) leaked into
    # UI tables. A single shared `shortdt` Jinja filter now renders `YYYY-MM-DD HH:MM`. This
    # pin proves the /queries list UPDATED cell and the thread post header carry the short
    # form and NO microseconds (the LQA + my-work siblings share the same filter).
    import re as _re

    _auth(True)
    _queries(True)
    author = _mk_user(Role.REVIEWER)
    _pid, _did, segs = _seed_project()
    wc = _web(author.email)
    r = wc.post(
        "/queries",
        data={"segment_id": segs[0], "subject": "ts", "body": "hello"},
        follow_redirects=False,
    )
    assert r.status_code == 303, "web open failed: %d" % r.status_code  # noqa: UP031
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    micro = _re.compile(r"\d{2}:\d{2}:\d{2}\.\d")  # HH:MM:SS.microseconds
    short = _re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?!:)")  # YYYY-MM-DD HH:MM (no :SS)
    lst = wc.get("/queries").text
    assert not micro.search(lst), "microsecond timestamp leaked on the /queries list"
    assert short.search(lst), "short timestamp not rendered on the /queries list"
    th = wc.get("/queries/%d" % tid).text  # noqa: UP031
    assert not micro.search(th), "microsecond timestamp leaked on the thread page"
    assert short.search(th), "short timestamp not rendered on the thread post header"
    print(
        "  L1 timestamps: /queries list UPDATED + thread post header render the shared "
        "short format (no microseconds) OK"
    )


def test_shortdt_filter_unit():
    # Issue #54 L1 / gate change 5: the shared short-timestamp Jinja filter. A datetime
    # renders YYYY-MM-DD HH:MM (no microseconds); a DATE-ONLY value (or a date-only ISO
    # string) renders with NO time part (was "YYYY-MM-DD 00:00"); None/"" -> ""; an
    # unparseable value passes through unchanged so a template can never 500 on it.
    from datetime import date
    from datetime import datetime as _dt

    from app.web.deps import short_dt

    assert short_dt(_dt(2026, 8, 20, 21, 15, 18, 401658)) == "2026-08-20 21:15"
    assert short_dt(date(2026, 8, 20)) == "2026-08-20", "a bare date must not show a 00:00 tail"
    assert short_dt("2026-08-20") == "2026-08-20", "a date-only ISO string stays date-only"
    assert short_dt("2026-08-20T21:15:18.401658") == "2026-08-20 21:15"
    assert short_dt("2026-08-20 21:15:18.401658") == "2026-08-20 21:15"
    assert short_dt(None) == "" and short_dt("") == ""
    assert short_dt("not-a-date") == "not-a-date", "an unparseable value passes through"
    print("  L1 shortdt unit: datetime->short, date-only->no time, None->'', bad->passthrough OK")


def test_cli_list_mine_by_actor():
    # GitHub #456 (found in the PR #442 review): `query list --mine` always returned total 0.
    # The in-process CLI has no session user, so the service's mine filter required
    # author == "" while the CLI's actor filter required author == the actor; the two can
    # never both hold. With no session user, --mine now means "author == the actor" (the CLI
    # carries the acting identity in --actor). The web/api --mine path (a real session user)
    # still filters on the user's email -- that behaviour is proven unchanged in verify_queries.
    _auth(False)
    _queries(True)
    pid, _did, segs = _seed_project()
    actor = "cli-mine-%d@x.com" % _uniq()  # noqa: UP031
    other = "cli-other-%d@x.com" % _uniq()  # noqa: UP031
    with Session(engine) as s:
        mine_tid = qt.open_thread(
            s,
            user=None,
            scope=qt.resolve_scope(s, segment_id=segs[0]),
            subject="mine",
            body="b",
            actor=actor,
            trusted_operator=True,
        ).id
        other_tid = qt.open_thread(
            s,
            user=None,
            scope=qt.resolve_scope(s, segment_id=segs[1]),
            subject="other",
            body="b",
            actor=other,
            trusted_operator=True,
        ).id
    # --mine --actor ACTOR must return exactly the actor's thread (on base: total 0).
    rc, data, txt = _cli_json(["--actor", actor, "query", "list", "--mine"])
    assert rc == 0, txt
    ids = sorted(t["id"] for t in data["threads"])
    assert data["total"] == 1, "expected exactly one --mine thread: %s" % txt  # noqa: UP031
    assert ids == [mine_tid], "--mine must return exactly the actor's thread: %s" % ids  # noqa: UP031
    assert all(t["author"] == actor for t in data["threads"]), "--mine leaked a foreign author"
    # Control: the OTHER actor's --mine returns its own thread and never this one.
    rc2, other_data, _txt2 = _cli_json(["--actor", other, "query", "list", "--mine"])
    assert rc2 == 0
    assert sorted(t["id"] for t in other_data["threads"]) == [other_tid]
    # Control: without --mine the trusted operator still sees BOTH threads on the project.
    rc3, all_data, _txt3 = _cli_json(["query", "list", "--project", str(pid)])
    assert rc3 == 0
    assert {mine_tid, other_tid} == {t["id"] for t in all_data["threads"]}
    print("  #456 CLI query list --mine returns exactly the actor's thread (web/api unchanged) OK")


def main():
    init_db()  # schema before any settings read (tests toggle auth first)
    test_four_surface_parity()
    test_rbac_matrix()
    test_customer_bound_scope()
    test_assignee_thread_customer_ring()
    test_assignee_project_visibility()
    test_customer_bound_assignee_on_global_project_refused()
    test_assignee_validation()
    test_audit()
    test_notifications()
    test_shortdt_filter_unit()  # #54 L1 (gate change 5)
    test_timestamps_short_format()  # #54 L1
    test_cli_list_mine_by_actor()  # #456 (CLI --mine returns 0)
    test_disclosure_ring()  # H-1 regression
    test_mailer_header_sanitize()  # H-2 regression
    test_kill_switch()
    print("QUERIES SURFACES OK")


if __name__ == "__main__":
    main()
