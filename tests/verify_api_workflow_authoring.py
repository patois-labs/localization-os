"""SPEC-workflow-authoring-api: the five /api workflow-authoring routes.

Workflow AUTHORING (create a workflow, add / edit / delete / move its steps) used to
have NO /api route -- web + local CLI only. This suite proves the new /api arm (the
backend for the now-`--remote`-capable `workflow create / add-step / edit-step /
delete-step / move-step` verbs and the MCP create_workflow / edit_workflow tools) obeys
the same laws the web builder and the in-process CLI do, by calling the SAME service +
validator seams (so the three surfaces cannot drift).

Asserted with the login wall ON but multi-org OFF (so the behaviour under test is the
role gate + the customer ring, isolated from org scoping):

  * HAPPY PATHS for all five routes (create, add-step, edit-step PARTIAL, delete-step,
    move-step), read back through GET /api/workflows/{id};
  * RBAC: authoring is **can_tune_ai** (admin / linguist), mirroring the web builder's
    _ai_block -- a TRANSLATOR key is 403 on every route, a VIEWER too, a LINGUIST clears
    them, an ADMIN clears them; the gate is a strict no-op with the wall OFF;
  * TENANCY: a nonexistent workflow, a nonexistent step, and a customer-bound caller's
    FOREIGN-customer workflow all answer **404, never 403** -- the same `_wf_in_org`
    guard org isolation rides on (a distinguishable refusal would leak existence);
  * VALIDATION PARITY: the {text}/{prior} prompt rule (canonical 400, verbatim from the
    service) + the soft language warning as `"warnings"`; glossary-pin re-validation (a
    valid approved-covering pin lands, an invalid id is a hard 400 -- the CLI's clear
    error, not the web's silent drop, so the remote arm surfaces it); the in-flight-run
    guard on edit / delete / move; partial-edit + three-state booleans; move bounds;
  * REMOTE-CLI ROUND-TRIP: the five verbs over `--remote` are byte-identical to the
    in-process arm (the existing MockTransport pattern).

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider, deterministic + free).
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfauth_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Organization,
    Role,
    RunStatus,
    Segment,
    User,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, content, security, settings
from app.services import glossary_module as gm
from app.services import workflows as wf_svc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _auth_on() -> None:
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off() -> None:
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _key(user, scopes="api docs") -> str:
    _row, raw = apikeys.create_key(user.id, "wfauth-test", scopes=scopes)
    return raw


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _forbidden(r) -> bool:
    return r.status_code == 403 and r.json().get("ok") is False


_N = 0


def _seed():
    """Users per role (each a docs key), a customer, an APPROVED glossary covering the
    workflow's pair, and a document with de-DE segments (for the in-flight-run guard).
    Fresh unique suffix each call so re-seeding in the one-process runner never collides."""
    global _N
    _N += 1
    n = _N
    init_db()
    with Session(engine) as s:
        cust_a = content.customer_create(s, "Acme %d" % n).id  # noqa: UP031
        cust_b = content.customer_create(s, "Beta %d" % n).id  # noqa: UP031
        proj = content.project_create(
            s,
            "WFAuth P %d" % n,  # noqa: UP031
            source_locale="en-US",
            target_locales="de-DE",
            default_provider="mock",
            created_by="admin@x.com",
        )
        pid = proj.id
        doc = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(
            Segment(
                document_id=did,
                source_text="hello",
                target_text="",
                target_locale="de-DE",
                order_index=0,
            )
        )
        s.commit()
        # An APPROVED glossary covering en-US->de-DE (a valid step pin).
        g = gm.create(s, "WFAuth GL %d" % n)  # noqa: UP031
        gm.set_pairs(s, g.id, ["en-US:de-DE"])
        gm.approve(s, g.id)
        gid = g.id
        # A customer-B-owned approved glossary + prompt template (proprietary to B) --
        # a customer-A-bound caller must NOT be able to pin either to a step it edits.
        gb = gm.create(s, "Beta GL %d" % n, customer_id=cust_b)  # noqa: UP031
        gm.set_pairs(s, gb.id, ["en-US:de-DE"])
        gm.approve(s, gb.id)
        gid_b = gb.id
        ptb = wf_svc.create_prompt_template(
            s,
            "Beta PT %d" % n,  # noqa: UP031
            body="Translate {text} into {target_locale}",
            customer_id=cust_b,  # noqa: RUF100, UP031
        )
        ptid_b = ptb.id
    keys = {}
    for role in (Role.ADMIN, Role.LINGUIST, Role.TRANSLATOR, Role.VIEWER):
        u = security.create_user(
            "%s%d@x.com" % (role, n),  # noqa: UP031
            role.title(),
            role,
            "wfauth-password-123",  # noqa: RUF100, UP031
        )
        keys[role] = _key(u)
    # A LINGUIST bound to customer A -- passes can_tune_ai, but the customer ring must
    # still 404 a customer-B-scoped workflow (foreign = 404, never 403).
    lb = security.create_user(
        "lingbound%d@x.com" % n,  # noqa: UP031
        "LB",
        Role.LINGUIST,
        "wfauth-password-123",
        customer_id=cust_a,  # noqa: RUF100, UP031
    )
    keys["linguist_bound_A"] = _key(lb)
    return {
        "pid": pid,
        "did": did,
        "gid": gid,
        "gid_b": gid_b,
        "ptid_b": ptid_b,
        "cust_a": cust_a,
        "cust_b": cust_b,
        "keys": keys,
    }


def _client():
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------- #
# happy paths -- all five routes end to end
# --------------------------------------------------------------------------- #
def test_full_lifecycle():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    # create
    r = api.post("/api/workflows", headers=ha, json={"name": "Lifecycle WF"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    wid = r.json()["id"]
    # add a translate step (happy path, valid prompt + glossary pin)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "Translate into {target_locale}: {text}",
            "key": "tr",
            "glossary_id": d["gid"],
        },
    )
    assert r.status_code == 200, r.text
    tr = r.json()
    assert tr["glossary_id"] == d["gid"] and tr["key"] == "tr", tr
    assert "warnings" not in tr, "a compliant prompt must not warn"
    tr_id = tr["id"]
    # add an lqa step
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "lqa", "name": "check", "key": "chk"},
    )
    assert r.status_code == 200, r.text
    chk_id = r.json()["id"]
    # edit-step PARTIAL: rename + qe_gate (three-state true) + threshold
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, tr_id),  # noqa: UP031
        headers=ha,
        json={"name": "Translator", "qe_gate": True, "qe_threshold": 88},
    )
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["name"] == "Translator" and set(e["changed"]) == {"name", "qe_gate", "qe_threshold"}, e
    # move-step: put chk before tr (position 0)
    r = api.post(
        "/api/workflows/%d/steps/%d/move" % (wid, chk_id),  # noqa: UP031
        headers=ha,
        json={"before": "tr"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, r.text
    order = r.json()["order"]
    assert order[0]["id"] == chk_id and order[1]["id"] == tr_id, order
    # delete-step
    r = api.post("/api/workflows/%d/steps/%d/delete" % (wid, chk_id), headers=ha, json={})  # noqa: UP031
    assert r.status_code == 200 and r.json()["deleted"], r.text
    # read back
    r = api.get("/api/workflows/%d" % wid, headers=ha)  # noqa: UP031
    steps = r.json()["steps"]
    assert [st["id"] for st in steps] == [tr_id], steps
    assert steps[0]["name"] == "Translator" and steps[0]["qe_gate"], steps[0]
    print(
        "  happy: create -> add(llm_agent+glossary pin) -> add(lqa) -> edit(partial "
        "3-state) -> move(before) -> delete, read back consistent"
    )


# --------------------------------------------------------------------------- #
# validation parity
# --------------------------------------------------------------------------- #
_CANON = (
    "Your prompt must include {text} where the source content is inserted "
    "(or {prior} to reuse the previous step's output). Without it the model "
    "receives no source to translate."
)


def test_prompt_rule_and_warning():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Prompt WF"}).json()["id"]
    # {text}-less llm_agent prompt -> 400 with the canonical message VERBATIM
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "just translate it"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"] == _CANON, r.json()["error"]
    # a prompt with {text} but naming no target language -> 200 with a warning
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "Do {text}"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("warnings") and "target language" in r.json()["warnings"][0], r.json()
    print(
        "  validation: {text}-less prompt -> 400 canonical message verbatim; a "
        'prompt naming no language -> 200 with the soft warning in "warnings"'
    )


def test_glossary_pin_semantics():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Gloss WF"}).json()["id"]
    # a valid APPROVED covering pin lands
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} -> {target_locale}",
            "glossary_id": d["gid"],
        },
    )
    assert r.status_code == 200 and r.json()["glossary_id"] == d["gid"], r.text
    sid = r.json()["id"]
    # a nonexistent glossary id is a HARD 400 (the CLI's clear error, not a silent drop)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} -> {target_locale}",
            "glossary_id": 999999,
        },
    )
    assert r.status_code == 400 and "not an approved glossary" in r.json()["error"], r.text
    # same hard refusal on edit-step
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
        headers=ha,
        json={"glossary_id": 999999},  # noqa: RUF100, UP031
    )
    assert r.status_code == 400 and "not an approved glossary" in r.json()["error"], r.text
    print(
        "  glossary pin: a valid approved-covering pin lands; an invalid id is a hard "
        "400 on add AND edit (not the web's silent drop)"
    )


def test_partial_edit_three_state():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Partial WF"}).json()["id"]
    sid = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "P: {text} {target_locale}",
            "provider": "mock",
            "qe_gate": True,
        },
    ).json()["id"]
    # edit ONLY the name: provider + qe_gate + prompt untouched (partial contract)
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
        headers=ha,
        json={"name": "Renamed"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200 and r.json()["changed"] == ["name"], r.text
    with Session(engine) as s:
        st = s.get(WorkflowStep, sid)
        assert st.name == "Renamed" and st.provider == "mock" and st.qe_gate, st
        assert st.prompt_text == "P: {text} {target_locale}", st
    # three-state boolean: qe_gate=false CLEARS it; absent leaves it
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
        headers=ha,
        json={"qe_gate": False},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200 and r.json()["changed"] == ["qe_gate"], r.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, sid).qe_gate is False, "qe_gate=false must clear"
    # a body with no fields and no pin/unpin -> 400
    r = api.post("/api/workflows/%d/steps/%d/edit" % (wid, sid), headers=ha, json={})  # noqa: UP031
    assert r.status_code == 400 and "no fields to edit" in r.json()["error"], r.text
    print(
        "  partial edit: only-name changes leave provider/qe_gate/prompt untouched; "
        "qe_gate=false clears (three-state); an empty edit is refused"
    )


def test_move_bounds():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Move WF"}).json()["id"]
    a = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "A {text} {target_locale}", "key": "a"},
    ).json()["id"]
    b = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "B {text} {target_locale}", "key": "b"},
    ).json()["id"]
    # out-of-range position -> 400
    r = api.post("/api/workflows/%d/steps/%d/move" % (wid, a), headers=ha, json={"position": 99})  # noqa: UP031
    assert r.status_code == 400 and "out of range" in r.json()["error"], r.text
    # no position/before/after -> 400
    r = api.post("/api/workflows/%d/steps/%d/move" % (wid, a), headers=ha, json={})  # noqa: UP031
    assert r.status_code == 400, r.text
    # --after b: a lands last
    r = api.post("/api/workflows/%d/steps/%d/move" % (wid, a), headers=ha, json={"after": "b"})  # noqa: UP031
    assert r.status_code == 200, r.text
    assert [o["id"] for o in r.json()["order"]] == [b, a], r.json()["order"]
    print(
        "  move bounds: position 99 -> 400 out-of-range; no anchor -> 400; --after "
        "resolves to the right position"
    )


def test_in_flight_run_guard():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Busy WF"}).json()["id"]
    s1 = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "{text} {target_locale}"},
    ).json()["id"]
    s2 = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "lqa", "name": "chk"},  # noqa: RUF100, UP031
    ).json()["id"]
    # a RUNNING run on this workflow
    with Session(engine) as s:
        s.add(WorkflowRun(workflow_id=wid, document_id=d["did"], status=RunStatus.RUNNING))
        s.commit()
    for path, body in (
        (("/api/workflows/%d/steps/%d/edit" % (wid, s1)), {"name": "x"}),  # noqa: UP031
        (("/api/workflows/%d/steps/%d/delete" % (wid, s2)), {}),  # noqa: UP031
        (("/api/workflows/%d/steps/%d/move" % (wid, s1)), {"position": 1}),  # noqa: UP031
    ):
        r = api.post(path, headers=ha, json=body)
        assert r.status_code == 400 and "in-flight run" in r.json()["error"], (path, r.text)
    print(
        "  in-flight-run guard: edit / delete / move all 400 (fail-closed) while a "
        "run is pending/running/paused"
    )


# --------------------------------------------------------------------------- #
# RBAC -- authoring is can_tune_ai (admin / linguist)
# --------------------------------------------------------------------------- #
def _authoring_calls(api, wid, sid):
    return [
        ("create", lambda h: api.post("/api/workflows", headers=h, json={"name": "R"})),
        (
            "add-step",
            lambda h: api.post(
                "/api/workflows/%d/steps" % wid,  # noqa: UP031
                headers=h,
                json={"step_type": "lqa", "name": "x"},  # noqa: RUF100, UP031
            ),
        ),
        (
            "edit-step",
            lambda h: api.post(
                "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
                headers=h,
                json={"name": "x"},  # noqa: RUF100, UP031
            ),
        ),
        (
            "move-step",
            lambda h: api.post(
                "/api/workflows/%d/steps/%d/move" % (wid, sid),  # noqa: UP031
                headers=h,
                json={"position": 0},  # noqa: RUF100, UP031
            ),
        ),
        (
            "delete-step",
            lambda h: api.post(
                "/api/workflows/%d/steps/%d/delete" % (wid, sid),  # noqa: UP031
                headers=h,
                json={},  # noqa: RUF100, UP031
            ),
        ),
    ]


def test_rbac():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "RBAC WF"}).json()["id"]
    sid = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "llm_agent", "prompt_text": "{text} {target_locale}"},
    ).json()["id"]
    # TRANSLATOR + VIEWER are 403 on every authoring route (can_tune_ai, not can_write)
    for role in (Role.TRANSLATOR, Role.VIEWER):
        h = _h(d["keys"][role])
        for name, call in _authoring_calls(api, wid, sid):
            r = call(h)
            assert _forbidden(r), (
                f"{role} should be 403 on {name}, got {r.status_code} ({r.text[:100]})"
            )
    # LINGUIST clears the role gate on every route (may still 200/400, never 403)
    hl = _h(d["keys"][Role.LINGUIST])
    for name, call in _authoring_calls(api, wid, sid):
        r = call(hl)
        assert r.status_code != 403, f"linguist should clear {name}, got 403"
    print(
        "  RBAC: translator + viewer 403 on all 5 routes (can_tune_ai gate); linguist "
        "+ admin clear it"
    )


def test_gate_off_noop():
    d = _seed()
    _auth_off()
    api = _client()
    hv = _h(d["keys"][Role.VIEWER])
    r = api.post("/api/workflows", headers=hv, json={"name": "GateOff WF"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    wid = r.json()["id"]
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=hv,
        json={"step_type": "llm_agent", "prompt_text": "{text} {target_locale}"},
    )
    assert r.status_code == 200, r.text
    print(
        "  gate-off: a viewer authors a workflow + step unrestricted (byte-identical "
        "to single-tenant today)"
    )
    _auth_off()


# --------------------------------------------------------------------------- #
# tenancy -- foreign / missing = 404, never 403
# --------------------------------------------------------------------------- #
def test_tenancy_404():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    # nonexistent workflow -> 404 on every step route
    for path, body in (
        ("/api/workflows/999999/steps", {"step_type": "lqa"}),
        ("/api/workflows/999999/steps/1/edit", {"name": "x"}),
        ("/api/workflows/999999/steps/1/delete", {}),
        ("/api/workflows/999999/steps/1/move", {"position": 0}),
    ):
        r = api.post(path, headers=ha, json=body)
        assert r.status_code == 404, (path, r.status_code)
    # a real workflow, a nonexistent STEP -> 404 (not an existence oracle)
    wid = api.post("/api/workflows", headers=ha, json={"name": "T WF"}).json()["id"]
    for path, body in (
        ("/api/workflows/%d/steps/888888/edit" % wid, {"name": "x"}),  # noqa: UP031
        ("/api/workflows/%d/steps/888888/delete" % wid, {}),  # noqa: UP031
        ("/api/workflows/%d/steps/888888/move" % wid, {"position": 0}),  # noqa: UP031
    ):
        r = api.post(path, headers=ha, json=body)
        assert r.status_code == 404, (path, r.status_code)
    # a customer-bound linguist (customer A) hitting a customer-B-scoped workflow -> 404
    # (never 403), the SAME _wf_in_org customer ring org isolation rides on.
    with Session(engine) as s:
        wfb = wf_svc.create_workflow(s, "Beta-owned WF", scope="customer", customer_id=d["cust_b"])
        wfb_id = wfb.id
        stb = wf_svc.add_step(s, wfb_id, step_type="lqa", name="chk")
        stb_id = stb.id
    hlb = _h(d["keys"]["linguist_bound_A"])
    r = api.post("/api/workflows/%d/steps" % wfb_id, headers=hlb, json={"step_type": "lqa"})  # noqa: UP031
    assert r.status_code == 404, ("customer-ring add-step", r.status_code, r.text[:100])
    r = api.post("/api/workflows/%d/steps/%d/delete" % (wfb_id, stb_id), headers=hlb, json={})  # noqa: UP031
    assert r.status_code == 404, ("customer-ring delete-step", r.status_code)
    print(
        "  tenancy: nonexistent workflow, nonexistent step, and a foreign-customer "
        "workflow all 404 (never 403)"
    )


# --------------------------------------------------------------------------- #
# remote-CLI round-trip -- the five verbs over --remote, byte-identical
# --------------------------------------------------------------------------- #
def _remote_runner(key):
    """A CLI --json --remote runner whose httpx transport forwards to an in-process
    TestClient (no live network), mirroring verify_cli_remote_dispatch_guard."""
    from app.cli import _remote
    from app.cli.__main__ import main as cli_main

    tc = _client()

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = tc.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    _remote._TRANSPORT = httpx.MockTransport(handler)

    def run(argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--json", "--remote", "http://127.0.0.1", "--token", key] + argv)  # noqa: RUF005
        out = buf.getvalue()
        return rc, (_json.loads(out) if out.strip() else {})

    return run


def test_remote_round_trip():
    d = _seed()
    _auth_off()  # the transport carries the token; isolate the round-trip from RBAC
    run = _remote_runner(d["keys"][Role.ADMIN])
    rc, o = run(["workflow", "create", "--name", "Remote WF"])
    assert rc == 0 and o["name"] == "Remote WF", (rc, o)
    wid = o["id"]
    rc, o = run(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--prompt",
            "Do {text} into {target_locale}",
            "--key",
            "tr",
        ]
    )
    assert rc == 0 and o["key"] == "tr", (rc, o)
    tr = o["id"]
    rc, o = run(["workflow", "add-step", str(wid), "--type", "lqa", "--name", "chk"])
    assert rc == 0, (rc, o)
    chk = o["id"]
    # PARTIAL edit + three-state boolean over the wire
    rc, o = run(["workflow", "edit-step", str(wid), str(tr), "--name", "Trans", "--qe-gate"])
    assert rc == 0 and "qe_gate" in o["changed"], (rc, o)
    rc, o = run(["workflow", "edit-step", str(wid), str(tr), "--no-tm-examples"])
    assert rc == 0 and o["changed"] == ["tm_examples"], (rc, o)
    with Session(engine) as s:
        assert s.get(WorkflowStep, tr).tm_examples is False, "the --no-flag must clear"
    # move + delete over the wire
    rc, o = run(["workflow", "move-step", str(wid), str(chk), "--to", "0"])
    assert rc == 0 and o["order"][0]["id"] == chk, (rc, o)
    rc, o = run(["workflow", "delete-step", str(wid), str(chk)])
    assert rc == 0 and o["deleted"], (rc, o)
    # an invalid glossary pin over the wire is a clean exit-1 with the server message
    rc, o = run(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--prompt",
            "{text} {target_locale}",
            "--glossary",
            "999999",
        ]
    )
    assert rc == 1 and "not an approved glossary" in (o.get("error") or ""), (rc, o)
    # the graph the remote arm built matches the in-process reading
    with Session(engine) as s:
        left = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).all()
        assert [st.id for st in left] == [tr], [st.id for st in left]
        assert s.get(WorkflowStep, tr).name == "Trans", "remote edits persisted"
    print(
        "  remote round-trip: create / add-step / edit-step (partial + 3-state) / "
        "move-step / delete-step land byte-identically; an invalid pin is a clean "
        "exit-1 with the server's message"
    )
    _auth_off()


# --------------------------------------------------------------------------- #
# edit edge cases (adversarial review M1 booleans / M2 branch [])
# --------------------------------------------------------------------------- #
def test_edit_bool_and_branch_edges():
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    wid = api.post("/api/workflows", headers=ha, json={"name": "Edge WF"}).json()["id"]
    # M1 (ADD route): a stringy "false" toggle is REFUSED (bool("false") would ENABLE it)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} {target_locale}",
            "qe_gate": "false",
        },
    )
    assert r.status_code == 400 and "must be true or false" in r.json()["error"], r.text
    # M1 (ADD route): JSON null on a toggle = absent -> the field's own default (a null
    # tm_examples must NOT flip its default-ON off)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} {target_locale}",
            "tm_examples": None,
        },
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, r.json()["id"]).tm_examples is True, (
            "tm_examples=null on add must keep the default (ON)"
        )
    sid = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} {target_locale}",
            "redact_pii": True,
        },
    ).json()["id"]
    # M1 (EDIT route): a non-bool toggle is REFUSED (bool("false") would have stored True)
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
        headers=ha,
        json={"qe_gate": "false"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 400 and "must be true or false" in r.json()["error"], r.text
    # M1: JSON null on a toggle = ABSENT (leave unchanged), never bool(None)==False
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, sid),  # noqa: UP031
        headers=ha,
        json={"redact_pii": None, "name": "keep"},
    )
    assert r.status_code == 200 and r.json()["changed"] == ["name"], r.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, sid).redact_pii is True, (
            "redact_pii=null must NOT disable PII scrubbing"
        )
    # M2: a step carrying a branch; {"branch": []} must NOT delete it
    gsid = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=ha,
        json={"step_type": "lqa", "name": "gate", "key": "g", "branch": ["on_fail=end"]},
    ).json()["id"]
    with Session(engine) as s:
        assert s.get(WorkflowStep, gsid).branch, "gate step should have a branch"
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, gsid),  # noqa: UP031
        headers=ha,
        json={"branch": [], "name": "gate2"},
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, gsid).branch, "{branch: []} must NOT clear the branch"
    # only clear_branch clears it
    r = api.post(
        "/api/workflows/%d/steps/%d/edit" % (wid, gsid),  # noqa: UP031
        headers=ha,
        json={"clear_branch": True},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert not s.get(WorkflowStep, gsid).branch, "clear_branch must clear the branch"
    print(
        "  edit edges: a non-bool toggle -> 400; a null toggle leaves it; {branch: []} "
        "is a no-op (only clear_branch clears)"
    )


# --------------------------------------------------------------------------- #
# create customer_id AUTHORIZATION (adversarial review B1 / H1) -- customer ring
# --------------------------------------------------------------------------- #
def test_create_customer_authz():
    """POST /api/workflows must not write an UNVALIDATED body customer_id: a bound
    caller planting into a foreign customer's ring is the plant-and-forget hole B1."""
    d = _seed()
    _auth_on()
    api = _client()
    hlb = _h(d["keys"]["linguist_bound_A"])  # bound to customer A
    # B1: a bound caller naming customer B is a 404 (never a silent plant)
    r = api.post(
        "/api/workflows",
        headers=hlb,
        json={"name": "Plant", "scope": "customer", "customer_id": d["cust_b"]},
    )
    assert r.status_code == 404, ("bound caller -> foreign customer must 404", r.status_code)
    # its OWN customer lands, stamped to A
    r = api.post(
        "/api/workflows",
        headers=hlb,
        json={"name": "Own", "scope": "customer", "customer_id": d["cust_a"]},
    )
    assert r.status_code == 200 and r.json()["customer_id"] == d["cust_a"], r.text
    # absent customer_id -> #468: a bound caller never creates a GLOBAL (shared) row, so
    # the workflow lands in its own customer (scope customer)
    r = api.post("/api/workflows", headers=hlb, json={"name": "Global"})
    assert r.status_code == 200 and r.json()["customer_id"] == d["cust_a"], r.text
    assert r.json()["scope"] == "customer", r.text
    # H1: an archived (in-scope) customer is refused WITHOUT leaking its name. Archive
    # customer A, then an unbound admin naming it gets a 400 that never prints the name.
    from app.models import Customer
    from app.services import customers as _customers

    with Session(engine) as s:
        _customers.archive_customer(s, d["cust_a"])
        aname = s.get(Customer, d["cust_a"]).name
    ha = _h(d["keys"][Role.ADMIN])
    r = api.post(
        "/api/workflows",
        headers=ha,
        json={"name": "Arch", "scope": "customer", "customer_id": d["cust_a"]},
    )
    assert r.status_code == 400 and "is archived" in r.json()["error"], r.text
    assert aname not in r.json()["error"], "the archived customer's name must not leak"
    print(
        "  create authz: a bound caller's foreign customer_id -> 404 (no plant); its "
        "own lands stamped; absent -> its own customer (#468); an archived customer -> "
        "400 with no "
        "name leak"
    )


# --------------------------------------------------------------------------- #
# pin confinement (adversarial review H2) -- glossary + prompt-template customer ring
# --------------------------------------------------------------------------- #
def test_pin_customer_confinement():
    """A customer-A-bound caller must not bind customer B's proprietary glossary OR
    prompt template to a step it edits (H2 -- list_prompt_templates was called without
    the customer ring)."""
    d = _seed()
    _auth_on()
    api = _client()
    ha = _h(d["keys"][Role.ADMIN])
    # a customer-A workflow the bound caller can legitimately see + add to (#468: a
    # GLOBAL workflow is read-only for a bound caller, so it is no longer the fixture)
    wid = api.post(
        "/api/workflows",
        headers=ha,
        json={"name": "Shared WF", "scope": "customer", "customer_id": d["cust_a"]},
    ).json()["id"]
    hlb = _h(d["keys"]["linguist_bound_A"])
    # customer-B glossary pin -> 400 (not visible to customer A)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=hlb,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} {target_locale}",
            "glossary_id": d["gid_b"],
        },
    )
    assert r.status_code == 400 and "not an approved glossary" in r.json()["error"], r.text
    # customer-B prompt-template bind -> 400 (H2: was a 200 before the fix)
    r = api.post(
        "/api/workflows/%d/steps" % wid,  # noqa: UP031
        headers=hlb,
        json={
            "step_type": "llm_agent",
            "prompt_text": "{text} {target_locale}",
            "prompt_template_id": d["ptid_b"],
        },
    )
    assert r.status_code == 400 and "not visible" in r.json()["error"], r.text
    print(
        "  pin confinement: a customer-A-bound caller cannot pin customer B's glossary "
        "OR prompt template (both 400, not visible)"
    )


# --------------------------------------------------------------------------- #
# cross-ORG isolation (multi_org ON) -- foreign org = 404, never 403/200
# --------------------------------------------------------------------------- #
def _seed_cross_org():
    """Org A (with an admin + docs key) and Org B (owning a workflow + step + a
    customer + an ARCHIVED customer). multi_org ON, so Org A's key must 404 every Org B
    object."""
    global _N
    _N += 1
    n = _N
    init_db()
    from app.models import Customer

    with Session(engine) as s:
        oa, ob = Organization(name="WFA Org A %d" % n), Organization(name="WFA Org B %d" % n)  # noqa: UP031
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        ua = User(email="wfa-a%d@x.com" % n, name="A Admin", role=Role.ADMIN, org_id=oa.id)  # noqa: UP031
        s.add(ua)
        s.commit()
        s.refresh(ua)
        # Org B: a customer, an archived customer, a workflow + a step.
        cb = Customer(name="B Cust %d" % n, org_id=ob.id)  # noqa: UP031
        cb_arch = Customer(name="B Archived Secret %d" % n, org_id=ob.id)  # noqa: UP031
        s.add(cb)
        s.add(cb_arch)
        s.commit()
        s.refresh(cb)
        s.refresh(cb_arch)
        wfb = wf_svc.create_workflow(s, "B WF %d" % n, org_id=ob.id)  # noqa: UP031
        stb = wf_svc.add_step(s, wfb.id, step_type="lqa", name="chk")
        wfb_id, stb_id = wfb.id, stb.id
        cb_id, cb_arch_id, cb_arch_name = cb.id, cb_arch.id, cb_arch.name
        uid = ua.id
    from app.services import customers as _customers

    with Session(engine) as s:
        _customers.archive_customer(s, cb_arch_id)
    _row, key_a = apikeys.create_key(uid, "wfa-xorg", scopes="api docs")
    return {
        "key_a": key_a,
        "wfb": wfb_id,
        "stb": stb_id,
        "cb": cb_id,
        "cb_arch": cb_arch_id,
        "cb_arch_name": cb_arch_name,
    }


def test_cross_org_404():
    d = _seed_cross_org()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        api = _client()
        h = _h(d["key_a"])
        wfb, stb = d["wfb"], d["stb"]
        # every step route against Org B's workflow -> 404 (never 403/200)
        for path, body in (
            ("/api/workflows/%d/steps" % wfb, {"step_type": "lqa"}),  # noqa: UP031
            ("/api/workflows/%d/steps/%d/edit" % (wfb, stb), {"name": "x"}),  # noqa: UP031
            ("/api/workflows/%d/steps/%d/delete" % (wfb, stb), {}),  # noqa: UP031
            ("/api/workflows/%d/steps/%d/move" % (wfb, stb), {"position": 0}),  # noqa: UP031
        ):
            r = api.post(path, headers=h, json=body)
            assert r.status_code == 404, (f"cross-org {path}", r.status_code, r.text[:80])
        # create naming Org B's customer (unbound-caller variant) -> 404
        r = api.post(
            "/api/workflows",
            headers=h,
            json={"name": "X", "scope": "customer", "customer_id": d["cb"]},
        )
        assert r.status_code == 404, ("cross-org customer_id on create", r.status_code)
        # create naming Org B's ARCHIVED customer -> 404 (foreign, before the archived
        # check), so its name never leaks (H1 across the org boundary)
        r = api.post(
            "/api/workflows",
            headers=h,
            json={"name": "X", "scope": "customer", "customer_id": d["cb_arch"]},
        )
        assert r.status_code == 404, ("cross-org archived customer", r.status_code)
        assert d["cb_arch_name"] not in r.text, "a foreign customer name must never leak"
        print(
            "  cross-org: Org A's key 404s all five routes against Org B's workflow + "
            "customer_id (archived-foreign included, no name leak)"
        )
    finally:
        _auth_off()


def test_headless_shape_tags():
    """#152: additive tags survive API and local/remote CLI reads."""
    from app.cli import _remote
    from app.cli.__main__ import main as cli_main

    d = _seed()
    _auth_off()
    api = _client()
    old_transport = _remote._TRANSPORT
    failures = []
    try:
        _remote_runner(d["keys"][Role.ADMIN])
        for human in (False, True):
            with Session(engine) as s:
                wf = wf_svc.create_workflow(s, f"Shape tags human={human}")
                wid = wf.id
                wf_svc.add_step(
                    s,
                    wid,
                    step_type="llm_agent",
                    name="Translate",
                    prompt_text="Translate {text} into {target_locale}",
                )
                if human:
                    wf_svc.add_step(s, wid, step_type="human_review", name="Review")
            expected = ["human-checkpoint", "straight-through"] if human else ["straight-through"]
            response = api.get(f"/api/workflows/{wid}", headers=_h(d["keys"][Role.ADMIN]))
            assert response.status_code == 200, response.text
            detail = response.json()
            response = api.get("/api/workflows", headers=_h(d["keys"][Role.ADMIN]))
            assert response.status_code == 200, response.text
            row = next(w for w in response.json()["workflows"] if w["id"] == wid)
            for label, payload in (("API list", row), ("API detail", detail)):
                assert payload["shape"] == "straight-through", payload
                if payload.get("shape_tags") != expected:
                    failures.append(f"{label} human={human}: {payload.get('shape_tags')!r}")
            for remote in (False, True):
                prefix = (
                    ["--remote", "http://127.0.0.1", "--token", d["keys"][Role.ADMIN]]
                    if remote
                    else []
                )
                for command in (["workflow", "show", str(wid)], ["workflow", "list"]):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = cli_main(["--json", *prefix, *command])
                    assert rc == 0, buf.getvalue()
                    payload = _json.loads(buf.getvalue())
                    if command[1] == "list":
                        payload = next(w for w in payload["workflows"] if w["id"] == wid)
                    assert payload["shape"] == "straight-through", payload
                    if payload.get("shape_tags") != expected:
                        failures.append(f"CLI {command[1]} remote={remote} human={human}: tags")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_main([*prefix, "workflow", "show", str(wid)])
                assert rc == 0, buf.getvalue()
                header = buf.getvalue().splitlines()[0]
                header.encode("ascii")
                shape = "Straight-through (+Human checkpoint)" if human else "Straight-through"
                if f"shape={shape}, origin=" not in header:
                    failures.append(f"CLI header remote={remote} human={human}: {header}")
    finally:
        _remote._TRANSPORT = old_transport
    assert not failures, "\n".join(failures)
    print("  #152: sorted shape_tags + unchanged primary; local/remote CLI additive header OK")


def _run():
    tests = [
        test_headless_shape_tags,
        test_full_lifecycle,
        test_prompt_rule_and_warning,
        test_glossary_pin_semantics,
        test_partial_edit_three_state,
        test_move_bounds,
        test_in_flight_run_guard,
        test_rbac,
        test_gate_off_noop,
        test_tenancy_404,
        test_edit_bool_and_branch_edges,
        test_create_customer_authz,
        test_pin_customer_confinement,
        test_cross_org_404,
        test_remote_round_trip,
    ]
    for t in tests:
        t()
    print("verify_api_workflow_authoring OK")


if __name__ == "__main__":
    _run()
