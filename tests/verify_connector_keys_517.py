"""Issue #517: companion apps must not leak a user's key or hand the paid Jira batch to any viewer.

Server side (provable here): the Jira saved-JQL batch routes (API sync, JQL translate,
translate-pending, and the web twins) need an ADMIN account on a Jira connection; single
issue translate keeps the ordinary write role; other connector kinds are untouched.
Extension / Forge JS: the pure trust and permission guards are exercised under plain Node
(skipped with a note when node is absent); the React and Forge wiring is checked statically.

Final line: "CONNECTOR KEYS 517 OK".
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_conn517_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import apikeys, security, settings
from app.services.connectors import store
from tests._csrf import csrf_for
from tests._piece_patch import patch_pieces

EXT = os.path.join(_ROOT, "integrations", "airtable-extension", "frontend")
FORGE = os.path.join(_ROOT, "integrations", "jira-forge-app")
PW = "conn517-password-123"


def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _key(role, tag):
    email = f"{role}-{tag}@c517.test"
    u = security.create_user(email, email, role, PW)
    _row, raw = apikeys.create_key(u.id, "c517", scopes="api docs")
    return {"X-Loc-Token": raw}, email


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_api_jira_batch_is_admin_only():
    _auth_on()
    jira = store.create("jira", "c517-jira", {}, {})
    api = TestClient(app)
    ling, _ = _key(Role.LINGUIST, "api")
    admin, _ = _key(Role.ADMIN, "api")
    stub = (True, "stubbed", {"translated": 1})
    with (
        patch_pieces("app.services.connectors.store", "translate_now", return_value=stub) as run,
        patch_pieces("app.services.connectors.store", "jira_translate", return_value=stub) as jt,
        patch_pieces(
            "app.services.connectors.store", "jira_translate_pending", return_value=stub
        ) as pend,
    ):
        r = api.post(f"/api/connectors/{jira.id}/sync", headers=ling, json={})
        assert r.status_code == 403, ("linguist must not run the saved JQL", r.status_code, r.text)
        run.assert_not_called()
        r = api.post(f"/api/connectors/{jira.id}/jira/translate", headers=ling, json={})
        assert r.status_code == 403, ("a JQL translate is a batch", r.status_code, r.text)
        r = api.post(
            f"/api/connectors/{jira.id}/jira/translate", headers=ling, json={"jql": "project = X"}
        )
        assert r.status_code == 403, ("a JQL translate is a batch", r.status_code, r.text)
        jt.assert_not_called()
        r = api.post(f"/api/connectors/{jira.id}/jira/translate-pending", headers=ling)
        assert r.status_code == 403, ("pending release is a batch", r.status_code, r.text)
        pend.assert_not_called()
        # The ordinary write role still translates one named issue.
        r = api.post(
            f"/api/connectors/{jira.id}/jira/translate", headers=ling, json={"issue_key": "AB-1"}
        )
        assert r.status_code == 200, (r.status_code, r.text)
        # An admin account still runs the batch.
        assert (
            api.post(f"/api/connectors/{jira.id}/sync", headers=admin, json={}).status_code == 200
        )
        assert run.call_count == 1
        assert (
            api.post(f"/api/connectors/{jira.id}/jira/translate-pending", headers=admin).status_code
            == 200
        )
        # A long key list is a batch in disguise: admin only past the named-issue cap.
        many = [f"AB-{i}" for i in range(1, 40)]
        r = api.post(
            f"/api/connectors/{jira.id}/jira/translate", headers=ling, json={"issue_keys": many}
        )
        assert r.status_code == 403, ("39 keys is a batch", r.status_code, r.text)
        assert (
            api.post(
                f"/api/connectors/{jira.id}/jira/translate",
                headers=admin,
                json={"issue_keys": many},
            ).status_code
            == 200
        )
        # The generic job door must not be a side entrance to the same batch.
        for body in (
            {"connection_id": jira.id},
            {"connection_id": jira.id, "jira_translate_pending": True},
            {
                "connection_id": jira.id,
                "jira_translate_pending": True,
                "retry_failures": [{"issue_key": "AB-1", "locale": "de-DE", "error": "x"}],
            },
        ):
            r = api.post(
                "/api/jobs/run", headers=ling, json={"kind": "connector_translate", "payload": body}
            )
            assert r.status_code == 403, ("jobs/run side door", body, r.status_code, r.text)
        # Other kinds are unaffected by the Jira rule.
        at = store.create("airtable", "c517-at", {}, {"api_key": "x"})
        assert api.post(f"/api/connectors/{at.id}/sync", headers=ling, json={}).status_code == 200
    print(
        "  1. #517: Jira batch routes are admin-only on the API; "
        "single-issue and other kinds keep the write role"
    )


def test_web_jira_batch_is_admin_only():
    _auth_on()
    jira = store.create("jira", "c517-web", {}, {})
    outcomes = {}
    for role in (Role.LINGUIST, Role.ADMIN):
        email = f"{role}-web@c517.test"
        security.create_user(email, email, role, PW)
        c = TestClient(app)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": email, "password": PW, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        csrf_for(c)
        with patch("app.kernel.jobs.submit_unique", return_value=(7, True)) as sub:
            r = c.post(f"/connectors/connection/{jira.id}/sync", follow_redirects=False)
            outcomes[role] = (r.headers.get("location", ""), sub.call_count)
    ling_loc, ling_jobs = outcomes[Role.LINGUIST]
    assert ling_jobs == 0 and "needs%20an%20admin" in ling_loc.replace(" ", "%20"), outcomes
    admin_loc, admin_jobs = outcomes[Role.ADMIN]
    assert admin_jobs == 1 and "job=7" in admin_loc, outcomes
    print("  2. #517: the web Translate-now button on a Jira connection is admin-only")


def test_gate_side_doors_closed():
    """Review gate findings: a smuggled key list, a hand-written retry list and a schedule
    must not hand a non-admin the org-wide Jira batch either."""
    import httpx

    from app.services.connectors.jira import JiraConnector

    _auth_on()
    # (a) One issue_keys element must not carry JQL or a comma list into the search.
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"issues": [], "total": 0})

    jr = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=httpx.MockTransport(handler),
    )
    for smuggled in ("AB-1) OR (key is not EMPTY", "AB-1,AB-2,AB-3", "AB-1 ", 7):
        try:
            jr.fetch_issues(issue_keys=[smuggled], fields=("summary",))
            raise AssertionError(("smuggled key accepted", smuggled))
        except ValueError:
            pass
    assert seen == [], ("no search may fire for a malformed key", seen)
    jr.fetch_issues(issue_keys=["AB-1"], fields=("summary",))
    assert len(seen) == 1, seen

    jira = store.create("jira", "c517-gate", {}, {})
    api = TestClient(app)
    ling, _ = _key(Role.LINGUIST, "gate")
    admin, _ = _key(Role.ADMIN, "gate")
    many = [{"issue_key": f"AB-{i}", "locale": "de-DE", "error": "x"} for i in range(1, 40)]
    few = many[:3]
    with patch("app.kernel.jobs.submit_unique", return_value=(9, True)) as sub:
        # (b) A hand-written retry list of 39 issues is a batch: admin only, on both doors.
        r = api.post(f"/api/connectors/{jira.id}/retry", headers=ling, json={"failures": many})
        assert r.status_code == 403, ("retry list is a batch", r.status_code, r.text)
        r = api.post(
            "/api/jobs/run",
            headers=ling,
            json={
                "kind": "connector_translate",
                "payload": {"connection_id": jira.id, "retry_failures": many},
            },
        )
        assert r.status_code == 403, ("jobs/run retry list is a batch", r.status_code, r.text)
        # A string spelling of the id resolves to the same Jira connection in the worker.
        r = api.post(
            "/api/jobs/run",
            headers=ling,
            json={"kind": "connector_translate", "payload": {"connection_id": str(jira.id)}},
        )
        assert r.status_code == 403, ("string id side door", r.status_code, r.text)
        assert sub.call_count == 0, sub.call_args_list
        # A short retry list keeps the write role; an admin may send the long one.
        r = api.post(f"/api/connectors/{jira.id}/retry", headers=ling, json={"failures": few})
        assert r.status_code == 202, (r.status_code, r.text)
        r = api.post(f"/api/connectors/{jira.id}/retry", headers=admin, json={"failures": many})
        assert r.status_code == 202, (r.status_code, r.text)
    # (c) Turning on a schedule starts recurring org-wide batches: admin only.
    r = api.post(f"/api/connectors/{jira.id}/schedule", headers=ling, json={"every_minutes": 60})
    assert r.status_code == 403, ("linguist scheduled the batch", r.status_code, r.text)
    r = api.post(
        f"/api/connectors/{jira.id}/update",
        headers=ling,
        json={"config": {"sync_every_minutes": 15, "sync_next_at": "2000-01-01T00:00:00"}},
    )
    assert r.status_code == 403, ("schedule via config update", r.status_code, r.text)
    r = api.post(
        "/api/connectors",
        headers=ling,
        json={"kind": "jira", "name": "c517-sched", "config": {"sync_every_minutes": 15}},
    )
    assert r.status_code == 403, ("schedule via create", r.status_code, r.text)
    assert not (store.get(jira.id).config or {}).get("sync_every_minutes")
    r = api.post(f"/api/connectors/{jira.id}/schedule", headers=ling, json={"off": True})
    assert r.status_code == 200, ("turning a schedule off stays open", r.status_code, r.text)
    r = api.post(f"/api/connectors/{jira.id}/schedule", headers=admin, json={"every_minutes": 60})
    assert r.status_code == 200, (r.status_code, r.text)
    # Web twin of the schedule gate.
    email = "linguist-sched@c517.test"
    security.create_user(email, email, Role.LINGUIST, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    csrf_for(c)
    with patch("app.services.scheduler.set_schedule") as ss:
        r = c.post(
            f"/connectors/connection/{jira.id}/schedule",
            data={"every_minutes": "60"},
            follow_redirects=False,
        )
        assert ss.call_count == 0 and "admin" in r.headers.get("location", ""), (
            r.status_code,
            r.headers.get("location"),
        )
    print("  5. #517 gate: smuggled keys, long retry lists and Jira schedules are admin-only")


def _node():
    return shutil.which("node")


def _run_node(source):
    out = subprocess.run(
        [_node(), "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, (out.stdout, out.stderr)
    return out.stdout


def _as_mjs(path):
    tmp = tempfile.mkdtemp(prefix="c517_")
    dest = os.path.join(tmp, os.path.basename(path).replace(".js", ".mjs"))
    shutil.copyfile(path, dest)
    return dest.replace("\\", "/")


def test_extension_trust_rules():
    src = _read(EXT, "index.js")
    # The key must never follow the shared, editor-writable config.
    assert not re.search(r"setAsync\('loc\w*Url'", src), "URL must not go to shared config"
    assert "useState(readLocal(URL_KEY))" in src, "URL must be per-user local state"
    assert "if (!trusted)" in src and "redirect: 'error'" in src, "run() must require trust"
    assert re.search(r"api\(norm\.origin, token", src), "key must go to the parsed origin only"
    if not _node():
        print("  3. #517: node missing, trust helper unit test SKIPPED (static checks passed)")
        return
    mod = _as_mjs(os.path.join(EXT, "trust.js"))
    out = _run_node(
        f"""
import {{ normalizeServerUrl as n, isTrustedOrigin as t }} from 'file:///{mod}';
const bad = ['', 'attacker', 'http://attacker.example', 'ftp://x.example', 'https://u:p@x.example',
  'javascript:alert(1)'];
for (const b of bad) if (n(b).ok) throw new Error('accepted ' + b);
if (n('https://ok.example/path?q=1').origin !== 'https://ok.example') throw new Error('origin');
if (!n('http://localhost:8000').ok || !n('http://127.0.0.1:8000').ok) throw new Error('local dev');
if (!t('https://ok.example/x', 'https://ok.example')) throw new Error('trusted');
if (t('https://attacker.example', 'https://ok.example')) throw new Error('swapped origin');
if (t('https://ok.example', '')) throw new Error('nothing trusted yet');
if (t('https://ok.example.attacker.example', 'https://ok.example')) throw new Error('suffix');
console.log('ok');
"""
    )
    assert "ok" in out
    print("  3. #517: extension never sends a key to an unconfirmed or non-https origin")


def test_forge_guards():
    src = _read(FORGE, "src", "index.js")
    assert "isJiraAdmin(askJira)" in src and "canTranslateIssue(askJira, key)" in src
    assert "asUser()" in src and "asApp()" not in src, "checks must run as the invoking user"
    assert "read:jira-work" in _read(FORGE, "manifest.yml")
    if not _node():
        print("  4. #517: node missing, Forge guard unit test SKIPPED (static checks passed)")
        return
    mod = _as_mjs(os.path.join(FORGE, "src", "guard.js"))
    out = _run_node(
        f"""
import {{ isJiraAdmin, canTranslateIssue }} from 'file:///{mod}';
const reply = (perms, ok = true) => async () =>
  ({{ ok, json: async () => ({{ permissions: perms }}) }});
const have = (v) => ({{ havePermission: v }});
if (!(await isJiraAdmin(reply({{ ADMINISTER: have(true) }})))) throw new Error('admin refused');
if (await isJiraAdmin(reply({{ ADMINISTER: have(false) }}))) throw new Error('viewer allowed');
if (await isJiraAdmin(reply({{}}))) throw new Error('empty allowed');
if (await isJiraAdmin(reply({{ ADMINISTER: have(true) }}, false)))
  throw new Error('http error allowed');
if (await isJiraAdmin(async () => {{ throw new Error('boom'); }})) throw new Error('throw allowed');
const all = {{ BROWSE_PROJECTS: have(true), EDIT_ISSUES: have(true), ADD_COMMENTS: have(true) }};
if (!(await canTranslateIssue(reply(all), 'AB-1'))) throw new Error('writer refused');
if (await canTranslateIssue(reply({{ ...all, EDIT_ISSUES: have(false) }}), 'AB-1'))
  throw new Error('read-only allowed');
if (await canTranslateIssue(reply(all), 'not a key; drop')) throw new Error('bad key allowed');
console.log('ok');
"""
    )
    assert "ok" in out
    print(
        "  4. #517: Forge sync is Jira-admin only; "
        "translate needs edit and comment rights; fail closed"
    )


def main():
    init_db()
    test_api_jira_batch_is_admin_only()
    test_web_jira_batch_is_admin_only()
    test_extension_trust_rules()
    test_forge_guards()
    test_gate_side_doors_closed()
    print("CONNECTOR KEYS 517 OK")


if __name__ == "__main__":
    main()
