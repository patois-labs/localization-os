"""Role gating gaps from the security review (issue #540), per role, with sign-in on.

  [1] /jobs        install-wide AI spend is manager-and-above data (ADR 0042): the spend line
                   is rendered for manager and admin only, never for the other four roles.
  [2] chrome       the red spend-cap banner names the figures for manager and admin; every
                   other role still sees that the cap is reached, with no dollar amounts.
  [3] (retired: the evaluation compare Cost column belongs to a paid add-on.)
  [4] connection   the schedule, settings (prefilled non-secret credential fields) and rotate
                   cards are rendered only for roles that may manage the connection.
  [6] run page     the per-run total, per-step cost and Gantt cost columns of /runs/{id} (and
                   its graph fragment) are manager-and-above; the page still opens for all.
  [7] hidden keys  the /changes feed (both modes) and keys/empties (web and /api) leave out
      (more)       hidden keys for roles below manager.
  [5] hidden keys  a key marked "hidden from translators" is absent from the grid, the /api
                   list and every per-key route (web and /api answer the same 404) for roles
                   below manager; manager and admin still see and edit it.
  [6b] run/batch JSON the /api/runs, /api/batches and web batch status JSON carry no cost_usd or
                   token field below manager (#574).
  [8] keys translate a project holding a hidden key refuses a launch below manager, web and /api
                   (#574); managers and admins are not refused for that reason.
  [5c] hidden keys a file import (merge) that names a hidden key does not write it below
      (import)     manager, and a cleanup import never deletes (or counts) a hidden key.

Hermetic + offline. ASCII-only prints. Final line: "verify_role_gating_540 OK".
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rolegate540_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    Customer,
    Document,
    Project,
    ProjectMember,
    ProjectType,
    Role,
    RunStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, content, security, settings
from app.services import keys as keys_svc
from app.services.connectors import store as conn_store
from tests._csrf import csrf_for

init_db()

PW = "rolegate540-pw-123456"
ROLES = [Role.ADMIN, Role.MANAGER, Role.LINGUIST, Role.REVIEWER, Role.TRANSLATOR, Role.VIEWER]
MANAGERS = {Role.ADMIN, Role.MANAGER}
PROBE_EMAIL = "support-probe-540@example.test"
HIDDEN_NAME = "secret_hidden_key_540"
VISIBLE_NAME = "plain_visible_key_540"
HIDDEN_DONE = "secret_translated_hidden_540"
VISIBLE_DONE = "plain_translated_visible_540"
NS = "ns540"  # [5c] cleanup sweeps one namespace; keep it apart from the other fixtures
_SINCE = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
RUN_COST = 0.4321  # shown as "0.4321" wherever the AI figure leaks


def _fail(msg):
    raise AssertionError(msg)


def _login(role):
    email = f"gate540-{role}@x.com"
    user = security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (role, r.status_code)
    csrf_for(c)
    return c, user


def _seed():
    settings.set_many(
        {
            "security_auth_required": "0",
            "key_projects_enabled": "1",
        }
    )
    settings.invalidate_cache()
    out = {}
    with Session(engine) as s:
        cust = Customer(name="Gate540 Co")
        s.add(cust)
        s.commit()
        out["cust"] = cust.id
        proj = content.project_create(
            s,
            "Gate540 Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        out["pid"] = proj.id
        project = s.get(Project, proj.id)
        hidden = keys_svc.create_key(
            s, project, HIDDEN_NAME, base_value="Hidden text", is_hidden=True, actor="fixture"
        )
        visible = keys_svc.create_key(
            s, project, VISIBLE_NAME, base_value="Visible text", actor="fixture"
        )
        out["hidden_id"], out["visible_id"] = hidden.id, visible.id
        ns_hidden = keys_svc.create_key(
            s,
            project,
            "ns_hidden",
            namespace=NS,
            base_value="NS hidden",
            is_hidden=True,
            actor="fixture",
        )
        keys_svc.create_key(
            s, project, "ns_visible", namespace=NS, base_value="NS visible", actor="fixture"
        )
        out["ns_hidden_id"] = ns_hidden.id
        # translated pair: the modified_after feed only lists keys with written values
        for nm, hid in ((HIDDEN_DONE, True), (VISIBLE_DONE, False)):
            k = keys_svc.create_key(
                s, project, nm, base_value="Base text", is_hidden=hid, actor="fixture"
            )
            if hid:
                out["hidden_done_id"] = k.id
            keys_svc.set_value(
                s, k, "de-DE", "Fertiger Text", origin=keys_svc.ORIGIN_HUMAN, actor="fixture"
            )
        # [6] a document project with a completed run that recorded spend
        dproj = Project(name="Gate540 Docs", source_locale="en-US", customer_id=cust.id)
        s.add(dproj)
        s.commit()
        out["dpid"] = dproj.id
        doc = Document(
            project_id=dproj.id,
            filename="g.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hi."}]},
        )
        s.add(doc)
        wf = Workflow(name="Gate540 wf", scope="global")
        s.add(wf)
        s.commit()
        step = WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="MT",
            step_type=StepType.LLM_AGENT,
            provider="mock",
        )
        s.add(step)
        s.commit()
        run = WorkflowRun(
            workflow_id=wf.id, document_id=doc.id, target_locale="de-DE", status=RunStatus.COMPLETED
        )
        s.add(run)
        s.commit()
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.COMPLETED,
                cost_usd=RUN_COST,
                tokens_in=111,
                tokens_out=222,
                latency_ms=40,
            )
        )
        s.commit()
        out["run_id"] = run.id
        out["wf_id"] = wf.id
        # [6b] a batch over that run, so the batch JSON surfaces carry the same spend
        batch = WorkflowBatch(
            workflow_id=wf.id, document_id=doc.id, requested_locales=["de-DE"], created_by="fixture"
        )
        s.add(batch)
        s.commit()
        run.batch_id = batch.id
        s.add(run)
        s.commit()
        out["batch_id"] = batch.id
        conn = Connection(
            kind="zendesk",
            name="gate540 zendesk",
            config={"email": PROBE_EMAIL, "subdomain": "probe540"},
            secret_enc=conn_store.enc_secret({"email": PROBE_EMAIL}),
            created_by="fixture",
        )
        s.add(conn)
        s.commit()
        out["cid"] = conn.id
    return out


def check_all(fx):
    import app.kernel.spend as spend_mod

    real_would_block = spend_mod.would_block
    clients = {role: _login(role) for role in ROLES}
    # translators, reviewers and viewers see only projects they are members of
    with Session(engine) as s:
        for _c, user in clients.values():
            s.add(ProjectMember(project_id=fx["pid"], user_id=user.id))
            s.add(ProjectMember(project_id=fx["dpid"], user_id=user.id))
        s.commit()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        for role, (c, user) in clients.items():
            manager = role in MANAGERS
            # [1] /jobs spend line
            jobs = c.get("/jobs")
            assert jobs.status_code == 200, (role, jobs.status_code)
            has_spend = 'id="jobs-spend-line"' in jobs.text
            assert has_spend == manager, f"/jobs spend line for {role}: shown={has_spend}"
            # [2] cap banner (cap tripped): figures for managers only, the notice for all
            spend_mod.would_block = lambda: (True, 12.34, 10.0)
            try:
                home = c.get("/jobs")
            finally:
                spend_mod.would_block = real_would_block
            assert "Spend cap reached" in home.text, (role, "banner missing")
            assert ("12.34" in home.text) == manager, f"banner figures for {role}"
            assert ("10.00" in home.text) == manager, f"banner cap figure for {role}"
            # [4] connection cards
            conn = c.get(f"/connectors/connection/{fx['cid']}", follow_redirects=False)
            if conn.status_code == 200:
                manages = role in (Role.ADMIN, Role.LINGUIST)
                for card in ("schedule", "settings", "rotate"):
                    shown = f'data-card="{card}"' in conn.text
                    assert shown == manages, f"connection {card} card for {role}: {shown}"
                assert (PROBE_EMAIL in conn.text) == manages, f"prefilled email for {role}"
            else:
                assert role not in (Role.ADMIN, Role.LINGUIST), (role, conn.status_code)
            # [5] hidden keys
            see = role in MANAGERS
            grid = c.get(f"/projects/{fx['pid']}/keys")
            if grid.status_code == 200:
                assert VISIBLE_NAME in grid.text, (role, "visible key missing from grid")
                assert (HIDDEN_NAME in grid.text) == see, f"hidden key in grid for {role}"
                post = c.post(
                    f"/projects/{fx['pid']}/keys/{fx['hidden_done_id']}/value",
                    data={"locale": "de-DE", "text": "x"},
                    follow_redirects=False,
                )
                if see:
                    assert post.status_code != 404, (role, post.status_code)
                else:
                    assert post.status_code in (403, 404), (role, post.status_code)
            else:
                # a role the project ring already refuses (403/404) sees nothing at all
                assert not see, (role, "manager could not open the grid", grid.status_code)
            _, raw = apikeys.create_key(user.id, f"gate540-{role}", scopes="api docs keys")
            h = {"X-Loc-Token": raw}
            lst = c.get(f"/api/projects/{fx['pid']}/keys", headers=h)
            if lst.status_code == 200:
                names = {k["name"] for k in lst.json()["keys"]}
                assert VISIBLE_NAME in names, (role, names)
                assert (HIDDEN_NAME in names) == see, f"api list hidden key for {role}: {names}"
                one = c.get(f"/api/keys/{fx['hidden_id']}/values", headers=h)
                if see:
                    assert one.status_code == 200, (role, one.status_code)
                else:
                    assert one.status_code == 404, (role, one.status_code)
            else:
                assert not see, (role, "manager refused by the project ring", lst.status_code)
            # [5b] bulk upsert by name must not edit or unhide a hidden key below manager
            up = c.post(
                f"/api/projects/{fx['pid']}/keys",
                json={
                    "keys": [
                        {
                            "name": HIDDEN_NAME,
                            **({"description": "ok"} if see else {"is_hidden": False}),
                        }
                    ]
                },
                headers=h,
            )
            with Session(engine) as s:
                still_hidden = s.get(keys_svc.StringKey, fx["hidden_id"]).is_hidden
            assert still_hidden, f"bulk upsert unhid the hidden key for {role}"
            if see:
                assert up.status_code == 200 and fx["hidden_id"] in up.json()["updated"], (
                    role,
                    up.status_code,
                )
            elif up.status_code == 200:
                assert fx["hidden_id"] not in up.json()["updated"], (role, up.json())
                assert up.json()["errors"], (role, "hidden key upsert not refused")
            else:
                assert up.status_code in (403, 404), (role, up.status_code)
            # [5c] file import (merge, then cleanup) below manager never writes, deletes
            # or counts a hidden key
            if not see:
                imp = c.post(
                    f"/api/projects/{fx['pid']}/keys/import",
                    json={
                        "filename": "__default__.json",
                        "locale": "en-US",
                        "data": json.dumps({HIDDEN_NAME: "Overwritten 540"}),
                    },
                    headers=h,
                )
                with Session(engine) as s:
                    base = keys_svc.base_values(s, s.get(keys_svc.StringKey, fx["hidden_id"]))
                assert "Overwritten 540" not in base.values(), (
                    f"import overwrote the hidden key for {role}: {base}"
                )
                if imp.status_code == 200:
                    assert imp.json()["errors"], (role, "hidden key import not refused")
                else:
                    assert imp.status_code in (403, 404), (role, imp.status_code)
                clean = c.post(
                    f"/api/projects/{fx['pid']}/keys/import",
                    json={
                        "filename": f"{NS}.json",
                        "locale": "en-US",
                        "cleanup": True,
                        "data": json.dumps({"ns_visible": "NS visible"}),
                    },
                    headers=h,
                )
                with Session(engine) as s:
                    gone = s.get(keys_svc.StringKey, fx["ns_hidden_id"]).deleted_at is not None
                assert not gone, f"cleanup import deleted the hidden key for {role}"
                if clean.status_code == 200:
                    assert clean.json()["deleted"] == 0, (role, clean.json())
                else:
                    assert clean.status_code in (403, 404), (role, clean.status_code)
            # [6] run page + graph fragment: no AI dollar or token figure below manager
            run_pg = c.get(f"/runs/{fx['run_id']}")
            assert run_pg.status_code == 200, (role, "run page", run_pg.status_code)
            frag = c.get(f"/runs/{fx['run_id']}/graph-fragment")
            assert frag.status_code == 200, (role, "graph fragment", frag.status_code)
            for label, body in (("run page", run_pg.text), ("graph fragment", frag.text)):
                shown = "0.4321" in body
                assert shown == manager, f"{label} AI cost for {role}: shown={shown}"
            assert ("runSpendTotal" in run_pg.text) == manager, f"run total for {role}"
            assert ("333 tok" in run_pg.text) == manager, f"per-step tokens for {role}"
            # [6b] the JSON run and batch surfaces carry no spend or token field below manager
            if lst.status_code == 200:
                for label, resp in (
                    ("api run", c.get(f"/api/runs/{fx['run_id']}?visits=1", headers=h)),
                    ("api batch", c.get(f"/api/batches/{fx['batch_id']}?detail=steps", headers=h)),
                    (
                        "web batch status",
                        c.get(f"/batches/{fx['batch_id']}/status.json?detail=steps"),
                    ),
                ):
                    assert resp.status_code == 200, (role, label, resp.status_code)
                    for needle in ("0.4321", "cost_usd", "tokens_in", "tokens_out"):
                        assert (needle in resp.text) == manager, (
                            f"{label} {needle} for {role}: shown={needle in resp.text}"
                        )
            # [8] keys translate: a project holding a hidden key is not launched below manager
            if lst.status_code == 200 and role != Role.VIEWER:
                # an unknown target locale keeps a manager's call from launching (and so
                # translating the fixture keys); the hidden-key refusal comes BEFORE that check
                note = "hidden from translators"
                api_tr = c.post(
                    f"/api/projects/{fx['pid']}/keys/translate",
                    json={"workflow_id": fx["wf_id"], "locale": "xx-XX"},
                    headers=h,
                )
                web_tr = c.post(
                    f"/projects/{fx['pid']}/keys/translate",
                    data={"workflow_id": str(fx["wf_id"]), "locale": "xx-XX"},
                    follow_redirects=False,
                )
                web_text = unquote_plus(web_tr.headers.get("location", ""))
                assert (note in api_tr.text) == (not see), (role, "api", api_tr.status_code)
                assert (note in web_text) == (not see), (role, "web", web_tr.status_code, web_text)
            # [7] changes feed and empties panel
            chg_since = c.get(f"/api/projects/{fx['pid']}/changes?since=0", headers=h)
            chg_mod = c.get(f"/api/projects/{fx['pid']}/changes?modified_after={_SINCE}", headers=h)
            emp_api = c.get(f"/api/projects/{fx['pid']}/keys/empties?locale=de-DE", headers=h)
            emp_web = c.get(f"/projects/{fx['pid']}/keys/empties?locale=de-DE")
            if lst.status_code == 200:
                for label, resp, hid_nm, vis_nm in (
                    ("changes since", chg_since, HIDDEN_DONE, VISIBLE_DONE),
                    ("changes modified_after", chg_mod, HIDDEN_DONE, VISIBLE_DONE),
                    ("changes since (empties pair)", chg_since, HIDDEN_NAME, VISIBLE_NAME),
                    ("api empties", emp_api, HIDDEN_NAME, VISIBLE_NAME),
                ):
                    assert resp.status_code == 200, (role, label, resp.status_code)
                    assert vis_nm in resp.text, (role, label, "visible key missing")
                    assert (hid_nm in resp.text) == see, f"{label} hidden key for {role}"
                assert emp_web.status_code == 200, (role, emp_web.status_code)
                assert (HIDDEN_NAME in emp_web.text) == see, f"web empties hidden key for {role}"
                assert VISIBLE_NAME in emp_web.text, (role, "web empties visible key missing")
            print(
                f"  {role}: spend/banner OK; "
                f"connection={conn.status_code} grid={grid.status_code} api-list={lst.status_code}"
            )
    finally:
        spend_mod.would_block = real_would_block
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


if __name__ == "__main__":
    check_all(_seed())
    print("verify_role_gating_540 OK")
