"""Connected projects (T4: convert, shape on bind, first pull, follow-ups) verification (offline).

Proves, on top of the T1 suite (`verify_connected_projects`):

- the FIRST PULL of a connection with no project (`_ensure_project`, every kind that reaches it)
  creates a connected project bound to that same connection, not scheduled, with the connection's
  languages; with the automation module off it stays the legacy project
- "Make this a connected project" binds the connection that already feeds an existing project:
  no lane, no connection change, no schedule, no state change; refused for foreign uploads, inbox,
  archived, already connected or bound, several candidates without a pick, a connection another
  project runs on; on the service, /api (ring, role), web, CLI (in-process, --remote) and MCP
- an UPGRADE changes nothing: a legacy connection-fed project is untouched by a second boot
- shape on bind: a keys kind (github) on an EMPTY connected project makes it key_based (audited);
  mismatches are refused with a message; plain projects never change shape
- key add / upsert / import are refused on a connected project on every surface
- create-from-template can make a connected project (web, /api, CLI) and never binds
- the Airtable and Jira OAuth round trip carries a validated return_to in the server-side session
"""

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

init_env("loctms_cconvert_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Connection,
    Document,
    Project,
    ProjectAutomation,
    Role,
    StringKey,
    TargetLocale,
)
from app.services import (
    apikeys,
    content,
    content_projects,
    security,
    settings,
)
from app.services import (
    connected_projects as cp,
)
from app.services import (
    project_automation as pa,
)
from app.services import (
    templates as tpl,
)
from app.services.connectors import store as cstore

PW = "pw-fixture-5521"


def mk_customer(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def mk_conn(name, customer_id=None, kind="zendesk", cfg=None, secret=None):
    base = {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de, fr"}
    return cstore.create(
        kind,
        name,
        cfg or base,
        secret if secret is not None else {"email": "a@acme.com", "api_token": "tok-fixture"},
        actor="t",
        customer_id=customer_id,
    )


def mk_project(name, intake=None, customer_id=None, targets="de, fr", **kw):
    with Session(engine) as s:
        p = content_projects.project_create(
            s,
            name,
            "en-US",
            targets,
            customer_id=customer_id,
            created_by="t@x.co",
            intake=intake,
            **kw,
        )
        return p.id


def feed(conn_id, pid):
    """The legacy way a connection feeds a project: Connection.project_id, no binding."""
    with Session(engine) as s:
        c = s.get(Connection, conn_id)
        c.project_id = pid
        s.add(c)
        s.commit()


def row(pid):
    with Session(engine) as s:
        return s.get(Project, pid)


def auto_of(pid):
    with Session(engine) as s:
        return pa.get(s, pid)


def n_conns():
    with Session(engine) as s:
        return len(s.exec(select(Connection)).all())


def snapshot_conn(cid):
    with Session(engine) as s:
        c = s.get(Connection, cid)
        return (dict(c.config or {}), c.project_id, c.status, c.secret_enc)


def audits(action):
    with Session(engine) as s:
        return [a for a in s.exec(select(AuditLog)).all() if a.action == action]


def cli(*argv):
    from app.cli.__main__ import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = main(["--json", *argv])
        except SystemExit as e:
            code = e.code
    out = buf.getvalue().strip()
    try:
        return code, (json.loads(out.splitlines()[-1]) if out else {})
    except ValueError:
        return code, {"raw": out}


def refused(fn, needle=""):
    try:
        fn()
    except cp.IntakeError as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError("expected IntakeError")


# ---------------------------------------------------------------------------------------
def test_first_pull_creates_connected_bound():
    from app.services.connectors import store_part2 as sp2

    for kind, cfg, secret in (
        ("zendesk", None, None),
        (
            "airtable",
            {
                "base_id": "appABC",
                "table": "T",
                "source_locale": "en-US",
                "target_locales": "de-DE, fr-FR",
            },
            {"api_token": "tok-fixture"},
        ),
        (
            "jira",
            {
                "base_url": "https://t.atlassian.net",
                "source_locale": "en-US",
                "target_locales": "de-DE, fr-FR",
            },
            {"email": "a@acme.com", "api_token": "tok-fixture"},
        ),
    ):
        c = mk_conn(f"FP {kind}", kind=kind, cfg=cfg, secret=secret)
        with Session(engine) as s:
            conn = s.get(Connection, c.id)
            proj = sp2._ensure_project(s, conn)
            pid = proj.id
        p = row(pid)
        assert p.intake == "connected" and p.project_type == "document", (kind, p.intake)
        a = auto_of(pid)
        assert a is not None, f"{kind}: first pull must bind"
        assert a.connection_id == c.id and a.run_connection_id == c.id, (kind, "no lane")
        assert a.enabled is False and not a.times and not a.interval_minutes, kind
        assert a.next_at is None and a.last_at is None, "nothing scheduled, nothing run"
        with Session(engine) as s:
            locs = sorted(
                t.locale for t in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid))
            )
        assert len(locs) == 2, (kind, locs)
        assert snapshot_conn(c.id)[1] == pid, "the connection still points at its project"
        # a second call is the same project (idempotent), still one binding
        with Session(engine) as s:
            again = sp2._ensure_project(s, s.get(Connection, c.id))
            assert again.id == pid
            assert len(s.exec(select(ProjectAutomation)).all()) >= 1
    assert audits("automation.bind"), "the bind is audited"

    # the flow's own entry point (_new_connector_doc) goes through the same path
    c = mk_conn("FP doc")
    with Session(engine) as s:
        doc = sp2._new_connector_doc(s, s.get(Connection, c.id), None, "x.txt", "t")
        pid = doc.project_id
    assert row(pid).intake == "connected" and auto_of(pid) is not None

    # module off: the legacy project, byte-identical (no intake, no binding)
    settings.set_many({"project_automation_enabled": "0"})
    try:
        c = mk_conn("FP off")
        with Session(engine) as s:
            pid = sp2._ensure_project(s, s.get(Connection, c.id)).id
        assert row(pid).intake == "upload" and auto_of(pid) is None
    finally:
        settings.set_many({"project_automation_enabled": "1"})

    # a connection another project already runs on is not double-bound
    c = mk_conn("FP shared")
    other = mk_project("FP other")
    pa.bind(other, c.id, actor="t", is_admin=True)
    with Session(engine) as s:
        conn = s.get(Connection, c.id)
        conn.project_id = None
        s.add(conn)
        s.commit()
    with Session(engine) as s:
        pid = sp2._ensure_project(s, s.get(Connection, c.id)).id
    assert pid != other and auto_of(pid) is None and row(pid).intake == "upload"
    print("  first pull: connected + bound (not scheduled) for zendesk, airtable, jira; module off")


def test_first_pull_keys_kind_shape():
    from app.services.connectors import store_part2 as sp2

    c = mk_conn(
        "FP git",
        kind="github",
        cfg={"repo": "acme/app", "source_locale": "en-US", "target_locales": "de-DE"},
        secret={"token": "tok-fixture"},
    )
    with Session(engine) as s:
        pid = sp2._ensure_project(s, s.get(Connection, c.id)).id
    p = row(pid)
    assert p.intake == "connected" and p.project_type == "key_based", (p.intake, p.project_type)
    assert auto_of(pid) is not None
    print("  first pull: a keys kind (github) makes the empty connected project key_based")


def test_upgrade_changes_nothing():
    """A legacy install: connections feed projects through Connection.project_id with no binding.
    Booting again (init_db: alembic + shim) must not bind, convert or schedule anything."""
    cust = mk_customer("UP Cust")
    pid = mk_project("UP legacy", customer_id=cust)
    c = mk_conn("UP conn", customer_id=cust)
    feed(c.id, pid)
    with Session(engine) as s:
        s.add(
            Document(
                project_id=pid,
                filename="legacy.txt",
                fmt="zendesk",
                skeleton={"connector_id": c.id},
            )
        )
        s.commit()
    before = (snapshot_conn(c.id), row(pid).intake, row(pid).project_type, n_conns())
    n_auto = None
    with Session(engine) as s:
        n_auto = len(s.exec(select(ProjectAutomation)).all())
    init_db()
    init_db()
    after = (snapshot_conn(c.id), row(pid).intake, row(pid).project_type, n_conns())
    assert before == after, (before, after)
    assert auto_of(pid) is None, "an upgrade never binds an existing project"
    with Session(engine) as s:
        assert len(s.exec(select(ProjectAutomation)).all()) == n_auto
    assert row(pid).intake == "upload"
    # the project page offers the button but does not do it
    assert cp_offer(pid), "the offer exists"
    print("  upgrade: a legacy connection-fed project is unchanged after boot; offer only")


def test_opus_gate_additions():
    """Review gate (T4): the git trio's own first pull (git_core._ensure_key_project) also makes a
    connected, bound, unscheduled key project; a first pull never double-binds a connection that
    already feeds another project's lane; "Make this a connected project" refuses an Airtable or
    Jira (no per-item claims) connection that already feeds another project's lane; a full app
    boot (lifespan) plus scheduler and automation ticks still change nothing on a legacy install."""
    from app.services import scheduler
    from app.services.connectors import git_core
    from app.services.connectors import store_part2 as sp2

    # 1. git first pull -> connected + bound key project, nothing scheduled
    c = mk_conn(
        "GG git",
        kind="github",
        cfg={
            "repo": "acme/app",
            "source_locale": "en-US",
            "target_locales": "de-DE",
            "source_paths": "locales/{locale}.json",
        },
        secret={"token": "tok-fixture"},
    )
    st = git_core.load_settings(cstore.get(c.id).config, c.id)
    pid = git_core._ensure_key_project(cstore.get(c.id), st, ["de-DE"], "t")
    p = row(pid)
    assert p.intake == "connected" and p.project_type == "key_based", (p.intake, p.project_type)
    a = auto_of(pid)
    assert a is not None and a.connection_id == c.id and a.run_connection_id == c.id
    assert a.enabled is False and a.next_at is None and not a.times and not a.interval_minutes
    assert snapshot_conn(c.id)[1] == pid
    # the next pull reuses it, still one binding
    assert git_core._ensure_key_project(cstore.get(c.id), st, ["de-DE"], "t") == pid
    with Session(engine) as s:
        assert (
            len(
                s.exec(
                    select(ProjectAutomation).where(ProjectAutomation.run_connection_id == c.id)
                ).all()
            )
            == 1
        )
    # module off: the legacy key project
    settings.set_many({"project_automation_enabled": "0"})
    try:
        c2 = mk_conn(
            "GG git off",
            kind="github",
            cfg={
                "repo": "acme/app2",
                "source_locale": "en-US",
                "target_locales": "de-DE",
                "source_paths": "locales/{locale}.json",
            },
            secret={"token": "tok-fixture"},
        )
        st2 = git_core.load_settings(cstore.get(c2.id).config, c2.id)
        pid2 = git_core._ensure_key_project(cstore.get(c2.id), st2, ["de-DE"], "t")
        assert row(pid2).intake == "upload" and auto_of(pid2) is None
    finally:
        settings.set_many({"project_automation_enabled": "1"})

    # 2. a connection that already feeds another project's LANE is never bound again by a pull
    feeder = mk_conn("GG feeder")
    lane_owner = mk_project("GG lane owner")
    pa.bind(lane_owner, feeder.id, actor="t", is_admin=True, clone=True)
    assert auto_of(lane_owner).run_connection_id != feeder.id, "a lane was made"
    assert snapshot_conn(feeder.id)[1] is None, "the feeder itself feeds no project"
    with Session(engine) as s:
        pid3 = sp2._ensure_project(s, s.get(Connection, feeder.id)).id
    assert auto_of(pid3) is None and row(pid3).intake == "upload", "no second binding"

    # 3. make-connected refuses a no-claims kind whose connection feeds another project's lane
    cust = mk_customer("GG Cust")
    at = mk_conn(
        "GG airtable",
        customer_id=cust,
        kind="airtable",
        cfg={
            "base_id": "appGG",
            "table": "T",
            "source_locale": "en-US",
            "target_locales": "de-DE",
        },
        secret={"api_token": "tok-fixture"},
    )
    other = mk_project("GG other", customer_id=cust)
    pa.bind(other, at.id, actor="t", is_admin=True, clone=True)
    legacy = mk_project("GG legacy at", customer_id=cust)
    feed(at.id, legacy)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, legacy)), "one project")
    assert auto_of(legacy) is None and row(legacy).intake == "upload"

    # 4. a full boot (app lifespan) and both schedulers change nothing on a legacy install
    lp = mk_project("GG boot legacy", customer_id=cust)
    lc = mk_conn("GG boot conn", customer_id=cust)
    feed(lc.id, lp)
    with Session(engine) as s:
        n_auto = len(s.exec(select(ProjectAutomation)).all())
    before = (snapshot_conn(lc.id), row(lp).intake, row(lp).project_type, n_conns())
    with TestClient(app):
        pass
    scheduler.tick()
    pa.tick()
    after = (snapshot_conn(lc.id), row(lp).intake, row(lp).project_type, n_conns())
    assert before == after, (before, after)
    with Session(engine) as s:
        assert len(s.exec(select(ProjectAutomation)).all()) == n_auto
    assert auto_of(lp) is None
    print("  gate: git first pull connected; no double bind; no-claims lane refusal; boot inert")


def cp_offer(pid):
    with Session(engine) as s:
        return cp.convert_offer(s, s.get(Project, pid))


def test_convert_service():
    cust = mk_customer("CV Cust")
    pid = mk_project("CV legacy", customer_id=cust)
    c = mk_conn("CV conn", customer_id=cust)
    feed(c.id, pid)
    with Session(engine) as s:  # documents pulled by THIS connection are fine
        s.add(
            Document(
                project_id=pid, filename="a.txt", fmt="zendesk", skeleton={"connector_id": c.id}
            )
        )
        s.add(Document(project_id=pid, filename="b.txt", fmt="zendesk"))
        s.commit()
    langs_before = sorted(t.locale for t in _tl(pid))
    snap = snapshot_conn(c.id)
    nc = n_conns()
    with Session(engine) as s:
        a = cp.convert_existing(s, s.get(Project, pid), actor="mgr@x.co")
        assert a.connection_id == c.id and a.run_connection_id == c.id
    p = row(pid)
    assert p.intake == "connected" and p.project_type == "document"
    a = auto_of(pid)
    assert a.enabled is False and not a.times and not a.interval_minutes and a.next_at is None
    assert a.last_at is None and a.last_status == "" and a.scope is None
    assert snapshot_conn(c.id) == snap, "the connection is not touched"
    assert n_conns() == nc, "no lane, no new connection"
    assert sorted(t.locale for t in _tl(pid)) == langs_before, "languages unchanged"
    assert audits("project.made_connected")
    # converting again is refused (already connected)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, pid)), "already")
    print("  convert (service): same connection bound, no lane, no schedule, no state change")


def _tl(pid):
    with Session(engine) as s:
        return s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all()


def test_convert_refusals():
    cust = mk_customer("CR Cust")
    # uploaded document not from the connection
    p1 = mk_project("CR upload", customer_id=cust)
    c1 = mk_conn("CR c1", customer_id=cust)
    feed(c1.id, p1)
    with Session(engine) as s:
        s.add(Document(project_id=p1, filename="uploaded.docx", fmt="docx"))
        s.commit()
    with Session(engine) as s:
        msg = refused(lambda: cp.convert_existing(s, s.get(Project, p1)), "uploaded")
        assert "1 document" in msg
    assert row(p1).intake == "upload" and auto_of(p1) is None
    # a document of the same kind from ANOTHER connection is foreign too
    p1b = mk_project("CR other conn doc", customer_id=cust)
    c1b = mk_conn("CR c1b", customer_id=cust)
    feed(c1b.id, p1b)
    with Session(engine) as s:
        s.add(
            Document(
                project_id=p1b, filename="z.txt", fmt="zendesk", skeleton={"connector_id": c1.id}
            )
        )
        s.commit()
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p1b)), "uploaded")
    # archived
    p2 = mk_project("CR archived", customer_id=cust)
    c2 = mk_conn("CR c2", customer_id=cust)
    feed(c2.id, p2)
    with Session(engine) as s:
        content.archive_project(s, p2, actor="t") if hasattr(content, "archive_project") else None
        pr = s.get(Project, p2)
        if pr.archived_at is None:
            from datetime import datetime, timezone

            pr.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(pr)
            s.commit()
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p2)), "archived")
    # inbox
    p3 = mk_project("CR inbox", customer_id=cust)
    c3 = mk_conn("CR c3", customer_id=cust)
    feed(c3.id, p3)
    with Session(engine) as s:
        pr = s.get(Project, p3)
        pr.inbox_key = "dept:cr"
        s.add(pr)
        s.commit()
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p3)), "inbox")
    # no connection feeds it
    p4 = mk_project("CR none", customer_id=cust)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p4)), "no connection")
    assert cp_offer(p4) == [], "no button without a feeding connection"
    # already bound (a plain project with a binding)
    p5 = mk_project("CR bound", customer_id=cust)
    c5 = mk_conn("CR c5", customer_id=cust)
    pa.bind(p5, c5.id, actor="t", is_admin=True)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p5)), "content source")
    # already connected (created connected)
    p6 = mk_project("CR connected", intake="connected", customer_id=cust)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p6)), "already")
    # two connections feed one project: a pick is required, a wrong pick refused
    p7 = mk_project("CR two", customer_id=cust)
    ca, cb = mk_conn("CR a", customer_id=cust), mk_conn("CR b", customer_id=cust)
    feed(ca.id, p7)
    feed(cb.id, p7)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p7)), "more than one")
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p7), 999999), "does not feed")
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p7), "٣"), "does not feed")
    with Session(engine) as s:
        a = cp.convert_existing(s, s.get(Project, p7), cb.id)
        assert a.connection_id == cb.id
    # a connection another project runs on
    p8 = mk_project("CR taken", customer_id=cust)
    c8 = mk_conn("CR c8", customer_id=cust)
    pa.bind(mk_project("CR owner", customer_id=cust), c8.id, actor="t", is_admin=True)
    feed(c8.id, p8)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p8)), "another project")
    # a connection of another customer cannot be used
    p9 = mk_project("CR cust", customer_id=cust)
    other = mk_customer("CR Other")
    c9 = mk_conn("CR c9", customer_id=other)
    feed(c9.id, p9)
    with Session(engine) as s:
        refused(lambda: cp.convert_existing(s, s.get(Project, p9)), "cannot be used")
    # module off
    settings.set_many({"project_automation_enabled": "0"})
    try:
        p10 = mk_project("CR off", customer_id=cust)
        c10 = mk_conn("CR c10", customer_id=cust)
        feed(c10.id, p10)
        with Session(engine) as s:
            refused(lambda: cp.convert_existing(s, s.get(Project, p10)), "switched off")
        assert cp_offer(p10) == []
    finally:
        settings.set_many({"project_automation_enabled": "1"})
    # a failed convert changed nothing
    for pid in (p1, p1b, p2, p3, p4):
        assert row(pid).intake == "upload" and auto_of(pid) is None, pid
    print(
        "  convert refusals: uploads, foreign docs, archived, inbox, none, bound, ambiguous, taken"
    )


def test_shape_on_bind():
    cust = mk_customer("SH Cust")
    # keys kind on an empty connected project -> key_based, audited
    pid = mk_project("SH empty", intake="connected", customer_id=cust)
    gh = mk_conn(
        "SH gh",
        customer_id=cust,
        kind="github",
        cfg={"repo": "acme/app", "source_locale": "en-US", "target_locales": "de-DE"},
        secret={"token": "tok-fixture"},
    )
    assert row(pid).project_type == "document"
    pa.bind(pid, gh.id, actor="t@x.co", is_admin=True)
    assert row(pid).project_type == "key_based"
    assert audits("project.type_set"), "the shape change is audited"
    # a plain (upload) project keeps its shape when a git connection is bound
    plain = mk_project("SH plain", customer_id=cust)
    gh2 = mk_conn(
        "SH gh2",
        customer_id=cust,
        kind="github",
        cfg={"repo": "acme/app2", "source_locale": "en-US", "target_locales": "de-DE"},
        secret={"token": "tok-fixture"},
    )
    pa.bind(plain, gh2.id, actor="t@x.co", is_admin=True)
    assert row(plain).project_type == "document", "only connected projects change shape"
    # keys kind on a connected project that holds documents -> refused, nothing created
    docp = mk_project("SH docs", intake="connected", customer_id=cust)
    with Session(engine) as s:
        s.add(Document(project_id=docp, filename="a.txt"))
        s.commit()
    gh3 = mk_conn(
        "SH gh3",
        customer_id=cust,
        kind="github",
        cfg={"repo": "acme/app3", "source_locale": "en-US", "target_locales": "de-DE"},
        secret={"token": "tok-fixture"},
    )
    nc = n_conns()
    try:
        pa.bind(docp, gh3.id, actor="t", is_admin=True)
    except pa.AutomationError as exc:
        assert exc.status == 400 and "keys" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("keys kind on a non-empty document project must be refused")
    assert auto_of(docp) is None and row(docp).project_type == "document" and n_conns() == nc
    # the same refusal before a NEW connection is created by bind
    try:
        pa.bind(
            docp,
            None,
            actor="t",
            new_connection={"kind": "github", "name": "n", "config": {"repo": "a/b"}},
        )
    except pa.AutomationError as exc:
        assert exc.status == 400, str(exc)
    else:
        raise AssertionError("must refuse before creating a connection")
    assert n_conns() == nc, "no connection created for a refused shape"
    # document kind on a key_based project that HAS keys -> refused
    from app.services import keys as keys_svc

    with Session(engine) as s:
        keys_svc.create_key(s, s.get(Project, pid), "greeting", base_value="Hello")
    pa.unbind(pid, actor="t")
    zd = mk_conn("SH zd", customer_id=cust)
    try:
        pa.bind(pid, zd.id, actor="t", is_admin=True)
    except pa.AutomationError as exc:
        assert exc.status == 400 and "key" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("document kind on a key project with keys must be refused")
    assert row(pid).project_type == "key_based" and auto_of(pid) is None
    # an EMPTY key_based connected project may go back to documents
    empty_k = mk_project("SH back", intake="connected", customer_id=cust)
    pa.bind(
        empty_k,
        mk_conn(
            "SH gh4",
            customer_id=cust,
            kind="github",
            cfg={"repo": "acme/app4", "source_locale": "en-US", "target_locales": "de-DE"},
            secret={"token": "tok-fixture"},
        ).id,
        actor="t",
        is_admin=True,
    )
    assert row(empty_k).project_type == "key_based"
    pa.unbind(empty_k, actor="t")
    pa.bind(empty_k, mk_conn("SH zd2", customer_id=cust).id, actor="t", is_admin=True)
    assert row(empty_k).project_type == "document"
    # keys switched off: a keys source cannot reshape the project
    settings.set_many({"key_projects_enabled": "0"})
    try:
        offp = mk_project("SH keys off", intake="connected", customer_id=cust)
        gh5 = mk_conn(
            "SH gh5",
            customer_id=cust,
            kind="github",
            cfg={"repo": "acme/app5", "source_locale": "en-US", "target_locales": "de-DE"},
            secret={"token": "tok-fixture"},
        )
        try:
            pa.bind(offp, gh5.id, actor="t", is_admin=True)
        except pa.AutomationError as exc:
            assert "turned off" in str(exc), str(exc)
        else:
            raise AssertionError("keys off must refuse")
    finally:
        settings.set_many({"key_projects_enabled": "1"})
    print("  shape on bind: github on empty connected -> key_based; mismatches refused")


def test_key_import_refused_on_connected():
    settings.set_many({"security_auth_required": "1"})
    try:
        from tests._csrf import csrf_for

        admin = security.create_user("cc-key-admin@x.co", "CC Key", Role.ADMIN, PW)
        _k, ak = apikeys.create_key(admin.id, "cc key", scopes="api docs keys")
        A = {"X-Loc-Token": ak}
        cust = mk_customer("KY Cust")
        pid = mk_project("KY connected", intake="connected", customer_id=cust)
        gh = mk_conn(
            "KY gh",
            customer_id=cust,
            kind="github",
            cfg={"repo": "acme/ky", "source_locale": "en-US", "target_locales": "de-DE"},
            secret={"token": "tok-fixture"},
        )
        pa.bind(pid, gh.id, actor="t", is_admin=True)
        assert row(pid).project_type == "key_based"
        plain = mk_project("KY plain", customer_id=cust, project_type="key_based")
        c = TestClient(app)

        def n_keys(p):
            with Session(engine) as s:
                return len(s.exec(select(StringKey).where(StringKey.project_id == p)).all())

        # /api upsert + import
        r = c.post(
            f"/api/projects/{pid}/keys", headers=A, json={"keys": [{"name": "k1", "base": "x"}]}
        )
        assert r.status_code == 400 and "connected project" in r.text, (r.status_code, r.text)
        r = c.post(
            f"/api/projects/{pid}/keys/import",
            headers=A,
            json={"filename": "a.json", "data": '{"k1": "x"}', "fmt": "json"},
        )
        assert r.status_code == 400 and "connected project" in r.text, (r.status_code, r.text)
        assert n_keys(pid) == 0
        # the same calls on a plain key project still work
        r = c.post(
            f"/api/projects/{plain}/keys", headers=A, json={"keys": [{"name": "k1", "base": "x"}]}
        )
        assert r.status_code == 200 and r.json()["created"], r.text
        # web add + import
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post("/auth/login", data={"email": admin.email, "password": PW, "next": "/"})
        assert r.status_code == 303
        csrf_for(cl)
        r = cl.post(f"/projects/{pid}/keys/add", data={"name": "k2", "base_value": "y"})
        assert r.status_code == 303 and "connected" in r.headers["location"].lower(), r.headers
        r = cl.post(
            f"/projects/{pid}/keys/import",
            files={"file": ("a.json", b'{"k3": "z"}', "application/json")},
        )
        assert r.status_code == 303 and "connected" in r.headers["location"].lower(), r.headers
        assert n_keys(pid) == 0
        # CLI in-process
        path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "cc_keys.json")
        with open(path, "wb") as fh:
            fh.write(b'{"k4": "w"}')
        code, out = cli("keys", "import", "--project", str(pid), "--file", path, "--yes")
        assert code == 2, (code, out)
        code, out = cli("keys", "add", "--project", str(pid), "--name", "k5", "--base", "v")
        assert code == 2, (code, out)
        assert n_keys(pid) == 0
        code, out = cli("keys", "add", "--project", str(plain), "--name", "k5", "--base", "v")
        assert code == 0, (code, out)
        print(
            "  key import/add/upsert: refused on a connected project (web, /api, CLI); plain fine"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})


def test_convert_api_web_cli_mcp():
    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        from tests._csrf import csrf_for

        admin = security.create_user("cc-cv-admin@x.co", "CC Admin", Role.ADMIN, PW)
        mgr = security.create_user("cc-cv-mgr@x.co", "CC Mgr", Role.MANAGER, PW)
        trans = security.create_user("cc-cv-tr@x.co", "CC Tr", Role.TRANSLATOR, PW)
        ca = mk_customer("CV API A")
        cb = mk_customer("CV API B")
        bound = security.create_user(
            "cc-cv-b@x.co", "CC Bound", Role.TRANSLATOR, PW, customer_id=cb
        )
        _k, ak = apikeys.create_key(admin.id, "cc a", scopes="api docs")
        _k, mk = apikeys.create_key(mgr.id, "cc m", scopes="api docs")
        _k, tk = apikeys.create_key(trans.id, "cc t", scopes="api docs")
        _k, bk = apikeys.create_key(bound.id, "cc b", scopes="api docs")
        _A, M, T, B = ({"X-Loc-Token": t} for t in (ak, mk, tk, bk))
        c = TestClient(app)

        def fed(name):
            pid = mk_project(name, customer_id=ca)
            cn = mk_conn(f"{name} conn", customer_id=ca)
            feed(cn.id, pid)
            return pid, cn.id

        pid, cid = fed("CV api one")
        # ring first: a foreign customer's project answers like a missing one
        r_foreign = c.post(f"/api/projects/{pid}/make-connected", headers=B, json={})
        r_missing = c.post("/api/projects/999999/make-connected", headers=B, json={})
        assert r_foreign.status_code == 404 and r_foreign.content == r_missing.content, (
            r_foreign.text,
            r_missing.text,
        )
        # role: a translator in the project's scope is refused (403) after the ring
        r = c.post(f"/api/projects/{pid}/make-connected", headers=T, json={})
        assert r.status_code == 403, (r.status_code, r.text)
        assert row(pid).intake == "upload" and auto_of(pid) is None
        # a manager converts
        snap = snapshot_conn(cid)
        nc = n_conns()
        r = c.post(f"/api/projects/{pid}/make-connected", headers=M, json={})
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["intake"] == "connected" and body["connection_id"] == cid, body
        assert snapshot_conn(cid) == snap and n_conns() == nc
        a = auto_of(pid)
        assert a.enabled is False and a.next_at is None
        # again -> 400 with a reason
        r = c.post(f"/api/projects/{pid}/make-connected", headers=M, json={})
        assert r.status_code == 400 and "already" in r.json()["error"], r.text
        # bad bodies
        pid2, _cid2 = fed("CV api two")
        r = c.post(f"/api/projects/{pid2}/make-connected", headers=M, content=b"[1]")
        assert r.status_code == 400
        r = c.post(f"/api/projects/{pid2}/make-connected", headers=M, json={"connection_id": "x"})
        assert r.status_code == 400 and "does not feed" in r.json()["error"], r.text
        assert auto_of(pid2) is None
        # uploaded document -> 400 explains
        pid3, _cid3 = fed("CV api three")
        with Session(engine) as s:
            s.add(Document(project_id=pid3, filename="mine.docx", fmt="docx"))
            s.commit()
        r = c.post(f"/api/projects/{pid3}/make-connected", headers=M, json={})
        assert r.status_code == 400 and "uploaded" in r.json()["error"], r.text

        # web: the button, the POST, the refusal flash
        def login(user):
            cl = TestClient(app, follow_redirects=False)
            csrf_for(cl)
            r = cl.post("/auth/login", data={"email": user.email, "password": PW, "next": "/"})
            assert r.status_code == 303
            csrf_for(cl)
            return cl

        w = login(mgr)
        pid4, cid4 = fed("CV web one")
        page = w.get(f"/projects/{pid4}")
        assert page.status_code == 200 and 'id="make-connected"' in page.text, "the button"
        assert (
            "Make this a connected project" in page.text
            and "nothing is scheduled" in page.text.lower()
        )
        tw = login(trans)
        assert 'id="make-connected"' not in tw.get(f"/projects/{pid4}").text, "manager and up only"
        r = tw.post(f"/projects/{pid4}/make-connected", data={})
        assert r.status_code in (403, 303) and auto_of(pid4) is None, r.status_code
        r = w.post(f"/projects/{pid4}/make-connected", data={})
        assert r.status_code == 303 and r.headers["location"].endswith("#source"), r.headers
        assert "src_ok=1" in r.headers["location"]
        assert row(pid4).intake == "connected" and auto_of(pid4).run_connection_id == cid4
        pg = w.get(r.headers["location"])
        assert 'id="source"' in pg.text and 'id="make-connected"' not in pg.text
        # refusal lands on the automation page with the reason
        pid5, _ = fed("CV web two")
        with Session(engine) as s:
            s.add(Document(project_id=pid5, filename="up.docx", fmt="docx"))
            s.commit()
        r = w.post(f"/projects/{pid5}/make-connected", data={})
        assert r.status_code == 303 and "ok=0" in r.headers["location"], r.headers
        assert (
            "uploaded" in r.headers["location"].lower()
            or "uploaded" in w.get(r.headers["location"]).text.lower()
        )
        assert 'id="make-connected"' in w.get(f"/projects/{pid5}").text, (
            "still offered, refusal explains"
        )
        # a plain project nothing feeds has no button
        pid6 = mk_project("CV web none", customer_id=ca)
        assert 'id="make-connected"' not in w.get(f"/projects/{pid6}").text

        # CLI in-process
        pid7, cid7 = fed("CV cli one")
        code, out = cli("project", "make-connected", str(pid7))
        assert code == 0 and out["connection_id"] == cid7 and out["intake"] == "connected", out
        code, out = cli("project", "make-connected", str(pid7))
        assert code == 2, (code, out)
        pid8 = mk_project("CV cli none", customer_id=ca)
        code, out = cli("project", "make-connected", str(pid8))
        assert code == 2, (code, out)
        # --remote: the request body carries the pick
        sent = {}

        class FakeClient:
            def post(self, path, json=None):
                sent["post"] = (path, json)
                return {"ok": True, "connection_id": 5, "intake": "connected"}

        from app.cli import _remote_impl_part2 as ri2

        real = ri2._client
        ri2._client = lambda args: FakeClient()
        try:
            from app.cli import _remote_impl as ri

            class Emit:
                as_json = True

                def result(self, msg, payload):
                    sent["result"] = (msg, payload)

            ri.project_make_connected(argparse.Namespace(project_id=9, connection=5), Emit())
            assert sent["post"] == ("/api/projects/9/make-connected", {"connection_id": 5}), sent
            ri.project_make_connected(argparse.Namespace(project_id=9, connection=None), Emit())
            assert sent["post"] == ("/api/projects/9/make-connected", {}), sent
        finally:
            ri2._client = real

        # MCP
        import asyncio

        sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
        import registry
        import server

        calls = []

        class Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *a):
                return False

        async def fake_post(client, path, body=None):
            calls.append((path, body))
            return {"ok": True, "intake": "connected"}

        real_m = (server._client, server._post_json)
        server._client, server._post_json = lambda: Ctx(), fake_post
        try:
            asyncio.run(server.configure_project(5, "make-connected"))
            assert calls[-1] == ("/api/projects/5/make-connected", {}), calls
            asyncio.run(server.configure_project(5, "make-connected", connection_id=7))
            assert calls[-1] == ("/api/projects/5/make-connected", {"connection_id": 7}), calls
        finally:
            server._client, server._post_json = real_m
        cfgt = next(t for t in registry.TOOLS if t.name == "configure_project")
        assert "make-connected" in cfgt.input_schema["properties"]["action"]["enum"]
        assert "connection_id" in cfgt.input_schema["properties"]
        assert ("POST", "/api/projects/{pid}/make-connected") in cfgt.routes
        print("  convert surfaces: /api (ring, role, bodies), web, CLI (+remote), MCP")
    finally:
        settings.set_many({"security_auth_required": "0"})


def test_template_create_connected():
    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        from tests._csrf import csrf_for

        admin = security.create_user("cc-tp-admin@x.co", "CC Tpl", Role.ADMIN, PW)
        _k, ak = apikeys.create_key(admin.id, "cc tpl", scopes="api docs")
        A = {"X-Loc-Token": ak}
        cust = mk_customer("TP Cust")
        src = mk_project("TP source", customer_id=cust)
        with Session(engine) as s:
            tid = tpl.save_as_template(s, src, "TP tpl", scope="customer").id
        n_auto = len(_all_autos())
        # web
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post("/auth/login", data={"email": admin.email, "password": PW, "next": "/"})
        assert r.status_code == 303
        csrf_for(cl)
        pg = cl.get("/projects")
        assert 'name="connected"' in pg.text, "the from-template form offers Connected"
        r = cl.post(
            "/projects/from-template",
            data={
                "template_id": str(tid),
                "name": "TP web conn",
                "customer_id": str(cust),
                "connected": "1",
            },
        )
        assert r.status_code == 303 and r.headers["location"].endswith("#source"), r.headers
        wp = int(r.headers["location"].split("/")[2].split("#")[0].split("?")[0])
        assert row(wp).intake == "connected" and auto_of(wp) is None
        r = cl.post(
            "/projects/from-template",
            data={"template_id": str(tid), "name": "TP web plain", "customer_id": str(cust)},
        )
        assert r.status_code == 303 and "#source" not in r.headers["location"]
        pp = int(r.headers["location"].split("/")[2].split("?")[0])
        assert row(pp).intake == "upload"
        # /api
        c = TestClient(app)
        r = c.post(
            f"/api/templates/{tid}/apply",
            headers=A,
            json={"name": "TP api conn", "customer_id": cust, "intake": "connected"},
        )
        assert r.status_code == 200 and r.json()["intake"] == "connected", r.text
        ap = r.json()["project_id"]
        assert auto_of(ap) is None
        r = c.post(
            f"/api/templates/{tid}/apply",
            headers=A,
            json={"name": "TP api bad", "customer_id": cust, "intake": "sideways"},
        )
        assert r.status_code == 400, r.text
        # CLI in-process and the remote body
        code, out = cli("template", "apply", str(tid), "--name", "TP cli conn", "--connected")
        assert code == 0 and out["intake"] == "connected", (code, out)
        assert auto_of(out["project_id"]) is None
        code, out = cli("template", "apply", str(tid), "--name", "TP cli plain")
        assert code == 0 and out["intake"] == "upload", (code, out)
        sent = {}

        class FakeClient:
            def post(self, path, json=None):
                sent["post"] = (path, json)
                return {
                    "ok": True,
                    "project_id": 3,
                    "name": "x",
                    "intake": "connected",
                    "warnings": [],
                }

        from app.cli import _remote_impl_part6 as ri6

        real = ri6._client
        ri6._client = lambda args: FakeClient()
        try:
            from app.cli import _remote_impl as ri

            class Emit:
                as_json = True

                def result(self, msg, payload):
                    sent["result"] = (msg, payload)

                def warn(self, m):
                    pass

            ri.template_apply(
                argparse.Namespace(template_id=4, name="x", customer_id=None, connected=True),
                Emit(),
            )
            assert sent["post"][1].get("intake") == "connected", sent
        finally:
            ri6._client = real
        assert len(_all_autos()) == n_auto, "a template never binds or schedules"
        print("  templates: a connected project from a template (web, /api, CLI); never binds")
    finally:
        settings.set_many({"security_auth_required": "0"})


def _all_autos():
    with Session(engine) as s:
        return s.exec(select(ProjectAutomation)).all()


def test_wording_keys_and_locale_add_gate():
    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        from tests._csrf import csrf_for

        admin = security.create_user("cc-lg-admin@x.co", "CC LG", Role.ADMIN, PW)
        mgr = security.create_user("cc-lg-mgr@x.co", "CC LG M", Role.MANAGER, PW)
        ling = security.create_user("cc-lg-lin@x.co", "CC LG L", Role.TRANSLATOR, PW)
        cust = mk_customer("LG Cust")
        cb = mk_customer("LG Other")
        bound = security.create_user("cc-lg-b@x.co", "CC LG B", Role.TRANSLATOR, PW, customer_id=cb)
        _k, mk = apikeys.create_key(mgr.id, "lg m", scopes="api docs")
        _k, tk = apikeys.create_key(ling.id, "lg t", scopes="api docs")
        _k, bk = apikeys.create_key(bound.id, "lg b", scopes="api docs")
        M, T, B = ({"X-Loc-Token": t} for t in (mk, tk, bk))
        c = TestClient(app)

        # wording: the three kinds read Documents, Keys, Connected
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        assert (
            cl.post(
                "/auth/login", data={"email": admin.email, "password": PW, "next": "/"}
            ).status_code
            == 303
        )
        csrf_for(cl)
        page = cl.get("/projects").text
        assert "<strong>Keys</strong>" in page and "<strong>Strings</strong>" not in page
        assert "<strong>Connected</strong>" in page and "<strong>Documents</strong>" in page
        helptxt = cl.get("/help").text
        assert "next to Documents and Keys" in helptxt and "Documents and Strings" not in helptxt

        bound_p = mk_project("LG bound", customer_id=cust, targets="de")
        pa.bind(bound_p, mk_conn("LG conn", customer_id=cust).id, actor="t", is_admin=True)
        plain_p = mk_project("LG plain", customer_id=cust, targets="de")

        def langs(pid):
            return sorted(t.locale for t in _tl(pid))

        before = langs(bound_p)
        # below manager: refused, nothing added (API add-locale and apply-langset)
        r = c.post(f"/api/projects/{bound_p}/add-locale", headers=T, json={"locale": "ja-JP"})
        assert r.status_code == 403 and "manager" in r.json()["error"], (r.status_code, r.text)
        assert langs(bound_p) == before
        # 404 before 403: another customer's user cannot tell the project exists
        rb = c.post(f"/api/projects/{bound_p}/add-locale", headers=B, json={"locale": "ja-JP"})
        rm = c.post("/api/projects/999999/add-locale", headers=B, json={"locale": "ja-JP"})
        assert rb.status_code == 404 and rb.content == rm.content, (rb.text, rm.text)
        # a manager may; a plain project keeps the write rule
        r = c.post(f"/api/projects/{bound_p}/add-locale", headers=M, json={"locale": "ja-JP"})
        assert r.status_code == 200 and r.json()["added"], r.text
        r = c.post(f"/api/projects/{plain_p}/add-locale", headers=T, json={"locale": "ja-JP"})
        assert r.status_code == 200 and r.json()["added"], r.text
        # web
        tw = TestClient(app, follow_redirects=False)
        csrf_for(tw)
        assert (
            tw.post(
                "/auth/login", data={"email": ling.email, "password": PW, "next": "/"}
            ).status_code
            == 303
        )
        csrf_for(tw)
        r = tw.post(f"/projects/{bound_p}/add-locale", data={"locale": "es-ES"})
        assert r.status_code == 403 and "manager" in r.text, r.status_code
        assert "es-ES" not in langs(bound_p)
        r = tw.post(f"/projects/{plain_p}/add-locale", data={"locale": "es-ES"})
        assert r.status_code == 303 and "es-ES" in langs(plain_p)
        # an unbound connected project and plain ones keep today's rule (checked above for plain)

        # Review gate: apply-langset is the same gate (API and web), and unbinding leaves a stopped
        # language stopped (decision 10).
        from datetime import datetime, timezone

        from app.services import languages as langs_svc

        ls = langs_svc.create("LG set", "en-US", "it-IT, pt-BR", actor="t")
        r = c.post(f"/api/projects/{bound_p}/apply-langset", headers=T, json={"langset_id": ls.id})
        assert r.status_code == 403 and "manager" in r.json()["error"], (r.status_code, r.text)
        rb = c.post(f"/api/projects/{bound_p}/apply-langset", headers=B, json={"langset_id": ls.id})
        assert rb.status_code == 404, (rb.status_code, rb.text)
        r = tw.post(f"/projects/{bound_p}/languages", data={"language_set_id": str(ls.id)})
        assert r.status_code == 403 and "manager" in r.text, r.status_code
        assert "it-IT" not in langs(bound_p)
        r = c.post(f"/api/projects/{bound_p}/apply-langset", headers=M, json={"langset_id": ls.id})
        assert r.status_code == 200, r.text
        assert "it-IT" in langs(bound_p)
        with Session(engine) as s:
            tl = s.exec(
                select(TargetLocale).where(
                    TargetLocale.project_id == bound_p, TargetLocale.locale == "it-IT"
                )
            ).one()
            tl.stopped_at = datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(tl)
            s.commit()
        pa.unbind(bound_p, actor="t")
        with Session(engine) as s:
            assert content.stopped_target_locales(s, bound_p) == ["it-IT"], "stays stopped"
        assert auto_of(bound_p) is None, "unbound"
        print("  wording Keys (dialog, help); add-language on a bound project needs a manager")
    finally:
        settings.set_many({"security_auth_required": "0"})


def _fake_complete(cid):
    """Like the real complete_authorize: pops the one-shot session keys, then succeeds."""
    from app.services.connectors import oauth as m

    def f(request, code, state):
        for k in m._SESSION_KEYS:
            request.session.pop(k, None)
        return (
            cid,
            {"access_token": "AT", "refresh_token": "RT", "cloudid": "c", "expires_at": 0},
        ), None

    return f


def test_oauth_return_to():
    """The setup menu's return_to rides the server-side OAuth session (never the provider URL), is
    validated at the start and again at the callback, and brings the person back to the project
    with the new connection selected. A hostile value is dropped (the normal connection page)."""
    from app.services.connectors import oauth as conn_oauth_mod

    settings.set_many(
        {
            "oauth_atlassian_client_id": "atl-client",
            "oauth_atlassian_client_secret": "atl-secret",
            "oauth_airtable_client_id": "at-client",
            "oauth_airtable_client_secret": "at-secret",
        }
    )
    pid = mk_project("OA project", intake="connected")
    orig = conn_oauth_mod.complete_authorize
    try:
        for kind, form_extra in (
            ("jira", {"base_url": "https://team.atlassian.net"}),
            ("airtable", {}),
        ):
            c = TestClient(app)
            # the connector page carries the validated return_to into the OAuth start form
            pg = c.get(f"/connectors/{kind}?return_to=/projects/{pid}")
            assert pg.status_code == 200, pg.status_code
            start_form = pg.text.split(f'action="/connectors/{kind}/oauth/start"', 1)[1].split(
                "</form>"
            )[0]
            assert f'name="return_to" value="/projects/{pid}"' in start_form, kind
            hostile = c.get(f"/connectors/{kind}?return_to=//evil.example/x")
            assert "evil.example" not in hostile.text, "a hostile return_to is never echoed"
            r = c.post(
                f"/connectors/{kind}/oauth/start",
                data={"name": f"OA {kind}", "return_to": f"/projects/{pid}", **form_extra},
                follow_redirects=False,
            )
            assert r.status_code == 303, (kind, r.status_code)
            loc = r.headers["location"]
            assert "return_to" not in loc and f"/projects/{pid}" not in loc, (
                "the provider URL never carries the return path",
                loc,
            )
            made = cstore.list_connections(kind)[0]
            conn_oauth_mod.complete_authorize = _fake_complete(made.id)
            r = c.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
            assert r.status_code == 303, r.status_code
            assert r.headers["location"] == f"/projects/{pid}?connection={made.id}#source", (
                kind,
                r.headers["location"],
            )
            # the return path is one-shot: a second callback goes to the connection page
            r = c.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
            assert "/projects/" not in r.headers["location"], r.headers["location"]

            # hostile / absent return_to: the normal landing page, nothing stored
            for bad in (
                "//evil.example/x",
                "https://evil.example/",
                "/\\evil.example",
                "/projects/1/../../x",
                "/projects/1\n//evil.example",
                "/projects/١٢",  # Arabic-Indic digits are not ASCII digits
                "javascript:alert(1)",
                "",
            ):
                c2 = TestClient(app)
                r = c2.post(
                    f"/connectors/{kind}/oauth/start",
                    data={"name": f"OA {kind} bad", "return_to": bad, **form_extra},
                    follow_redirects=False,
                )
                assert r.status_code == 303
                made2 = cstore.list_connections(kind)[0]
                conn_oauth_mod.complete_authorize = _fake_complete(made2.id)
                r = c2.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
                assert r.headers["location"].startswith(f"/connectors/connection/{made2.id}"), (
                    bad,
                    r.headers["location"],
                )
            # a provider denial clears the return path and lands on the connector page
            c3 = TestClient(app)
            c3.post(
                f"/connectors/{kind}/oauth/start",
                data={"name": f"OA {kind} deny", "return_to": f"/projects/{pid}", **form_extra},
                follow_redirects=False,
            )
            r = c3.get("/connectors/oauth/callback?error=access_denied", follow_redirects=False)
            assert r.headers["location"].startswith(f"/connectors/{kind}?msg="), r.headers[
                "location"
            ]
            r = c3.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
            assert "/projects/" not in r.headers["location"], "denial cleared the stored path"

            # Review gate: an ABANDONED flow from the setup menu (consent screen never finished)
            # must not hand its return path to the next flow started without one.
            c4 = TestClient(app)
            c4.post(
                f"/connectors/{kind}/oauth/start",
                data={"name": f"OA {kind} gone", "return_to": f"/projects/{pid}", **form_extra},
                follow_redirects=False,
            )
            c4.post(
                f"/connectors/{kind}/oauth/start",
                data={"name": f"OA {kind} plain", **form_extra},
                follow_redirects=False,
            )
            made4 = cstore.list_connections(kind)[0]
            conn_oauth_mod.complete_authorize = _fake_complete(made4.id)
            r = c4.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
            assert r.headers["location"].startswith(f"/connectors/connection/{made4.id}"), (
                "a stale return path leaked into a later OAuth flow",
                r.headers["location"],
            )

            # Review gate: a tampered (mismatched) state is refused by the REAL complete_authorize
            # and never follows the stored return path.
            conn_oauth_mod.complete_authorize = orig
            c5 = TestClient(app)
            c5.post(
                f"/connectors/{kind}/oauth/start",
                data={"name": f"OA {kind} tamper", "return_to": f"/projects/{pid}", **form_extra},
                follow_redirects=False,
            )
            r = c5.get("/connectors/oauth/callback?code=abc&state=tampered", follow_redirects=False)
            assert r.headers["location"].startswith(f"/connectors/{kind}?msg="), r.headers[
                "location"
            ]
            assert "/projects/" not in r.headers["location"]
            r = c5.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
            assert "/projects/" not in r.headers["location"], "the refused flow cleared the path"
    finally:
        conn_oauth_mod.complete_authorize = orig
    print("  oauth: return_to rides the server-side session; hostile values dropped")


def main() -> None:
    init_db()
    test_first_pull_creates_connected_bound()
    test_first_pull_keys_kind_shape()
    test_upgrade_changes_nothing()
    test_convert_service()
    test_convert_refusals()
    test_shape_on_bind()
    test_key_import_refused_on_connected()
    test_convert_api_web_cli_mcp()
    test_template_create_connected()
    test_oauth_return_to()
    test_wording_keys_and_locale_add_gate()
    test_opus_gate_additions()
    print("verify_connected_convert OK")


if __name__ == "__main__":
    main()
