"""#630: locked, editable prompts on the workflow page: permissions, audit and four-surface parity.

The browser behaviour (tabs, Unlock/Apply, the lose-changes dialog, the 30 minute re-lock) is in
verify_workflow_prompt_lock. This suite is the server half, offline:

  page    every prompt field (the two System prompt heads and each step's Inline prompt) is rendered
          READONLY. An admin gets Unlock on the heads and on steps; a linguist (can_tune_ai) gets
          Unlock on step prompts only and sees the heads locked with no Unlock; a MANAGER gets
          Unlock on the heads (10-08: one workflow's heads are admin or manager); a
          translator gets neither. With the feature flag off the heads have no Unlock.
  audit   Apply and Restore default prompt on a head, and Apply / Restore default on a step, each
          leave an audit row (action, target, a hash and length, never the prompt text).
  parity  the same read / set / restore-default exist on /api, the CLI (in-process) and the MCP
          registry with the same permission: a manager's API and web head writes work and are
          audited, a linguist's or translator's API write to a head is 403, a step
          prompt edit (and `prompt_text: ""` = restore default) works for can_tune_ai and audits.
Final line: "VERIFY WORKFLOW PROMPT LOCK API OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_prompt_lock_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role, StepType, User, Workflow, WorkflowStep
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

FLAG = "system_prompt_overrides_enabled"
HEAD = "Workflow rule: write in a formal register. Output only the translation."
STEP = "Translate to Spanish with a formal tone: {text}"


def login(c, email, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def key(email):
    with Session(engine) as s:
        uid = s.exec(select(User).where(User.email == email)).first().id
    return apikeys.create_key(uid, "plk-test", scopes="api docs")[1]


def audit_rows(action_prefix):
    with Session(engine) as s:
        return [
            (r.actor, r.action, r.target, r.detail)
            for r in s.exec(select(AuditLog).order_by(AuditLog.id)).all()
            if r.action.startswith(action_prefix)
        ]


def section(html, anchor_id):
    start = html.index(f'id="{anchor_id}"')
    return html[start : html.index("</li>", start)]


settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0", FLAG: "1"})
settings.invalidate_cache()
security.seed_initial()
with Session(engine) as s:
    wf = Workflow(name="WF-lock", scope="global", source_locale="en-US", target_locale="es-419")
    s.add(wf)
    s.commit()
    s.refresh(wf)
    s.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate to Spanish: {text}",
            chunk_policy="none",
        )
    )
    s.commit()
    WID = wf.id
    SID = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == WID)).first().id

MGR_EMAIL, MGR_PW = "plk-manager@x.test", "plk-manager-pass-630"
with Session(engine) as s:
    _adm_org = s.exec(select(User).where(User.email == "admin")).first().org_id
security.create_user(MGR_EMAIL, "Manager", Role.MANAGER, MGR_PW, org_id=_adm_org)

adm, lng, trn, mgr = TestClient(app), TestClient(app), TestClient(app), TestClient(app)
login(adm, "admin", "admin")
login(lng, "linguist", "linguist")
login(trn, "user1", "user1")
login(mgr, MGR_EMAIL, MGR_PW)
api = TestClient(app)
H_ADM = {"X-Loc-Token": key("admin")}
H_LNG = {"X-Loc-Token": key("linguist")}
H_TRN = {"X-Loc-Token": key("user1")}
H_MGR = {"X-Loc-Token": key(MGR_EMAIL)}


def prompt_field_tags(html):
    """Every textarea that holds a prompt on the builder page."""
    return re.findall(r"<textarea[^>]*(?:data-lock-field)[^>]*>", html)


# ---- page: locked by default, Unlock only for who may edit -----------------------------------
page = adm.get(f"/workflows/{WID}").text
tags = prompt_field_tags(page)
assert len(tags) == 3, ("two heads + one step prompt", len(tags))
assert all(" readonly" in t for t in tags), "every prompt field is readonly (not disabled)"
assert not any(" disabled" in t for t in tags), "locked is readonly, never disabled"
assert page.count("data-lock-unlock") == 3, "admin: Unlock on each of the three sections"
assert page.count("data-lock-apply") == 3 and page.count("data-lock-cancel") == 3
assert page.count("data-lock-status") == 3 and 'role="status"' in section(page, "wsp-step_head")
assert "Restore default prompt" in page
sp = section(page, "wsp-step_head")
assert "Locked" in sp, "the lock state is written in words"
assert 'aria-describedby="plk-wsp-step_head' in sp
# each section has its own Unlock/Apply/Restore (per-section, not one global switch)
assert f"plk-step-{SID}" in page
print("  page: every prompt field is readonly; admin sees Unlock on the heads and the step OK")

lp = lng.get(f"/workflows/{WID}").text
ltags = prompt_field_tags(lp)
assert len(ltags) == 1 and " readonly" in ltags[0], "linguist: only the step prompt is a field"
assert lp.count("data-lock-unlock") == 1, "linguist (can_tune_ai) may unlock the step prompt"
heads = section(lp, "wsp-step_head")
assert "data-lock-unlock" not in heads and "<textarea" not in heads
assert "Locked" in heads and "You do not have permission" in heads
assert "Only an admin or a manager can edit this text." in heads

tp = trn.get(f"/workflows/{WID}")
assert tp.status_code != 200 or (
    "data-lock-unlock" not in tp.text and "system-prompt" not in tp.text
)
print("  page: a linguist unlocks step prompts only; a translator unlocks nothing OK")

# 10-08 (#630 gate): a MANAGER may edit one workflow's system prompt heads (Unlock,
# Apply, Restore default) on every surface; the org-level heads stay admin only.
mp = mgr.get(f"/workflows/{WID}")
assert mp.status_code == 200, mp.status_code
for k in ("step_head", "mt_head"):
    msec = section(mp.text, f"wsp-{k}")
    assert "data-lock-unlock" in msec and "data-lock-apply" in msec, ("manager Unlock", k)
    assert re.search(r"<textarea[^>]*data-lock-field[^>]*readonly", msec) or (
        " readonly" in re.findall(r"<textarea[^>]*>", msec)[0]
    ), "a manager's head is still locked until Unlock"
    assert "Restore default prompt" in msec
r = mgr.post(
    f"/workflows/{WID}/system-prompts/step_head/save",
    data={"body": HEAD, "csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 303 and "sp=saved" in r.headers["location"], r.status_code
r = mgr.post(
    f"/workflows/{WID}/system-prompts/step_head/reset",
    data={"csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 303 and "sp=reset" in r.headers["location"], r.status_code
mrows = [x for x in audit_rows("workflow.system_prompt_") if x[0] == MGR_EMAIL]
assert [a for _w, a, _t, _d in mrows] == [
    "workflow.system_prompt_set",
    "workflow.system_prompt_reset",
], mrows
# the web head routes still refuse a linguist and a translator (and keep CSRF)
from app.models import WorkflowSystemPromptOverride as _WSPO


def _wspo_rows():
    with Session(engine) as s:
        return len([o for o in s.exec(select(_WSPO)).all() if o.workflow_id == WID])


n_rows = _wspo_rows()
for cl in (lng, trn):
    r = cl.post(
        f"/workflows/{WID}/system-prompts/step_head/save",
        data={"body": HEAD, "csrf_token": csrf_for(cl)},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 403) and "sp=saved" not in r.headers.get("location", "")
_tok = mgr.headers.pop("X-CSRF-Token")  # csrf_for installs it as a default header
r = mgr.post(
    f"/workflows/{WID}/system-prompts/step_head/save", data={"body": HEAD}, follow_redirects=False
)
mgr.headers["X-CSRF-Token"] = _tok
# a form post with no CSRF token is refused by the middleware (sent to re-login, nothing saved)
assert r.status_code == 303 and "/auth/login" in r.headers["location"], r.status_code
assert _wspo_rows() == n_rows, "a refused head write saved nothing"
# the org-level heads stay admin only
assert (
    api.put("/api/system-prompts/step_head", headers=H_MGR, json={"body": HEAD}).status_code == 403
)
print("  page: a manager unlocks, applies and restores the heads (web, audited, CSRF) OK")

settings.set_many({FLAG: "0"})
settings.invalidate_cache()
off = adm.get(f"/workflows/{WID}").text
offh = section(off, "wsp-step_head")
assert "data-lock-unlock" not in offh and "Editing is switched off" in offh
assert off.count("data-lock-unlock") == 1, "the step prompt still unlocks with the flag off"
settings.set_many({FLAG: "1"})
settings.invalidate_cache()
print("  page: feature flag off locks the heads with no Unlock OK")

# ---- audit: Apply and Restore default on a head (web) ----------------------------------------
r = adm.post(
    f"/workflows/{WID}/system-prompts/step_head/save",
    data={"body": HEAD, "csrf_token": csrf_for(adm)},
    follow_redirects=False,
)
assert r.status_code == 303 and "sp=saved" in r.headers["location"]
r = adm.post(
    f"/workflows/{WID}/system-prompts/step_head/reset",
    data={"csrf_token": csrf_for(adm)},
    follow_redirects=False,
)
assert r.status_code == 303 and "sp=reset" in r.headers["location"]
rows = [x for x in audit_rows("workflow.system_prompt_") if x[0] != MGR_EMAIL]  # the admin's
actions = [a for _who, a, _t, _d in rows]
assert actions == ["workflow.system_prompt_set", "workflow.system_prompt_reset"], actions
assert all(f"workflow {WID} system_prompt step_head" == t for _w, _a, t, _d in rows)
assert all(HEAD not in d for _w, _a, _t, d in rows), "the prompt text is never in the audit row"
assert "sha256=" in rows[0][3] and "chars=" in rows[0][3]
print("  audit: Apply and Restore default prompt on a head are audited (hash and length) OK")

# ---- audit: step prompt Apply / Restore default (web edit form) ------------------------------
edit_url = f"/workflows/{WID}/steps/{SID}/edit"
page = adm.get(f"/workflows/{WID}").text
form = page[page.index(f'action="{edit_url}"') :]
form = form[: form.index("</form>")]
fields = {}
for m in re.finditer(r"<input[^>]*name=\"([^\"]+)\"[^>]*>", form):
    tag = m.group(0)
    if 'type="checkbox"' in tag or 'type="submit"' in tag:
        continue
    v = re.search(r'value="([^"]*)"', tag)
    fields[m.group(1)] = v.group(1) if v else ""
for m in re.finditer(r"<select[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</select>", form, re.S):
    sel = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(2))
    fields[m.group(1)] = sel.group(1) if sel else ""
fields["step_type"] = "llm_agent"
fields["prompt_text"] = STEP
fields["csrf_token"] = csrf_for(adm)
before = audit_rows("workflow.edit_step")
r = adm.post(edit_url, data=fields, follow_redirects=False)
assert r.status_code == 303, (r.status_code, r.text[:400])
rows = audit_rows("workflow.edit_step")[len(before) :]
assert len(rows) == 1 and "prompt_text" in rows[0][3] and "sha256=" in rows[0][3], rows
assert STEP not in rows[0][3], "the prompt text is never in the audit row"
# an edit that does not touch the prompt is not a prompt audit row
r = adm.post(edit_url, data={**fields, "csrf_token": csrf_for(adm)}, follow_redirects=False)
assert r.status_code == 303 and len(audit_rows("workflow.edit_step")) == len(before) + 1
# Restore default prompt = its OWN route (#630): clears the prompt only. Other edits typed in
# the step's edit form (here a new name and provider) must NOT be saved by a restore.
restore_url = f"/workflows/{WID}/steps/{SID}/restore-prompt"
with Session(engine) as s:
    assert (s.get(WorkflowStep, SID).prompt_text or "") == STEP
    name_before = s.get(WorkflowStep, SID).name
r = adm.post(
    restore_url,
    data={**fields, "name": "UNSAVED-RENAME", "provider": "other", "csrf_token": csrf_for(adm)},
    follow_redirects=False,
)
assert r.status_code == 303, (r.status_code, r.text[:300])
rows = audit_rows("workflow.edit_step")[len(before) :]
assert len(rows) == 2 and "restored to default" in rows[1][3], rows
with Session(engine) as s:
    st = s.get(WorkflowStep, SID)
    assert (st.prompt_text or "") == ""
    assert st.name == name_before and st.name != "UNSAVED-RENAME", "restore saved another field"
    assert st.provider == "mock", "restore saved another field"
# the template wires the button to that route, not to the edit form's action
page = adm.get(f"/workflows/{WID}").text
assert f'data-restore-action="{restore_url}"' in page
# a restore on an already-default prompt is not a new audit row
r = adm.post(restore_url, data={"csrf_token": csrf_for(adm)}, follow_redirects=False)
assert r.status_code == 303 and len(audit_rows("workflow.edit_step")) == len(before) + 2
# a translator cannot reach it; neither can an anonymous caller
with Session(engine) as s:
    keep = s.get(WorkflowStep, SID)
    keep.prompt_text = STEP
    s.add(keep)
    s.commit()
r = trn.post(restore_url, data={"csrf_token": csrf_for(trn)}, follow_redirects=False)
assert r.status_code in (302, 303, 401, 403), r.status_code
with Session(engine) as s:
    assert (s.get(WorkflowStep, SID).prompt_text or "") == STEP, "translator restored a prompt"
r = lng.post(restore_url, data={"csrf_token": csrf_for(lng)}, follow_redirects=False)
assert r.status_code == 303, "can_tune_ai may restore a step prompt"
with Session(engine) as s:
    assert (s.get(WorkflowStep, SID).prompt_text or "") == ""
print("  audit: step prompt Apply and Restore default are audited on the web form OK")

# ---- permissions: a translator cannot write a step prompt ------------------------------------
r = trn.post(edit_url, data={**fields, "prompt_text": "x {text}", "csrf_token": csrf_for(trn)})
assert r.status_code in (302, 303, 401, 403), r.status_code
with Session(engine) as s:
    assert (s.get(WorkflowStep, SID).prompt_text or "") == ""

# ---- parity: API (same permission, same audit) ----------------------------------------------
base = f"/api/workflows/{WID}/system-prompts/step_head"
assert api.get(base, headers=H_LNG).status_code == 200, "can_tune_ai reads"
assert api.put(base, headers=H_LNG, json={"body": HEAD}).status_code == 403, "head writes: admin"
assert api.put(base, headers=H_TRN, json={"body": HEAD}).status_code == 403
assert api.get(base, headers=H_TRN).status_code == 403, "a translator does not read the heads"
mg = api.get(base, headers=H_MGR)
assert mg.status_code == 200 and mg.json()["can_edit"] is True, mg.text
assert api.put(base, headers=H_MGR, json={"body": HEAD}).status_code == 200, "manager writes"
rd = api.post(base + "/restore-default", headers=H_MGR, json={})
assert rd.status_code == 200 and rd.json()["changed"] is True, rd.text
assert api.get(base, headers=H_LNG).json()["can_edit"] is False
assert api.put(base, headers=H_ADM, json={"body": HEAD}).status_code == 200
assert api.get(base, headers=H_ADM).json()["prompt"]["override"]["body"] == HEAD
assert api.delete(base, headers=H_ADM).status_code == 200  # restore default
step_url = f"/api/workflows/{WID}/steps/{SID}/edit"
ra = api.post(step_url, headers=H_LNG, json={"prompt_text": STEP})
assert ra.status_code == 200, ra.text  # can_tune_ai
rr = api.post(step_url, headers=H_LNG, json={"prompt_text": ""})  # restore default prompt
assert rr.status_code == 200, rr.text
api_rows = [
    (a, d) for _w, a, _t, d in audit_rows("workflow.edit_step") if "changed: prompt_text" in d
]
assert len(api_rows) >= 2
print("  parity: API read / set / restore default, same permission, audited OK")

# ---- parity: CLI in-process and MCP registry --------------------------------------------------
from tests._cli_runner import run_cli

code, out = run_cli(
    [
        "workflow",
        "prompt",
        "system-set",
        "--workflow",
        str(WID),
        "--key",
        "step_head",
        "--body",
        HEAD,
    ]
)[:2]
assert code == 0, out
code, out = run_cli(["workflow", "prompt", "system", "--workflow", str(WID), "--key", "step_head"])[
    :2
]
assert code == 0 and HEAD in out, out
code, out = run_cli(
    ["workflow", "prompt", "system-reset", "--workflow", str(WID), "--key", "step_head"]
)[:2]
assert code == 0, out
code, out = run_cli(["workflow", "edit-step", str(WID), str(SID), "--prompt", ""])[:2]
assert code == 0, out
sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
import registry  # stdlib-only data module

edit_workflow = registry.tool_by_name("edit_workflow")
claimed = set(edit_workflow.routes)
for route in (
    ("GET", "/api/workflows/{wid}/system-prompts"),  # read
    ("PUT", "/api/workflows/{wid}/system-prompts/{key}"),  # set
    ("POST", "/api/workflows/{wid}/system-prompts/{key}/restore-default"),  # restore default
    ("POST", "/api/workflows/{wid}/steps/{sid}/edit"),  # a step prompt (prompt_text "" = default)
):
    assert route in claimed, route
assert ("DELETE", "/api/workflows/{wid}/system-prompts/{key}") in registry.NEVER_COVER
actions = edit_workflow.input_schema["properties"]["action"]["enum"]
assert {"get_system_prompts", "set_system_prompt", "restore_default_system_prompt"} <= set(actions)
# the new restore-default route is the DELETE's twin: same gate, same audit row
base = f"/api/workflows/{WID}/system-prompts/mt_head"
assert (
    api.put(base, headers=H_ADM, json={"body": "not a chat assistant. " + HEAD}).status_code == 200
)
assert api.post(base + "/restore-default", headers=H_LNG, json={}).status_code == 403
rd = api.post(base + "/restore-default", headers=H_ADM, json={})
assert rd.status_code == 200 and rd.json()["changed"] is True, rd.text
assert api.get(base, headers=H_ADM).json()["prompt"]["override"] is None
assert audit_rows("workflow.system_prompt_reset")[-1][2] == f"workflow {WID} system_prompt mt_head"
with open(os.path.join(_ROOT, "app/templates/help.html"), encoding="utf-8") as fh:
    helptxt = fh.read()
assert "Restore default prompt" in helptxt, "help names the Restore default prompt button"
print("  parity: CLI and the MCP registry carry read / set / restore default OK")
print("VERIFY WORKFLOW PROMPT LOCK API OK")
