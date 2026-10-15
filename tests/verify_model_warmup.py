"""#341: run-start warm-up of a router-mode local server's models. Offline (MockTransport).

Asserts the collector (step order, dedupe, cloud kinds skipped, the cap of 3, blank model
resolved to the profile's own), the wire behaviour (load calls only for models the router
reports as not loaded; a non-router server or a 404 is skipped quietly; the stored key only
ever reaches the engine's own origin), the switch, and the seam: a real run through BOTH
execution loops sends the loads, a resume does not, and a failing warm-up never changes the
run's outcome.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_model_warmup_")

import json

import httpx
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import engines, model_warmup, orchestrator, settings

init_db()

HOST_A = "http://127.0.0.1:8082"
HOST_B = "http://127.0.0.1:8083"


def _fail(msg):
    raise AssertionError(msg)


class Server:
    """A fake llama.cpp router. `status` maps model id -> status value (None = no status
    object at all, i.e. a non-router server). Records every request."""

    def __init__(
        self, status, *, load_code=200, models_code=200, explode=False, timeout_loads=(), gate=None
    ):
        self.status = status
        self.timeout_loads = set(timeout_loads)  # models whose load POST times out
        self.gate = gate  # a threading.Event: every request blocks until it is set
        self.load_code = load_code
        self.models_code = models_code
        self.explode = explode
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            self.gate.wait(30)
        if (
            request.method == "POST"
            and request.url.path == "/models/load"
            and json.loads(request.content)["model"] in self.timeout_loads
        ):
            raise httpx.ReadTimeout("slow", request=request)
        if self.explode:
            raise httpx.ConnectError("boom", request=request)
        if request.method == "GET" and request.url.path == "/v1/models":
            data = []
            for mid, st in self.status.items():
                entry = {"id": mid, "object": "model"}
                if st is not None:
                    entry["status"] = {"value": st}
                data.append(entry)
            return httpx.Response(self.models_code, json={"object": "list", "data": data})
        if request.method == "POST" and request.url.path == "/models/load":
            return httpx.Response(self.load_code, json={"success": True})
        return httpx.Response(404, json={"error": "nope"})

    def loads(self):
        return [
            json.loads(r.content)["model"]
            for r in self.requests
            if r.method == "POST" and r.url.path == "/models/load"
        ]


def use(server):
    model_warmup._transport = httpx.MockTransport(server)


_n = [0]


def profile(kind="local", base=HOST_A, key="", model="", enabled=True):
    _n[0] += 1
    return engines.create_profile(
        f"warm-{_n[0]}",
        kind,
        base_url=base,
        api_key=key or ("k" if kind == "openai_compatible" else ""),
        model=model,
        enabled=enabled,
    )


def step(provider, model="", **kw):
    return WorkflowStep(
        workflow_id=1,
        name="s",
        step_type=kw.pop("step_type", StepType.LLM_AGENT),
        provider=provider,
        model=model or None,
        **kw,
    )


def ref(p):
    return f"engine:{p.id}"


def warm(steps):
    return model_warmup.warm_targets(model_warmup.collect_targets(steps))


def test_two_models_in_step_order():
    p = profile()
    srv = Server({"gemma-4-31b": "unloaded", "qwen3.8-27b": "unloaded"})
    use(srv)
    done = warm([step(ref(p), "gemma-4-31b"), step(ref(p), "qwen3.8-27b", step_type=StepType.LQA)])
    assert srv.loads() == ["gemma-4-31b", "qwen3.8-27b"], srv.loads()
    assert [d[2] for d in done] == ["load-requested"] * 2, done
    print("  two-model workflow sends two loads in step order OK")


def test_dedupe_and_blank_model_uses_profile_model():
    p = profile(model="own-model")
    srv = Server({"own-model": "unloaded", "other": "unloaded"})
    use(srv)
    warm([step(ref(p)), step(ref(p), "own-model"), step(ref(p), "other"), step(ref(p), "other")])
    assert srv.loads() == ["own-model", "other"], srv.loads()
    print("  duplicates deduped; blank model resolves to the engine's own OK")


def test_fallback_qe_and_autofix_refs_collected_in_order():
    a, b, c = profile(), profile(), profile()
    s = step(
        ref(a),
        "m-a",
        fallback_provider=ref(b),
        fallback_model="m-b",
        qe_gate=True,
        qe_provider=ref(c),
        qe_autofix=True,
        qe_autofix_provider=ref(a),
    )
    got = model_warmup.collect_targets([s])
    # primary a/m-a, fallback b/m-b, judge c (its own model is blank and the profile has
    # none -> not nameable, skipped), fixer a with the install default model for that ref
    # (blank -> the profile's own model, none -> skipped).
    assert got == [(a.id, "m-a"), (b.id, "m-b")], got
    c2 = profile(model="judge-model")
    s2 = step(ref(a), "m-a", qe_gate=True, qe_provider=ref(c2))
    assert model_warmup.collect_targets([s2]) == [(a.id, "m-a"), (c2.id, "judge-model")]
    print("  fallback/QE/auto-fix refs collected after the primary OK")


def test_cloud_and_builtin_skipped():
    cloud = profile("openai_compatible", base="https://api.example.com/v1", key="sk-x")
    disabled = profile(enabled=False)
    loc = profile()
    srv = Server({"m1": "unloaded"})
    use(srv)
    warm(
        [
            step(ref(cloud), "m1"),
            step(ref(disabled), "m1"),
            step("claude", "m1"),
            step("mock"),
            step(None, "m1"),
            step(ref(loc), "m1", step_type=StepType.HUMAN_REVIEW),
            step(ref(loc), "m1"),
        ]
    )
    assert srv.loads() == ["m1"], srv.loads()
    assert all(r.url.host == "127.0.0.1" for r in srv.requests), srv.requests
    print("  cloud kinds, built-ins, disabled and non-engine steps skipped OK")


def test_cap_of_three():
    p = profile()
    models = [f"m{i}" for i in range(6)]
    srv = Server(dict.fromkeys(models, "unloaded"))
    use(srv)
    warm([step(ref(p), m) for m in models])
    assert srv.loads() == ["m0", "m1", "m2"], srv.loads()
    assert model_warmup.MAX_WARM_MODELS == 3
    print("  at most 3 models warmed, in step order OK")


def test_loaded_loading_not_reloaded_and_unlisted_skipped():
    p = profile()
    srv = Server({"a": "loaded", "b": "loading", "c": "unloaded"})
    use(srv)
    done = warm([step(ref(p), m) for m in ("a", "b", "c", "ghost")])
    assert srv.loads() == ["c"], srv.loads()
    assert {d[1]: d[2] for d in done} == {"a": "loaded", "b": "loading", "c": "load-requested"}
    print("  loaded/loading models not reloaded; unlisted model skipped OK")


def test_non_router_and_404_skipped():
    p = profile()
    srv = Server({"m1": None})
    use(srv)
    assert warm([step(ref(p), "m1")]) == [] and srv.loads() == []
    srv = Server({"m1": "unloaded"}, load_code=404)
    use(srv)
    assert warm([step(ref(p), "m1")]) == [], "404 on /models/load must be skipped"
    srv = Server({"m1": "unloaded"}, models_code=500)
    use(srv)
    assert warm([step(ref(p), "m1")]) == [] and srv.loads() == []
    print("  non-router server, 404 and error listing skipped quietly OK")


def test_key_only_to_own_origin():
    a = profile(base=HOST_A, key="sekret-A", model="ma")
    b = profile(base=HOST_B, key="sekret-B", model="mb")
    srv = Server({"ma": "unloaded", "mb": "unloaded"})
    use(srv)
    warm([step(ref(a)), step(ref(b))])
    assert srv.loads() == ["ma", "mb"], srv.loads()
    for r in srv.requests:
        auth = r.headers.get("authorization")
        want = "Bearer sekret-A" if r.url.port == 8082 else "Bearer sekret-B"
        assert auth == want, (r.url, auth)
        assert "sekret" not in str(r.url) and b"sekret" not in r.content
    print("  each stored key sent only to its own engine origin OK")


def test_switch_off_and_resume_send_nothing():
    p = profile()
    srv = Server({"m1": "unloaded"})
    use(srv)
    s = [step(ref(p), "m1")]
    settings.set_many({"local_model_warmup": "0"})
    try:
        assert model_warmup.start_for_run(s, run_id=1) is None
    finally:
        settings.set_many({"local_model_warmup": "1"})
    assert model_warmup.start_for_run(s, is_resume=True, run_id=1) is None
    assert srv.requests == [], srv.requests
    t = model_warmup.start_for_run(s, run_id=1)
    assert t is not None
    t.join(10)
    assert srv.loads() == ["m1"]
    print("  switch off and resume send nothing; default on sends OK")


def _seed(mode, wf_steps):
    with Session(engine) as s:
        project = Project(name="Warm", source_locale="en-US", default_provider="mock")
        s.add(project)
        s.commit()
        doc = Document(
            project_id=project.id,
            filename="w.txt",
            fmt="txt",
            mode=mode,
            status="new",
            skeleton={"format": "txt", "units": [{"text": "Hello world.", "p_index": 0}]},
        )
        wf = Workflow(name=f"Warm {mode}")
        s.add(doc)
        s.add(wf)
        s.add(TargetLocale(project_id=project.id, locale="fr-FR"))
        s.commit()
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        for i, (prov, fb, fbm) in enumerate(wf_steps):
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    key=f"s{i}",
                    name=f"Step {i}",
                    step_type=StepType.LLM_AGENT,
                    provider=prov,
                    fallback_provider=fb,
                    fallback_model=fbm,
                    prompt_text="Translate: {text}",
                )
            )
        s.commit()
        return wf.id, doc.id


def _run(mode, srv):
    """A real run of a mock-provider workflow whose FALLBACK slots name the local engine
    (so the workflow references the models without ever calling them)."""
    p = profile()
    wf_id, doc_id = _seed(mode, [("mock", ref(p), "m1"), ("mock", ref(p), "m2")])
    use(srv)
    threads = []
    original = model_warmup.start_for_run

    def spy(*a, **kw):
        t = original(*a, **kw)
        threads.append((t, kw.get("is_resume")))
        return t

    model_warmup.start_for_run = spy
    try:
        with Session(engine) as s:
            events = list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "fr-FR"))
            run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
            s.expire_all()
            status = s.get(WorkflowRun, run_id).status
    finally:
        model_warmup.start_for_run = original
    for t, _ in threads:
        if t is not None:
            t.join(10)
    return status, threads


def test_seam_both_loops_and_failure_isolation():
    for mode in (TransMode.SEGMENTED, TransMode.FULL_TEXT):
        srv = Server({"m1": "unloaded", "m2": "unloaded"})
        status, threads = _run(mode, srv)
        assert status == RunStatus.COMPLETED, (mode, status)
        assert len(threads) == 1 and threads[0][0] is not None, (mode, threads)
        assert srv.loads() == ["m1", "m2"], (mode, srv.loads())
        for explode in (True,):
            bad = Server({"m1": "unloaded"}, explode=explode)
            status, threads = _run(mode, bad)
            assert status == RunStatus.COMPLETED, ("failing warm-up changed the run", mode, status)
            assert bad.requests, "the warm-up should have tried"
    print("  seam: both run loops warm once; a failing warm-up leaves the run COMPLETED OK")


def test_one_load_timeout_does_not_skip_the_rest():
    """LOW-1: a ReadTimeout on the first load request must not skip the engine's other models."""
    p = profile()
    srv = Server({"a": "unloaded", "b": "unloaded", "c": "unloaded"}, timeout_loads=("a",))
    use(srv)
    done = warm([step(ref(p), m) for m in ("a", "b", "c")])
    assert srv.loads() == ["a", "b", "c"], srv.loads()
    outcomes = {d[1]: d[2] for d in done}
    assert outcomes == {"a": "error:ReadTimeout", "b": "load-requested", "c": "load-requested"}, (
        outcomes
    )
    print("  a load timeout on one model still warms the engine's other models OK")


def test_hung_engine_does_not_delay_the_run():
    """LOW-5: an engine that never answers must not hold the run back; the warm-up is a
    background thread. The run COMPLETES while the transport is still blocked."""
    import threading

    gate = threading.Event()
    srv = Server({"m1": "unloaded", "m2": "unloaded"}, gate=gate)
    p = profile()
    wf_id, doc_id = _seed(TransMode.SEGMENTED, [("mock", ref(p), "m1"), ("mock", ref(p), "m2")])
    use(srv)
    threads = []
    original = model_warmup.start_for_run

    def spy(*a, **kw):
        t = original(*a, **kw)
        threads.append(t)
        return t

    model_warmup.start_for_run = spy
    try:
        with Session(engine) as s:
            events = list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "fr-FR"))
            run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
            s.expire_all()
            status = s.get(WorkflowRun, run_id).status
        assert status == RunStatus.COMPLETED, status
        assert threads and threads[0] is not None and threads[0].is_alive(), (
            "warm-up should still be blocked on the hung engine when the run completes"
        )
    finally:
        model_warmup.start_for_run = original
        gate.set()
        for t in threads:
            if t is not None:
                t.join(10)
    print("  a hung engine does not delay the run OK")


def main():
    test_one_load_timeout_does_not_skip_the_rest()
    test_hung_engine_does_not_delay_the_run()
    test_two_models_in_step_order()
    test_dedupe_and_blank_model_uses_profile_model()
    test_fallback_qe_and_autofix_refs_collected_in_order()
    test_cloud_and_builtin_skipped()
    test_cap_of_three()
    test_loaded_loading_not_reloaded_and_unlisted_skipped()
    test_non_router_and_404_skipped()
    test_key_only_to_own_origin()
    test_switch_off_and_resume_send_nothing()
    test_seam_both_loops_and_failure_isolation()
    print("verify_model_warmup OK")


if __name__ == "__main__":
    main()
