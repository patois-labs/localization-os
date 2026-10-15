"""Multi-org confinement batch 2026-10-05 (offline, throwaway data dir).

Issues covered, each under an ACTIVE multi-org gate (never enabled on a real install):

  [1] #538 the Ledger prompt-capture toggle and clear are host-only: an org admin gets 403
      from POST /prompt-tap/toggle and /prompt-tap/clear, the setting and the shared
      legacy capture file are untouched, the controls are not offered, and the generic
      config door refuses prompt_tap_enabled. Gate off: unchanged.
  [2] #536 an org-A admin cannot invite (or activate through an accept link) an inactive
      passwordless account that lives in org B.
  [3] #467 a customer-bound user moved to another org lists none of the old org's projects.
  [4] #487 audit rows are confined by the org they were WRITTEN in, not the actor's
      current org (subject-access export, customer audit view, workspace export).
  [5] #515 CLI verbs honour --org (or refuse) under multi-org.
"""

import hashlib
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mo_b1005_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db as app_db
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Document, Invite, Organization, Project, Role, Segment, User
from app.services import access, apikeys, audit, gdpr, prompt_tap, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for, session_data

ADMIN_A = "b1005-a@example.com"
ADMIN_B = "b1005-b@example.com"
PW = "B1005-Admin-Pass-2026-x"  # pregate: allow-secret throwaway fixture login

FIX = {}


def _fixture():
    init_db()
    with Session(engine) as s:
        org_a = Organization(name="Org A b1005")
        org_b = Organization(name="Org B b1005")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        FIX["org_a"], FIX["org_b"] = org_a.id, org_b.id
    security.create_user(ADMIN_A, "Admin A", Role.ADMIN, PW, org_id=FIX["org_a"])
    security.create_user(ADMIN_B, "Admin B", Role.ADMIN, PW, org_id=FIX["org_b"])
    with Session(engine) as s:
        for key, org_id in (("a", FIX["org_a"]), ("b", FIX["org_b"])):
            cust = Customer(name=f"Cust {key}", org_id=org_id)
            s.add(cust)
            s.commit()
            s.refresh(cust)
            proj = Project(
                name=f"Proj {key}", customer_id=cust.id, org_id=org_id, source_locale="en-US"
            )
            s.add(proj)
            s.commit()
            s.refresh(proj)
            doc = Document(project_id=proj.id, filename=f"doc_{key}.md", fmt="md")
            s.add(doc)
            s.commit()
            s.refresh(doc)
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    source_text="Use the app.",
                    target_text="Utilisez l'app.",
                )
            )
            s.commit()
            FIX[f"cust_{key}"] = cust.id
            FIX[f"proj_{key}"] = proj.id
            FIX[f"doc_{key}"] = doc.id
        FIX["uid_a"] = s.exec(select(User).where(User.email == ADMIN_A)).first().id
        FIX["uid_b"] = s.exec(select(User).where(User.email == ADMIN_B)).first().id
    _row, raw = apikeys.create_key(FIX["uid_b"], "b1005-b", scopes="api docs admin")
    FIX["hdr_b"] = {"X-Loc-Token": raw}
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})


def _login(email) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    c.get("/settings")
    csrf_for(c)
    return c


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_mo_b1005_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    assert access.multi_org_active() is on


# --------------------------------------------------------------------------- #
def test_1_ledger_capture_controls_are_host_only():
    _set_gate(True)
    assert settings.get("prompt_tap_enabled") == "0"
    c = _login(ADMIN_B)
    r = c.post("/prompt-tap/toggle", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    assert settings.get("prompt_tap_enabled") == "0", "an org admin turned capture on for every org"
    r = c.post("/prompt-tap/clear", data={}, follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    page = c.get("/prompt-tap").text
    assert 'action="/prompt-tap/toggle"' not in page, "the capture toggle is still offered"
    assert 'action="/prompt-tap/clear"' not in page, "the clear control is still offered"
    api = TestClient(app)
    # prompt_tap is a feature flag: its own /api and web feature doors refuse as well.
    r = api.post("/api/admin/features/prompt_tap", headers=FIX["hdr_b"], json={"enabled": True})
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert settings.get("prompt_tap_enabled") == "0"
    r = c.post("/settings/features/prompt_tap", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    assert settings.get("prompt_tap_enabled") == "0"
    assert "prompt_tap_enabled" in settings.security_setting_keys()
    # The host operator keeps the door (CLI path calls the service directly).
    prompt_tap.set_enabled(True, actor="host")
    assert settings.get("prompt_tap_enabled") == "1"
    prompt_tap.set_enabled(False, actor="host")
    print("  [1] #538 Ledger capture toggle/clear are host-only under multi-org")


def test_1b_ledger_controls_gate_off_unchanged():
    _set_gate(False)
    c = _login(ADMIN_A)
    page = c.get("/prompt-tap").text
    assert 'action="/prompt-tap/toggle"' in page, "gate off: the toggle is offered as before"
    r = c.post("/prompt-tap/toggle", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert settings.get("prompt_tap_enabled") == "1"
    r = c.post("/prompt-tap/toggle", data={"enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert settings.get("prompt_tap_enabled") == "0"
    print("  [1b] #538 gate off: toggle behaves as before")


def _precreate_inactive(email, org_id):
    """The 'pre-create then invite' row: local, passwordless, inactive, in `org_id`."""
    security.create_user(email, "Pre", Role.TRANSLATOR, "", org_id=org_id)
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == email)).first()
        row.active = False
        row.password_hash = ""
        s.add(row)
        s.commit()
        assert not row.password_hash and not row.active
    return email


def test_2_cross_org_invite_is_refused():
    _set_gate(True)
    victim = _precreate_inactive("b1005-victim@example.com", FIX["org_b"])
    # create side: org A's admin may not invite org B's pre-created row.
    try:
        security.create_invite(victim, Role.TRANSLATOR, ADMIN_A, org_id=FIX["org_a"])
    except ValueError:
        pass
    else:
        raise AssertionError("an org-A admin invited an org-B account")
    with Session(engine) as s:
        assert not s.exec(select(Invite).where(Invite.email == victim)).all()
    # accept side: an org-A invitation that already exists (issued before the account
    # moved, or minted directly) must not activate or re-password the org-B row.
    tok = "b1005-cross-org-token-0001"
    with Session(engine) as s:
        s.add(
            Invite(
                email=victim,
                role=Role.TRANSLATOR,
                token=tok,
                created_by=ADMIN_A,
                org_id=FIX["org_a"],
                expires_at=datetime.utcnow() + timedelta(days=1),
            )
        )
        s.commit()
    try:
        security.accept_invite(tok, "Attacker", "Attacker-Chosen-Pass-2026-zz")
    except security.InviteRefused:
        pass
    else:
        raise AssertionError("an org-A invitation activated an org-B account")
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == victim)).first()
        assert not row.active and not row.password_hash, "the org-B account was taken over"
        assert row.org_id == FIX["org_b"]
    # the web accept page refuses too (second invite row, same shape)
    tok2 = "b1005-cross-org-token-0002"
    with Session(engine) as s:
        s.add(
            Invite(
                email=victim,
                role=Role.TRANSLATOR,
                token=tok2,
                created_by=ADMIN_A,
                org_id=FIX["org_a"],
                expires_at=datetime.utcnow() + timedelta(days=1),
            )
        )
        s.commit()
    c = TestClient(app)
    c.get(f"/auth/accept/{tok2}")
    r = c.post(
        f"/auth/accept/{tok2}",
        data={
            "name": "x",
            "password": "Attacker-Chosen-Pass-2026-zz",
            "confirm": "Attacker-Chosen-Pass-2026-zz",
            "csrf_token": session_data(c).get("csrf"),
        },
        follow_redirects=False,
    )
    assert r.status_code in (400, 403), r.status_code
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == victim)).first()
        assert not row.active and not row.password_hash
    # positive control: the SAME-org invitation of a pre-created row still works.
    mine = _precreate_inactive("b1005-mine@example.com", FIX["org_a"])
    inv = security.create_invite(mine, Role.TRANSLATOR, ADMIN_A, org_id=FIX["org_a"])
    got = security.accept_invite(inv.token, "Mine", "Same-Org-Pass-2026-zz")
    assert got is not None and got.active and got.org_id == FIX["org_a"]
    print("  [2] #536 cross-org invite / accept of an inactive passwordless account refused")


def test_2b_invite_gate_off_unchanged():
    _set_gate(False)
    other = _precreate_inactive("b1005-gateoff@example.com", FIX["org_b"])
    inv = security.create_invite(other, Role.TRANSLATOR, ADMIN_A, org_id=FIX["org_a"])
    got = security.accept_invite(inv.token, "Off", "Gate-Off-Pass-2026-zz")
    assert got is not None and got.active
    print("  [2b] #536 gate off: invitation behaves as before")


def test_3_moved_customer_bound_user_sees_no_old_org_projects():
    _set_gate(True)
    bound = security.create_user(
        "b1005-bound@example.com",
        "Bound",
        Role.TRANSLATOR,
        PW,
        customer_id=FIX["cust_a"],
        org_id=FIX["org_a"],
    )
    with Session(engine) as s:
        u = s.get(User, bound.id)
        assert access.visible_project_ids(s, u) == [FIX["proj_a"]]
        # The move itself is refused while the binding names another org's customer.
        try:
            access.move_user_to_org(s, bound.id, FIX["org_b"])
        except ValueError as exc:
            assert "bound to customer" in str(exc), exc
        else:
            raise AssertionError("a customer-bound user was moved to another org")
        s.rollback()
        assert s.get(User, bound.id, populate_existing=True).org_id == FIX["org_a"]
        # A row that is ALREADY in the stale state (moved by an older build, an import)
        # must not list the old org's projects either: lists and the single-row door agree.
        u = s.get(User, bound.id)
        u.org_id = FIX["org_b"]
        s.add(u)
        s.commit()
        s.refresh(u)
        assert access.visible_project_ids(s, u) == [], access.visible_project_ids(s, u)
        assert not access.can_view_project(s, u, FIX["proj_a"])
        # Unbinding first makes the move legal and the user sees org B's projects only.
        u.customer_id = None
        u.org_id = FIX["org_a"]
        s.add(u)
        s.commit()
        user, _old = access.move_user_to_org(s, bound.id, FIX["org_b"])
        assert user.org_id == FIX["org_b"]
        ids = access.visible_project_ids(s, user)
        assert FIX["proj_a"] not in (ids or []), ids
    print("  [3] #467 moved customer-bound user: move refused, stale row lists nothing")


def test_3b_bound_user_gate_off_unchanged():
    _set_gate(False)
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "b1005-bound@example.com")).first()
        u.customer_id = FIX["cust_a"]
        s.add(u)
        s.commit()
        s.refresh(u)
        assert access.visible_project_ids(s, u) == [FIX["proj_a"]]
    print("  [3b] #467 gate off: bound user keeps its customer's projects")


_SECRET_TARGET = "project 12 (Acme confidential launch)"


def test_4_audit_rows_follow_the_org_they_were_written_in():
    _set_gate(True)
    mover = "b1005-mover@example.com"
    security.create_user(mover, "Mover", Role.TRANSLATOR, PW, org_id=FIX["org_a"])
    audit.log(actor=mover, action="project.delete", target=_SECRET_TARGET, force=True)
    with Session(engine) as s:
        uid = s.exec(select(User).where(User.email == mover)).first().id
        access.move_user_to_org(s, uid, FIX["org_b"])
    # what `user org` records (cmd_user._user_org), which the backfill reads
    audit.log(
        actor="operator",
        action="user.move_org",
        target=mover,
        detail=f"org {FIX['org_a']}->{FIX['org_b']} (content stays in {FIX['org_a']})",
        force=True,
    )
    audit.log(actor=mover, action="project.create", target="project 99 (org b work)", force=True)
    # [a] subject-access export by org B: only org B's rows
    recs = [r for sec, r in gdpr.export_user(uid, caller_org_id=FIX["org_b"]) if sec == "audit"]
    targets = {r.get("target") for r in recs}
    assert _SECRET_TARGET not in targets, targets
    assert "project 99 (org b work)" in targets, targets
    # [b] customer /audit view
    seen_b = {r.target for r in audit.recent(limit=500, org_id=FIX["org_b"])}
    assert _SECRET_TARGET not in seen_b, "org B's audit view shows an org-A row"
    assert "project 99 (org b work)" in seen_b
    seen_a = {r.target for r in audit.recent(limit=500, org_id=FIX["org_a"])}
    assert _SECRET_TARGET in seen_a, "org A lost its own history"
    assert audit.count(org_id=FIX["org_b"]) == len(audit.recent(limit=5000, org_id=FIX["org_b"]))
    # [c] workspace export slice (iter_export is what _write_audit_slice streams, raw)
    ws_b = {r.get("target") for r in audit.iter_export(org_id=FIX["org_b"], batch=50)}
    assert _SECRET_TARGET not in ws_b, "the workspace export of org B carries an org-A row"
    ws_a = {r.get("target") for r in audit.iter_export(org_id=FIX["org_a"], batch=50)}
    assert _SECRET_TARGET in ws_a
    # the row carries the org it was written in (column asserts run AFTER the behavioural ones,
    # so the pre-fix failure is an assertion on the leak, not a missing-column error)
    with Session(engine) as s:
        from app.models import AuditLog

        rows = s.exec(select(AuditLog).where(AuditLog.actor == mover)).all()
        by_action = {r.action: r.org_id for r in rows}
    assert by_action["project.delete"] == FIX["org_a"], by_action
    assert by_action["project.create"] == FIX["org_b"], by_action
    # system and unauthenticated rows belong to no org
    audit.log(actor="unauthenticated 203.0.113.9", action="login.failed", force=True)
    for org in (FIX["org_a"], FIX["org_b"]):
        assert not [r for r in audit.recent(limit=500, org_id=org) if "unauthenticated" in r.actor]
    # the hash chain still verifies over stamped rows
    ok, _checked, bad = audit.verify_chain()
    assert ok, bad
    print("  [4] #487 audit rows are confined by the org they were written in (3 paths)")


def test_4b_backfill_of_rows_from_before_the_column():
    from app.models import AuditLog

    with Session(engine) as s:
        for r in s.exec(select(AuditLog).where(AuditLog.org_id != None)).all():  # noqa: E711
            r.org_id = None
            s.add(r)
        s.commit()
    with engine.begin() as conn:
        app_db._backfill_auditlog_org(conn)
    with Session(engine) as s:
        rows = {
            r.action: r.org_id
            for r in s.exec(
                select(AuditLog).where(AuditLog.actor == "b1005-mover@example.com")
            ).all()
        }
    # the recorded user.move_org row re-stamps the pre-move row with the OLD org
    assert rows["project.delete"] == FIX["org_a"], rows
    assert rows["project.create"] == FIX["org_b"], rows
    print("  [4b] #487 backfill re-attributes pre-move rows using the recorded move")


_OP_SECRET = "b1005-operator-secret-value"  # pregate: allow-secret throwaway fixture key


def _cli(argv, env=None):
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        return run_cli(argv)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _op_env():
    return {"LOC_TMS_OPERATOR_KEY": _OP_SECRET}


def test_5_cli_verbs_honour_org_under_multi_org():
    from app.models import (
        Connection,
        Job,
        JobStatus,
        LQAResult,
        QueryThread,
        WorkflowGroup,
    )
    from app.services import query_threads as qt

    settings.set_many({"operator_key_hash": hashlib.sha256(_OP_SECRET.encode()).hexdigest()})
    settings.invalidate_cache()
    _set_gate(True)
    a, b = str(FIX["org_a"]), str(FIX["org_b"])
    # ---- install-wide verbs: --org cannot scope them, --operator can -------------
    # retention ON (throwaway data dir only) so a leaked purge would really run
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    before = {k: settings.get(k) for k in ("retention_enabled", "retention_days", "srx_enabled")}
    tok_before = settings.get("connector_api_token")
    for argv in (
        ["admin", "retention", "purge-now", "--yes"],
        ["admin", "retention", "config", "--enabled", "on", "--days", "30"],
        ["admin", "srx", "enable"],
        ["admin", "ldap", "config", "--enabled", "on"],
        ["admin", "oidc", "config", "--enabled", "on"],
        ["admin", "lqa", "rulebook", "get", "en-US", "fr-FR"],
        ["connector", "token-regen", "--yes"],
    ):
        code, out, err = _cli(["--org", a, *argv])
        assert code == 2, (argv, code, out[-200:], err[-300:])
        assert "operator" in err, (argv, err)
    after = {k: settings.get(k) for k in before}
    assert after == before, (before, after)
    assert settings.get("connector_api_token") == tok_before
    assert settings.get("ldap_enabled") != "1" and settings.get("sso_enabled") != "1"
    code, out, err = _cli(
        ["--operator", "admin", "retention", "config", "--enabled", "off", "--days", "0", "--yes"],
        env=_op_env(),
    )
    assert code == 0, (code, err[-300:])
    # ---- query: another org's thread is invisible and unchangeable under --org ----
    with Session(engine) as s:
        scope = qt.resolve_scope(s, project_id=FIX["proj_b"])
        thread = qt.open_thread(
            s,
            user=None,
            scope=scope,
            subject="b-secret",
            body="org b body",
            trusted_operator=True,
            actor="op",
            org_id=access.UNSCOPED,
        )
        tid = thread.id
    for argv in (
        ["query", "show", str(tid)],
        ["query", "reply", str(tid), "--body", "injected"],
        ["query", "answer", str(tid), "--body", "injected"],
        ["query", "close", str(tid)],
        ["query", "assign", str(tid), "--assignee", ADMIN_A],
    ):
        code, out, err = _cli(["--org", a, *argv])
        assert code != 0, (argv, out[-200:])
        assert "b-secret" not in out + err and "org b body" not in out + err, argv
    with Session(engine) as s:
        t = s.get(QueryThread, tid)
        assert t.status == "open" and not t.assignee_email and t.post_count == 1, (
            t.status,
            t.assignee_email,
            t.post_count,
        )
    code, out, err = _cli(["--org", b, "query", "show", str(tid)])
    assert code == 0 and "b-secret" in out, (code, out, err)
    # the key-verified --operator door (access.UNSCOPED) still reads any org's thread, and the
    # ring does not compare a project's org against the sentinel object
    code, out, err = _cli(["--operator", "query", "show", str(tid)], env=_op_env())
    assert code == 0 and "b-secret" in out, (code, out, err)
    code, out, err = _cli(["--operator", "query", "list"], env=_op_env())
    assert code == 0 and "b-secret" in out, (code, out, err)
    # ---- workflow group show ----------------------------------------------------
    with Session(engine) as s:
        g = WorkflowGroup(name="b-group", org_id=FIX["org_b"])
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id
    code, out, err = _cli(["--org", a, "workflow", "group", "show", str(gid)])
    assert code == 2 and "b-group" not in out, (code, out, err)
    code, out, err = _cli(["--org", b, "workflow", "group", "show", str(gid)])
    assert code == 0 and "b-group" in out, (code, out, err)
    # ---- job run / status ---------------------------------------------------------
    code, out, err = _cli(["--org", a, "job", "run", "data_export", "--payload", "x=1"])
    assert code == 2, (code, out, err)
    code, out, err = _cli(
        ["--org", a, "job", "run", "connector_translate", "--payload", "webhook_ping_pull=1"]
    )
    assert code == 2, (code, out, err)
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"project_id": FIX["proj_b"]},
            created_by=ADMIN_B,
            status=JobStatus.COMPLETED,
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        jid = j.id
    code, out, err = _cli(["--org", a, "job", "status", str(jid)])
    assert code == 2, (code, out, err)
    code, out, err = _cli(["--org", b, "job", "status", str(jid)])
    assert code == 0, (code, out, err)
    # ---- context show -------------------------------------------------------------
    code, out, err = _cli(["--org", a, "context", "show", "--document", str(FIX["doc_b"])])
    assert code == 2, (code, out, err)
    code, out, err = _cli(["--org", a, "context", "show", "--project", str(FIX["proj_b"])])
    assert code == 2, (code, out, err)
    code, out, err = _cli(["--org", b, "context", "show", "--project", str(FIX["proj_b"])])
    assert code == 0, (code, out, err)
    # ---- lqa ----------------------------------------------------------------------
    with Session(engine) as s:
        seg_b = s.exec(select(Segment).where(Segment.document_id == FIX["doc_b"])).first().id
    code, out, err = _cli(["lqa", "findings", "--document", str(FIX["doc_b"])])
    assert code == 2 and "multi-org" in err, (code, err)
    code, out, err = _cli(["--org", a, "lqa", "findings", "--document", str(FIX["doc_b"])])
    assert code == 2, (code, out, err)
    log_b = [
        "lqa",
        "log",
        "--document",
        str(FIX["doc_b"]),
        "--segment",
        str(seg_b),
        "--dimension",
        "accuracy",
        "--severity",
        "minor",
        "--note",
    ]
    code, out, err = _cli(["--org", a, *log_b, "x"])
    assert code == 2, (code, out, err)
    with Session(engine) as s:
        assert not s.exec(select(LQAResult).where(LQAResult.document_id == FIX["doc_b"])).all()
    code, out, err = _cli(["--org", b, *log_b, "own org ok"])
    assert code == 0, (code, out, err)
    with Session(engine) as s:
        fid = s.exec(select(LQAResult).where(LQAResult.document_id == FIX["doc_b"])).first().id
    for argv in (["lqa", "withdraw", str(fid)], ["lqa", "edit", str(fid), "--note", "hijack"]):
        code, out, err = _cli(["--org", a, *argv])
        assert code != 0, (argv, out)
    with Session(engine) as s:
        row = s.get(LQAResult, fid)
        assert row.withdrawn_at is None and row.note == "own org ok"
    # ---- account ------------------------------------------------------------------
    for verb in (["show"], ["reset-onboarding"]):
        code, out, err = _cli(["--org", a, "account", *verb, "--user", ADMIN_B])
        assert code == 2 and "no such user" in err, (verb, code, out, err)
    code, out, err = _cli(["--org", b, "account", "show", "--user", ADMIN_B])
    assert code == 0, (code, out, err)
    # ---- connector prune-config --all is confined to the org ------------------------
    from app.services.connectors import store_part2

    with Session(engine) as s:
        ca = Connection(kind="jira", name="a-conn", org_id=FIX["org_a"], config={})
        cb = Connection(kind="jira", name="b-conn", org_id=FIX["org_b"], config={})
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        cid_a, cid_b = ca.id, cb.id
    seen = []
    real = store_part2.prune_orphaned_config

    def spy(cid, **kw):
        seen.append(cid)
        return {"connection_id": cid, "total": 0, "removed": []}

    store_part2.prune_orphaned_config = spy
    try:
        code, out, err = _cli(["--org", a, "connector", "prune-config", "--all"])
        assert code == 0, (code, out, err)
        assert cid_a in seen and cid_b not in seen, seen
        del seen[:]
        code, out, err = _cli(["--operator", "connector", "prune-config", "--all"], env=_op_env())
        assert code == 0 and cid_a in seen and cid_b in seen, (code, out, err, seen)
        # a bare None scope under an active gate sweeps nothing (fail closed)
        del seen[:]
        assert store_part2.prune_all_orphaned_config(org_id=None)["connections"] == []
        assert not seen, seen
    finally:
        store_part2.prune_orphaned_config = real
    # ---- scim status needs a scope under multi-org -----------------------------------
    for argv in (["scim", "status"],):
        code, out, err = _cli(argv)
        assert code == 2 and "multi-org" in err, (argv, code, out[-200:], err[-300:])
    code, out, err = _cli(["--operator", "scim", "status"], env=_op_env())
    assert code == 0, (code, out, err)
    print("  [5] #515 CLI verbs honour --org (or need --operator) under multi-org")


def test_5b_cli_gate_off_unchanged():
    _set_gate(False)
    for argv in (
        ["admin", "retention", "status"],
        ["admin", "srx", "enable"],
        ["admin", "srx", "disable"],
        ["scim", "status"],
    ):
        code, _out, err = _cli(argv)
        assert code == 0, (argv, code, err)
    print("  [5b] #515 gate off: install-wide admin verbs run without --org or --operator")


def main():
    _fixture()
    try:
        test_1_ledger_capture_controls_are_host_only()
        test_2_cross_org_invite_is_refused()
        test_3_moved_customer_bound_user_sees_no_old_org_projects()
        test_4_audit_rows_follow_the_org_they_were_written_in()
        test_4b_backfill_of_rows_from_before_the_column()
        test_5_cli_verbs_honour_org_under_multi_org()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_1b_ledger_controls_gate_off_unchanged()
    test_2b_invite_gate_off_unchanged()
    test_3b_bound_user_gate_off_unchanged()
    test_5b_cli_gate_off_unchanged()
    print("verify_multiorg_batch1005 OK")


if __name__ == "__main__":
    main()
