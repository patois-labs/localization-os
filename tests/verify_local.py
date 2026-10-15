"""Local / self-hosted provider verification (offline, no real server).

- 'local' is a first-class provider everywhere (PROVIDERS, new-project form, default select)
- base-URL normalization adds /v1 idempotently
- get_provider / provider_is_live switch on whether a base URL is configured
- model discovery + connection test fail gracefully when the server is unreachable
- run_completion routes to the local provider when selected (and to mock when off)
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient

from app import config
from app.db import init_db
from app.main import app
from app.services import settings
from app.services.providers import (
    LocalProvider,
    MockProvider,
    ProviderSpec,
    _normalize_local_base,
    get_provider,
    local_list_models,
    provider_is_live,
    run_completion,
)

UNREACHABLE = "http://127.0.0.1:9"  # discard port — refuses fast


def main() -> None:
    init_db()
    c = TestClient(app)

    # A shipped install defaults local_base_url to EMPTY (never a LAN address).
    # Fresh DB: no saved value, so the registry default is what an install sees. An empty URL means
    # "not configured": the engine is not live, discovery returns nothing WITHOUT any network call,
    # the Setup AI card says "not configured", and no shipped page carries the old literal.
    import time

    assert settings.DEFAULTS["local_base_url"] == "", settings.DEFAULTS["local_base_url"]
    assert (settings.get("local_base_url") or "") == "", settings.get("local_base_url")
    assert not config.LOCAL_BASE_URL, config.LOCAL_BASE_URL
    assert provider_is_live("local") is False, "empty local_base_url must not read as a live engine"
    _t0 = time.monotonic()
    assert local_list_models() == [], "empty base must list no models"
    assert local_list_models("") == [], "explicit blank base must list no models"
    assert time.monotonic() - _t0 < 2, "an empty base URL must never be probed (8 s connect delay)"
    _sa = c.get("/setup-ai")
    assert "not configured" in _sa.text, "Setup AI local card must say not configured"
    for _page in ("/setup-ai", "/help"):
        assert "192.168.1.50" not in c.get(_page).text, f"{_page} still ships a LAN address"
    _tr = c.post("/setup-ai/local/test", data={"base_url": "", "api_key": "", "local_model": ""})
    assert _tr.status_code == 200 and _tr.json().get("ok") is False
    assert "not configured" in _tr.json().get("error", ""), _tr.text
    print("  shipped default local_base_url is empty: engine not configured, no probe")

    # 'local' is registered everywhere
    assert "local" in config.PROVIDERS, config.PROVIDERS
    assert 'value="local"' in c.get("/projects").text, "new-project form missing local option"
    assert "self-hosted" in c.get("/setup-ai").text.lower(), "setup-ai missing local card"
    print("  'local' is a first-class provider (PROVIDERS + forms + setup-ai)")

    # base normalization. Regression for the "\v1"-vs-"/v1" escape bug: the
    # endswith() guard must see a real "/v1" so a base that already ends in /v1 is
    # NEVER doubled to /v1/v1 (a vertical-tab "\v1" made the guard never match and
    # ALWAYS append, breaking LM Studio / local engines whose base already has /v1).
    assert _normalize_local_base("http://192.168.1.50:1234") == "http://192.168.1.50:1234/v1"
    assert _normalize_local_base("http://x:1234/v1/") == "http://x:1234/v1"  # trailing slash
    assert (
        _normalize_local_base("http://x:1234/v1") == "http://x:1234/v1"
    )  # no trailing slash: NOT doubled
    assert (
        _normalize_local_base("http://host:1234/v1") == "http://host:1234/v1"
    )  # the exact bug case
    assert (
        _normalize_local_base("HTTPS://API.DEEPSEEK.COM/V1") == "HTTPS://API.DEEPSEEK.COM/V1"
    )  # case-insensitive, not doubled
    assert (
        _normalize_local_base("http://bare:1234") == "http://bare:1234/v1"
    )  # bare base still gets /v1
    assert _normalize_local_base("") == ""
    print("  base-URL normalization adds /v1 idempotently")

    # off -> mock
    config.LOCAL_BASE_URL = ""
    assert provider_is_live("local") is False
    assert isinstance(get_provider("local"), MockProvider)
    off = run_completion(
        ProviderSpec(provider="local"),
        "x",
        task="translate",
        payload={"text": "Hello", "target": "fr-FR", "glossary": []},
    )
    assert off["text"], "with no local server, should fall back to mock output"
    print("  local OFF -> mock fallback")

    # configured -> LocalProvider selected
    config.LOCAL_BASE_URL = UNREACHABLE
    assert provider_is_live("local") is True
    assert isinstance(get_provider("local"), LocalProvider)
    print("  local CONFIGURED -> LocalProvider selected")

    # discovery + test fail gracefully when unreachable
    assert local_list_models(UNREACHABLE, "lm-studio") == [], "unreachable should give []"
    r = c.post("/setup-ai/local/models", data={"base_url": UNREACHABLE, "api_key": "lm-studio"})
    assert r.json()["models"] == []
    r = c.post(
        "/setup-ai/local/test",
        data={"base_url": UNREACHABLE, "api_key": "lm-studio", "local_model": "qwen3-vl-30b"},
    )
    assert r.json()["ok"] is False and r.json()["error"], r.json()
    print("  model fetch + connection test degrade gracefully when unreachable")

    # saving local settings applies to config (via settings.apply_to_config)
    r = c.post(
        "/setup-ai/local",
        data={"base_url": UNREACHABLE, "api_key": "lm-studio", "local_model": "glm-4.7-flash"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert config.LOCAL_BASE_URL == UNREACHABLE
    assert config.DEFAULT_MODELS["local"] == "glm-4.7-flash"
    assert "glm-4.7-flash" in c.get("/setup-ai").text
    print("  saving local engine persists + applies to runtime config")

    # default provider can be set to local
    c.post(
        "/setup-ai",
        data={
            "default_provider": "local",
            "model_claude": "claude-haiku-4-5",
            "model_gemini": "g",
            "model_openai": "o",
        },
    )
    assert config.DEFAULT_PROVIDER == "local"
    print("  default provider can be set to local (self-hosted)")

    # reset so we don't leave the test pointing at a dead server in this process
    settings.set_many({"local_base_url": "", "default_provider": "claude"})
    print("LOCAL OK")


if __name__ == "__main__":
    main()
