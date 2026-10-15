"""Multi-org account invariants (#178 items 1, 4, 5 and #181), on a throwaway two-org install.

Each case drives the surface where the defect showed, end to end, and each failed on the
base commit (accc9661):

  * last_admin_*          #178-1: org A's last admin via the CLI while org B has an admin,
                          including a move out of the org (`user org`, move_user_to_org).
  * member_add_*          #178-4: web project membership resolved the email install-wide.
  * self_export_*         #178-5: /account/export after a user moved orgs, every section
                          (projects, documents, job payloads, ...; gate LOW-2).
  * orgless_*             #181: "an account must name an organization" at the service seam.
  * create_call_ratchet   #181: every call site reaching create_user / create_invite or
                          constructing User( in app/ is inventoried; a new one FAILS.

The gate (`multi_org_enabled`) is only ever turned on inside this suite's temp data dir.
"""

import ast
import io
import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

init_env("loctms_multiorg_inv_")
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Glossary, Invite, Organization, Project, ProjectMember, Role, User
from app.services import access, apikeys, gdpr, security, settings
from tests._csrf import csrf_for

PASSWORD = "Throwaway-MultiorgInv-42!"  # pregate: allow-secret test-only credential
LOGIN_FIELD = "password"  # pregate: allow-secret form field name


def gate(on: bool) -> None:
    settings.set_many(
        {"security_auth_required": "1" if on else "0", "multi_org_enabled": "1" if on else "0"}
    )
    settings.invalidate_cache()


def cli(argv):
    """Run the CLI in-process: (exit_code, stdout + stderr)."""
    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


def login(email):
    client = TestClient(app)
    csrf_for(client)
    r = client.post(
        "/auth/login", data={"email": email, LOGIN_FIELD: PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code, r.text[:300])
    csrf_for(client)
    return client


def row(email):
    with Session(engine) as s:
        return s.exec(select(User).where(User.email == email)).first()


def setup():
    init_db()
    gate(True)
    with Session(engine) as s:
        a, b = Organization(name="Inv Org A"), Organization(name="Inv Org B")
        s.add(a)
        s.add(b)
        s.commit()
        aid, bid = a.id, b.id
    security.create_user("admin@a.test", "Admin A", Role.ADMIN, PASSWORD, org_id=aid)
    security.create_user("admin@b.test", "Admin B", Role.ADMIN, PASSWORD, org_id=bid)
    return aid, bid


# --- #178-1: last administrator, counted in the account's OWN org -----------------------
def _assert_still_admin(email):
    u = row(email)
    assert u.active and u.role == Role.ADMIN, (email, u.active, u.role)


def _restore_admin(email):
    """Put the admin back even when a case goes red, so one failure cannot cascade into
    the cases after it (on the base commit every refusal below was a success)."""
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        if u is not None:
            u.active, u.role = True, Role.ADMIN
            s.add(u)
            s.commit()


def last_admin_cli_scoped(aid):
    """`--org A user disable/role` on org A's only admin, while org B still has one. The
    CLI used to count admins install-wide, so the admin in org B let this through."""
    try:
        code, out = cli(["--org", str(aid), "user", "disable", "admin@a.test", "--yes"])
        assert code != 0 and "last active administrator" in out, (code, out)
        _assert_still_admin("admin@a.test")
        code, out = cli(["--org", str(aid), "user", "role", "admin@a.test", Role.VIEWER])
        assert code != 0 and "last active administrator" in out, (code, out)
        _assert_still_admin("admin@a.test")
    finally:
        _restore_admin("admin@a.test")


def last_admin_cli_operator():
    """The key-verified --operator door resolves the org to UNSCOPED; the seam still has to
    count the TARGET's org, not the caller's (which is every org)."""
    from app.cli import _common

    try:
        with patch.object(_common, "_verify_operator_key", lambda: None):
            code, out = cli(["--operator", "user", "disable", "admin@a.test", "--yes"])
            assert code != 0 and "last active administrator" in out, (code, out)
            code, out = cli(["--operator", "user", "role", "admin@a.test", Role.VIEWER])
            assert code != 0 and "last active administrator" in out, (code, out)
        _assert_still_admin("admin@a.test")
    finally:
        _restore_admin("admin@a.test")


def last_admin_service_seam():
    """The invariant lives at the mutation boundary, so a caller that skips every route
    guard (UNSCOPED operator scope, the erase path) is still refused. Uses its own org C,
    so an erase that wrongly succeeds cannot take org A's admin down with it."""
    with Session(engine) as s:
        c = Organization(name="Inv Org C")
        s.add(c)
        s.commit()
        cid = c.id
    uid = security.create_user("admin@c.test", "Admin C", Role.ADMIN, "", org_id=cid).id
    with Session(engine) as s:
        res = security.set_active(s, uid, False, org_id=access.UNSCOPED)
        assert not res["ok"] and res.get("reason") == "last_admin", res
        res = security.set_role(s, uid, Role.VIEWER, org_id=access.UNSCOPED)
        assert not res["ok"] and res.get("reason") == "last_admin", res
    _assert_still_admin("admin@c.test")
    ok, msg = gdpr.erase_user(uid, actor="operator@host.test")
    assert not ok and "last active administrator" in msg, (ok, msg)
    _assert_still_admin("admin@c.test")


def last_admin_positive_control(aid):
    """With a SECOND admin in org A the same CLI call goes through, so the refusal above
    is about the org's count and not a blanket ban."""
    security.create_user("admin2@a.test", "Admin A2", Role.ADMIN, PASSWORD, org_id=aid)
    code, out = cli(["--org", str(aid), "user", "disable", "admin2@a.test", "--yes"])
    assert code == 0, (code, out)
    assert not row("admin2@a.test").active


def last_admin_move_refused(aid, bid):
    """Gate multiorg-inv round 1, LOW-1: moving an org's last admin OUT of the org would
    leave it with no administrator, the outcome set_role, set_active and erase refuse. The
    operator CLI door and the service seam both refuse; a move that leaves another admin
    behind, and a same-org "move", go through."""
    from app.cli import _common

    with patch.object(_common, "_verify_operator_key", lambda: None):
        code, out = cli(["--operator", "user", "org", "admin@a.test", str(bid)])
    assert code != 0 and "last active administrator" in out, (code, out)
    assert row("admin@a.test").org_id == aid, row("admin@a.test").org_id
    _assert_still_admin("admin@a.test")
    cid = row("admin@c.test").org_id
    with Session(engine) as s:
        uid = row("admin@c.test").id
        try:
            access.move_user_to_org(s, uid, bid)
            raise AssertionError("the service moved org C's last admin out")
        except ValueError as exc:
            assert "last active administrator" in str(exc), exc
        # Same org: nothing leaves, so nothing to refuse.
        user, old = access.move_user_to_org(s, uid, cid)
        assert (user.org_id, old) == (cid, cid), (user.org_id, old)
    assert row("admin@c.test").org_id == cid
    # Positive control: a second active admin in org A may leave; admin@a.test stays.
    security.create_user("admin3@a.test", "Admin A3", Role.ADMIN, PASSWORD, org_id=aid)
    with patch.object(_common, "_verify_operator_key", lambda: None):
        code, out = cli(["--operator", "user", "org", "admin3@a.test", str(bid)])
    assert code == 0, (code, out)
    assert row("admin3@a.test").org_id == bid


def last_admin_gate_off_is_install_global(aid, bid):
    """Gate off, the install is the tenant: org B's admin still counts, exactly as before."""
    gate(False)
    try:
        uid = row("admin@a.test").id
        with Session(engine) as s:
            assert not security.is_last_admin_of_own_org(s, uid)
            res = security.set_role(s, uid, Role.MANAGER, org_id=None)
            assert res["ok"], res
            res = security.set_role(s, uid, Role.ADMIN, org_id=None)
            assert res["ok"], res
    finally:
        gate(True)


# --- #178-4: project membership, target resolved inside the project's org ---------------
def member_add_foreign_same_as_missing(aid, bid):
    """A project manager in org A adds an email that belongs to org B, and one that has no
    account. Both must be refused with the SAME answer (no existence oracle), and no
    membership row may appear. The CLI and /api already scoped the target; the web form
    resolved it install-wide."""
    security.create_user("member@b.test", "Foreign Member", Role.TRANSLATOR, "", org_id=bid)
    security.create_user("member@a.test", "Home Member", Role.TRANSLATOR, "", org_id=aid)
    with Session(engine) as s:
        p = Project(name="Inv Project A", org_id=aid, created_by="admin@a.test")
        s.add(p)
        s.commit()
        pid = p.id
    client = login("admin@a.test")

    def add(email):
        r = client.post(f"/projects/{pid}/members", data={"email": email}, follow_redirects=False)
        assert r.status_code == 303, (email, r.status_code)
        return r.headers["location"].replace(email.replace("@", "%40"), "EMAIL")

    foreign = add("member@b.test")
    missing = add("nobody@b.test")
    assert foreign == missing, ("existence oracle", foreign, missing)
    assert "member_ok=0" in foreign, foreign
    with Session(engine) as s:
        rows = s.exec(select(ProjectMember).where(ProjectMember.project_id == pid)).all()
        member_ids = {m.user_id for m in rows}
    assert row("member@b.test").id not in member_ids, "a foreign account became a member"
    # Positive control: a same-org account is added as before.
    assert "member_ok=1" in add("member@a.test")
    with Session(engine) as s:
        rows = s.exec(select(ProjectMember).where(ProjectMember.project_id == pid)).all()
    assert row("member@a.test").id in {m.user_id for m in rows}
    return pid


def member_add_service_every_surface(pid):
    """The ring lives in access.add_member itself, so every surface inherits it."""
    with Session(engine) as s:
        ok_f, msg_f = access.add_member(s, pid, "member@b.test")
        ok_m, msg_m = access.add_member(s, pid, "nobody2@b.test")
    assert not ok_f and not ok_m
    assert msg_f.replace("member@b.test", "X") == msg_m.replace("nobody2@b.test", "X")


# --- #178-5: self-service export is scoped to the caller's current org -----------------
def self_export_after_move(aid, bid):
    """A user authors an org-owned row in org A, is moved to org B, then downloads their own
    data. The copy must not carry org A's org-owned rows any more; their org B row stays."""
    security.create_user("mover@a.test", "Mover", Role.TRANSLATOR, PASSWORD, org_id=aid)
    with Session(engine) as s:
        s.add(Glossary(name="Former org glossary", org_id=aid, created_by="mover@a.test"))
        s.commit()
        u = s.exec(select(User).where(User.email == "mover@a.test")).one()
        access.move_user_to_org(s, u.id, bid)
        s.add(Glossary(name="Current org glossary", org_id=bid, created_by="mover@a.test"))
        s.commit()
    client = login("mover@a.test")
    r = client.get("/account/export")
    assert r.status_code == 200, r.status_code
    records = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    names = {rec.get("name") for rec in records if rec.get("section") == "glossaries"}
    assert "Former org glossary" not in names, "the export spilled the former org's rows"
    assert "Current org glossary" in names, names
    assert any(rec.get("section") == "user" for rec in records), "the subject row is missing"


def _seed_export_rows(org_id, uid, email, tag):
    """One row the subject is attributed on, in `org_id`, for every export section whose
    model has no org_id of its own or that the gate found unscoped (gate multiorg-inv
    round 1, LOW-2). Returns {section: id of that row}."""
    from app.models import (
        Connection,
        Customer,
        DatabricksExportRun,
        DeliveryEmptyApproval,
        Document,
        Job,
        KeyChangeLog,
        KeyImage,
        LanguageSet,
        LQAResult,
        MemoryEntry,
        MemoryEvent,
        ProjectGlossary,
        ProjectTmSource,
        QueryPost,
        QueryThread,
        Segment,
        SegmentPostEdit,
        StringKey,
        SyncLog,
        TapAction,
        TermCandidate,
        Workflow,
        WorkflowRun,
    )

    ids = {}
    with Session(engine) as s:

        def add(section, obj):
            s.add(obj)
            s.commit()
            s.refresh(obj)
            if section:
                ids[section] = obj.id
            return obj

        proj = add("projects", Project(name=f"proj-{tag}", created_by=email, org_id=org_id))
        doc = add(
            "documents", Document(project_id=proj.id, filename=f"{tag}.docx", created_by=email)
        )
        conn = add(
            "connections",
            Connection(kind="airtable", name=f"conn-{tag}", created_by=email, org_id=org_id),
        )
        cust = add(None, Customer(name=f"cust-{tag}", org_id=org_id))
        gl = add(None, Glossary(name=f"gl-{tag}", org_id=org_id, created_by="other@x.test"))
        wf = add(None, Workflow(name=f"wf-{tag}", org_id=org_id))
        key = add(None, StringKey(project_id=proj.id, name=f"key.{tag}"))
        seg = add(None, Segment(document_id=doc.id, target_locale="fr-FR"))
        add(
            "jobs",
            Job(kind="translate_document", created_by=email, payload={"project_id": proj.id}),
        )
        add(
            "jobs_json",
            Job(
                kind="connector_translate",
                created_by="scheduler",
                payload={"actor": email, "connection_id": conn.id},
            ),
        )
        add("language_sets", LanguageSet(name=f"ls-{tag}", created_by=email, org_id=org_id))
        add("project_members", ProjectMember(project_id=proj.id, user_id=uid, added_by="x@x.test"))
        add(
            "invites",
            Invite(
                email=f"invitee-{tag}@x.test", token=f"tok-{tag}", created_by=email, org_id=org_id
            ),
        )
        add("sync_logs", SyncLog(connection_id=conn.id, actor=email))
        add("memory_events", MemoryEvent(kind="edit", actor=email, org_id=org_id))
        add(
            "memory_entries",
            MemoryEntry(customer_id=cust.id, body=f"b-{tag}", created_by=email, org_id=org_id),
        )
        add(
            "databricks_runs",
            DatabricksExportRun(run_uuid=f"run-{tag}", actor=email, org_id=org_id),
        )
        add("tap_actions", TapAction(id=f"tap-{tag}", initiator=email, org_id=org_id))
        add(
            "project_tm_sources",
            ProjectTmSource(project_id=proj.id, source_project_id=proj.id, created_by=email),
        )
        add(
            "project_glossaries",
            ProjectGlossary(project_id=proj.id, glossary_id=gl.id, created_by=email),
        )
        add("key_change_logs", KeyChangeLog(project_id=proj.id, key_id=key.id, actor=email))
        add("key_images", KeyImage(key_id=key.id, project_id=proj.id, uploaded_by=email))
        add(
            "delivery_empty_approvals",
            DeliveryEmptyApproval(project_id=proj.id, locale="fr-FR", approved_by=email),
        )
        add("term_candidates", TermCandidate(glossary_id=gl.id, source_term=tag, decided_by=email))
        add("workflow_runs", WorkflowRun(workflow_id=wf.id, document_id=doc.id, created_by=email))
        add(
            "segment_post_edits",
            SegmentPostEdit(segment_id=seg.id, document_id=doc.id, actor=email),
        )
        thread = add(
            "query_threads",
            QueryThread(project_id=proj.id, author=email, subject=f"subject-{tag}"),
        )
        ids["query_thread_subjects"] = thread.id
        add("query_posts", QueryPost(thread_id=thread.id, author=email, body=f"body-{tag}"))
        add("lqa_findings", LQAResult(document_id=doc.id, actor=email, note=f"note-{tag}"))
    return ids


def self_export_every_section_after_move(aid, bid):
    """Gate multiorg-inv round 1, LOW-2: the self export of a moved user used to carry the
    former org's projects, documents, job payloads and every other section keyed on
    attribution only. Seed one row per such section in org A, move the user to org B, seed
    the same in org B: /account/export carries every org B row and no org A row. The
    operator's unscoped export (Art. 15 in full) still carries both."""
    from app.models import Job

    email = "mover-all@a.test"
    uid = security.create_user(email, "Mover All", Role.TRANSLATOR, PASSWORD, org_id=aid).id
    former = _seed_export_rows(aid, uid, email, "orgA")
    with Session(engine) as s:
        # A job naming no project, document or connection cannot be placed in an org.
        orphan = Job(kind="backfill_embeddings", created_by=email, payload={})
        s.add(orphan)
        s.commit()
        orphan_id = orphan.id
        access.move_user_to_org(s, uid, bid)
    current = _seed_export_rows(bid, uid, email, "orgB")

    def by_section(records):
        out = {}
        for rec in records:
            out.setdefault(rec.get("section"), set()).add(rec.get("id"))
        out["jobs_json"] = out.get("jobs", set())
        return out

    r = login(email).get("/account/export")
    assert r.status_code == 200, r.status_code
    got = by_section(json.loads(line) for line in r.text.splitlines() if line.strip())
    leaked = sorted(sec for sec, rid in former.items() if rid in got.get(sec, set()))
    missing = sorted(sec for sec, rid in current.items() if rid not in got.get(sec, set()))
    assert not leaked, f"the self export carried the former org's rows: {leaked}"
    assert not missing, f"the self export lost the current org's rows: {missing}"
    assert orphan_id not in got.get("jobs", set()), "an org-less job reached a scoped export"
    assert "orgA" not in r.text, "a former-org marker reached the download"
    assert "orgB" in r.text, "the current-org markers are missing"
    assert len(former) == len(current) >= 25, (len(former), len(current))

    full = by_section({"section": sec, **rec} for sec, rec in gdpr.export_user(uid))
    for sec, rid in {**former, **current}.items():
        assert rid in full.get(sec, set()), ("the operator export lost a row", sec)
    assert orphan_id in full["jobs"], "the operator export lost the org-less job"


# --- #181: an account must name an organization, enforced once at the seam -------------
def orgless_service_seam(aid):
    for org in (None, access.BLOCKED_ORG_ID, access.UNSCOPED):
        try:
            security.create_user("orphan@a.test", "Orphan", Role.TRANSLATOR, PASSWORD, org_id=org)
        except security.OrglessAccountError:
            pass
        else:
            raise AssertionError(f"create_user minted an org-less account for org_id={org!r}")
        try:
            security.create_invite("orphan@a.test", Role.TRANSLATOR, "operator", org_id=org)
        except security.OrglessAccountError:
            pass
        else:
            raise AssertionError(f"create_invite issued an org-less invitation for {org!r}")
    assert row("orphan@a.test") is None
    # The legacy created_by resolution still stamps the inviter's org.
    inv = security.create_invite("invitee@a.test", Role.TRANSLATOR, "admin@a.test")
    assert inv.org_id == aid, inv.org_id
    # The explicit boot exemption is the only way past it, and only on purpose.
    boot = security.create_user(
        "boot@local", "Boot", Role.VIEWER, "", enforce_policy=False, allow_orgless_boot=True
    )
    assert boot.org_id is None
    # Gate off: no-op, byte-identical to before.
    gate(False)
    try:
        u = security.create_user("single@a.test", "Single", Role.TRANSLATOR, PASSWORD)
        assert u.org_id is None
    finally:
        gate(True)


def _wall_down():
    """Simulate the future carve-out #181 warns about: the token wall and the login wall
    stop refusing an org-less principal. The seam must still hold."""
    return patch.object(access, "user_org_active", lambda _u: True)


def orgless_api_door_without_the_wall():
    """app/web/api_cli_users.py created the account org-less and stamped afterwards, so an
    org-less caller minted an orphan held back only by the token wall."""
    with Session(engine) as s:
        s.add(User(email="nullorg-admin@x.test", role=Role.ADMIN, org_id=None))
        s.commit()
        uid = s.exec(select(User.id).where(User.email == "nullorg-admin@x.test")).one()
    _row, raw = apikeys.create_key(uid, "inv", scopes="api docs admin")
    api = TestClient(app)
    with _wall_down():
        r = api.post(
            "/api/users",
            headers={"X-Loc-Token": raw},
            json={"email": "api-orphan@x.test", "role": Role.TRANSLATOR},
        )
        assert r.status_code == 400, (r.status_code, r.text[:300])
        assert row("api-orphan@x.test") is None, "the /api door minted an org-less account"
        r = api.post(
            "/api/users/invite",
            headers={"X-Loc-Token": raw},
            json={"email": "api-orphan@x.test", "role": Role.TRANSLATOR},
        )
        assert r.status_code == 400, (r.status_code, r.text[:300])


def orgless_web_door_without_the_wall():
    """app/web/admin.py POST /users and the invite form: same shape, held back only by the
    login wall."""
    with Session(engine) as s:
        u = User(email="nullorg-web@x.test", role=Role.ADMIN, org_id=None)
        s.add(u)
        s.commit()
        uid = u.id
    from app.services import password_policy

    with Session(engine) as s:
        u = s.get(User, uid)
        password_policy.record_password(s, u, PASSWORD)
        s.commit()
    with _wall_down():
        client = login("nullorg-web@x.test")
        r = client.post(
            "/users",
            data={"email": "web-orphan@x.test", "role": Role.TRANSLATOR, LOGIN_FIELD: PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
        assert row("web-orphan@x.test") is None, "the web door minted an org-less account"
        r = client.post(
            "/users/invite",
            data={"email": "web-orphan@x.test", "role": Role.TRANSLATOR},
            follow_redirects=False,
        )
        assert "error=" in r.headers.get("location", ""), (r.status_code, r.headers)
    with Session(engine) as s:
        invites = s.exec(select(Invite).where(Invite.email == "web-orphan@x.test")).all()
    assert not invites, "the web door issued an org-less invitation"


def orgless_api_create_list_authenticate(aid):
    """The /api door now stamps at creation instead of afterwards; drive the account it
    makes through create -> list -> authenticate so the change is proven end to end."""
    uid = row("admin@a.test").id
    _row, raw = apikeys.create_key(uid, "inv-a", scopes="api docs admin")
    api = TestClient(app)
    h = {"X-Loc-Token": raw}
    r = api.post(
        "/api/users",
        headers=h,
        json={"email": "api-made@a.test", "role": Role.TRANSLATOR, LOGIN_FIELD: PASSWORD},
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert row("api-made@a.test").org_id == aid
    listed = [u["email"] for u in api.get("/api/users", headers=h).json()["users"]]
    assert "api-made@a.test" in listed, listed
    login("api-made@a.test")


# --- #181 ratchet: every account-creating call site is inventoried ----------------------
# (path, enclosing function, callee) -> (count, kind, reason). kind:
#   seam      reaches security.create_user / create_invite, which refuses an org-less
#             create under an active gate (refuse_orgless_account). No exemption.
#   boot      passes allow_orgless_boot=True on purpose. Must be a boot path.
#   construct builds User( directly, bypassing the seam. Must say why that is safe.
CREATE_CALL_SITES = {
    ("app/cli/cmd_user.py", "_user_create", "create_user"): (
        1,
        "seam",
        "cli user create; passes the --org scope, maps OrglessAccountError to its --org hint",
    ),
    ("app/cli/cmd_user.py", "_user_invite", "create_invite"): (
        1,
        "seam",
        "cli user invite; same --org hint",
    ),
    ("app/services/ldap_auth.py", "get_or_create_ldap_user", "create_user"): (
        1,
        "seam",
        "LDAP JIT; maps OrglessAccountError to NOT_PROVISIONED",
    ),
    ("app/services/scim.py", "create_user", "create_user"): (
        1,
        "seam",
        "SCIM create; the org is the SCIM key's org, always set",
    ),
    ("app/services/security.py", "get_or_create_sso_user", "create_user"): (
        1,
        "seam",
        "OIDC JIT; maps OrglessAccountError to its not-provisioned message",
    ),
    ("app/services/security.py", "get_or_create_saml_user", "create_user"): (
        1,
        "seam",
        "SAML JIT; maps OrglessAccountError to its not-provisioned message",
    ),
    ("app/services/security.py", "accept_invite", "create_user"): (
        1,
        "seam",
        "invite acceptance; stamps the invitation's org (and refuses an org-less one first)",
    ),
    ("app/services/security.py", "seed_initial", "create_user"): (
        1,
        "boot",
        "dev seed on an EMPTY install (user_count() == 0) at boot, before any org context",
    ),
    ("app/services/security.py", "seed_admin", "create_user"): (
        1,
        "boot",
        "first admin on an EMPTY install (init CLI, env seeding); no org context exists yet",
    ),
    ("app/services/security.py", "ensure_linguist", "create_user"): (
        1,
        "boot",
        "boot-time persona backfill (main._startup), not a public create",
    ),
    ("app/services/security_client_ip_one_authority_.py", "create_user", "User"): (
        1,
        "construct",
        "the seam itself: refuse_orgless_account runs before this construct",
    ),
    ("app/services/trust_auth.py", "_provision_user", "User"): (
        1,
        "construct",
        "trust principal; _resolve_org REQUIRES an existing org under multi-org (boot fails "
        "without LOC_TMS_TRUST_ORG_ID), so it never constructs an org-less row under the gate",
    ),
    ("app/web/admin.py", "users_create", "create_user"): (
        1,
        "seam",
        "POST /users; stamps the caller's org",
    ),
    ("app/web/admin.py", "users_invite", "create_invite"): (
        1,
        "seam",
        "POST /users/invite; stamps the caller's org",
    ),
    ("app/web/api_cli_users.py", "api_user_create", "create_user"): (
        1,
        "seam",
        "POST /api/users; stamps the caller's org at creation",
    ),
    ("app/web/api_cli_users.py", "api_user_invite", "create_invite"): (
        1,
        "seam",
        "POST /api/users/invite; stamps the caller's org",
    ),
    ("app/web/auth.py", "auth_setup_post", "create_user"): (
        1,
        "boot",
        "first-run setup wizard; refuses unless user_count() == 0, no org context exists yet",
    ),
    ("app/web/scim.py", "create_user", "create_user"): (
        1,
        "seam",
        "SCIM route; delegates to scim.create_user (listed above) with the key's org",
    ),
}
_CALLEES = {"create_user", "create_invite", "User"}


class _CreateCallScan(ast.NodeVisitor):
    """Collects, for one file, every call to a name in _CALLEES keyed by its enclosing
    function, whether it passes the boot exemption, and any aliased import of a callee."""

    def __init__(self, rel, found, boot_flags, aliases):
        self.rel, self.found, self.boot_flags, self.aliases = rel, found, boot_flags, aliases
        self.stack = []

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name in _CALLEES and a.asname and a.asname != a.name:
                self.aliases.append((self.rel, node.lineno, a.name, a.asname))

    def visit_Call(self, node):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name in _CALLEES:
            key = (self.rel, self.stack[-1] if self.stack else "<module>", name)
            self.found[key] = self.found.get(key, 0) + 1
            kws = {k.arg: k.value for k in node.keywords}
            if None in kws:
                self.boot_flags[key] = "**kwargs"  # hides the flag from review
            elif "allow_orgless_boot" in kws:
                v = kws["allow_orgless_boot"]
                literal_true = isinstance(v, ast.Constant) and v.value is True
                self.boot_flags[key] = "true" if literal_true else "non-literal"
        self.generic_visit(node)


def _scan_create_calls():
    found, boot_flags, aliases = {}, {}, []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_bytes().decode("utf-8"), filename=rel)
        _CreateCallScan(rel, found, boot_flags, aliases).visit(tree)
    return found, boot_flags, aliases


def create_call_ratchet():
    """#181's ratchet: FAIL on a new, unlisted site. It fails on absence, which is the
    shape of check that has actually caught things in this effort."""
    found, boot_flags, aliases = _scan_create_calls()
    assert not aliases, f"an aliased import hides a call site from this scan: {aliases}"
    unlisted = sorted(set(found) - set(CREATE_CALL_SITES))
    assert not unlisted, (
        "new call site(s) reaching create_user / create_invite or constructing User( "
        f"directly: {unlisted}. Route it through the guarded seam, or add it to "
        "CREATE_CALL_SITES with a written reason."
    )
    stale = sorted(set(CREATE_CALL_SITES) - set(found))
    assert not stale, f"CREATE_CALL_SITES lists sites that no longer exist: {stale}"
    for key, (count, kind, reason) in CREATE_CALL_SITES.items():
        assert reason.strip(), key
        assert found[key] == count, (key, "count", found[key], count)
        flag = boot_flags.get(key)
        if kind == "boot":
            assert flag == "true", (key, "a boot site must pass allow_orgless_boot=True", flag)
        else:
            assert flag is None, (key, f"only a listed boot site may pass the exemption ({flag})")
    # The seam really is guarded: refuse_orgless_account runs before the User( construct.
    src = (ROOT / "app/services/security_client_ip_one_authority_.py").read_bytes().decode()
    tree = ast.parse(src)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("create_user", "create_invite"):
        calls = [
            n.lineno
            for n in ast.walk(fns[name])
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "refuse_orgless_account"
        ]
        assert calls, f"{name} no longer calls refuse_orgless_account"
        writes = [
            n.lineno
            for n in ast.walk(fns[name])
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("User", "Invite")
        ]
        assert writes and min(calls) < min(writes), f"{name}: the guard must precede the write"
    print(f"  ratchet: {len(found)} call sites inventoried ({sum(found.values())} calls)")


def main():
    logging.disable(logging.CRITICAL)
    aid, bid = setup()
    cases = [
        ("create_call_ratchet", create_call_ratchet),
        ("last_admin_cli_scoped", lambda: last_admin_cli_scoped(aid)),
        ("last_admin_cli_operator", last_admin_cli_operator),
        ("last_admin_service_seam", last_admin_service_seam),
        ("last_admin_positive_control", lambda: last_admin_positive_control(aid)),
        ("last_admin_move_refused", lambda: last_admin_move_refused(aid, bid)),
        ("last_admin_gate_off", lambda: last_admin_gate_off_is_install_global(aid, bid)),
        (
            "member_add_foreign_same_as_missing",
            lambda: member_add_foreign_same_as_missing(aid, bid),
        ),
        ("self_export_after_move", lambda: self_export_after_move(aid, bid)),
        (
            "self_export_every_section_after_move",
            lambda: self_export_every_section_after_move(aid, bid),
        ),
        ("orgless_service_seam", lambda: orgless_service_seam(aid)),
        ("orgless_api_door_without_the_wall", orgless_api_door_without_the_wall),
        ("orgless_web_door_without_the_wall", orgless_web_door_without_the_wall),
        ("orgless_api_create_list_authenticate", lambda: orgless_api_create_list_authenticate(aid)),
    ]
    print("collected ids: " + ", ".join(name for name, _ in cases))
    failures = []
    state = {}
    for name, case in cases:
        try:
            result = case()
            if name == "member_add_foreign_same_as_missing":
                state["pid"] = result
            print(f"PASS {name}")
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    if "pid" in state:
        try:
            member_add_service_every_surface(state["pid"])
            print("PASS member_add_service_every_surface")
        except Exception as exc:
            failures.append("member_add_service_every_surface")
            print(f"FAIL member_add_service_every_surface: {type(exc).__name__}: {exc}")
    else:
        failures.append("member_add_service_every_surface (no project)")
    assert not failures, failures
    print("MULTIORG INV OK")


if __name__ == "__main__":
    main()
