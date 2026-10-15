"""Content routing (MCP Phase B) — resolver + admin CRUD (offline).

Chunk 1 coverage:
  - resolver: matching by ext + priority (lower priority wins); department scoping
    (a dept-scoped rule only matches its department); explicit caller project_id
    wins over any rule; fallback to the (per-actor) inbox when nothing matches
  - validate_rule: a rule targeting a human_review workflow is rejected at save
  - admin CRUD: /routing create/toggle/delete (admin-gated, CSRF, audited)
  - end-to-end: an SRT and a JSON document route to DIFFERENT projects via rules

Chunk 2 extends this suite (User.department wiring + department inbox projects).

Phase-B-review (Finding 1) adds END-TO-END HTTP-API tests proving rules are honored
on the real submit paths (not just resolve()): an upload with no project_id lands in
a matching rule's project; a translate honors a rule's provider override (job uses
it); a rule -> keyless provider still 409s + enqueues nothing; no rules -> the
department-inbox fallback. Plus Finding 6 (department case is canonical).

ASCII-only output.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_routing_")

FIXTURE_PW = "pw-fixture-4417"  # pregate: allow-secret test fixture

import time
from io import BytesIO

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Job,
    JobStatus,
    Project,
    RoutingRule,
    Segment,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import apikeys, jobs, routing, security, settings
from app.services.providers import Provider, register_provider
from tests._csrf import csrf_for, sign_out


class _NamedStub(Provider):
    """A 'configured' engine (so provider_is_live() is True, no key needed) that
    tags its output with its own provider name, so a job's result.provider_used
    reveals which engine actually ran."""

    def __init__(self, name):
        self.name = name

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{self.name}:{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def _dx(paras):
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _docs_headers(email, password=FIXTURE_PW, role=None):
    """Create (if needed) a user with a docs-scoped API key; return the header."""
    from app.models import Role

    u = security.get_by_email(email)
    if u is None:
        u = security.create_user(email, email.split("@")[0], role or Role.TRANSLATOR, password)
    _k, tok = apikeys.create_key(u.id, "docs", scopes="api docs")
    return {"X-Loc-Token": tok}, u


def _wait_job(jid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = jobs.get(jid)
        if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            return j
        time.sleep(0.05)
    return jobs.get(jid)


def _reset_rules() -> None:
    with Session(engine) as s:
        for r in s.exec(select(RoutingRule)).all():
            s.delete(r)
        s.commit()


def test_resolver_priority_and_ext() -> None:
    _reset_rules()
    with Session(engine) as s:
        srt_proj = Project(name="Subtitles", created_by="admin")
        json_proj = Project(name="Strings", created_by="admin")
        s.add(srt_proj)
        s.add(json_proj)
        s.commit()
        s.refresh(srt_proj)
        s.refresh(json_proj)
        # Two rules: a specific srt rule (priority 10) and a catch-all (priority 100).
        s.add(RoutingRule(priority=10, match_ext="srt,vtt", project_id=srt_proj.id))
        s.add(RoutingRule(priority=100, match_ext="", project_id=json_proj.id))
        s.commit()

        d_srt = routing.resolve(s, ext="srt", actor="admin", ensure_inbox=False)
        assert d_srt.project_id == srt_proj.id, d_srt
        d_json = routing.resolve(s, ext="json", actor="admin", ensure_inbox=False)
        assert d_json.project_id == json_proj.id, d_json
        # dot / case insensitive ext match
        d_vtt = routing.resolve(s, ext=".VTT", actor="admin", ensure_inbox=False)
        assert d_vtt.project_id == srt_proj.id, d_vtt
    print("  resolver: ext+priority - srt->Subtitles, json->catch-all, .VTT case/dot-insensitive")


def test_resolver_department_scoping() -> None:
    _reset_rules()
    with Session(engine) as s:
        legal = Project(name="Legal project", created_by="admin")
        general = Project(name="General project", created_by="admin")
        s.add(legal)
        s.add(general)
        s.commit()
        s.refresh(legal)
        s.refresh(general)
        # A department-scoped rule + a catch-all. The scoped one has lower priority
        # but only matches its department.
        s.add(RoutingRule(priority=10, match_ext="docx", department="legal", project_id=legal.id))
        s.add(RoutingRule(priority=100, match_ext="docx", project_id=general.id))
        s.commit()

        d_legal = routing.resolve(s, ext="docx", department="legal", actor="a", ensure_inbox=False)
        assert d_legal.project_id == legal.id, d_legal
        # Same ext, different department -> only the catch-all matches.
        d_other = routing.resolve(
            s, ext="docx", department="marketing", actor="a", ensure_inbox=False
        )
        assert d_other.project_id == general.id, d_other
        # No department -> catch-all (dept-scoped rule requires a department).
        d_none = routing.resolve(s, ext="docx", department="", actor="a", ensure_inbox=False)
        assert d_none.project_id == general.id, d_none
    print("  resolver: department scoping - legal->Legal, others->catch-all")


def test_resolver_explicit_override_wins() -> None:
    _reset_rules()
    with Session(engine) as s:
        ruled = Project(name="Ruled", created_by="admin")
        chosen = Project(name="Chosen", created_by="admin")
        s.add(ruled)
        s.add(chosen)
        s.commit()
        s.refresh(ruled)
        s.refresh(chosen)
        s.add(RoutingRule(priority=10, match_ext="srt", project_id=ruled.id))
        s.commit()
        # Even though a rule matches srt->Ruled, an explicit caller project wins.
        d = routing.resolve(
            s, ext="srt", actor="admin", explicit_project_id=chosen.id, ensure_inbox=False
        )
        assert d.project_id == chosen.id, d
        assert "explicit" in d.reason, d.reason
    print("  resolver: explicit caller project_id wins over a matching rule")


def test_resolver_fallback_to_inbox() -> None:
    _reset_rules()
    with Session(engine) as s:
        # No rules at all -> fallback to the actor's inbox project (created here).
        d = routing.resolve(s, ext="txt", actor="nobody@example.com", ensure_inbox=True)
        assert d.project_id is not None, d
        proj = s.get(Project, d.project_id)
        assert proj is not None and "Inbox" in proj.name, proj.name if proj else None
        assert "inbox" in d.reason.lower(), d.reason
        # A second resolve for the same actor reuses the SAME inbox (idempotent).
        d2 = routing.resolve(s, ext="md", actor="nobody@example.com", ensure_inbox=True)
        assert d2.project_id == d.project_id, (d, d2)
    print("  resolver: no rule -> department/actor inbox (idempotent)")


def test_validate_human_review_rejected() -> None:
    with Session(engine) as s:
        wf = Workflow(name="With review")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, step_type=StepType.LLM_AGENT))
        s.add(WorkflowStep(workflow_id=wf.id, order_index=1, step_type=StepType.HUMAN_REVIEW))
        s.commit()

        wf_ok = Workflow(name="Automated only")
        s.add(wf_ok)
        s.commit()
        s.refresh(wf_ok)
        s.add(WorkflowStep(workflow_id=wf_ok.id, order_index=0, step_type=StepType.LLM_AGENT))
        s.commit()

        err = routing.validate_rule(s, workflow_id=wf.id)
        assert err and "human-review" in err.lower(), err
        assert routing.validate_rule(s, workflow_id=wf_ok.id) is None
        assert routing.validate_rule(s, workflow_id=None) is None
    print("  validate_rule: human-review workflow rejected; automated-only accepted")


def test_admin_crud() -> None:
    _reset_rules()
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)  # login POST carries the session CSRF token
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        csrf_for(c)  # session rotated on login -> refresh the token
        # Create a rule via the admin form.
        with Session(engine) as s:
            proj = s.exec(select(Project).where(Project.name == "Subtitles")).first()
        r = c.post(
            "/routing",
            data={
                "priority": "5",
                "match_ext": "srt",
                "department": "",
                "project_id": str(proj.id),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text)
        with Session(engine) as s:
            rule = s.exec(
                select(RoutingRule).where(RoutingRule.match_ext == "srt", RoutingRule.priority == 5)
            ).first()
            assert rule is not None and rule.active, rule
            rid = rule.id
        # Toggle it inactive.
        r = c.post(f"/routing/{rid}/toggle", follow_redirects=False)
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert s.get(RoutingRule, rid).active is False
        # Delete it.
        r = c.post(f"/routing/{rid}/delete", follow_redirects=False)
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert s.get(RoutingRule, rid) is None
        # A non-admin cannot reach /routing.
        sign_out(c)
        csrf_for(c)
        c.post("/auth/login", data={"email": "user1", "password": "user1"})
        csrf_for(c)
        r = c.get("/routing", follow_redirects=False)
        assert r.status_code == 403, r.status_code
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  admin CRUD: create/toggle/delete (303, audited); non-admin -> 403")


def test_admin_rejects_human_review_workflow() -> None:
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)  # login POST carries the session CSRF token
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        csrf_for(c)  # session rotated on login -> refresh the token
        with Session(engine) as s:
            wf = s.exec(select(Workflow).where(Workflow.name == "With review")).first()
        n_before = _rule_count()
        r = c.post(
            "/routing",
            data={"priority": "1", "match_ext": "pdf", "workflow_id": str(wf.id)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
        assert _rule_count() == n_before, "human-review rule must NOT be created"
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  admin CRUD: rule targeting a human-review workflow rejected (no row)")


def _rule_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(RoutingRule.id)).all())


def test_rule_missing_fk_clean() -> None:
    """#26: a rule naming a nonexistent workflow/project/language-set is rejected with
    a clean, specific message BEFORE the INSERT — no raw FK IntegrityError (SQL leak)
    on either surface. validate_rule reports the first missing id; create_rule is a
    fail-closed backstop that raises RoutingRuleError (never reaches the DB)."""
    with Session(engine) as s:
        assert routing.validate_rule(s, workflow_id=999999) == "no such workflow: 999999"
        assert (
            routing.validate_rule(s, workflow_id=None, project_id=888888)
            == "no such project: 888888"
        )
        assert (
            routing.validate_rule(s, workflow_id=None, language_set_id=777777)
            == "no such language set: 777777"
        )
        n_before = _rule_count()
        try:
            routing.create_rule(s, match_ext="docx", workflow_id=999999, actor="t")
            assert False, "create_rule must fail closed on a bad FK, not INSERT it"
        except routing.RoutingRuleError as e:
            assert "no such workflow: 999999" in str(e), str(e)
        assert _rule_count() == n_before, "no row may be written on a bad-FK rule"

    # Web parity: the admin form returns a clean ?error= redirect (303), not a 500
    # from an uncaught IntegrityError (pre-fix, TestClient re-raised the exception).
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        csrf_for(c)
        n_before = _rule_count()
        r = c.post(
            "/routing",
            data={"priority": "5", "match_ext": "docx", "workflow_id": "999999"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "error=" in r.headers["location"], (
            r.status_code,
            r.headers,
        )
        assert _rule_count() == n_before, "bad-FK rule must NOT be created via the form"
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  #26: bad workflow/project/language-set id -> clean error, no SQL leak, no row")


def _run_cli(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as _cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = _cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def test_rule_show_readback() -> None:
    """`admin routing rule show` reads back a single rule's full config (human +
    --json), single-sourced from routing.rule_to_dict (the same seam /api uses). A
    missing id is a clean 'no such rule' (exit 2), never a crash."""
    _reset_rules()
    with Session(engine) as s:
        proj = s.exec(select(Project).where(Project.name == "Subtitles")).first()
        rule = routing.create_rule(
            s,
            priority=7,
            match_ext="srt,vtt",
            match_subject="subs@x",
            department="Media",
            project_id=proj.id,
            provider="engine:1",
            actor="t",
        )
        rid = rule.id
    # human read-back names every stored field
    code, out, err = _run_cli(["admin", "routing", "rule", "show", str(rid)])
    assert code == 0, (out, err)
    assert f"Rule #{rid}" in out and "srt,vtt" in out and "Media" in out, out
    assert "subs@x" in out and "engine:1" in out, out
    # --json read-back: the stable rule_to_dict shape (superset of `rule list`)
    code, jout, _ = _run_cli(["--json", "admin", "routing", "rule", "show", str(rid)])
    assert code == 0, jout
    import json as _json

    d = _json.loads(jout.strip().splitlines()[-1])
    assert d["ok"] and d["id"] == rid, d
    for k in (
        "priority",
        "match_ext",
        "match_subject",
        "department",
        "project_id",
        "workflow_id",
        "language_set_id",
        "provider",
        "active",
        "created_by",
        "created_at",
    ):
        assert k in d, (f"rule show --json missing {k!r}", d)
    assert d["match_ext"] == "srt,vtt" and d["match_subject"] == "subs@x", d
    # missing id -> clean CliError (exit 2), no traceback
    code, out, err = _run_cli(["admin", "routing", "rule", "show", "999999"])
    assert code == 2, (code, out, err)
    assert "no such rule: 999999" in (out + err), (out, err)
    print("  rule show: human + --json read-back (rule_to_dict); missing id -> exit 2")


def test_routing_budget_meters() -> None:
    """CV-5: /routing renders the global AI-spend meter + a per-department
    accessible token-usage bar (role="img" + aria-label) next to each budget row.
    Web-only rendering of data already on /api/budgets + the spend ledger (no new
    state). An unlimited (limit 0) department shows 'unlimited' with no bar."""
    from app.services import budgets as _budgets

    with Session(engine) as s:
        _budgets.upsert_budget(s, "cv5-capped", 500000)
        _budgets.upsert_budget(s, "cv5-open", 0)
        s.commit()
    c = TestClient(app)  # auth off in main() -> _admin_block is a no-op
    r = c.get("/routing")
    assert r.status_code == 200, r.status_code
    t = r.text
    # global spend meter present (same figures as Setup AI's spend-cap card)
    assert "AI translation spend" in t, "global spend meter missing on /routing"
    # per-department accessible usage bar: the capped dept gets a role="img" bar
    # whose aria-label names its department + the used/limit/percent; the open
    # dept renders 'unlimited' text, no bar.
    assert 'class="usage-bar"' in t and 'role="img"' in t, "usage bar not rendered"
    assert "cv5-capped budget:" in t and "of 500,000 tokens used" in t, (
        "the usage bar's aria-label must state department + used/limit"
    )
    # the unlimited department row shows 'unlimited', not a fake full/empty bar
    assert "unlimited" in t
    print(
        "  CV-5 meters: /routing shows the global spend meter + a per-department "
        "accessible (role=img/aria-label) token-usage bar; unlimited depts show text"
    )


def test_end_to_end_srt_vs_json() -> None:
    _reset_rules()
    with Session(engine) as s:
        subs = Project(name="E2E Subs", created_by="admin")
        strings = Project(name="E2E Strings", created_by="admin")
        s.add(subs)
        s.add(strings)
        s.commit()
        s.refresh(subs)
        s.refresh(strings)
        s.add(RoutingRule(priority=10, match_ext="srt", project_id=subs.id))
        s.add(RoutingRule(priority=20, match_ext="json", project_id=strings.id))
        s.commit()
        d_srt = routing.resolve(s, ext="srt", actor="admin", ensure_inbox=False)
        d_json = routing.resolve(s, ext="json", actor="admin", ensure_inbox=False)
        assert d_srt.project_id == subs.id, d_srt
        assert d_json.project_id == strings.id, d_json
        assert d_srt.project_id != d_json.project_id
    print("  end-to-end: SRT and JSON route to DIFFERENT projects")


# ---------------------------------------------------------------------------
# Chunk 2: User.department identity + department inbox projects
# ---------------------------------------------------------------------------
def test_department_for_reads_user_column() -> None:
    """department_for(ident) returns the User.department; blank/absent -> ''."""
    from app.models import User

    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "admin")).first()
        u.department = "legal"
        s.add(u)
        s.commit()
        s.refresh(u)
        assert routing.department_for({"kind": "user", "user": u}) == "legal"
    # connector token / auth-off -> no department
    assert routing.department_for({"kind": "connector"}) == ""
    assert routing.department_for(None) == ""
    print("  department_for: reads User.department; connector/None -> ''")


def test_department_inbox_shared_per_department() -> None:
    """ensure_department_inbox: ONE inbox per department (shared across actors),
    but one per actor when there is no department."""
    _reset_rules()
    with Session(engine) as s:
        i1 = routing.ensure_department_inbox(s, "legal", "alice@example.com")
        i2 = routing.ensure_department_inbox(s, "legal", "bob@example.com")
        assert i1.id == i2.id, "same department must share ONE inbox regardless of actor"
        assert "legal" in i1.name.lower(), i1.name
        # different department -> different inbox
        i3 = routing.ensure_department_inbox(s, "marketing", "alice@example.com")
        assert i3.id != i1.id, "different department must get its own inbox"
        # no department -> per-actor inbox (pre-Phase-B behaviour)
        a = routing.ensure_department_inbox(s, "", "carol@example.com")
        b = routing.ensure_department_inbox(s, "", "dave@example.com")
        assert a.id != b.id, "no-department inbox is per actor"
        assert "MCP Inbox" in a.name, a.name
    print("  inbox: one shared inbox per department; per-actor when no department")


def test_resolver_department_fallback_inbox() -> None:
    """With no matching rule, resolve() lands in the actor's DEPARTMENT inbox
    (shared) — two actors in the same department resolve to the same project."""
    _reset_rules()
    with Session(engine) as s:
        d1 = routing.resolve(
            s, ext="txt", department="support", actor="p1@example.com", ensure_inbox=True
        )
        d2 = routing.resolve(
            s, ext="pdf", department="support", actor="p2@example.com", ensure_inbox=True
        )
        assert d1.project_id == d2.project_id, "same department -> same inbox project"
        proj = s.get(Project, d1.project_id)
        assert "support" in proj.name.lower(), proj.name
    print("  resolver: department fallback -> shared department inbox")


def test_department_case_canonical() -> None:
    """Finding 6: "Sales" and "sales" are ONE department — they share one inbox and
    one budget. Department is stored lower-cased; inbox identity + budget lookup are
    case-insensitive, so mixed-case never forks a second department."""
    from app.models import DepartmentBudget
    from app.services import budgets

    _reset_rules()
    with Session(engine) as s:
        # inbox identity: mixed-case department strings collapse to one inbox
        up = routing.ensure_department_inbox(s, "Sales", "up@example.com")
        lo = routing.ensure_department_inbox(s, "sales", "lo@example.com")
        assert up.id == lo.id, ("mixed-case department must share one inbox", up.name, lo.name)
        # budget lookup is case-insensitive: a row stored one case is found by another
        s.add(DepartmentBudget(department="Sales", monthly_token_limit=1000))
        s.commit()
        assert budgets.get_budget(s, "sales") is not None, "budget must resolve case-insensitively"
        assert budgets.get_budget(s, "SALES") is not None
    print("  case: 'Sales'/'sales' share one inbox + one budget (canonical department)")


def test_admin_edit_department() -> None:
    """Admin can set a user's department via the users UI; it is audited.
    Stored lower-cased (Finding 6 canonical form) even when typed mixed-case."""
    from app.models import User

    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        csrf_for(c)
        with Session(engine) as s:
            u1 = s.exec(select(User).where(User.email == "user1")).first()
            uid = u1.id
        r = c.post(
            f"/users/{uid}/department", data={"department": "Finance"}, follow_redirects=False
        )
        assert r.status_code == 303, (r.status_code, r.text)
        with Session(engine) as s:
            assert s.get(User, uid).department == "finance", "department must be stored lower-cased"
        # non-admin cannot edit
        sign_out(c)
        csrf_for(c)
        c.post("/auth/login", data={"email": "user1", "password": "user1"})
        csrf_for(c)
        r = c.post(
            f"/users/{uid}/department", data={"department": "hacked"}, follow_redirects=False
        )
        assert r.status_code == 403, r.status_code
        with Session(engine) as s:
            assert s.get(User, uid).department == "finance", "non-admin must not change it"
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  admin: set user department (303, audited); non-admin -> 403")


def test_gdpr_export_includes_department() -> None:
    from app.models import User
    from app.services import gdpr

    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "user1")).first()
        uid = u.id
    dept = None
    for section, rec in gdpr.export_user(uid):
        if section == "user":
            dept = rec.get("department")
            break
    assert dept == "finance", f"GDPR export user section missing department: {dept!r}"
    print("  gdpr: export_user user section includes department")


# ---------------------------------------------------------------------------
# Finding 1: rules are honored END-TO-END through the HTTP API (not resolve()).
# ---------------------------------------------------------------------------
def test_api_upload_routes_to_rule_project() -> None:
    """(a) Upload with NO project_id + a matching ext rule -> the document lands in
    the rule's target project (not the department inbox)."""
    _reset_rules()
    c = TestClient(app)
    with Session(engine) as s:
        target = Project(name="Rule Target (docx)", created_by="admin", default_provider="claude")
        s.add(target)
        s.commit()
        s.refresh(target)
        tid = target.id
        s.add(RoutingRule(priority=10, match_ext="docx", project_id=tid))
        s.commit()
    H, _u = _docs_headers("upl@x.co")
    r = c.post(
        "/api/documents",
        headers=H,
        files={
            "file": (
                "memo.docx",
                _dx(["A line."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["project_id"] == tid, ("upload should route to the rule's project", r.json())
    print("  api upload: no project_id + docx rule -> lands in the rule's project")


def test_api_upload_no_rule_falls_back_to_inbox() -> None:
    """(d) No matching rule -> byte-identical legacy behaviour: the file lands in the
    submitter's department inbox project."""
    _reset_rules()
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        H, u = _docs_headers("inbox-user@x.co")
        with Session(engine) as s:
            from app.models import User

            row = s.get(User, u.id)
            row.department = "ops"
            s.add(row)
            s.commit()
        r = c.post(
            "/api/documents",
            headers=H,
            files={
                "file": (
                    "notes.docx",
                    _dx(["Hi."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, (r.status_code, r.text)
        pid = r.json()["project_id"]
        with Session(engine) as s:
            proj = s.get(Project, pid)
        assert proj is not None and "ops" in proj.name.lower(), (
            "no rule -> department inbox",
            proj.name if proj else None,
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  api upload: no rule -> department inbox (legacy fallback)")


def test_api_translate_rule_provider_override_wins() -> None:
    """(b) A rule provider override wins over the project default AT translate time:
    the enqueued job carries the override, and the completed job's provider_used
    shows that engine actually ran."""
    _reset_rules()
    c = TestClient(app)
    register_provider("claude", _NamedStub("claude"))  # project default engine
    register_provider("gemini", _NamedStub("gemini"))  # the rule's override engine
    try:
        # Rule: docx -> provider gemini (no project target -> project unchanged).
        with Session(engine) as s:
            s.add(RoutingRule(priority=5, match_ext="docx", provider="gemini"))
            s.commit()
        H, _u = _docs_headers("ovr@x.co")
        r = c.post(
            "/api/documents",
            headers=H,
            files={
                "file": (
                    "d.docx",
                    _dx(["Translate me."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()["document_id"]
        r = c.post(f"/api/documents/{doc_id}/translate", headers=H, json={"targets": ["fr-FR"]})
        assert r.status_code == 202, (r.status_code, r.text)
        jid = r.json()["jobs"][0]["job_id"]
        # The job payload carries the effective provider stamp.
        with Session(engine) as s:
            job = s.get(Job, jid)
        assert (job.payload or {}).get("provider") == "gemini", (
            "rule provider must be stamped on the job",
            job.payload,
        )
        # End-to-end: gemini actually ran.
        done = _wait_job(jid)
        assert done.status == JobStatus.COMPLETED, (done.status, done.error)
        assert "gemini" in (done.result or {}).get("provider_used", {}), (
            "the rule's provider must run, not the project default",
            done.result,
        )
        with Session(engine) as s:
            seg = s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).first()
        assert seg.target_text.startswith("[gemini:"), ("gemini output expected", seg.target_text)
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)
        _prov._OVERRIDES.pop("gemini", None)
    print("  api translate: rule provider override wins over project default (job uses it)")


def test_api_translate_rule_keyless_provider_409() -> None:
    """(c) A rule routing to a keyless real provider still 409s (the mock-hard-fail
    evaluates the FINAL effective provider), and enqueues nothing."""
    _reset_rules()
    c = TestClient(app)
    register_provider("claude", _NamedStub("claude"))  # project default is live
    try:
        # Rule: docx -> provider openai, but openai has NO key and NO stub -> not live.
        with Session(engine) as s:
            s.add(RoutingRule(priority=5, match_ext="docx", provider="openai"))
            s.commit()
        H, _u = _docs_headers("keyless@x.co")
        r = c.post(
            "/api/documents",
            headers=H,
            files={
                "file": (
                    "k.docx",
                    _dx(["Line."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()["document_id"]
        before = len(_all_translate_jobs())
        r = c.post(f"/api/documents/{doc_id}/translate", headers=H, json={"targets": ["fr-FR"]})
        assert r.status_code == 409, ("rule -> keyless provider must 409", r.status_code, r.text)
        after = len(_all_translate_jobs())
        assert after == before, ("409 must enqueue zero jobs", before, after)
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)
    print("  api translate: rule -> keyless provider -> 409, zero jobs enqueued")


def _all_translate_jobs():
    with Session(engine) as s:
        return s.exec(select(Job.id).where(Job.kind == "translate_document")).all()


# ---------------------------------------------------------------------------
# QW-D: reserved-for-v2 rule fields (subject / workflow / language-set) are consulted
# by NO reader in v1 -- refused at authoring time on EVERY surface (web/CLI/api/MCP),
# and pre-existing rules that still carry them are flagged INERT in list/show output.
# ---------------------------------------------------------------------------
def test_reserved_field_error_helper() -> None:
    """routing.reserved_field_error names exactly the reserved fields supplied, with
    correct grammar; honored-only fields return None; blank/whitespace is not 'set'."""
    assert routing.reserved_field_error() is None
    assert (
        routing.reserved_field_error(match_subject="", workflow_id=None, language_set_id=None)
        is None
    )
    assert routing.reserved_field_error(match_subject="   ") is None
    one = routing.reserved_field_error(workflow_id=5)
    assert one.startswith("workflow is reserved for v2"), one
    two = routing.reserved_field_error(match_subject="x", workflow_id=5)
    assert two.startswith("subject and workflow are reserved for v2"), two
    three = routing.reserved_field_error(match_subject="x", workflow_id=5, language_set_id=9)
    assert three.startswith("subject, workflow and language set are reserved for v2"), three
    # the honored-fields promise is spelled out verbatim
    assert "honors only ext, department, project, provider and priority" in one, one
    print("  reserved_field_error: names set fields, correct grammar, None when clean")


def test_web_refuses_reserved_fields() -> None:
    """The /routing form refuses a rule carrying a reserved field (subject/workflow/
    language-set): a 303 ?error= redirect and NO row written. Checked on the RAW form
    value BEFORE coercion (MED-2), so a NON-coercible reserved value (0/-1/'abc'/'1e3'/
    a huge int) that _int_or_none would silently drop to None still refuses -- it must
    never be swallowed into a created rule. Empty-string fields (what a form actually
    posts) stay allowed."""
    _reset_rules()
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        csrf_for(c)
        cases = [
            ("workflow_id", "5"),
            ("language_set_id", "9"),
            ("match_subject", "subs@x"),
            # MED-2 coercion-bypass regression: non-coercible / edge reserved values
            ("workflow_id", "0"),
            ("workflow_id", "-1"),
            ("workflow_id", "abc"),
            ("workflow_id", "1e3"),
            ("workflow_id", "9" * 40),
            ("language_set_id", "0"),
            ("language_set_id", "notanint"),
        ]
        for field, val in cases:
            n_before = _rule_count()
            r = c.post(
                "/routing",
                data={"priority": "5", "match_ext": "docx", field: val},
                follow_redirects=False,
            )
            assert r.status_code == 303 and "error=" in r.headers["location"], (
                field,
                val,
                r.status_code,
                r.headers,
            )
            assert _rule_count() == n_before, (
                field,
                val,
                "reserved-field rule must NOT be created",
            )
        # Empty-string reserved fields (a form posts them) are ALLOWED -> rule created.
        n_before = _rule_count()
        r = c.post(
            "/routing",
            data={
                "priority": "5",
                "match_ext": "csv",
                "workflow_id": "",
                "language_set_id": "",
                "match_subject": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and "saved" in r.headers["location"], (r.status_code, r.headers)
        assert _rule_count() == n_before + 1, "empty reserved fields must be allowed"
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  web form: reserved fields refused pre-coercion (0/-1/abc/huge); empty allowed")


def test_cli_refuses_reserved_fields() -> None:
    """Both CLI twins (`routing rule add` + `admin routing rule add`) refuse a reserved
    field at add time with exit 2 (EXIT_USAGE) and a reserved-for-v2 message; no row."""
    _reset_rules()
    for grp in (["routing", "rule", "add"], ["admin", "routing", "rule", "add"]):
        n_before = _rule_count()
        code, out, err = _run_cli(grp + ["--ext", "docx", "--workflow", "5"])  # noqa: RUF005
        assert code == 2, (grp, code, out, err)
        assert "reserved for v2" in (out + err) and "workflow" in (out + err), (grp, out, err)
        assert _rule_count() == n_before, (grp, "no row on refusal")
    code, out, err = _run_cli(["routing", "rule", "add", "--ext", "x", "--subject", "s@x"])
    assert code == 2 and "subject" in (out + err), (out, err)
    code, out, err = _run_cli(["admin", "routing", "rule", "add", "--language-set", "9"])
    assert code == 2 and "language set" in (out + err), (out, err)
    print("  cli: routing + admin routing rule add refuse reserved fields (exit 2, no row)")


def test_api_refuses_reserved_fields() -> None:
    """POST /api/routing/rules refuses a reserved field with a 400 and writes no row --
    checked on the RAW body value BEFORE coercion (MED-2), so a non-coercible / edge
    reserved value (0/-1/'abc'/true/huge int) that _int_id would drop to None still
    refuses instead of silently creating a rule. A non-string `subject` (a JSON number/
    list) must 400, NOT 500 (LOW-5). The MCP add_rule tool relays this same 400."""
    _reset_rules()
    c = TestClient(app)
    adm = security.get_by_email("admin")
    _k, tok = apikeys.create_key(adm.id, "qwd-adm", scopes="api docs")
    H = {"X-Loc-Token": tok}
    cases = [
        ("workflow", 5),
        ("language_set", 9),
        ("subject", "s@x"),
        # MED-2 coercion-bypass regression on the RAW body:
        ("workflow", 0),
        ("workflow", -1),
        ("workflow", "abc"),
        ("workflow", True),
        ("workflow", 10**20),
        ("language_set", 0),
        ("language_set", "notanint"),
        # LOW-5: a non-string subject must 400, not AttributeError -> 500
        ("subject", 123),
        ("subject", ["x"]),
        ("subject", True),
    ]
    for body_field, val in cases:
        n_before = _rule_count()
        r = c.post(
            "/api/routing/rules", headers=H, json={"ext": "docx", "priority": 50, body_field: val}
        )
        assert r.status_code == 400, (body_field, val, r.status_code, r.text)
        assert "reserved for v2" in r.json().get("error", ""), (body_field, val, r.text)
        assert _rule_count() == n_before, (body_field, val, "no row on 400")
    # empty-string / absent reserved fields are allowed; a clean rule still creates.
    r = c.post(
        "/api/routing/rules",
        headers=H,
        json={"ext": "docx", "department": "docs", "priority": 50, "workflow": "", "subject": ""},
    )
    assert r.status_code == 200, r.text
    print("  api: reserved fields refused pre-coercion incl. non-string subject; clean ok")


def test_inert_fields_flagged_in_readback() -> None:
    """A legacy rule carrying reserved fields (constructed via the service, as a
    migration/backfill or pre-fix row would be) is flagged INERT in `rule show` and
    `rule list` on BOTH CLI twins -- silent dead config is made discoverable, not
    invalidated."""
    _reset_rules()
    with Session(engine) as s:
        proj = s.exec(select(Project).where(Project.name == "Subtitles")).first()
        if proj is None:
            proj = Project(name="Subtitles", created_by="admin")
            s.add(proj)
            s.commit()
            s.refresh(proj)
        # The service still stores these (existing rows read back intact); the SURFACES
        # refuse NEW ones. This mimics a rule authored before the QW-D fix.
        rule = routing.create_rule(
            s, priority=8, match_ext="srt", match_subject="subs@x", project_id=proj.id, actor="t"
        )
        rid = rule.id
    for grp in (["admin", "routing", "rule"], ["routing", "rule"]):
        code, out, _ = _run_cli(grp + ["show", str(rid)])  # noqa: RUF005
        assert code == 0 and "reserved v2, not consulted" in out, (grp, out)
        code, out, _ = _run_cli(grp + ["list"])  # noqa: RUF005
        assert code == 0 and f"#{rid}" in out and "not consulted at run time" in out, (grp, out)
    print("  inert: rule show flags the reserved field; rule list notes the inert rule")


def test_inert_flag_web_cli_agree_on_whitespace_subject() -> None:
    """LOW-8: the web inert badge and the CLI/refusal presence test must AGREE, even on a
    whitespace-only match_subject. create_rule strips subject to "", so a whitespace-only
    value can only reach a row via a RAW insert (a legacy row / migration). _field_present
    treats "   " as ABSENT, so neither the CLI note NOR the web badge fires -- the web badge
    is now driven by the route's kernel-computed `inert_rule_ids`, not a hand-rolled Jinja
    truthiness twin that wrongly flagged such a row inert on the web while the CLI called it
    clean."""
    _reset_rules()
    with Session(engine) as s:
        proj = s.exec(select(Project).where(Project.name == "Subtitles")).first()
        if proj is None:
            proj = Project(name="Subtitles", created_by="admin")
            s.add(proj)
            s.commit()
            s.refresh(proj)
        # RAW insert (bypasses create_rule's .strip()) -> a whitespace-only subject.
        s.add(RoutingRule(priority=9, match_ext="srt", match_subject="   ", project_id=proj.id))
        # A control row with a REAL reserved field, so the positive badge path is exercised
        # on the same page render.
        s.add(RoutingRule(priority=11, match_ext="vtt", match_subject="real@x", project_id=proj.id))
        s.commit()
        ws_rid = s.exec(select(RoutingRule).where(RoutingRule.priority == 9)).first().id
    # CLI: whitespace-only subject reads back CLEAN (no note); the real one is flagged.
    code, out, _ = _run_cli(["admin", "routing", "rule", "show", str(ws_rid)])
    assert code == 0 and "reserved v2, not consulted" not in out, out
    # Web: exactly ONE "inert fields" badge on the page (the real row, never the whitespace).
    c = TestClient(app)
    settings.set_many({"security_auth_required": "1"})
    try:
        csrf_for(c)
        c.post("/auth/login", data={"email": "admin", "password": "admin"})
        page = c.get("/routing").text
        assert page.count("inert fields") == 1, (
            "web badge must fire once (real row only), never on whitespace subject",
            page.count("inert fields"),
        )
        sign_out(c)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  inert flag: web badge and CLI agree on whitespace-only subject (both clean)")


def main() -> None:
    init_db()
    security.seed_initial()  # admin/admin + linguist + user1
    settings.set_many({"audit_enabled": "1", "security_auth_required": "0"})
    test_resolver_priority_and_ext()
    test_resolver_department_scoping()
    test_resolver_explicit_override_wins()
    test_resolver_fallback_to_inbox()
    test_validate_human_review_rejected()
    test_admin_crud()
    test_admin_rejects_human_review_workflow()
    test_rule_missing_fk_clean()
    test_rule_show_readback()
    test_routing_budget_meters()
    test_end_to_end_srt_vs_json()
    # --- Chunk 2 ---
    test_department_for_reads_user_column()
    test_department_inbox_shared_per_department()
    test_resolver_department_fallback_inbox()
    test_department_case_canonical()
    test_admin_edit_department()
    test_gdpr_export_includes_department()
    # --- Finding 1: rules honored end-to-end through the HTTP API ---
    test_api_upload_routes_to_rule_project()
    test_api_upload_no_rule_falls_back_to_inbox()
    test_api_translate_rule_provider_override_wins()
    test_api_translate_rule_keyless_provider_409()
    # --- QW-D: reserved-for-v2 fields refused at input; legacy rows flagged inert ---
    test_reserved_field_error_helper()
    test_web_refuses_reserved_fields()
    test_cli_refuses_reserved_fields()
    test_api_refuses_reserved_fields()
    test_inert_fields_flagged_in_readback()
    test_inert_flag_web_cli_agree_on_whitespace_subject()
    print("ROUTING OK")


if __name__ == "__main__":
    main()
