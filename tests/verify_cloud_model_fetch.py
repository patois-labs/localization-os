"""Cloud provider model-discovery verification (offline, no real keys).

Mirrors verify_local.py for the three cloud LLM providers (claude / gemini / openai):
the Setup AI page pings the provider with the configured key, lists the models that
key can access (including OpenAI fine-tunes), and offers a PICK-list instead of a
free-text field.

Covered:
  - cloud_list_models parses model ids from a mocked Anthropic / OpenAI / Gemini
    /models response (via an injected httpx transport) — incl. a fine-tune id for
    OpenAI and the 'models/' prefix stripped for Gemini
  - it fails SOFT (returns [], never raises) on auth error, network error, no key,
    and an unknown provider name
  - it reads the CONFIGURED key from config.* when api_key is None
  - POST /setup-ai/cloud/models returns {"models": [...]}, 200s, and is _ai_block-gated
  - setup_ai_save still round-trips model_claude / model_gemini / model_openai
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import httpx
import httpx2  # anthropic 1.x speaks httpx2; its model-list transport double is an httpx2 one
from fastapi.testclient import TestClient

from app import config
from app.db import init_db
from app.main import app
from app.services import security, settings

# A NEW symbol: importing it FAILS on pre-fix code (ImportError) — fail-pre-fix proof.
from app.services.providers import cloud_list_models
from tests._csrf import csrf_for, sign_out

# #390 (ai-02): the value a mocked /models server echoes back as a model id (the caller
# passes it as the api_key so the guard matches the ACTUAL key sent). Non-secret-shaped
# on purpose; the guard matches by substring, not by a credential pattern.
ECHO_VAL = "echoed-server-sentinel-42"


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
                        {"type": "model", "id": "claude-haiku-4-5"},
                    ],
                    "has_more": False,
                    "first_id": "a",
                    "last_id": "b",
                },
            )
        return httpx2.Response(404, json={})

    return httpx2.MockTransport(h)


def _openai_transport():
    def h(req):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "gpt-4o-mini", "object": "model"},
                    {"id": "ft:gpt-3.5-turbo:acme::abc123", "object": "model"},
                ],
            },
        )

    return httpx.MockTransport(h)


def _gemini_transport():
    def h(req):
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.0-flash",
                        "supportedGenerationMethods": ["generateContent", "countTokens"],
                    },
                    {
                        "name": "models/gemini-1.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    # embedding-only model: no generateContent -> filtered out
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )

    return httpx.MockTransport(h)


def _raising_transport(exc):
    def h(req):
        raise exc

    return httpx.MockTransport(h)


def main() -> None:
    init_db()
    c = TestClient(app)

    # --- parsing per provider (transport injected; a key is supplied so we reach it)
    claude = cloud_list_models("claude", api_key="sk-test", transport=_anthropic_transport())
    assert claude == ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"], claude
    openai = cloud_list_models("openai", api_key="sk-test", transport=_openai_transport())
    assert openai == ["gpt-4o", "gpt-4o-mini", "ft:gpt-3.5-turbo:acme::abc123"], openai
    assert any(m.startswith("ft:") for m in openai), "fine-tune must be retained"
    gemini = cloud_list_models("gemini", api_key="k", transport=_gemini_transport())
    assert gemini == ["gemini-2.0-flash", "gemini-1.5-pro"], (
        gemini
    )  # prefix stripped, embed dropped
    print("  cloud_list_models parses ids for claude / openai (incl. fine-tune) / gemini")

    # --- fail soft: auth error (401), network error, no key, unknown provider
    err401 = _raising_transport(
        httpx.HTTPStatusError(
            "401", request=httpx.Request("GET", "https://x/v1/models"), response=httpx.Response(401)
        )
    )
    assert cloud_list_models("claude", api_key="bad", transport=err401) == []
    net = _raising_transport(httpx.ConnectError("boom"))
    assert cloud_list_models("openai", api_key="bad", transport=net) == []
    assert cloud_list_models("gemini", api_key="bad", transport=net) == []
    assert cloud_list_models("claude") == [], "no configured key -> [] (config keys blank)"
    assert cloud_list_models("openai", api_key="") == [], "blank key -> []"
    assert cloud_list_models("bogus", api_key="x") == [], "unknown provider -> []"
    print("  cloud_list_models fails soft ([], never raises) on auth/network/no-key/unknown")

    # --- reads the CONFIGURED key from config.* when api_key is None
    config.ANTHROPIC_API_KEY = "sk-configured"
    try:
        got = cloud_list_models("claude", transport=_anthropic_transport())
        assert got and got[0] == "claude-opus-4-8", got
        print("  cloud_list_models uses the configured config.* key when api_key is None")
    finally:
        config.ANTHROPIC_API_KEY = ""

    # --- SECURITY #390 (ai-02): a /models server that echoes the key it received back
    # as a model id must NOT hand that key to the discovery caller (Setup AI + the
    # workflow picker can be reached by users who may not read stored keys). The guard
    # drops the echoing id and keeps every real id. FAILS pre-fix (no guard existed).
    def _echo_anthropic():
        def h(req):
            if req.url.path.endswith("/v1/models"):
                return httpx2.Response(
                    200,
                    json={
                        "data": [
                            {"type": "model", "id": ECHO_VAL},
                            {"type": "model", "id": "claude-opus-4-8"},
                        ],
                        "has_more": False,
                        "first_id": "a",
                        "last_id": "b",
                    },
                )
            return httpx2.Response(404, json={})

        return httpx2.MockTransport(h)

    def _echo_openai():
        return httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": ECHO_VAL, "object": "model"},
                        {"id": "gpt-4o", "object": "model"},
                    ],
                },
            )
        )

    def _echo_gemini():
        return httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/" + ECHO_VAL,
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-2.0-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        )

    echo_claude = cloud_list_models("claude", api_key=ECHO_VAL, transport=_echo_anthropic())
    assert echo_claude == ["claude-opus-4-8"], echo_claude
    echo_openai = cloud_list_models("openai", api_key=ECHO_VAL, transport=_echo_openai())
    assert echo_openai == ["gpt-4o"], echo_openai
    echo_gemini = cloud_list_models("gemini", api_key=ECHO_VAL, transport=_echo_gemini())
    assert echo_gemini == ["gemini-2.0-flash"], echo_gemini
    assert all(ECHO_VAL not in m for m in echo_claude + echo_openai + echo_gemini), (
        "an id echoing the sent key must be dropped"
    )
    print("  #390 cloud discovery drops an id that echoes the sent key (claude/openai/gemini)")

    # --- SECURITY: a caller-supplied base_url must NOT redirect the configured cloud
    # key to an attacker host. base_url is ignored for cloud providers; the SDK's
    # OFFICIAL host is always used. (Pre-fix, base_url was threaded into the SDK, so
    # the configured ANTHROPIC key reached the attacker host -> this FAILS pre-fix.)
    captured = {"hosts": [], "keys_to_evil": []}
    EVIL = "https://evil.attacker.example"

    def _capturing(req):
        captured["hosts"].append(req.url.host)
        if req.url.host != "api.anthropic.com":
            captured["keys_to_evil"].append(req.headers.get("x-api-key"))
        return httpx2.Response(
            200,
            json={
                "data": [{"type": "model", "id": "claude-opus-4-8"}],
                "has_more": False,
                "first_id": "a",
                "last_id": "b",
            },
        )

    config.ANTHROPIC_API_KEY = "sk-secret-configured"
    try:
        out = cloud_list_models(
            "claude", api_key="", base_url=EVIL, transport=httpx2.MockTransport(_capturing)
        )
    finally:
        config.ANTHROPIC_API_KEY = ""
    assert out == ["claude-opus-4-8"], out
    assert captured["hosts"] == ["api.anthropic.com"], (
        f"configured key must go ONLY to the official host, saw {captured['hosts']}"
    )
    assert "evil.attacker.example" not in captured["hosts"], "key sent to attacker host!"
    assert captured["keys_to_evil"] == [], "configured key leaked to a non-official host!"
    print("  base_url override is IGNORED - configured cloud key only ever hits the official host")

    # --- route: returns {"models":[...]}, 200s (keyless -> [] cleanly, no 500)
    r = c.post("/setup-ai/cloud/models", data={"provider": "claude"})
    assert r.status_code == 200, r.status_code
    assert r.json() == {"models": []}, r.json()
    r = c.post(
        "/setup-ai/cloud/models", data={"provider": "openai", "api_key": "x", "base_url": ""}
    )
    assert r.status_code == 200 and "models" in r.json(), r.json()
    print("  POST /setup-ai/cloud/models returns {'models':[...]} and 200s (keyless -> [])")

    # --- setup_ai_save still round-trips the three cloud models
    r = c.post(
        "/setup-ai",
        data={
            "default_provider": "claude",
            "model_claude": "claude-opus-4-8",
            "model_gemini": "gemini-1.5-pro",
            "model_openai": "ft:gpt-3.5-turbo:acme::abc123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert config.DEFAULT_MODELS["claude"] == "claude-opus-4-8"
    assert config.DEFAULT_MODELS["gemini"] == "gemini-1.5-pro"
    assert config.DEFAULT_MODELS["openai"] == "ft:gpt-3.5-turbo:acme::abc123"
    assert settings.get("model_claude") == "claude-opus-4-8"
    page = c.get("/setup-ai").text
    assert "claude-opus-4-8" in page and "ft:gpt-3.5-turbo:acme::abc123" in page, (
        "saved cloud models should preselect in the dropdowns"
    )
    print("  setup_ai_save round-trips model_claude / model_gemini / model_openai")

    # reset so the process leaves no picked defaults behind
    settings.set_many(
        {"model_claude": "", "model_gemini": "", "model_openai": "", "default_provider": "claude"}
    )

    # --- route is _ai_block-gated (admins + linguists only). Auth ON via seed_initial;
    # a translator (can_tune_ai False) gets 403, an admin gets 200. Done LAST so the
    # login wall doesn't disturb the auth-off assertions above.
    security.seed_initial()  # admin/admin, linguist/linguist, user1/user1; auth ON
    gc = TestClient(app)
    assert _login(gc, "user1", "user1").status_code == 303
    r = gc.post("/setup-ai/cloud/models", data={"provider": "claude"}, follow_redirects=False)
    assert r.status_code == 403, f"translator must be blocked, got {r.status_code}"
    sign_out(gc, follow_redirects=False)
    assert _login(gc, "admin", "admin").status_code == 303
    r = gc.post("/setup-ai/cloud/models", data={"provider": "claude"}, follow_redirects=False)
    assert r.status_code == 200 and "models" in r.json(), (r.status_code, r.text[:200])
    print("  POST /setup-ai/cloud/models is _ai_block-gated (translator 403, admin 200)")

    settings.set_many({"security_auth_required": "0"})
    print("CLOUD MODEL FETCH OK")


if __name__ == "__main__":
    main()
