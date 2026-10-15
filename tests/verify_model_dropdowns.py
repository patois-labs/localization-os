"""Model-dropdowns-everywhere verification (offline, no real keys) — the unified
provider model-listing seam + its two new surfaces (workflow-builder route + CLI)
and the reworked two-stage cloud Test.

Covered:
  - providers.list_models_for_ref dispatch for every ref shape:
      * claude/gemini/openai -> cloud_list_models (transport-injected, configured key)
      * local                -> local_list_models(config.LOCAL_BASE_URL, LOCAL_API_KEY)
      * engine:<id> local/openai_compatible -> local_list_models(base, dec_key(...))
      * engine:<id> foreign-org -> []   (ownership gate)
      * engine:<id> disabled    -> []
      * engine:<id> non-listable kind (deepl) -> []
      * "" / unknown / bad engine id -> []
    and it NEVER accepts a caller base_url/api_key (signature has neither) and never
    raises (fail-soft []).
  - POST /workflows/models: returns {"models":[...]}, org-scoped, forwards NO caller
    base_url/api_key, and is gated EXACTLY like add_step (_ai_block: translator 403,
    admin 200).
  - POST /setup-ai/test two-stage: keyless->mock (text+live False), key-missing (stage
    1 empty) -> "API key missing or invalid", key-valid+quota-fail -> distinct
    "key valid ... quota exhausted" message, happy path -> "Connection OK ... <text>".
  - POST /setup-ai/local/test with a blank model: unresolvable (unreachable/no
    loaded models) -> clean "No model selected" message; REACHABLE server that
    reports loaded models -> still tests against the first listed model (fix for
    task_cca6bb89's polish item: the real connectivity test is never weakened by
    the blank-model guard, only the truly-unresolvable case short-circuits).
  - CLI `providers models --provider <ref>`: one id per line + exit 0 on hits; a clean
    message + exit 1 when empty.

Fail-pre-fix proof: `list_models_for_ref`, the `/workflows/models` route, and the
`providers` CLI group are ALL new — importing the symbol / hitting the route / running
the command errors on pre-fix code (proven for the symbol by the import below).
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_test_")

import httpx
import httpx2  # anthropic 1.x speaks httpx2
from fastapi.testclient import TestClient
from sqlmodel import Session as _Session

import app.web.admin as admin_mod
import app.web.workflows as wf_mod
from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine as _db_engine
from app.db import init_db
from app.main import app
from app.services import access, engines, security, settings
from app.services import providers as providers_mod

# NEW symbol — importing it FAILS on pre-fix code (ImportError) => fail-pre-fix proof.
from app.services.providers import list_models_for_ref
from tests._csrf import csrf_for, sign_out

# #390 (ai-02): the value an engine server echoes back as a model id when it echoes the
# bearer it was sent (the saved engine key). The caller passes it as the engine's key so
# the guard matches the ACTUAL key sent; non-secret-shaped on purpose.
ECHO_VAL = "echoed-engine-sentinel-7"


def _echo_local_transport():
    """Mock /models transport for local/openai_compatible discovery: it echoes the
    Authorization bearer back as the first model id (the #390 attack) plus one real id.

    A test transport injected through the real discovery path (engines.list_models /
    local_list_models) — the guard is the real drop_credential_ids check, not a stub."""

    def h(req):
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        return httpx.Response(200, json={"data": [{"id": token}, {"id": "gpt-real-1"}]})

    return httpx.MockTransport(h)


def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _anthropic_transport():
    def h(req):
        if req.url.path.endswith("/v1/models"):
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {"type": "model", "id": "claude-opus-4-8"},
                        {"type": "model", "id": "claude-sonnet-4-6"},
                    ],
                    "has_more": False,
                    "first_id": "a",
                    "last_id": "b",
                },
            )
        return httpx2.Response(404, json={})

    return httpx2.MockTransport(h)


def _cli(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _make_org(name):
    with _Session(_db_engine) as s:
        return access.create_org(s, name).id


def test_list_models_for_ref_dispatch():
    org7 = _make_org("dispatch-org")
    # --- cloud refs: dispatch to cloud_list_models with the CONFIGURED key + injected
    #     transport (no caller key/base_url is ever passed through this seam).
    config.ANTHROPIC_API_KEY = "sk-configured"
    try:
        got = list_models_for_ref("claude", transport=_anthropic_transport())
        assert got == ["claude-opus-4-8", "claude-sonnet-4-6"], got
        assert list_models_for_ref("CLAUDE", transport=_anthropic_transport()) == got, (
            "ref is case-insensitive"
        )
    finally:
        config.ANTHROPIC_API_KEY = ""
    # keyless cloud -> [] (no configured key), never raises
    assert list_models_for_ref("gemini") == []
    assert list_models_for_ref("openai") == []

    # --- "local" ref -> local_list_models(config.LOCAL_BASE_URL, config.LOCAL_API_KEY)
    captured = {}

    def _fake_local(base_url=None, api_key=None):
        captured["local"] = (base_url, api_key)
        return ["qwen3", "glm-4"]

    orig_local = providers_mod.local_list_models
    set_pieces("app.services.providers", "local_list_models", _fake_local)
    config.LOCAL_BASE_URL = "http://192.168.0.5:1234"
    config.LOCAL_API_KEY = "lm-studio"
    try:
        got = list_models_for_ref("local")
        assert got == ["qwen3", "glm-4"], got
        assert captured["local"] == ("http://192.168.0.5:1234", "lm-studio"), captured["local"]
        print("  list_models_for_ref: cloud (transport) + local dispatch OK")

        # --- engine:<id> openai_compatible -> local_list_models(base, DECRYPTED key)
        prof = engines.create_profile(
            "oai-host",
            "openai_compatible",
            base_url="https://openrouter.ai/api",
            api_key="secret-key-123",
            model="x",
            org_id=org7,
        )
        captured.pop("local", None)
        got = list_models_for_ref("engine:%d" % prof.id, org_id=org7)  # noqa: UP031
        assert got == ["qwen3", "glm-4"], got
        assert captured["local"] == ("https://openrouter.ai/api", "secret-key-123"), (
            "engine base_url + DECRYPTED key must be passed: {!r}".format(captured["local"])
        )

        # --- ownership gate: a foreign org sees [] (no network call attempted)
        captured.pop("local", None)
        assert list_models_for_ref("engine:%d" % prof.id, org_id=999) == [], (  # noqa: UP031
            "foreign org must be []"
        )
        assert "local" not in captured, "foreign-org ref must not reach local_list_models"

        # --- org_id=None: gate off, still lists
        assert list_models_for_ref("engine:%d" % prof.id, org_id=None) == ["qwen3", "glm-4"]  # noqa: UP031

        # --- disabled engine -> []
        dis = engines.create_profile(
            "oai-off",
            "openai_compatible",
            base_url="https://x",
            api_key="k",
            enabled=False,
            org_id=org7,
        )
        captured.pop("local", None)
        assert list_models_for_ref("engine:%d" % dis.id, org_id=org7) == [], "disabled -> []"  # noqa: UP031
        assert "local" not in captured, "disabled ref must not reach local_list_models"

        # --- non-listable kind (deepl) -> [] (no /models endpoint)
        deepl = engines.create_profile("my-deepl", "deepl", api_key="k", org_id=org7)
        captured.pop("local", None)
        assert list_models_for_ref("engine:%d" % deepl.id, org_id=org7) == [], "deepl -> []"  # noqa: UP031
        assert "local" not in captured, "non-listable kind must not reach local_list_models"
        print(
            "  list_models_for_ref: engine local/openai_compatible + ownership/"
            "enabled/kind gates OK"
        )
    finally:
        set_pieces("app.services.providers", "local_list_models", orig_local)
        config.LOCAL_BASE_URL = ""

    # --- "" / unknown / bad engine id / undefined -> [] (never raises)
    assert list_models_for_ref("") == []
    assert list_models_for_ref("bogus") == []
    assert list_models_for_ref("engine:notanint") == []
    assert list_models_for_ref("engine:99999") == []
    assert list_models_for_ref(None) == []  # type: ignore[arg-type]
    print("  list_models_for_ref: blank/unknown/bad-id -> [] (fail-soft, never raises)")


def test_local_discovery_drops_echoed_key():
    """#390 (ai-02): a local/openai_compatible engine whose /models server echoes the
    bearer it received (the saved key) back as a model id must NOT leak it through
    discovery. Exercises the real path (engines.list_models -> local_list_models_result)
    with an injected echo transport; FAILS pre-fix (no guard existed)."""
    prof = engines.create_profile(
        "echo-local",
        "openai_compatible",
        base_url="http://127.0.0.1:18765/v1",
        api_key=ECHO_VAL,
        model="x",
    )
    res = engines.list_models(prof.id, transport=_echo_local_transport())
    assert isinstance(res, dict), res
    assert ECHO_VAL not in res["models"], f"saved key leaked via engines.list_models: {res}"
    assert res["models"] == ["gpt-real-1"], res

    # The lower seam (setup-ai local fetch path) drops the echoing id the same way.
    listed = providers_mod.local_list_models(
        "http://127.0.0.1:18765/v1", ECHO_VAL, transport=_echo_local_transport()
    )
    assert listed == ["gpt-real-1"], listed

    # Review gate: a NON-string id (list/object) cannot be substring-checked, and served
    # as-is it would carry the key past the guard; it is dropped outright.
    def _shaped(req):
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        rows = [{"id": [token]}, {"id": {"k": token}}, {"id": "gpt-real-1"}]
        return httpx.Response(200, json={"data": rows})

    res = engines.list_models(prof.id, transport=httpx.MockTransport(_shaped))
    assert res["models"] == ["gpt-real-1"], res
    assert ECHO_VAL not in repr(res), res
    print("  #390 local/openai_compatible discovery drops an id echoing the saved key")


def test_workflow_route_echo_dropped(c):
    """#390 (ai-02), authenticated consumer: a linguist (can_tune_ai) hitting
    POST /workflows/models for an engine ref must not receive the engine's saved key
    even when the engine server echoes the bearer it was sent as the model id. The
    guarded httpx transport's inner HTTPTransport is intercepted so this runs offline.
    FAILS pre-fix (the key rode straight through)."""
    assert _login(c, "linguist", "linguist").status_code == 303
    prof = engines.create_profile(
        "echo-route",
        "local",
        base_url="http://127.0.0.1:18765/v1",
        api_key=ECHO_VAL,
        model="x",
    )

    def _wire(request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return httpx.Response(200, json={"data": [{"id": token}]})

    old = httpx.HTTPTransport.handle_request
    httpx.HTTPTransport.handle_request = lambda self, req: _wire(req)
    try:
        r = c.post("/workflows/models", data={"provider": f"engine:{prof.id}"})
    finally:
        httpx.HTTPTransport.handle_request = old
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert ECHO_VAL not in r.text, f"linguist received the saved engine key: {r.text[:200]}"
    print("  #390 POST /workflows/models never returns a saved engine key to a linguist")


def test_workflows_models_route(c):
    # The route forwards to list_models_for_ref(provider, <caller org>) and forwards NO
    # caller base_url/api_key. Monkeypatch the seam to capture the exact args (proves
    # org scoping + the no-caller-credential guard hermetically).
    seen = {}

    def _fake_seam(provider_ref, org_id=None, *, transport=None):
        seen["args"] = (provider_ref, org_id)
        seen["kwargs"] = {"transport": transport}
        return ["model-a", "model-b"]

    orig = wf_mod.list_models_for_ref
    set_pieces("app.web.workflows", "list_models_for_ref", _fake_seam)
    try:
        # Even when a caller SMUGGLES base_url/api_key form fields, the route ignores
        # them (its signature has only `provider`) — the seam is called with just the
        # provider + the caller's org, nothing else.
        r = c.post(
            "/workflows/models",
            data={
                "provider": "claude",
                "base_url": "https://evil.example",
                "api_key": "sk-attacker",
            },
        )
        assert r.status_code == 200, r.status_code
        assert r.json() == {"models": ["model-a", "model-b"]}, r.json()
        assert seen["args"][0] == "claude", seen["args"]
        # No base_url/api_key was forwarded (the seam has no such parameters).
        assert seen["kwargs"] == {"transport": None}, seen["kwargs"]
        print(
            "  POST /workflows/models returns {'models':[...]}; forwards NO caller base_url/api_key"
        )
    finally:
        set_pieces("app.web.workflows", "list_models_for_ref", orig)


def test_workflows_models_auth(c_authed):
    # Gated EXACTLY like add_step (_ai_block): a translator (can_tune_ai False) is 403,
    # an admin is 200 — i.e. the route rejects precisely whoever add_step rejects.
    gc = c_authed
    assert _login(gc, "user1", "user1").status_code == 303
    r = gc.post("/workflows/models", data={"provider": "claude"}, follow_redirects=False)
    assert r.status_code == 403, f"translator must be blocked, got {r.status_code}"
    sign_out(gc, follow_redirects=False)
    assert _login(gc, "admin", "admin").status_code == 303
    r = gc.post("/workflows/models", data={"provider": "claude"}, follow_redirects=False)
    assert r.status_code == 200 and "models" in r.json(), (r.status_code, r.text[:200])
    print("  POST /workflows/models is _ai_block-gated (translator 403, admin 200)")


def test_two_stage_cloud_test(c):
    # --- keyless path: provider not live -> the offline MOCK. Legacy shape preserved
    #     (text present, live False) so the tile still demos the mock translating.
    d = c.post("/setup-ai/test", data={"provider": "claude"}).json()
    assert d["live"] is False and d["text"], d
    assert d["ok"] is True and "mock" in d["message"], d
    print("  /setup-ai/test keyless -> mock (text + live False)")

    orig_live = admin_mod.provider_is_live
    orig_list = admin_mod.cloud_list_models
    orig_run = admin_mod.run_completion
    try:
        set_pieces(
            "app.web.admin", "provider_is_live", lambda p: True
        )  # pretend a key is configured

        # STAGE 1 fails: the key is missing/invalid (no models visible).
        set_pieces("app.web.admin", "cloud_list_models", lambda p, *a, **k: [])
        d = c.post("/setup-ai/test", data={"provider": "openai"}).json()
        assert d["ok"] is False and d["stage"] == "list", d
        assert "API key missing or invalid" in d["message"], d["message"]

        # STAGE 1 ok, STAGE 2 fails with a QUOTA marker -> distinct "key valid" message.
        set_pieces(
            "app.web.admin", "cloud_list_models", lambda p, *a, **k: ["gpt-4o", "gpt-4o-mini"]
        )
        set_pieces(
            "app.web.admin",
            "run_completion",
            lambda *a, **k: {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "error": "RateLimitError: 429 insufficient_quota - no credit",
                "usage": None,
                "failover": False,
            },
        )
        d = c.post("/setup-ai/test", data={"provider": "openai"}).json()
        assert d["ok"] is False and d.get("key_valid") is True and d.get("quota") is True, d
        assert d["models_visible"] == 2, d
        assert "key valid" in d["message"] and "quota exhausted" in d["message"], d["message"]
        print(
            "  /setup-ai/test key-valid + quota-fail -> distinct 'key valid ... "
            "quota exhausted' message"
        )

        # Happy path: STAGE 1 ok + STAGE 2 succeeds.
        set_pieces("app.web.admin", "cloud_list_models", lambda p, *a, **k: ["claude-opus-4-8"])
        set_pieces(
            "app.web.admin",
            "run_completion",
            lambda *a, **k: {
                "text": "Bonjour",
                "provider_used": "claude",
                "model_used": "claude-opus-4-8",
                "attempts": 1,
                "error": None,
                "usage": None,
                "failover": False,
            },
        )
        d = c.post("/setup-ai/test", data={"provider": "claude"}).json()
        assert d["ok"] is True and "Connection OK" in d["message"] and "Bonjour" in d["message"], d
        print("  /setup-ai/test happy path -> 'Connection OK (N models visible) - <text>'")
    finally:
        set_pieces("app.web.admin", "provider_is_live", orig_live)
        set_pieces("app.web.admin", "cloud_list_models", orig_list)
        set_pieces("app.web.admin", "run_completion", orig_run)


def test_local_test_blank_model_unresolvable(c):
    """Blank model + an unreachable server (no loaded models to fall back to) ->
    the clean, actionable "No model selected" message -- never local_complete's
    raw literal-"local-model" fallback (LM Studio's confusing model_not_found)."""
    d = c.post(
        "/setup-ai/local/test",
        data={"base_url": "http://x:1234", "api_key": "lm-studio", "local_model": ""},
    ).json()
    assert d["ok"] is False, d
    assert "No model selected" in d["error"] and "pick one" in d["error"], d
    print("  /setup-ai/local/test blank model, unresolvable -> clean 'No model selected' message")


def test_local_test_blank_model_reachable_defaults(c):
    """Blank model + a REACHABLE server that reports loaded models: the real
    connectivity test still runs, against the FIRST listed model, rather than an
    unconditional block -- the "do not weaken the real connectivity test" half of
    task_cca6bb89's fix. resolve_default_model("local") never supplies a default
    itself (an engine's own saved model is the only correct default, by design --
    see its docstring), so the route falls through to local_list_models next."""
    orig_list, orig_complete = admin_mod.local_list_models, admin_mod.local_complete
    seen = {}

    def _fake_list(base_url=None, api_key=None):
        return ["qwen3.5-9b-distilled"]

    def _fake_complete(base_url, api_key, model, prompt, system=None, **kw):
        seen["model"] = model
        return "Bonjour le monde"

    set_pieces("app.web.admin", "local_list_models", _fake_list)
    set_pieces("app.web.admin", "local_complete", _fake_complete)
    try:
        d = c.post(
            "/setup-ai/local/test",
            data={"base_url": "http://192.168.0.5:1234", "api_key": "lm-studio", "local_model": ""},
        ).json()
        assert d["ok"] is True and d["text"] == "Bonjour le monde", d
        assert seen["model"] == "qwen3.5-9b-distilled", (
            f"must test against the first listed model, got {seen}"
        )
        print(
            "  /setup-ai/local/test blank model, REACHABLE server -> tests against the "
            "first listed model instead of blocking"
        )
    finally:
        set_pieces("app.web.admin", "local_list_models", orig_list)
        set_pieces("app.web.admin", "local_complete", orig_complete)


def test_cli_providers_models():
    orig = providers_mod.list_models_for_ref
    try:
        # happy: one id per line, exit 0
        set_pieces(
            "app.services.providers",
            "list_models_for_ref",
            lambda ref, org_id=None, **k: ["m1", "m2"],
        )
        code, out, _ = _cli(["providers", "models", "--provider", "claude"])
        assert code == 0, (code, out)
        assert out.splitlines() == ["m1", "m2"], out

        # empty: clean message on stderr + non-zero exit
        set_pieces(
            "app.services.providers", "list_models_for_ref", lambda ref, org_id=None, **k: []
        )
        code, out, err = _cli(["providers", "models", "--provider", "deepl-ish"])
        assert code == 1, (code, out, err)
        assert "no models available" in err, err
        assert out.strip() == "", out  # nothing on stdout when empty
        print("  CLI `providers models`: one id per line + exit 0; empty -> msg + exit 1")
    finally:
        set_pieces("app.services.providers", "list_models_for_ref", orig)


def main() -> None:
    init_db()
    c = TestClient(app)

    test_list_models_for_ref_dispatch()
    test_local_discovery_drops_echoed_key()
    test_workflows_models_route(c)
    test_two_stage_cloud_test(c)
    test_local_test_blank_model_unresolvable(c)
    test_local_test_blank_model_reachable_defaults(c)
    test_cli_providers_models()

    # Auth-gating LAST (login wall on) so it doesn't disturb the auth-off assertions.
    security.seed_initial()  # admin/admin, user1/user1 (translator); auth ON
    test_workflow_route_echo_dropped(TestClient(app))  # #390: linguist must not see the key
    test_workflows_models_auth(TestClient(app))
    settings.set_many({"security_auth_required": "0"})

    print("MODEL DROPDOWNS OK")


if __name__ == "__main__":
    main()
