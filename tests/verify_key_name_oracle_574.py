"""Issue #574 item 2: the hidden key name is not an existence oracle.

A caller below manager who creates a key whose name matches a HIDDEN key must get exactly the
answer a VISIBLE (taken) name gets: the same wording, the same status code. The maintainer's
decision
(2026-10-07): one generic refusal, "the key name 'x' is not available in ...", and the per-row
import/upsert refusal of a hidden key reads "key name not available".

  [1] create   service, web route: hidden-name and visible-name duplicates answer byte-identical
               text (name aside) and the same status, below manager.
  [2] upsert   /api bulk upsert and file import: a row naming a hidden key reports the generic
               string, below manager, and the key stays untouched.
  [3] manager  a manager still updates a hidden key by upsert (no behavior change).
"""

import io
import json
import os
import sys
from contextlib import redirect_stdout
from urllib.parse import unquote_plus

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keyoracle574_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, ProjectMember, ProjectType, Role
from app.services import apikeys, content, security, settings
from app.services import keys as keys_svc
from tests._csrf import csrf_for

init_db()

PW = "keyoracle574-pw-123456"
HIDDEN = "oracle_hidden_574"
VISIBLE = "oracle_visible_574"
GENERIC_ROW = "key name not available"


def _msg(name, where="the default namespace"):
    return f"the key name {name!r} is not available in {where}."


def _login(role):
    email = f"oracle574-{role}@x.com"
    user = security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (role, r.status_code)
    csrf_for(c)
    return c, user


def _remote_cli_add(c, raw, pid, name):
    """`keys add --remote` as a caller holding `raw`: (exit code, --json error text)."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = c.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    _remote._set_transport(httpx.MockTransport(handler))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = cli_main(
                [
                    *("--json", "--remote", "http://127.0.0.1", "--token", raw),
                    *("keys", "add", "--project", str(pid), "--name", name, "--base", "probe"),
                ]
            )
    finally:
        _remote._set_transport(None)
    out = buf.getvalue().strip()
    return rc, (json.loads(out).get("error") if out else None)


def main():
    settings.set_many({"security_auth_required": "0", "key_projects_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        cust = Customer(name="Oracle574 Co")
        s.add(cust)
        s.commit()
        proj = content.project_create(
            s,
            "Oracle574 Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        pid = proj.id
        project = s.get(Project, pid)
        hidden = keys_svc.create_key(
            s, project, HIDDEN, base_value="Hidden text", is_hidden=True, actor="fixture"
        )
        keys_svc.create_key(s, project, VISIBLE, base_value="Visible text", actor="fixture")
        hidden_id = hidden.id

        # [1] service level: identical text and class, hidden or visible
        texts = {}
        for nm in (HIDDEN, VISIBLE):
            try:
                keys_svc.create_key(s, project, nm, base_value="dup", actor="fixture")
            except keys_svc.DuplicateKeyError as exc:
                texts[nm] = str(exc)
                assert (exc.namespace, exc.name, exc.context) == ("", nm, "")
            else:
                raise AssertionError(f"duplicate {nm} was accepted")
        assert texts[HIDDEN] == _msg(HIDDEN), texts[HIDDEN]
        assert texts[VISIBLE] == _msg(VISIBLE), texts[VISIBLE]
        assert "already exists" not in texts[HIDDEN]
        s.rollback()

    roles = [Role.LINGUIST, Role.MANAGER]
    clients = {r: _login(r) for r in roles}
    with Session(engine) as s:
        for _c, user in clients.values():
            s.add(ProjectMember(project_id=pid, user_id=user.id))
        s.commit()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        for role, (c, user) in clients.items():
            manager = role == Role.MANAGER
            _, raw = apikeys.create_key(user.id, f"oracle574-{role}", scopes="api docs keys")
            h = {"X-Loc-Token": raw}
            # [1] web create route: same redirect text and status for a hidden and a visible name
            web = {}
            for nm in (HIDDEN, VISIBLE):
                r = c.post(f"/projects/{pid}/keys/add", data={"name": nm}, follow_redirects=False)
                web[nm] = (r.status_code, unquote_plus(r.headers.get("location", "")))
            assert web[HIDDEN][0] == web[VISIBLE][0], (role, web)
            # Gate: both roles reach the duplicate check (a 403 here would make the check vacuous).
            assert web[VISIBLE][0] == 303, (role, web)
            assert _msg(VISIBLE) in web[VISIBLE][1], (role, web[VISIBLE])
            assert web[HIDDEN][1].replace(HIDDEN, VISIBLE) == web[VISIBLE][1], (role, web)
            assert _msg(HIDDEN) in web[HIDDEN][1], (role, web[HIDDEN])

            # [2]/[3] bulk upsert naming the hidden key
            up = c.post(
                f"/api/projects/{pid}/keys",
                json={"keys": [{"name": HIDDEN, "description": "probe 574"}]},
                headers=h,
            )
            with Session(engine) as s:
                desc = s.get(keys_svc.StringKey, hidden_id).description
            if manager:
                assert up.status_code == 200 and hidden_id in up.json()["updated"], up.text
                assert not up.json()["errors"], up.json()
                assert desc == "probe 574", desc
            else:
                assert up.status_code == 200, (role, up.status_code, up.text)
                errs = up.json()["errors"]
                assert [e["error"] for e in errs] == [GENERIC_ROW], errs
                assert not up.json()["updated"] and not up.json()["created"], up.json()
                assert desc != "probe 574", desc

            # [2] file import naming the hidden key
            if not manager:
                imp = c.post(
                    f"/api/projects/{pid}/keys/import",
                    json={
                        "filename": "__default__.json",
                        "locale": "en-US",
                        "data": json.dumps({HIDDEN: "Overwritten 574"}),
                    },
                    headers=h,
                )
                assert imp.status_code == 200, (role, imp.status_code, imp.text)
                errs = imp.json()["errors"]
                assert [e["error"] for e in errs] == [GENERIC_ROW], errs
            # [1] CLI --remote `keys add`: exit code and message identical for both names
            cli = {nm: _remote_cli_add(c, raw, pid, nm) for nm in (HIDDEN, VISIBLE)}
            assert cli[VISIBLE][0] != 0 and cli[VISIBLE][1] == _msg(VISIBLE), (role, cli)
            assert cli[HIDDEN][0] == cli[VISIBLE][0], (role, cli)
            assert cli[HIDDEN][1] == _msg(HIDDEN), (role, cli)
            print(f"  {role}: web={web[HIDDEN][0]}/{web[VISIBLE][0]} upsert={up.status_code}")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


if __name__ == "__main__":
    main()
    print("verify_key_name_oracle_574 OK")
