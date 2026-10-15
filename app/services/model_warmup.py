"""Model warm-up: ask a router-mode local server to preload the models a run will use.

GitHub #341. A llama.cpp server in ROUTER mode loads a model on the first request that
names it (10-30 s for a 12-31B model) and keeps up to `--models-max` (3 on a typical router) loaded.
A workflow whose steps use different models (translate on one, LQA on another) therefore
stalls at the step boundary where the second model is first requested. At run start this
module collects the (engine, model) pairs the workflow will use and, in a daemon thread,
sends `POST {server}/models/load` for the ones the server reports as not loaded.

BEST EFFORT, NEVER LOAD-BEARING. The warm-up runs on its own thread with its own client, is
capped at MAX_WARM_MODELS models, swallows every failure (logged at INFO) and never touches
the run: no run row write, no wait, no exception on the caller. A non-router server (no
`status` object in /v1/models, or a 404 on /models/load) is skipped without a word. The
engine's stored key rides only to that engine's own origin, through the same
`local_engine_client_kwargs` door the real engine calls use (egress validation + tailnet
SOCKS proxy).

SWITCH: the global `local_model_warmup` setting (default on). Off = nothing is sent.

Documented in: docs/architecture/engines-and-providers.md
"""

import logging
import threading

import httpx

from app import config
from app.models import StepType

log = logging.getLogger("loc_tms.model_warmup")

#: The most models warmed per run. A typical router holds 3 (`--models-max 3`); asking for a
#: fourth would evict one the run is about to use.
MAX_WARM_MODELS = 3

#: Per-request budget. `models/load` returns at once (loading is asynchronous), so this only
#: bounds a dead or slow host; the thread is a daemon and never blocks a run.
WARM_TIMEOUT_S = 8

#: Test hook, like `_transport` on the providers: an httpx transport used INSTEAD of the
#: network (and instead of the tailnet proxy), so the wire shape is verifiable offline.
_transport = None

# The step types that never call an engine (their provider column is meaningless).
_NO_ENGINE_STEPS = (StepType.HUMAN_REVIEW, StepType.TM_MATCH, StepType.EXTERNAL)

_OFF_VALUES = ("0", "false", "off", "no", "")


def enabled() -> bool:
    """The `local_model_warmup` switch (default on)."""
    from app.services import settings

    try:
        raw = settings.get("local_model_warmup", "1")
    except Exception:
        return False
    return str(raw if raw is not None else "1").strip().lower() not in _OFF_VALUES


def _step_refs(step) -> list[tuple[str, str]]:
    """(provider ref, model) pairs one step will call, in use order, resolved by the SAME
    helpers the orchestrator uses at run time (blank provider -> install default, blank
    model -> the engine profile's own model, applied later in `_engine_pair`)."""
    from app.services import qe_autofix
    from app.services.orchestrator_part2 import _qe_spec_for_step, _spec_for_step

    if getattr(step, "step_type", None) in _NO_ENGINE_STEPS:
        return []
    out: list[tuple[str, str]] = []
    spec = _spec_for_step(step)
    out.append((spec.provider or "", spec.model or ""))
    if step.step_type == StepType.LQA:
        return out
    if spec.fallback_provider:
        out.append((spec.fallback_provider, spec.fallback_model or ""))
    if getattr(step, "qe_gate", False):
        qe = _qe_spec_for_step(step)
        out.append((qe.provider or "", qe.model or ""))
        if getattr(step, "qe_autofix", False):
            fixer, _reason = qe_autofix.fixer_spec(step)
            if fixer is not None:
                out.append((fixer.provider or "", fixer.model or ""))
    return out


def collect_targets(steps) -> list[tuple[int, str]]:
    """Distinct (engine profile id, model) pairs of kind `local` the steps will use,
    ordered by first use (step order) and cut at MAX_WARM_MODELS. Only `engine:<id>`
    refs qualify: cloud kinds, built-in slots and registered test providers are skipped."""
    from app.services import engines
    from app.services.providers_hub import _OVERRIDES

    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for step in steps or []:
        for ref, model in _step_refs(step):
            ref = (ref or "").strip().lower()
            if ref in _OVERRIDES:
                continue  # a hot-swapped provider is not an HTTP server
            pid = engines._parse_engine_ref(ref)
            if pid is None:
                continue
            prof = engines.get_profile(pid)
            if prof is None or not prof.enabled or prof.kind != "local":
                continue
            model = (model or "").strip() or (prof.model or "").strip()
            if not model or (pid, model) in seen:
                continue
            seen.add((pid, model))
            out.append((pid, model))
            if len(out) >= MAX_WARM_MODELS:
                return out
    return out


def _client(prof, root: str) -> httpx.Client:
    """The SAME client construction the engine probes use: egress validation, then the
    tailnet SOCKS proxy for a tailnet host. No redirects, so the key cannot follow a 30x
    to another origin."""
    from app.services.providers import local_engine_client_kwargs

    kwargs = local_engine_client_kwargs(root, timeout=WARM_TIMEOUT_S, transport=_transport)
    kwargs["follow_redirects"] = False
    return httpx.Client(**kwargs)


def _root_and_key(prof) -> tuple[str, str]:
    from app.services.providers import _normalize_local_base
    from app.services.providers_hub import _provider_for_profile

    # The provider the real calls use already binds the profile's base URL and its key
    # (the decrypted key, or the slot key ONLY at the slot's own origin): reuse that
    # resolution rather than repeating it here.
    bound = _provider_for_profile(prof)
    base = _normalize_local_base(bound._profile_base or config.LOCAL_BASE_URL)
    root = base[: -len("/v1")] if base.lower().endswith("/v1") else base
    return root, bound._profile_key or "lm-studio"


def _status_of(payload, model: str) -> str | None:
    """The router `status.value` of `model` in a /v1/models body, or None when the server
    is not in router mode (no `status` object on the entry) or does not list the model."""
    data = payload.get("data") if isinstance(payload, dict) else None
    for entry in data or []:
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        status = entry.get("status")
        if isinstance(status, dict):
            return str(status.get("value") or "")
        return None
    return None


def _warm_engine(pid: int, models: list[str]) -> list[tuple[str, str]]:
    """Warm `models` on one engine. Returns [(model, outcome)] for the models it acted on
    or found already warm; a non-router server returns []."""
    from app.services import engines

    prof = engines.get_profile(pid)
    if prof is None:
        return []
    root, key = _root_and_key(prof)
    if not root:
        return []  # local engine not configured (empty base URL): never probe an empty URL
    headers = {"Authorization": f"Bearer {key}"}
    results: list[tuple[str, str]] = []
    with _client(prof, root) as c:
        r = c.get("/v1/models", headers=headers)
        if r.status_code != 200:
            return []
        try:
            payload = r.json()
        except ValueError:
            return []
        for model in models:
            status = _status_of(payload, model)
            if status is None:
                continue  # not router mode, or the model is unknown to it
            if status in ("loaded", "loading"):
                results.append((model, status))
                continue
            try:
                lr = c.post("/models/load", headers=headers, json={"model": model})
            except Exception as exc:
                # One slow/failed load request must not skip the engine's other models.
                results.append((model, f"error:{type(exc).__name__}"))
                continue
            if lr.status_code == 404:
                return results  # no router API on this server: skip quietly
            results.append((model, "load-requested" if lr.status_code < 300 else "refused"))
    return results


def warm_targets(targets: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Synchronous body of the warm-up (the thread target; tests call it directly).
    One INFO line per model acted on. Never raises."""
    by_engine: dict[int, list[str]] = {}
    for pid, model in targets[:MAX_WARM_MODELS]:
        by_engine.setdefault(pid, []).append(model)
    done: list[tuple[int, str, str]] = []
    for pid, models in by_engine.items():
        try:
            for model, outcome in _warm_engine(pid, models):
                log.info("model warm-up: engine:%s model=%s -> %s", pid, model, outcome)
                done.append((pid, model, outcome))
        except Exception as exc:
            log.info("model warm-up: engine:%s skipped (%s)", pid, type(exc).__name__)
    return done


def start_for_run(steps, *, is_resume: bool = False, run_id=None) -> threading.Thread | None:
    """The run-start hook. Snapshot the targets from `steps` on the caller's thread (plain
    attribute reads), then warm them on a daemon thread. Returns the thread (or None when
    nothing started) so a test can join it. A resumed run does not re-warm. NEVER raises."""
    if is_resume:
        return None
    try:
        if not enabled():
            return None
        targets = collect_targets(steps)
        if not targets:
            return None
        thread = threading.Thread(
            target=warm_targets,
            args=(targets,),
            name=f"model-warmup-run-{run_id}",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception as exc:
        log.info("model warm-up not started (%s)", type(exc).__name__)
        return None
