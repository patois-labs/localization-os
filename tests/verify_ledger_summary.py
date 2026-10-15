"""run-190 Item C: the LLM-Ledger bodies-withheld SUMMARY surface (gate round 1, H4).

The BACKLOG named two required tests the train shipped WITHOUT: "the remote arm never
returns a prompt or response body" and "the run API's per-step failure shape matches the
Ledger's calls for run 190's fixture". This suite is those two plus the H3 within-org
customer ring, the M4 list caps, the M6 rollup consistency, and the --summary/--calls
refusal -- every one MUTATION-PROVEN (it goes red without the fix it guards).

Standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys cleared ->
MockProvider). Final line: "LEDGER SUMMARY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ledger_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    Role,
    RunStatus,
    StepRun,
    StepType,
    TapAction,
    TapActionKind,
    TapRun,
    TapStatus,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, prompt_tap, security, settings, tap_envelope, tap_view


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


def _gate_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _h(raw):
    return {"X-Loc-Token": raw}


def _key(user_id, scopes="api docs"):
    _row, raw = apikeys.create_key(user_id, "ledger-test", scopes=scopes)
    return raw


def _user(email, role, org_id, customer_id=None):
    u = security.create_user(email, email, role, "ledger-pass-1234", customer_id=customer_id)
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    return u.id


def _client():
    from app.main import app

    return TestClient(app)


def _reset_tap():
    """Per-test isolation: clear the call files AND the envelope rows (init_db creates
    tables but never wipes rows, so actions would otherwise accumulate across tests)."""
    import shutil

    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    with Session(engine) as s:
        for row in s.exec(select(TapRun)).all():
            s.delete(row)
        for row in s.exec(select(TapAction)).all():
            s.delete(row)
        s.commit()
    settings.set_many({"prompt_tap_enabled": "1"})
    settings.invalidate_cache()


def _mk_action(
    *,
    org_id=None,
    customer_id=None,
    project_id=None,
    document_id=None,
    status=TapStatus.COMPLETED,
    kind=TapActionKind.WORKFLOW,
):
    """A TapAction + one TapRun. Returns (action_id, run_id)."""
    aid = tap_envelope.new_id()
    rid = tap_envelope.new_id()
    with Session(engine) as s:
        s.add(
            TapAction(
                id=aid,
                kind=kind,
                org_id=org_id,
                customer_id=customer_id,
                project_id=project_id,
                document_id=document_id,
                requested_locales=["es-ES"],
                status=status,
            )
        )
        s.add(TapRun(id=rid, action_id=aid, target_locale="es-ES", status=status))
        s.commit()
    return aid, rid


def _cap(
    aid,
    rid,
    *,
    prompt="p",
    response="r",
    finish=None,
    error_class=None,
    output_chars=None,
    usage=None,
    outcome="ok",
    error=None,
    provider="local",
    model="google/gemma-4-12b",
):
    prompt_tap.capture(
        prompt_tap.build_record(
            provider=provider,
            provider_kind=provider,
            model=model,
            task="translate",
            system="sys",
            prompt=prompt,
            max_tokens=120,
            payload={"text": prompt},
            outcome=outcome,
            error=error,
            response_text=response,
            usage=usage,
            finish_reason=finish,
            error_class=error_class,
            output_chars=output_chars,
            tap_meta={"action_id": aid, "run_id": rid},
        )
    )


def _walk_strings(obj):
    """Every string anywhere in a nested JSON structure (keys AND values)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            out.extend(_walk_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_walk_strings(v))
    elif isinstance(obj, str):
        out.append(obj)
    else:
        out.append(str(obj))
    return out


# --------------------------------------------------------------------------- #
# H3: the within-org CUSTOMER ring on both routes
# --------------------------------------------------------------------------- #
def test_h3_customer_ring_confines_both_routes():
    """Viewers cannot read Ledger (#261). The service retains its customer ring,
    and an admin key in the same org sees both customers."""
    init_db()
    _reset_tap()
    with Session(engine) as s:
        org = Organization(name="Org L")
        s.add(org)
        s.commit()
        s.refresh(org)
        oid = org.id
        ca, cb = Customer(name="Cust A", org_id=oid), Customer(name="Cust B", org_id=oid)
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        cust_a, cust_b = ca.id, cb.id
    aid_a, _ra = _mk_action(org_id=oid, customer_id=cust_a)
    aid_b, _rb = _mk_action(org_id=oid, customer_id=cust_b)
    aid_adv, _ = _mk_action(org_id=oid, customer_id=cust_b, kind=TapActionKind.ADVANCED)
    viewer = _user("viewer-a@x.com", Role.VIEWER, oid, customer_id=cust_a)
    staff = _user("staff@x.com", Role.ADMIN, oid)  # unbound -> sees all in org
    _gate_on()
    try:
        c = _client()
        # Role refusal is identical for own, foreign and missing action IDs.
        vh = _h(_key(viewer))
        lst = c.get("/api/prompt-tap/actions", headers=vh)
        assert lst.status_code == 403, lst.text
        for aid in (aid_a, aid_b, aid_adv, "missing"):
            assert c.get(f"/api/prompt-tap/actions/{aid}/summary", headers=vh).status_code == 403
        # Keep the independent service customer-ring regression.
        rows = tap_view.list_action_summaries(org_id=oid, customer_id=cust_a)
        assert {a["id"] for a in rows} == {aid_a}
        assert tap_view.action_summary(aid_a, org_id=oid, customer_id=cust_a) is not None
        for aid in (aid_b, aid_adv):
            assert tap_view.action_summary(aid, org_id=oid, customer_id=cust_a) is None
        # STAFF caller (no customer binding): sees BOTH -- proves the ring, not a blanket hide.
        sh = _h(_key(staff))
        sids = {a["id"] for a in c.get("/api/prompt-tap/actions", headers=sh).json()["actions"]}
        assert {aid_a, aid_b} <= sids, ("staff must see every customer's action in its org", sids)
        assert c.get(f"/api/prompt-tap/actions/{aid_b}/summary", headers=sh).status_code == 200
        assert aid_adv in sids
        advanced = c.get(f"/api/prompt-tap/actions/{aid_adv}/summary", headers=sh)
        assert advanced.status_code == 200 and advanced.json()["action"]["kind"] == "advanced"
    finally:
        _gate_off()
    _ok("H3/#261: viewer refused; service customer ring retained; admin reads allowed")


# --------------------------------------------------------------------------- #
# H4: bodies never leak (both routes + the shared read model)
# --------------------------------------------------------------------------- #
_SECRET_PROMPT = "CONFIDENTIAL-PROMPT-Zx91-do-not-leak"
_SECRET_RESPONSE = "CONFIDENTIAL-RESPONSE-Qw72-do-not-leak"


def test_h4_bodies_never_leak():
    """The whole point of the SUMMARY surface: no prompt or response body is EVER returned.
    Walk the ENTIRE JSON of both routes AND the shared tap_view read model (which the MCP
    handler forwards verbatim) and fail on any occurrence of the fixture's prompt/response
    text; bodies_withheld must be True."""
    init_db()
    _reset_tap()
    aid, rid = _mk_action()
    _cap(
        aid,
        rid,
        prompt=_SECRET_PROMPT,
        response=_SECRET_RESPONSE,
        finish="stop",
        output_chars=len(_SECRET_RESPONSE),
        usage={"input_tokens": 3, "output_tokens": 4},
    )
    uid = _user("leak@x.com", Role.ADMIN, None)
    _gate_off()
    c = _client()
    h = _h(_key(uid))
    # the raw record on disk really does carry the secret (else the test proves nothing)
    raw_recs = prompt_tap.read_records(limit=10)
    assert any(_SECRET_PROMPT in _walk_strings(r) for r in raw_recs), "fixture bodies missing"

    lst = c.get("/api/prompt-tap/actions", headers=h).json()
    summ = c.get(f"/api/prompt-tap/actions/{aid}/summary", headers=h).json()
    model_list = tap_view.list_action_summaries()
    model_one = tap_view.action_summary(aid)
    for label, blob in (
        ("list route", lst),
        ("summary route", summ),
        ("read-model list", model_list),
        ("read-model summary", model_one),
    ):
        strings = _walk_strings(blob)
        # LOW-2 (gate round 2): SUBSTRING containment, not list membership. A body embedded
        # in a larger string ("prompt: <secret>", a truncated body, a body folded into a
        # formatted message) would slip a `not in [list]` check; this is the one test whose
        # entire job is "the remote arm never returns a prompt or response body".
        assert not any(_SECRET_PROMPT in s for s in strings), f"{label} LEAKED the prompt body"
        assert not any(_SECRET_RESPONSE in s for s in strings), f"{label} LEAKED the response body"
    assert lst["bodies_withheld"] is True and summ["bodies_withheld"] is True, (lst, summ)
    assert model_one["bodies_withheld"] is True, model_one
    _ok("H4: no prompt/response body on either route or the read model; bodies_withheld true")


# --------------------------------------------------------------------------- #
# H4: --summary local == remote key-identity + the --summary/--calls refusal
# --------------------------------------------------------------------------- #
def test_h4_summary_local_remote_key_identity():
    """`prompt-tap show-action --summary` LOCAL (tap_view.action_summary) and REMOTE (the
    /api route) return the SAME shape -- the remote arm is a faithful mirror, never a wider
    or narrower payload."""
    init_db()
    _reset_tap()
    aid, rid = _mk_action()
    _cap(
        aid, rid, prompt="p", response="r", finish="stop", output_chars=1, usage={"input_tokens": 1}
    )
    uid = _user("parity@x.com", Role.ADMIN, None)
    _gate_off()
    c = _client()
    remote = c.get(f"/api/prompt-tap/actions/{aid}/summary", headers=_h(_key(uid))).json()["action"]
    local = tap_view.action_summary(aid)
    assert set(local) == set(remote), (
        "summary key-set drift local vs remote",
        set(local) ^ set(remote),
    )
    lr, rr = local["runs"][0], remote["runs"][0]
    assert set(lr) == set(rr), ("per-run summary key drift", set(lr) ^ set(rr))
    assert set(lr["calls_summary"]) == set(rr["calls_summary"]), "calls_summary key drift"
    _ok("H4: --summary local and remote are key-identical")


def test_h4_summary_calls_mutually_exclusive():
    """`show-action --summary --calls` is a clean usage refusal (bodies are host-only, a
    summary never carries them) -- checked BEFORE any gate/DB access."""
    from app.cli._common import CliError
    from app.cli.cmd_prompt_tap import _show_action

    class _Args:
        summary = True
        calls = True
        action_id = "x"
        remote = None

    try:
        _show_action(_Args(), emit=None)
        _fail("--summary --calls must be refused")
    except CliError as e:
        assert "mutually exclusive" in str(e), e
    _ok("H4: --summary and --calls are mutually exclusive (host-only bodies)")


# --------------------------------------------------------------------------- #
# H4 + M2: the run-190 failure shape reconciles with the Ledger summary
# --------------------------------------------------------------------------- #
def test_h4_failure_shape_reconciles_with_ledger():
    """The run-190 shape: 3 attempts, all finish_reason=length, 0 output chars. GET
    /api/runs/{id}['failure_shape'] must AGREE with the Ledger summary's calls_summary for
    the same run, and BOTH must name the engine that failed (M2 -- not provider=null)."""
    init_db()
    _reset_tap()
    prov, model = "local", "google/gemma-4-12b"
    with Session(engine) as s:
        proj = Project(name="Run190 Proj")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(project_id=proj.id, filename="r190.txt", fmt="txt")
        wf = Workflow(name="r190 wf", scope="global")
        s.add(doc)
        s.add(wf)
        s.commit()
        s.refresh(doc)
        s.refresh(wf)
        step = WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            step_type=StepType.LLM_AGENT,
            provider=prov,
            model=model,
            prompt_text="Translate: {text}",
        )
        s.add(step)
        s.commit()
        s.refresh(step)
        step_id = step.id
        run = WorkflowRun(
            workflow_id=wf.id, document_id=doc.id, target_locale="es-ES", status=RunStatus.FAILED
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step_id,
                status=RunStatus.FAILED,
                attempts=3,
                finish_reason="length",
                error_class="ThinkingBurn",
                output_chars=0,
                provider_used=prov,
                model_used=model,
            )
        )
        s.commit()
        run_id = run.id
    # a matching Ledger envelope: 3 CALLS, each finish=length / 0 output, on the same engine.
    aid, rid = _mk_action(status=TapStatus.FAILED)
    for _ in range(3):
        _cap(
            aid,
            rid,
            prompt="Book",
            response="",
            finish="length",
            error_class="ThinkingBurn",
            output_chars=0,
            outcome="provider-error",
            error="thinking burn",
            provider=prov,
            model=model,
            usage={"input_tokens": 1660},
        )

    uid = _user("r190@x.com", Role.ADMIN, None)
    _gate_off()
    c = _client()
    body = c.get(f"/api/runs/{run_id}", headers=_h(_key(uid))).json()
    shape = [r for r in body.get("failure_shape", []) if r.get("step_id") == step_id]
    assert shape, ("failure_shape missing the failed step", body.get("failure_shape"))
    fs = shape[0]
    assert fs["attempts"] == 3 and fs["finish_reason"] == "length" and fs["output_chars"] == 0, fs
    assert fs["provider"] == prov and fs["model"] == model, (
        "failure_shape must name the failing engine, not null/null (M2)",
        fs,
    )
    cs = tap_view.action_summary(aid)["runs"][0]["calls_summary"]
    assert cs["calls"] == 3 and cs["finish_reasons"] == {"length": 3}, cs
    # reconciliation: the step's per-step shape agrees with the Ledger's per-call summary.
    assert fs["attempts"] == cs["calls"], (fs, cs)
    assert fs["finish_reason"] in cs["finish_reasons"], (fs, cs)
    assert all(sh["provider"] == prov and sh["model"] == model for sh in cs["shape"]), cs["shape"]
    _ok("H4/M2: run-view failure_shape reconciles with the Ledger summary, engine named")


# --------------------------------------------------------------------------- #
# M4: the list route is bounded (default 50 / cap 500 / truncated flag / 400)
# --------------------------------------------------------------------------- #
def test_m4_list_caps_and_truncated():
    init_db()
    _reset_tap()
    for _ in range(3):
        _mk_action()
    uid = _user("caps@x.com", Role.ADMIN, None)
    _gate_off()
    c = _client()
    h = _h(_key(uid))
    one = c.get("/api/prompt-tap/actions?limit=1", headers=h).json()
    assert one["count"] == 1 and one["truncated"] is True, one
    allr = c.get("/api/prompt-tap/actions?limit=50", headers=h).json()
    assert allr["count"] == 3 and allr["truncated"] is False, allr
    bad = c.get("/api/prompt-tap/actions?limit=notanint", headers=h)
    assert bad.status_code == 400, (
        "a malformed limit must be a 400, not a silent coerce",
        bad.text,
    )
    # LOW-3 (gate round 2): the CAP itself. A giant limit is ACCEPTED and silently clamped to
    # PROMPT_TAP_LIST_CAP (200, no 5xx, count never above the cap), and a zero / negative limit
    # is a clean 400 (never a coerce-to-default). Pins the tighter-than-house 500 window.
    from app.web.api_cli import PROMPT_TAP_LIST_CAP

    huge = c.get("/api/prompt-tap/actions?limit=100000", headers=h)
    assert huge.status_code == 200, ("a giant limit must clamp, not 5xx", huge.text)
    assert huge.json()["count"] <= PROMPT_TAP_LIST_CAP, (
        "the list route must never return more than the cap",
        huge.json()["count"],
        PROMPT_TAP_LIST_CAP,
    )
    for bad_l in ("0", "-1"):
        r = c.get(f"/api/prompt-tap/actions?limit={bad_l}", headers=h)
        assert r.status_code == 400, (f"limit={bad_l} must be a 400, not a coerce", r.text)
    _ok("M4: list route bounded (default/cap 500 + truncated flag + 400 on bad/zero/neg limit)")


# --------------------------------------------------------------------------- #
# M6: calls_summary totals reuse the envelope rollup (cache_write folded in)
# --------------------------------------------------------------------------- #
def test_m6_tokens_in_includes_cache_write():
    """calls_summary.tokens_in must fold input + cache_read + cache_write (the SAME sum the
    envelope rollup uses), so the two numbers cannot disagree on the diagnostics surface."""
    init_db()
    _reset_tap()
    aid, rid = _mk_action()
    _cap(
        aid,
        rid,
        finish="stop",
        output_chars=5,
        usage={
            "input_tokens": 100,
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
            "output_tokens": 7,
        },
    )
    cs = tap_view.action_summary(aid)["runs"][0]["calls_summary"]
    assert cs["tokens_in"] == 115, (
        "tokens_in must include cache_write (100+10+5)",
        cs["tokens_in"],
    )
    _ok("M6: calls_summary.tokens_in folds cache_write (115 = 100+10+5)")


def test_m6_unenriched_failures_not_hard_zero():
    """M6: an un-enriched run's summary.failures must count the REAL failed calls (from the
    run's own metric rows), not the hard 0 the empty-calls call produced -- so summary and
    calls_summary agree."""
    init_db()
    _reset_tap()
    aid, rid = _mk_action()
    _cap(
        aid,
        rid,
        finish="length",
        error_class="ThinkingBurn",
        output_chars=0,
        outcome="provider-error",
        error="boom",
    )
    _cap(aid, rid, finish="stop", output_chars=3, outcome="ok")
    rv = tap_view.action_summary(aid)["runs"][0]
    assert rv["calls_summary"]["failures"] == 1, rv["calls_summary"]
    assert rv["summary"]["failures"] == 1, (
        "summary.failures must count the real failures, not hard 0 (M6)",
        rv["summary"],
    )
    _ok("M6: an un-enriched run's summary.failures matches calls_summary.failures")


def test_low1_engines_card_names_the_summary():
    """LOW-1 (gate round 2): the engines capability card an LLM agent reads for "engines and
    diagnostics" must distinguish the admin/host-only RAW recorder from the docs-tier
    bodies-withheld SUMMARY (actions --summary / show-action --summary / the GET routes / the
    ledger_summary MCP tool), matching llm.md. Counterfactual: on ac7bf169 engines.md never
    mentioned the summary, so this goes red there."""
    with open(os.path.join(_ROOT, "app", "capabilities", "engines.md"), encoding="utf-8") as f:
        card = f.read()
    assert "bodies-withheld" in card.lower(), "engines.md must name the bodies-withheld summary"
    assert "ledger_summary" in card, "engines.md must name the ledger_summary MCP tool"
    assert "--summary" in card, "engines.md must name the --summary verb"
    assert "docs tier" in card.lower(), "engines.md must state the summary is docs-tier readable"
    _ok("LOW-1: engines card distinguishes the raw recorder from the docs-tier summary")


def main():
    test_h3_customer_ring_confines_both_routes()
    test_h4_bodies_never_leak()
    test_h4_summary_local_remote_key_identity()
    test_h4_summary_calls_mutually_exclusive()
    test_h4_failure_shape_reconciles_with_ledger()
    test_m4_list_caps_and_truncated()
    test_m6_tokens_in_includes_cache_write()
    test_m6_unenriched_failures_not_hard_zero()
    test_low1_engines_card_names_the_summary()
    print("LEDGER SUMMARY OK")


if __name__ == "__main__":
    main()
