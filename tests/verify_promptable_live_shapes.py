"""Promptable-setup LIVE-SHAPE regression suite -- the lens the original hermetic
fixtures were structurally BLIND to.

Two live failures slipped past the 124-suite hermetic run because every promptable-
setup test resolved `claude` to the keyless offline MockProvider (which needs no
model and never trips the empty-model guard):

  BUG 1 -- the authoring seam (`setup_author._author_json` -> `complete()`) passed
  NO model. With a REAL cloud key configured but DEFAULT_<X>_MODEL empty, the
  provider's empty-model pre-flight guard fired ("no model configured for provider
  'claude' ...") and there was no way for the operator to pick a model. The older
  `orchestrator.author_prompt` seam had the identical hole.

  BUG 2 -- the workflow "Describe a workflow" full-page route surfaced that SAME
  empty-model failure as a full-page error every time (the project dialog showed it
  inline via JSON; the workflow route renders a page), so it read as a hard failure
  "no matter what".

The fix threads a MODEL end to end (UI dropdowns -> routes -> services -> complete),
resolving a sensible cloud default server-side (providers.resolve_default_model) so a
keyed provider with an empty DEFAULT_<X>_MODEL still gets a real model, and only falls
back to the provider's clean guard error when nothing is resolvable.

This suite encodes the missing lens:
  * seed a dummy cloud key via the in-app cloud_keys seam INSIDE the throwaway data
    dir (the pytest wrapper BLANKS the env keys, so a stored key is the only way to
    make `claude` resolve to the REAL client-with-guard offline), then assert the
    authoring path either PASSES a model or returns the clean actionable error;
  * drive /projects/author.json + /workflows/author + confirm through the app factory
    the way the QA server boots it (auth ON), asserting a model threads through and
    the flows no longer error when a model is supplied.

Offline + hermetic: `claude` is resolved either to the REAL ClaudeProvider (whose
empty-model guard fires BEFORE any network call) or to a purpose-built test double
that MIRRORS that guard (providers._require_model) and then returns canned JSON -- no
API key material, no network, ever. ASCII-only output (Windows console).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_liveshapes_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine as db_engine
from app.db import init_db
from app.models import Workflow, WorkflowStep
from app.services import (
    cloud_keys,
    content,
    orchestrator,
    providers,
    security,
    setup_author,
)

init_db()


def _session():
    return Session(db_engine)


# A canned model response that satisfies BOTH the project and the workflow validators
# (each reads only the keys it needs): project -> name/target_locales; workflow ->
# name/target_locale/steps.
_COMBINED_JSON = (
    '{"name": "Live Draft", "customer": null, "source_locale": "en-US", '
    '"target_locales": ["es-ES"], "target_locale": "es-ES", '
    '"steps": [{"name": "Translate", "step_type": "llm_agent", '
    '"prompt_text": "Translate {text} to {target_locale}."}]}'
)


class _GuardClaude(providers.Provider):
    """A `claude` test double that MIRRORS ClaudeProvider's first line -- the same
    empty-model pre-flight guard (providers._require_model) -- then returns canned
    output. It records the model kwarg it received so a test can prove the seam
    THREADS a model. Registered via register_provider so get_provider('claude')
    resolves it (an override wins over the keyed/mock resolution), keeping the whole
    suite offline while exercising the real guard behaviour."""

    name = "claude"
    seen_models: list = []  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        # EXACTLY ClaudeProvider.complete's guard: model must resolve non-empty or the
        # NoModelConfigured guard raises BEFORE any (mock) work -- the offline stand-in
        # for "the empty-model guard fires before the network call".
        model = providers._require_model(
            "claude", kw.get("model") or config.DEFAULT_MODELS["claude"]
        )
        _GuardClaude.seen_models.append(model)
        if task == "author_prompt":
            return "Translate {text} to {target_locale}."
        return _COMBINED_JSON


# --- 1. unit: resolve_default_model ------------------------------------------
def check_resolve_default_model():
    saved = config.DEFAULT_MODELS.get("claude")
    try:
        # a caller pick ALWAYS wins (even over a configured default).
        config.DEFAULT_MODELS["claude"] = "claude-haiku-4-5"
        assert providers.resolve_default_model("claude", "opus-x", None) == "opus-x"
        # blank -> the configured default.
        assert providers.resolve_default_model("claude", "", None) == "claude-haiku-4-5"
        # blank + EMPTY default + NO key -> "" (the provider's clean guard fires; this
        # is the exact hole behind bug 1). list_models_for_ref needs a key -> [].
        config.DEFAULT_MODELS["claude"] = ""
        assert providers.resolve_default_model("claude", "", None) == "", (
            "a cloud provider with no default and no key must resolve to '' so the "
            "empty-model guard surfaces its clean message"
        )
        # an engine:<id> / local ref keeps a BLANK model (its own profile model wins) --
        # a built-in default must NEVER be forced onto it.
        assert providers.resolve_default_model("engine:7", "", None) == ""
        assert providers.resolve_default_model("local", "", None) == ""
    finally:
        config.DEFAULT_MODELS["claude"] = saved
    print(
        "  1. resolve_default_model: pick wins; blank->default; cloud empty+keyless->"
        "'' (clean guard); engine/local keep blank"
    )


# --- 2. the bug-1 lens: REAL client-with-guard via a stored dummy key --------
def check_real_client_guard_surfaces_clean():
    """With a stored (dummy) cloud key, `claude` resolves to the REAL ClaudeProvider
    (NOT the offline mock) -- the resolution the hermetic fixtures never reached. With
    DEFAULT_CLAUDE_MODEL empty and NO model threaded, author_project_setup surfaces the
    provider's clean empty-model guard message -- offline (the guard fires before any
    network call). This is the exact failure the maintainer saw; the assert pins that it stays a
    CLEAN, actionable SetupAuthorError, never a raw 500 or a silent mock success."""
    saved = config.DEFAULT_MODELS.get("claude")
    cloud_keys.set_key("claude", "sk-dummy-not-real-offline", actor="test")
    try:
        config.DEFAULT_MODELS["claude"] = ""  # simulate an install with no default model
        prov = providers.get_provider("claude")
        assert isinstance(prov, providers.ClaudeProvider), (
            "a stored key must resolve claude to the REAL client (bug-1 condition), got "
            + type(prov).__name__
        )
        assert not isinstance(prov, providers.MockProvider)
        with _session() as s:
            content.customer_create(s, "GuardCo")
        raised = False
        with _session() as s:
            try:
                setup_author.author_project_setup(
                    s,
                    description="en to es marketing",
                    provider="claude",
                    org_id=None,
                    actor="t",
                    model=None,
                )
            except setup_author.SetupAuthorError as exc:
                raised = True
                assert "no model configured" in str(exc), (
                    "the clean guard message must surface verbatim",
                    str(exc),
                )
                assert exc.status == 502, exc.status
        assert raised, (
            "with a real key, empty default model, and no model threaded, the seam "
            "must surface the clean empty-model guard error"
        )
    finally:
        config.DEFAULT_MODELS["claude"] = saved
        cloud_keys.remove_key("claude", actor="test")  # back to keyless for later checks
    print(
        "  2. bug-1 lens: a stored key resolves claude to the REAL guarded client; "
        "no model + empty default -> clean SetupAuthorError (never a raw 500), offline"
    )


# --- 3. the fix: a threaded MODEL reaches complete() (project seam) -----------
def check_model_threads_project():
    providers.register_provider("claude", _GuardClaude())  # override wins hereafter
    saved = config.DEFAULT_MODELS.get("claude")
    try:
        config.DEFAULT_MODELS["claude"] = ""  # only a THREADED model can pass the guard
        _GuardClaude.seen_models = []
        with _session() as s:
            out = setup_author.author_project_setup(
                s,
                description="en to es",
                provider="claude",
                org_id=None,
                actor="t",
                model="claude-haiku-4-5",
            )
        assert out["fields"]["name"] == "Live Draft", out["fields"]
        assert _GuardClaude.seen_models == ["claude-haiku-4-5"], (
            "the picked model must be THREADED to complete() (this is the fix; pre-fix "
            "no model was passed and the guard fired)",
            _GuardClaude.seen_models,
        )
        # no model + empty default -> the clean guard error (acceptable final fallback).
        _GuardClaude.seen_models = []
        raised = False
        with _session() as s:
            try:
                setup_author.author_project_setup(
                    s, description="x", provider="claude", org_id=None, actor="t", model=None
                )
            except setup_author.SetupAuthorError as exc:
                raised = True
                assert "no model configured" in str(exc), str(exc)
        assert raised and _GuardClaude.seen_models == [], (
            "no resolvable model -> clean guard error, no successful call"
        )
    finally:
        config.DEFAULT_MODELS["claude"] = saved
    print(
        "  3. fix (project seam): a picked model is THREADED to complete() -> draft; "
        "no resolvable model -> the clean guard error"
    )


# --- 4. the fix: workflow seam + the older author_prompt seam ----------------
def check_model_threads_workflow_and_author_prompt():
    saved = config.DEFAULT_MODELS.get("claude")
    try:
        config.DEFAULT_MODELS["claude"] = ""
        _GuardClaude.seen_models = []
        # workflow authoring threads the model.
        with _session() as s:
            out = setup_author.author_workflow_setup(
                s,
                description="en to es 1-step",
                provider="claude",
                org_id=None,
                actor="t",
                model="claude-haiku-4-5",
            )
        assert out["fields"]["steps"], "the workflow draft must carry a step"
        assert "claude-haiku-4-5" in _GuardClaude.seen_models, _GuardClaude.seen_models
        # the OLDER prompt-authoring seam threads the model too (same hole, fixed).
        _GuardClaude.seen_models = []
        draft = orchestrator.author_prompt(
            "a translator step", provider="claude", model="claude-haiku-4-5"
        )
        assert "{text}" in draft, draft
        assert _GuardClaude.seen_models == ["claude-haiku-4-5"], _GuardClaude.seen_models
        # and both surface the clean guard error when nothing resolves.
        raised = False
        try:
            orchestrator.author_prompt("x", provider="claude", model=None)
        except providers.ProviderCallError as exc:
            raised = True
            assert "no model configured" in str(exc), str(exc)
        assert raised, "author_prompt must surface the clean guard error too"
    finally:
        config.DEFAULT_MODELS["claude"] = saved
    print(
        "  4. fix (workflow + author_prompt seams): the model threads through both; "
        "no resolvable model -> the clean guard error on both"
    )


# --- 5b. BUG 5 lens: setup authoring REQUESTS a generous seeded output budget -----
def check_setup_author_requests_large_max_tokens():
    """BUG 5 regression (workflow authoring ALWAYS 502'd on real Claude): setup
    authoring OUTPUT (a full workflow JSON with per-step prompt_text) is much LARGER
    than its short INPUT description, but the provider sized max_tokens from the INPUT
    (_sized_max_tokens, a TRANSLATION heuristic where output ~= input) -> the draft
    truncated (stop_reason=max_tokens) -> ProviderTruncated -> 502.

    The fix routes setup_author._author_json through run_completion with a GENEROUS
    seeded starting budget (min(model ceiling, 16384)) that OVERRIDES the input-sized
    heuristic in every prompt-based provider -- and, unlike the old fixed budget, an
    at-ceiling truncation ESCALATES toward the ceiling instead of failing at a small
    cap. For this engine's model (local-model, output ceiling 8192) the seed clamps to
    8192 on the wire. MockProvider ignores max_tokens (canned JSON), so the ONLY way to
    prove the budget is actually
    REQUESTED is to inspect the OUTBOUND wire request of the REAL provider: an
    OpenAICompatibleProvider with an injected httpx.MockTransport (the same seam
    verify_prompt_authoring/the connector suites use). Pre-fix the body carried NO
    max_tokens (the openai_compatible complete sent none) -- so this assert fails on
    old code and passes on new. This is the test that would have caught the bug."""
    import httpx

    from app.services import engines
    from app.services.providers import OpenAICompatibleProvider, register_provider

    captured: dict = {}

    def handler(req):
        import json as _json

        captured["body"] = _json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _COMBINED_JSON}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 40},
            },
        )

    prof = engines.create_profile(
        "Bug5Local",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{prof.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=engines.get_profile(prof.id), transport=httpx.MockTransport(handler)
        ),
    )
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s,
            description="en to es one-step LQA workflow",
            provider=ref,
            org_id=None,
            actor="t",
            model=None,
        )
    assert out["fields"]["steps"], (
        "the workflow draft must carry a step (a full draft, not a truncated one)",
        out["fields"],
    )
    assert captured.get("body"), "the engine must have received an outbound request"
    expected_seed = min(config.model_output_ceiling("local-model"), 16384)  # 8192 here
    assert captured["body"].get("max_tokens") == expected_seed, (
        "setup authoring must REQUEST the generous seeded output budget "
        "(min(model ceiling, 16384)) on the wire, not the small input-sized heuristic "
        "that truncated the draft (BUG 5)",
        captured["body"].get("max_tokens"),
        expected_seed,
    )
    print(
        "  5b. BUG 5: setup authoring requests the seeded max_tokens "
        f"({expected_seed}) on the wire via run_completion (generous budget + "
        "escalation that stops the workflow-draft truncation)"
    )


# --- 5. bug-2 lens: the full web flow, AUTH ON (flips auth globally; run LAST) -
def check_web_author_with_model_auth_on():
    from tests._csrf import csrf_for

    security.seed_initial()  # auth wall ON + admin/admin (the maintainer's real-install posture)
    from app.main import app

    saved = config.DEFAULT_MODELS.get("claude")
    try:
        config.DEFAULT_MODELS["claude"] = ""  # only a threaded model passes the guard
        client = TestClient(app)
        csrf_for(client)
        r = client.post(
            "/auth/login",
            data={"email": "admin", "password": "admin", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("admin login", r.status_code)
        csrf_for(client)

        # (a) project author.json WITH a model -> 200 draft JSON (pre-fix: the model was
        # ignored, the guard fired, and this was a 502 error body).
        r = client.post(
            "/projects/author.json",
            data={"description": "en to es", "provider": "claude", "model": "claude-haiku-4-5"},
        )
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert r.json().get("ok") is True and r.json()["draft"]["name"] == "Live Draft"

        # (b) THE bug-2 discriminator: workflow "Describe a workflow" WITH a model ->
        # the review page renders (200), not a full-page error. Pre-fix, no model was
        # threaded, the guard fired, and this route returned the error page every time.
        r = client.post(
            "/workflows/author",
            data={
                "description": "en to es 1-step",
                "provider": "claude",
                "model": "claude-haiku-4-5",
            },
        )
        assert r.status_code == 200, (
            "workflow author with a model must render the review page, not an error",
            r.status_code,
        )
        assert "Review the drafted workflow" in r.text and "s0_prompt_text" in r.text

        # (c) confirm creates the workflow atomically + the redirect target renders.
        r = client.post(
            "/workflows/author/confirm",
            data={
                "step_count": "1",
                "name": "Live WF Confirmed",
                "customer_id": "",
                "source_locale": "en-US",
                "target_locales": ["es-ES"],
                "s0_step_type": "llm_agent",
                "s0_prompt_text": "Translate {text} to Spanish.",
                "s0_qe_threshold": "70",
                "s0_tm_examples_window": "3",
                "s0_tm_examples": "1",
                "notes_for_reviewer": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and "/workflows/" in r.headers["location"], (
            r.status_code,
            r.headers.get("location"),
        )
        with _session() as s:
            wf = s.exec(select(Workflow).where(Workflow.name == "Live WF Confirmed")).first()
            assert (
                wf is not None
                and len(s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)).all())
                == 1
            )
        # the builder page the confirm redirects to must render (no post-create 500).
        r = client.get(r.headers["location"], follow_redirects=True)
        assert r.status_code == 200, ("the post-confirm builder page must render", r.status_code)

        # (d) the acceptable final fallback: NO model + empty default -> a CLEAN error
        # page (502), never a raw 500 -- the workflow route still degrades gracefully.
        r = client.post("/workflows/author", data={"description": "en to es", "provider": "claude"})
        assert r.status_code == 502, (
            "no resolvable model -> a clean 502 error page, never a raw 500",
            r.status_code,
        )
        assert "no model configured" in r.text
    finally:
        config.DEFAULT_MODELS["claude"] = saved
    print(
        "  5. bug-2 lens (auth ON): /projects/author.json + /workflows/author + "
        "confirm thread a model and succeed end to end; no model -> a clean 502 page"
    )


def main() -> None:
    check_resolve_default_model()
    check_real_client_guard_surfaces_clean()
    check_model_threads_project()
    check_model_threads_workflow_and_author_prompt()
    check_setup_author_requests_large_max_tokens()  # BUG 5
    check_web_author_with_model_auth_on()  # LAST: flips auth ON globally
    print("PROMPTABLE LIVE SHAPES OK")


if __name__ == "__main__":
    main()
