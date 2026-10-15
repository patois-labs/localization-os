"""Jev quality-judge engine kinds (jev / jev_local) as plug-and-play engines (Train A).

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys blanked, NO network (every HTTP
call rides an httpx.MockTransport or a patched qe_jev._post; every URL is an IP
literal or the pinned TypeSafe constant, so no DNS either). Run only through
tests/test_verify_suites.py.

Covers:
  1. create/update rules: hosted jev pins base_url + needs a key; jev_local needs a
     base_url; judge config coerced + range-checked; model defaults to jev-latest;
     the networked-keyless warning.
  2. providers: get_provider on a judge kind raises JudgeEngineNotAProvider (a
     DeadEngineError); an unknown kind raises instead of silently mocking; a keyless
     jev_local with a URL is live.
  3. qe_jev targets: resolve_target for "jev" (env) and engine refs; dead/disabled/non-
     judge refs refused; the key is never in repr.
  4. score_segment with a jev_local target: unpriced (no spend booked, cap never
     consulted), routed through the local-engine door (tailnet), no bearer when
     keyless; with a hosted jev target: priced + capped, pinned host, bearer sent.
  5. trace + Prompt Tap: request/response/latency recorded, engine ref as provider,
     the key never present.
  6. orchestrator dispatch: an engine:<id> judge is resolved ONCE per step visit and
     every segment is scored with it; the legacy "jev" still calls the one-argument
     seam; a disabled judge skips every segment with provider_error.
  7. role filtering: translate pickers exclude judges, judge pickers include them (and
     exclude NMT); step/project/settings/pair writes refuse a judge in a translate slot.
  8. the Test probe for both kinds (model list, configured-model presence, latency,
     one-question check, auth failure).
  9. surfaces: Setup AI web create + page grouping, /api create (warnings, no key),
     CLI create/show, MCP manage_install (no key), Evaluations Lab jev_ref.
 10. the key never appears in engine_row, MCP output, a trace, a tap record, a probe
     message or an error string.
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_jeveng_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import EngineProfile, SegStatus
from app.services import engines, qe, qe_jev, settings, spend
from app.services import providers_common as PC
from app.services.providers import DeadEngineError, get_provider, provider_is_live

HOSTED_KEY = "ts-hosted-KEY-0123456789"  # pregate: allow-secret fake test key, HTTP is mocked
LOCAL_KEY = "semif-local-KEY-9876543210"  # pregate: allow-secret fake test key, HTTP is mocked

FAILURES: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print("  FAIL: " + msg)


def answer(**levels):
    return {
        "model": "semif-1",
        "answers": {
            **{
                c: {
                    "type": "score",
                    "probabilities": [float(i == levels.get(c, 0)) for i in range(4)],
                    "confidence": 0.9,
                }
                for c in qe.QE_CATEGORIES
            },
            "meaning_changed": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 1000, "output_tokens": 20},
    }


def no_key(blob) -> bool:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    return HOSTED_KEY not in text and LOCAL_KEY not in text


def jev_transport(seen, *, status=200, data=None, models=None):
    """One MockTransport answering the two Jev routes and recording every request."""

    def handler(req: httpx.Request):
        seen.append(
            {
                "url": str(req.url),
                "host": req.url.host,
                "path": req.url.path,
                "auth": req.headers.get("Authorization"),
                "body": json.loads(req.content) if req.content else None,
            }
        )
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in (models or [])]})
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=data if data is not None else answer())

    return httpx.MockTransport(handler)


# --- 1. create / update rules ------------------------------------------------
def test_create_rules():
    try:
        engines.create_profile("jev-nokey", "jev")
        check(False, "hosted jev without a key must be refused")
    except ValueError as exc:
        check("API key" in str(exc), f"jev key message: {exc}")
    try:
        engines.create_profile("jev-evil", "jev", base_url="http://10.0.0.5:9", api_key=HOSTED_KEY)
        check(False, "hosted jev with a foreign base_url must be refused")
    except ValueError as exc:
        check("jev_local" in str(exc) and no_key(str(exc)), f"pin message: {exc}")
    hosted = engines.create_profile("Jev hosted", "jev", api_key=HOSTED_KEY)
    check(hosted.base_url == engines.JEV_HOSTED_BASE_URL, "jev base_url pinned")
    check(hosted.model == "jev-latest", "jev model defaults to jev-latest")
    same = engines.create_profile(
        "Jev hosted 2", "jev", base_url="https://api.typesafe.ai/", api_key=HOSTED_KEY
    )
    check(same.base_url == engines.JEV_HOSTED_BASE_URL, "the pinned URL itself is accepted")
    try:
        engines.update_profile(hosted.id, base_url="https://evil.example.com")
        check(False, "moving a hosted jev off TypeSafe must be refused")
    except ValueError:
        pass
    engines.update_profile(hosted.id, base_url="", name="Jev hosted")
    check(
        engines.get_profile(hosted.id).base_url == engines.JEV_HOSTED_BASE_URL,
        "a blank base_url edit keeps the pinned URL",
    )
    try:
        engines.create_profile("semif-nourl", "jev_local")
        check(False, "jev_local without a base_url must be refused")
    except ValueError:
        pass
    try:
        engines.create_profile(
            "semif-badfloor",
            "jev_local",
            base_url="http://127.0.0.1:8077",
            cfg={"confidence_floor": "0.3"},
        )
        check(False, "confidence_floor below 0.5 must be refused")
    except ValueError as exc:
        check("confidence_floor" in str(exc), f"floor message: {exc}")
    local = engines.create_profile(
        "SemIf loopback",
        "jev_local",
        base_url="http://127.0.0.1:8077",
        cfg={"confidence_floor": "0.7", "meaning_floor": "0.9", "timeout_s": ""},
    )
    check(local.config == {"confidence_floor": 0.7, "meaning_floor": 0.9}, str(local.config))
    check(engines.profile_warnings(local) == [], "loopback keyless jev_local: no warning")
    lan = engines.create_profile("SemIf LAN", "jev_local", base_url="http://10.9.8.7:8077/v1")
    check(len(engines.profile_warnings(lan)) == 1, "networked keyless jev_local warns")
    keyed = engines.create_profile(
        "SemIf LAN keyed", "jev_local", base_url="http://10.9.8.7:8077/v1/", api_key=LOCAL_KEY
    )
    check(engines.profile_warnings(keyed) == [], "networked keyed jev_local: no warning")
    engines.update_profile(local.id, cfg={"timeout_s": "12"}, clear=["meaning_floor"])
    check(
        engines.get_profile(local.id).config == {"confidence_floor": 0.7, "timeout_s": 12.0},
        "update merges + coerces judge config and honours clear",
    )
    for prof in (hosted, keyed):
        row = engines.engine_row(engines.get_profile(prof.id))
        check(row["judge"] is True and row["key_set"] is True, "engine_row judge + key_set")
        check(no_key(row), "engine_row never carries the key")
    check(engines.engine_row(engines.get_profile(local.id))["key_set"] is False, "keyless row")
    print("[1] create/update rules OK")
    return hosted, local, lan, keyed


# --- 2. providers seam ----------------------------------------------------------
def test_providers(hosted, local):
    for prof in (hosted, local):
        try:
            get_provider(f"engine:{prof.id}")
            check(False, f"get_provider must refuse judge engine:{prof.id}")
        except PC.JudgeEngineNotAProvider as exc:
            check(isinstance(exc, DeadEngineError), "JudgeEngineNotAProvider is a DeadEngineError")
            check("QE judge" in str(exc) and no_key(str(exc)), f"judge message: {exc}")
    with Session(db_engine) as s:
        odd = EngineProfile(
            name="odd kind", kind="bogus_kind", api_key_enc=engines.enc_key("x"), enabled=True
        )
        s.add(odd)
        s.commit()
        s.refresh(odd)
        odd_id = odd.id
    try:
        get_provider(f"engine:{odd_id}")
        check(False, "an unknown kind must raise, not silently mock")
    except PC.JudgeEngineNotAProvider:
        check(False, "an unknown kind is not a judge")
    except DeadEngineError as exc:
        check("unknown engine kind" in str(exc), f"unknown kind message: {exc}")
    with Session(db_engine) as s:
        s.delete(s.get(EngineProfile, odd_id))
        s.commit()
    check(provider_is_live(f"engine:{local.id}"), "keyless jev_local with a URL is live")
    check(provider_is_live(f"engine:{hosted.id}"), "keyed hosted jev is live")
    print("[2] providers seam OK")


# --- 3. targets -------------------------------------------------------------------
def test_targets(hosted, local, keyed):
    check(qe_jev.is_judge_ref("jev") and qe_jev.is_judge_ref(" JEV "), "legacy jev is a judge")
    check(qe_jev.is_judge_ref(f"engine:{local.id}"), "jev_local ref is a judge")
    check(not qe_jev.is_judge_ref("claude"), "claude is not a Jev judge")
    check(not qe_jev.is_judge_ref("engine:999999"), "a missing engine is not a Jev judge")
    env = qe_jev.resolve_target("jev")
    check(env.legacy and env.provider_ref == "jev" and env.priced, "env target")
    tgt = qe_jev.resolve_target(f"engine:{local.id}")
    check(
        (tgt.kind, tgt.base_url, tgt.key, tgt.priced, tgt.timeout_s, tgt.confidence_floor)
        == ("jev_local", "http://127.0.0.1:8077", "", False, 12.0, 0.7),
        repr(tgt),
    )
    lan_t = qe_jev.resolve_target(f"engine:{keyed.id}")
    check(lan_t.key == LOCAL_KEY and LOCAL_KEY not in repr(lan_t), "key loaded, not in repr")
    host_t = qe_jev.resolve_target(f"engine:{hosted.id}")
    check(
        host_t.priced and host_t.base_url == engines.JEV_HOSTED_BASE_URL and host_t.timeout_s == 10,
        repr(host_t),
    )
    check(HOSTED_KEY not in repr(host_t), "hosted key not in repr")
    dl = engines.create_profile("A DeepL", "deepl", api_key="dl-key")  # pregate: allow-secret fake
    try:
        qe_jev.resolve_target(f"engine:{dl.id}")
        check(False, "a non-judge engine is not a Jev target")
    except ValueError:
        pass
    off = engines.create_profile("SemIf off", "jev_local", base_url="http://127.0.0.1:8078")
    engines.update_profile(off.id, enabled=False)
    try:
        qe_jev.resolve_target(f"engine:{off.id}")
        check(False, "a disabled judge must be refused")
    except DeadEngineError as exc:
        check(no_key(str(exc)), "dead message has no key")
    print("[3] targets OK")
    return dl, off


# --- 4 + 5. scoring, pricing, transport, trace, tap -----------------------------------
def test_scoring(hosted, local, keyed):
    from app.ports import telemetry

    seen = []
    qe_jev._TRANSPORT = jev_transport(seen)
    try:
        target = qe_jev.resolve_target(f"engine:{local.id}")
        before = spend.spent_this_period()
        trace = {}
        with (
            patch.object(spend, "would_block", side_effect=AssertionError("cap consulted")),
            patch.object(spend, "record_external_usd") as booked,
            patch.object(
                PC, "local_engine_client_kwargs", wraps=PC.local_engine_client_kwargs
            ) as door,
        ):
            card = qe_jev.score_segment(
                "Pay ten euros.", "Zahlen Sie zehn Euro.", "en", "de", judge=target, trace=trace
            )
        check(not card["skipped"] and card["score"] == 100, f"jev_local card: {card}")
        check(card["cost_usd"] == 0.0 and card["tokens_in"] == 1000, "jev_local is $0")
        booked.assert_not_called()
        check(spend.spent_this_period() == before, "jev_local books no spend")
        check(door.call_count == 1, "jev_local goes through the local-engine door (tailnet)")
        check(door.call_args.args[0] == "http://127.0.0.1:8077", "door gets the base URL")
        req = seen[-1]
        check(req["path"] == "/v1/systemone" and req["host"] == "127.0.0.1", str(req))
        check(req["auth"] is None, "keyless jev_local sends no bearer")
        check(req["body"]["model"] == "jev-latest", "model from the target")
        for k in ("engine", "endpoint", "request", "response", "status", "latency_ms", "attempts"):
            check(k in trace, f"trace carries {k}")
        check(trace["engine"] == f"engine:{local.id}" and trace["status"] == 200, str(trace))
        check(trace["endpoint"] == "http://127.0.0.1:8077/v1/systemone", trace["endpoint"])
        bare = answer()
        del bare["usage"]  # a local server may not report usage at all
        qe_jev._TRANSPORT = jev_transport(seen, data=bare)
        card = qe_jev.score_segment("s", "t", "en", "de", judge=target)
        check(not card["skipped"] and card["error"] is None, f"usage-less local card: {card}")
        qe_jev._TRANSPORT = jev_transport(seen)

        # keyed local: bearer sent, never in trace / tap
        seen.clear()
        t_keyed = qe_jev.resolve_target(f"engine:{keyed.id}")
        trace = {}
        with (
            patch.object(telemetry, "enabled", return_value=True),
            patch.object(telemetry, "emit") as emit,
        ):
            card = qe_jev.score_segment(
                "s", "t", "en", "de", tap_meta={"workflow_id": 1}, judge=t_keyed, trace=trace
            )
        check(seen[-1]["auth"] == f"Bearer {LOCAL_KEY}", "keyed jev_local sends its bearer")
        check(seen[-1]["path"] == "/v1/systemone", "base /v1 suffix trimmed")
        check(no_key(trace), "the key never enters the trace")
        rec = emit.call_args.args[0]
        check(rec["provider"] == f"engine:{keyed.id}", f"tap provider: {rec['provider']}")
        check(rec["provider_kind"] == "jev_local" and rec["cost_usd"] == 0.0, "tap kind/cost")
        check(no_key(rec), "the key never enters the tap record")

        # hosted: priced, capped, pinned host
        seen.clear()
        t_host = qe_jev.resolve_target(f"engine:{hosted.id}")
        before = spend.spent_this_period()
        card = qe_jev.score_segment("s", "t", "en", "de", judge=t_host)
        check(not card["skipped"] and card["cost_usd"] > 0, f"hosted card: {card}")
        check(spend.spent_this_period() > before, "hosted jev books spend")
        check(seen[-1]["host"] == "api.typesafe.ai", "hosted goes to the pinned host")
        check(seen[-1]["auth"] == f"Bearer {HOSTED_KEY}", "hosted sends its bearer")
        seen.clear()
        with patch.object(spend, "would_block", return_value=(True, 5.0, 5.0)):
            card = qe_jev.score_segment("s", "t", "en", "de", judge=t_host)
        check(card["reason"] == "provider_error" and not seen, "a tripped cap stops hosted jev")

        # errors never carry the key
        qe_jev._TRANSPORT = jev_transport(seen, status=401)
        card = qe_jev.score_segment("s", "t", "en", "de", judge=t_host)
        check(card["reason"] == "provider_error" and no_key(card), f"401 card: {card}")

        # a jev_local legacy switch-off does not gate a profile; it still gates "jev"
        settings.set_many({"qe_jev_enabled": "0"})
        qe_jev._TRANSPORT = jev_transport(seen)
        check(
            not qe_jev.score_segment("s", "t", "en", "de", judge=target)["skipped"],
            "qe_jev_enabled does not gate a judge profile",
        )
        with patch.object(config, "TYPESAFE_API_KEY", "legacy"):  # pregate: allow-secret fake
            check(
                qe_jev.score_segment("s", "t", "en", "de")["reason"] == "jev_disabled",
                "qe_jev_enabled still gates the legacy env target",
            )
        settings.set_many({"qe_jev_enabled": "1"})
    finally:
        qe_jev._TRANSPORT = None
    print("[4-5] scoring, pricing, transport, trace, tap OK")


# --- 6. orchestrator dispatch ----------------------------------------------------------
def test_dispatch(local, off):
    from app.services import orchestrator
    from app.services.providers import register_provider

    with patch("tests._bootstrap.init_env", return_value=_TMP):
        from tests.verify_qe_gate import QEProvider, make_wf, new_segmented_doc, segs_for

    class TranslatedQEProvider(QEProvider):
        def complete(self, *args, **kwargs):
            text = super().complete(*args, **kwargs)
            if kwargs.get("task") == "translate":
                text = text.replace("one.", "un.").replace("two.", "deux.")
            return text

    register_provider("qeprov", TranslatedQEProvider())
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "JevEng",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "qeprov",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    calls = []

    def fake_post(body, *args):
        calls.append(args)
        return 200, answer(accuracy=2) if "BAD" in body["state"]["translation"] else answer()

    # (a) an engine:<id> judge: resolved once per step visit, used for every segment.
    with Session(db_engine) as s:
        wid = make_wf(
            s, "jev_local gate", qe_gate=True, qe_provider=f"engine:{local.id}", qe_threshold=70
        )
    did = new_segmented_doc(client, pid, ["Fine one.", "BAD two."])
    QEProvider.reset()
    with (
        patch.object(qe_jev, "_post", side_effect=fake_post),
        patch.object(qe_jev, "resolve_target", wraps=qe_jev.resolve_target) as resolve,
        Session(db_engine) as s,
    ):
        events = list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
    check(any(e.get("type") == "done" for e in events), "run completes")
    check(resolve.call_count == 1, f"target resolved once per step visit: {resolve.call_count}")
    check(len(calls) == 2, f"one judge call per segment: {len(calls)}")
    check(
        all(len(a) == 1 and a[0].provider_ref == f"engine:{local.id}" for a in calls),
        "every segment scored with the profile target",
    )
    check(not [c for c in QEProvider.calls if c["task"] == "qe"], "no LLM judge call")
    with Session(db_engine) as s:
        segs = segs_for(s, did)
        check(segs[0].status == SegStatus.MT, "clean segment passes")
        check(segs[1].status == SegStatus.NEEDS_REVIEW, "flagged segment routed to review")

    # (b) the legacy "jev" still uses the one-argument env seam.
    calls.clear()
    with Session(db_engine) as s:
        wid = make_wf(s, "legacy jev gate", qe_gate=True, qe_provider="jev")
    did = new_segmented_doc(client, pid, ["Legacy one."])
    with (
        patch.object(config, "TYPESAFE_API_KEY", "legacy"),  # pregate: allow-secret fake
        patch.object(qe_jev, "_post", side_effect=fake_post),
        Session(db_engine) as s,
    ):
        list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
    check(calls == [()], f"legacy jev calls _post(body) only: {calls}")

    # (c) a disabled judge engine: every segment skipped with provider_error, no HTTP.
    calls.clear()
    with Session(db_engine) as s:
        wid = make_wf(s, "dead judge gate", qe_gate=True, qe_provider=f"engine:{off.id}")
    did = new_segmented_doc(client, pid, ["Dead one.", "Dead two."])
    with patch.object(qe_jev, "_post", side_effect=fake_post), Session(db_engine) as s:
        events = list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
    check(any(e.get("type") == "done" for e in events), "a dead judge never fails the run")
    check(not calls, "a dead judge makes no HTTP call")
    with Session(db_engine) as s:
        for seg in segs_for(s, did):
            flag = next(f for f in seg.qa_flags if f["type"] == "qe")
            check(flag.get("skipped") is True and "skipped" in flag.get("message", ""), str(flag))
            check(seg.status == SegStatus.MT, "a skipped judge never routes to review")
    qe_events = [e["qe"] for e in events if e.get("type") == "done" and "qe" in e]
    check(qe_events and qe_events[-1]["skipped"] == 2, f"both segments skipped: {qe_events}")
    print("[6] orchestrator dispatch OK")
    return client, pid


# --- 7. role filtering -------------------------------------------------------------------
def test_roles(client, pid, local, dl):
    from app.services import pair_providers, workflows
    from app.web.deps import engine_options

    judge_ref, dl_ref = f"engine:{local.id}", f"engine:{dl.id}"
    translate = [v for v, _ in engine_options(None)]
    judge = [v for v, _ in engine_options(None, "judge")]
    check(judge_ref not in translate and dl_ref in translate, "translate picker hides judges")
    check(judge_ref in judge and dl_ref not in judge, "judge picker: judges yes, NMT no")
    for ref, role in ((judge_ref, "translate"), ("jev", "translate"), (dl_ref, "judge")):
        try:
            engines.assert_ref_role(ref, role)
            check(False, f"{ref} must be refused as {role}")
        except engines.EngineRoleError:
            pass
    for ref, role in ((judge_ref, "judge"), ("jev", "judge"), (dl_ref, "translate"), ("", "x")):
        try:
            engines.assert_ref_role(ref, role) if role != "x" else None
        except engines.EngineRoleError as exc:
            check(False, f"{ref} as {role} wrongly refused: {exc}")
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, "roles")
        for field in ("provider", "fallback_provider"):
            try:
                workflows.add_step(
                    s,
                    wf.id,
                    name="x",
                    step_type="llm_agent",
                    prompt_text="{text}",
                    **{field: judge_ref},
                )
                check(False, f"a judge as step {field} must be refused")
            except workflows.WorkflowError as exc:
                check("QE judge" in str(exc), f"step {field} message: {exc}")
        st = workflows.add_step(
            s,
            wf.id,
            name="ok",
            step_type="llm_agent",
            provider="mock",
            prompt_text="{text}",
            qe_gate=True,
            qe_provider=judge_ref,
        )
        check(st.qe_provider == judge_ref, "a judge is accepted as the QE judge")
        try:
            workflows.update_step(s, st.id, provider=judge_ref)
            check(False, "editing a step provider to a judge must be refused")
        except workflows.WorkflowError:
            pass
        workflows.update_step(s, st.id, prompt_text="Again: {text}")  # unrelated edit OK
    try:
        settings.validate_value("default_provider", judge_ref)
        check(False, "a judge as the install default must be refused")
    except settings.InvalidSettingValue:
        pass
    check(judge_ref not in pair_providers._valid_providers(None), "pair providers exclude judges")
    r = client.post(
        "/projects",
        data={
            "name": "JudgeDefault",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": judge_ref,
        },
        follow_redirects=False,
    )
    check(
        r.status_code in (303, 400) and "/projects/" not in r.headers.get("location", "x/"),
        f"project create with a judge default refused: {r.status_code} {r.headers}",
    )
    print("[7] role filtering OK")


# --- 8. probe ----------------------------------------------------------------------------
def test_probe(hosted, local):
    seen = []
    ok, msg = engines.test_profile(local.id, transport=jev_transport(seen, models=["semif-1"]))
    check(ok, f"jev_local probe ok: {msg}")
    for part in ("1 model(s)", "semif-1", "'jev-latest' NOT in the list", "ms", "0.05"):
        check(part in msg, f"probe message has {part!r}: {msg}")
    check(msg.isascii(), "probe message is ASCII")
    check([r["path"] for r in seen] == ["/v1/models", "/v1/systemone"], str(seen))
    check(list(seen[1]["body"]["questions"]) == ["meaning_changed"], "one-question probe")
    engines.update_profile(local.id, model="semif-1")
    ok, msg = engines.test_profile(local.id, transport=jev_transport([], models=["semif-1"]))
    check(ok and "'semif-1' listed" in msg, msg)
    seen.clear()

    def unauthorized(req):
        seen.append(req.headers.get("Authorization"))
        return httpx.Response(401, json={"error": "bad key"})

    ok, msg = engines.test_profile(hosted.id, transport=httpx.MockTransport(unauthorized))
    check(not ok and "server rejected the key" in msg and no_key(msg), msg)
    check(seen and seen[0] == f"Bearer {HOSTED_KEY}", "hosted probe sends its bearer")
    print("[8] probe OK")


# --- 9. surfaces -------------------------------------------------------------------------
def test_surfaces(client):
    from app.cli import cmd_engine
    from app.cli.__main__ import main as cli
    from app.services import apikeys, security

    # Web: create a jev_local from the Setup AI form; the page groups it as a judge.
    r = client.post(
        "/setup-ai/engines",
        data={
            "name": "Web SemIf",
            "kind": "jev_local",
            "base_url": "http://10.1.2.3:8077",
            "confidence_floor": "0.65",
            "timeout_s": "20",
        },
        follow_redirects=False,
    )
    check(r.status_code == 303 and "engine_warning=" in r.headers["location"], r.headers)
    prof = engines.get_by_name("Web SemIf")
    check(prof is not None and prof.config == {"confidence_floor": 0.65, "timeout_s": 20.0}, "web")
    page = client.get("/setup-ai").text
    check("Quality judges" in page and "Web SemIf" in page, "Setup AI shows Quality judges")
    r = client.post(
        f"/setup-ai/engines/{prof.id}",
        data={"name": "Web SemIf", "base_url": "http://10.1.2.3:8077", "enabled": "1"},
        follow_redirects=False,
    )
    check(engines.get_profile(prof.id).config == {}, "blank floors on Edit clear them")
    r = client.post(
        "/setup-ai/engines",
        data={
            "name": "Web Jev",
            "kind": "jev",
            "api_key": HOSTED_KEY,
            "confidence_floor": "0.7",
            "timeout_s": "15",
        },
        follow_redirects=False,
    )
    check(r.status_code == 303 and "engine_saved=1" in r.headers["location"], r.headers)
    page = client.get("/setup-ai").text
    check(no_key(page), "the Setup AI page never renders the key")
    # Docs pass (web/CLI parity): hosted Jev takes the floors + timeout on the web form too,
    # the Edit form renders them, and a blank field on Edit clears them like jev_local.
    web_jev = engines.get_by_name("Web Jev")
    check(web_jev.config == {"confidence_floor": 0.7, "timeout_s": 15.0}, str(web_jev.config))
    check(
        'name="confidence_floor" value="0.7"' in page and 'placeholder="10"' in page,
        "the hosted Jev Edit form renders its floors and the 10 s timeout default",
    )
    client.post(
        f"/setup-ai/engines/{web_jev.id}",
        data={"name": "Web Jev", "enabled": "1"},
        follow_redirects=False,
    )
    check(engines.get_profile(web_jev.id).config == {}, "blank hosted floors on Edit clear them")

    # /api: create a hosted jev with its key; the response carries no key.
    user = security.create_user("jeveng@test.invalid", "Jev admin", "admin", "jev-password-1234")
    _, raw = apikeys.create_key(user.id, "jeveng", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    headers = {"X-Loc-Token": raw}
    r = client.post(
        "/api/admin/engines",
        json={"name": "API Jev", "kind": "jev", "api_key": HOSTED_KEY},
        headers=headers,
    )
    check(r.status_code == 200 and no_key(r.text), f"api create: {r.status_code} {r.text}")
    check(r.json().get("warnings") == [], "api create returns a warnings list")
    r = client.post(
        "/api/admin/engines",
        json={"name": "API SemIf", "kind": "jev_local", "base_url": "http://10.4.4.4:8077"},
        headers=headers,
    )
    check(r.status_code == 200 and len(r.json()["warnings"]) == 1, r.text)
    r = client.post(
        "/api/admin/engines",
        json={"name": "API bad", "kind": "jev", "api_key": HOSTED_KEY, "base_url": "http://x.y"},
        headers=headers,
    )
    check(r.status_code == 400 and no_key(r.text), f"api pinned refusal: {r.text}")

    # CLI: create + show (JSON), warnings, never the key.
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli(
            [
                "--json",
                "engine",
                "create",
                "--name",
                "CLI SemIf",
                "--kind",
                "jev_local",
                "--base-url",
                "http://10.5.5.5:8077",
                "--config",
                "meaning_floor=0.75",
            ]
        )
    payload = json.loads(out.getvalue())
    check(code == 0 and payload["kind"] == "jev_local" and payload["judge"], str(payload))
    check(payload["config"] == {"meaning_floor": 0.75} and len(payload["warnings"]) == 1, "cli")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        cli(
            [
                "--json",
                "engine",
                "create",
                "--name",
                "CLI Jev",
                "--kind",
                "jev",
                "--secret",
                "api_key=" + HOSTED_KEY,  # pregate: allow-secret the fake module constant
            ]
        )
    created = json.loads(out.getvalue())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli(["--json", "engine", "show", str(created["id"])])
    check(no_key(out.getvalue()) and '"key_set": true' in out.getvalue(), "cli show masks key")
    seen = []
    cmd_engine._set_transport(jev_transport(seen, models=[]))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli(["--json", "engine", "test", str(payload["id"])])
    cmd_engine._set_transport(None)
    check(code == 0 and "one-question check OK" in out.getvalue(), out.getvalue())

    # MCP manage_install: create a jev_local (no key ever carried) and test one.
    mcp_dir = Path(_ROOT) / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("jeveng_mcp_server", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", raw):
        result = json.loads(
            asyncio.run(
                server.manage_install(
                    action="create_engine",
                    name="MCP SemIf",
                    kind="jev_local",
                    base_url="http://127.0.0.1:8079",
                    config={"confidence_floor": 0.8},
                )
            )
        )
        check(result["engine"]["kind"] == "jev_local", str(result))
        check(result["engine"]["config"] == {"confidence_floor": 0.8}, str(result))
        api_jev = engines.get_by_name("API Jev")
        shown = asyncio.run(
            server.manage_install(
                action="update_engine", engine_id=api_jev.id, config={"timeout_s": 9}
            )
        )
        check(no_key(shown) and '"key_set": true' in shown, "MCP output never carries the key")
        try:
            asyncio.run(
                server.manage_install(
                    action="create_engine",
                    name="MCP Jev",
                    kind="jev",
                )
            )
            check(False, "MCP cannot create a keyless hosted jev")
        except Exception as exc:
            check(no_key(str(exc)), "MCP refusal has no key")
    settings.set_many({"remote_admin_enabled": "0"})
    print("[9] surfaces OK")


# --- Evaluations Lab -------------------------------------------------------------------
def test_lab(local, off):
    from app.services import eval_metrics

    with patch.object(qe_jev, "_post", return_value=(200, answer())) as post:
        result = eval_metrics.score(
            "judge_qe_jev",
            source_text="a",
            output_text="b",
            ctx={"jev_ref": f"engine:{local.id}"},
        )
    check(result.ok and result.value == 100, str(result))
    check(result.detail["judge_provider"] == f"engine:{local.id}", str(result.detail))
    check(len(post.call_args.args) == 2, "a jev_ref scores through the profile target")
    result = eval_metrics.score(
        "judge_qe_jev", source_text="a", output_text="b", ctx={"jev_ref": f"engine:{off.id}"}
    )
    check(result.not_applicable, "a dead jev_ref is not_applicable, never a zero")
    with patch.object(config, "TYPESAFE_API_KEY", ""):
        result = eval_metrics.score("judge_qe_jev", source_text="a", output_text="b")
    check(result.not_applicable and result.detail["judge_provider"] == "jev", "env default")
    print("[10] Evaluations Lab jev_ref OK")


# --- Fix round 1 (Reviewer review + live findings) ------------------------------------------
def flex_transport(seen, route):
    """A MockTransport whose answer per request comes from route(request) -> Response."""

    def handler(req: httpx.Request):
        seen.append(
            {
                "host": req.url.host,
                "path": req.url.path,
                "auth": req.headers.get("Authorization"),
                "body": json.loads(req.content) if req.content else None,
            }
        )
        return route(req)

    return httpx.MockTransport(handler)


def fix_l1_models_shape():
    """L1: the real Jev / SemIf / Kev GET /v1/models shape is models[].name."""
    prof = engines.create_profile("L1 SemIf", "jev_local", base_url="http://127.0.0.1:8091")
    live_models = {
        "models": [
            {"name": "Qwen/Qwen3.5-4B", "object": "model"},
            {"name": "jev-latest", "object": "model"},
            {"name": "jev-preview", "object": "model"},
        ]
    }

    def route(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=live_models)
        return httpx.Response(200, json=answer())

    ok, msg = engines.test_profile(prof.id, transport=flex_transport([], route))
    check(ok and "3 model(s)" in msg, f"[L1] live models shape counted: {msg}")
    check("'jev-latest' listed" in msg, f"[L1] jev-latest found: {msg}")

    def route_oai(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "jev-latest"}]})
        return httpx.Response(200, json=answer())

    ok, msg = engines.test_profile(prof.id, transport=flex_transport([], route_oai))
    check(ok and "1 model(s)" in msg and "listed" in msg, f"[L1] data[].id fallback: {msg}")
    print("[L1] live models[].name shape OK")


def fix_l2_ref_string(local):
    """L2: a ref string as judge= resolves; a wrong type is a TypeError, never a skip."""
    with patch.object(qe_jev, "_post", return_value=(200, answer())) as post:
        card = qe_jev.score_segment("s", "t", "en", "de", judge=f"engine:{local.id}")
    check(not card["skipped"] and card["score"] == 100, f"[L2] ref string resolves: {card}")
    check(len(post.call_args.args) == 2, "[L2] ref string scored via the profile target")
    try:
        qe_jev.score_segment("s", "t", "en", "de", judge=12345)
        check(False, "[L2] a non-target judge must raise TypeError, not skip")
    except TypeError:
        pass
    print("[L2] judge ref string OK")


def fix_a2_probe_spend():
    """A2: the hosted probe POST is capped and its reported usage is booked."""
    hosted = engines.create_profile("A2 Jev", "jev", api_key=HOSTED_KEY)
    local = engines.create_profile("A2 SemIf", "jev_local", base_url="http://127.0.0.1:8092")
    for status in (200, 500):
        seen = []

        def route(req, status=status):
            if req.url.path == "/v1/models":
                return httpx.Response(200, json={"models": []})
            body = answer() if status == 200 else {"usage": {"input_tokens": 500}}
            if status == 200:
                body["usage"] = {"input_tokens": 500, "output_tokens": 5}
            return httpx.Response(status, json=body)

        with patch.object(spend, "record_external_usd") as booked:
            engines.test_profile(hosted.id, transport=flex_transport(seen, route))
        check(
            booked.call_count == 1
            and abs(booked.call_args.args[0] - 500 * qe_jev.JEV_USD_PER_INPUT_TOKEN) < 1e-15,
            f"[A2] hosted probe books its usage on HTTP {status}: {booked.call_args_list}",
        )
    seen = []
    with (
        patch.object(spend, "would_block", return_value=(True, 9.0, 9.0)),
        patch.object(spend, "record_external_usd") as booked,
    ):
        ok, msg = engines.test_profile(
            hosted.id,
            transport=flex_transport(seen, lambda req: httpx.Response(200, json=answer())),
        )
    check(not ok and "spend cap" in msg.lower(), f"[A2] tripped cap reported: {msg}")
    check(not [r for r in seen if r["path"] == "/v1/systemone"], "[A2] no POST once capped")
    booked.assert_not_called()
    usage_answer = answer()
    usage_answer["usage"] = {"input_tokens": 500}
    with (
        patch.object(spend, "would_block", return_value=(True, 9.0, 9.0)),
        patch.object(spend, "record_external_usd") as booked,
    ):
        ok, msg = engines.test_profile(
            local.id,
            transport=flex_transport([], lambda req: httpx.Response(200, json=usage_answer)),
        )
    check(ok, f"[A2] jev_local probe ignores the cap: {msg}")
    booked.assert_not_called()
    print("[A2] hosted probe spend cap + accounting OK")


def fix_a3_key_hygiene():
    """A3: a control character in a key is refused at save; probe errors never echo it."""
    bad = "abc\nSECRETVALUE-A3"  # pregate: allow-secret synthetic key for the leak test
    for kind, base in (("jev", ""), ("jev_local", "http://127.0.0.1:8093")):
        try:
            engines.create_profile(f"A3 {kind}", kind, base_url=base, api_key=bad)
            check(False, f"[A3] a key with a newline must be refused ({kind})")
        except ValueError as exc:
            check("SECRETVALUE" not in str(exc), "[A3] refusal never echoes the key")
    padded = engines.create_profile(
        "A3 padded",
        "jev_local",
        base_url="http://127.0.0.1:8093",
        api_key="  padkey-A3 \n",  # pregate: allow-secret synthetic padded key for the strip test
    )
    check(
        engines.dec_key(engines.get_profile(padded.id).api_key_enc) == "padkey-A3",
        "[A3] surrounding whitespace is stripped",
    )
    # A row written before the save-time check: force the bad key in, then probe.
    with Session(db_engine) as s:
        row = s.get(EngineProfile, padded.id)
        row.api_key_enc = engines.enc_key(bad)
        s.add(row)
        s.commit()

    def boom(req):
        raise httpx.LocalProtocolError(
            "Illegal header value " + repr(req.headers.get("Authorization"))
        )

    ok, msg = engines.test_profile(padded.id, transport=httpx.MockTransport(boom))
    check(not ok and "SECRETVALUE" not in msg, f"[A3] probe error sanitized: {msg}")
    print("[A3] key hygiene OK")


def fix_a4_probe_timeout():
    """A4: both probe requests use the profile's resolved timeout (scoring's rules)."""
    cases = (
        (engines.create_profile("A4 default", "jev_local", base_url="http://127.0.0.1:8094"), 30),
        (
            engines.create_profile(
                "A4 set", "jev_local", base_url="http://127.0.0.1:8094", cfg={"timeout_s": "90"}
            ),
            90,
        ),
        (engines.create_profile("A4 hosted", "jev", api_key=HOSTED_KEY), 10),
    )
    for prof, want in cases:
        with patch.object(engines, "_client", wraps=engines._client) as mk:
            engines.test_profile(
                prof.id,
                transport=flex_transport([], lambda req: httpx.Response(200, json=answer())),
            )
        got = mk.call_args.kwargs.get("timeout", mk.call_args.args[2:3] or None)
        check(got == want, f"[A4] {prof.name}: probe timeout {got!r}, want {want}")
        check(
            qe_jev.resolve_target(f"engine:{prof.id}").timeout_s == want,
            "[A4] scoring uses the same timeout",
        )
    print("[A4] probe timeout OK")


def fix_a5_floors_redirect():
    """A5: profile floors really change the verdict; redirects are never followed."""
    strict = engines.create_profile(
        "A5 strict",
        "jev_local",
        base_url="http://127.0.0.1:8095",
        cfg={"confidence_floor": "0.9", "meaning_floor": "0.5"},
    )
    plain = engines.create_profile("A5 plain", "jev_local", base_url="http://127.0.0.1:8095")
    borderline = answer()
    borderline["answers"]["terminology"]["probabilities"] = [0.85, 0.15, 0, 0]
    meaning = answer()
    meaning["answers"]["meaning_changed"]["noul"] = 0.6
    for data, prof, check_fn, label in (
        (borderline, plain, lambda c: c["score"] == 100 and not c["unsure"], "p_none 0.85 @0.6"),
        (borderline, strict, lambda c: c["reason"] == "low_confidence", "p_none 0.85 @0.9"),
        (meaning, plain, lambda c: c["score"] == 100, "noul 0.6 @0.8"),
        (meaning, strict, lambda c: c["categories"].get("accuracy") == 2, "noul 0.6 @0.5"),
    ):
        with patch.object(qe_jev, "_post", return_value=(200, data)):
            card = qe_jev.score_segment(
                "s", "t", "en", "de", judge=qe_jev.resolve_target(f"engine:{prof.id}")
            )
        check(check_fn(card), f"[A5] floor case {label}: {card}")
    seen = []

    def redirect(req):
        return httpx.Response(307, headers={"Location": "http://10.99.99.99:9/v1/systemone"})

    qe_jev._TRANSPORT = flex_transport(seen, redirect)
    try:
        card = qe_jev.score_segment(
            "s", "t", "en", "de", judge=qe_jev.resolve_target(f"engine:{plain.id}")
        )
    finally:
        qe_jev._TRANSPORT = None
    check(card["reason"] == "provider_error", f"[A5] a redirect is an error: {card}")
    check([r["host"] for r in seen] == ["127.0.0.1"], f"[A5] redirect not followed: {seen}")
    seen.clear()
    ok, msg = engines.test_profile(plain.id, transport=flex_transport(seen, redirect))
    check(not ok and all(r["host"] == "127.0.0.1" for r in seen), f"[A5] probe: {msg} {seen}")
    print("[A5] floors + redirects OK")


def fix_a5_unsure_dispatch(client, pid, local):
    """A5: an unsure card from a PROFILE judge never passes the gate."""
    from app.services import orchestrator

    with patch("tests._bootstrap.init_env", return_value=_TMP):
        from tests.verify_qe_gate import QEProvider, make_wf, new_segmented_doc, segs_for
    data = answer(style=1)
    data["answers"]["accuracy"]["probabilities"] = [0.44, 0.56, 0, 0]
    with Session(db_engine) as s:
        wid = make_wf(
            s,
            "unsure profile gate",
            qe_gate=True,
            qe_provider=f"engine:{local.id}",
            qe_threshold=70,
        )
    did = new_segmented_doc(client, pid, ["Unsure one."])
    QEProvider.reset()
    with patch.object(qe_jev, "_post", return_value=(200, data)), Session(db_engine) as s:
        events = list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
    with Session(db_engine) as s:
        seg = segs_for(s, did)[0]
        flag = next(f for f in seg.qa_flags if f["type"] == "qe")
    # Docs pass H2 (Reviewer LOW): the "counted as QE skipped" half of UNDECIDED_RULE, in both
    # the step tally and the run tally: one skipped, nothing scored, passed or failed.
    undecided_tally = {"scored": 0, "passed": 0, "failed": 0, "skipped": 1}
    step_tallies = [e["qe"] for e in events if e.get("type") != "done" and "qe" in e]
    run_tallies = [e["qe"] for e in events if e.get("type") == "done" and "qe" in e]
    check(step_tallies == [undecided_tally], f"[A5] step QE tally: {step_tallies}")
    check(run_tallies == [undecided_tally], f"[A5] run QE tally: {run_tallies}")
    check(flag.get("passed") is not True, f"[A5] an unsure card never passes: {flag}")
    check(flag.get("undecided") is True and flag.get("score") == 95, f"[A5] undecided: {flag}")
    # Docs pass H2: the behaviour qe_autofix.UNDECIDED_RULE states (and the Inspect page
    # renders): not routed to review, a "QE undecided" qe flag, counted as QE skipped.
    check(seg.status == SegStatus.MT, f"[A5] undecided carries on unchanged: {seg.status}")
    check(str(flag.get("message", "")).startswith("QE undecided"), f"[A5] badge: {flag}")
    print("[A5] unsure profile dispatch OK")


def fix_a1_cross_org():
    """A1: org A can never update, test or delete org B's engine over /api, and B's key
    never reaches a host A controls. Foreign and missing ids answer the same 404."""
    from app.models import Organization, User
    from app.services import apikeys, security

    with Session(db_engine) as s:
        org_a, org_b = Organization(name="Jev Org A"), Organization(name="Jev Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        oa, ob = org_a.id, org_b.id
    b_eng = engines.create_profile(
        "B SemIf keyed",
        "jev_local",
        base_url="http://10.20.20.20:8077",
        api_key=LOCAL_KEY,
        org_id=ob,
    )
    a_eng = engines.create_profile(
        "A SemIf", "jev_local", base_url="http://10.30.30.30:8077", org_id=oa
    )
    user = security.create_user("orga@jev.invalid", "Org A admin", "admin", "jev-password-1234")
    with Session(db_engine) as s:
        row = s.get(User, user.id)
        row.org_id = oa
        s.add(row)
        s.commit()
    _, raw = apikeys.create_key(user.id, "orga", scopes="api docs admin")
    headers = {"X-Loc-Token": raw}
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "1", "remote_admin_enabled": "1"}
    )
    settings.invalidate_cache()
    probed = []

    def recorder(prof, key, transport):
        probed.append((prof.id, prof.base_url, key))
        return True, "recorded"

    client = TestClient(app)
    try:
        with patch.dict(engines._PROBES, {"jev_local": recorder}):
            missing = client.post(
                "/api/admin/engines/999999/update", json={"name": "x"}, headers=headers
            )
            r = client.post(
                f"/api/admin/engines/{b_eng.id}/update",
                json={"base_url": "http://10.66.66.66:9"},
                headers=headers,
            )
            check(r.status_code == 404, f"[A1] foreign update refused: {r.status_code}")
            check(r.json() == missing.json(), "[A1] foreign update looks like a missing id")
            check(
                engines.get_profile(b_eng.id).base_url == "http://10.20.20.20:8077",
                "[A1] B's base_url unchanged",
            )
            r = client.post(f"/api/admin/engines/{b_eng.id}/test", headers=headers)
            missing = client.post("/api/admin/engines/999999/test", headers=headers)
            check(r.status_code == 404 and r.json() == missing.json(), f"[A1] test: {r.text}")
            check(not probed, f"[A1] B's key never sent anywhere: {probed}")
            r = client.post(f"/api/admin/engines/{b_eng.id}/delete", headers=headers)
            missing = client.post("/api/admin/engines/999999/delete", headers=headers)
            check(r.status_code == 404 and r.json() == missing.json(), f"[A1] delete: {r.text}")
            check(engines.get_profile(b_eng.id) is not None, "[A1] B's engine still exists")
            r = client.post(
                f"/api/admin/engines/{a_eng.id}/update", json={"model": "semif-1"}, headers=headers
            )
            check(r.status_code == 200, f"[A1] own engine still editable: {r.text}")
            r = client.post(f"/api/admin/engines/{a_eng.id}/test", headers=headers)
            check(r.status_code == 200 and probed == [(a_eng.id, a_eng.base_url, "")], str(probed))
        # The step seam confines judge refs to the workflow's org too.
        from app.models import Workflow
        from app.services import workflows

        with Session(db_engine) as s:
            wf = workflows.create_workflow(s, "org A judge wf")
            row = s.get(Workflow, wf.id)
            row.org_id = oa
            s.add(row)
            s.commit()
            for ref, ok_expected in ((f"engine:{b_eng.id}", False), (f"engine:{a_eng.id}", True)):
                try:
                    workflows.add_step(
                        s,
                        wf.id,
                        name="judge",
                        step_type="llm_agent",
                        provider="mock",
                        prompt_text="{text}",
                        qe_gate=True,
                        qe_provider=ref,
                    )
                    check(ok_expected, f"[A1] org A step may not use org B's judge {ref}")
                except workflows.WorkflowError as exc:
                    check(not ok_expected, f"[A1] own judge refused: {exc}")
                    check("not an available engine" in str(exc), f"[A1] message: {exc}")
    finally:
        settings.set_many(
            {"security_auth_required": "0", "multi_org_enabled": "0", "remote_admin_enabled": "0"}
        )
        settings.invalidate_cache()
    print("[A1] cross-org engine API OK")


# --- Fix round 2 (Review gate notes M-1, L-1, L-2) -----------------------------------------
TYPESAFE_VARIANTS = (
    "https://api.typesafe.ai",
    "http://API.TypeSafe.AI:8443/v1",
    "https://api.typesafe.ai./",
    "https://user:pw@api.typesafe.ai/x",  # pregate: allow-secret userinfo in a URL fixture
    "https://eu.typesafe.ai",
    "api.typesafe.ai:443",
)


def fix2_m1_typesafe_host():
    """M-1: a jev_local profile may never point at TypeSafe (it would bill uncapped)."""
    for url in TYPESAFE_VARIANTS:
        try:
            engines.create_profile("M1 " + url, "jev_local", base_url=url, api_key=HOSTED_KEY)
            check(False, f"[M-1] jev_local at {url} must be refused")
        except ValueError as exc:
            expected = "API key field" if "@" in url else "Jev (TypeSafe"
            check(expected in str(exc) and no_key(str(exc)), f"[M-1] message: {exc}")
    ok_prof = engines.create_profile("M1 local", "jev_local", base_url="http://127.0.0.1:8096")
    try:
        engines.update_profile(ok_prof.id, base_url="https://api.typesafe.ai")
        check(False, "[M-1] moving a jev_local onto TypeSafe must be refused")
    except ValueError:
        pass
    # Call time: a row written some other way (direct DB write, a future import path).
    with Session(db_engine) as s:
        row = s.get(EngineProfile, ok_prof.id)
        row.base_url = "https://API.typesafe.ai./v1"
        row.api_key_enc = engines.enc_key(HOSTED_KEY)
        s.add(row)
        s.commit()
    try:
        qe_jev.resolve_target(f"engine:{ok_prof.id}")
        check(False, "[M-1] resolving a jev_local at TypeSafe must be refused")
    except DeadEngineError as exc:
        check("Jev (TypeSafe" in str(exc) and no_key(str(exc)), f"[M-1] resolve: {exc}")
    seen = []
    forged = qe_jev.JevTarget(
        kind="jev_local",
        base_url="https://api.typesafe.ai",
        key=HOSTED_KEY,
        model="jev-latest",
        confidence_floor=None,
        meaning_floor=None,
        timeout_s=5.0,
        priced=False,
        provider_ref="engine:0",
        profile_id=0,
    )
    qe_jev._TRANSPORT = flex_transport(seen, lambda req: httpx.Response(200, json=answer()))
    try:
        card = qe_jev.score_segment("s", "t", "en", "de", judge=forged)
    finally:
        qe_jev._TRANSPORT = None
    check(card["skipped"] and not seen, f"[M-1] transport refuses TypeSafe unpriced: {card}")
    ok, msg = engines.test_profile(
        ok_prof.id, transport=flex_transport(seen, lambda req: httpx.Response(200, json=answer()))
    )
    check(not ok and not seen and "Jev (TypeSafe" in msg, f"[M-1] probe refused: {msg} {seen}")
    print("[M-1] jev_local never reaches TypeSafe OK")


def fix2_l1_routing():
    """L-1: a routing rule's provider is a translate slot."""
    from app.services import routing

    judge = engines.create_profile("L1 judge", "jev_local", base_url="http://127.0.0.1:8097")
    with Session(db_engine) as s:
        try:
            routing.create_rule(s, match_ext="docx", provider=f"engine:{judge.id}")
            check(False, "[L-1] a routing rule must refuse a judge provider")
        except routing.RoutingRuleError as exc:
            check("QE judge" in str(exc), f"[L-1] message: {exc}")
        rule = routing.create_rule(s, match_ext="docx", provider="mock")
        check(rule.provider == "mock", "[L-1] a translate provider is still accepted")
    print("[L-1] routing rule role check OK")


def fix2_l2_defences():
    """L-2: the call-time hosted pin and the customer-default role check are guarded."""
    from app.models import Customer
    from app.services import customers

    hosted = engines.create_profile("L2 Jev", "jev", api_key=HOSTED_KEY)
    with Session(db_engine) as s:
        row = s.get(EngineProfile, hosted.id)
        row.base_url = "http://10.44.44.44:9"  # written around the save-time pin
        s.add(row)
        s.commit()
    target = qe_jev.resolve_target(f"engine:{hosted.id}")
    check(target.base_url == engines.JEV_HOSTED_BASE_URL, f"[L-2] call-time pin: {target}")
    seen = []
    qe_jev._TRANSPORT = flex_transport(seen, lambda req: httpx.Response(200, json=answer()))
    try:
        qe_jev.score_segment("s", "t", "en", "de", judge=target)
    finally:
        qe_jev._TRANSPORT = None
    check([r["host"] for r in seen] == ["api.typesafe.ai"], f"[L-2] scoring host: {seen}")
    seen.clear()
    engines.test_profile(
        hosted.id, transport=flex_transport(seen, lambda req: httpx.Response(200, json=answer()))
    )
    check(seen and all(r["host"] == "api.typesafe.ai" for r in seen), f"[L-2] probe: {seen}")
    judge = engines.create_profile("L2 judge", "jev_local", base_url="http://127.0.0.1:8098")
    with Session(db_engine) as s:
        cust = Customer(name="L2 customer")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        try:
            customers.set_customer_defaults(s, cust.id, provider=f"engine:{judge.id}")
            check(False, "[L-2] a judge as a customer default must be refused")
        except ValueError as exc:
            check("QE judge" in str(exc), f"[L-2] message: {exc}")
        check(s.get(Customer, cust.id).default_provider is None, "[L-2] default unchanged")
    print("[L-2] call-time pin + customer default OK")


def fix3_i390_probe_echo(keyed):
    """#390 (Review gate): the Test message lists model ids, so a server echoing the bearer
    it received back as a model id must not put the saved key in that message (a
    can_tune_ai linguist reads it). The probe's message scrub already redacted the key;
    the guard now drops the echo before it is counted or listed (FAILS pre-fix on the
    "1 model(s)" count: the echo showed as a "[redacted]" model)."""

    def route(req):
        if req.url.path == "/v1/models":
            token = req.headers.get("Authorization", "").removeprefix("Bearer ")
            return httpx.Response(200, json={"data": [{"id": token}, {"id": "semif-1"}]})
        return httpx.Response(200, json=answer())

    _ok, msg = engines.test_profile(keyed.id, transport=flex_transport([], route))
    check(no_key(msg), f"probe message must not echo the saved key: {msg}")
    check("1 model(s)" in msg and "semif-1" in msg, f"real id still listed: {msg}")
    print("[fix3] #390 probe drops an id echoing the saved key")


def main():
    init_db()
    hosted, local, _lan, keyed = test_create_rules()
    test_providers(hosted, local)
    dl, off = test_targets(hosted, local, keyed)
    test_scoring(hosted, local, keyed)
    client, pid = test_dispatch(local, off)
    test_roles(client, pid, local, dl)
    test_probe(hosted, local)
    test_surfaces(client)
    test_lab(local, off)
    for fix in (
        fix_l1_models_shape,
        lambda: fix_l2_ref_string(local),
        fix_a2_probe_spend,
        fix_a3_key_hygiene,
        fix_a4_probe_timeout,
        fix_a5_floors_redirect,
        lambda: fix_a5_unsure_dispatch(client, pid, local),
        fix_a1_cross_org,
        fix2_m1_typesafe_host,
        fix2_l1_routing,
        fix2_l2_defences,
        lambda: fix3_i390_probe_echo(keyed),
    ):
        try:
            fix()
        except Exception as exc:  # one broken fix test must not hide the others
            check(False, f"{getattr(fix, '__name__', 'fix')}: {type(exc).__name__}: {exc}")
    assert not FAILURES, FAILURES
    print("verify_jev_engines OK")


if __name__ == "__main__":
    main()
