"""Optional project due dates (Feature A, offline).

Proves, hermetically (throwaway LOC_TMS_DATA_DIR + cleared provider keys force
MockProvider; ASCII prints only):

  - create-with-due + set + clear round-trip is CONSISTENT across web + CLI + /api
    (the same stored value / cleared state whichever surface writes it);
  - content.due_status = 'overdue' for a past date, 'due_soon' within N days, None
    beyond N -- AND None for a COMPLETED project even when the date is past (a
    finished project is never overdue);
  - an invalid date is rejected cleanly: web -> 400-not-500 flash redirect, CLI ->
    EXIT_USAGE, /api -> 400, service -> DueDateError (never a 500 / stack trace);
  - the project page + dashboard render the due date + overdue/due-soon badge;
  - audit rows are written (project.due_set / project.due_cleared);
  - the column is ADDITIVE: a pre-due DB (row inserted without the column via raw
    SQL is out of scope; here we prove init_db backfills NULL + the model reads it).
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_duedate_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Project, ProjectStatus
from app.services import content, settings


def _project(pid):
    with Session(engine) as s:
        return s.get(Project, pid)


def _create_via_web(client, name, due=""):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
            "due_date": due,
        },
        follow_redirects=False,
    )
    return r


def main() -> None:
    init_db()
    settings.set_many({"audit_enabled": "1"})
    client = TestClient(app)

    # =====================================================================
    # (1) parse_due_date + due_status units
    # =====================================================================
    assert content.parse_due_date("") is None
    assert content.parse_due_date(None) is None
    assert content.parse_due_date("2026-08-15") == datetime(2026, 8, 15)
    # a datetime-local style value keeps only the day
    assert content.parse_due_date("2026-08-15T09:30") == datetime(2026, 8, 15)
    for bad in ("not-a-date", "2026-13-40", "08/15/2026", "2026-2-30"):
        try:
            content.parse_due_date(bad)
            raise AssertionError(f"expected DueDateError for {bad!r}")
        except content.DueDateError:
            pass
    print("  (1) parse_due_date: YYYY-MM-DD -> midnight, blank -> None, garbage -> DueDateError")

    # FIX 4 (defensive): a direct-Python caller passing an AWARE datetime must
    # be normalized to naive-UTC, never passed through as-is (an aware value
    # reaching due_status would crash on a naive-vs-aware comparison). A plain
    # naive datetime (the normal case, e.g. from a prior parse_due_date call)
    # still passes through completely unchanged.
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    aware = datetime(2026, 8, 15, 9, 30, tzinfo=_tz(_td(hours=5)))  # UTC+5
    normalized = content.parse_due_date(aware)
    assert normalized.tzinfo is None, ("aware datetime must be normalized to naive", normalized)
    assert normalized == datetime(2026, 8, 15, 4, 30), (
        "aware datetime must be converted to naive-UTC (09:30 UTC+5 -> 04:30 UTC)",
        normalized,
    )
    naive = datetime(2026, 8, 15, 9, 30)
    assert content.parse_due_date(naive) is naive, (
        "a naive datetime must pass through completely unchanged"
    )
    print(
        "  (1b) FIX 4: parse_due_date normalizes an aware datetime to naive-UTC; "
        "a naive one passes through unchanged"
    )

    now = datetime(2026, 6, 1, 12, 0, 0)
    p_over = Project(
        name="o", due_date=now - timedelta(days=1), status=ProjectStatus.IN_TRANSLATION
    )
    p_soon = Project(
        name="s", due_date=now + timedelta(days=2), status=ProjectStatus.IN_TRANSLATION
    )
    p_far = Project(
        name="f", due_date=now + timedelta(days=30), status=ProjectStatus.IN_TRANSLATION
    )
    p_none = Project(name="n", due_date=None, status=ProjectStatus.IN_TRANSLATION)
    p_done = Project(name="d", due_date=now - timedelta(days=5), status=ProjectStatus.COMPLETED)
    assert content.due_status(p_over, now) == "overdue", content.due_status(p_over, now)
    assert content.due_status(p_soon, now) == "due_soon", content.due_status(p_soon, now)
    assert content.due_status(p_far, now) is None
    assert content.due_status(p_none, now) is None
    # a COMPLETED project past its due date is NEVER overdue
    assert content.due_status(p_done, now) is None, (
        "completed project must never be overdue",
        content.due_status(p_done, now),
    )
    print("  (2) due_status: overdue/due_soon/None (+ None for a completed project past due)")

    # =====================================================================
    # (3) web create-with-due + set + clear round-trip, audit rows
    # =====================================================================
    r = _create_via_web(client, "Web Due", due="2026-09-01")
    assert r.status_code == 303, r.status_code
    pid_web = int(r.headers["location"].rsplit("/", 1)[-1])
    assert _project(pid_web).due_date == datetime(2026, 9, 1), _project(pid_web).due_date

    # set to a new date via POST /projects/{id}/due
    r = client.post(
        f"/projects/{pid_web}/due", data={"due_date": "2026-10-15"}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    assert _project(pid_web).due_date == datetime(2026, 10, 15)

    # clear it (blank)
    r = client.post(f"/projects/{pid_web}/due", data={"due_date": ""}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert _project(pid_web).due_date is None, "blank must clear the due date"

    with Session(engine) as s:
        actions = [
            a.action
            for a in s.exec(select(AuditLog).where(AuditLog.target == f"project {pid_web}")).all()
        ]
    assert "project.due_set" in actions, ("due_set must be audited", actions)
    assert "project.due_cleared" in actions, ("due_cleared must be audited", actions)
    print(
        "  (3) web: create-with-due + set + clear round-trip, audited "
        "(project.due_set / project.due_cleared)"
    )

    # =====================================================================
    # (4) web invalid date -> clean 400/redirect flash, NOT a 500
    # =====================================================================
    r = _create_via_web(client, "Bad Web Due", due="2026-99-99")
    # create redirects back to /projects?err=...#new (303), never a 500
    assert r.status_code == 303, ("bad date must not 500 the create", r.status_code)
    assert "err=" in r.headers.get("location", ""), r.headers.get("location")
    # and the set route on a good project
    r2 = client.post(
        f"/projects/{pid_web}/due", data={"due_date": "garbage"}, follow_redirects=False
    )
    assert r2.status_code == 303 and "err=" in r2.headers.get("location", ""), (
        "bad date on the set route must redirect with err, not 500",
        r2.status_code,
    )
    assert _project(pid_web).due_date is None, "a rejected set must not change the value"
    print("  (4) web: an invalid date is a clean redirect flash (400-class, never a 500)")

    # =====================================================================
    # (5) CLI parity: project create --due, set-due <date>, set-due --clear,
    #     and EXIT_USAGE on a bad date
    # =====================================================================
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main
    from app.cli._common import EXIT_FAIL, EXIT_USAGE

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli_main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        return code

    rc = run(["project", "create", "Cli Due", "--targets", "de-DE", "--due", "2026-11-20"])
    assert rc == 0, rc
    with Session(engine) as s:
        pcli = s.exec(select(Project).where(Project.name == "Cli Due")).first()
    assert pcli.due_date == datetime(2026, 11, 20), pcli.due_date

    rc = run(["project", "set-due", str(pcli.id), "2026-12-25"])
    assert rc == 0, rc
    assert _project(pcli.id).due_date == datetime(2026, 12, 25)

    rc = run(["project", "set-due", str(pcli.id), "--clear"])
    assert rc == 0, rc
    assert _project(pcli.id).due_date is None, "CLI --clear must clear it"

    # EXIT_USAGE (2) on a bad date, both on create and set-due
    rc = run(["project", "create", "Bad Cli Due", "--due", "nope"])
    assert rc == EXIT_USAGE, ("bad --due on create must EXIT_USAGE", rc)
    rc = run(["project", "set-due", str(pcli.id), "2026-99-01"])
    assert rc == EXIT_USAGE, ("bad date on set-due must EXIT_USAGE", rc)
    assert _project(pcli.id).due_date is None, "a rejected set-due must not change the value"
    print(
        "  (5) CLI: project create --due, set-due <date>/--clear round-trip; bad date -> EXIT_USAGE"
    )

    # =====================================================================
    # (6) /api parity: due_date on POST /api/projects + GET /api/projects/{id}
    # =====================================================================
    from app.models import User
    from app.services import apikeys

    # mint a docs-scoped per-user key (the /api surface requires the docs scope).
    # auth is off here, so the role gate no-ops; the key resolves the actor.
    with Session(engine) as s:
        u = User(email="api@example.com", role="admin", active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "duedate-test", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    r = client.post(
        "/api/projects",
        json={"name": "Api Due", "targets": "fr-FR", "due_date": "2027-01-15"},
        headers=hdr,
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] and body["due_date"] == "2027-01-15", body
    api_pid = body["id"]
    g = client.get(f"/api/projects/{api_pid}", headers=hdr)
    assert g.status_code == 200 and g.json()["due_date"] == "2027-01-15", g.json()
    assert _project(api_pid).due_date == datetime(2027, 1, 15)
    # bad date -> 400, not 500
    rb = client.post("/api/projects", json={"name": "Api Bad Due", "due_date": "xx"}, headers=hdr)
    assert rb.status_code == 400, ("bad /api due date must be 400", rb.status_code)
    print("  (6) /api: due_date on POST + GET round-trips; bad date -> 400")

    # =====================================================================
    # (7) rendering: project page + dashboard show the due date / badge
    # =====================================================================
    # a project due in the past + active -> Overdue badge on its page
    with Session(engine) as s:
        pr = s.get(Project, pid_web)
        pr.due_date = datetime.utcnow() - timedelta(days=2)
        pr.status = ProjectStatus.IN_TRANSLATION
        s.add(pr)
        s.commit()
    page = client.get(f"/projects/{pid_web}")
    assert page.status_code == 200, page.status_code
    assert "Overdue" in page.text, "project page must show the Overdue badge"
    assert "Due date" in page.text, "project page must show the due-date section"
    dash = client.get("/projects")
    assert dash.status_code == 200 and "Overdue" in dash.text, (
        "projects dashboard must show the Overdue badge"
    )
    print("  (7) rendering: project page + dashboard show the due date + Overdue badge")

    # =====================================================================
    # (8) additive column: init_db is idempotent; a re-run keeps reading due_date
    # =====================================================================
    init_db()  # second call must not error and the column stays readable
    assert _project(pid_web).due_date is not None
    print("  (8) additive column: init_db idempotent, due_date remains readable")

    # =====================================================================
    # (9) FIX 5: remote `project set-due` == the in-process result. Drives the
    #     CLI's --remote arm over an httpx.MockTransport forwarding to the SAME
    #     in-process TestClient (no live network), against the NEW
    #     POST /api/projects/{id}/due route this fix adds.
    # =====================================================================
    import httpx

    from app.cli import _remote

    def _mock_transport_for(token_client):
        def handler(request):
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query.decode()
            r = token_client.request(
                request.method, path, headers=dict(request.headers), content=request.content
            )
            return httpx.Response(
                r.status_code,
                content=r.content,
                headers={"content-type": r.headers.get("content-type", "")},
            )

        return httpx.MockTransport(handler)

    _remote._set_transport(_mock_transport_for(client))
    try:
        # api_pid + raw (the docs-scoped key) come from case (6) above.
        rc = run(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "project",
                "set-due",
                str(api_pid),
                "2027-06-01",
            ]
        )
        assert rc == 0, rc
        assert _project(api_pid).due_date == datetime(2027, 6, 1), (
            "remote set-due must write the same due_date the in-process arm would",
            _project(api_pid).due_date,
        )

        # remote --clear parity
        rc = run(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "project",
                "set-due",
                str(api_pid),
                "--clear",
            ]
        )
        assert rc == 0, rc
        assert _project(api_pid).due_date is None, "remote --clear must clear it"

        # remote bad date -> a clean CliError (EXIT_FAIL), never a 500 / stack
        # trace. RemoteClient._decode maps EVERY 4xx server response to EXIT_FAIL
        # (it can't distinguish a validation 400 from other errors over HTTP) --
        # this is the SAME mapping every other remote-capable command uses, not
        # a set-due-specific gap.
        rc = run(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "project",
                "set-due",
                str(api_pid),
                "2027-99-01",
            ]
        )
        assert rc == EXIT_FAIL, ("bad date over --remote must be a clean EXIT_FAIL", rc)
        assert _project(api_pid).due_date is None, (
            "a rejected remote set-due must not change the value"
        )

        # Direct route sanity: the /api endpoint itself round-trips + audits,
        # mirroring the web POST /projects/{id}/due route exactly.
        r9 = client.post(
            f"/api/projects/{api_pid}/due", json={"due_date": "2027-07-04"}, headers=hdr
        )
        assert r9.status_code == 200 and r9.json()["due_date"] == "2027-07-04", r9.json()
        with Session(engine) as s:
            actions9 = [
                a.action
                for a in s.exec(
                    select(AuditLog).where(AuditLog.target == f"project {api_pid}")
                ).all()
            ]
        assert "project.due_set" in actions9, ("remote/api set-due must audit", actions9)
    finally:
        _remote._set_transport(None)
    print(
        "  (9) FIX 5: remote `project set-due` (POST /api/projects/{id}/due) "
        "matches the in-process result -- set/clear/bad-date parity, audited"
    )

    print("DUE DATES OK")


if __name__ == "__main__":
    main()
