"""Wave 3 Train D — the cross-project reviewer worklist ("My work").

SPEC-wave3-collaboration §11 Train D (items 28-34). Proves, on a throwaway data dir:

 28. Section 1 (review) — a paused run whose STEP DEFINITION names me appears; one naming
     someone else does not; a COMPLETED run whose step names me does not; two paused runs
     of the SAME workflow BOTH appear (the §8.3 limitation, pinned as behaviour).
 29. Section 2 (query) — assigned open/answered appear; my own answered ones appear with
     reason 'answered'; closed do not; a thread in a project I cannot see does not, and
     does NOT count toward total.
 30. Section 3 (quality, RE-KEYED, HIGH-4) — a live major/critical finding in the window
     appears; a withdrawn-only doc does not; a minor/neutral-only doc does not; one older
     than the window does not (and ?days= brings it back). A doc with NEEDS_REVIEW segments
     and NO findings is ABSENT from the default view (the flood guard) and PRESENT under
     quality=needs_review with its own total. The section is absent for a translator.
 30b. F1 memory law (MED-1) — the quality projection excludes LQAResult.note; a fixture of
     >150 documents proves chunking; an UNRESTRICTED caller (visible_project_ids -> None)
     gets the full result, not an empty list (the .in_(None) trap) and not an enumerate.
 31. Scoping (HIGH) — a customer-bound user sees only its customer's work; a non-member
     under project_scoping only member/created; total matches the rows' scope.
 32. Identity refusals — GET /api/worklist with a shared connector token is a 400 with the
     named message, not an empty list; in-process `work list` without --actor refuses.
 33. Feature gate + cross-feature — worklist_enabled=0 removes /my-work + /api/worklist +
     the CLI group + the nav; queries_enabled=0 drops ONLY the query section (review +
     quality survive) and does NOT hide the worklist; the nav i18n key exists in all four.
 34. Index presence — the three §8.5 indexes exist on the running (create_all) build path.
"""

import os
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_worklist_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    LQAResult,
    Project,
    QueryStatus,
    Role,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    apikeys,
    security,
    settings,
    worklist,
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
    _set(security_auth_required=("1" if on else "0"))


def _queries(on):
    _set(queries_enabled=("1" if on else "0"))


def _worklist(on):
    _set(worklist_enabled=("1" if on else "0"))


def _customer(name=None, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name or "WC %d" % _uniq(), org_id=org_id)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _project(name=None, *, customer_id=None, org_id=None, created_by=""):
    with Session(engine) as s:
        p = Project(
            name=name or "WP %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            customer_id=customer_id,
            org_id=org_id,
            created_by=created_by,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _doc(pid, fmt="txt"):
    with Session(engine) as s:
        d = Document(project_id=pid, filename="f%d.%s" % (_uniq(), fmt), fmt=fmt)  # noqa: UP031
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _seg(did, locale="fr-FR", status=SegStatus.MT, order=0):
    with Session(engine) as s:
        sg = Segment(
            document_id=did,
            target_locale=locale,
            order_index=order,
            source_text="src %d" % _uniq(),  # noqa: UP031
            status=status,
        )
        s.add(sg)
        s.commit()
        s.refresh(sg)
        return sg.id


def _user(role, customer_id=None, email=None):
    email = email or ("u%d@x.com" % _uniq())  # noqa: UP031
    return security.create_user(email, "U", role, PW, customer_id=customer_id)


def _paused_run(
    pid,
    did,
    assignee_email,
    *,
    wf_id=None,
    step_id=None,
    run_status=RunStatus.PAUSED,
    step_status=RunStatus.PAUSED,
    locale="fr-FR",
):
    """A workflow + human_review step (assignee_email) + a run + its StepRun. Reuse
    (wf_id, step_id) to add a SECOND run of the SAME workflow (the §8.3 fixture)."""
    with Session(engine) as s:
        if wf_id is None:
            wf = Workflow(name="WF %d" % _uniq())  # noqa: UP031
            s.add(wf)
            s.commit()
            s.refresh(wf)
            wf_id = wf.id
            step = WorkflowStep(
                workflow_id=wf_id,
                order_index=0,
                name="Review",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email=assignee_email,
            )
            s.add(step)
            s.commit()
            s.refresh(step)
            step_id = step.id
        run = WorkflowRun(
            workflow_id=wf_id, document_id=did, target_locale=locale, status=run_status
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        sr = StepRun(
            workflow_run_id=run.id,
            step_id=step_id,
            status=step_status,
            started_at=datetime.utcnow(),
        )
        s.add(sr)
        s.commit()
        return run.id, wf_id, step_id


def _lqa(
    did,
    *,
    sev="major",
    locale="fr-FR",
    days_ago=1,
    withdrawn=False,
    seg_id=None,
    finding_source="llm",
    actor=None,
):
    with Session(engine) as s:
        r = LQAResult(
            document_id=did,
            segment_id=seg_id,
            dimension="accuracy",
            severity=sev,
            note="a finding note %d" % _uniq(),  # noqa: UP031
            target_locale=locale,
            finding_source=finding_source,
            actor=actor,
            created_at=datetime.utcnow() - timedelta(days=days_ago),
            withdrawn_at=(datetime.utcnow() if withdrawn else None),
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _work(user, **kw):
    with Session(engine) as s:
        return worklist.my_work(s, user, **kw)


def _refs(rows, section):
    return {r.ref_id for r in rows if r.section == section}


def _key(user):
    return apikeys.create_key(user.id, "wl", scopes="api docs")[1]


def _h(raw):
    return {"X-Loc-Token": raw}


def _web(email):
    """A TestClient logged in as `email` (auth ON) with a CSRF header, for rendering the
    dashboard. Login is a mutating POST, so an anon CSRF token is fetched first and
    refreshed after the session rotates (the verify_queries_surfaces pattern)."""
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code in (302, 303), "login for %s failed: %d" % (email, r.status_code)  # noqa: UP031
    csrf_for(c)
    return c


# ============================================================================
# 28. review section
# ============================================================================
def test_review_section():
    _auth(True)
    try:
        cid = _customer()
        me = _user(Role.LINGUIST, customer_id=cid, email="rev%d@x.com" % _uniq())  # noqa: UP031
        pid = _project(customer_id=cid)
        d1 = _doc(pid)
        r_me, wf, step = _paused_run(pid, d1, me.email)
        # (a) names someone else -> not mine
        d2 = _doc(pid)
        _paused_run(pid, d2, "someone-else@x.com")
        # (b) a COMPLETED run whose step names me -> not shown
        d3 = _doc(pid)
        _paused_run(
            pid, d3, me.email, run_status=RunStatus.COMPLETED, step_status=RunStatus.COMPLETED
        )
        # (c) a SECOND paused run of the SAME workflow -> BOTH appear (§8.3)
        d4 = _doc(pid)
        r_me2, _, _ = _paused_run(pid, d4, me.email, wf_id=wf, step_id=step)

        rows, _total = _work(me, sections=("review",))
        got = _refs(rows, "review")
        assert r_me in got and r_me2 in got, (
            "both same-workflow paused runs must appear (§8.3)",
            got,
        )
        # exactly my two; the other-assignee + completed runs are absent
        assert got == {r_me, r_me2}, ("review leaked a foreign/completed run", got)
        # the case-fold match holds
        me_upper = _user(Role.LINGUIST, customer_id=cid, email="Mix%d@x.com" % _uniq())  # noqa: UP031
        du = _doc(pid)
        ru, _, _ = _paused_run(pid, du, me_upper.email.upper())
        rows_u, _ = _work(me_upper, sections=("review",))
        assert ru in _refs(rows_u, "review"), "assignee match must be case-insensitive"
    finally:
        _auth(False)
    print(
        "  28. review: paused steps that name me (both same-workflow runs), not others, "
        "not completed, case-insensitive OK"
    )


# ============================================================================
# 29. query section
# ============================================================================
def test_query_section():
    _auth(True)
    _queries(True)
    try:
        cid = _customer()
        me = _user(Role.LINGUIST, customer_id=cid, email="q%d@x.com" % _uniq())  # noqa: UP031
        other = _user(Role.LINGUIST, customer_id=cid, email="oth%d@x.com" % _uniq())  # noqa: UP031
        pid = _project(customer_id=cid)
        did = _doc(pid)

        def _open(subject, assignee="", author="op@x.com", answered=False, closed=False, user=None):
            with Session(engine) as s:
                scope = qt.resolve_scope(s, document_id=did)
                t = qt.open_thread(
                    s,
                    user=user,
                    scope=scope,
                    subject=subject,
                    body="b",
                    assignee_email=assignee,
                    actor=author,
                    trusted_operator=(user is None),
                )
                if answered:
                    qt.set_status(
                        s,
                        user=None,
                        thread_id=t.id,
                        status=QueryStatus.ANSWERED,
                        trusted_operator=True,
                    )
                if closed:
                    qt.set_status(
                        s,
                        user=None,
                        thread_id=t.id,
                        status=QueryStatus.CLOSED,
                        trusted_operator=True,
                    )
                return t.id

        t_assigned_open = _open("assigned-open", assignee=me.email)
        t_assigned_ans = _open("assigned-answered", assignee=me.email, answered=True)
        t_mine_ans = _open("mine-answered", author=me.email, answered=True)
        t_assigned_closed = _open("assigned-closed", assignee=me.email, closed=True)
        t_mine_open = _open("mine-open-not-answered", author=me.email)  # not answered
        t_other = _open("someone-elses", assignee=other.email)

        rows, total = _work(me, sections=("query",))
        got = _refs(rows, "query")
        assert t_assigned_open in got and t_assigned_ans in got, (
            "assigned open/answered missing",
            got,
        )
        assert t_mine_ans in got, ("my own answered thread missing", got)
        assert t_assigned_closed not in got, "a closed thread must not appear"
        assert t_mine_open not in got, (
            "my OWN OPEN (not answered) thread must not appear in the query section",
            got,
        )
        assert t_other not in got, "a foreign-assignee thread must not appear"
        # reason labels
        by_ref = {r.ref_id: r for r in rows if r.section == "query"}
        assert by_ref[t_assigned_open].reason == "assigned"
        assert by_ref[t_mine_ans].reason == "answered"

        # cross-scope: a thread in a project I cannot see is absent AND uncounted.
        cid2 = _customer()
        pid2 = _project(customer_id=cid2)
        did2 = _doc(pid2)
        with Session(engine) as s:
            scope2 = qt.resolve_scope(s, document_id=did2)
            # The assign path now REFUSES a black-hole assignment (#67: `me` cannot see
            # this foreign customer-2 project), so stamp the assignee_email DIRECTLY on the
            # row to forge the exact precondition this test defends against — a persisted
            # foreign assignment the worklist READ side must still filter (defense in depth,
            # e.g. a pre-#67 row or a customer reassignment after the fact).
            foreign = qt.open_thread(
                s,
                user=None,
                scope=scope2,
                subject="foreign",
                body="b",
                actor="op@x.com",
                trusted_operator=True,
            )
            foreign.assignee_email = me.email
            s.add(foreign)
            s.commit()
        rows2, total2 = _work(me, sections=("query",))
        assert all(r.project_id == pid for r in rows2), (
            "query section leaked a foreign project",
            [r.project_id for r in rows2],
        )
        assert total2 == total, ("a foreign thread changed the scoped total", total, total2)
    finally:
        _queries(True)
        _auth(False)
    print(
        "  29. query: assigned open/answered + my answered (reason right), not closed, "
        "not my-own-open, foreign project absent AND uncounted OK"
    )


# ============================================================================
# 30 / 30b. quality section (re-keyed) + F1 memory law
# ============================================================================
def test_quality_section():
    _auth(True)
    try:
        cid = _customer()
        me = _user(Role.LINGUIST, customer_id=cid, email="qa%d@x.com" % _uniq())  # noqa: UP031
        pid = _project(customer_id=cid)

        d_major = _doc(pid)
        _lqa(d_major, sev="major", days_ago=1)
        d_crit = _doc(pid)
        _lqa(d_crit, sev="critical", days_ago=1)
        d_withdrawn = _doc(pid)
        _lqa(d_withdrawn, sev="major", days_ago=1, withdrawn=True)
        d_minor = _doc(pid)
        _lqa(d_minor, sev="minor", days_ago=1)
        _lqa(d_minor, sev="neutral", days_ago=1)
        d_old = _doc(pid)
        _lqa(d_old, sev="critical", days_ago=200)

        rows, _ = _work(me, sections=("quality",), days=90)
        got = _refs(rows, "quality")
        assert d_major in got and d_crit in got, ("live major/critical missing", got)
        assert d_withdrawn not in got, "a withdrawn-only doc must not appear"
        assert d_minor not in got, "a minor/neutral-only doc must not appear"
        assert d_old not in got, "a finding older than the window must not appear"
        # ?days= brings the old one back
        rows_w, _ = _work(me, sections=("quality",), days=365)
        assert d_old in _refs(rows_w, "quality"), "widening --days did not surface the old doc"

        # the reason names the severities
        by_ref = {r.ref_id: r for r in rows if r.section == "quality"}
        assert "critical" in by_ref[d_crit].reason, by_ref[d_crit].reason

        # NEEDS_REVIEW flood guard: a doc with NEEDS_REVIEW segments + NO findings is
        # ABSENT from the default view, PRESENT under quality=needs_review.
        d_nr = _doc(pid)
        _seg(d_nr, status=SegStatus.NEEDS_REVIEW)
        _seg(d_nr, status=SegStatus.NEEDS_REVIEW, order=1)
        rows_def, _ = _work(me, sections=("quality",), days=90)
        assert d_nr not in _refs(rows_def, "quality"), (
            "NEEDS_REVIEW-only doc leaked into the default quality view (the flood)"
        )
        rows_nr, _total_nr = _work(me, sections=("quality",), quality="needs_review")
        got_nr = _refs(rows_nr, "quality")
        assert d_nr in got_nr, "needs_review filter did not surface the NEEDS_REVIEW doc"
        # and the default findings are NOT in the needs_review list (it SWAPS the section)
        assert d_major not in got_nr, "needs_review list should not carry finding docs"
        nr_row = next(r for r in rows_nr if r.ref_id == d_nr)
        assert "need review" in nr_row.reason, nr_row.reason

        # the section is ABSENT for a translator (rank < reviewer)
        tr = _user(Role.TRANSLATOR, customer_id=cid, email="tr%d@x.com" % _uniq())  # noqa: UP031
        rows_tr, _ = _work(tr, sections=("quality",), days=90)
        assert not _refs(rows_tr, "quality"), "a translator must not see the quality section"
    finally:
        _auth(False)
    print(
        "  30. quality: live major/critical in window; withdrawn/minor/old excluded; "
        "NEEDS_REVIEW off-by-default flood guard; absent for a translator OK"
    )


def test_quality_f1_memory_law():
    # (a) projection excludes LQAResult.note — a query-shape assertion on the actual
    # projection tuple (the reports.py idiom), not the source text.
    colnames = {c.name for c in worklist._QUALITY_COLS}
    assert "note" not in colnames, (
        f"the quality projection must never load LQAResult.note (F1): {colnames!r}"
    )
    assert {"document_id", "severity", "target_locale", "created_at"} <= colnames, colnames

    # (b) chunking over >CHUNK documents (150) works — no unbounded IN list, full result.
    _auth(True)
    try:
        cid = _customer()
        me = _user(Role.LINGUIST, customer_id=cid, email="f1%d@x.com" % _uniq())  # noqa: UP031
        pid = _project(customer_id=cid)
        made = set()
        for _ in range(worklist.CHUNK + 12):  # 162 docs, each with one major finding
            d = _doc(pid)
            _lqa(d, sev="major", days_ago=1)
            made.add(d)
        rows, total = _work(me, sections=("quality",), days=90, limit=50, offset=0)
        # the confined set spans >1 chunk; total must count every one, page is bounded.
        assert total >= len(made), "chunking dropped rows: total=%d < made=%d" % (total, len(made))  # noqa: UP031
        assert len(rows) <= 50, "page not bounded"

        # (c) the UNRESTRICTED (visible_project_ids -> None) arm returns the full result,
        # NOT an empty list (the .in_(None) trap) and NOT an enumerate. An internal admin
        # under project_scoping off is unrestricted.
        _set(project_scoping="0", multi_org_enabled="0")
        admin = _user(Role.ADMIN, email="adm%d@x.com" % _uniq())  # noqa: UP031
        from app.services import access

        with Session(engine) as s:
            assert access.visible_project_ids(s, admin) is None, (
                "fixture: admin must be unrestricted (None)"
            )
        rows_a, total_a = _work(admin, sections=("quality",), days=90)
        assert total_a >= len(made), (
            "the None-unrestricted arm returned an empty/"  # noqa: UP031
            "under-count (the .in_(None) trap): %d" % total_a
        )
        assert made.issubset(_refs(rows_a, "quality")) or total_a >= len(made)
    finally:
        _auth(False)
    print(
        "  30b. F1: projection excludes note; >150-doc chunking; the None-unrestricted "
        "arm returns the full result (not the .in_(None) empty trap) OK"
    )


# ============================================================================
# 31. scoping (HIGH)
# ============================================================================
def test_scoping():
    _auth(True)
    try:
        # customer-bound: only that customer's work
        c1 = _customer()
        c2 = _customer()
        u1 = _user(Role.LINGUIST, customer_id=c1, email="s1%d@x.com" % _uniq())  # noqa: UP031
        p1 = _project(customer_id=c1)
        p2 = _project(customer_id=c2)
        d1 = _doc(p1)
        d2 = _doc(p2)
        _lqa(d1, sev="major", days_ago=1)
        _lqa(d2, sev="major", days_ago=1)  # another customer's finding
        r1, _, _ = _paused_run(p1, d1, u1.email)
        _paused_run(p2, d2, u1.email)  # names u1 but in c2's project
        rows, total = _work(u1, days=90)
        pids = {r.project_id for r in rows}
        assert pids <= {p1}, f"customer-bound worklist leaked another customer: {pids!r}"
        assert total == len(rows), "total must match the scoped rows on a single page"
        assert r1 in _refs(rows, "review"), "own-customer review item missing"

        # non-member under project_scoping: only member/created projects
        _set(project_scoping="1")
        owner = _user(Role.VIEWER, email="own%d@x.com" % _uniq())  # noqa: UP031
        nonmember = _user(Role.VIEWER, email="nm%d@x.com" % _uniq())  # noqa: UP031
        p_own = _project(created_by=owner.email)
        d_own = _doc(p_own)
        _lqa(d_own, sev="critical", days_ago=1)
        # owner is a viewer -> can't see the quality section, but the review section is
        # role-free; give them a review item to prove membership scoping there.
        r_own, _, _ = _paused_run(p_own, d_own, owner.email)
        rows_own, _ = _work(owner, sections=("review",))
        assert r_own in _refs(rows_own, "review"), "owner cannot see own project's work"
        rows_nm, _total_nm = _work(nonmember, sections=("review",))
        assert r_own not in _refs(rows_nm, "review"), "non-member saw a foreign review item"
        _set(project_scoping="0")
    finally:
        _auth(False)
    print(
        "  31. scoping: customer-bound sees only its customer; non-member under "
        "project_scoping only member/created; total matches scope OK"
    )


# ============================================================================
# 32. identity refusals
# ============================================================================
def test_identity_refusals():
    init_db()
    _auth(True)
    _worklist(True)
    try:
        # (a) /api/worklist with a shared CONNECTOR token -> 400 with the named message.
        from app.web import deps

        tok = deps.connector_api_token()  # ensures one is generated + stored
        r = TestClient(app).get("/api/worklist", headers=_h(tok))
        assert r.status_code == 400, "connector token must be a 400, got %d (%s)" % (  # noqa: UP031
            r.status_code,
            r.text,
        )
        assert "per-user API key" in r.json().get("error", ""), r.text

        # positive control: a per-user key works (200).
        u = _user(Role.LINGUIST, email="idk%d@x.com" % _uniq())  # noqa: UP031
        r2 = TestClient(app).get("/api/worklist", headers=_h(_key(u)))
        assert r2.status_code == 200 and r2.json().get("ok") is True, r2.text

        # (b) in-process `work list` without --actor -> refusal (not empty).
        from app.cli import cmd_work
        from app.cli._common import EXIT_USAGE, CliError

        class _Emit:
            as_json = False
            quiet = False

            def json(self, *a, **k):
                pass

            def table(self, *a, **k):
                pass

            def say(self, *a, **k):
                pass

        args = types.SimpleNamespace(
            actor="cli",
            remote=None,
            days=90,
            quality="",
            limit=50,
            offset=0,
            org=None,
            operator=False,
        )
        try:
            cmd_work._work_list(args, _Emit())
            raise AssertionError("work list ran without --actor")
        except CliError as exc:
            assert exc.code == EXIT_USAGE, exc.code
            assert "--actor" in str(exc) and "--remote" in str(exc), str(exc)

        # positive control: with --actor it runs (exit 0).
        args.actor = u.email
        assert cmd_work._work_list(args, _Emit()) == 0
    finally:
        _auth(False)
    print(
        "  32. identity: /api/worklist connector token -> named 400; work list without "
        "--actor refuses (usage); per-user key + --actor work OK"
    )


# ============================================================================
# 33. feature gate + cross-feature isolation + nav i18n
# ============================================================================
def test_feature_gate():
    init_db()
    _auth(False)
    _worklist(True)
    _queries(True)
    cid = _customer()
    me = _user(Role.LINGUIST, customer_id=cid, email="fg%d@x.com" % _uniq())  # noqa: UP031
    pid = _project(customer_id=cid)
    did = _doc(pid)
    _lqa(did, sev="major", days_ago=1)
    r_rev, _, _ = _paused_run(pid, did, me.email)
    with Session(engine) as s:
        scope = qt.resolve_scope(s, document_id=did)
        tid = qt.open_thread(
            s,
            user=None,
            scope=scope,
            subject="q",
            body="b",
            assignee_email=me.email,
            actor="op@x.com",
            trusted_operator=True,
        ).id

    # queries OFF: the query section DISAPPEARS; review + quality SURVIVE (§5.2).
    _queries(False)
    rows_qoff, _ = _work(me, days=90)
    assert not _refs(rows_qoff, "query"), "query section survived queries_enabled=0"
    assert r_rev in _refs(rows_qoff, "review"), "queries off hid the review section"
    assert did in _refs(rows_qoff, "quality"), "queries off hid the quality section"
    _queries(True)
    # back on: the query item returns
    rows_qon, _ = _work(me, days=90)
    assert tid in _refs(rows_qon, "query"), "query section did not return on re-enable"

    # LOW-2: the dashboard "N queries assigned to you" line (§8.4/RV-1) is double-gated on
    # queries_enabled — proven at the data seam AND on the rendered page, non-vacuously.
    _auth(True)
    with Session(engine) as s:
        assert worklist.my_open_query_count(s, me) >= 1, "fixture: me has an assigned thread"
    c = _web(me.email)
    assert "queries assigned to you" in c.get("/").text, (
        "dashboard queries line missing while queries_enabled=1"
    )
    _queries(False)
    assert "queries assigned to you" not in c.get("/").text, (
        "dashboard queries line survived queries_enabled=0"
    )
    with Session(engine) as s:
        assert worklist.my_open_query_count(s, me) == 0, (
            "my_open_query_count survived queries_enabled=0"
        )
    _queries(True)
    _auth(False)

    # worklist OFF: /my-work + /api/worklist 404; nav absent; CLI refuses.
    _worklist(False)
    for auth in (False, True):
        _set(security_auth_required=("1" if auth else "0"))
        rweb = TestClient(app).get("/my-work", follow_redirects=False)
        assert rweb.status_code == 404, "/my-work off/auth=%s -> %d" % (auth, rweb.status_code)  # noqa: UP031
    _auth(False)
    u = _user(Role.LINGUIST, email="fgk%d@x.com" % _uniq())  # noqa: UP031
    rapi = TestClient(app).get("/api/worklist", headers=_h(_key(u)))
    assert rapi.status_code == 404 and rapi.json().get("ok") is False, rapi.text
    from app.cli import cmd_work
    from app.cli._common import CliError

    try:
        cmd_work._flag_gate()
        raise AssertionError("cmd_work._flag_gate did not refuse while off")
    except CliError as exc:
        assert str(exc) == ("the reviewer worklist is disabled (setting worklist_enabled=0)."), str(
            exc
        )
    # nav absent + card 404
    from app import features

    assert features.card_is_off("worklist") is True
    home = TestClient(app).get("/").text
    assert 'href="/my-work"' not in home, "nav /my-work present while off"
    # worklist off does NOT hide queries (cross-feature)
    _queries(True)
    assert 'href="/queries"' in TestClient(app).get("/").text, "worklist off hid queries nav"
    _worklist(True)
    assert 'href="/my-work"' in TestClient(app).get("/").text, "nav did not return on re-enable"

    # nav i18n key present in all four catalogues and non-en differs from en
    import json

    vals = {}
    for loc in ("en", "de", "es", "fr"):
        with open(
            os.path.join(_ROOT, "app", "i18n", f"{loc}.json"),
            encoding="utf-8",  # noqa: RUF100, UP032
        ) as fh:
            vals[loc] = json.load(fh)["nav"]["my_work"]
    for loc in ("de", "es", "fr"):
        assert vals[loc] and vals[loc] != vals["en"], f"nav.my_work not localized for {loc}"
    print(
        "  33. gate: queries off drops ONLY the query section (review+quality survive); "
        "worklist off 404s web+api+nav+card, CLI refuses, queries unaffected; i18n OK"
    )


# ============================================================================
# 34. index presence (§8.5)
# ============================================================================
def test_indexes():
    init_db()
    insp = sa_inspect(engine)
    want = {
        "workflowstep": "ix_workflowstep_assignee_email",
        "steprun": "ix_steprun_status",
        "document": "ix_document_project_id",
    }
    # accept either the exact declared name OR any index that covers the single column
    # (SQLModel/alembic may name it differently on the create_all path).
    cols = {"workflowstep": "assignee_email", "steprun": "status", "document": "project_id"}
    for table, _name in want.items():
        idxs = insp.get_indexes(table)
        covered = any(ix.get("column_names") == [cols[table]] for ix in idxs)
        assert covered, "no single-column index on {}.{} (§8.5): {!r}".format(
            table,
            cols[table],
            [ix.get("column_names") for ix in idxs],
        )
    # and the model fields declare index=True on the mapped table (the zero-diff alembic
    # guarantee — the same index exists on both the create_all and migration paths).
    assert _field_indexed(WorkflowStep, "assignee_email"), "WorkflowStep.assignee_email index"
    assert _field_indexed(StepRun, "status"), "StepRun.status index"
    assert _field_indexed(Document, "project_id"), "Document.project_id index"
    print(
        "  34. indexes: workflowstep.assignee_email, steprun.status, document.project_id "
        "present on the build path OK"
    )


def _field_indexed(model, name) -> bool:
    """True if the column is indexed on the mapped table (robust to how index=True was
    declared)."""
    col = model.__table__.columns.get(name)
    if col is None:
        return False
    if col.index:
        return True
    return any(name in [c.name for c in ix.columns] for ix in model.__table__.indexes)


def main():
    init_db()
    test_review_section()
    test_query_section()
    test_quality_section()
    test_quality_f1_memory_law()
    test_scoping()
    test_identity_refusals()
    test_feature_gate()
    test_indexes()
    print("WORKLIST OK")


if __name__ == "__main__":
    main()
