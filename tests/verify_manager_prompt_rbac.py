"""The maintainer's 2026-10-08 decision after #630: managers may edit STEP prompts and change a
group's
default workflow, narrowly, on every surface.

  step    A MANAGER may Unlock, Apply and Restore default on a workflow step's PROMPT: the
          builder shows a manager a prompt-only form (POST /workflows/{wid}/steps/{sid}/prompt,
          nothing but prompt_text) and the Restore default button, and the full step edit form
          stays can_tune_ai. Web Apply and Restore are audited (workflow.edit_step, length and
          hash, never the text) and CSRF-protected. On /api (and so the CLI over --remote and MCP
          edit_step) a manager may send a body with prompt_text ALONE; any other field, pin or
          unpin is 403. The {text} rule still applies. A translator and a viewer are refused
          everywhere; the org ring answers first (a missing workflow is a 404, not a 403).
  group   A MANAGER may make an existing member a group's default, also when that moves an
          inherited system prompt head (#365/#386 needed an admin); a linguist is still refused
          when a head is involved; a translator and a viewer are refused; managing membership
          (add) stays can_tune_ai.
  narrow  A manager is still refused AI tuning that is not a step prompt: adding a step, the
          full step edit, creating a glossary, the Setup AI page; security.can_tune_ai stays
          False for a manager.
Final line: "VERIFY MANAGER PROMPT RBAC OK".
"""

import os
import re
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mgr_prompt_rbac_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role, StepType, User, Workflow, WorkflowStep
from app.services import apikeys, security, settings
from app.services import system_prompt_overrides as spo
from app.services import workflows as wf_service
from tests._csrf import csrf_for

init_db()

FLAG = "system_prompt_overrides_enabled"
STEP0 = "Translate to Spanish: {text}"
STEP1 = "Translate to Spanish, formal register: {text}"
STEP2 = "Translate to Spanish for a manager's customer: {text}"
HEAD = "Workflow rule: keep a formal register. Output only the translation."
PW = "Mgr-prompt-rbac-pass-1008"


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
    return apikeys.create_key(uid, "mgr-rbac", scopes="api docs")[1]


def step_prompt(sid):
    with Session(engine) as s:
        return s.get(WorkflowStep, sid).prompt_text or ""


def step_name(sid):
    with Session(engine) as s:
        return s.get(WorkflowStep, sid).name


def edit_rows(actor):
    with Session(engine) as s:
        return [
            (r.target, r.detail)
            for r in s.exec(select(AuditLog).order_by(AuditLog.id)).all()
            if r.action == "workflow.edit_step" and r.actor == actor
        ]


def make_wf(name, prompt=STEP0, target="es-419"):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global", source_locale="en-US", target_locale=target)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st = WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text=prompt,
            chunk_policy="none",
        )
        s.add(st)
        s.commit()
        s.refresh(st)
        return wf.id, st.id


settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0", FLAG: "1"})
settings.invalidate_cache()
security.seed_initial()
with Session(engine) as s:
    _org = s.exec(select(User).where(User.email == "admin")).first().org_id
MGR, LNG, TRN, VWR = (
    "mgr1008-manager@x.test",
    "mgr1008-linguist@x.test",
    "mgr1008-translator@x.test",
    "mgr1008-viewer@x.test",
)
for email, role in (
    (MGR, Role.MANAGER),
    (LNG, Role.LINGUIST),
    (TRN, Role.TRANSLATOR),
    (VWR, Role.VIEWER),
):
    security.create_user(email, email, role, PW, org_id=_org)

WID, SID = make_wf("WF-mgr1008")
mgr, lng, trn, vwr = (login(TestClient(app), e, PW) for e in (MGR, LNG, TRN, VWR))
api = TestClient(app)
H_MGR, H_LNG, H_TRN = ({"X-Loc-Token": key(e)} for e in (MGR, LNG, TRN))
try:
    H_VWR = {"X-Loc-Token": key(VWR)}
except Exception:  # a viewer may be unable to hold a key at all: refused either way
    H_VWR = None

with Session(engine) as s:
    _users = {e: s.exec(select(User).where(User.email == e)).first() for e in (MGR, LNG, TRN, VWR)}
assert not security.can_tune_ai(_users[MGR]), "a manager must NOT get can_tune_ai"

# ---- step prompt: the page --------------------------------------------------------------------
mp = mgr.get(f"/workflows/{WID}")
assert mp.status_code == 200, mp.status_code
assert f'action="/workflows/{WID}/steps/{SID}/prompt"' in mp.text, "manager: prompt-only form"
assert f'action="/workflows/{WID}/steps/{SID}/edit"' not in mp.text, "no full step edit form"
box = mp.text[mp.text.index(f'id="plk-step-{SID}-box"') :]
box = box[: box.index("</form>")]
assert "data-lock-unlock" in box and "data-lock-apply" in box, "manager: Unlock and Apply"
assert f"/steps/{SID}/restore-prompt" in box, "manager: Restore default prompt"
tags = re.findall(r"<textarea[^>]*>", box)
assert len(tags) == 1 and " readonly" in tags[0] and 'name="prompt_text"' in tags[0]
# the prompt-only form posts nothing but the prompt (and the CSRF token)
names = set(re.findall(r'name="([^"]+)"', box))
assert names <= {"prompt_text", "csrf_token"}, names
lp = lng.get(f"/workflows/{WID}").text
assert f'action="/workflows/{WID}/steps/{SID}/edit"' in lp, "a linguist keeps the full edit form"
assert f'action="/workflows/{WID}/steps/{SID}/prompt"' not in lp
for cl in (trn, vwr):
    tp = cl.get(f"/workflows/{WID}")
    assert tp.status_code != 200 or (
        f"/steps/{SID}/prompt" not in tp.text and f"plk-step-{SID}" not in tp.text
    ), "a translator or viewer gets no step prompt Unlock"
print("  page: a manager unlocks a step prompt (prompt-only form); no full step edit OK")

# ---- step prompt: web Apply, Restore, refusals --------------------------------------------------
r = mgr.post(
    f"/workflows/{WID}/steps/{SID}/prompt",
    data={"prompt_text": STEP1, "csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 303 and r.headers["location"].startswith(f"/workflows/{WID}"), r.status_code
assert step_prompt(SID) == STEP1
rows = edit_rows(MGR)
assert len(rows) == 1 and rows[0][0] == f"workflow {WID} step {SID}", rows
assert "sha256=" in rows[0][1] and STEP1 not in rows[0][1], "hash and length, never the text"
# the {text} rule still applies to a manager's prompt
r = mgr.post(
    f"/workflows/{WID}/steps/{SID}/prompt",
    data={"prompt_text": "No placeholder here", "csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 400 and step_prompt(SID) == STEP1, r.status_code
# the full step edit stays can_tune_ai: a manager is refused and nothing changes
r = mgr.post(
    f"/workflows/{WID}/steps/{SID}/edit",
    data={
        "name": "Renamed by manager",
        "step_type": StepType.LLM_AGENT,
        "prompt_text": STEP2,
        "csrf_token": csrf_for(mgr),
    },
    follow_redirects=False,
)
assert r.status_code == 403, r.status_code
assert step_name(SID) == "Translate" and step_prompt(SID) == STEP1
# no CSRF token: refused by the middleware, nothing saved
_tok = mgr.headers.pop("X-CSRF-Token")
r = mgr.post(
    f"/workflows/{WID}/steps/{SID}/prompt", data={"prompt_text": STEP2}, follow_redirects=False
)
mgr.headers["X-CSRF-Token"] = _tok
assert r.status_code == 303 and "/auth/login" in r.headers["location"], r.status_code
assert step_prompt(SID) == STEP1
# a translator and a viewer are refused on both prompt routes
for cl in (trn, vwr):
    for path, data in (("prompt", {"prompt_text": STEP2}), ("restore-prompt", {})):
        r = cl.post(
            f"/workflows/{WID}/steps/{SID}/{path}",
            data={**data, "csrf_token": csrf_for(cl)},
            follow_redirects=False,
        )
        assert r.status_code == 403, (path, r.status_code)
assert step_prompt(SID) == STEP1
# the ring answers first: a missing workflow is a 404 for a translator, never a role 403 (a
# viewer's POSTs are all refused earlier by the app-wide viewer write guard)
for path in ("prompt", "restore-prompt"):
    r = trn.post(
        f"/workflows/{WID + 999}/steps/{SID}/{path}",
        data={"prompt_text": STEP2, "csrf_token": csrf_for(trn)},
        follow_redirects=False,
    )
    assert r.status_code == 404, (path, r.status_code)
# Restore default prompt
r = mgr.post(
    f"/workflows/{WID}/steps/{SID}/restore-prompt",
    data={"csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 303 and step_prompt(SID) == "", r.status_code
assert edit_rows(MGR)[-1][1] == "changed: prompt_text restored to default (cleared)"
# a linguist still may (can_tune_ai)
r = lng.post(
    f"/workflows/{WID}/steps/{SID}/prompt",
    data={"prompt_text": STEP0, "csrf_token": csrf_for(lng)},
    follow_redirects=False,
)
assert r.status_code == 303 and step_prompt(SID) == STEP0, r.status_code
print("  web: a manager applies and restores a step prompt (audited, CSRF); others refused OK")

# ---- step prompt: /api (the CLI over --remote and MCP edit_step forward here) ------------------
edit = f"/api/workflows/{WID}/steps/{SID}/edit"
r = api.post(edit, headers=H_MGR, json={"prompt_text": STEP2})
assert r.status_code == 200 and r.json()["changed"] == ["prompt_text"], r.text
assert step_prompt(SID) == STEP2
assert edit_rows(MGR)[-1][1] == "changed: prompt_text"
r = api.post(edit, headers=H_MGR, json={"prompt_text": ""})
assert r.status_code == 200 and step_prompt(SID) == "", r.text
r = api.post(edit, headers=H_MGR, json={"prompt_text": "no placeholder"})
assert r.status_code == 400, r.text
for body in (
    {"prompt_text": STEP1, "name": "x"},
    {"name": "x"},
    {"provider": "mock"},
    {"pin": True},
    {"prompt_text": STEP1, "unpin": True},
    {"prompt_template_id": None},
):
    r = api.post(edit, headers=H_MGR, json=body)
    assert r.status_code == 403, (body, r.status_code, r.text)
assert step_prompt(SID) == "" and step_name(SID) == "Translate"
for h in (H_TRN, H_VWR):
    if h is None:
        continue
    r = api.post(edit, headers=h, json={"prompt_text": STEP1})
    assert r.status_code == 403, r.text
assert step_prompt(SID) == ""
r = api.post(
    f"/api/workflows/{WID + 999}/steps/{SID}/edit", headers=H_TRN, json={"prompt_text": STEP1}
)
assert r.status_code == 404, r.status_code
r = api.post(edit, headers=H_LNG, json={"prompt_text": STEP0, "name": "Translate"})
assert r.status_code == 200, r.text
# the CLI's --remote body for `workflow edit-step --prompt X` carries prompt_text alone
from app.cli._remote_impl_common import _edit_step_body

_ns = SimpleNamespace(prompt_text=STEP1)
assert _edit_step_body(_ns) == {"prompt_text": STEP1}, _edit_step_body(_ns)
print("  api: a manager may send prompt_text alone; any other field is 403 OK")

# ---- narrow: a manager still has no other AI tuning --------------------------------------------
r = api.post(
    f"/api/workflows/{WID}/steps",
    headers=H_MGR,
    json={"step_type": "llm_agent", "prompt_text": STEP1},
)
assert r.status_code == 403, r.text
r = api.post("/api/glossaries", headers=H_MGR, json={"name": "Mgr glossary"})
assert r.status_code == 403, r.text
assert mgr.get("/setup-ai", follow_redirects=False).status_code == 403
print("  narrow: a manager is still refused add-step, glossary and Setup AI OK")

# ---- group default -----------------------------------------------------------------------------
BASE, _bs = make_wf("WF-mgr1008-base")
with Session(engine) as s:
    VAR = wf_service.copy_to_pairs(s, BASE, [("en-US", "fr-FR")])["created"][0]["workflow_id"]
    GID = s.get(Workflow, BASE).group_id
    spo.wf_save(s, BASE, "step_head", HEAD, "admin")
assert GID is not None
with Session(engine) as s:
    assert spo.group_default_change_needs_admin(s, VAR, GID, True), "a head is involved"
dflt = f"/api/workflow-groups/{GID}/default"
# a linguist is still refused when a head is involved; a translator and a viewer always
for h in (H_LNG, H_TRN, H_VWR):
    if h is None:
        continue
    r = api.post(dflt, headers=h, json={"workflow_id": VAR})
    assert r.status_code == 403, r.text
for cl in (lng, trn, vwr):
    r = cl.post(
        f"/workflow-groups/{GID}/default",
        data={"workflow_id": VAR, "csrf_token": csrf_for(cl)},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.status_code


def default_of(gid):
    with Session(engine) as s:
        return [w.id for w in wf_service.group_variants(s, gid) if w.is_default]


assert default_of(GID) == [BASE]
# a manager may, on /api ...
r = api.post(dflt, headers=H_MGR, json={"workflow_id": VAR})
assert r.status_code == 200 and r.json()["is_default"] is True, r.text
assert default_of(GID) == [VAR]
# ... and on the web (CSRF kept)
_tok = mgr.headers.pop("X-CSRF-Token")
r = mgr.post(f"/workflow-groups/{GID}/default", data={"workflow_id": BASE}, follow_redirects=False)
mgr.headers["X-CSRF-Token"] = _tok
assert r.status_code == 303 and "/auth/login" in r.headers["location"], r.status_code
assert default_of(GID) == [VAR]
r = mgr.post(
    f"/workflow-groups/{GID}/default",
    data={"workflow_id": BASE, "csrf_token": csrf_for(mgr)},
    follow_redirects=False,
)
assert r.status_code == 303, r.status_code
assert default_of(GID) == [BASE]
# managing membership stays can_tune_ai
PLAIN, _ps = make_wf("WF-mgr1008-plain", target="it-IT")
r = api.post(
    f"/api/workflow-groups/{GID}/add",
    headers=H_MGR,
    json={"workflow_id": PLAIN, "pair": "en-US:it-IT"},
)
assert r.status_code == 403, r.text
# no head involved: a manager may set the default too
NB, _nbs = make_wf("WF-mgr1008-nohead")
with Session(engine) as s:
    NV = wf_service.copy_to_pairs(s, NB, [("en-US", "de-DE")])["created"][0]["workflow_id"]
    NGID = s.get(Workflow, NB).group_id
r = api.post(f"/api/workflow-groups/{NGID}/default", headers=H_MGR, json={"workflow_id": NV})
assert r.status_code == 200, r.text
print("  group: a manager sets a group's default, even when it moves a head; linguist refused OK")

print("VERIFY MANAGER PROMPT RBAC OK")
